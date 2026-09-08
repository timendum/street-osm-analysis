"""Parse the ``cities`` command's LIKE-pattern file.

Each non-blank, non-comment line is a SQLite ``LIKE`` pattern matched against
street names (``%`` = any run, ``_`` = one char, case-insensitive for ASCII).
A line beginning with ``-`` (minus) is an *exclusion*: its remainder is a
``NOT LIKE`` pattern, so a name matched by any include but also by any exclude
is dropped. Running the patterns lives in :func:`strade.store.read_ways_matching`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Lines starting with this (after stripping) are comments and are skipped.
_COMMENT = "#"
# Lines starting with this (after stripping) are NOT LIKE exclusions.
_EXCLUDE = "-"


class PatternError(Exception):
    """Raised when the pattern file has no usable include pattern; halts the command."""


@dataclass(frozen=True)
class Patterns:
    """The include/exclude LIKE patterns parsed from a pattern file.

    ``includes`` are ORed together (a name matches if it satisfies any); a name
    is kept only if it also satisfies every ``excludes`` as ``NOT LIKE`` (i.e.
    matches none of them). ``includes`` is always non-empty; ``excludes`` may be.
    """

    includes: list[str]
    excludes: list[str] = field(default_factory=list)


def parse_pattern_file(path: Path) -> Patterns:
    """Parse a LIKE-pattern file into include/exclude patterns, in file order.

    Skips blank and ``#`` comment lines, trims whitespace, and keeps duplicates.
    A line whose first non-space character is ``-`` becomes an exclusion: the
    ``-`` and any following whitespace are stripped and the remainder is used as
    a ``NOT LIKE`` pattern. All other lines are include (``LIKE``) patterns.

    Raises:
        PatternError: if no usable include pattern remains (file empty, all
            comments, or only exclusions).
    """
    includes: list[str] = []
    excludes: list[str] = []
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(_COMMENT):
            continue
        if line.startswith(_EXCLUDE):
            pattern = line[len(_EXCLUDE) :].strip()
            if pattern:
                excludes.append(pattern)
            continue
        includes.append(line)
    if not includes:
        raise PatternError(
            f"{path}: no LIKE include patterns found "
            f"(file is empty, all comments, or only exclusions)"
        )
    return Patterns(includes=includes, excludes=excludes)
