"""github-release: the highest version among a GitHub repository's
releases.

Tags are matched against `tag_pattern` (default `v1.2.3` or `1.2.3`), whose
`version` group is the version. Drafts are skipped, and prereleases unless
`prerelease: true`. Edits with an `asset` get that file's SHA-256 (and size)
from the release API's digest, checked against a download.
"""

from __future__ import annotations

import re

from .. import UpdateError, versions
from ..config import DEFAULT_TAG_PATTERN
from . import Latest

API = "https://api.github.com/repos"


def label(check: dict) -> str:
    return check["repo"]


def validate_current(current: str) -> None:
    versions.numeric(current)


def _candidates(check: dict, releases) -> list[tuple[str, dict]]:
    pattern = re.compile(check.get("tag_pattern") or DEFAULT_TAG_PATTERN)
    out = []
    for r in releases if isinstance(releases, list) else []:
        if r.get("draft") or (r.get("prerelease") and not check.get("prerelease")):
            continue
        m = pattern.fullmatch(str(r.get("tag_name", "")))
        if m and versions.is_numeric(m.group("version")):
            out.append((m.group("version"), r))
    return out


def latest(check: dict, current: str, http) -> Latest:
    repo = check["repo"]
    # The newest 100 releases (by date); the highest version among them.
    releases = http.json(f"{API}/{repo}/releases?per_page=100")
    found = _candidates(check, releases)
    if not found:
        raise UpdateError(f"no release of {repo} matches /{check.get('tag_pattern') or DEFAULT_TAG_PATTERN}/ among the newest 100")
    version, release = max(found, key=lambda vr: versions.numeric(vr[0]))
    tag = release["tag_name"]
    old_tag = next((r["tag_name"] for v, r in found if v == current), None)
    if old_tag is None and version in tag:
        old_tag = tag.replace(version, current)
    assets = {}
    for a in release.get("assets") or []:
        digest = a.get("digest") or ""
        assets[a["name"]] = {
            "sha256": digest.removeprefix("sha256:") if digest.startswith("sha256:") else None,
            "size": a.get("size"),
            "url": a.get("browser_download_url") or f"https://github.com/{repo}/releases/download/{tag}/{a['name']}",
        }
    return Latest(
        version=version,
        source=f"the releases of [{repo}](https://github.com/{repo}/releases)",
        release_url=release.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}",
        compare_url=f"https://github.com/{repo}/compare/{old_tag}...{tag}" if old_tag else None,
        tag=tag,
        assets=assets,
    )


def newer(check: dict, new: Latest, current: str, http) -> bool:
    return versions.newer(new.version, current)
