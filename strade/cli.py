"""Command-line interface for the Italian Street Extractor.

Exposes the ``extract``, ``join``, ``threshold``, ``prefixes``, ``map``,
``alias``, and ``cities`` subcommands, each with its own options. This module
owns argument parsing (:func:`parse_args`), the derivation of default output
paths (:func:`default_database_path`, :func:`default_map_output_path`), and the
per-command orchestration (``run_extract`` / ``run_join`` / ``run_threshold`` /
``run_prefixes`` / ``run_map`` / ``run_alias`` / ``run_cities``).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

from strade import store
from strade.aliases import AliasError, check_consistency, parse_alias_file, unknown_keys
from strade.cities import assign_matches, build_city_index, write_csv
from strade.collector import collect
from strade.geometry import Projector
from strade.joiner import (
    CandidatePair,
    find_candidate_pairs,
    join_group,
    print_candidate_pairs,
)
from strade.mapper import build_grid, render_map
from strade.parser import parse_admin_areas, parse_highways
from strade.patterns import PatternError, parse_pattern_file
from strade.prefixes import format_counts, scan_first_words
from strade.reporter import Reporter
from strade.store import StreetWriter
from strade.validation import InputError, validate_input
from strade.writer import print_top_street_groups

# Database extension appended to the derived default path.
DB_SUFFIX = ".db"

# OSM serialization suffixes, longest (compound) first so that e.g. ``.osm.pbf``
# is matched before the bare ``.osm`` / ``.pbf`` fallbacks.
OSM_SUFFIXES: tuple[str, ...] = (
    ".osm.pbf",
    ".osm.xml",
    ".osm.bz2",
    ".osm.gz",
    ".osm",
    ".pbf",
)

# Default Heuristic_Join proximity threshold in meters.
DEFAULT_THRESHOLD_M = 100.0

# Number of highest-count street groups printed to stdout after a join.
TOP_STREET_GROUPS = 10

# Default square grid cell edge length for the `map` command, in kilometers.
DEFAULT_CELL_KM = 10.0

# Default minimum street count for a `map` grid cell to be plotted; cells below
# this are dropped so a handful of streets cannot produce an extreme ratio.
# 42 = floor((0.98/0.15)^2): the samples a cell needs for its share to be good
# to +-0.15 at 95% confidence (worst-case p=0.5 binomial margin of error).
DEFAULT_MIN_STREETS = 42

# Image suffix appended to the derived default `map` output path.
MAP_SUFFIX = ".png"


@dataclass(frozen=True)
class ExtractOptions:
    """Resolved options for the ``extract`` command.

    ``database_path`` is always a concrete path: when the user omits the
    database argument it is derived from ``input_path`` via
    :func:`default_database_path`, so ``run_extract`` never has to derive it.
    """

    input_path: Path  # OSM dump
    database_path: Path  # SQLite checkpoint to write
    verbosity: int  # reporter verbosity level (incremented per -v)


@dataclass(frozen=True)
class JoinOptions:
    """Resolved options for the ``join`` command.

    The join writes distinct streets and their per-``norm_name`` aggregation back
    into the input database; the top street groups are printed to standard
    output. ``proximity_threshold_m`` defaults to 100 meters.
    """

    database_path: Path  # SQLite checkpoint produced by ``extract``
    proximity_threshold_m: float  # default 100.0
    verbosity: int  # reporter verbosity level (incremented per -v)


@dataclass(frozen=True)
class ThresholdOptions:
    """Resolved options for the ``threshold`` command.

    Like ``join`` this reads the database produced by ``extract`` and streams
    name groups, but instead of joining it reports the borderline "almost joined"
    way pairs: those whose projected distance is at least ``proximity_threshold_m``
    yet below twice that value. The pairs and their distances are printed to
    standard output so the threshold can be tuned. ``proximity_threshold_m``
    defaults to 100 meters, matching ``join``.
    """

    database_path: Path  # SQLite checkpoint produced by ``extract``
    proximity_threshold_m: float  # default 100.0
    verbosity: int  # reporter verbosity level (incremented per -v)


@dataclass(frozen=True)
class PrefixesOptions:
    """Resolved options for the ``prefixes`` command.

    Scans a dump for the first word of every named way, excluding the words the
    normalizer already strips, so the results can be folded back into
    ``normalize._PREFIXES`` by hand.
    """

    input_path: Path  # OSM dump to scan
    verbosity: int  # reporter verbosity level (incremented per -v)


@dataclass(frozen=True)
class MapOptions:
    """Resolved options for the ``map`` command.

    Reads the ``streets``/``ways`` tables from a database that ``join`` has
    already populated and renders a density-normalized prominence map for
    ``target``: each square grid cell is coloured by the share of its streets
    whose ``norm_name`` equals ``target``, so the result is not merely a map of
    street density. ``target`` must be a normalization key (as produced by
    ``join``), not a display name, so bilingual/prefix variants count together.
    ``output_path`` defaults to the database path with a ``.png`` extension.
    ``cell_km`` is the grid cell edge in kilometers; ``min_streets`` drops cells
    with fewer than that many streets so sparse cells cannot dominate the scale.
    """

    database_path: Path  # SQLite checkpoint produced by `extract` + `join`
    target: str  # norm_name whose per-cell share is mapped
    output_path: Path  # image file to write
    cell_km: float  # grid cell edge length in kilometers
    min_streets: int  # minimum streets for a cell to be plotted
    verbosity: int  # reporter verbosity level (incremented per -v)


@dataclass(frozen=True)
class AliasOptions:
    """Resolved options for the ``alias`` command.

    Reads a hand-curated alias file and folds its variant ``norm_name`` keys into
    their canonical keys in the ``streets`` table of a database that ``join`` has
    already populated, then rebuilds ``street_groups`` and prints the refreshed
    top groups. ``alias_path`` defaults to ``aliases.txt`` in the database's
    directory (the aliases are region-independent, so the name carries no
    database stem); ``-a/--aliases`` overrides it.
    """

    database_path: Path  # SQLite checkpoint produced by `extract` + `join`
    alias_path: Path  # alias file mapping variant keys to canonical keys
    verbosity: int  # reporter verbosity level (incremented per -v)


@dataclass(frozen=True)
class CitiesOptions:
    """Resolved options for the ``cities`` command.

    Reads ``admin_level=8`` comune boundaries from the OSM dump and the named
    ways from the ``extract`` database, then reports (as CSV on stdout) for each
    comune whether it contains at least one street whose name matches a
    ``LIKE`` pattern from ``pattern_path``. ``database_path`` defaults to the
    dump path with a ``.db`` extension (like ``extract``) when the optional
    ``database`` positional is omitted; the pattern file has no default and must
    be supplied.
    """

    input_path: Path  # OSM dump, read for comune boundaries
    database_path: Path  # SQLite checkpoint produced by `extract`, read for ways
    pattern_path: Path  # file of SQLite LIKE patterns, one per line
    verbosity: int  # reporter verbosity level (incremented per -v)


def default_alias_path(database_path: Path) -> Path:
    """Derive the default ``alias`` file path from the database path.

    Returns ``aliases.txt`` in the database's directory. The name deliberately
    omits the database stem because the alias list is region-independent: the
    same spelling fixes (``marx`` -> ``karlmarx`` …) apply to every dump, so one
    ``aliases.txt`` beside the databases is reused across regions.
    """
    return database_path.with_name("aliases.txt")


def default_map_output_path(database_path: Path, target: str) -> Path:
    """Derive the default ``map`` image path from the database path and target.

    Concatenates the database file's stem with the target ``norm_name`` and a
    ``.png`` extension, keeping the file in the database's directory, so mapping
    different targets from one database yields distinct files
    (``aosta.db`` + ``roma`` -> ``aosta-roma.png``).
    """
    return database_path.with_name(f"{database_path.stem}-{target}{MAP_SUFFIX}")


def default_database_path(input_path: Path) -> Path:
    """Derive the default database path from the OSM dump path.

    Strips the (possibly compound) OSM suffix from the file name and appends
    ``.db``, keeping the file in the same directory as the input. Examples::

        italy.osm.pbf   -> italy.db
        italy.osm       -> italy.db
        region.osm.xml  -> region.db
        piedmont.osm.gz -> piedmont.db

    Falls back to appending ``.db`` to the stem when no known OSM suffix matches.
    """
    name = input_path.name
    for suffix in OSM_SUFFIXES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            break
    else:
        base = input_path.stem
    return input_path.with_name(base + DB_SUFFIX)


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with the ``extract``/``join`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="strade",
        description="Extract distinct named Italian streets from an OpenStreetMap dump.",
    )

    # Shared options every subcommand accepts. -v/--verbose is an incremental
    # flag: each occurrence raises the reporter's verbosity (-v enables info,
    # -vv enables debug).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v",
        "--verbose",
        dest="verbosity",
        action="count",
        default=0,
        help="Increase output verbosity (-v for info, -vv for debug).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # extract: parse + collect, writing the intermediate SQLite database.
    extract = subparsers.add_parser(
        "extract",
        parents=[common],
        help="Parse an OSM dump and write the intermediate SQLite checkpoint.",
    )
    extract.add_argument(
        "input",
        type=Path,
        help="Path to the OSM dump to read (.osm.pbf, .osm, ...).",
    )
    # The database may be supplied as an optional positional or via -d/--database;
    # when omitted it defaults to the dump path with its OSM suffix replaced by .db.
    extract.add_argument(
        "database",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Path to the SQLite checkpoint to write. "
            "Defaults to the dump path with a .db extension."
        ),
    )
    extract.add_argument(
        "-d",
        "--database",
        dest="database_opt",
        type=Path,
        default=None,
        help="Path to the SQLite checkpoint to write (overrides the default).",
    )

    # join: read the database, join, and emit the street list.
    join = subparsers.add_parser(
        "join",
        parents=[common],
        help=(
            "Join collected ways into distinct streets, aggregate them by "
            "norm_name into the street_groups table, and print the top groups."
        ),
    )
    join.add_argument(
        "database",
        type=Path,
        help="Path to the SQLite checkpoint produced by `extract`.",
    )
    join.add_argument(
        "-t",
        "--threshold",
        dest="threshold",
        metavar="METERS",
        type=float,
        default=DEFAULT_THRESHOLD_M,
        help="Proximity threshold in meters for the heuristic join (default: 100.0).",
    )

    # threshold: read the database and report borderline candidate join pairs.
    threshold = subparsers.add_parser(
        "threshold",
        parents=[common],
        help=(
            "Report candidate join pairs whose distance is between the threshold "
            "and twice the threshold, with their distance, to help tune -t."
        ),
    )
    threshold.add_argument(
        "database",
        type=Path,
        help="Path to the SQLite checkpoint produced by `extract`.",
    )
    threshold.add_argument(
        "-t",
        "--threshold",
        dest="threshold",
        metavar="METERS",
        type=float,
        default=DEFAULT_THRESHOLD_M,
        help=(
            "Current proximity threshold in meters; candidate pairs fall in "
            "[threshold, 2*threshold) (default: 100.0)."
        ),
    )

    # prefixes: scan a dump for candidate street-type prefixes.
    prefixes = subparsers.add_parser(
        "prefixes",
        parents=[common],
        help=(
            "Scan an OSM dump for the first word of every street name, excluding "
            "words the normalizer already strips, to extend normalize._PREFIXES."
        ),
    )
    prefixes.add_argument(
        "input",
        type=Path,
        help="Path to the OSM dump to scan (.osm.pbf, .osm, ...).",
    )

    # map: plot a density-normalized prominence map for a target norm_name.
    map_cmd = subparsers.add_parser(
        "map",
        parents=[common],
        help=(
            "Plot where a street name is proportionally most common: each grid "
            "cell is coloured by the share of its streets carrying the target "
            "norm_name, so the map is not just a map of street density."
        ),
    )
    map_cmd.add_argument(
        "database",
        type=Path,
        help="Path to the SQLite checkpoint produced by `extract` and `join`.",
    )
    map_cmd.add_argument(
        "target",
        help="The norm_name (grouping key, not display name) to map the share of.",
    )
    map_cmd.add_argument(
        "-o",
        "--output",
        dest="output",
        type=Path,
        default=None,
        help=(
            "Path to the image to write. Defaults to the database path with a "
            ".png extension."
        ),
    )
    map_cmd.add_argument(
        "--cell",
        dest="cell",
        metavar="KM",
        type=float,
        default=DEFAULT_CELL_KM,
        help="Square grid cell edge length in kilometers (default: 10).",
    )
    map_cmd.add_argument(
        "--min-streets",
        dest="min_streets",
        metavar="N",
        type=int,
        default=DEFAULT_MIN_STREETS,
        help=(
            "Drop grid cells with fewer than N streets so sparse cells do not "
            "produce extreme ratios (default: 42)."
        ),
    )

    # alias: fold variant norm_name keys into canonical keys in a joined database.
    alias = subparsers.add_parser(
        "alias",
        parents=[common],
        help=(
            "Merge hand-listed street-name variants: relabel their norm_name in "
            "the streets table to a canonical key, then rebuild street_groups."
        ),
    )
    alias.add_argument(
        "database",
        type=Path,
        help="Path to the SQLite checkpoint produced by `extract` and `join`.",
    )
    alias.add_argument(
        "-a",
        "--aliases",
        dest="aliases",
        type=Path,
        default=None,
        help=(
            "Path to the alias file (old=new per line). Defaults to aliases.txt "
            "in the database's directory."
        ),
    )

    # cities: report, per admin_level=8 comune, whether a matching street exists.
    cities = subparsers.add_parser(
        "cities",
        parents=[common],
        help=(
            "For every admin_level=8 comune boundary in the dump, report as CSV "
            "whether it contains at least one street whose name matches a LIKE "
            "pattern from the pattern file."
        ),
    )
    cities.add_argument(
        "input",
        type=Path,
        help="Path to the OSM dump to read comune boundaries from (.osm.pbf, ...).",
    )
    cities.add_argument(
        "patterns",
        type=Path,
        help="Path to a file of SQLite LIKE patterns, one per line (# comments allowed).",
    )
    # Optional and last so the two-argument form (dump + patterns) resolves the
    # database from the dump path without ambiguity.
    cities.add_argument(
        "database",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Path to the SQLite checkpoint produced by `extract` (read for the "
            "ways). Defaults to the dump path with a .db extension."
        ),
    )

    return parser


def parse_args(
    argv: list[str],
) -> (
    ExtractOptions
    | JoinOptions
    | ThresholdOptions
    | PrefixesOptions
    | MapOptions
    | AliasOptions
    | CitiesOptions
):
    """Parse command-line arguments into resolved options.

    Dispatches on the subcommand and returns :class:`ExtractOptions` for
    ``extract``, :class:`JoinOptions` for ``join``, :class:`PrefixesOptions`
    for ``prefixes``, :class:`MapOptions` for ``map``, or :class:`AliasOptions`
    for ``alias``. For ``extract``, the
    database path defaults to :func:`default_database_path` when omitted; an
    explicit ``-d/--database`` value takes precedence over the optional
    positional, which in turn overrides the derived default. For ``join``, the
    proximity threshold defaults to 25 meters. For ``alias``, the alias file
    defaults to :func:`default_alias_path` when ``-a/--aliases`` is omitted.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "extract":
        # -d/--database wins over the optional positional; either overrides the
        # derived default.
        database = args.database_opt or args.database
        if database is None:
            database = default_database_path(args.input)
        return ExtractOptions(
            input_path=args.input,
            database_path=database,
            verbosity=args.verbosity,
        )

    if args.command == "prefixes":
        return PrefixesOptions(input_path=args.input, verbosity=args.verbosity)

    if args.command == "map":
        output = args.output or default_map_output_path(args.database, args.target)
        return MapOptions(
            database_path=args.database,
            target=args.target,
            output_path=output,
            cell_km=args.cell,
            min_streets=args.min_streets,
            verbosity=args.verbosity,
        )

    if args.command == "alias":
        alias_path = args.aliases or default_alias_path(args.database)
        return AliasOptions(
            database_path=args.database,
            alias_path=alias_path,
            verbosity=args.verbosity,
        )

    if args.command == "cities":
        # The optional database positional overrides the default derived from the
        # dump path (as in `extract`).
        database = args.database
        if database is None:
            database = default_database_path(args.input)
        return CitiesOptions(
            input_path=args.input,
            database_path=database,
            pattern_path=args.patterns,
            verbosity=args.verbosity,
        )

    if args.command == "threshold":
        return ThresholdOptions(
            database_path=args.database,
            proximity_threshold_m=args.threshold,
            verbosity=args.verbosity,
        )

    # args.command == "join"
    return JoinOptions(
        database_path=args.database,
        proximity_threshold_m=args.threshold,
        verbosity=args.verbosity,
    )


