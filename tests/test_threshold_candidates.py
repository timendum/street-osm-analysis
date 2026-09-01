"""Tests for :func:`strade.joiner.find_candidate_pairs` street-level pairing.

The ``threshold`` command reports "almost joined" *street* pairs to help tune the
proximity threshold. The group's ways are joined into streets first, so ways in
the same street (joined transitively via shared nodes and/or intermediate ways)
are never compared to each other — they are one unit. Only two *distinct*
streets whose exact geometry-to-geometry gap is in ``[threshold, 2 * threshold)``
are reported.

These tests build a synthetic name group where way A and way C are joined
transitively through way B (A shares a node with B, B shares a node with C) yet
are more than ``threshold`` but less than ``2 * threshold`` apart pairwise. A, B
and C collapse into one street, so no A/B/C pair is ever emitted. A genuinely
separate way D in the band forms its own street and yields exactly one
street pair, guarding against both over- and under-reporting.
"""

from __future__ import annotations

import unittest

from strade.geometry import Projector
from strade.joiner import find_candidate_pairs
from strade.models import HighwayWay, NameGroup, NodeRef
from strade.reporter import Reporter

# Proximity threshold in meters used across these tests.
_THRESHOLD_M = 100.0

_NAME = "Via Roma"
_KEY = "roma"


def _way(way_id: int, node_ids: list[int], coords: list[NodeRef]) -> HighwayWay:
    """Build a highway way with explicit node ids and resolved coordinates.

    ``node_ids`` is kept separate from the coordinate node ids so a way can share
    an OSM node id with another way (driving a Certain_Join) independently of the
    two-vertex geometry used for the projected-distance comparison.
    """
    return HighwayWay(
        way_id=way_id,
        name=_NAME,
        node_ids=node_ids,
        coords=coords,
    )


def _segment(lon: float) -> list[NodeRef]:
    """A short north-south segment at ``lon`` (two coords, ~66 m tall)."""
    return [
        NodeRef(node_id=int(lon * 1e7), lon=lon, lat=45.0),
        NodeRef(node_id=int(lon * 1e7) + 1, lon=lon, lat=45.0006),
    ]


class FindCandidatePairsComponentFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.projector = Projector()
        # A dedicated reporter per test; no warnings are expected.
        self.reporter = Reporter(verbosity=0)

    def _street_pairs(self, group: NameGroup) -> list[tuple[int, int]]:
        found = find_candidate_pairs(group, _THRESHOLD_M, self.projector, self.reporter)
        return [
            (min(p.street_id_a, p.street_id_b), max(p.street_id_a, p.street_id_b))
            for p in found
        ]

    def test_single_joined_street_yields_no_pairs(self) -> None:
        # A (lon 7.0) and C (lon 7.0018) are ~142 m apart: in [100, 200).
        # B (lon 7.0009) bridges them by sharing node id 500 with A and node id
        # 501 with C, so A-B-C collapse into one joined street via Certain_Join.
        way_a = _way(1, [500, 100], _segment(7.0))
        way_b = _way(2, [500, 501], _segment(7.0009))
        way_c = _way(3, [501, 300], _segment(7.0018))
        group = NameGroup(name=_NAME, key=_KEY, ways=[way_a, way_b, way_c])

        # A, B and C are one street, so there is no second street to compare it
        # to: nothing is reported.
        self.assertEqual(self._street_pairs(group), [])

    def test_separate_street_pair_is_reported(self) -> None:
        # Same A-B-C joined chain as above (one street, street_id 1), plus a
        # genuinely separate way D (lon 7.0034) that shares no node and is
        # ~126 m from C: in [100, 200) and its own street (street_id 4).
        way_a = _way(1, [500, 100], _segment(7.0))
        way_b = _way(2, [500, 501], _segment(7.0009))
        way_c = _way(3, [501, 300], _segment(7.0018))
        way_d = _way(4, [900, 901], _segment(7.0034))
        group = NameGroup(name=_NAME, key=_KEY, ways=[way_a, way_b, way_c, way_d])

        pairs = self._street_pairs(group)

        # The two distinct streets (A-B-C and D) sit in the band, so exactly one
        # street pair is reported — identified by each street's smallest way id.
        self.assertEqual(pairs, [(1, 4)])

    def test_reported_pair_carries_nearest_way_and_node(self) -> None:
        # Street A-B-C's nearest fragment to D is C (way 3); D is way 4. The gap
        # runs between C's east end and D, so the nearest nodes are the shared
        # lon of each segment's vertices. Confirm the debug extras are populated.
        way_a = _way(1, [500, 100], _segment(7.0))
        way_b = _way(2, [500, 501], _segment(7.0009))
        way_c = _way(3, [501, 300], _segment(7.0018))
        way_d = _way(4, [900, 901], _segment(7.0034))
        group = NameGroup(name=_NAME, key=_KEY, ways=[way_a, way_b, way_c, way_d])

        found = find_candidate_pairs(group, _THRESHOLD_M, self.projector, self.reporter)
        self.assertEqual(len(found), 1)
        pair = found[0]

        # street_id_a < street_id_b by construction (sorted pos order), so A-B-C
        # is side A and D is side B.
        self.assertEqual(pair.street_id_a, 1)
        self.assertEqual(pair.street_id_b, 4)
        # The nearest way on the A-B-C street is fragment C (way 3); on the other
        # side it is D itself (way 4).
        self.assertEqual(pair.way_id_a, 3)
        self.assertEqual(pair.way_id_b, 4)
        # The nearest nodes are real vertices of C (lon 7.0018) and D (lon 7.0034).
        self.assertIn(pair.node_id_a, {n.node_id for n in _segment(7.0018)})
        self.assertIn(pair.node_id_b, {n.node_id for n in _segment(7.0034)})


if __name__ == "__main__":
    unittest.main()
