"""The ``cities`` command's aggregation: match streets to comuni by containment.

Answers, for every Italian comune (``admin_level=8`` boundary), the yes/no
question: does it contain at least one street whose name matches one of the
supplied ``LIKE`` patterns? The heavy lifting is split so each half stays cheap:

- The string match runs in SQLite (:func:`strade.store.read_ways_matching`), so
  only the ways that already matched a pattern are handed here for the more
  expensive geometry step.
- Containment is a point-in-polygon test over the comuni boundaries. A
  :class:`shapely.STRtree` built over the boundary geometries coarse-filters
  each street point to the handful of comuni whose bounding box covers it, so
  the test is near-constant per street instead of scanning all ~8000 comuni.

Boundaries and their street points are both plain WGS84 lon/lat: containment is
topological, so no metric projection is needed.

An OSM regional/national extract carries the full boundaries of comuni that lie
just across the cut, so those out-of-region comuni would otherwise become report
rows. :func:`select_comuni` optionally drops them: the parser reads a comune's
parent levels (region/country) in the same pass, and given a parent boundary's
OSM id it keeps only the comuni whose representative point falls inside that
parent.

The result is merged into a single ``<database>-cities.csv``: one row per comune
carrying the identifying tags and the source OSM reference, plus one
``true``/``false`` result column per pattern file (named after that file's
stem). Re-running a pattern file overwrites its column; a new pattern file adds
one, keyed on the comune's ``osm_id`` reference. A trailing ``Total`` row counts
the matched comuni per result column.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shapely import Point, STRtree

from strade.parser import COMUNE_ADMIN_LEVEL

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from strade.models import CityArea
    from strade.reporter import Reporter
    from strade.store import MatchedWay


class RegionError(Exception):
    """The requested region-filter boundary is absent from the dump.

    Raised when the ``cities`` command is given a region relation id (``-r``) but
    no assemblable ``admin_level`` 2/4 boundary with that id is found in the dump
    — a mistyped id, or a parent boundary that did not fully assemble because the
    extract clips it. The command turns this into a fatal, non-zero exit rather
    than silently emitting an empty (or unfiltered) report.
    """


# The identifying columns every row carries, shared across pattern-file runs.
# ``osm_id`` carries the source OSM element reference (e.g. ``relation/45690``)
# so each row can be traced back to the boundary it came from; it is also the
# stable per-comune id the merge keys on.
ID_COLUMNS: tuple[str, ...] = (
    "name",
    "postal_code",
    "istat",
    "catasto",
    "wikidata",
    "osm_id",
)


@dataclass
class CityMatch:
    """A comune paired with whether any matching street falls inside it.

    ``matched`` starts ``False`` and is flipped to ``True`` the first time a
    matching street's representative point is found inside the ``area``'s
    boundary; once true it never needs re-testing.
    """

    area: CityArea
    matched: bool = False


class CityIndex:
    """A spatial index over comuni boundaries for point-in-polygon lookup.

    Wraps a :class:`shapely.STRtree` built over the comuni geometries and keeps a
    parallel list of :class:`CityMatch` (one per boundary, in read order) so a
    tree hit maps straight back to the comune whose flag must be set. Querying a
    street point with the ``intersects`` predicate returns every boundary that
    contains it (a point on a shared border may fall in two comuni; both are
    marked, which is the desired "at least one street" semantics).
    """

    def __init__(self, cities: Iterable[CityArea]) -> None:
        self._matches: list[CityMatch] = [CityMatch(area=area) for area in cities]
        geometries = [match.area.geometry for match in self._matches]
        # STRtree over an empty list is valid and simply matches nothing, so a
        # dump with no admin_level=8 boundaries still produces a (header-only)
        # report rather than raising.
        self._tree = STRtree(geometries)

    def mark_point(self, lon: float, lat: float) -> None:
        """Flag every comune whose boundary contains ``(lon, lat)`` as matched.

        The STRtree first narrows to boundaries whose bounding box covers the
        point; the ``intersects`` predicate then confirms true containment. In
        :meth:`STRtree.query` the predicate reads ``query_geom.predicate(tree_geom)``,
        so for a point query ``intersects`` is the point-in-polygon test (edge
        included), and ``contains``/``covers`` would ask the reverse (whether the
        point contains the polygon) and never match. Each confirmed comune's flag
        is set, so a later street in the same comune costs a cheap already-true
        test.
        """
        point = Point(lon, lat)
        for index in self._tree.query(point, predicate="intersects"):
            self._matches[int(index)].matched = True

    @property
    def matches(self) -> list[CityMatch]:
        """The per-comune match records in boundary read order."""
        return self._matches


def select_comuni(
    areas: Iterable[CityArea],
    region_id: int | None,
    reporter: Reporter,
) -> list[CityArea]:
    """Split parsed admin boundaries into the comuni the report should cover.

    :func:`~strade.parser.parse_admin_areas` yields several admin levels in one
    pass: comuni (:data:`~strade.parser.COMUNE_ADMIN_LEVEL`) plus coarser parents
    (region/country). This picks out the comuni and, when ``region_id`` is given,
    keeps only those whose representative point falls inside the parent boundary
    with that OSM id.

    An OSM regional/national extract carries the full boundary of comuni that lie
    just across the cut, so without a filter those out-of-region comuni become
    report rows. Supplying the parent boundary's relation id (e.g. the region a
    single-region dump was cut from) drops them.

    Args:
        areas: The parsed admin boundaries (comuni and parents, mixed).
        region_id: OSM id of the parent boundary to clip to, or ``None`` to keep
            every comune (no filtering).
        reporter: Progress/warning sink.

    Returns:
        The comuni to report on, in read order.

    Raises:
        RegionError: ``region_id`` is given but no assembled parent boundary
            (admin level 2/4) with that id was found in the dump.
    """
    comuni: list[CityArea] = []
    parents: dict[int, CityArea] = {}
    for area in areas:
        if area.admin_level == COMUNE_ADMIN_LEVEL:
            comuni.append(area)
        else:
            # A parent (region/country) level; keyed by OSM id for the filter.
            parents[area.osm_id] = area

    if region_id is None:
        return comuni

    region = parents.get(region_id)
    if region is None:
        available = ", ".join(str(oid) for oid in sorted(parents)) or "none"
        raise RegionError(
            f"region boundary relation/{region_id} not found in the dump "
            f"(assembled parent boundaries: {available}). Pass the id of an "
            "admin_level 2/4 boundary present in this dump."
        )

    # Representative points are interior to the polygon, so `covers` (edge
    # included) here answers "is this comune inside the region"; a comune whose
    # interior point is outside the parent is a cross-border spill-over.
    boundary = region.geometry
    kept = [c for c in comuni if boundary.covers(c.geometry.representative_point())]
    dropped = len(comuni) - len(kept)
    reporter.progress(
        f"cities: region filter relation/{region_id} "
        f"({region.name or 'unnamed'}): kept {len(kept)} comune(i), "
        f"dropped {dropped} outside the region"
    )
    return kept


def build_city_index(
    cities: Iterable[CityArea],
    reporter: Reporter,
) -> CityIndex:
    """Build a :class:`CityIndex` from parsed comuni, reporting the count.

    Materializes the boundaries (the only in-memory dataset the command holds:
    a few thousand polygons) into the spatial index and records how many were
    indexed so the run's progress line reflects the comuni count.
    """
    index = CityIndex(cities)
    reporter.progress(f"cities: indexed {len(index.matches)} comune boundary(ies)")
    return index


def assign_matches(index: CityIndex, ways: Iterable[MatchedWay]) -> int:
    """Test each matched way's point against ``index`` and flag its comune(s).

    Returns the number of matched ways consumed (for the progress summary). Each
    way is placed by its single representative point (its middle vertex); a way
    whose point falls in no boundary (outside every comune, e.g. a coastal or
    cross-border way) simply marks nothing.
    """
    consumed = 0
    for way in ways:
        index.mark_point(way.lon, way.lat)
        consumed += 1
    return consumed


def _cell(value: str | None) -> str:
    """Render an optional tag as a CSV cell (missing tag -> empty string)."""
    return "" if value is None else value


def _row_key(row: dict[str, str]) -> str:
    """Stable identity for a comune row: its ``osm_id`` reference.

    Every comune carries a unique OSM element reference (``relation/<id>`` or
    ``way/<id>``), so it is the reliable merge id: the same comune re-run always
    yields the same reference. ``wikidata`` is not used because it is optional
    and occasionally shared by two distinct comuni (bad OSM/wikidata data), which
    would collapse them into one row.
    """
    return row.get("osm_id", "")


def _match_rows(matches: Iterable[CityMatch], column: str) -> list[dict[str, str]]:
    """Turn the match records into per-comune dicts carrying one result column.

    Each dict holds the :data:`ID_COLUMNS` (empty string for an absent tag) plus
    ``column`` set to the lowercase literal ``true``/``false``.
    """
    rows: list[dict[str, str]] = []
    for match in matches:
        area = match.area
        rows.append(
            {
                "name": _cell(area.name),
                "postal_code": _cell(area.postal_code),
                "istat": _cell(area.istat),
                "catasto": _cell(area.catasto),
                "wikidata": _cell(area.wikidata),
                "osm_id": area.osm_ref,
                column: "true" if match.matched else "false",
            }
        )
    return rows


# The summary footer row is a normal keyed row carrying a fake ``osm_id`` so it
# merges through the same ``osm_id``-keyed path as any comune (no comune uses
# this id). ``name`` is set to ``Total`` for readability; its result cells hold
# the per-column count of matched comuni, refreshed on every write.
TOTAL_KEY = "total"
TOTAL_LABEL = "Total"


def _refresh_total(header: list[str], rows: Iterable[dict[str, str]]) -> dict[str, str]:
    """Build the summary row: per-result-column count of ``true`` comune cells.

    Keyed by the fake ``osm_id`` :data:`TOTAL_KEY`, labelled ``Total`` in the
    ``name`` column with the other identifying columns blank, and each result
    column set to its ``true`` count over the comuni (the summary row excluded).
    """
    result_columns = [col for col in header if col not in ID_COLUMNS]
    comuni = [row for row in rows if _row_key(row) != TOTAL_KEY]
    total: dict[str, str] = {"name": TOTAL_LABEL, "osm_id": TOTAL_KEY}
    for col in result_columns:
        total[col] = str(sum(1 for row in comuni if row.get(col) == "true"))
    return total


def _read_existing(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read an existing ``cities`` CSV into its header and row dicts.

    Returns the column order and one dict per data row (comuni plus, if present,
    the :data:`TOTAL_KEY` summary row, which is just another keyed row here). A
    missing file yields the bare :data:`ID_COLUMNS` header and no rows, so the
    caller can treat "create" and "update" uniformly.
    """
    if not path.is_file():
        return list(ID_COLUMNS), []
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        header = list(reader.fieldnames or ID_COLUMNS)
        return header, [dict(row) for row in reader]