def run_extract(options: ExtractOptions, reporter: Reporter) -> int:
    """Run the ``extract`` command; returns the process exit code.

    Validates the input dump, opens the intermediate database, and streams the
    parser into the Collector/``WayWriter``: named highway ways are inserted into
    the ``ways`` table while unnamed ways are excluded and counted.
    Parsing resumes past the last committed way id, so re-running after an
    interruption continues where it left off.

    A fatal input error (missing file, unsupported format) is reported via the
    Reporter and mapped to a non-zero exit code. On success
    the parsed and group counts are reported in the extract summary and the run
    terminates with ``0`` when no non-fatal warnings were recorded, else a
    non-zero code.
    """
    try:
        fmt = validate_input(options.input_path)
    except InputError as exc:
        # Fatal input error: report it and terminate non-zero.
        reporter.warn(str(exc))
        return reporter.exit_code or 1

    conn = store.connect(options.database_path)
    try:
        with store.WayWriter(conn) as writer:
            cursor = writer.resume_cursor()
            # No cursor means this is a fresh (or previously completed) run, not
            # a resume: empty the ways table so re-running against a populated
            # database does not collide on the way_id primary key.
            if cursor is None:
                store.clear_ways(conn)
            ways = parse_highways(
                options.input_path,
                fmt,
                reporter,
                resume_after_way_id=cursor,
            )
            counts = collect(ways, writer)
        # Persist the extract-stage tallies once the writer has flushed its rows.
        store.set_counts(conn, counts.parsed_count, counts.unnamed_count)
        # The parser was driven to exhaustion, so every way in the dump is now
        # committed. Drop the resume cursor so a re-run starts clean instead of
        # treating this finished database as a partial checkpoint.
        store.clear_resume_cursor(conn)
    finally:
        conn.close()

    # Group count comes from the persisted ways table (COUNT(DISTINCT name)).
    header = store.read_header(options.database_path)
    reporter.set_counts(parsed=counts.parsed_count, groups=header.group_count)
    reporter.summary()

    # 0 when no non-fatal warnings were recorded, else non-zero.
    return reporter.exit_code


