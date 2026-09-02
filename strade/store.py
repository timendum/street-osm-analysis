"""SQLite-backed pipeline store: schema, connection, and (de)serialization.

This module owns the on-disk boundary between the ``extract`` and ``join``
commands. It wraps a single SQLite database (opened via the standard-library
``sqlite3`` module) and holds all pipeline state in four tables:

- ``ways``    — one row per named highway way, in dump order, indexed by name.
- ``meta``    — key/value bookkeeping (resume cursor, parsed/unnamed counts).
- ``streets`` — one row per produced street (name, norm_name, composing way ids).
- ``street_groups`` — one row per norm_name: count of distinct streets sharing it.
- ``done``    — the set of street names whose streets are fully committed.

A way's geometry lives in two BLOB columns — ``node_ids`` and ``coords`` — which
dominate the on-disk size and are stored in a compact, lossless binary encoding
(see the serialization section for the format). The raw ``name`` is stored as
SQLite ``TEXT`` to preserve exact Unicode, including diacritics, so the
human-readable name is never lossy. Each
row also carries a ``norm_name`` — the language- and type-agnostic grouping key
from :func:`strade.normalize.normalize_name` — computed once at write time so the
join-side grouped read can stream rows already ordered by that key.

The database is opened in WAL mode with ``synchronous = NORMAL`` so each
``COMMIT`` is durable while keeping write throughput high; every unit of work is
committed in its own transaction, so a crash rolls back at most the in-flight
transaction.

This module covers schema, connection, (de)serialization, the extract-side
``WayWriter`` with its resume cursor and meta counters, and the join-side
grouped read (``read_header`` / ``read_groups``) with the ``DoneSet`` used to
skip already-committed groups on resume.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from strade.models import HighwayWay, NameGroup, NodeRef, Street
from strade.normalize import normalize_name

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import Self

# --- Schema -----------------------------------------------------------------

# Each statement uses ``IF NOT EXISTS`` so opening an existing database is a
# no-op and opening a fresh one creates the full schema.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    (
        "CREATE TABLE IF NOT EXISTS ways ("
        "way_id INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "norm_name TEXT NOT NULL, "
        "node_ids BLOB NOT NULL, "
        "coords BLOB NOT NULL)"
    ),
    # Ways are grouped on the normalization key (norm_name), so the streaming
    # grouped read is served by an index on (norm_name, name, way_id): equal
    # keys are contiguous, and within a key the rows are ordered so a stable
    # representative display name can be picked.
    "CREATE INDEX IF NOT EXISTS ways_by_norm_name ON ways (norm_name, name, way_id)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    (
        "CREATE TABLE IF NOT EXISTS streets ("
        "street_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, "
        "norm_name TEXT NOT NULL, "
        "way_ids TEXT NOT NULL)"
    ),
    "CREATE INDEX IF NOT EXISTS streets_by_name ON streets (name, street_id)",
    # Serves the norm_name aggregation into street_groups and lets callers join
    # street_groups.norm_name back to the streets that compose each group.
    "CREATE INDEX IF NOT EXISTS streets_by_norm_name ON streets (norm_name)",
    # One row per distinct norm_name: how many distinct streets carry that key.
    # norm_name is the primary key because the aggregation collapses all streets
    # sharing a key into a single row; join back to streets.norm_name for the
    # composing streets and their way ids.
    (
        "CREATE TABLE IF NOT EXISTS street_groups ("
        "norm_name TEXT PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "count INTEGER NOT NULL)"
    ),
    "CREATE TABLE IF NOT EXISTS done (name TEXT PRIMARY KEY)",
)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they do not already exist.

    Idempotent: safe to call on both fresh and existing databases because every
    statement uses ``CREATE ... IF NOT EXISTS``. The ``ways`` geometry columns
    (``node_ids``, ``coords``) hold packed binary BLOBs, a format not compatible
    with databases written by older versions that stored plain JSON text or
    zlib-compressed JSON; such databases must be re-created (re-run ``extract``)
    rather than migrated in place.
    """
    with conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)


