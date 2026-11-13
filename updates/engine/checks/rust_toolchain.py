"""rust-toolchain: the current stable Rust, from the channel manifest."""

from __future__ import annotations

import re
import tomllib

from .. import UpdateError, versions
from . import Latest

CHANNEL_URL = "https://static.rust-lang.org/dist/channel-rust-stable.toml"


def label(check: dict) -> str:
    return "Rust"


def validate_current(current: str) -> None:
    versions.numeric(current)


def latest(check: dict, current: str, http) -> Latest:
    try:
        channel = tomllib.loads(http.text(CHANNEL_URL))
    except tomllib.TOMLDecodeError as e:
        raise UpdateError(f"{CHANNEL_URL} isn't TOML: {e}") from e
    try:
        full = channel["pkg"]["rust"]["version"]  # "1.100.0 (0ef1bc386 2026-11-09)"
    except (KeyError, TypeError) as e:
        raise UpdateError(f"{CHANNEL_URL} has no pkg.rust.version") from e
    m = re.match(r"(\d+\.\d+\.\d+)\b", full)
    if not m:
        raise UpdateError(f"unexpected Rust version {full!r} in {CHANNEL_URL}")
    v = m.group(1)
    return Latest(
        version=v,
        source=f"the stable channel, `{CHANNEL_URL}`",
        release_url=f"https://github.com/rust-lang/rust/releases/tag/{v}",
        links=["Release notes: https://github.com/rust-lang/rust/blob/main/RELEASES.md"],
    )


def newer(check: dict, new: Latest, current: str, http) -> bool:
    return versions.newer(new.version, current)
