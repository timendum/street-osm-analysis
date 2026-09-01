"""Core data models for the Italian Street Extractor.

These dataclasses describe the records that flow through the pipeline:

- ``NodeRef``   — an OSM node id with its geographic coordinate (immutable).
- ``HighwayWay`` — a highway way with its ordered node ids and resolved coords.
- ``NameGroup`` — all highway ways that share one exact street name.
- ``Street``     — a distinct produced street: display name, norm_name key, and
  sorted, de-duplicated way ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NodeRef:
    """An OSM node reference with its coordinate.

     Frozen/immutable: a node's id and location never change once resolved
    .
    """

    node_id: int
    lon: float
    lat: float


@dataclass
class HighwayWay:
    """A highway way parsed from the OSM dump.

    Carries the ordered list of referenced node ids and the
    resolved coordinate of each node. ``name`` is ``None`` when
    the way has no ``name`` tag.
    """

    way_id: int
    name: str | None
    node_ids: list[int] = field(default_factory=list)
    coords: list[NodeRef] = field(default_factory=list)


@dataclass
class NameGroup:
    """All highway ways that share one normalization key.

    ``key`` is the language- and type-agnostic grouping key from
    :func:`strade.normalize.normalize_name`; ways whose raw names differ only by
    street-type prefix, language, or diacritics share a key and join together.
    ``name`` is a representative human-readable name chosen for output — the lossy
    ``key`` never reaches the produced street list. ``key`` is the stable resume
    marker for the join side, since it is independent of which surface form was
    picked for display.
    """

    name: str
    key: str = ""
    ways: list[HighwayWay] = field(default_factory=list)


@dataclass
class Street:
    """A distinct produced street.

    ``way_ids`` holds the identifiers of the composing highway ways and is kept
    sorted and de-duplicated at all times. The invariant is
    enforced on construction and after any assignment via ``__post_init__``.
    ``norm_name`` is the normalization key of the group the street came from,
    carried through to the output so each street records both its display name
    and the grouping key that produced it.
    """

    name: str
    norm_name: str = ""
    way_ids: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._normalize_way_ids()

    def _normalize_way_ids(self) -> None:
        # sorted() over a set both de-duplicates and orders ascending.
        object.__setattr__(self, "way_ids", sorted(set(self.way_ids)))

    @property
    def count(self) -> int:
        """Number of distinct composing way ids in this street."""
        return len(self.way_ids)