def run_join(options: JoinOptions, reporter: Reporter) -> int:
    """Run the ``join`` command; returns the process exit code.

     Loads the done-set once, then streams name groups from the intermediate
     database in ascending name order, skipping any group whose name is already
     recorded in ``done`` so an interrupted run resumes where it left off
    . Each remaining group is joined into distinct
     streets via :func:`~strade.joiner.join_group` and persisted with
     :class:`~strade.store.StreetWriter`, which inserts the group's streets and
     its done-marker in one transaction so a crash never leaves streets without
     their marker.

     A dedicated write connection is used for the writer/done-set while
     :func:`~strade.store.read_groups` drives its own read connection by
     path; the store's WAL mode lets the per-group commits proceed without
     blocking the read cursor, and each group is committed in its own transaction.

     After all groups are processed the distinct streets are aggregated into the
     ``street_groups`` table (one row per ``norm_name`` with its street count)
     and the top :data:`TOP_STREET_GROUPS` rows, ordered by descending count,
     are printed to standard output. The combined parsed/group/street counts are
     reported and the run terminates with ``0`` when no non-fatal warnings were
     recorded, else a non-zero code.
    """
    projector = Projector()
    conn = store.connect(options.database_path)
    street_count = 0
    # The header's group_count equals the number of groups read_groups yields, so
    # it is an exact total for the progress bar. Read once up front and reuse for
    # the summary below to avoid a second scan.
    header = store.read_header(options.database_path)
    try:
        writer = StreetWriter(conn)
        done = store.DoneSet(conn)
        # Load the done-set once up front so resume skips are a cheap membership
        # test rather than a per-group query.
        done_names = done.load()
        # An empty done-set means this is a fresh (or previously completed) run,
        # not a resume: empty the streets table so re-running against a populated
        # database does not accumulate duplicate street rows.
        if not done_names:
            writer.clear()

        # Drawn on stderr to keep stdout reserved for the exported street list.
        groups = tqdm(
            store.read_groups(options.database_path),
            desc="joining groups",
            total=header.group_count,
            unit="group",
        )
        for group in groups:
            if group.key in done_names:
                continue
            streets = join_group(
                group,
                options.proximity_threshold_m,
                projector,
                reporter,
            )
            # Streets and the done-marker land in a single transaction, so this
            # group is resumable at normalization-key granularity.
            writer.write_group_streets(group.key, streets)
            street_count += len(streets)
        # Every group was processed, so the join is complete. Drop the
        # done-markers so a re-run starts clean instead of skipping every group
        # as already committed.
        done.clear()
        # Aggregate the distinct streets into the street_groups table (one row
        # per norm_name with its street count), then print the highest-count
        # rows to stdout. Reuse the write connection before it is closed.
        store.build_street_groups(conn)
        top_groups = store.read_top_street_groups(conn, TOP_STREET_GROUPS)
    finally:
        conn.close()

    print_top_street_groups(top_groups, sys.stdout)

    # Parsed and group counts are carried across stages in the header; combine
    # them with the streets produced here for the join summary. The header was
    # read once up front to size the progress bar, so it is reused here.
    reporter.set_counts(
        parsed=header.parsed_count,
        groups=header.group_count,
        streets=street_count,
    )
    reporter.summary()

    # 0 when no non-fatal warnings were recorded, else non-zero.
    return reporter.exit_code


