"""Route named highway ways to storage and count unnamed ones during extract.

Named ways are handed to the :class:`~strade.store.WayWriter`; unnamed ways are
counted and skipped. Grouping is deferred to the join side
(:func:`strade.store.read_groups`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from strade.models import HighwayWay
    from strade.store import WayWriter


def is_named(way: HighwayWay) -> bool:
    """Return ``True`` iff ``way`` carries a ``name`` tag (presence, not content)."""
    return way.name is not None


@dataclass
class CollectCounts:
    """Running tally from a :func:`collect` pass.

    ``parsed_count`` is every highway way seen; ``unnamed_count`` is the subset
    skipped for having no ``name`` tag.
    """

    parsed_count: int = 0
    unnamed_count: int = 0


def collect(ways: Iterable[HighwayWay], writer: WayWriter) -> CollectCounts:
    """Iterate ``ways`` once, appending named ways to ``writer`` and returning
    the tally. The caller persists the counts and flushes ``writer``.
    """
    counts = CollectCounts()
    for way in ways:
        counts.parsed_count += 1
        if is_named(way):
            writer.append(way)
        else:
            counts.unnamed_count += 1
    return counts
