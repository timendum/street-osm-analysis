"""Tests for the ``cities`` command's pattern file, DB matching, and aggregation."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from shapely import Polygon

from strade import store
from strade.cities import CityIndex, assign_matches, build_city_index, write_csv
from strade.models import CityArea, HighwayWay, NodeRef
from strade.patterns import PatternError, Patterns, parse_pattern_file
from strade.reporter import Reporter
from strade.store import MatchedWay


def _square(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    """A rectangular boundary from two opposite corners (lon/lat)."""
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _city(name: str, poly: Polygon, **tags: str | None) -> CityArea:
    osm_id = tags.get("osm_id")
    from_way = tags.get("from_way")
    return CityArea(
        name=name,
        postal_code=tags.get("postal_code"),
        istat=tags.get("istat"),
        catasto=tags.get("catasto"),
        wikidata=tags.get("wikidata"),
        osm_id=int(osm_id) if osm_id is not None else 1,
        from_way=bool(from_way),
        geometry=poly,
    )


class ParsePatternFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "patterns.txt"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_reads_one_pattern_per_line_in_order(self) -> None:
        self._write("%roma%\nvia garibaldi%\n%matteotti%\n")
        self.assertEqual(
            parse_pattern_file(self.path),
            Patterns(includes=["%roma%", "via garibaldi%", "%matteotti%"]),
        )

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        self._write("# streets named after Roma\n\n%roma%\n   # indented\n\n%mazzini%\n")
        self.assertEqual(
            parse_pattern_file(self.path), Patterns(includes=["%roma%", "%mazzini%"])
        )

    def test_surrounding_whitespace_trimmed(self) -> None:
        self._write("   %roma%   \n")
        self.assertEqual(parse_pattern_file(self.path), Patterns(includes=["%roma%"]))

    def test_duplicate_patterns_preserved(self) -> None:
        self._write("%roma%\n%roma%\n")
        self.assertEqual(
            parse_pattern_file(self.path), Patterns(includes=["%roma%", "%roma%"])
        )

    def test_empty_file_raises(self) -> None:
        self._write("# only a comment\n\n")
        with self.assertRaises(PatternError):
            parse_pattern_file(self.path)

    def test_minus_lines_become_excludes(self) -> None:
        self._write("% Marconi\n- %di Sasso Marconi%\n-%Pasquale Marconi%\n")
        self.assertEqual(
            parse_pattern_file(self.path),
            Patterns(
                includes=["% Marconi"],
                excludes=["%di Sasso Marconi%", "%Pasquale Marconi%"],
            ),
        )

    def test_only_excludes_raises(self) -> None:
        self._write("- %foo%\n")
        with self.assertRaises(PatternError):
            parse_pattern_file(self.path)

    def test_bare_minus_line_is_dropped(self) -> None:
        # A line that is only "-" (or "- ") has no pattern and is skipped.
        self._write("%roma%\n-\n-   \n")
        self.assertEqual(parse_pattern_file(self.path), Patterns(includes=["%roma%"]))


class CityIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        # Two non-overlapping unit squares and one that shares an edge with A.
        self.a = _city("A", _square(0, 0, 10, 10))
        self.b = _city("B", _square(20, 20, 30, 30))
        self.index = CityIndex([self.a, self.b])

    def test_point_inside_flags_only_its_city(self) -> None:
        self.index.mark_point(5, 5)
        flags = {m.area.name: m.matched for m in self.index.matches}
        self.assertTrue(flags["A"])
        self.assertFalse(flags["B"])

    def test_point_outside_all_flags_nothing(self) -> None:
        self.index.mark_point(100, 100)
        self.assertFalse(any(m.matched for m in self.index.matches))

    def test_point_on_boundary_is_contained(self) -> None:
        # A point on the polygon edge intersects it, so it counts.
        self.index.mark_point(0, 5)
        flags = {m.area.name: m.matched for m in self.index.matches}
        self.assertTrue(flags["A"])

    def test_repeated_points_stay_matched(self) -> None:
        self.index.mark_point(5, 5)
        self.index.mark_point(6, 6)
        self.assertTrue(self.index.matches[0].matched)

    def test_empty_index_matches_nothing(self) -> None:
        index = CityIndex([])
        index.mark_point(5, 5)  # must not raise
        self.assertEqual(index.matches, [])

    def test_matches_preserve_read_order(self) -> None:
        names = [m.area.name for m in self.index.matches]
        self.assertEqual(names, ["A", "B"])


class BuildCityIndexTest(unittest.TestCase):
    def test_reports_and_indexes_all_cities(self) -> None:
        stream = io.StringIO()
        reporter = Reporter(stream=stream)
        cities = [_city("A", _square(0, 0, 1, 1)), _city("B", _square(2, 2, 3, 3))]
        index = build_city_index(iter(cities), reporter)
        self.assertEqual(len(index.matches), 2)
        self.assertIn("indexed 2", stream.getvalue())


class AssignMatchesTest(unittest.TestCase):
    def test_flags_cities_and_counts_ways(self) -> None:
        a = _city("A", _square(0, 0, 10, 10))
        b = _city("B", _square(20, 20, 30, 30))
        index = CityIndex([a, b])
        ways = [
            MatchedWay(name="Via Roma", lon=5, lat=5),  # in A
            MatchedWay(name="Via Roma", lon=25, lat=25),  # in B
            MatchedWay(name="Via Roma", lon=99, lat=99),  # outside all
        ]
        consumed = assign_matches(index, iter(ways))
        self.assertEqual(consumed, 3)
        flags = {m.area.name: m.matched for m in index.matches}
        self.assertTrue(flags["A"])
        self.assertTrue(flags["B"])


class WriteCsvTest(unittest.TestCase):
    def test_header_and_rows(self) -> None:
        # A comma in the name forces the csv writer to quote the field.
        a = _city(
            "Reggio nell'Emilia, città",
            _square(0, 0, 1, 1),
            postal_code="42100",
            istat="035033",
            catasto="H223",
            wikidata="Q13361",
            osm_id="43125",
        )
        # A way-sourced boundary renders with a way/ prefix instead of relation/.
        b = _city("Bard", _square(2, 2, 3, 3), istat="007009", osm_id="99", from_way="1")
        index = CityIndex([a, b])
        index.mark_point(0.5, 0.5)  # flags A only

        out = io.StringIO()
        rows = write_csv(index.matches, out, None)
        lines = out.getvalue().splitlines()

        self.assertEqual(rows, 2)
        self.assertEqual(
            lines[0], "name,postal_code,istat,catasto,wikidata,osm_id,matched"
        )
        # A matched; the comma in the name forces quoting.
        self.assertEqual(
            lines[1],
            '"Reggio nell\'Emilia, città",42100,035033,H223,Q13361,relation/43125,true',
        )
        # B did not match; absent tags render as empty fields.
        self.assertEqual(lines[2], "Bard,,007009,,,way/99,false")
        self.assertEqual(lines[3], "Total,,,,,,1")

    def test_name_without_comma_is_not_quoted(self) -> None:
        # A slash (Aosta / Aoste) is not a CSV special char, so no quoting.
        a = _city(
            "Aosta / Aoste", _square(0, 0, 1, 1), postal_code="11100", osm_id="45489"
        )
        out = io.StringIO()
        write_csv(CityIndex([a]).matches, out, None)
        self.assertEqual(
            out.getvalue().splitlines()[1],
            "Aosta / Aoste,11100,,,,relation/45489,false",
        )

    def test_none_name_renders_empty(self) -> None:
        c = _city("", _square(0, 0, 1, 1), osm_id="7")
        out = io.StringIO()
        write_csv(CityIndex([c]).matches, out, None)
        self.assertEqual(out.getvalue().splitlines()[1], ",,,,,relation/7,false")


class ReadWaysMatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "test.db"
        conn = store.connect(self.db)
        # Seed the ways table via the extract-side writer so coords round-trip
        # through the real binary codec.
        with store.WayWriter(conn) as writer:
            writer.append(
                HighwayWay(
                    way_id=1,
                    name="Via Roma",
                    node_ids=[10, 11],
                    coords=[
                        NodeRef(node_id=10, lon=7.32, lat=45.74),
                        NodeRef(node_id=11, lon=7.33, lat=45.75),
                    ],
                )
            )
            writer.append(
                HighwayWay(
                    way_id=2,
                    name="Viale Giuseppe Garibaldi",
                    node_ids=[20],
                    coords=[NodeRef(node_id=20, lon=9.19, lat=45.46)],
                )
            )
            writer.append(
                HighwayWay(
                    way_id=3,
                    name="Via Milano",
                    node_ids=[30],
                    coords=[NodeRef(node_id=30, lon=9.0, lat=45.0)],
                )
            )
            # A matching name but no resolved coords: must be skipped.
            writer.append(
                HighwayWay(
                    way_id=4, name="Via Roma senza punti", node_ids=[40], coords=[]
                )
            )
            # Two names ending in "Marconi": the first is the scientist (kept),
            # the second a surname the exclude patterns must drop.
            writer.append(
                HighwayWay(
                    way_id=5,
                    name="Via Guglielmo Marconi",
                    node_ids=[50],
                    coords=[NodeRef(node_id=50, lon=8.0, lat=44.0)],
                )
            )
            writer.append(
                HighwayWay(
                    way_id=6,
                    name="Via Pasquale Marconi",
                    node_ids=[60],
                    coords=[NodeRef(node_id=60, lon=8.1, lat=44.1)],
                )
            )
        conn.close()

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_matches_like_pattern_case_insensitively(self) -> None:
        ways = list(store.read_ways_matching(self.db, Patterns(includes=["%roma%"])))
        names = sorted(w.name for w in ways)
        # "Via Roma" matches; "Via Roma senza punti" matches the LIKE but has no
        # coords so it is dropped.
        self.assertEqual(names, ["Via Roma"])

    def test_middle_vertex_is_the_representative_point(self) -> None:
        # Via Roma has vertices [(7.32, 45.74), (7.33, 45.75)]; the middle index
        # (len // 2 == 1) picks the second vertex, avoiding the border-prone ends.
        (way,) = store.read_ways_matching(self.db, Patterns(includes=["Via Roma"]))
        self.assertAlmostEqual(way.lon, 7.33)
        self.assertAlmostEqual(way.lat, 45.75)

    def test_multiple_patterns_are_ored(self) -> None:
        ways = list(
            store.read_ways_matching(
                self.db, Patterns(includes=["%roma%", "%garibaldi%"])
            )
        )
        names = sorted(w.name for w in ways)
        self.assertEqual(names, ["Via Roma", "Viale Giuseppe Garibaldi"])

    def test_no_match_yields_nothing(self) -> None:
        self.assertEqual(
            list(store.read_ways_matching(self.db, Patterns(includes=["%napoli%"]))), []
        )

    def test_excludes_drop_matched_names(self) -> None:
        # Both "…Marconi" names match the include; the exclude drops the surname,
        # keeping only Guglielmo Marconi.
        ways = list(
            store.read_ways_matching(
                self.db,
                Patterns(includes=["%marconi%"], excludes=["%Pasquale Marconi%"]),
            )
        )
        names = sorted(w.name for w in ways)
        self.assertEqual(names, ["Via Guglielmo Marconi"])

    def test_multiple_excludes_are_all_applied(self) -> None:
        # Excluding both Marconi names leaves nothing.
        ways = list(
            store.read_ways_matching(
                self.db,
                Patterns(
                    includes=["%marconi%"],
                    excludes=["%Pasquale Marconi%", "%Guglielmo Marconi%"],
                ),
            )
        )
        self.assertEqual(ways, [])

    def test_empty_patterns_raises(self) -> None:
        with self.assertRaises(ValueError):
            list(store.read_ways_matching(self.db, Patterns(includes=[])))


if __name__ == "__main__":
    unittest.main()
