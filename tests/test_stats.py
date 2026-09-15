"""Tests for the ``kpi`` command's pure aggregation (:mod:`strade.stats`)."""

from __future__ import annotations

import io
import math
import unittest

from shapely import Polygon

from strade.models import CityArea
from strade.stats import CSV_HEADER, NameKpi, RegionIndex, compute_kpis, write_csv
from strade.store import StreetPoint


def _region(name: str, x0: float, y0: float, x1: float, y1: float) -> CityArea:
    """A rectangular admin_level=4 region boundary named ``name`` (lon/lat)."""
    return CityArea(
        name=name,
        postal_code=None,
        istat=None,
        catasto=None,
        wikidata=None,
        osm_id=1,
        from_way=False,
        geometry=Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]),
        admin_level="4",
    )


class _IdentityProjector:
    """A stand-in projector that treats lon/lat as meters directly.

    Using an identity transform keeps the standard-distance arithmetic exact and
    independent of the real CRS, so the tests assert on hand-computable numbers
    rather than pyproj output. ``compute_kpis`` only calls ``transform_point``.
    """

    def transform_point(self, lon: float, lat: float) -> tuple[float, float]:
        return lon, lat


def _point(norm_name: str, lon: float, lat: float) -> StreetPoint:
    return StreetPoint(norm_name=norm_name, lon=lon, lat=lat)


class ComputeKpisTest(unittest.TestCase):
    def test_drops_names_at_or_below_min_count(self) -> None:
        # "rare" has exactly min_count (2) points -> dropped; "common" has 3 -> kept.
        points = [
            _point("rare", 0.0, 0.0),
            _point("rare", 1.0, 0.0),
            _point("common", 0.0, 0.0),
            _point("common", 1.0, 0.0),
            _point("common", 2.0, 0.0),
        ]
        kpis = compute_kpis(points, min_count=2, projector=_IdentityProjector())
        self.assertEqual([k.norm_name for k in kpis], ["common"])

    def test_standard_distance_and_mean_center(self) -> None:
        # Four points on a unit square centred at (0.5, 0.5); each corner is
        # sqrt(0.5) meters from the center, so the RMS distance is sqrt(0.5) m.
        points = [
            _point("sq", 0.0, 0.0),
            _point("sq", 1.0, 0.0),
            _point("sq", 0.0, 1.0),
            _point("sq", 1.0, 1.0),
        ]
        (kpi,) = compute_kpis(points, min_count=1, projector=_IdentityProjector())
        self.assertEqual(kpi.count, 4)
        self.assertAlmostEqual(kpi.mean_lon, 0.5)
        self.assertAlmostEqual(kpi.mean_lat, 0.5)
        # metres -> km: sqrt(0.5) m = 0.0007071.. km
        self.assertAlmostEqual(kpi.std_distance_km, math.sqrt(0.5) / 1000.0)

    def test_clustered_name_scores_above_scattered_name(self) -> None:
        # "tight" sits in a 1-meter box; "wide" spans 1000 meters. The result is
        # sorted most-skewed first, and a tighter cluster is more skewed, so
        # "tight" must come first with the smaller standard distance.
        points = [
            _point("tight", 0.0, 0.0),
            _point("tight", 1.0, 0.0),
            _point("tight", 0.0, 1.0),
            _point("wide", 0.0, 0.0),
            _point("wide", 1000.0, 0.0),
            _point("wide", 0.0, 1000.0),
        ]
        kpis = compute_kpis(points, min_count=1, projector=_IdentityProjector())
        self.assertEqual([k.norm_name for k in kpis], ["tight", "wide"])
        self.assertGreater(kpis[0].skew_score, kpis[1].skew_score)
        self.assertLess(kpis[0].std_distance_km, kpis[1].std_distance_km)

    def test_ties_broken_by_norm_name_ascending(self) -> None:
        # Two names with identical spreads must order deterministically by key.
        points = [
            _point("bbb", 0.0, 0.0),
            _point("bbb", 2.0, 0.0),
            _point("aaa", 10.0, 0.0),
            _point("aaa", 12.0, 0.0),
        ]
        kpis = compute_kpis(points, min_count=1, projector=_IdentityProjector())
        self.assertEqual([k.norm_name for k in kpis], ["aaa", "bbb"])
        self.assertAlmostEqual(kpis[0].std_distance_km, kpis[1].std_distance_km)

    def test_empty_input_yields_no_rows(self) -> None:
        self.assertEqual(
            compute_kpis([], min_count=1, projector=_IdentityProjector()), []
        )

    def test_region_fields_none_without_regions(self) -> None:
        # No RegionIndex passed: the region-concentration fields stay unset.
        points = [_point("x", 0.0, 0.0), _point("x", 1.0, 0.0)]
        (kpi,) = compute_kpis(points, min_count=1, projector=_IdentityProjector())
        self.assertIsNone(kpi.top_region)
        self.assertIsNone(kpi.top_region_lq)
        self.assertIsNone(kpi.top_region_share)

    def test_default_projector_reports_meters(self) -> None:
        # With the real projector, two points ~1 degree of latitude apart in
        # Italy should be on the order of 100 km, and the mean center should sit
        # between them. This guards the metric-CRS wiring, not exact geodesy.
        points = [
            _point("x", 7.3, 45.0),
            _point("x", 7.3, 46.0),
        ]
        (kpi,) = compute_kpis(points, min_count=1)
        self.assertAlmostEqual(kpi.mean_lat, 45.5)
        # ~1 deg latitude ~= 111 km; standard distance from the midpoint is half
        # that separation, so roughly 55 km. Wide bounds keep it a wiring check.
        self.assertGreater(kpi.std_distance_km, 40.0)
        self.assertLess(kpi.std_distance_km, 70.0)


