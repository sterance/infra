"""node-lts: the newest LTS major of Node.js. Majors only: minor and patch
releases float within a pinned major (`node:24-alpine`, an `.nvmrc` of
`24`)."""

from __future__ import annotations

from .. import UpdateError
from . import Latest

INDEX_URL = "https://nodejs.org/dist/index.json"


def label(check: dict) -> str:
    return "Node.js"


def validate_current(current: str) -> None:
    if not current.isdigit():
        raise UpdateError(f"node-lts pins a major version (e.g. 24), not {current!r}")


def newest_lts(index) -> tuple[int, str, str]:
    """(major, full version, LTS codename) of the newest LTS release."""
    best = None
    for release in index if isinstance(index, list) else []:
        if not release.get("lts"):
            continue
        version = str(release.get("version", "")).lstrip("v")
        try:
            key = tuple(int(p) for p in version.split("."))
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, version, release["lts"])
    if best is None:
        raise UpdateError(f"{INDEX_URL} lists no LTS release")
    return best[0][0], best[1], best[2]


def latest(check: dict, current: str, http) -> Latest:
    major, full, codename = newest_lts(http.json(INDEX_URL))
    return Latest(
        version=str(major),
        source=f'`{INDEX_URL}` (LTS "{codename}", latest {full})',
        release_url=f"https://nodejs.org/en/blog/release/v{full}",
        links=[f"Changelog for the major: https://github.com/nodejs/node/blob/main/doc/changelogs/CHANGELOG_V{major}.md"],
    )


def newer(check: dict, new: Latest, current: str, http) -> bool:
    return int(new.version) > int(current)
