"""Tests for parsing and validating the street-alias file."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from strade import store
from strade.aliases import (
    AliasError,
    check_consistency,
    parse_alias_file,
    unknown_keys,
)
from strade.models import Street
from strade.store import StreetWriter


class ParseAliasFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "aliases.txt"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_well_formed_file_parses_to_mapping(self) -> None:
        self._write(
            "carlomarx=karlmarx\ncarlmarx=karlmarx\nmarx=karlmarx\nkmarx=karlmarx\n"
        )
        self.assertEqual(
            parse_alias_file(self.path),
            {
                "carlomarx": "karlmarx",
                "carlmarx": "karlmarx",
                "marx": "karlmarx",
                "kmarx": "karlmarx",
            },
        )

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        self._write(
            "# Karl Marx, however OSM spelled him\n"
            "\n"
            "marx=karlmarx\n"
            "   # indented comment\n"
            "\n"
            "kmarx=karlmarx\n"
        )
        self.assertEqual(
            parse_alias_file(self.path),
            {"marx": "karlmarx", "kmarx": "karlmarx"},
        )

    def test_whitespace_around_separator_is_trimmed(self) -> None:
        self._write("  marx   =   karlmarx  \n")
        self.assertEqual(parse_alias_file(self.path), {"marx": "karlmarx"})

    def test_self_map_is_dropped(self) -> None:
        self._write("karlmarx=karlmarx\nmarx=karlmarx\n")
        self.assertEqual(parse_alias_file(self.path), {"marx": "karlmarx"})

    def test_line_without_separator_raises(self) -> None:
        self._write("marx karlmarx\n")
        with self.assertRaises(AliasError):
            parse_alias_file(self.path)

    def test_empty_side_raises(self) -> None:
        self._write("marx=\n")
        with self.assertRaises(AliasError):
            parse_alias_file(self.path)
        self._write("=karlmarx\n")
        with self.assertRaises(AliasError):
            parse_alias_file(self.path)

    def test_duplicate_variant_key_raises(self) -> None:
        self._write("marx=karlmarx\nmarx=carlomarx\n")
        with self.assertRaises(AliasError):
            parse_alias_file(self.path)

    def test_only_first_separator_splits(self) -> None:
        # A stray '=' on the canonical side stays part of the value.
        self._write("a=b=c\n")
        self.assertEqual(parse_alias_file(self.path), {"a": "b=c"})


class CheckConsistencyTest(unittest.TestCase):
    def test_consistent_mapping_passes(self) -> None:
        mapping = {"carlomarx": "karlmarx", "marx": "karlmarx", "kmarx": "karlmarx"}
        # Should not raise.
        check_consistency(mapping)

    def test_chained_mapping_raises(self) -> None:
        # a=b and b=c: b is both a canonical key and a variant key.
        mapping = {"a": "b", "b": "c"}
        with self.assertRaises(AliasError):
            check_consistency(mapping)

    def test_error_names_the_offending_key(self) -> None:
        mapping = {"a": "b", "b": "c"}
        with self.assertRaises(AliasError) as ctx:
            check_consistency(mapping)
        self.assertIn("b", str(ctx.exception))


class UnknownKeysTest(unittest.TestCase):
    def test_returns_variants_absent_from_data(self) -> None:
        mapping = {"marx": "karlmarx", "kmarx": "karlmarx", "ghost": "karlmarx"}
        existing = {"marx", "kmarx", "roma"}
        self.assertEqual(unknown_keys(mapping, existing), {"ghost"})

    def test_returns_empty_when_all_present(self) -> None:
        mapping = {"marx": "karlmarx"}
        existing = {"marx", "roma"}
        self.assertEqual(unknown_keys(mapping, existing), set())


class ApplyAliasesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "test.db"
        self.conn = store.connect(self.db)
        # Seed one street per Marx variant plus an unrelated street. way_ids
        # differ so the rows stay distinct after relabeling.
        writer = StreetWriter(self.conn)
        writer.write_group_streets(
            "carlomarx",
            [Street(name="Via Carlo Marx", norm_name="carlomarx", way_ids=[1])],
        )
        writer.write_group_streets(
            "marx", [Street(name="Via Marx", norm_name="marx", way_ids=[2])]
        )
        writer.write_group_streets(
            "kmarx", [Street(name="Karl-Marx-Strasse", norm_name="kmarx", way_ids=[3])]
        )
        writer.write_group_streets(
            "roma", [Street(name="Via Roma", norm_name="roma", way_ids=[4])]
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._dir.cleanup()

    def _norm_names(self) -> dict[int, str]:
        rows = self.conn.execute("SELECT way_ids, norm_name FROM streets").fetchall()
        # way_ids is JSON like "[1]"; strip to the single id for a readable map.
        return {int(way_ids.strip("[]")): norm for way_ids, norm in rows}

    def test_relabels_matching_rows_to_canonical_key(self) -> None:
        mapping = {"carlomarx": "karlmarx", "marx": "karlmarx", "kmarx": "karlmarx"}
        counts = store.apply_aliases(self.conn, mapping)

        self.assertEqual(counts, {"carlomarx": 1, "marx": 1, "kmarx": 1})
        self.assertEqual(sum(counts.values()), 3)
        norms = self._norm_names()
        self.assertEqual(norms[1], "karlmarx")
        self.assertEqual(norms[2], "karlmarx")
        self.assertEqual(norms[3], "karlmarx")

    def test_unrelated_rows_are_untouched(self) -> None:
        store.apply_aliases(self.conn, {"marx": "karlmarx"})
        self.assertEqual(self._norm_names()[4], "roma")

    def test_unknown_variant_relabels_nothing(self) -> None:
        counts = store.apply_aliases(self.conn, {"ghost": "karlmarx"})
        self.assertEqual(counts, {"ghost": 0})
        # No row now carries the canonical key, since no variant matched.
        self.assertNotIn("karlmarx", set(self._norm_names().values()))

    def test_read_street_norm_names_returns_distinct_keys(self) -> None:
        self.assertEqual(
            store.read_street_norm_names(self.conn),
            {"carlomarx", "marx", "kmarx", "roma"},
        )

    def test_build_street_groups_collapses_relabeled_rows(self) -> None:
        mapping = {"carlomarx": "karlmarx", "marx": "karlmarx", "kmarx": "karlmarx"}
        store.apply_aliases(self.conn, mapping)
        store.build_street_groups(self.conn)

        row = self.conn.execute(
            "SELECT name, count FROM street_groups WHERE norm_name = ?",
            ("karlmarx",),
        ).fetchone()
        self.assertIsNotNone(row)
        name, count = row
        # Three streets folded into one group; MIN(name) is deterministic.
        self.assertEqual(count, 3)
        self.assertEqual(name, "Karl-Marx-Strasse")
        # The variant keys no longer form their own groups.
        remaining = {
            r[0] for r in self.conn.execute("SELECT norm_name FROM street_groups")
        }
        self.assertEqual(remaining, {"karlmarx", "roma"})


if __name__ == "__main__":
    unittest.main()
