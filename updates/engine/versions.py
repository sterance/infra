"""Version comparison."""

from __future__ import annotations

import re

from . import UpdateError

NUMERIC = re.compile(r"\d+(\.\d+)*")


def numeric(v: str) -> tuple[int, ...]:
    """"1.100.0" -> (1, 100, 0). Raises UpdateError for anything else."""
    if not NUMERIC.fullmatch(v):
        raise UpdateError(f"{v!r} isn't a numeric version")
    return tuple(int(p) for p in v.split("."))


def is_numeric(v: str) -> bool:
    return bool(NUMERIC.fullmatch(v))


def newer(new: str, current: str) -> bool:
    """Whether numeric version `new` is above `current` (1.10 > 1.9; 1.0 == 1.0.0)."""
    a, b = numeric(new), numeric(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def highest(versions) -> str:
    return max(versions, key=numeric)