class RegionConcentrationTest(unittest.TestCase):
    def test_normalizes_against_region_street_density(self) -> None:
        # Two regions of very different street density. "Big" holds 12 streets, 2
        # of them "target". "Small" holds 3 streets, 2 of them "target". Both
        # regions carry the SAME absolute count of target streets (2), so a raw
        # count would tie them. But target is 2/3 of Small's streets versus only
        # 2/12 of Big's, so normalized against each region's density Small is the
        # concentration outlier. This is the whole point of the KPI.
        regions = RegionIndex(
            [
                _region("Big", 0.0, 0.0, 10.0, 10.0),
                _region("Small", 20.0, 0.0, 21.0, 1.0),
            ]
        )
        points: list[StreetPoint] = []
        # Big: 2 target + 10 filler (spread so their own KPIs are irrelevant).
        points += [_point("target", 1.0, 1.0), _point("target", 2.0, 2.0)]
        points += [_point("filler", 1.0 + i * 0.1, 5.0) for i in range(10)]
        # Small: 2 target + 1 filler.
        points += [_point("target", 20.2, 0.2), _point("target", 20.4, 0.4)]
        points += [_point("filler", 20.6, 0.6)]

        kpis = {
            k.norm_name: k
            for k in compute_kpis(
                points, min_count=1, projector=_IdentityProjector(), regions=regions
            )
        }
        target = kpis["target"]
        self.assertEqual(target.top_region, "Small")
        # Narrow the Optional fields for the type checker before arithmetic.
        assert target.top_region_share is not None
        assert target.top_region_lq is not None
        # global share = 4 target / 15 placed; Small share = 2/3.
        self.assertAlmostEqual(target.top_region_share, 2.0 / 3.0)
        self.assertAlmostEqual(target.top_region_lq, (2.0 / 3.0) / (4.0 / 15.0))
        # ... and the location quotient exceeds 1 (over-represented) there.
        self.assertGreater(target.top_region_lq, 1.0)

    def test_points_outside_all_regions_are_not_placed(self) -> None:
        # A name whose streets all fall outside every region boundary gets no
        # region assignment, so its region fields stay None.
        regions = RegionIndex([_region("R", 0.0, 0.0, 1.0, 1.0)])
        points = [_point("offshore", 50.0, 50.0), _point("offshore", 51.0, 51.0)]
        (kpi,) = compute_kpis(
            points, min_count=1, projector=_IdentityProjector(), regions=regions
        )
        self.assertIsNone(kpi.top_region)
        self.assertIsNone(kpi.top_region_lq)

    def test_empty_region_index_places_nothing(self) -> None:
        # A RegionIndex over no boundaries is valid and simply places no street.
        regions = RegionIndex([])
        self.assertEqual(len(regions), 0)
        points = [_point("x", 0.0, 0.0), _point("x", 1.0, 0.0)]
        (kpi,) = compute_kpis(
            points, min_count=1, projector=_IdentityProjector(), regions=regions
        )
        self.assertIsNone(kpi.top_region)

    def test_region_index_uses_osm_ref_when_name_missing(self) -> None:
        # A region without a name falls back to its OSM reference for the label.
        region = _region("", 0.0, 0.0, 1.0, 1.0)
        object.__setattr__(region, "name", None)
        regions = RegionIndex([region])
        self.assertEqual(regions.region_name(0), region.osm_ref)


