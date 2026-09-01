"""Tests that the store groups ways by normalization key."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from strade import store
from strade.models import HighwayWay, NodeRef, Street
from strade.store import StreetWriter


def _way(way_id: int, name: str) -> HighwayWay:
    """Build a minimal two-node highway way with a resolvable geometry."""
    coords = [
        NodeRef(node_id=way_id * 10, lon=7.0, lat=45.0),
        NodeRef(node_id=way_id * 10 + 1, lon=7.001, lat=45.001),
    ]
    return HighwayWay(
        way_id=way_id,
        name=name,
        node_ids=[c.node_id for c in coords],
        coords=coords,
    )


class GroupingByKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "test.db"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _write(self, ways: list[HighwayWay]) -> None:
        conn = store.connect(self.db)
        try:
            with store.WayWriter(conn) as writer:
                for way in ways:
                    writer.append(way)
        finally:
            conn.close()

    def test_variant_names_collapse_into_one_group(self) -> None:
        self._write(
            [
                _way(1, "Viale - Avenue Giuseppe Garibaldi"),
                _way(2, "Viale Giuseppe Garibaldi"),
                _way(3, "Viale Giuseppe Garibaldi"),
                _way(4, "Via Roma"),
            ]
        )
        groups = list(store.read_groups(self.db))

        # Two distinct keys: the Garibaldi variants and Via Roma.
        self.assertEqual(len(groups), 2)

        by_key = {g.key: g for g in groups}
        garibaldi = by_key["giuseppegaribaldi"]
        self.assertEqual({w.way_id for w in garibaldi.ways}, {1, 2, 3})
        # The representative display name is the most frequent raw form.
        self.assertEqual(garibaldi.name, "Viale Giuseppe Garibaldi")

    def test_group_count_in_header_counts_distinct_keys(self) -> None:
        self._write(
            [
                _way(1, "Viale - Avenue Giuseppe Garibaldi"),
                _way(2, "Viale Giuseppe Garibaldi"),
                _way(3, "Via Roma"),
            ]
        )
        header = store.read_header(self.db)
        self.assertEqual(header.parsed_count, 0)  # set_counts not called here
        self.assertEqual(header.group_count, 2)


if __name__ == "__main__":
    unittest.main()


class ResumeStateClearedTest(unittest.TestCase):
    """The resume state is dropped once a stage runs to completion."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "test.db"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_clear_resume_cursor_removes_last_way_id_only(self) -> None:
        conn = store.connect(self.db)
        try:
            with store.WayWriter(conn) as writer:
                writer.append(_way(1, "Via Roma"))
                writer.append(_way(2, "Via Milano"))
            store.set_counts(conn, parsed_count=2, unnamed_count=1)

            # After writing, the resume cursor points at the last committed way.
            writer = store.WayWriter(conn)
            self.assertEqual(writer.resume_cursor(), 2)

            store.clear_resume_cursor(conn)

            # The cursor is gone, so a re-run starts clean...
            self.assertIsNone(store.WayWriter(conn).resume_cursor())
            # ...but the summary counts the join still needs are preserved.
            header = store.read_header(self.db)
            self.assertEqual(header.parsed_count, 2)
        finally:
            conn.close()

    def test_done_set_clear_empties_the_table(self) -> None:
        conn = store.connect(self.db)
        try:
            done = store.DoneSet(conn)
            with conn:
                done.mark("Via Roma")
                done.mark("Via Milano")
            self.assertEqual(done.load(), {"Via Roma", "Via Milano"})

            done.clear()

            self.assertEqual(done.load(), set())
        finally:
            conn.close()

    def test_clear_ways_empties_the_table(self) -> None:
        conn = store.connect(self.db)
        try:
            with store.WayWriter(conn) as writer:
                writer.append(_way(1, "Via Roma"))
                writer.append(_way(2, "Via Milano"))
            self.assertEqual(len(list(store.read_groups(self.db))), 2)

            store.clear_ways(conn)

            self.assertEqual(list(store.read_groups(self.db)), [])
        finally:
            conn.close()

    def test_reappending_same_way_ids_after_clear_does_not_collide(self) -> None:
        # Mirrors a re-run of extract against a populated database: without the
        # clear, re-inserting way_id 1 would raise a primary-key conflict.
        conn = store.connect(self.db)
        try:
            with store.WayWriter(conn) as writer:
                writer.append(_way(1, "Via Roma"))

            store.clear_ways(conn)

            with store.WayWriter(conn) as writer:
                writer.append(_way(1, "Via Roma"))
            groups = list(store.read_groups(self.db))
            self.assertEqual({w.way_id for g in groups for w in g.ways}, {1})
        finally:
            conn.close()


class StreetWriterClearTest(unittest.TestCase):
    """A fresh join empties the streets table before writing."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "test.db"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_clear_empties_streets_table(self) -> None:
        conn = store.connect(self.db)
        try:
            writer = StreetWriter(conn)
            writer.write_group_streets("viaroma", [Street(name="Via Roma")])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM streets").fetchone()[0], 1
            )

            writer.clear()

            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM streets").fetchone()[0], 0
            )
        finally:
            conn.close()
