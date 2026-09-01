"""Geometry projection for meter-accurate distance measurement.

OSM coordinates are WGS84 longitude/latitude in degrees, but the proximity
threshold is specified in meters. The :class:`Projector`
transforms geographic coordinates into a projected metric CRS (default
EPSG:6875 / RDN2008 Italy zone) so that shapely distances come out in meters.

A single :class:`pyproj.Transformer` is built once and cached on the instance,
because constructing a transformer is comparatively expensive and every way in a
name group is projected through the same one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyproj import Transformer
from shapely import LineString, Point
from shapely.ops import nearest_points

if TYPE_CHECKING:
    from strade.models import HighwayWay

# WGS84 lon/lat, the CRS OSM coordinates are expressed in.
_WGS84_EPSG = 4326  # EPSG:4326

# A metric CRS covering western Italy.
_DEFAULT_METRIC_EPSG = 6875

# A LineString needs at least two distinct vertices to form a line.
_MIN_COORDS_FOR_LINE = 2


class Projector:
    """Transforms WGS84 lon/lat into a metric CRS and builds projected geometries.

    All distance comparisons in the Joiner operate on the projected geometries
    produced here, so the proximity threshold is interpreted in meters.
    """

    def __init__(self, metric_epsg: int = _DEFAULT_METRIC_EPSG) -> None:
        """Build a cached transformer from WGS84 to the given metric CRS.

        Args:
            metric_epsg: EPSG code of the target metric CRS. Defaults to
                EPSG:6875 (RDN2008 / Italy zone, covering all of Italy).

        ``always_xy=True`` fixes the coordinate order to (lon, lat) / (x, y),
        matching the order OSM stores coordinates in.
        """
        self.metric_epsg = metric_epsg
        self._transformer = Transformer.from_crs(
            _WGS84_EPSG,
            metric_epsg,
            always_xy=True,
        )

    def to_metric_linestring(self, way: HighwayWay) -> LineString | None:
        """Build a projected shapely ``LineString`` from a way's resolved coords.

        Returns ``None`` for a way with fewer than two coordinates, since such a
        degenerate geometry cannot form a line; callers handle the ``None`` case.
        """
        if len(way.coords) < _MIN_COORDS_FOR_LINE:
            return None

        lons = [ref.lon for ref in way.coords]
        lats = [ref.lat for ref in way.coords]
        xs, ys = self._transformer.transform(lons, lats)
        return LineString(zip(xs, ys, strict=True))

    def distance_m(self, a: LineString, b: LineString) -> float:
        """Return the shortest distance in meters between two projected lines."""
        return a.distance(b)

    def to_metric_nodes(self, way: HighwayWay) -> list[tuple[int, Point]]:
        """Project a way's resolved coords to ``(node_id, metric Point)`` pairs.

        Every resolved :class:`~strade.models.NodeRef` becomes one projected
        point tagged with its OSM node id, so the nearest *node* between two
        streets can be recovered after the exact distance is measured on the
        linestrings. A way with no resolved coords yields an empty list.
        """
        if not way.coords:
            return []
        lons = [ref.lon for ref in way.coords]
        lats = [ref.lat for ref in way.coords]
        xs, ys = self._transformer.transform(lons, lats)
        return [
            (ref.node_id, Point(x, y))
            for ref, x, y in zip(way.coords, xs, ys, strict=True)
        ]

    def nearest_node(
        self, geometry: LineString, nodes: list[tuple[int, Point]]
    ) -> int | None:
        """Return the OSM node id in ``nodes`` closest to ``geometry``.

        Used to attach the nearest actual node to a street-to-street candidate:
        the exact distance is measured on the geometries, then the closest
        vertex is reported as extra debugging info. ``None`` when ``nodes`` is
        empty.
        """
        if not nodes:
            return None
        return min(nodes, key=lambda item: geometry.distance(item[1]))[0]


def nearest_metric_points(a: LineString, b: LineString) -> tuple[Point, Point]:
    """Return the nearest points on ``a`` and ``b`` respectively (projected CRS).

    Thin wrapper over :func:`shapely.ops.nearest_points` so callers do not import
    shapely directly; the two returned points realize ``a.distance(b)``.
    """
    point_a, point_b = nearest_points(a, b)
    return point_a, point_b
