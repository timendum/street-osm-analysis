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
topological, so no metric projection is needed (unlike the joiner, which
measures distances in meters).

The result is one CSV row per comune in the order the boundaries were read,
carrying the identifying tags and a ``true``/``false`` ``matched`` column.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shapely import Point, STRtree

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import TextIO

    from strade.models import CityArea
    from strade.reporter import Reporter
    from strade.store import MatchedWay

# CSV header, one column per reported comune attribute plus the yes/no result.
CSV_HEADER: tuple[str, ...] = (
    "name",
    "postal_code",
    "istat",
    "catasto",
    "wikidata",
    "matched",
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
    way is placed by its single representative point; a way whose point falls in
    no boundary (outside every comune, e.g. a coastal or cross-border way) simply
    marks nothing.
    """
    consumed = 0
    for way in ways:
        index.mark_point(way.lon, way.lat)
        consumed += 1
    return consumed


def _cell(value: str | None) -> str:
    """Render an optional tag as a CSV cell (missing tag -> empty string)."""
    return "" if value is None else value


def write_csv(matches: Iterable[CityMatch], stream: TextIO) -> int:
    """Write one CSV row per comune to ``stream`` and return the rows written.

    Emits :data:`CSV_HEADER` first, then one row per :class:`CityMatch` in the
    given order: the five identifying tags (empty when absent) and ``matched`` as
    the lowercase literal ``true``/``false``. Uses :mod:`csv` so names containing
    commas or quotes are quoted correctly.
    """
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    rows = 0
    for match in matches:
        area = match.area
        writer.writerow(
            (
                _cell(area.name),
                _cell(area.postal_code),
                _cell(area.istat),
                _cell(area.catasto),
                _cell(area.wikidata),
                "true" if match.matched else "false",
            )
        )
        rows += 1
    return rows
