"""debian-stable: Debian's stable release, by codename. The pin is a
codename (`bookworm`); codenames are compared by their Debian version, read
from the mirror's Release files."""

from __future__ import annotations

import re

from .. import UpdateError
from ..http import NotFound
from . import Latest

DISTS = "https://deb.debian.org/debian/dists"


def label(check: dict) -> str:
    return "Debian"


def validate_current(current: str) -> None:
    if not re.fullmatch(r"[a-z]+", current):
        raise UpdateError(f"debian-stable pins a codename (e.g. trixie), not {current!r}")


def release_info(suite: str, http) -> tuple[str, int] | None:
    """(codename, major version) of a suite ("stable" or a codename), or None
    if the mirror no longer has it."""
    try:
        text = http.text(f"{DISTS}/{suite}/Release")
    except NotFound:
        return None
    codename = re.search(r"^Codename:\s*(\S+)", text, re.MULTILINE)
    version = re.search(r"^Version:\s*(\d+)", text, re.MULTILINE)
    if not codename or not version:
        raise UpdateError(f"{DISTS}/{suite}/Release has no Codename or Version")
    return codename.group(1), int(version.group(1))


def latest(check: dict, current: str, http) -> Latest:
    stable = release_info("stable", http)
    if stable is None:
        raise UpdateError(f"{DISTS}/stable/Release is missing")
    codename, major = stable
    return Latest(
        version=codename,
        source=f"`{DISTS}/stable/Release` (Debian {major})",
        release_url=f"https://www.debian.org/releases/{codename}/",
        links=[f"Release notes: https://www.debian.org/releases/{codename}/releasenotes"],
    )


def newer(check: dict, new: Latest, current: str, http) -> bool:
    if new.version == current:
        return False
    stable = release_info(new.version, http)
    mine = release_info(current, http)
    if stable is None:
        raise UpdateError(f"{DISTS}/{new.version}/Release is missing")
    # Gone from the mirror: older than oldstable.
    return mine is None or mine[1] < stable[1]
