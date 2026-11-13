"""docker-tag: the highest version among an image's tags (Docker Hub, GHCR,
Quay or any registry with the v2 API), filtered by `tag_pattern`."""

from __future__ import annotations

import re

from .. import UpdateError, versions
from ..config import DEFAULT_TAG_PATTERN
from ..http import split_image
from . import Latest


def label(check: dict) -> str:
    return check["image"]


def validate_current(current: str) -> None:
    versions.numeric(current)


def page_url(image: str) -> str | None:
    registry, repo = split_image(image)
    if registry == "registry-1.docker.io":
        return f"https://hub.docker.com/{'_' if repo.startswith('library/') else 'r'}/{repo.removeprefix('library/')}/tags"
    if registry == "quay.io":
        return f"https://quay.io/repository/{repo}?tab=tags"
    return None


def latest(check: dict, current: str, http) -> Latest:
    image = check["image"]
    pattern = re.compile(check.get("tag_pattern") or DEFAULT_TAG_PATTERN)
    found = []
    for t in http.image_tags(image):
        m = pattern.fullmatch(t)
        if m and versions.is_numeric(m.group("version")):
            found.append(m.group("version"))
    if not found:
        raise UpdateError(f"no tag of {image} matches /{pattern.pattern}/")
    v = versions.highest(found)
    page = page_url(image)
    return Latest(version=v, source=f"the tags of `{image}`", links=[f"Tags: {page}"] if page else [])


def newer(check: dict, new: Latest, current: str, http) -> bool:
    return versions.newer(new.version, current)