def connect(path: Path) -> sqlite3.Connection:
    """Open (or create) the intermediate database at ``path``.

    Applies WAL journaling and ``synchronous = NORMAL`` for durable, fast
    commits, ensures the schema exists, and returns the open connection. The
    caller owns the connection and is responsible for closing it.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_schema(conn)
    return conn


# --- Serialization helpers --------------------------------------------------


# --- Serialization: zlib-JSON codec (retained alternative) ------------------
#
# An alternative to the binary codec the pipeline uses (defined below), kept side
# by side so the two can be compared on speed and on-disk size.
#
# A way's geometry columns (``node_ids`` and ``coords``) are the two largest
# consumers of disk space in a full database, and their JSON text is highly
# redundant: repeated brackets/commas, node ids duplicated across both columns,
# and lon/lat values within one way sharing long leading digits. Each column is
# therefore stored as a zlib-compressed BLOB wrapping a JSON payload, which
# shrinks the on-disk size while staying lossless — the decompressed bytes are
# byte-identical to the plain-JSON form, so ``NodeRef`` round-trips exactly.
# Per-row compression keeps streaming and resume independent across rows.
_COMPRESS_LEVEL = 3


def serialize_node_ids(node_ids: list[int]) -> bytes:
    """Serialize an ordered list of node ids to a zlib-compressed JSON BLOB."""
    return zlib.compress(json.dumps(node_ids).encode("utf-8"), _COMPRESS_LEVEL)


def deserialize_node_ids(blob: bytes) -> list[int]:
    """Deserialize a zlib-compressed JSON BLOB back into an ordered node id list."""
    return [int(node_id) for node_id in json.loads(zlib.decompress(blob))]


def serialize_coords(coords: list[NodeRef]) -> bytes:
    """Serialize resolved coordinates to a zlib-compressed JSON BLOB.

    Each :class:`~strade.models.NodeRef` becomes a ``[node_id, lon, lat]`` triple
    so the way's geometry stays ordered and co-located within its row; the JSON
    array of triples is then zlib-compressed to shrink the stored bytes.
    """
    payload = json.dumps([[ref.node_id, ref.lon, ref.lat] for ref in coords])
    return zlib.compress(payload.encode("utf-8"), _COMPRESS_LEVEL)


def deserialize_coords(blob: bytes) -> list[NodeRef]:
    """Deserialize a zlib-compressed JSON BLOB back into a list of :class:`NodeRef`."""
    return [
        NodeRef(node_id=int(node_id), lon=float(lon), lat=float(lat))
        for node_id, lon, lat in json.loads(zlib.decompress(blob))
    ]


# --- Serialization: binary codec (used by the pipeline) ---------------------
#
# The codec the pipeline actually reads and writes: it packs each column directly
# to bytes with no per-row compression pass, which is markedly faster to
# serialize and deserialize than the zlib-JSON codec above, for a small increase
# in on-disk size.
#
# ``node_ids`` are stored as delta + zigzag + varint bytes: OSM node ids within
# one way tend to be numerically close, so encoding successive differences (and
# zigzag-folding them so small negatives stay small) lets most ids fit in one or
# two varint bytes. This is the same integer packing Protocol Buffers and the
# OSM PBF format use, and it is lossless.
#
# ``coords`` are stored as packed ``<qdd`` records — node id (int64) plus lon/lat
# (float64) per point. The node id is kept per point (not recovered from
# ``node_ids``) because a way's ``coords`` list is not positionally aligned with
# its ``node_ids``: a node whose location is missing is dropped from ``coords``
# but retained in ``node_ids``, so a mid-way gap would misalign a lon/lat-only
# encoding. ``float64`` round-trips the exact WGS84 doubles, so it is lossless.


def serialize_node_ids_binary(node_ids: list[int]) -> bytes:
    """Serialize node ids as delta + zigzag + varint bytes (lossless)."""
    out = bytearray()
    prev = 0
    for node_id in node_ids:
        delta = node_id - prev
        prev = node_id
        # Zigzag: map signed delta to unsigned so small magnitudes stay short.
        zigzag = (delta << 1) ^ (delta >> 63)
        while True:
            byte = zigzag & 0x7F
            zigzag >>= 7
            if zigzag:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                break
    return bytes(out)


def deserialize_node_ids_binary(blob: bytes) -> list[int]:
    """Deserialize delta + zigzag + varint bytes back into ordered node ids."""
    node_ids: list[int] = []
    prev = 0
    zigzag = 0
    shift = 0
    for byte in blob:
        zigzag |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
            continue
        # Reverse zigzag, then undo the delta to recover the absolute id.
        delta = (zigzag >> 1) ^ -(zigzag & 1)
        prev += delta
        node_ids.append(prev)
        zigzag = 0
        shift = 0
    return node_ids


# Per-point coord record: node id as signed int64, lon/lat as float64.
_COORD_RECORD = struct.Struct("<qdd")


def serialize_coords_binary(coords: list[NodeRef]) -> bytes:
    """Serialize resolved coordinates as packed ``<qdd`` (node_id, lon, lat) records.

    Each point is self-contained — its own node id is stored — because a way's
    ``coords`` list is *not* positionally aligned with its ``node_ids``: a node
    with a missing location is dropped from ``coords`` but kept in ``node_ids``,
    so a mid-way gap would misalign a lon/lat-only encoding. Storing the id per
    point keeps this codec correct on its own. ``float64`` is lossless.
    """
    pack = _COORD_RECORD.pack
    return b"".join(pack(ref.node_id, ref.lon, ref.lat) for ref in coords)


def deserialize_coords_binary(blob: bytes) -> list[NodeRef]:
    """Deserialize packed ``<qdd`` records back into a list of :class:`NodeRef`."""
    return [
        NodeRef(node_id=node_id, lon=lon, lat=lat)
        for node_id, lon, lat in _COORD_RECORD.iter_unpack(blob)
    ]


# --- Meta key/value helpers -------------------------------------------------

# Well-known keys in the ``meta`` table. Values are always stored as TEXT and
# parsed back to ``int`` on read.
_META_LAST_WAY_ID = "last_way_id"
_META_PARSED_COUNT = "parsed_count"
_META_UNNAMED_COUNT = "unnamed_count"


def read_meta_int(conn: sqlite3.Connection, key: str) -> int | None:
    """Return the integer stored at ``meta[key]``, or ``None`` if the key is absent."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return int(row[0])


