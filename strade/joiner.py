"""Join fragmented highway ways within a name group into distinct streets.

Two join rules that together decide which ways belong to
the same physical street:

- **Certain_Join**: two ways that share at least one OSM node id are
  the same street.
- **Heuristic_Join**: two ways whose projected geometries lie within
  the proximity threshold (in meters) are the same street.

Both rules feed a single disjoint-set (union-find) structure, so connectivity is
transitive across the two rules: a street joined partly by shared nodes
and partly by proximity still collapses into one connected component. Each
maximal connected component becomes one :class:`~strade.models.Street` carrying
the group's name. A group with a single way short-circuits to exactly
one street. A pair whose distance cannot be evaluated is skipped with a
warning rather than aborting the group.

The R-tree (``shapely.STRtree``) is a coarse spatial filter only: it narrows the
O(n²) pairwise comparison to nearby candidates, and every candidate pair is then
confirmed against the exact projected distance before being unioned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from shapely import STRtree, unary_union

from strade.geometry import nearest_metric_points
from strade.models import Street

if TYPE_CHECKING:
    from typing import TextIO

    from shapely import LineString, Point
    from shapely.geometry.base import BaseGeometry

    from strade.geometry import Projector
    from strade.models import HighwayWay, NameGroup
    from strade.reporter import Reporter

# Field separator for the printed candidate-pair rows.
_FIELD_SEP = "\t"

# A name group with this many ways needs no joining: a single way is trivially
# its own street.
_SINGLE_WAY = 1


class UnionFind:
    """A disjoint-set structure over the integer indices ``0..size-1``.

    Uses union by rank and path compression so ``union`` and ``find`` run in
    near-constant amortized time. The Joiner unions way indices that belong to
    the same street and then reads back the connected components.
    """

    def __init__(self, size: int) -> None:
        # Each element starts in its own singleton set.
        self._parent: list[int] = list(range(size))
        self._rank: list[int] = [0] * size

    def find(self, x: int) -> int:
        """Return the representative root of ``x``'s set, compressing the path."""
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression: point every node on the path straight at the root.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        """Merge the sets containing ``a`` and ``b`` (no-op if already merged)."""
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        # Union by rank: attach the shorter tree under the taller one.
        if self._rank[root_a] < self._rank[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        if self._rank[root_a] == self._rank[root_b]:
            self._rank[root_a] += 1

    def components(self) -> dict[int, list[int]]:
        """Group all elements by their set root.

        Returns a mapping from each set's root to the sorted list of member
        indices. Because ``find`` is order-independent, the grouping is stable
        regardless of the order unions were applied.
        """
        groups: dict[int, list[int]] = {}
        for x in range(len(self._parent)):
            groups.setdefault(self.find(x), []).append(x)
        return groups


def join_group(
    group: NameGroup,
    threshold_m: float,
    projector: Projector,
    reporter: Reporter,
) -> list[Street]:
    """Join the ways of one name group into distinct streets.

    Applies the Certain_Join (shared node ids) and Heuristic_Join (proximity
    within ``threshold_m`` meters) rules through a shared union-find, then emits
    one :class:`Street` per connected component, each carrying ``group.name``,
    the group's ``norm_name`` key, and the sorted, de-duplicated way ids of its
    members.

    Args:
        group: The name group whose ways are joined.
        threshold_m: Proximity threshold in meters for the Heuristic_Join.
        projector: Projects ways to a metric CRS and measures distance in meters.
        reporter: Receives a non-fatal warning for any pair whose distance cannot
            be evaluated.

    Returns:
        One :class:`Street` per maximal connected component of ways.
    """
    ways = group.ways

    # Single-way fast path: no joining is possible, so the way is its own street
    # . Also covers the empty-group edge case by returning [].
    if len(ways) <= _SINGLE_WAY:
        return [
            Street(name=group.name, norm_name=group.key, way_ids=[w.way_id]) for w in ways
        ]

    uf = _build_union_find(group, threshold_m, projector, reporter)

    # One Street per maximal connected component. Street keeps its
    # way_ids sorted and de-duplicated via its own __post_init__.
    return [
        Street(
            name=group.name,
            norm_name=group.key,
            way_ids=[ways[k].way_id for k in indices],
        )
        for indices in uf.components().values()
    ]


def _build_union_find(
    group: NameGroup,
    threshold_m: float,
    projector: Projector,
    reporter: Reporter,
) -> UnionFind:
    """Build the union-find that joins one group's ways at ``threshold_m``.

    Runs both join rules — Certain_Join (shared node ids) and Heuristic_Join
    (proximity within ``threshold_m``) — over a :class:`UnionFind` indexed by
    ``group.ways`` position, so callers can read back the same connected
    components :func:`join_group` produces. Shared by :func:`join_group` and
    :func:`find_candidate_pairs` so both agree on which ways already belong to
    the same street.
    """
    uf = UnionFind(len(group.ways))
    _certain_join(group, uf)
    _heuristic_join(group, threshold_m, projector, reporter, uf)
    return uf


def _certain_join(group: NameGroup, uf: UnionFind) -> None:
    """Union ways that share at least one OSM node id.

    Walks every way's ordered node ids, recording the first way seen for each
    node id; any later way referencing the same node id is unioned with it. The
    full ``node_ids`` list is used (not just resolved coords) so shared-node
    connectivity does not depend on coordinate resolution.
    """
    node_to_way: dict[int, int] = {}
    for i, way in enumerate(group.ways):
        for node_id in way.node_ids:
            first = node_to_way.get(node_id)
            if first is None:
                node_to_way[node_id] = i
            else:
                uf.union(i, first)


def _heuristic_join(
    group: NameGroup,
    threshold_m: float,
    projector: Projector,
    reporter: Reporter,
    uf: UnionFind,
) -> None:
    """Union ways whose projected geometries are within ``threshold_m``.

    Builds an ``STRtree`` over the projectable linestrings as a coarse filter,
    queries each line's nearby candidates, and unions a pair only after the exact
    projected distance confirms it is at most the threshold. Ways with fewer than
    two resolvable coordinates cannot form a line; they are excluded from the
    index and simply remain in whatever component the Certain_Join placed them.
    """
    # Project each way; None marks a degenerate geometry (fewer than two coords).
    lines: list[LineString | None] = [
        projector.to_metric_linestring(w) for w in group.ways
    ]

    # Index only the projectable lines, remembering each line's original way index.
    indexed: list[tuple[int, LineString]] = [
        (i, line) for i, line in enumerate(lines) if line is not None
    ]
    if len(indexed) < _SINGLE_WAY + 1:
        # Fewer than two lines means no pair can be compared.
        return

    geometries = [line for _, line in indexed]
    tree = STRtree(geometries)

    for pos, (i, line) in enumerate(indexed):
        # "dwithin" returns candidates whose geometry is within threshold_m; it is
        # a coarse filter that we confirm below with the exact projected distance.
        for candidate in tree.query(line, predicate="dwithin", distance=threshold_m):
            other_pos = int(candidate)
            if other_pos <= pos:
                # Skip self-pairs and pairs already considered in the other order.
                continue
            j, other_line = indexed[other_pos]
            try:
                within = projector.distance_m(line, other_line) <= threshold_m
            except Exception as error:  # noqa: BLE001 - any failure is non-fatal
                reporter.warn(
                    f"could not evaluate join for ways "
                    f"{group.ways[i].way_id} and {group.ways[j].way_id}: {error}"
                )
                continue
            if within:
                uf.union(i, j)


@dataclass(frozen=True)
class CandidatePair:
    """Two *streets* from one name group that *nearly* join.

    Emitted by :func:`find_candidate_pairs` for threshold tuning. The group's
    ways are first joined into streets (the same connected components
    :func:`join_group` produces); this pair then names two distinct streets whose
    exact geometry-to-geometry distance is at least ``threshold_m`` yet below
    ``2 * threshold_m``. These are the borderline street pairs that a slightly
    larger threshold would collapse into one, so reviewing them (and their exact
    ``distance_m``) helps pick a better threshold.

    ``street_id_a``/``street_id_b`` identify each street by the smallest way id
    in its component — a stable, human-checkable handle. ``way_id_a``/``way_id_b``
    are the specific ways carrying the nearest point on each street (handy for
    jumping straight to the relevant fragment while debugging), and
    ``node_id_a``/``node_id_b`` are the OSM node ids closest to the gap. The node
    ids are informational: the exact ``distance_m`` is measured on the street
    geometries (matching how the Heuristic_Join actually decides), so it may be
    slightly smaller than the node-to-node distance when the closest approach
    falls mid-segment. A node id is ``None`` only if its street has no resolved
    coordinates.
    """

    name: str
    norm_name: str
    street_id_a: int
    street_id_b: int
    way_id_a: int
    way_id_b: int
    node_id_a: int | None
    node_id_b: int | None
    distance_m: float


@dataclass(frozen=True)
class _JoinedStreet:
    """One joined street (union-find component) prepared for distance work.

    Holds everything :func:`find_candidate_pairs` needs to measure the gap to
    another street and describe it: the merged projected ``geometry`` (used for
    the exact street-to-street distance and the STRtree coarse filter), the
    per-way ``(way, line)`` pairs so the way carrying the nearest point can be
    named, and the projected ``(node_id, Point)`` list so the nearest OSM node
    can be recovered. ``street_id`` is the smallest way id in the component, a
    stable handle for output.
    """

    street_id: int
    geometry: BaseGeometry
    lines: list[tuple[HighwayWay, LineString]]
    nodes: list[tuple[int, Point]]


def _build_joined_streets(
    group: NameGroup,
    uf: UnionFind,
    projector: Projector,
) -> list[_JoinedStreet]:
    """Turn one group's union-find components into measurable :class:`_JoinedStreet`s.

    Projects each way once, buckets the projectable ways by their component root,
    and merges each bucket's lines into a single geometry via ``unary_union``.
    Components with no projectable way (every member degenerate) are dropped,
    since no distance can be measured to them.
    """
    ways = group.ways

    # Bucket each component's projectable ways under its union-find root. Only
    # ways with a non-degenerate line are kept, and the narrowed LineString is
    # stored with its way so downstream types stay `LineString`, not
    # `LineString | None`.
    buckets: dict[int, list[tuple[HighwayWay, LineString]]] = {}
    for i, way in enumerate(ways):
        line = projector.to_metric_linestring(way)
        if line is not None:
            buckets.setdefault(uf.find(i), []).append((way, line))

    streets: list[_JoinedStreet] = []
    for member_lines in buckets.values():
        merged = unary_union([line for _, line in member_lines])
        nodes: list[tuple[int, Point]] = []
        for way, _ in member_lines:
            nodes.extend(projector.to_metric_nodes(way))
        streets.append(
            _JoinedStreet(
                street_id=min(way.way_id for way, _ in member_lines),
                geometry=merged,
                lines=member_lines,
                nodes=nodes,
            )
        )
    return streets


def _describe_gap(
    street: _JoinedStreet,
    point: Point,
    projector: Projector,
) -> tuple[int, int | None]:
    """Return ``(way_id, node_id)`` on ``street`` closest to ``point``.

    ``point`` is the nearest point on ``street`` toward the other street. The way
    carrying it is the one whose line is closest to it, and the node is the
    street's projected vertex closest to it — both purely descriptive extras
    layered on top of the exact geometry distance.
    """
    way, _ = min(street.lines, key=lambda item: item[1].distance(point))
    node_id = projector.nearest_node(point, street.nodes)
    return way.way_id, node_id


def find_candidate_pairs(
    group: NameGroup,
    threshold_m: float,
    projector: Projector,
    reporter: Reporter,
) -> list[CandidatePair]:
    """Find *street* pairs in one group whose gap is in ``[threshold_m, 2*threshold_m)``.

    The group's ways are first joined into streets — the same connected
    components :func:`join_group` produces at ``threshold_m`` — so the reported
    unit is a whole street, not a raw fragment. Two distinct streets are reported
    when the exact distance between their merged geometries is at least
    ``threshold_m`` yet below ``2 * threshold_m``: these are the borderline pairs
    a slightly larger threshold would collapse into one street, and their exact
    ``distance_m`` is precisely the quantity that decides whether a bigger
    threshold merges them.

    Working street-first (rather than the old way-first scan) means each near
    pair is emitted once, with the true minimum street-to-street distance, and
    ways already joined into the same street are never compared — they are one
    unit. Each pair also carries the way and OSM node closest to the gap on each
    side (see :class:`CandidatePair`) as debugging extras.

    An ``STRtree`` over the merged street geometries coarse-filters the pairs to
    those within ``2 * threshold_m``; every survivor is confirmed against the
    exact projected distance. Streets with no projectable geometry (all members
    degenerate) are dropped, since no distance can be measured to them.

    Args:
        group: The name group whose streets are compared.
        threshold_m: The current Heuristic_Join threshold in meters. Pairs are
            reported when ``threshold_m <= distance < 2 * threshold_m``.
        projector: Projects ways to a metric CRS and measures distance in meters.
        reporter: Receives a non-fatal warning for any pair whose distance cannot
            be evaluated.

    Returns:
        The candidate street pairs, sorted by ascending distance.
    """
    ways = group.ways
    if len(ways) <= _SINGLE_WAY:
        return []

    upper_m = 2 * threshold_m

    # Join into streets first, then measure gaps between whole streets. The same
    # union-find join_group computes, so the components match the produced output.
    uf = _build_union_find(group, threshold_m, projector, reporter)
    streets = _build_joined_streets(group, uf, projector)
    if len(streets) < _SINGLE_WAY + 1:
        # Fewer than two measurable streets means no pair can be compared.
        return []

    geometries = [s.geometry for s in streets]
    tree = STRtree(geometries)

    pairs: list[CandidatePair] = []
    for pos, street in enumerate(streets):
        # Coarse filter out to the upper band edge; the exact distance below then
        # keeps only street pairs that fall in [threshold_m, 2*threshold_m).
        for candidate in tree.query(
            street.geometry, predicate="dwithin", distance=upper_m
        ):
            other_pos = int(candidate)
            if other_pos <= pos:
                # Skip self-pairs and pairs already considered in the other order.
                continue
            other = streets[other_pos]
            try:
                distance = street.geometry.distance(other.geometry)
            except Exception as error:  # noqa: BLE001 - any failure is non-fatal
                reporter.warn(
                    f"could not evaluate distance for streets "
                    f"{street.street_id} and {other.street_id}: {error}"
                )
                continue
            if not (threshold_m <= distance < upper_m):
                continue

            # Locate the nearest point on each street, then name the way and node
            # carrying it as descriptive extras alongside the exact distance.
            point_a, point_b = nearest_metric_points(street.geometry, other.geometry)
            way_id_a, node_id_a = _describe_gap(street, point_a, projector)
            way_id_b, node_id_b = _describe_gap(other, point_b, projector)
            pairs.append(
                CandidatePair(
                    name=group.name,
                    norm_name=group.key,
                    street_id_a=street.street_id,
                    street_id_b=other.street_id,
                    way_id_a=way_id_a,
                    way_id_b=way_id_b,
                    node_id_a=node_id_a,
                    node_id_b=node_id_b,
                    distance_m=distance,
                )
            )

    pairs.sort(key=lambda p: p.distance_m)
    return pairs


def print_candidate_pairs(
    pairs: list[CandidatePair],
    stream: TextIO,
    header: bool = True,
) -> None:
    """Write borderline candidate street pairs to ``stream`` as tab-separated rows.

    Emits one row per pair with the columns
    ``distance_m<TAB>street_id_a<TAB>street_id_b<TAB>way_id_a<TAB>way_id_b<TAB>``
    ``node_id_a<TAB>node_id_b<TAB>norm_name<TAB>name``, in the order given (the
    caller sorts them by ascending distance). The distance is rounded to two
    decimals and a ``None`` node id is written as an empty field. When ``header``
    is true a column header line is written first, so a caller streaming groups
    can print the header only for the first non-empty batch. Written to
    ``stream`` (standard output for the ``threshold`` command) so the pairs are
    visible without opening the database.
    """
    if header:
        stream.write(
            f"distance_m{_FIELD_SEP}"
            f"way_id_a{_FIELD_SEP}way_id_b"
            f"{_FIELD_SEP}norm_name{_FIELD_SEP}name\n"
        )
    stream.writelines(
        f"{pair.distance_m:.2f}{_FIELD_SEP}"
        f"{pair.way_id_a}{_FIELD_SEP}{pair.way_id_b}"
        f"{_FIELD_SEP}{pair.norm_name}{_FIELD_SEP}{pair.name}\n"
        for pair in pairs
    )
