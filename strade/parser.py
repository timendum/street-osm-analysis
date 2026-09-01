"""Streaming OSM parser: emit highway ways with resolved node coordinates.

Wraps pyosmium (the ``osmium`` package) to stream an OSM dump and yield one
:class:`~strade.models.HighwayWay` for every way carrying a ``highway`` tag
. Node coordinates come from pyosmium's built-in node-location
cache: :meth:`~osmium.FileProcessor.with_locations` keeps the coordinate of
every node it reads and attaches it to each way's node references, so a way's
geometry is resolved in a single streaming pass without a manual node index
.

An :class:`~osmium.filter.EntityFilter` restricted to ways is installed *after*
location caching, so nodes still populate the cache while only ways reach the
iterator body. Way references whose location is absent from the dump (an invalid
location) are omitted and reported via the :class:`~strade.reporter.Reporter`
.

libosmium visits ways in ascending id order within a run, so ``resume_after_way_id``
is a cheap prefix skip: every way with an id at or below the cursor is dropped
before any other work, so a resumed run neither re-emits committed ways nor
re-raises their warnings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import osmium
import osmium.filter
import osmium.osm
from tqdm import tqdm

from strade.models import HighwayWay, NodeRef

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from strade.reporter import Reporter
    from strade.validation import SupportedFormat

# A way needs at least two resolved nodes to form a line; fewer than this and it
# cannot contribute a geometry, so it is dropped.
_MIN_NODES = 2


def parse_highways(
    path: Path,
    fmt: SupportedFormat,
    reporter: Reporter,
    resume_after_way_id: int | None = None,
) -> Iterator[HighwayWay]:
    """Yield a :class:`HighwayWay` for every highway way in the dump.

    Streams ``path`` with pyosmium, attaching each referenced node's coordinate
    from the location cache.

    Args:
        path: Filesystem path to the OSM dump.
        fmt: The detected input format (see :mod:`strade.validation`); accepted
            for interface symmetry with the rest of the pipeline. pyosmium
            infers the concrete reader from the file itself, so this argument is
            not otherwise consumed.
        reporter: Sink for non-fatal warnings about missing node references and
            dropped ways.
        resume_after_way_id: When set, every way whose id is at or below this
            cursor is skipped before any other work, so already-committed ways
            are neither re-emitted nor re-warned.

    Yields:
        One :class:`HighwayWay` per way carrying a ``highway`` tag,
        with its ordered ``node_ids`` and the resolved ``coords`` of
        every node whose location is present in the dump. Ways without
        a ``highway`` tag are skipped; a way left with fewer than two
        resolved nodes after dropping missing references is dropped with a
        warning.
    """
    del fmt  # pyosmium detects the reader from the file; kept for interface symmetry.

    processor = (
        osmium.FileProcessor(str(path))
        .with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
    )

    # The dump does not expose a way count without a prior pass, so the bar runs
    # without a total: it reports throughput and a running count of ways seen.
    ways = tqdm(
        processor,
        desc="parsing ways",
        unit="way",
        unit_scale=True,
    )

    for way in ways:
        # The EntityFilter(WAY) above guarantees only ways are yielded.
        assert isinstance(way, osmium.osm.Way)

        way_id = way.id

        # Resume: drop the already-committed prefix before any other work so no
        # warning is re-raised for a way handled before the interruption.
        if resume_after_way_id is not None and way_id <= resume_after_way_id:
            continue

        highway = way.tags.get("highway")
        if highway is None:
            # Not a road-like feature.
            continue

        node_ids, coords, missing = _resolve_nodes(way)

        if missing:
            reporter.info(
                f"way {way_id} references {missing} node(s) with no location; "
                "omitting them from the record"
            )

        if len(coords) < _MIN_NODES:
            reporter.info(
                f"way {way_id} has fewer than {_MIN_NODES} resolved nodes "
                "after dropping missing references; dropping the way"
            )
            continue

        name = way.tags.get("name")
        yield HighwayWay(
            way_id=way_id,
            name=name,
            node_ids=node_ids,
            coords=coords,
        )


def _resolve_nodes(way: osmium.osm.Way) -> tuple[list[int], list[NodeRef], int]:
    """Split a way's node references into ordered ids, resolved coords, and a miss count.

    ``node_ids`` keeps every referenced id in order. ``coords`` holds a
    :class:`NodeRef` for each node whose cached location is valid; a
    node whose location is absent from the dump is omitted from ``coords`` and
    counted in ``missing``.
    """
    node_ids: list[int] = []
    coords: list[NodeRef] = []
    missing = 0
    for node in way.nodes:
        node_ids.append(node.ref)
        location = node.location
        if location.valid():
            coords.append(NodeRef(node_id=node.ref, lon=location.lon, lat=location.lat))
        else:
            missing += 1
    return node_ids, coords, missing