def write_meta_int(conn: sqlite3.Connection, key: str, value: int) -> None:
    """Upsert ``meta[key] = value`` (stored as TEXT).

    Does not open its own transaction so callers can group the write with other
    statements; wrap the call in ``with conn:`` (or a surrounding transaction)
    to commit it.
    """
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --- Extract side: way writer and resume cursor -----------------------------


class WayWriter:
    """Insert highway ways into the ``ways`` table, tracking the last committed way id.

    Each ``append`` buffers one row; rows are flushed to SQLite in transactions
    of at most ``batch_size`` ways. Every flush inserts the buffered rows and
    updates ``meta['last_way_id']`` to the highest way id in the batch within the
    same transaction, so the resume cursor never points past a way whose row was
    not committed. ``close`` flushes any remaining buffered rows, so
    callers must ``close`` (or use the writer as a context manager) to persist a
    trailing partial batch.
    """

    def __init__(self, conn: sqlite3.Connection, batch_size: int = 1000) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._conn = conn
        self._batch_size = batch_size
        # Buffered (way_id, name, norm_name, node_ids_blob, coords_blob) rows
        # awaiting flush; the last two are packed binary BLOBs (see the binary
        # serialization section).
        self._pending: list[tuple[int, str, str, bytes, bytes]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def resume_cursor(self) -> int | None:
        """Return ``meta['last_way_id']`` as ``int``, or ``None`` if absent."""
        return read_meta_int(self._conn, _META_LAST_WAY_ID)

    def append(self, way: HighwayWay) -> None:
        """Buffer ``way`` for insertion into the ``ways`` table.

        The row is flushed once the buffer reaches ``batch_size``. ``way.name``
        must not be ``None`` (the Collector filters unnamed ways before the
        WayWriter and the ``ways.name`` column is ``NOT NULL``); a ``None`` name
        raises ``ValueError``.
        """
        if way.name is None:
            raise ValueError(f"cannot append unnamed way {way.way_id}")
        self._pending.append(
            (
                way.way_id,
                way.name,
                normalize_name(way.name),
                serialize_node_ids_binary(way.node_ids),
                serialize_coords_binary(way.coords),
            )
        )
        if len(self._pending) >= self._batch_size:
            self._flush()

    def close(self) -> None:
        """Flush any buffered ways so a trailing partial batch is persisted."""
        self._flush()

    def _flush(self) -> None:
        """Commit buffered rows and advance ``meta['last_way_id']`` in one transaction.

        The buffer is cleared before the transaction returns so an interrupted
        or retried flush (``close`` runs during exception unwinding) never
        re-submits rows whose transaction already committed. Inserts use
        ``INSERT OR REPLACE`` so a way id already present from a prior run — or a
        commit that landed just before an interrupt — overwrites the existing
        row instead of raising ``UNIQUE constraint failed`` on ``ways.way_id``;
        re-extracting an updated dump refreshes a changed way rather than
        keeping the stale row.
        """
        if not self._pending:
            return
        pending = self._pending
        # Clear the buffer up front: if the transaction below commits and then
        # an interrupt fires before we return, ``close`` must not resubmit these
        # rows. The local ``pending`` keeps them for the executemany.
        self._pending = []
        last_way_id = pending[-1][0]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO ways (way_id, name, norm_name, node_ids, coords) "
                "VALUES (?, ?, ?, ?, ?)",
                pending,
            )
            write_meta_int(self._conn, _META_LAST_WAY_ID, last_way_id)