class RegionSpreadTest(unittest.TestCase):
    def _two_regions(self) -> RegionIndex:
        # Two equal-footprint regions far enough apart not to overlap.
        return RegionIndex(
            [
                _region("A", 0.0, 0.0, 1.0, 1.0),
                _region("B", 10.0, 0.0, 11.0, 1.0),
            ]
        )

    def test_even_spread_gives_zero_gini_and_full_effective_count(self) -> None:
        # Same street total per region and the name equally present in each ->
        # equal density weights -> perfectly even: gini 0, effective = 2 regions.
        regions = self._two_regions()
        points = [_point("even", 0.2 + i * 0.1, 0.5) for i in range(3)]
        points += [_point("even", 10.2 + i * 0.1, 0.5) for i in range(3)]
        (kpi,) = compute_kpis(
            points, min_count=1, projector=_IdentityProjector(), regions=regions
        )
        assert kpi.region_gini is not None
        assert kpi.effective_regions is not None
        self.assertAlmostEqual(kpi.region_gini, 0.0)
        self.assertAlmostEqual(kpi.effective_regions, 2.0)

    def test_confined_to_one_region_gives_effective_one(self) -> None:
        # The name reaches only region A though two are indexed: it effectively
        # occupies one region, and the Gini over both regions is 0.5 (one of two
        # carries all the weight).
        regions = self._two_regions()
        points = [_point("solo", 0.2 + i * 0.1, 0.5) for i in range(4)]
        (kpi,) = compute_kpis(
            points, min_count=1, projector=_IdentityProjector(), regions=regions
        )
        assert kpi.region_gini is not None
        assert kpi.effective_regions is not None
        self.assertAlmostEqual(kpi.effective_regions, 1.0)
        self.assertAlmostEqual(kpi.region_gini, 0.5)

    def test_more_empty_regions_raise_the_gini_of_a_confined_name(self) -> None:
        # A name confined to one region reads as MORE unequal the more regions it
        # is absent from: 1-of-2 -> 0.5, 1-of-4 -> 0.75. This is why the Gini is
        # taken over all indexed regions, not just the ones the name reaches.
        four = RegionIndex(
            [
                _region("A", 0.0, 0.0, 1.0, 1.0),
                _region("B", 10.0, 0.0, 11.0, 1.0),
                _region("C", 20.0, 0.0, 21.0, 1.0),
                _region("D", 30.0, 0.0, 31.0, 1.0),
            ]
        )
        points = [_point("solo", 0.2 + i * 0.1, 0.5) for i in range(4)]
        (kpi,) = compute_kpis(
            points, min_count=1, projector=_IdentityProjector(), regions=four
        )
        assert kpi.region_gini is not None
        self.assertAlmostEqual(kpi.region_gini, 0.75)

    def test_spread_is_density_normalized_not_raw_count(self) -> None:
        # Region A holds far more streets than B, but the name has the SAME
        # per-region density in both (half of each region's streets). Density
        # weights are therefore equal, so the spread reads as perfectly even
        # (gini 0, effective 2) even though A carries more of the name by count.
        regions = self._two_regions()
        points: list[StreetPoint] = []
        # A: 8 streets, 4 of them "dense" (share 1/2).
        points += [_point("dense", 0.1 + i * 0.05, 0.5) for i in range(4)]
        points += [_point("filler", 0.1 + i * 0.05, 0.8) for i in range(4)]
        # B: 2 streets, 1 of them "dense" (share 1/2, same density as A).
        points += [_point("dense", 10.3, 0.5)]
        points += [_point("filler", 10.6, 0.5)]
        kpis = {
            k.norm_name: k
            for k in compute_kpis(
                points, min_count=1, projector=_IdentityProjector(), regions=regions
            )
        }
        dense = kpis["dense"]
        assert dense.region_gini is not None
        assert dense.effective_regions is not None
        # Equal density in both regions -> even, despite A holding 4x the count.
        self.assertAlmostEqual(dense.region_gini, 0.0)
        self.assertAlmostEqual(dense.effective_regions, 2.0)

    def test_spread_none_without_regions(self) -> None:
        points = [_point("x", 0.0, 0.0), _point("x", 1.0, 0.0)]
        (kpi,) = compute_kpis(points, min_count=1, projector=_IdentityProjector())
        self.assertIsNone(kpi.effective_regions)
        self.assertIsNone(kpi.region_gini)


