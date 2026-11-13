"""YAML in (via yq or PyYAML) and a small, deterministic YAML writer for the
generated Dependabot config."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import UpdateError


def load(path: Path):
    """Parses a YAML file into plain Python values."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise UpdateError(f"{path} doesn't exist") from e
    return loads(text, str(path))


def loads(text: str, name: str = "<yaml>"):
    if not text.strip():
        return None
    if shutil.which("yq"):
        # mikefarah/yq v4 (GitHub's runners): YAML 1.2, so `on`/`yes` stay strings.
        r = subprocess.run(["yq", "-o=json", "."], input=text, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise UpdateError(f"{name} isn't valid YAML: {r.stderr.strip()}")
        return json.loads(r.stdout) if r.stdout.strip() else None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as e:
        raise UpdateError("reading YAML needs `yq` (mikefarah/yq v4) or PyYAML (pip install pyyaml)") from e
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise UpdateError(f"{name} isn't valid YAML: {e}") from e


# ---------------- writing ----------------

_PLAIN = re.compile(r"^[A-Za-z_/][A-Za-z0-9_./+-]*$")
_RESERVED = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~"}


def scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if v is None:
        return "null"
    s = str(v)
    if _PLAIN.match(s) and s.lower() not in _RESERVED:
        return s
    return json.dumps(s, ensure_ascii=False)  # a valid YAML double-quoted string


def dump(value, indent: int = 0) -> str:
    """Block-style YAML for dicts, lists and scalars, keys in the given order."""
    return "".join(_lines(value, indent))


def _lines(value, indent: int):
    pad = " " * indent
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                yield f"{pad}{scalar(k)}:\n"
                yield from _lines(v, indent + 2 if isinstance(v, dict) else indent + 2)
            else:
                yield f"{pad}{scalar(k)}: {_empty_or_scalar(v)}\n"
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                first = True
                for line in _lines(item, indent + 2):
                    if first:
                        yield f"{pad}- {line[indent + 2:]}"
                        first = False
                    else:
                        yield line
            elif isinstance(item, list) and item:
                yield f"{pad}-\n"
                yield from _lines(item, indent + 2)
            else:
                yield f"{pad}- {_empty_or_scalar(item)}\n"
    else:
        yield f"{pad}{scalar(value)}\n"


def _empty_or_scalar(v) -> str:
    if isinstance(v, dict):
        return "{}"
    if isinstance(v, list):
        return "[]"
    return scalar(v)
