"""End-to-end tests for the `alias` command orchestration (`run_alias`)."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from strade import store
from strade.cli import AliasOptions, default_alias_path, parse_args, run_alias
from strade.models import Street
from strade.reporter import Reporter
from strade.store import StreetWriter


class RunAliasTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)
        self.db = self.base / "test.db"
        self.alias_path = self.base / "aliases.txt"
        conn = store.connect(self.db)
        try:
            writer = StreetWriter(conn)
            writer.write_group_streets(
                "carlomarx",
                [Street(name="Via Carlo Marx", norm_name="carlomarx", way_ids=[1])],
            )
            writer.write_group_streets(
                "marx", [Street(name="Via Marx", norm_name="marx", way_ids=[2])]
            )
            writer.write_group_streets(
                "kmarx",
                [Street(name="Karl-Marx-Strasse", norm_name="kmarx", way_ids=[3])],
            )
            writer.write_group_streets(
                "roma", [Street(name="Via Roma", norm_name="roma", way_ids=[4])]
            )
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _options(self) -> AliasOptions:
        return AliasOptions(
            database_path=self.db, alias_path=self.alias_path, verbosity=0
        )

    def _street_norms(self) -> list[str]:
        conn = store.connect(self.db)
        try:
            rows = conn.execute("SELECT norm_name FROM streets").fetchall()
            return sorted(row[0] for row in rows)
        finally:
            conn.close()

    def _group_keys(self) -> set[str]:
        conn = store.connect(self.db)
        try:
            return {r[0] for r in conn.execute("SELECT norm_name FROM street_groups")}
        finally:
            conn.close()

    def test_folds_variants_into_one_group(self) -> None:
        self.alias_path.write_text(
            "carlomarx=karlmarx\nmarx=karlmarx\nkmarx=karlmarx\n", encoding="utf-8"
        )
        reporter = Reporter(stream=io.StringIO())
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_alias(self._options(), reporter)

        self.assertEqual(code, 0)
        # All three Marx variants now carry the canonical key; Roma untouched.
        self.assertEqual(
            self._street_norms(), ["karlmarx", "karlmarx", "karlmarx", "roma"]
        )
        self.assertEqual(self._group_keys(), {"karlmarx", "roma"})
        # The refreshed top-groups summary is printed to stdout.
        self.assertIn("karlmarx", out.getvalue())

    def test_unknown_variant_warns_but_succeeds(self) -> None:
        self.alias_path.write_text("marx=karlmarx\nghost=karlmarx\n", encoding="utf-8")
        stderr = io.StringIO()
        reporter = Reporter(stream=stderr)
        with redirect_stdout(io.StringIO()):
            code = run_alias(self._options(), reporter)

        # A skipped unknown key is a non-fatal warning: non-zero exit, data still
        # relabeled for the keys that did match.
        self.assertEqual(code, 1)
        self.assertIn("ghost", stderr.getvalue())
        self.assertIn("karlmarx", self._street_norms())

    def test_inconsistent_file_halts_without_touching_data(self) -> None:
        # a=b and b=c: b is both canonical and variant -> reject before any write.
        self.alias_path.write_text("marx=karlmarx\nkarlmarx=roma\n", encoding="utf-8")
        before = self._street_norms()
        stderr = io.StringIO()
        reporter = Reporter(stream=stderr)
        with redirect_stdout(io.StringIO()):
            code = run_alias(self._options(), reporter)

        self.assertEqual(code, 1)
        # Streets are exactly as before: nothing was relabeled.
        self.assertEqual(self._street_norms(), before)
        self.assertIn("inconsistent", stderr.getvalue())

    def test_missing_file_halts_with_error(self) -> None:
        # No alias file written.
        before = self._street_norms()
        stderr = io.StringIO()
        reporter = Reporter(stream=stderr)
        with redirect_stdout(io.StringIO()):
            code = run_alias(self._options(), reporter)

        self.assertEqual(code, 1)
        self.assertEqual(self._street_norms(), before)


class DefaultAliasPathTest(unittest.TestCase):
    def test_defaults_to_aliases_txt_beside_database(self) -> None:
        # The stem is dropped: aliases are region-independent.
        self.assertEqual(
            default_alias_path(Path("data/aosta.db")),
            Path("data/aliases.txt"),
        )

    def test_no_directory_keeps_it_local(self) -> None:
        self.assertEqual(default_alias_path(Path("aosta.db")), Path("aliases.txt"))


class ParseAliasArgsTest(unittest.TestCase):
    def test_omitting_flag_derives_default_beside_db(self) -> None:
        options = parse_args(["alias", "data/aosta.db"])
        self.assertIsInstance(options, AliasOptions)
        assert isinstance(options, AliasOptions)  # narrow for type checkers
        self.assertEqual(options.database_path, Path("data/aosta.db"))
        self.assertEqual(options.alias_path, Path("data/aliases.txt"))

    def test_flag_overrides_default(self) -> None:
        options = parse_args(["alias", "aosta.db", "-a", "custom.txt"])
        assert isinstance(options, AliasOptions)
        self.assertEqual(options.alias_path, Path("custom.txt"))

    def test_long_flag_overrides_default(self) -> None:
        options = parse_args(["alias", "aosta.db", "--aliases", "other/marx.txt"])
        assert isinstance(options, AliasOptions)
        self.assertEqual(options.alias_path, Path("other/marx.txt"))

    def test_verbosity_counts(self) -> None:
        options = parse_args(["alias", "aosta.db", "-vv"])
        assert isinstance(options, AliasOptions)
        self.assertEqual(options.verbosity, 2)


if __name__ == "__main__":
    unittest.main()
