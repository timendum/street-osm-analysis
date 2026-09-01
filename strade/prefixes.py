"""Scan an OSM dump for candidate street-type prefixes.

Streams every named highway way from a dump, takes the *first word* of each
name, folds it to lowercase ASCII letters, and tallies how often each word
appears. Words already listed in :data:`strade.normalize._PREFIXES` are skipped,
so the result is exactly the set of leading words the normalizer does *not* yet
strip — the raw material for manually extending that list from real data.

Output goes to stdout as ``count<tab>word`` lines sorted by descending count
(ties broken alphabetically), keeping stdout scriptable while progress and
warnings stay on stderr via the shared :class:`~strade.reporter.Reporter`.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from strade.normalize import first_word, is_known_prefix
from strade.parser import parse_highways

if TYPE_CHECKING:
    from pathlib import Path

    from strade.reporter import Reporter
    from strade.validation import SupportedFormat


def scan_first_words(
    path: Path,
    fmt: SupportedFormat,
    reporter: Reporter,
) -> Counter[str]:
    """Tally the first word of every named way, excluding known prefixes.

    Streams the dump via :func:`~strade.parser.parse_highways`, and for each way
    that carries a ``name`` takes its :func:`~strade.normalize.first_word`. Words
    already recognized by :func:`~strade.normalize.is_known_prefix` are dropped;
    the rest are counted. Unnamed ways and names with no alphabetic first word
    contribute nothing.
    """
    counts: Counter[str] = Counter()
    for way in parse_highways(path, fmt, reporter):
        if way.name is None:
            continue
        word = first_word(way.name)
        if word is None or is_known_prefix(word):
            continue
        counts[word] += 1
    return counts


def format_counts(counts: Counter[str]) -> str:
    """Render ``counts`` as ``count<tab>word`` lines, most frequent first.

    Ties on count are broken alphabetically by word so the output is stable
    across runs. Returns the empty string when there are no candidates.
    """
    ordered = counts.most_common(20)
    return "\n".join(f"{count}\t{word}" for word, count in ordered)
