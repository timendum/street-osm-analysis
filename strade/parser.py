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

Two further readers back the ``cities`` command: ``parse_admin_areas`` yields a
:class:`~strade.models.CityArea` per administrative boundary, and
``parse_squares`` a :class:`~strade.models.Square` per named ``place=square``
element, each reduced to a representative point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import osmium
import osmium.filter
import osmium.geom
import osmium.osm
import shapely
from tqdm import tqdm

from strade.models import CityArea, HighwayWay, NodeRef, Square

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from strade.reporter import Reporter
    from strade.validation import SupportedFormat

# A way needs at least two resolved nodes to form a line; fewer than this and it
# cannot contribute a geometry, so it is dropped.
_MIN_NODES = 2

# The OSM tags that select the administrative boundaries the `cities` command
# reads. ``admin_level=8`` is the comune level in Italy; the coarser levels are
# read in the same pass so the `cities` region filter can find a parent boundary
# (region or country) and keep only the comuni inside it.
#
# NOTE: these levels are Italy's country/region/comune hierarchy (2 = country,
# 4 = region, 8 = comune). For another country whose administrative hierarchy
# uses different admin_level values, adjust `_ADMIN_LEVELS` (and `COMUNE_ADMIN_LEVEL`)
# accordingly.
_BOUNDARY_TAG = ("boundary", "administrative")
# The comune admin_level: the boundaries the `cities` command reports on. Public
# so the cities aggregation can tell comuni apart from the coarser parent levels
# read in the same pass.
COMUNE_ADMIN_LEVEL = "8"
# Country and region are the parent levels read alongside the comune so the
# `cities` region filter can find a parent boundary by id.
PARENT_ADMIN_LEVELS = ("2", "4")
_ADMIN_LEVELS = (*PARENT_ADMIN_LEVELS, COMUNE_ADMIN_LEVEL)
_ADMIN_LEVEL_TAGS = tuple(("admin_level", level) for level in _ADMIN_LEVELS)
_ADMIN_LEVEL_SET = frozenset(_ADMIN_LEVELS)

# The tag that selects squares for the `cities` command. OSM maps a square as a
# node, a closed way (area), or a multipolygon relation, all tagged place=square.
_SQUARE_TAG = ("place", "square")


