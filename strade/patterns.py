"""Parse the ``cities`` command's LIKE-pattern file.

Each non-blank, non-comment line is a SQLite ``LIKE`` pattern matched against
street names (``%`` = any run, ``_`` = one char, case-insensitive for ASCII).
Running the patterns lives in :func:`strade.store.read_ways_matching`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Lines starting with this (after stripping) are comments and are skipped.
_COMMENT = "#"


class PatternError(Exception):
    """Raised when the pattern file has no usable pattern; halts the command."""


def parse_pattern_file(path: Path) -> list[str]:
    """Parse a LIKE-pattern file into a list of patterns, in file order.

    Skips blank and ``#`` comment lines, trims whitespace, and keeps duplicates.

    Raises:
        PatternError: if no usable pattern remains (file empty or all comments).
    """
    patterns: list[str] = []
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(_COMMENT):
            continue
        patterns.append(line)
    if not patterns:
        raise PatternError(
            f"{path}: no LIKE patterns found (file is empty or all comments)"
        )
    return patterns
