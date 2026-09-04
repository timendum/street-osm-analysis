"""Parse the ``cities`` command's LIKE-pattern file.

The ``cities`` command asks a simple question of every ``admin_level=8`` city:
does it contain at least one street whose name matches any of a list of
patterns? Those patterns are supplied in a plain-text file, one per line, and
are used verbatim as SQLite ``LIKE`` patterns against the ``ways.name`` column,
so ``%`` matches any run of characters and ``_`` matches a single one::

    # Streets named after Garibaldi, anywhere in the name
    %garibaldi%
    # Streets whose name starts with "Via Roma"
    via roma%

Blank lines and ``#`` comment lines are ignored and surrounding whitespace on
each line is trimmed, matching the alias file's conventions
(:mod:`strade.aliases`). SQLite ``LIKE`` is case-insensitive for ASCII, so the
patterns need not worry about letter case. This module only reads the file into
a list of patterns; running them against the database lives in
:func:`strade.store.read_ways_matching`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Lines starting with this (after stripping) are comments and are skipped.
_COMMENT = "#"


class PatternError(Exception):
    """A fatal problem with the pattern file that must halt the command.

    Raised when the file contains no usable pattern at all (only blank lines and
    comments), since a ``cities`` run with no patterns would mark every city as
    unmatched and produce a meaningless report. The command reports the message
    and exits non-zero without touching the database or the dump.
    """


def parse_pattern_file(path: Path) -> list[str]:
    """Parse a LIKE-pattern file at ``path`` into a list of patterns.

    Reads the file line by line: blank lines and ``#`` comment lines are
    skipped and every remaining line is whitespace-trimmed and kept as one
    SQLite ``LIKE`` pattern, in file order. Duplicate patterns are preserved as
    written (they are harmless: an ``OR`` of a pattern with itself matches the
    same rows).

    Raises:
        PatternError: if the file contains no usable pattern (every line is
            blank or a comment), so the caller never runs an empty match.
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