def set_counts(conn: sqlite3.Connection, parsed_count: int, unnamed_count: int) -> None:
    """Persist the extract-stage ``parsed_count`` and ``unnamed_count`` in ``meta``.

    Written together in one transaction so the join summary and the
    unnamed tally stay consistent across a resume.
    """
    with conn:
        write_meta_int(conn, _META_PARSED_COUNT, parsed_count)
        write_meta_int(conn, _META_UNNAMED_COUNT, unnamed_count)


def clear_ways(conn: sqlite3.Connection) -> None:
    """Empty the ``ways`` table so a fresh ``extract`` starts from scratch.

    Called at the start of a non-resuming ``extract`` (no ``last_way_id``
    cursor) so re-running against a populated database does not collide with the
    already-present rows on the ``ways.way_id`` primary key. A resuming run must
    keep its committed prefix, so the caller only invokes this when there is no
    cursor. Runs in its own transaction.
    """
    with conn:
        conn.execute("DELETE FROM ways")


def clear_resume_cursor(conn: sqlite3.Connection) -> None:
    """Drop the extract resume cursor so the next ``extract`` run starts clean.

    Called once the parser has been driven to exhaustion, meaning every way in
    the dump was read and committed. Removing ``meta['last_way_id']`` means a
    subsequent ``extract`` no longer treats the finished database as a partial
    checkpoint to resume past; it re-parses from the first way instead. The
    parsed/unnamed count keys are left in place because the join stage still
    reads them for its summary.
    """
    with conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (_META_LAST_WAY_ID,))


# --- Join side: header, grouped read, and done-set -------------------------


@dataclass(frozen=True)
class IntermediateHeader:
    """Summary counts carried from the extract stage into the join stage.

    Combines the extract-stage tallies stored in ``meta`` with the group count
    derived from the ``ways`` table, giving ``join`` everything it needs for the
    final summary without re-parsing the dump.
    """

    parsed_count: int  # highway ways parsed
    unnamed_count: int  # unnamed ways counted by the Collector
    group_count: int  # number of distinct name groups


def read_header(db: Path) -> IntermediateHeader:
    """Read the extract-stage counts and the name-group count from ``db``.

    ``parsed_count`` and ``unnamed_count`` come from the ``meta`` table (written
    by :func:`set_counts`); a missing key defaults to ``0`` so a partially
    populated database still yields a usable header. ``group_count`` is
    ``COUNT(DISTINCT norm_name)`` over the ``ways`` table, which equals the number
    of :class:`~strade.models.NameGroup` values :func:`read_groups` will yield —
    ways are grouped by their normalization key, so variant spellings of one
    street count once. Opens its own read-only-style connection and
    closes it before returning.
    """
    conn = connect(db)
    try:
        parsed_count = read_meta_int(conn, _META_PARSED_COUNT) or 0
        unnamed_count = read_meta_int(conn, _META_UNNAMED_COUNT) or 0
        row = conn.execute("SELECT COUNT(DISTINCT norm_name) FROM ways").fetchone()
        group_count = int(row[0]) if row is not None else 0
    finally:
        conn.close()
    return IntermediateHeader(
        parsed_count=parsed_count,
        unnamed_count=unnamed_count,
        group_count=group_count,
    )


