"""Density-normalized prominence map for a target street name.

The ``map`` command shows where a given street name is proportionally most
common, rather than a plain map of street density. Streets are binned into a
square metric grid; :func:`build_grid` computes each cell's target share and
:func:`render_map` colours it by that share relative to the whole-map average.

This module holds only the pure aggregation (:func:`build_grid`) and matplotlib
rendering (:func:`render_map`); reading streets is the store's job
(:func:`strade.store.read_street_points`) and orchestration the CLI's
(``run_map``), which keeps the aggregation free of I/O and directly testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from strade.geometry import Projector

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from strade.store import StreetPoint


@dataclass(frozen=True)
class GridCell:
    """One populated square grid cell with its prominence ratio.

    ``x`` and ``y`` are the cell's lower-left corner in the projected metric CRS
    (meters); the cell spans ``[x, x + cell_size)`` by ``[y, y + cell_size)``.
    ``total`` is how many streets fell in the cell and ``matches`` how many of
    those carried the target ``norm_name``; ``ratio`` is ``matches / total``, the
    density-independent prominence plotted for the cell.

    ``populated`` is ``True`` when the cell cleared the ``min_streets`` floor. A
    cell below the floor is still kept (so it can be drawn as a distinct
    "too-sparse-to-judge" colour rather than vanishing into the map background),
    but it is excluded from the location-quotient statistics because a couple of
    streets there would give a meaningless ratio.
    """

    x: float
    y: float
    total: int
    matches: int
    ratio: float
    populated: bool

    def location_quotient(self, global_ratio: float) -> float:
        """Return this cell's ratio relative to ``global_ratio`` (a location quotient).

        ``ratio / global_ratio``: ``1.0`` means the target is exactly as common
        here as across the mapped area, ``> 1`` locally over-represented, ``< 1``
        under-represented. Dividing out the target's overall share is what lets a
        ubiquitous name (present in nearly every cell) still reveal where it is
        *disproportionately* common, instead of the map collapsing into a plain
        map of where streets are. ``global_ratio`` of zero (the target matched
        nothing) yields ``0.0`` so the map degrades gracefully rather than
        dividing by zero.
        """
        if global_ratio == 0:
            return 0.0
        return self.ratio / global_ratio


@dataclass(frozen=True)
class Grid:
    """The full binned result: the cells to plot and the parameters that made them.

    ``cells`` holds every populated cell, each flagged ``populated`` by whether it
    cleared the ``min_streets`` floor: cells above the floor are coloured by their
    location quotient, cells below it are drawn in a distinct neutral colour so
    "too few streets to judge" is visibly different from "low share". An empty
    list means no street was placed at all. ``cell_size`` is the edge length in
    meters and ``target`` the ``norm_name`` whose share each cell's ratio
    measures; both are carried through for the plot title/labels.

    ``global_ratio`` is the target's share across the cells that cleared the floor
    (``sum(matches) / sum(total)`` over the ``populated`` cells), i.e. the
    denominator each cell's location quotient is taken against, so a quotient of
    ``1.0`` means "average among the cells that count". Sparse cells are excluded
    from it so a stray match cannot skew the whole scale.
    """

    cells: list[GridCell]
    cell_size: float
    target: str
    display_name: str  # human-readable label for the plot (falls back to target)
    min_streets: int  # floor separating scored cells from the grey "too sparse" ones
    total_streets: int  # streets placed on the grid (before the min_streets floor)
    matching_streets: int  # of those, how many carried the target norm_name
    global_ratio: float  # target share over plotted cells (location-quotient base)


def build_grid(
    points: Iterable[StreetPoint],
    target: str,
    cell_size: float,
    min_streets: int,
    projector: Projector | None = None,
    display_name: str | None = None,
) -> Grid:
    """Bin street points into a square metric grid and compute per-cell prominence.

    Each :class:`~strade.store.StreetPoint` is projected from WGS84 to the metric
    CRS (via ``projector``, default :class:`~strade.geometry.Projector`) so cell
    edges are a true distance in meters, then assigned to the cell
    ``(floor(x / cell_size), floor(y / cell_size))``. For each cell the total and
    target-matching street counts are tallied and turned into a ``matches /
    total`` ratio.

    Cells with fewer than ``min_streets`` streets are kept but flagged
    ``populated=False``: with only a few streets a single match yields an
    extreme, meaningless ratio, so they are excluded from the location-quotient
    statistics and drawn in a distinct neutral colour by :func:`render_map`
    instead of being coloured as if their ratio were trustworthy. ``target`` is
    compared against each point's ``norm_name`` exactly, so it must be the
    normalization key (as produced by ``join``), not a display name — this is
    what makes bilingual/prefix variants count together.

    ``display_name`` is the human-readable label the plot shows in place of the
    lossy ``target`` key (e.g. ``Via Roma`` for ``roma``); it defaults to
    ``target`` when not supplied, since the aggregation itself only needs the key.

    Streaming: ``points`` is consumed lazily and only the per-cell tallies are
    retained, so this scales to a national database without holding every street
    in memory. Returns a :class:`Grid`; its ``cells`` are unordered.
    """
    if cell_size <= 0:
        raise ValueError("cell_size must be positive")
    if projector is None:
        projector = Projector()

    # Per-cell (total, matches) keyed by integer (col, row). Only tallies are
    # kept, so memory grows with the number of populated cells, not streets.
    tallies: dict[tuple[int, int], list[int]] = {}
    total_streets = 0
    matching_streets = 0
    for point in points:
        x, y = projector.transform_point(point.lon, point.lat)
        col = int(x // cell_size)
        row = int(y // cell_size)
        cell = tallies.setdefault((col, row), [0, 0])
        cell[0] += 1
        total_streets += 1
        if point.norm_name == target:
            cell[1] += 1
            matching_streets += 1

    cells = [
        GridCell(
            x=col * cell_size,
            y=row * cell_size,
            total=total,
            matches=matches,
            ratio=matches / total,
            populated=total >= min_streets,
        )
        for (col, row), (total, matches) in tallies.items()
    ]
    # Location-quotient denominator: the target's share across only the cells
    # that cleared the floor, so a quotient of 1.0 means "average among the cells
    # that count". Sparse cells are excluded so a stray match cannot skew it.
    scored = [cell for cell in cells if cell.populated]
    plotted_total = sum(cell.total for cell in scored)
    plotted_matches = sum(cell.matches for cell in scored)
    global_ratio = plotted_matches / plotted_total if plotted_total else 0.0
    return Grid(
        cells=cells,
        cell_size=cell_size,
        target=target,
        display_name=display_name if display_name is not None else target,
        min_streets=min_streets,
        total_streets=total_streets,
        matching_streets=matching_streets,
        global_ratio=global_ratio,
    )


def render_map(grid: Grid, output_path: Path) -> None:
    """Render ``grid`` to an image file at ``output_path``.

    Draws one filled square per populated cell, positioned at its projected
    (x, y) corner. Cells are **coloured by their location quotient** — the cell's
    target share divided by the target's share across the whole mapped area
    (``grid.global_ratio``) — on a diverging colormap centred at ``1.0``: cells
    below the area average read cool, cells above it read warm. Colouring by the
    quotient rather than the raw share is what makes even a ubiquitous name show
    where it is *disproportionately* common instead of the map collapsing into a
    plain map of where streets are.

    The colorbar, however, is **labelled in absolute cell share** (percent), not
    in quotient units: the tick at each quotient level is relabelled to the
    share it corresponds to (``quotient * global_ratio``), so the legend is read
    in the intuitive "X% of this cell's streets" terms while the colour still
    encodes over/under-representation. The colorbar and title use
    ``grid.display_name`` (the dominant human-readable name) rather than the lossy
    key; the area-wide average that sits at the colormap centre is noted in the
    plot's bottom-left corner.

    Cells that did not clear the ``min_streets`` floor (``populated=False``) are
    drawn in a hatched dark grey rather than coloured by a quotient, so a sparse
    cell reads as "too few streets to judge" and is visibly distinct from a
    genuinely low share (the cool end), an average share (the colormap's near
    white centre), and an absent cell (blank background). The hatch and a darker
    grey are deliberate: a plain light grey is too close to the near-white centre
    of the diverging scale, so the texture and a labelled legend entry keep the
    two apart. Sparse cells are omitted from the colorbar entirely.

    The axes use an equal aspect ratio so the metric grid is not distorted, and
    the footprint of the plotted cells traces the mapped area on its own — no
    boundary/basemap input is required. The figure is saved (not shown), so the
    command stays headless. Uses the non-interactive ``Agg`` backend, selected
    before importing pyplot so rendering never depends on a display.
    """
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PatchCollection
    from matplotlib.colors import TwoSlopeNorm
    from matplotlib.patches import Patch, Rectangle

    fig, ax = plt.subplots(figsize=(10, 12))

    # Cells below the min_streets floor are drawn first, so they read as "too few
    # streets to judge" rather than as a zero share (the low, blue end of the
    # diverging scale) or as absent (blank background). A plain grey would clash
    # with the diverging colormap's near-white centre (a quotient of 1.0, the
    # area average), so these use a *darker* grey plus a hatch pattern — a texture
    # no point on the smooth colour scale can have — and a labelled legend entry,
    # to stay unmistakable. They carry no colormap value of their own.
    sparse = [cell for cell in grid.cells if not cell.populated]
    if sparse:
        sparse_squares = [
            Rectangle((cell.x, cell.y), grid.cell_size, grid.cell_size) for cell in sparse
        ]
        ax.add_collection(
            PatchCollection(
                sparse_squares,
                facecolor="darkgrey",
                edgecolor="grey",
                linewidth=0.3,
                hatch="///",
            )
        )
        # A proxy patch so the hatched grey is explicitly named in a legend,
        # rather than left for the reader to infer from the map alone. The label
        # is generic (no norm_name, no threshold count) — it only conveys that
        # these cells lacked enough streets to score.
        sparse_key = Patch(
            facecolor="darkgrey",
            edgecolor="grey",
            hatch="///",
            label="too few streets",
        )
        ax.legend(handles=[sparse_key], loc="lower right", fontsize="small")

    scored = [cell for cell in grid.cells if cell.populated]
    if scored and grid.global_ratio > 0:
        squares = [
            Rectangle((cell.x, cell.y), grid.cell_size, grid.cell_size) for cell in scored
        ]
        quotients = [cell.location_quotient(grid.global_ratio) for cell in scored]
        # Diverging scale centred on 1.0 (the area average): below reads cool,
        # above warm. vmax is the largest observed quotient so the warm half
        # spans the real over-representation; vmin pinned at 0 (a cell can be as
        # low as zero share but never negative).
        vmax = max(max(quotients), 1.0 + 1e-9)
        norm = TwoSlopeNorm(vmin=0.0, vcenter=1.0, vmax=vmax)
        collection = PatchCollection(squares, cmap="coolwarm", norm=norm)
        collection.set_array(quotients)
        ax.add_collection(collection)

        cbar = fig.colorbar(
            collection,
            ax=ax,
            label=f"share of streets named '{grid.display_name}' per cell",
            shrink=0.6,
        )
        # Colour encodes the location quotient, but the ticks are relabelled into
        # absolute share (quotient * area share) so the legend reads in percent.
        tick_quotients = [t for t in cbar.get_ticks() if 0.0 <= t <= vmax]
        cbar.set_ticks(tick_quotients)
        cbar.set_ticklabels([f"{q * grid.global_ratio:.2%}" for q in tick_quotients])

        # The area-wide average (the colour centre) is reported in the plot's
        # bottom-left corner rather than crammed into the colorbar label.
        ax.text(
            0.0,
            0.0,
            f"area average {grid.global_ratio:.2%} (colour centre)",
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize="small",
            color="dimgrey",
        )

    if grid.cells:
        # Fit the view to every drawn cell (scored and sparse alike).
        ax.autoscale_view()
    else:
        ax.text(
            0.5,
            0.5,
            f"no streets matched '{grid.display_name}'"
            if grid.total_streets == 0
            else "no streets to map",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )

    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title(f"Relative prominence of '{grid.display_name}'")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
