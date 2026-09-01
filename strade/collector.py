"""Streaming Collector: route named highway ways to storage, count unnamed ones.

The Collector sits in the middle of the ``extract`` streaming pass. As each
:class:`~strade.models.HighwayWay` is parsed it makes a single decision: is the
way named? A named way is handed to the :class:`~strade.store.WayWriter`
for insertion into the ``ways`` table; an unnamed way is excluded and counted
. The only state held in memory during the pass is the running tally in
:class:`CollectCounts`, so the pass scales to arbitrarily large dumps and keeps
way-level resume intact.

Grouping is deliberately *not* performed here. Ways that share a street are
grouped later, on the join side, by :func:`strade.store.read_groups`,
which streams ``ways ORDER BY norm_name, name, way_id`` and yields one
:class:`~strade.models.NameGroup` per contiguous run of equal normalization keys
. Deferring grouping to a key-sorted read means the whole
dataset never has to be materialized in memory to group it. The grouping key is
the normalization key from :func:`strade.normalize.normalize_name` — which
strips street-type prefixes and folds to ASCII letters so bilingual and
type-prefixed variants of one street collapse together — computed once at write
time by the :class:`~strade.store.WayWriter`. This module still leaves the
raw ``name`` untouched.

This module does not manage the database connection or persist the tally. The
caller (``run_extract``, task 13.2) wires ``parse_highways`` into
:func:`collect`, then persists the returned counts via
:func:`strade.store.set_counts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from strade.models import HighwayWay
    from strade.store import WayWriter


def is_named(way: HighwayWay) -> bool:
    """Return ``True`` iff ``way`` carries a ``name`` tag.

    Presence, not content, decides: a way is named exactly when ``way.name`` is
    not ``None``. The raw name is left untouched here; the normalization key used
    for grouping is derived separately at write time by the
    :class:`~strade.store.WayWriter`. This means an empty or
    whitespace-only ``name`` string still counts as named.
    """
    return way.name is not None


@dataclass
class CollectCounts:
    """Running tally produced by a streaming :func:`collect` pass.

    ``parsed_count`` is every highway way seen; ``unnamed_count`` is the subset
    excluded for having no ``name`` tag. The named subset is
    ``parsed_count - unnamed_count`` and is exactly what the
    :class:`~strade.store.WayWriter` persisted.
    """

    parsed_count: int = 0
    unnamed_count: int = 0


def collect(ways: Iterable[HighwayWay], writer: WayWriter) -> CollectCounts:
    """Route named ways to ``writer`` and count unnamed ways.

    Iterates ``ways`` once, counting every highway way in
    ``CollectCounts.parsed_count``. A named way (per :func:`is_named`) is
    appended to ``writer``; an unnamed way is excluded and added to
    ``CollectCounts.unnamed_count``. No grouping happens here — that is deferred
    to :func:`strade.store.read_groups` on the join side.

    The returned :class:`CollectCounts` is the only in-memory state and is *not*
    persisted here; the caller (``run_extract``) records it via
    :func:`strade.store.set_counts` and flushes ``writer`` (typically by
    using it as a context manager).
    """
    counts = CollectCounts()
    for way in ways:
        counts.parsed_count += 1
        if is_named(way):
            writer.append(way)
        else:
            counts.unnamed_count += 1
    return counts
