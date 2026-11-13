"""Running one check: the shared flow for every type, the custom-check
contract, the Dependabot sync, and what a check leaves for the publish job.

Outputs (in --out, the check job's artifact):

- result.json: {"name", "result": "none" | "pr" | "issue", "title", "version"}
- body.md: the PR or issue text
- changes.patch: for a PR, `git diff --binary` of the working tree
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import UpdateError, config as configmod, dependabot, edits as editsmod
from .checks import TYPES, Latest
from .edits import Changes, Edit

DEPENDABOT_CHECK = "dependabot-config"
MARKER = "<!-- dependency-update:{name} -->"
FOOTER = (
    "Opened by the weekly dependency check (infra's `dependency-updates.yml`), check `{name}` in "
    "`.github/updates.yml`. **Review and merge by hand: nothing merges automatically.** "
    "Later runs update this {kind} in place."
)


@dataclass
class Outcome:
    name: str
    result: str  # none | pr | issue
    title: str = ""
    version: str = ""
    body: str = ""
    changes: Changes | None = None  # built-in checks
    changed_files: list[str] = field(default_factory=list)
    preview: str = ""  # a diff, for local runs of custom checks


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"warning: {msg}", flush=True)


def files_list(paths) -> str:
    return "\n".join(f"- `{p}`" for p in sorted(set(paths)))


# ---------------- built-in types ----------------


def run_builtin(root: Path, check: dict, http, dry_run: bool) -> Outcome:
    name, mode, kind = check["name"], check["mode"], TYPES[check["type"]]
    edits = [Edit.from_config(e) for e in check["edits"]]
    changes = Changes(root)
    current = editsmod.current_version(changes, edits, f"check {name!r}")
    kind.validate_current(current)
    latest: Latest = kind.latest(check, current, http)
    label = kind.label(check)
    log(f"{name}: {label} pinned {current}, latest {latest.version} (from {latest.source})")
    if not kind.newer(check, latest, current, http):
        log("up to date")
        return Outcome(name, "none", version=current)

    asset_rows = []
    images: dict[tuple[str, str], bool] = {}
    for e in edits:
        values = {"version": latest.version}
        if e.asset:
            values.update(_asset_values(e, latest, dry_run, http, asset_rows))
        for tag in editsmod.apply(changes, e, values):
            if e.docker_image:
                images[(e.docker_image, tag)] = False
    for key in images:
        images[key] = http.image_tag_exists(*key)
    missing = [f"{i}:{t}" for (i, t), ok in images.items() if not ok]
    if missing and mode == "pr":
        # Images usually follow a release within a day; next week's run will find them.
        warn(f"{label} {latest.version} is out but {', '.join(missing)} isn't published yet; not proposing it")
        return Outcome(name, "none", version=current)

    changed = sorted(changes.changed())
    if mode == "pr" and not changed:
        raise UpdateError(f"{name}: {latest.version} is newer than {current}, but the edits change nothing")
    body = _body(check, label, current, latest, changed, images, asset_rows, dry_run, changes, root)
    if mode == "issue":
        title = (
            f"Move from Debian {current} to {latest.version}"
            if check["type"] == "debian-stable"
            else f"{label} {latest.version} is available (pinned: {current})"
        )
        return Outcome(name, "issue", title, latest.version, body)
    shown = f"{latest.version} LTS" if check["type"] == "node-lts" else latest.version
    return Outcome(name, "pr", f"Update {label} to {shown}", latest.version, body, changes, changed)


def _asset_values(e: Edit, latest: Latest, dry_run: bool, http, rows: list) -> dict:
    name = e.asset.replace("{version}", latest.version).replace("{tag}", latest.tag or latest.version)
    a = latest.assets.get(name)
    if a is None:
        raise UpdateError(f"release {latest.tag} has no asset named {name!r} (has: {', '.join(sorted(latest.assets)) or 'none'})")
    if not a.get("sha256"):
        raise UpdateError(f"the release API gives no SHA-256 digest for {name}; not pinning it unverified")
    sha, size = a["sha256"], a.get("size")
    verified = False
    if not dry_run:
        log(f"  downloading {name} ({(size or 0) / 1e6:.0f} MB) to check its hash")
        got_sha, got_size = http.sha256(a["url"])
        if got_sha != sha or (size is not None and got_size != size):
            raise UpdateError(
                f"{name}: the download (sha256 {got_sha}, {got_size} bytes) doesn't match the release API "
                f"(sha256 {sha}, {size} bytes); not pinning it"
            )
        verified = True
    if (name, sha) not in [(r[0], r[1]) for r in rows]:
        rows.append((name, sha, size, verified))
    values = {"sha256": sha}
    if size is not None:
        values["size"] = str(size)
    elif "size" in e.groups:
        raise UpdateError(f"the release API gives no size for {name}")
    return values


def _body(check, label, current, latest: Latest, changed, images, asset_rows, dry_run, changes, root) -> str:
    name, mode = check["name"], check["mode"]
    parts = [MARKER.format(name=name), ""]
    parts.append(f"**{label}: {current} → {latest.version}** (the latest, from {latest.source}).")
    parts.append("")
    links = []
    if latest.release_url:
        links.append(f"Release: {latest.release_url}")
    if latest.compare_url:
        links.append(f"Changes: {latest.compare_url}")
    links += latest.links
    for (image, tag), ok in sorted(images.items()):
        links.append(f"Image `{image}:{tag}` {'exists' if ok else '**is not published yet**'}.")
    parts += [f"- {link}" for link in links]
    if asset_rows:
        how = (
            "Each SHA-256 is the release API's digest, and matched a download of the file."
            if all(r[3] for r in asset_rows)
            else "**Hashes are from the release API only (not downloaded): this was a dry run.**"
        )
        parts += ["", "## Assets", "", how, "", "| File | SHA-256 | Bytes |", "|---|---|---|"]
        parts += [f"| `{n}` | `{s}` | {f'{z:,}' if z is not None else '?'} |" for n, s, z, _ in asset_rows]
    parts.append("")
    if mode == "pr":
        parts += ["Changed:", "", files_list(changed)]
    else:
        parts += [
            "This is an issue rather than a PR (`mode: issue`): the move needs a deliberate change, not a weekly bump.",
            "",
            "Pinned in:",
            "",
            files_list(e["file"] for e in check["edits"]),
        ]
        if check["type"] == "debian-stable":
            mentions = _files_naming(root, current)
            if mentions:
                parts += ["", f"Files that mention `{current}`:", "", files_list(mentions)]
        diff = changes.diff()
        if diff:
            parts += ["", "<details><summary>The pins, moved</summary>", "", "```diff", diff.rstrip(), "```", "", "</details>"]
    if check.get("notes"):
        parts += ["", "## Notes", "", check["notes"].rstrip()]
    parts += ["", "---", FOOTER.format(name=name, kind="PR" if mode == "pr" else "issue"), ""]
    return "\n".join(parts)


def _files_naming(root: Path, word: str) -> list[str]:
    try:
        r = subprocess.run(["git", "grep", "-l", "-w", word], cwd=root, capture_output=True, text=True, check=False)
        return [p for p in r.stdout.splitlines() if p]
    except FileNotFoundError:
        return []


# ---------------- custom checks ----------------


def run_custom(root: Path, check: dict, dry_run: bool, in_place: bool) -> Outcome:
    """Runs `run` from the repo root with UPDATE_OUT (an empty directory) and,
    in dry runs, UPDATE_DRY_RUN=1. Locally (not in place) it runs in a copy
    of the repo, so your working tree is left alone."""
    name = check["name"]
    work = root if in_place else _copy_repo(root)
    out = Path(tempfile.mkdtemp(prefix=f"update-{name}-"))
    env = dict(os.environ, UPDATE_OUT=str(out), UPDATE_CHECK=name, PYTHONDONTWRITEBYTECODE="1")
    env.pop("UPDATE_DRY_RUN", None)
    if dry_run:
        env["UPDATE_DRY_RUN"] = "1"
    before = _snapshot(work) if not in_place else None
    log(f"{name}: running `{check['run']}`")
    r = subprocess.run(["bash", "-c", check["run"]], cwd=work, env=env, check=False)
    if r.returncode != 0:
        raise UpdateError(f"{name}: `{check['run']}` exited with {r.returncode}")
    res = _read_result(out, name)

    if in_place:
        changed = _git_changed(work)
        preview = ""
    else:
        changed, preview = _compare(work, before)
        shutil.rmtree(work, ignore_errors=True)
    for path in changed:
        if editsmod.workflow_path(path):
            raise UpdateError(f"{name}: changed {path}; checks never edit {editsmod.WORKFLOWS} (move the pin into a data file)")
    if res["result"] == "pr" and not changed:
        raise UpdateError(f"{name}: result.json says pr, but no file changed")
    if res["result"] != "pr" and changed:
        raise UpdateError(f"{name}: result.json says {res['result']}, but it changed {', '.join(changed)}")
    body = res["body"]
    if res["result"] != "none":
        body = MARKER.format(name=name) + "\n\n" + body.rstrip() + "\n"
        if check.get("notes"):
            body += "\n## Notes\n\n" + check["notes"].rstrip() + "\n"
        body += "\n---\n" + FOOTER.format(name=name, kind="PR" if res["result"] == "pr" else "issue") + "\n"
    return Outcome(name, res["result"], res["title"], res["version"], body, None, changed, preview)


def _read_result(out: Path, name: str) -> dict:
    try:
        res = json.loads((out / "result.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise UpdateError(f"{name}: wrote no UPDATE_OUT/result.json") from e
    except json.JSONDecodeError as e:
        raise UpdateError(f"{name}: UPDATE_OUT/result.json isn't JSON: {e}") from e
    if not isinstance(res, dict) or res.get("result") not in ("none", "pr", "issue"):
        raise UpdateError(f'{name}: result.json needs "result": "none", "pr" or "issue"')
    title, version = res.get("title", ""), res.get("version", "")
    if not isinstance(title, str) or not isinstance(version, str):
        raise UpdateError(f"{name}: result.json's title and version must be strings")
    body = ""
    if res["result"] != "none":
        if not title.strip():
            raise UpdateError(f"{name}: result.json needs a title for a {res['result']}")
        try:
            body = (out / "body.md").read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise UpdateError(f"{name}: wrote no UPDATE_OUT/body.md") from e
    shutil.rmtree(out, ignore_errors=True)
    return {"result": res["result"], "title": " ".join(title.split())[:200], "version": version.strip()[:100], "body": body}


def _repo_files(root: Path) -> list[str]:
    r = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root, capture_output=True, check=False
    )
    if r.returncode != 0:
        raise UpdateError(f"{root} isn't a git repository")
    return [p for p in r.stdout.decode().split("\0") if p]


def _copy_repo(root: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="update-copy-"))
    for p in _repo_files(root):
        src = root / p
        if src.is_file() or src.is_symlink():
            (tmp / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp / p, follow_symlinks=False)
    return tmp


def _snapshot(root: Path) -> dict[str, bytes]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.relative_to(root).parts and "__pycache__" not in p.parts:
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def _compare(work: Path, before: dict[str, bytes]) -> tuple[list[str], str]:
    import difflib

    after = _snapshot(work)
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    chunks = []
    for p in changed:
        a = before.get(p, b"").decode("utf-8", "replace").splitlines(keepends=True)
        b = after.get(p, b"").decode("utf-8", "replace").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(a, b, fromfile=f"a/{p}", tofile=f"b/{p}"))
    return changed, "".join(chunks)


def _git_changed(root: Path) -> list[str]:
    r = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root, capture_output=True, check=False
    )
    if r.returncode != 0:
        raise UpdateError("git status failed in the checkout")
    out, items = [], r.stdout.decode().split("\0")
    i = 0
    while i < len(items):
        entry = items[i]
        if not entry:
            i += 1
            continue
        status, path = entry[:2], entry[3:]
        out.append(path)
        if "R" in status or "C" in status:  # the next item is the old path
            out.append(items[i + 1])
            i += 1
        i += 1
    return sorted(set(out))


# ---------------- the Dependabot sync ----------------


def run_dependabot(root: Path, config: dict, defaults: dict | None = None) -> Outcome:
    defaults = defaults or dependabot.load_defaults()
    paths = _repo_files(root)
    want = dependabot.render(paths, config, defaults)
    changes = Changes(root)
    p = root / dependabot.PATH
    have = p.read_text(encoding="utf-8") if p.exists() else ""
    if have == want:
        log(f"{dependabot.PATH} is up to date")
        return Outcome(DEPENDABOT_CHECK, "none")
    changes.set(dependabot.PATH, want)
    body = "\n".join(
        [
            MARKER.format(name=DEPENDABOT_CHECK),
            "",
            f"`{dependabot.PATH}` is generated: from the ecosystems detected in this repository, the "
            "`dependabot:` section of `.github/updates.yml`, and infra's shared schedule "
            "(`updates/defaults.yml`). This PR brings it in line"
            + (" (it didn't exist)." if not have else "; a hand edit, a new lockfile or directory, or a changed `updates.yml` usually explains it."),
            "",
            "To change what it contains, edit `.github/updates.yml` rather than this file.",
            "",
            "```diff",
            changes.diff().rstrip(),
            "```",
            "",
            "---",
            FOOTER.format(name=DEPENDABOT_CHECK, kind="PR"),
            "",
        ]
    )
    return Outcome(DEPENDABOT_CHECK, "pr", "Update the generated Dependabot config", "", body, changes, [dependabot.PATH])


# ---------------- one entry point ----------------


def run(root: Path, config: dict, name: str, http, dry_run: bool, in_place: bool) -> Outcome:
    if name == DEPENDABOT_CHECK:
        return run_dependabot(root, config)
    check = configmod.find_check(config, name)
    if check["type"] == "custom":
        return run_custom(root, check, dry_run, in_place)
    return run_builtin(root, check, http, dry_run)


def write_outputs(root: Path, outcome: Outcome, out: Path) -> str:
    """Applies the changes in `root` and leaves result.json, body.md and
    changes.patch in `out`. Returns the patch."""
    out.mkdir(parents=True, exist_ok=True)
    if outcome.changes is not None:
        outcome.changes.write()
    patch = ""
    if outcome.result == "pr":
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        r = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=root, capture_output=True, check=True)
        patch = r.stdout.decode("utf-8", "replace")
        if not patch.strip():
            raise UpdateError(f"{outcome.name}: a PR with an empty patch")
        (out / "changes.patch").write_bytes(r.stdout)
    (out / "result.json").write_text(
        json.dumps({"name": outcome.name, "result": outcome.result, "title": outcome.title, "version": outcome.version}) + "\n",
        encoding="utf-8",
    )
    (out / "body.md").write_text(outcome.body, encoding="utf-8")
    return patch
