"""Built-in check types. Each knows only how to find the latest version and
compare it with the pinned one; reading and rewriting pins is shared
(engine/edits.py), and so is the PR text (engine/run.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Latest:
    version: str
    source: str  # where it came from, for the PR body
    release_url: str | None = None
    compare_url: str | None = None
    links: list[str] = field(default_factory=list)  # extra "- …" lines
    tag: str | None = None  # github-release: the release's tag
    assets: dict[str, dict] = field(default_factory=dict)  # name -> {sha256, size, url}


from . import chocolatey, debian_stable, docker_tag, github_release, node_lts, rust_toolchain  # noqa: E402

TYPES = {
    "rust-toolchain": rust_toolchain,
    "node-lts": node_lts,
    "chocolatey": chocolatey,
    "debian-stable": debian_stable,
    "github-release": github_release,
    "docker-tag": docker_tag,
}