def run_threshold(options: ThresholdOptions, reporter: Reporter) -> int:
    """Run the ``threshold`` command; returns the process exit code.

    Streams the name groups from the intermediate database in the same order as
    ``join`` but, instead of joining, collects the borderline "almost joined"
    way pairs via :func:`~strade.joiner.find_candidate_pairs`: pairs whose
    projected distance is at least ``proximity_threshold_m`` yet below twice that
    value. These are the pairs a slightly larger threshold would merge, so
    printing them with their exact distance helps tune ``-t``.

    Unlike ``join`` this is a read-only diagnostic: nothing is written back to
    the database and there is no resume/done bookkeeping. Candidate pairs are
    collected while the scan runs, then written to standard output as
    tab-separated rows in one block *after* the progress bar has finished, so the
    stderr progress bar and the stdout pair list never interleave. Progress and
    warnings go to stderr via the reporter. The run terminates with ``0`` when no
    non-fatal warnings were recorded, else a non-zero code.
    """
    projector = Projector()
    header = store.read_header(options.database_path)

    # Drawn on stderr to keep stdout reserved for the candidate-pair list.
    groups = tqdm(
        store.read_groups(options.database_path),
        desc="scanning groups",
        total=header.group_count,
        unit="group",
    )

    # Accumulate every candidate pair while the progress bar runs on stderr;
    # nothing is written to stdout inside the loop, so the redrawing bar can't
    # interleave with the pair rows. The whole list is printed once below.
    candidates: list[CandidatePair] = []
    for group in groups:
        candidates.extend(
            find_candidate_pairs(
                group,
                options.proximity_threshold_m,
                projector,
                reporter,
            )
        )

    # Bar is done: sort the full set by ascending distance and print it as one
    # clean block to stdout, with a single header line. Skip stdout entirely
    # when nothing matched so the pair list is never just a bare header.
    if candidates:
        candidates.sort(key=lambda p: p.distance_m)
        print_candidate_pairs(candidates, sys.stdout, header=True)

    reporter.progress(f"threshold: {len(candidates)} candidate pair(s) found")
    reporter.set_counts(parsed=header.parsed_count, groups=header.group_count)
    reporter.summary()

    # 0 when no non-fatal warnings were recorded, else non-zero.
    return reporter.exit_code