def write_csv(matches: Iterable[CityMatch], path: Path, column: str) -> int:
    """Merge the run's results into ``path`` under ``column`` and return the rows.

    Builds a single ``<database>-cities.csv`` that grows one result column per
    pattern file:

    - If ``path`` does not exist, the file is created with the :data:`ID_COLUMNS`
      followed by a single result column named ``column`` (the pattern file's
      stem) -- the same shape the old report had, only the fixed ``matched``
      header is replaced by the pattern name.
    - If ``path`` exists, the run's per-comune ``true``/``false`` result is added
      as a new ``column`` (or overwrites it when the same pattern file is re-run),
      reusing every existing column. Rows are matched on the ``osm_id``
      reference; comuni not already present are appended, and previously-seen
      comuni missing from this run keep their old cells with an empty value in
      the new column.

    In both cases a trailing summary row (fake ``osm_id`` :data:`TOTAL_KEY`,
    always sorted last) is refreshed to hold each result column's count of
    matched (``true``) comuni.

    Uses :mod:`csv` so names containing commas or quotes round-trip correctly.
    Returns the number of comune rows this run contributed.
    """
    header, existing = _read_existing(path)
    if column not in header:
        header.append(column)

    by_key = {_row_key(row): row for row in existing}
    order = list(by_key)

    new_rows = _match_rows(matches, column)
    for row in new_rows:
        key = _row_key(row)
        if key in by_key:
            by_key[key].update(row)
        else:
            by_key[key] = row
            order.append(key)

    # The summary row is just another keyed row: recompute its counts over the
    # merged comuni and force it last, whether it pre-existed or is created here.
    by_key[TOTAL_KEY] = _refresh_total(header, (by_key[key] for key in order))
    order = [key for key in order if key != TOTAL_KEY] + [TOTAL_KEY]

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=header, restval="", lineterminator="\n"
        )
        writer.writeheader()
        for key in order:
            writer.writerow(by_key[key])
    return len(new_rows)
