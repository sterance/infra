"""Edits: the one generic way a check reads and rewrites a pin.

Each edit is a file and a regular expression (multi-line mode). The pattern
captures the pinned value in named groups:

- `version`: the pinned version. Every edit of a check that has one must
  agree on it, which is how the check reads the current version.
- `sha256`, `size`: filled in from a release asset (github-release checks).
- `tag`: optional, the image tag `docker_image` verifies (see `image_tag`).

Each pattern must match exactly `count` times (default 1), or the check
fails saying the pin has moved. Only the named groups are replaced; the rest
of each match stays as it is.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import UpdateError

VALUE_GROUPS = ("version", "sha256", "size")
WORKFLOWS = ".github/workflows/"


def workflow_path(path: str) -> bool:
    p = PurePosixPath(path)
    return len(p.parts) >= 2 and p.parts[0] == ".github" and p.parts[1] == "workflows"


def check_path(path: str) -> None:
    """Refuses paths outside the repo and workflow files."""
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts or "\\" in path:
        raise UpdateError(f"{path!r} must be a relative path inside the repository")
    if p.parts[0] == ".git":
        raise UpdateError(f"{path!r} is inside .git")
    if workflow_path(path):
        raise UpdateError(
            f"{path}: checks never edit {WORKFLOWS} (the update app has no Workflows permission, on purpose). "
            "Move the pin into a data file the workflow reads (e.g. a version file or pins.env) and point the "
            "edit at that file"
        )


@dataclass
class Edit:
    file: str
    pattern: str
    count: int = 1
    docker_image: str | None = None
    asset: str | None = None

    def __post_init__(self):
        check_path(self.file)
        try:
            self.regex = re.compile(self.pattern, re.MULTILINE)
        except re.error as e:
            raise UpdateError(f"{self.file}: /{self.pattern}/ isn't a valid regular expression: {e}") from e

    @property
    def groups(self) -> set[str]:
        return set(self.regex.groupindex)

    @classmethod
    def from_config(cls, d: dict) -> "Edit":
        return cls(
            file=d["file"],
            pattern=d["pattern"],
            count=d.get("count", 1),
            docker_image=d.get("docker_image"),
            asset=d.get("asset"),
        )


@dataclass
class Changes:
    """New file contents, keyed by repo-relative path."""

    root: Path
    files: dict[str, str] = field(default_factory=dict)

    def read(self, path: str) -> str:
        check_path(path)
        if path in self.files:
            return self.files[path]
        p = self.root / path
        if p.is_symlink():
            raise UpdateError(f"{path} is a symbolic link; point the edit at the real file")
        try:
            return p.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise UpdateError(f"{path} doesn't exist (has the pin moved? update .github/updates.yml)") from e

    def set(self, path: str, text: str) -> None:
        check_path(path)
        self.files[path] = text

    def _disk(self, path: str) -> str:
        p = self.root / path
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def changed(self) -> dict[str, str]:
        return {p: t for p, t in self.files.items() if self._disk(p) != t}

    def diff(self) -> str:
        chunks = []
        for path, text in sorted(self.changed().items()):
            chunks.extend(
                difflib.unified_diff(
                    self._disk(path).splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
        return "".join(chunks)

    def write(self) -> list[str]:
        written = []
        for path, text in sorted(self.changed().items()):
            (self.root / path).write_text(text, encoding="utf-8")
            written.append(path)
        return written


def matches(changes: Changes, edit: Edit) -> list[re.Match]:
    found = list(edit.regex.finditer(changes.read(edit.file)))
    if len(found) != edit.count:
        raise UpdateError(
            f"{edit.file}: expected /{edit.pattern}/ {edit.count} time(s), found it {len(found)} time(s); "
            "the pin has moved or changed shape, so update .github/updates.yml"
        )
    return found


def current_version(changes: Changes, edits: list[Edit], what: str) -> str:
    """The version every edit agrees on."""
    seen: dict[str, list[str]] = {}
    for e in edits:
        for m in matches(changes, e):
            if "version" in e.groups:
                seen.setdefault(m.group("version"), []).append(e.file)
    if not seen:
        raise UpdateError(f"{what}: no edit has a `version` group")
    if len(seen) > 1:
        where = "; ".join(f"{v} in {', '.join(sorted(set(files)))}" for v, files in sorted(seen.items()))
        raise UpdateError(f"{what} disagrees with itself: {where}")
    return next(iter(seen))


def _substitute(text: str, m: re.Match, values: dict[str, str], start: int, end: int) -> str:
    """`text[start:end]` (a span inside match `m`) with the value groups
    replaced."""
    spans = []
    for g in VALUE_GROUPS:
        if g in values and g in m.re.groupindex and m.start(g) != -1:
            s, e = m.span(g)
            if start <= s and e <= end:
                spans.append((s, e, values[g]))
    out, pos = [], start
    for s, e, v in sorted(spans):
        if s < pos:
            raise UpdateError(f"/{m.re.pattern}/: the {', '.join(VALUE_GROUPS)} groups overlap")
        out.append(text[pos:s])
        out.append(v)
        pos = e
    out.append(text[pos:end])
    return "".join(out)


def apply(changes: Changes, edit: Edit, values: dict[str, str]) -> list[str]:
    """Rewrites every match of `edit`, returning each match's new text."""
    text = changes.read(edit.file)
    found = matches(changes, edit)
    pieces, new_matches, pos = [], [], 0
    for m in found:
        pieces.append(text[pos : m.start()])
        new = _substitute(text, m, values, m.start(), m.end())
        pieces.append(new)
        new_matches.append((m, new))
        pos = m.end()
    pieces.append(text[pos:])
    changes.set(edit.file, "".join(pieces))
    return [image_tag(m, new, values, text) for m, new in new_matches]


_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def image_tag(m: re.Match, new_match: str, values: dict[str, str], text: str) -> str:
    """The image tag a rewritten match names: its `tag` group if the pattern
    has one; else the text after the last ':' of the match
    (`rust:1.2.3-slim-trixie` -> `1.2.3-slim-trixie`); else the version."""
    if "tag" in m.re.groupindex and m.start("tag") != -1:
        return _substitute(text, m, values, *m.span("tag"))
    if ":" in new_match:
        t = _TAG.match(new_match.rsplit(":", 1)[1])
        if t:
            return t.group(0)
    return values.get("version", "")