@dataclass(frozen=True)
class StreetGroup:
    """One aggregated row of the ``street_groups`` table.

    Records how many distinct streets share a ``norm_name``. Join back to
    ``streets.norm_name`` to recover the composing streets and their way ids.
    """

    norm_name: str
    name: str
    count: int


def build_street_groups(conn: sqlite3.Connection) -> None:
    """Rebuild the ``street_groups`` aggregation from the ``streets`` table.

    Clears and repopulates ``street_groups`` with one row per distinct
    ``norm_name``: its representative display ``name`` (the alphabetically first
    name among the streets sharing the key, for determinism) and ``count`` of
    distinct streets carrying that key. Runs in a single transaction so the
    aggregation is always consistent with the ``streets`` table it summarizes.
    """
    with conn:
        conn.execute("DELETE FROM street_groups")
        conn.execute(
            "INSERT INTO street_groups (norm_name, name, count) "
            "SELECT norm_name, MIN(name), COUNT(*) "
            "FROM streets GROUP BY norm_name"
        )


def read_top_street_groups(conn: sqlite3.Connection, limit: int) -> list[StreetGroup]:
    """Return the ``limit`` street groups with the highest street ``count``.

    Ordered by ``count`` descending, ties broken by ``norm_name`` ascending so
    the result is deterministic. Reads the already-populated ``street_groups``
    table, so call :func:`build_street_groups` first.
    """
    rows = conn.execute(
        "SELECT norm_name, name, count FROM street_groups "
        "ORDER BY count DESC, norm_name ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        StreetGroup(norm_name=norm_name, name=name, count=int(count))
        for norm_name, name, count in rows
    ]


def read_groups(db: Path) -> Iterator[NameGroup]:
    """Stream :class:`NameGroup` values from ``db`` in ascending key order.

    Iterates a single cursor over ``ways ORDER BY norm_name, name, way_id``
    (served by the ``ways_by_norm_name`` index) and yields one group per
    contiguous run of equal ``norm_name`` keys. Only one group is materialized at
    a time, so ``join`` never holds the whole dataset in memory. Each row is
    rebuilt into a :class:`~strade.models.HighwayWay` via the existing
    deserialize helpers.

    Grouping is by the normalization key from
    :func:`strade.normalize.normalize_name`, so variant surface forms of one
    street — bilingual ``Viale - Avenue Giuseppe Garibaldi`` and Italian-only
    ``Viale Giuseppe Garibaldi`` — land in the same group and can join.
    A key with a single way still forms its own group.

    The group's :attr:`~strade.models.NameGroup.name` is a representative human
    readable name — the most frequent raw ``name`` in the group, ties broken by
    ascending name — chosen via :func:`_representative_name`. This keeps the
    lossy key out of the output while still collapsing variants. Groups arrive in
    ascending key order; the final street list is re-sorted by display name at
    export time, so key ordering here does not affect output order.

    The connection is opened for the lifetime of the iterator and closed when
    the generator is exhausted or closed, so callers should drive it to
    completion (or close it) to release the database handle.
    """
    conn = connect(db)
    try:
        cursor = conn.execute(
            "SELECT way_id, name, norm_name, node_ids, coords "
            "FROM ways ORDER BY norm_name, name, way_id"
        )
        current_key: str | None = None
        ways: list[HighwayWay] = []
        names: list[str] = []
        for way_id, name, norm_name, node_ids_blob, coords_blob in cursor:
            if current_key is not None and norm_name != current_key:
                yield NameGroup(
                    name=_representative_name(names), key=current_key, ways=ways
                )
                ways = []
                names = []
            current_key = norm_name
            names.append(name)
            ways.append(
                HighwayWay(
                    way_id=int(way_id),
                    name=name,
                    node_ids=deserialize_node_ids_binary(node_ids_blob),
                    coords=deserialize_coords_binary(coords_blob),
                )
            )
        if current_key is not None:
            yield NameGroup(name=_representative_name(names), key=current_key, ways=ways)
    finally:
        conn.close()


