"""Core data models for the Italian Street Extractor.

These dataclasses describe the records that flow through the pipeline:

- ``NodeRef``   — an OSM node id with its geographic coordinate (immutable).
- ``HighwayWay`` — a highway way with its ordered node ids and resolved coords.
- ``NameGroup`` — all highway ways that share one exact street name.
- ``Street``     — a distinct produced street: display name, norm_name key, and
  sorted, de-duplicated way ids.
- ``CityArea``   — an OSM ``admin_level=8`` boundary with its identifying tags
  and assembled polygon, used by the ``cities`` command.
- ``Square``     — a named ``place=square`` reduced to a single representative
  point, used by the ``cities`` command alongside the highway ways.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shapely import Polygon
    from shapely.geometry.base import BaseGeometry


@dataclass(frozen=True)
class NodeRef:
    """An OSM node reference with its coordinate.

     Frozen/immutable: a node's id and location never change once resolved
    .
    """

    node_id: int
    lon: float
    lat: float


@dataclass
class HighwayWay:
    """A highway way parsed from the OSM dump.

    Carries the ordered list of referenced node ids and the
    resolved coordinate of each node. ``name`` is ``None`` when
    the way has no ``name`` tag.
    """

    way_id: int
    name: str | None
    node_ids: list[int] = field(default_factory=list)
    coords: list[NodeRef] = field(default_factory=list)


@dataclass
class NameGroup:
    """All highway ways that share one normalization key.

    ``key`` is the language- and type-agnostic grouping key from
    :func:`strade.normalize.normalize_name`; ways whose raw names differ only by
    street-type prefix, language, or diacritics share a key and join together.
    ``name`` is a representative human-readable name chosen for output — the lossy
    ``key`` never reaches the produced street list. ``key`` is the stable resume
    marker for the join side, since it is independent of which surface form was
    picked for display.
    """

    name: str
    key: str = ""
    ways: list[HighwayWay] = field(default_factory=list)


@dataclass
class Street:
    """A distinct produced street.

    ``way_ids`` holds the identifiers of the composing highway ways and is kept
    sorted and de-duplicated at all times. The invariant is
    enforced on construction and after any assignment via ``__post_init__``.
    ``norm_name`` is the normalization key of the group the street came from,
    carried through to the output so each street records both its display name
    and the grouping key that produced it.
    """

    name: str
    norm_name: str = ""
    way_ids: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._normalize_way_ids()

    def _normalize_way_ids(self) -> None:
        # sorted() over a set both de-duplicates and orders ascending.
        object.__setattr__(self, "way_ids", sorted(set(self.way_ids)))

    @property
    def count(self) -> int:
        """Number of distinct composing way ids in this street."""
        return len(self.way_ids)


@dataclass(frozen=True)
class CityArea:
    """An OSM ``admin_level=8`` administrative boundary (an Italian ``comune``).

    Carries the identifying tags the ``cities`` command reports plus the
    assembled boundary ``geometry`` (a shapely ``Polygon``/``MultiPolygon`` in
    raw WGS84 lon/lat) used for point-in-polygon containment tests. Every tag
    except ``name`` may be absent in the dump and is then ``None``; ``name`` is
    also ``None`` for the (rare) boundary without a ``name`` tag.

    ``osm_id`` is the id of the source OSM element the boundary was assembled
    from, and ``from_way`` records whether that element is a way (``True``) or a
    relation (``False``).

    ``admin_level`` is the boundary's ``admin_level`` tag value (as read from the
    dump, e.g. ``"8"`` for a comune, ``"4"`` for a region, ``"2"`` for a
    country). It is ``None`` only for the (rare) boundary without an ``admin_level`` tag.
    """

    name: str | None
    postal_code: str | None
    istat: str | None
    catasto: str | None
    wikidata: str | None
    osm_id: int
    from_way: bool
    geometry: BaseGeometry | Polygon
    admin_level: str | None = None

    @property
    def osm_ref(self) -> str:
        """Human-readable OSM reference, e.g. ``relation/45690`` or ``way/123``.

        Prefixes ``osm_id`` with the source element type so the reference is
        unambiguous across OSM's separate node/way/relation id spaces.
        """
        return f"{'way' if self.from_way else 'relation'}/{self.osm_id}"


@dataclass(frozen=True)
class Square:
    """A named ``place=square`` reduced to a single representative point.

    OSM maps a square as a node, a closed way (area), or a multipolygon
    relation. All three are collapsed to one ``(lon, lat)`` point here — a node's
    own coordinate, or a polygon's representative interior point — because the
    ``cities`` command only needs a single point to test which comune the square
    falls in. Only named squares are extracted, so ``name`` is never ``None``.

    ``osm_id`` is the source OSM element's own id.
    """

    osm_id: int
    name: str
    lon: float
    lat: float