def run_prefixes(options: PrefixesOptions, reporter: Reporter) -> int:
    """Run the ``prefixes`` command; returns the process exit code.

    Validates the input dump, then streams it once counting the first word of
    every named way that the normalizer does not already strip. The tally is
    written to standard output as ``count<tab>word`` lines, most frequent first,
    so it can be reviewed and folded into ``normalize._PREFIXES`` by hand. All
    progress and warnings go to stderr via the reporter, keeping stdout clean.

    A fatal input error (missing file, unsupported format) is reported via the
    reporter and mapped to a non-zero exit code. On success the run terminates
    with ``0`` when no non-fatal warnings were recorded, else a non-zero code.
    """
    try:
        fmt = validate_input(options.input_path)
    except InputError as exc:
        reporter.warn(str(exc))
        return reporter.exit_code or 1

    counts = scan_first_words(options.input_path, fmt, reporter)

    rendered = format_counts(counts)
    if rendered:
        print(rendered)

    reporter.progress(f"prefixes: {len(counts)} candidate word(s) found")
    return reporter.exit_code


def run_map(options: MapOptions, reporter: Reporter) -> int:
    """Run the ``map`` command; returns the process exit code.

    Streams the produced streets from the database as representative points via
    :func:`~strade.store.read_street_points`, bins them into a square metric grid
    with :func:`~strade.mapper.build_grid` (colouring each cell by the share of
    its streets whose ``norm_name`` equals ``options.target``), and renders the
    grid to ``options.output_path`` with :func:`~strade.mapper.render_map`.

    The ``target`` is matched against each street's ``norm_name`` exactly, so it
    must be the grouping key produced by ``join``; a target that matches nothing
    still renders a valid map (every cell simply has a zero share) and is flagged
    as a warning so the caller notices a likely wrong key. When no cell clears
    the ``min_streets`` floor the map is still written, with a placeholder note,
    and a warning is recorded. Progress and the output path go to stderr via the
    reporter, keeping stdout unused. The run terminates with ``0`` when no
    non-fatal warnings were recorded, else a non-zero code.
    """
    cell_size_m = options.cell_km * 1000.0
    reporter.progress(
        f"map: binning streets into {options.cell_km:g} km cells "
        f"(min {options.min_streets} streets/cell)"
    )
    # street count sizes the bar; it is an upper bound since streets with no
    # resolved coords are skipped by read_street_points. Drawn on stderr like
    # the join/threshold bars.
    total_streets = store.count_streets(options.database_path)
    # Resolve the dominant human-readable name for the target key so the plot
    # labels read "Via Roma" rather than the lossy "roma"; fall back to the key
    # when nothing matches (an empty/wrong-key map still renders).
    display_name = store.read_display_name(options.database_path, options.target)
    points = tqdm(
        store.read_street_points(options.database_path),
        desc="mapping streets",
        total=total_streets,
        unit="street",
    )
    grid = build_grid(
        points,
        target=options.target,
        cell_size=cell_size_m,
        min_streets=options.min_streets,
        display_name=display_name,
    )

    if grid.total_streets == 0:
        reporter.warn(
            f"no streets found in {options.database_path}; "
            "has `join` been run on this database?"
        )
    elif grid.matching_streets == 0:
        reporter.warn(
            f"no streets matched norm_name '{options.target}'; "
            "check the key (use the norm_name from `join`, not a display name)"
        )
    scored = sum(1 for cell in grid.cells if cell.populated)
    sparse = len(grid.cells) - scored
    if scored == 0 and grid.total_streets:
        reporter.warn(
            f"no grid cell reached the min-streets floor of {options.min_streets}; "
            "lower --min-streets or increase --cell"
        )

    render_map(grid, options.output_path)
    reporter.progress(
        f"map: {grid.matching_streets}/{grid.total_streets} streets matched, "
        f"{scored} cell(s) scored, {sparse} too sparse -> {options.output_path}"
    )

    # 0 when no non-fatal warnings were recorded, else non-zero.
    return reporter.exit_code


