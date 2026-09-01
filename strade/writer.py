"""Print the join-side street-group summary.

The canonical join output lives in the database: distinct streets in the
``streets`` table (persisted by :class:`strade.store.StreetWriter`) and their
per-``norm_name`` aggregation in ``street_groups``. This module renders the
highest-count rows of that aggregation to a stream (standard output for
``join``) as tab-separated ``count/norm_name/name`` lines, so the summary is
visible without opening the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from strade.store import StreetGroup

# Field separator for the printed street-group summary rows.
_FIELD_SEP = "\t"


def print_top_street_groups(groups: list[StreetGroup], stream: TextIO) -> None:
    """Write the given street groups to ``stream`` as tab-separated rows.

    Emits a header line followed by one ``count<TAB>norm_name<TAB>name`` line per
    group, in the order the groups are given (the caller already sorts them by
    descending street count). Written to ``stream`` (standard output for the
    ``join`` command) so the aggregation is visible without opening the database.
    """
    stream.write(f"count{_FIELD_SEP}norm_name{_FIELD_SEP}name\n")
    stream.writelines(
        f"{group.count}{_FIELD_SEP}{group.norm_name}{_FIELD_SEP}{group.name}\n"
        for group in groups
    )
