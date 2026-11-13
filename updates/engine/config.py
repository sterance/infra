"""`.github/updates.yml`: loading and validation.

Every problem is reported at once, each with where it is
(`checks[2].edits[0].pattern: …`), so a bad config fails the plan job with
a message that says what to fix.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import UpdateError, yamlio
from .edits import Edit

CONFIG_PATH = ".github/updates.yml"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$|^[a-z0-9]$")
RESERVED_NAMES = {"dependabot-config", "failure"}
ECOSYSTEM = re.compile(r"^[a-z][a-z0-9-]*$")

COMMON_KEYS = {"name", "type", "mode", "notes", "edits"}
# type -> {key: (required, python type)}
TYPES: dict[str, dict[str, tuple[bool, type]]] = {
    "rust-toolchain": {},
    "node-lts": {},
    "chocolatey": {"package": (True, str)},
    "debian-stable": {},
    "github-release": {"repo": (True, str), "tag_pattern": (False, str), "prerelease": (False, bool)},
    "docker-tag": {"image": (True, str), "tag_pattern": (False, str)},
    "custom": {"run": (True, str)},
}
EDIT_KEYS = {"file", "pattern", "count", "docker_image", "asset"}
# Keys an entry under `dependabot:` may set, passed through to dependabot.yml.
# (Not `schedule`: every repo uses updates/defaults.yml's.)
DEPENDABOT_KEYS = {
    "directory",
    "enabled",
    "allow",
    "ignore",
    "groups",
    "labels",
    "assignees",
    "milestone",
    "commit-message",
    "open-pull-requests-limit",
    "rebase-strategy",
    "versioning-strategy",
    "vendor",
    "exclude-paths",
    "insecure-external-code-execution",
    "registries",
    "target-branch",
    "pull-request-branch-name",
}
DEFAULT_TAG_PATTERN = r"^v?(?P<version>\d+(?:\.\d+)*)$"


class ConfigError(UpdateError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(f"{CONFIG_PATH} is invalid:\n" + "\n".join(f"  - {p}" for p in problems))


def load(root: Path, path: str = CONFIG_PATH) -> dict:
    """The validated, normalised config; a missing file is an empty one."""
    f = root / path
    if not f.exists():
        return validate({"version": 1})
    return validate(yamlio.load(f))


def validate(raw) -> dict:
    p: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError(["the file must be a mapping with `version: 1`"])
    for k in sorted(set(raw) - {"version", "dependabot", "checks"}):
        p.append(f"{k}: unknown key (expected version, dependabot, checks)")
    if raw.get("version") != 1:
        p.append("version: must be 1")

    dependabot = _dependabot(raw.get("dependabot"), p)
    checks = _checks(raw.get("checks"), p)
    if p:
        raise ConfigError(p)
    return {"version": 1, "dependabot": dependabot, "checks": checks}


def _dependabot(d, p: list[str]) -> dict:
    if d is None:
        return {}
    if not isinstance(d, dict):
        p.append("dependabot: must be a mapping of ecosystem -> list of entries")
        return {}
    out = {}
    for eco, entries in d.items():
        where = f"dependabot.{eco}"
        if not isinstance(eco, str) or not ECOSYSTEM.match(eco):
            p.append(f"{where}: not a Dependabot package-ecosystem name")
            continue
        if not isinstance(entries, list) or not entries:
            p.append(f"{where}: must be a non-empty list of entries (each with a `directory`)")
            continue
        seen = set()
        clean = []
        for i, e in enumerate(entries):
            w = f"{where}[{i}]"
            if not isinstance(e, dict):
                p.append(f"{w}: must be a mapping")
                continue
            for k in sorted(set(e) - DEPENDABOT_KEYS):
                hint = " (the schedule is the same for every repo: updates/defaults.yml in infra)" if k == "schedule" else ""
                p.append(f"{w}.{k}: not allowed here{hint}")
            directory = e.get("directory")
            if not isinstance(directory, str) or not directory.startswith("/"):
                p.append(f"{w}.directory: required, a path starting with / (e.g. /frontend)")
                continue
            directory = "/" + directory.strip("/") if directory != "/" else "/"
            if directory in seen:
                p.append(f"{w}.directory: {directory} is listed twice")
            seen.add(directory)
            if "enabled" in e and not isinstance(e["enabled"], bool):
                p.append(f"{w}.enabled: must be true or false")
            clean.append({**e, "directory": directory})
        out[eco] = clean
    return out


def _checks(checks, p: list[str]) -> list[dict]:
    if checks is None:
        return []
    if not isinstance(checks, list):
        p.append("checks: must be a list")
        return []
    out, names = [], set()
    for i, c in enumerate(checks):
        where = f"checks[{i}]"
        if not isinstance(c, dict):
            p.append(f"{where}: must be a mapping")
            continue
        name = c.get("name")
        if isinstance(name, str):
            where = f"checks[{i}] ({name})"
        if not isinstance(name, str) or not NAME.match(name):
            p.append(f"{where}.name: required: lower-case letters, digits and dashes (it names the branch updates/<name>)")
        elif name in RESERVED_NAMES:
            p.append(f"{where}.name: {name!r} is reserved")
        elif name in names:
            p.append(f"{where}.name: {name!r} is used twice")
        names.add(name)

        ctype = c.get("type")
        if ctype not in TYPES:
            p.append(f"{where}.type: one of {', '.join(TYPES)} (got {ctype!r})")
            continue
        spec = TYPES[ctype]
        for k in sorted(set(c) - COMMON_KEYS - set(spec)):
            p.append(f"{where}.{k}: unknown key for type {ctype}")
        for k, (required, typ) in spec.items():
            if k not in c:
                if required:
                    p.append(f"{where}.{k}: required for type {ctype}")
            elif not isinstance(c[k], typ) or (typ is str and not c[k].strip()):
                p.append(f"{where}.{k}: must be a {'string' if typ is str else 'boolean'}")

        mode = c.get("mode", "pr")
        if ctype == "custom" and "mode" in c:
            p.append(f"{where}.mode: a custom check chooses pr or issue itself, in result.json")
        elif mode not in ("pr", "issue"):
            p.append(f"{where}.mode: pr or issue")
        if "notes" in c and not isinstance(c["notes"], str):
            p.append(f"{where}.notes: must be a string")

        norm = {k: v for k, v in c.items() if k != "edits"}
        norm["mode"] = None if ctype == "custom" else mode
        if ctype == "github-release":
            norm.setdefault("prerelease", False)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(c.get("repo", ""))):
                p.append(f"{where}.repo: owner/name of a GitHub repository")
        if ctype in ("github-release", "docker-tag"):
            tp = c.get("tag_pattern", DEFAULT_TAG_PATTERN)
            try:
                if "version" not in re.compile(str(tp)).groupindex:
                    p.append(f"{where}.tag_pattern: needs a (?P<version>…) group")
            except re.error as e:
                p.append(f"{where}.tag_pattern: invalid regular expression: {e}")
            norm["tag_pattern"] = tp

        if ctype == "custom":
            if "edits" in c:
                p.append(f"{where}.edits: a custom check edits files itself")
            norm["edits"] = []
        else:
            norm["edits"] = _edits(c.get("edits"), ctype, f"{where}.edits", p)
        out.append(norm)
    return out


def _edits(edits, ctype: str, where: str, p: list[str]) -> list[dict]:
    if not isinstance(edits, list) or not edits:
        p.append(f"{where}: required, a non-empty list of {{file, pattern}}")
        return []
    out = []
    has_version = False
    for i, e in enumerate(edits):
        w = f"{where}[{i}]"
        if not isinstance(e, dict):
            p.append(f"{w}: must be a mapping with file and pattern")
            continue
        for k in sorted(set(e) - EDIT_KEYS):
            p.append(f"{w}.{k}: unknown key (expected {', '.join(sorted(EDIT_KEYS))})")
        if not isinstance(e.get("file"), str) or not isinstance(e.get("pattern"), str):
            p.append(f"{w}: file and pattern are required strings")
            continue
        count = e.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            p.append(f"{w}.count: a whole number, at least 1")
            continue
        try:
            edit = Edit.from_config(e)
        except UpdateError as err:
            p.append(f"{w}: {err}")
            continue
        g = edit.groups
        if not g & {"version", "sha256", "size"}:
            p.append(f"{w}.pattern: needs a (?P<version>…) group (or sha256/size with an asset)")
        has_version |= "version" in g
        if g & {"sha256", "size"}:
            if ctype != "github-release":
                p.append(f"{w}.pattern: sha256/size groups are filled in from release assets: github-release only")
            elif not e.get("asset"):
                p.append(f"{w}.asset: required when the pattern has a sha256 or size group")
        if e.get("asset") is not None:
            if ctype != "github-release":
                p.append(f"{w}.asset: github-release only")
            elif not isinstance(e["asset"], str) or not g & {"sha256", "size"}:
                p.append(f"{w}.asset: an asset name, for a pattern with a sha256 or size group")
        if e.get("docker_image") is not None and not isinstance(e["docker_image"], str):
            p.append(f"{w}.docker_image: an image name, e.g. library/rust or ghcr.io/owner/image")
        out.append({"file": e["file"], "pattern": e["pattern"], "count": count,
                    **{k: e[k] for k in ("docker_image", "asset") if e.get(k) is not None}})
    if out and not has_version:
        p.append(f"{where}: at least one edit needs a (?P<version>…) group")
    return out


def find_check(config: dict, name: str) -> dict:
    for c in config["checks"]:
        if c["name"] == name:
            return c
    raise UpdateError(f"no check named {name!r} in {CONFIG_PATH}")