def run_alias(options: AliasOptions, reporter: Reporter) -> int:
    """Run the ``alias`` command; returns the process exit code.

    Parses the alias file (``old=new`` per line) into a variant->canonical
    mapping and validates its consistency *before* opening the database, so a
    malformed or inconsistent file (a canonical key that is itself a variant)
    halts the command with a warning and a non-zero exit code without touching
    any data — mirroring how :func:`run_extract` treats a fatal input error.

    With a valid mapping, it opens the database, reads the ``norm_name`` keys
    actually present in the ``streets`` table, and warns (non-fatally) for any
    variant key the file lists that matches no street, since a shared alias file
    may name streets absent from the region being processed. It then relabels the
    matching streets to their canonical key via
    :func:`~strade.store.apply_aliases` and rebuilds the ``street_groups``
    aggregation with :func:`~strade.store.build_street_groups`. It prints one
    ``<count> - <old name>`` line per mapping to standard output followed by the
    total number of relabeled street rows. A progress line reporting how many
    street rows were relabeled goes to stderr. The run terminates with ``0`` when
    no non-fatal warnings were recorded, else a non-zero code.
    """
    try:
        mapping = parse_alias_file(options.alias_path)
        check_consistency(mapping)
    except AliasError as exc:
        # Fatal alias-file error: report it and terminate non-zero before any
        # database work, so nothing is overwritten from an ambiguous file.
        reporter.warn(str(exc))
        return reporter.exit_code or 1
    except OSError as exc:
        reporter.warn(f"could not read alias file {options.alias_path}: {exc}")
        return reporter.exit_code or 1

    conn = store.connect(options.database_path)
    try:
        existing = store.read_street_norm_names(conn)
        missing = unknown_keys(mapping, existing)
        if missing:
            reporter.warn(
                f"{len(missing)} alias variant key(s) matched no street and were "
                f"skipped: {', '.join(sorted(missing))}"
            )
        counts = store.apply_aliases(conn, mapping)
        store.build_street_groups(conn)
    finally:
        conn.close()

    relabeled = sum(counts.values())
    for old, count in counts.items():
        print(f"{count} - {old}", file=sys.stdout)
    print(f"total: {relabeled}", file=sys.stdout)

    reporter.progress(
        f"alias: {relabeled} street(s) relabeled across {len(mapping)} mapping(s)"
    )
    # 0 when no non-fatal warnings were recorded, else non-zero.
    return reporter.exit_code