def parse_highways(
    path: Path,
    fmt: SupportedFormat,
    reporter: Reporter,
    resume_after_way_id: int | None = None,
) -> Iterator[HighwayWay]:
    """Yield a :class:`HighwayWay` for every ``highway``-tagged way in the dump.

    Node coordinates are resolved from pyosmium's location cache; references
    with no location are dropped and reported, and a way left with fewer than
    two nodes is dropped with a warning.

    Args:
        path: Filesystem path to the OSM dump.
        fmt: Detected input format; accepted for interface symmetry (pyosmium
            infers the reader from the file).
        reporter: Sink for non-fatal warnings about missing nodes and dropped
            ways.
        resume_after_way_id: When set, ways with an id at or below this cursor
            are skipped, so a resumed run neither re-emits nor re-warns them.
    """
    del fmt  # pyosmium detects the reader from the file; kept for interface symmetry.

    processor = (
        osmium.FileProcessor(str(path))
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway").enable_for(osmium.osm.WAY))
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
        # The filters above guarantee only ways carrying a ``highway`` tag are
        # yielded.
        assert isinstance(way, osmium.osm.Way)

        way_id = way.id

        # Resume: drop the already-committed prefix before any other work so no
        # warning is re-raised for a way handled before the interruption.
        if resume_after_way_id is not None and way_id <= resume_after_way_id:
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


def parse_admin_areas(
    path: Path,
    fmt: SupportedFormat,
    reporter: Reporter,
) -> Iterator[CityArea]:
    """Yield a :class:`CityArea` for each administrative boundary in the dump.

    Drives pyosmium's area builder over a ``boundary=administrative`` filter,
    keeping the levels in :data:`_ADMIN_LEVELS` (comune ``8`` plus the parent
    ``2``/``4`` the ``cities`` region filter needs). Each :class:`CityArea`
    carries its ``admin_level``, its geometry as a WGS84 ``MultiPolygon``, the
    ``cities`` identifying tags (``name``, ``postal_code``, ``ref:ISTAT``,
    ``ref:catasto``, ``wikidata``; missing tags become ``None``) and its OSM
    id/type (``orig_id()``/``from_way()``). Way-sourced areas (single boundary
    segments) and off-level areas are skipped.

    ``fmt`` is accepted for interface symmetry only. Boundaries whose geometry
    fails to assemble are dropped with a warning via ``reporter`` rather than
    aborting the scan.
    """
    del fmt  # pyosmium detects the reader from the file; kept for interface symmetry.

    wkb_factory = osmium.geom.WKBFactory()

    processor = (
        osmium.FileProcessor(str(path))
        .with_areas(osmium.filter.TagFilter(_BOUNDARY_TAG))
        .with_filter(osmium.filter.TagFilter(*_ADMIN_LEVEL_TAGS))
    )

    # The dump does not expose an area count without a prior pass, so the bar
    # runs without a total: it reports throughput and a running count.
    areas = tqdm(
        processor,
        desc="parsing boundaries",
        unit="area",
    )

    for obj in areas:
        # with_areas still streams non-area objects (the ways/nodes it read to
        # build the areas); only assembled areas carry a boundary geometry.
        if not obj.is_area():
            continue

        # An administrative area is a `type=boundary` relation;
        # So drop way-sourced areas and keep only the relation-sourced ones.
        if obj.from_way():
            continue

        tags = obj.tags
        admin_level = tags.get("admin_level")
        if admin_level not in _ADMIN_LEVEL_SET:
            # Not one of the levels we read (province/other administrative level).
            continue

        name = tags.get("name")
        try:
            # create_multipolygon returns a hex-encoded WKB string; shapely reads
            # the raw bytes. The geometry must be built while ``obj`` is still the
            # live area — libosmium invalidates it once iteration advances.
            wkb_hex = wkb_factory.create_multipolygon(obj)
            geometry = shapely.from_wkb(bytes.fromhex(wkb_hex))
        except (RuntimeError, ValueError) as exc:
            reporter.info(
                f"boundary {name or obj.orig_id!r} could not be assembled into a "
                f"polygon ({exc}); skipping it"
            )
            continue

        if geometry.is_empty:
            reporter.info(
                f"boundary {name or obj.orig_id!r} assembled to an empty geometry; "
                "skipping it"
            )
            continue

        yield CityArea(
            name=name,
            postal_code=tags.get("postal_code"),
            istat=tags.get("ref:ISTAT"),
            catasto=tags.get("ref:catasto"),
            wikidata=tags.get("wikidata"),
            # orig_id() is the source OSM element's id (the area's own id encodes
            # the source type as orig_id*2 for ways, orig_id*2+1 for relations);
            # from_way() disambiguates which id space that is.
            osm_id=obj.orig_id(),
            from_way=obj.from_way(),
            geometry=geometry,
            admin_level=admin_level,
        )


def parse_squares(
    path: Path,
    fmt: SupportedFormat,
    reporter: Reporter,
) -> Iterator[Square]:
    """Yield a :class:`Square` for every named ``place=square`` in the dump.

    OSM maps a square as a node, a closed way, or a multipolygon relation; all
    three are reduced to one representative ``(lon, lat)`` point, which is all
    the ``cities`` containment test needs. A node uses its own coordinate; an
    assembled way/relation area uses its polygon's ``representative_point()``
    (guaranteed interior, unlike a centroid).

    Only named squares are yielded. ``fmt`` is accepted for interface symmetry
    only. Squares whose geometry fails to assemble are dropped with a warning
    via ``reporter`` rather than aborting the scan.
    """
    del fmt  # pyosmium detects the reader from the file; kept for interface symmetry.

    wkb_factory = osmium.geom.WKBFactory()

    processor = (
        osmium.FileProcessor(str(path))
        .with_areas(osmium.filter.TagFilter(_SQUARE_TAG))
        .with_filter(
            osmium.filter.EmptyTagFilter().enable_for(osmium.osm.NODE | osmium.osm.WAY)
        )
    )

    squares = tqdm(
        processor,
        desc="scanning for squares",
        unit="obj",
    )

    for obj in squares:
        name = obj.tags.get("name")
        if name is None:
            # An unnamed square (or one of the raw nodes/ways the area builder
            # streams while assembling) cannot match a pattern; skip it.
            continue

        if obj.tags.get("place") != "square":
            # The area builder also streams the member ways/nodes it read to
            # build the areas; keep only the square-tagged objects themselves.
            continue

        if isinstance(obj, osmium.osm.Node):
            location = obj.location
            if not location.valid():
                reporter.info(
                    f"square node {obj.id} ({name!r}) has no valid location; skipping it"
                )
                continue
            yield Square(osm_id=obj.id, name=name, lon=location.lon, lat=location.lat)
            continue

        if not obj.is_area():
            # A place=square way/relation reaches us as an assembled area; the
            # raw way/relation objects (non-area) carry no polygon, so skip them
            # and wait for the area the builder emits.
            continue

        try:
            # create_multipolygon returns a hex-encoded WKB string; the geometry
            # must be built while ``obj`` is still the live area — libosmium
            # invalidates it once iteration advances.
            wkb_hex = wkb_factory.create_multipolygon(obj)
            geometry = shapely.from_wkb(bytes.fromhex(wkb_hex))
        except (RuntimeError, ValueError) as exc:
            reporter.info(
                f"square {name!r} could not be assembled into a polygon "
                f"({exc}); skipping it"
            )
            continue

        if geometry.is_empty:
            reporter.info(f"square {name!r} assembled to an empty geometry; skipping it")
            continue

        # representative_point() is guaranteed to lie inside the polygon, unlike
        # a centroid which can fall outside a concave/L-shaped square.
        point = geometry.representative_point()
        yield Square(
            osm_id=obj.orig_id(),
            name=name,
            lon=point.x,
            lat=point.y,
        )
