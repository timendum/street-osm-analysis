"""Scan a dump for candidate street-type prefixes to extend the normalizer."""

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
    """Count the first word of every named way, skipping already-known prefixes."""
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
    """Render the top counts as ``count<tab>word`` lines, most frequent first."""
    ordered = counts.most_common(50)
    return "\n".join(f"{count}\t{word}" for word, count in ordered)