def _representative_name(names: list[str]) -> str:
    """Pick the display name for a group from its members' raw names.

    Returns the most frequently occurring raw ``name``; ties are broken by
    ascending name so the choice is deterministic regardless of row order. This
    surfaces the dominant surface form of a street (e.g. the form used by the
    most ways) while keeping the normalization key out of the output.
    """
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    # max() keeps the first item on ties; sorting the items ascending by name
    # first makes "most frequent, then alphabetically first" the tie-break.
    return max(sorted(counts), key=lambda name: counts[name])


class DoneSet:
    """The set of street names already fully written, backed by the ``done`` table.

    Wraps a :class:`sqlite3.Connection` (like :class:`WayWriter`) so ``join`` can
    skip name groups whose streets were committed on a previous run (resume) and
    record newly completed groups. On resume, a name is in ``done`` only if its
    streets were committed in the same transaction, so the set never claims a
    group whose streets are missing.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def load(self) -> set[str]:
        """Return every name recorded in the ``done`` table (skip on resume)."""
        rows = self._conn.execute("SELECT name FROM done").fetchall()
        return {row[0] for row in rows}

    def clear(self) -> None:
        """Drop every done-marker so the next ``join`` run starts clean.

        Called once all name groups have been processed, meaning the join is
        complete. Emptying the ``done`` table means a subsequent ``join`` no
        longer treats the finished database as a partial run to resume; it
        re-joins every group instead. Runs in its own transaction.
        """
        with self._conn:
            self._conn.execute("DELETE FROM done")

    def mark(self, name: str) -> None:
        """Record ``name`` as done via ``INSERT OR IGNORE`` (idempotent).

        Does not open its own transaction, mirroring :func:`write_meta_int`, so
        the caller (the Writer, task 12) commits the ``done`` marker together
        with that group's ``streets`` rows in one transaction. Wrap the call in
        ``with conn:`` (or a surrounding transaction) to persist it.
        """
        self._conn.execute("INSERT OR IGNORE INTO done (name) VALUES (?)", (name,))


# --- Join side: street writer -----------------------------------------------


def serialize_way_ids(way_ids: list[int]) -> str:
    """Serialize a street's sorted composing way ids to JSON text."""
    return json.dumps(way_ids)


class StreetWriter:
    """Persist a completed group's streets to the ``streets`` table.

    Wraps a :class:`sqlite3.Connection` like :class:`WayWriter` and
    :class:`DoneSet`, reusing the store's connection and transaction patterns
    rather than introducing new ones. Each call to :meth:`write_group_streets`
    inserts one group's streets and marks the group's name done in a single
    transaction, so ``join`` can resume at the street-name granularity: a name
    appears in ``done`` only when its streets are committed.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Reuse the existing done-set writer so the done marker is inserted with
        # the same INSERT OR IGNORE semantics used elsewhere on the join side.
        self._done = DoneSet(conn)

    def clear(self) -> None:
        """Empty the ``streets`` table so a fresh ``join`` starts from scratch.

        Called at the start of a non-resuming ``join`` (an empty ``done`` set) so
        re-running against a populated database does not accumulate duplicate
        street rows. A resuming run keeps the streets already committed for its
        done groups, so the caller only invokes this when the ``done`` set is
        empty. Runs in its own transaction.
        """
        with self._conn:
            self._conn.execute("DELETE FROM streets")

    def write_group_streets(self, done_key: str, streets: list[Street]) -> None:
        """Insert one completed group's streets and mark it done in one transaction.

        All of ``streets`` are inserted into the ``streets`` table and ``done_key``
        is recorded in the ``done`` table within a single ``with conn:`` block, so
        a crash rolls back both together and never leaves streets without their
        done marker (or vice versa). ``done_key`` is the group's normalization key
        (``NameGroup.key``), not its display name, so resume is stable regardless
        of which surface form was chosen for output. ``way_ids`` are stored as JSON
        text, already sorted and de-duplicated by the
        :class:`~strade.models.Street` dataclass.
        """
        with self._conn:
            self._conn.executemany(
                "INSERT INTO streets (name, norm_name, way_ids) VALUES (?, ?, ?)",
                [
                    (
                        street.name,
                        street.norm_name,
                        serialize_way_ids(street.way_ids),
                    )
                    for street in streets
                ],
            )
            self._done.mark(done_key)