class WriteCsvTest(unittest.TestCase):
    def test_header_and_rows(self) -> None:
        kpis = [
            NameKpi(
                norm_name="wide",
                count=5,
                mean_lon=7.123456,
                mean_lat=45.654321,
                std_distance_km=12.3456,
            ),
        ]
        stream = io.StringIO()
        rows = write_csv(kpis, stream)
        self.assertEqual(rows, 1)
        lines = stream.getvalue().splitlines()
        self.assertEqual(lines[0], ",".join(CSV_HEADER))
        # Region fields are unset here, so all five region columns are blank; the
        # hand-built KPI keeps the default skew_score of 0.0.
        self.assertEqual(lines[1], "wide,5,7.123456,45.654321,12.346,,,,,,0.0000")

    def test_region_columns_rendered_when_present(self) -> None:
        kpis = [
            NameKpi(
                norm_name="target",
                count=4,
                mean_lon=9.0,
                mean_lat=45.0,
                std_distance_km=1.5,
                top_region="Lombardia",
                top_region_lq=2.5,
                top_region_share=0.6667,
                effective_regions=1.83,
                region_gini=0.42,
            ),
        ]
        stream = io.StringIO()
        write_csv(kpis, stream)
        row = stream.getvalue().splitlines()[1]
        self.assertEqual(
            row,
            "target,4,9.000000,45.000000,1.500,Lombardia,2.500,0.6667,1.83,0.4200,0.0000",
        )

    def test_region_name_with_comma_is_quoted(self) -> None:
        # csv must quote a region label containing a comma so the columns align.
        kpis = [
            NameKpi(
                norm_name="x",
                count=2,
                mean_lon=0.0,
                mean_lat=0.0,
                std_distance_km=0.0,
                top_region="Trentino, Alto Adige",
                top_region_lq=1.0,
                top_region_share=0.5,
            ),
        ]
        stream = io.StringIO()
        write_csv(kpis, stream)
        row = stream.getvalue().splitlines()[1]
        self.assertIn('"Trentino, Alto Adige"', row)

    def test_empty_writes_header_only(self) -> None:
        stream = io.StringIO()
        rows = write_csv([], stream)
        self.assertEqual(rows, 0)
        self.assertEqual(stream.getvalue().splitlines(), [",".join(CSV_HEADER)])


if __name__ == "__main__":
    unittest.main()
