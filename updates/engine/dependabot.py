"""The generated `.github/dependabot.yml`.

Ecosystems are detected from the repository's files, then the `dependabot:`
entries of `.github/updates.yml` are merged in. The schedule and grouping
are infra's (updates/defaults.yml), the same for every repo. The `docker`
ecosystem is never detected: image versions often have to move together
with other files, so add it per directory only where that isn't so.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from . import UpdateError, yamlio

PATH = ".github/dependabot.yml"
DEFAULTS = Path(__file__).resolve().parents[1] / "defaults.yml"
ORDER = ["cargo", "npm", "pip", "gomod", "github-actions"]
SKIP_DIRS = {"node_modules", "vendor", "target", ".git", "third_party"}
LOCKFILES = {
    "Cargo.lock": "cargo",
    "package-lock.json": "npm",
    "yarn.lock": "npm",
    "pnpm-lock.yaml": "npm",
    "go.sum": "gomod",
    "pyproject.toml": "pip",
}
REQUIREMENTS = re.compile(r"^requirements.*\.txt$")

HEADER = """\
# Managed by infra: generated from .github/updates.yml by the weekly dependency
# check. Edit .github/updates.yml (its `dependabot:` section) instead: changes
# made here are replaced by the next sync PR (branch updates/dependabot-config).
#
# Ecosystems are detected from the repository's files. The schedule and the
# grouping (minor and patch updates in one PR per ecosystem) are infra's
# updates/defaults.yml. The docker ecosystem is never added automatically.
"""


def load_defaults(path: Path = DEFAULTS) -> dict:
    d = yamlio.load(path)
    if not isinstance(d, dict) or not isinstance(d.get("schedule"), dict):
        raise UpdateError(f"{path} needs a schedule")
    return d


def detect(paths: list[str]) -> dict[str, set[str]]:
    """ecosystem -> directories ("/", "/frontend")."""
    found: dict[str, set[str]] = {}
    for path in paths:
        p = PurePosixPath(path)
        if any(part in SKIP_DIRS for part in p.parts[:-1]):
            continue
        directory = "/" + "/".join(p.parts[:-1])
        if len(p.parts) == 3 and p.parts[:2] == (".github", "workflows") and p.suffix in (".yml", ".yaml"):
            found.setdefault("github-actions", set()).add("/")
            continue
        eco = LOCKFILES.get(p.name) or ("pip" if REQUIREMENTS.match(p.name) else None)
        if eco:
            found.setdefault(eco, set()).add(directory)
    return found


def render(paths: list[str], config: dict, defaults: dict) -> str:
    detected = detect(paths)
    additions = config.get("dependabot", {})
    ecos = sorted(set(detected) | set(additions), key=lambda e: (ORDER.index(e) if e in ORDER else len(ORDER), e))
    updates = []
    for eco in ecos:
        plain = set(detected.get(eco, set()))
        special = []
        for entry in additions.get(eco, []):
            d = entry["directory"]
            if entry.get("enabled") is False:
                plain.discard(d)
                continue
            extra = {k: v for k, v in entry.items() if k not in ("directory", "enabled")}
            if extra:
                plain.discard(d)
                special.append((d, extra))
            else:
                plain.add(d)
        if plain:
            where = {"directory": next(iter(plain))} if len(plain) == 1 else {"directories": sorted(plain)}
            updates.append(_entry(eco, where, {}, defaults))
        for d, extra in sorted(special, key=lambda s: s[0]):
            updates.append(_entry(eco, {"directory": d}, extra, defaults))
    body = {"version": 2, "updates": updates}
    return HEADER + yamlio.dump(body)


def _entry(eco: str, where: dict, extra: dict, defaults: dict) -> dict:
    e = {"package-ecosystem": eco, **where, "schedule": dict(defaults["schedule"])}
    if "groups" not in extra:
        e["groups"] = {f"{eco}-minor-and-patch": {"update-types": list(defaults.get("group_update_types", ["minor", "patch"]))}}
    e.update(extra)
    return e