def run_cities(options: CitiesOptions, reporter: Reporter) -> int:
    """Run the ``cities`` command; returns the process exit code.

    Validates the OSM dump, then reads the LIKE-pattern file (a file with no
    usable pattern is a fatal error). It streams the dump's ``admin_level=8``
    comune boundaries into a :class:`~strade.cities.CityIndex` (a spatial index
    for point-in-polygon lookup), then streams only the ways whose name matches a
    pattern from the ``extract`` database
    (:func:`~strade.store.read_ways_matching`) and flags the comune each matching
    way falls in. Finally it writes one CSV row per comune to standard output —
    the identifying tags plus a ``true``/``false`` ``matched`` column — keeping
    all progress and warnings on stderr via the reporter.

    A fatal input error (missing/unsupported dump, missing database, or an empty
    pattern file) is reported via the reporter and mapped to a non-zero exit
    code. On success the run terminates with ``0`` when no non-fatal warnings
    were recorded, else a non-zero code.
    """
    try:
        fmt = validate_input(options.input_path)
    except InputError as exc:
        # Fatal input error: report it and terminate non-zero.
        reporter.warn(str(exc))
        return reporter.exit_code or 1

    if not options.database_path.is_file():
        reporter.warn(
            f"database not found: {options.database_path} (run `extract` first)"
        )
        return reporter.exit_code or 1

    try:
        patterns = parse_pattern_file(options.pattern_path)
    except (PatternError, OSError) as exc:
        reporter.warn(str(exc))
        return reporter.exit_code or 1

    reporter.progress(
        f"cities: matching {len(patterns)} pattern(s) against streets in comuni"
    )

    # Pass 1: build the comune spatial index from the dump's boundaries.
    index = build_city_index(
        parse_admin_areas(options.input_path, fmt, reporter),
        reporter,
    )

    # Pass 2: stream only the pattern-matched ways from the database and flag the
    # comune each one falls in. The string match runs in SQLite, so the (usually
    # small) matched subset is all that reaches the point-in-polygon test.
    matched_ways = store.read_ways_matching(options.database_path, patterns)
    consumed = assign_matches(index, matched_ways)

    rows = write_csv(index.matches, sys.stdout)

    hit = sum(1 for match in index.matches if match.matched)
    reporter.progress(
        f"cities: {hit} of {rows} comune(i) matched "
        f"({consumed} matching street(s) placed)"
    )
    # 0 when no non-fatal warnings were recorded, else non-zero.
    return reporter.exit_code


