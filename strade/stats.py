"""The ``kpi`` command's aggregation: per-``norm_name`` distribution measures.

For each street name with more than ``min_count`` streets, measures how spread
out its streets are. :func:`compute_kpis` does the aggregation, :func:`write_csv`
renders. Region KPIs need ``admin_level=4`` boundaries; without them those fields
are ``None`` / blank.

CSV columns (see :data:`CSV_HEADER`):

- ``count`` — number of streets with the name.
- ``mean_lon`` / ``mean_lat`` — WGS84 mean center of the streets.
- ``std_distance_km`` — RMS distance from the mean center (higher = scattered).
- ``top_region`` — region where the name is most over-represented.
- ``top_region_lq`` — location quotient of the top region (higher = concentrated
  there; 1 = average, 3 = 3x nationwide).
- ``top_region_share`` — fraction (0–1) of the top region's streets with the name.
- ``effective_regions`` — number of regions the name effectively spans (higher =
  spread; 1 = one region).
- ``region_gini`` — Gini of region weights (0 = even, ~1 = piled into few).
- ``skew_score`` — 0–1 composite sort key; higher = more skewed overall.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from shapely import Point, STRtree

from strade.geometry import Projector

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import TextIO

    from strade.models import CityArea
    from strade.store import StreetPoint


class PointProjector(Protocol):
    """Structural projection interface :func:`compute_kpis` needs.

    Satisfied by :class:`~strade.geometry.Projector`; tests pass a stand-in.
    """

    def transform_point(self, lon: float, lat: float) -> tuple[float, float]: ...


# Douglas-Peucker tolerance (WGS84 degrees, ~0.005 deg ≈ 500 m) applied to region
# boundaries before indexing for performance reasons.
_REGION_SIMPLIFY_TOLERANCE_DEG = 0.005


class RegionIndex:
    """STRtree over ``admin_level=4`` region boundaries for point-in-region lookup.

    :meth:`region_of` maps a street point to its containing region's index so
    :func:`compute_kpis` can tally streets per region. Boundaries are
    Douglas-Peucker simplified before indexing for speed. A point in no region
    yields ``None``.
    """

    def __init__(self, regions: Sequence[CityArea]) -> None:
        self._names: list[str] = [area.name or area.osm_ref for area in regions]
        # Simplify each boundary before indexing (see the class docstring). An
        # empty STRtree is valid and matches nothing, so a database with no region
        # boundaries yields region_of() == None for every point.
        simplified = [
            area.geometry.simplify(_REGION_SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
            for area in regions
        ]
        self._tree = STRtree(simplified)

    def __len__(self) -> int:
        """Number of indexed regions."""
        return len(self._names)

    def region_of(self, lon: float, lat: float) -> int | None:
        """Return the index of the region containing ``(lon, lat)``, or ``None``."""
        hits = self._tree.query(Point(lon, lat), predicate="intersects")
        if len(hits) == 0:
            return None
        return int(hits[0])

    def region_name(self, index: int) -> str:
        """Return the display name of the region at ``index`` (in tree order)."""
        return self._names[index]


# CSV header (see module docstring for column meanings). A new KPI adds a column
# here and a matching field on NameKpi.
CSV_HEADER: tuple[str, ...] = (
    "norm_name",
    "count",
    "mean_lon",
    "mean_lat",
    "std_distance_km",
    "top_region",
    "top_region_lq",
    "top_region_share",
    "effective_regions",
    "region_gini",
    "skew_score",
)


@dataclass(frozen=True)
class NameKpi:
    """Distribution KPIs for one ``norm_name`` (one field per CSV column).

    Region fields (``top_region`` onward) are ``None`` without ``admin_level=4``
    boundaries or when the name's streets fell in no region.
    """

    norm_name: str
    count: int
    mean_lon: float
    mean_lat: float
    std_distance_km: float
    top_region: str | None = None
    top_region_lq: float | None = None
    top_region_share: float | None = None
    effective_regions: float | None = None
    region_gini: float | None = None
    skew_score: float = 0.0


def _standard_distance_km(
    metric_points: list[tuple[float, float]],
    mean_x: float,
    mean_y: float,
) -> float:
    """RMS distance (km) of projected metric points from ``(mean_x, mean_y)``.

    A single point yields ``0.0``.
    """
    total_sq = sum((x - mean_x) ** 2 + (y - mean_y) ** 2 for x, y in metric_points)
    return math.sqrt(total_sq / len(metric_points)) / 1000.0


def _region_concentration(
    name_region_counts: dict[int, int],
    region_totals: dict[int, int],
    name_total: int,
    placed_total: int,
    regions: RegionIndex,
) -> tuple[str, float, float] | None:
    """Return the name's top region as ``(name, location_quotient, share)``.

    Per region, ``share = n_r / S_r`` and ``lq = share / (name_total /
    placed_total)``; the region with the highest ``lq`` wins. Returns ``None``
    when the name reached no region. Iterates in index order so ties are stable.
    """
    if name_total == 0 or placed_total == 0:
        return None
    global_share = name_total / placed_total
    best_index = -1
    best_lq = -1.0
    best_share = 0.0
    # Deterministic: iterate regions in index order so ties resolve to the
    # lowest region index (and thus a stable region name) rather than dict order.
    for region_index in sorted(name_region_counts):
        n_r = name_region_counts[region_index]
        share = n_r / region_totals[region_index]
        lq = share / global_share
        if lq > best_lq:
            best_lq = lq
            best_share = share
            best_index = region_index
    return regions.region_name(best_index), best_lq, best_share


def _region_spread(
    name_region_counts: dict[int, int],
    region_totals: dict[int, int],
    region_count: int,
) -> tuple[float, float] | None:
    """Return ``(effective_regions, region_gini)`` for one name's region spread.

    Both use density-normalized weights ``w_r = n_r / S_r`` renormalized to sum
    to one. ``effective_regions`` is ``exp`` of their Shannon entropy (1 = one
    region). ``region_gini`` is the Gini over *all* indexed regions, unreached
    ones as zero (0 = even, ~1 = few). Returns ``None`` when the name reached no
    region or none are indexed.
    """
    weights = [
        name_region_counts[r] / region_totals[r] for r in sorted(name_region_counts)
    ]
    weight_sum = sum(weights)
    if weight_sum == 0 or region_count == 0:
        return None
    weights = [w / weight_sum for w in weights]

    entropy = -sum(w * math.log(w) for w in weights if w > 0)
    effective_regions = math.exp(entropy)

    # Gini over all indexed regions: pad unreached regions with zero. With
    # Σ weights = 1 this reduces to (2 Σ i·x_i) / n − (n + 1) / n over the
    # ascending-sorted padded weights.
    padded = sorted(weights + [0.0] * (region_count - len(weights)))
    n = len(padded)
    cumulative = sum((i + 1) * w for i, w in enumerate(padded))
    region_gini = (2.0 * cumulative) / n - (n + 1) / n
    return effective_regions, region_gini


def _skew_scores(kpis: Sequence[NameKpi]) -> list[float]:
    """Return a rough 0–1 skewness score per KPI, in the same order.

    Each component is min-max normalized across the batch and flipped so higher
    always means more skewed (``std_distance_km`` and ``effective_regions`` are
    inverted; the rest as-is). The score is the mean of the present components, so
    names without region KPIs still score from ``std_distance_km`` alone. A
    component constant across the batch contributes ``0``; with a single scored
    name every component is constant, so the score is ``0`` and the ``norm_name``
    tiebreak decides order.
    """

    def normalized(values: list[float | None], *, invert: bool) -> list[float | None]:
        present = [v for v in values if v is not None]
        if not present:
            return [None] * len(values)
        low, high = min(present), max(present)
        span = high - low
        out: list[float | None] = []
        for v in values:
            if v is None:
                out.append(None)
            elif span == 0:
                out.append(0.0)
            else:
                unit = (v - low) / span
                out.append(1.0 - unit if invert else unit)
        return out

    components = [
        normalized([k.std_distance_km for k in kpis], invert=True),
        normalized([k.top_region_lq for k in kpis], invert=False),
        normalized([k.top_region_share for k in kpis], invert=False),
        normalized([k.effective_regions for k in kpis], invert=True),
        normalized([k.region_gini for k in kpis], invert=False),
    ]

    scores: list[float] = []
    for i in range(len(kpis)):
        parts: list[float] = [
            value for column in components if (value := column[i]) is not None
        ]
        scores.append(sum(parts) / len(parts) if parts else 0.0)
    return scores


def compute_kpis(
    points: Iterable[StreetPoint],
    min_count: int,
    projector: PointProjector | None = None,
    regions: RegionIndex | None = None,
) -> list[NameKpi]:
    """Group street points by ``norm_name`` and compute per-name distribution KPIs.

    Points are projected to the metric CRS (via ``projector``, default
    :class:`~strade.geometry.Projector`) and buffered per name; names with more
    than ``min_count`` streets become a :class:`NameKpi`. ``points`` is consumed
    once. When ``regions`` is given, the same pass tallies per-region totals to
    fill the region KPIs; otherwise those stay ``None``. Sorted by descending
    ``skew_score``, ties broken by ``norm_name``.
    """
    if projector is None:
        projector = Projector()

    # Per-name accumulators: projected (x, y) metres for the spread, and the
    # running lon/lat sums so the mean center can be reported in WGS84 without a
    # reverse projection.
    metric: dict[str, list[tuple[float, float]]] = {}
    lonlat_sums: dict[str, tuple[float, float]] = {}
    # Region tallies (only used when `regions` is given): streets per region and,
    # nested, each name's streets per region, plus how many streets were placed
    # in any region at all (the location-quotient denominator base).
    region_totals: dict[int, int] = {}
    name_region_counts: dict[str, dict[int, int]] = {}
    placed_total = 0
    for point in points:
        x, y = projector.transform_point(point.lon, point.lat)
        metric.setdefault(point.norm_name, []).append((x, y))
        lon_sum, lat_sum = lonlat_sums.get(point.norm_name, (0.0, 0.0))
        lonlat_sums[point.norm_name] = (lon_sum + point.lon, lat_sum + point.lat)
        if regions is not None:
            region_index = regions.region_of(point.lon, point.lat)
            if region_index is not None:
                region_totals[region_index] = region_totals.get(region_index, 0) + 1
                per_region = name_region_counts.setdefault(point.norm_name, {})
                per_region[region_index] = per_region.get(region_index, 0) + 1
                placed_total += 1

    kpis: list[NameKpi] = []
    for norm_name, pts in metric.items():
        count = len(pts)
        if count <= min_count:
            continue
        mean_x = sum(x for x, _ in pts) / count
        mean_y = sum(y for _, y in pts) / count
        lon_sum, lat_sum = lonlat_sums[norm_name]
        top_region = None
        top_region_lq = None
        top_region_share = None
        effective_regions = None
        region_gini = None
        if regions is not None:
            per_region = name_region_counts.get(norm_name, {})
            concentration = _region_concentration(
                per_region,
                region_totals,
                name_total=sum(per_region.values()),
                placed_total=placed_total,
                regions=regions,
            )
            if concentration is not None:
                top_region, top_region_lq, top_region_share = concentration
            spread = _region_spread(per_region, region_totals, len(regions))
            if spread is not None:
                effective_regions, region_gini = spread
        kpis.append(
            NameKpi(
                norm_name=norm_name,
                count=count,
                mean_lon=lon_sum / count,
                mean_lat=lat_sum / count,
                std_distance_km=_standard_distance_km(pts, mean_x, mean_y),
                top_region=top_region,
                top_region_lq=top_region_lq,
                top_region_share=top_region_share,
                effective_regions=effective_regions,
                region_gini=region_gini,
            )
        )

    # Roll the per-name KPIs up into one rough skew score (batch-normalized, so it
    # needs the whole list) and attach it. NameKpi is frozen, so replace in place.
    scores = _skew_scores(kpis)
    kpis = [replace(kpi, skew_score=score) for kpi, score in zip(kpis, scores)]

    # Most skewed first; the norm_name tiebreak keeps equal-score names in a
    # stable, reproducible order.
    kpis.sort(key=lambda k: (-k.skew_score, k.norm_name))
    return kpis


def write_csv(kpis: Iterable[NameKpi], stream: TextIO) -> int:
    """Write one CSV row per :class:`NameKpi` to ``stream`` and return the row count.

    Emits :data:`CSV_HEADER` first, then one row per KPI in the given order.
    Unset region fields render as blank cells.
    """
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    rows = 0
    for kpi in kpis:
        # None region fields render as empty cells so the column count stays fixed.
        lq = "" if kpi.top_region_lq is None else f"{kpi.top_region_lq:.3f}"
        share = "" if kpi.top_region_share is None else f"{kpi.top_region_share:.4f}"
        eff = "" if kpi.effective_regions is None else f"{kpi.effective_regions:.2f}"
        gini = "" if kpi.region_gini is None else f"{kpi.region_gini:.4f}"
        writer.writerow(
            (
                kpi.norm_name,
                kpi.count,
                f"{kpi.mean_lon:.6f}",
                f"{kpi.mean_lat:.6f}",
                f"{kpi.std_distance_km:.3f}",
                kpi.top_region or "",
                lq,
                share,
                eff,
                gini,
                f"{kpi.skew_score:.4f}",
            )
        )
        rows += 1
    return rows