def main(argv: list[str] | None = None) -> int:
    """Console entry point: parse args and dispatch to the matching command.

    Parses ``argv`` (defaulting to ``sys.argv[1:]``) into resolved options,
    constructs a :class:`~strade.reporter.Reporter`, and dispatches to
    :func:`run_extract` for :class:`ExtractOptions`, :func:`run_join` for
    :class:`JoinOptions`, :func:`run_threshold` for :class:`ThresholdOptions`,
    :func:`run_prefixes` for :class:`PrefixesOptions`, :func:`run_map` for
    :class:`MapOptions`, or :func:`run_alias` for :class:`AliasOptions`.
    Returns the command's process exit code.
    """
    try:
        options = parse_args(sys.argv[1:] if argv is None else argv)
        reporter = Reporter(verbosity=options.verbosity)
        if isinstance(options, ExtractOptions):
            return run_extract(options, reporter)
        if isinstance(options, PrefixesOptions):
            return run_prefixes(options, reporter)
        if isinstance(options, ThresholdOptions):
            return run_threshold(options, reporter)
        if isinstance(options, MapOptions):
            return run_map(options, reporter)
        if isinstance(options, AliasOptions):
            return run_alias(options, reporter)
        if isinstance(options, CitiesOptions):
            return run_cities(options, reporter)
        return run_join(options, reporter)
    except KeyboardInterrupt:
        # Ctrl-C
        print("interrupted; re-run to resume", file=sys.stderr)
        return 130
