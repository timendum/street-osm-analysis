"""Density-normalized prominence map for a target street name.

Bins streets into a square metric grid and colours each cell by how common the
target name is there relative to the whole-map average. Holds only the pure
aggregation (:func:`build_grid`) and matplotlib rendering (:func:`render_map`);
the store reads streets and the CLI (``run_map``) orchestrates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from strade.geometry import Projector

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from strade.cities import CityMatch
    from strade.store import StreetPoint


@dataclass(frozen=True)
class GridCell:
    """One square grid cell with its prominence ratio.

    ``x``/``y`` are the lower-left corner in the metric CRS (meters); the cell
    spans ``[x, x + cell_size)`` by ``[y, y + cell_size)``. ``total`` streets fell
    in the cell, ``matches`` of them carried the target ``norm_name``, and
    ``ratio`` is ``matches / total``. ``populated`` is ``True`` when the cell
    cleared the ``min_streets`` floor; below-floor cells are kept (drawn as a
    distinct colour) but excluded from the location-quotient stats.
    """

    x: float
    y: float
    total: int
    matches: int
    ratio: float
    populated: bool

    def location_quotient(self, global_ratio: float) -> float:
        """Return ``ratio / global_ratio`` (a location quotient).

        ``1.0`` means the target is as common here as across the mapped area,
        ``> 1`` over-represented, ``< 1`` under-represented. A ``global_ratio`` of
        zero yields ``0.0`` to avoid dividing by zero.
        """
        if global_ratio == 0:
            return 0.0
        return self.ratio / global_ratio


@dataclass(frozen=True)
class Grid:
    """The full binned result: cells to plot plus the parameters that made them.

    ``cells`` holds every cell that received a street (empty means none did);
    each is flagged ``populated`` by whether it cleared ``min_streets``.
    ``cell_size`` is the edge length in meters and ``target`` the ``norm_name``
    each cell's ratio measures. ``global_ratio`` is the target's share across the
    ``populated`` cells (``sum(matches) / sum(total)``), the denominator for each
    cell's location quotient; sparse cells are excluded so a stray match cannot
    skew the scale.
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

    Each point is projected to the metric CRS (via ``projector``, default
    :class:`~strade.geometry.Projector`) and assigned to cell
    ``(floor(x / cell_size), floor(y / cell_size))``, tallying total and
    target-matching street counts into a ``matches / total`` ratio. Cells below
    ``min_streets`` are kept but flagged ``populated=False`` and excluded from the
    location-quotient stats (too few streets give a meaningless ratio).

    ``target`` is matched against each point's ``norm_name`` exactly, so it must
    be the normalization key from ``join`` (not a display name), which is what
    makes bilingual/prefix variants count together. ``display_name`` is the plot
    label shown in place of the key, defaulting to ``target``.

    ``points`` is consumed lazily and only per-cell tallies are retained, so this
    scales to a national database. Returns a :class:`Grid` with unordered cells.
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

    One filled square per cell at its projected (x, y) corner. ``populated``
    cells are coloured by their location quotient on a diverging colormap centred
    at ``1.0`` (below-average cool, above-average warm). The colorbar is
    relabelled from quotient into absolute cell share (percent, ``quotient *
    global_ratio``); title and colorbar use ``grid.display_name`` and the area
    average is noted bottom-left.

    Below-floor cells (``populated=False``) are drawn in hatched dark grey and
    omitted from the colorbar, so "too few streets to judge" stays distinct from
    a low share, the average, and an absent cell. Axes use equal aspect; the
    figure is saved (not shown) via the non-interactive ``Agg`` backend so the
    command stays headless.
    """
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PatchCollection
    from matplotlib.colors import TwoSlopeNorm
    from matplotlib.patches import Patch, Rectangle

    fig, ax = plt.subplots(figsize=(10, 12))

    # Below-floor cells drawn first in hatched dark grey (not a colormap value):
    # a texture the smooth diverging scale can't have keeps "too few streets"
    # distinct from a zero share, the near-white average centre, and absent cells.
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
        # Proxy patch so the hatched grey is named in the legend.
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
        # Diverging scale centred on 1.0 (area average): vmax is the largest
        # observed quotient, vmin pinned at 0 (share is never negative).
        vmax = max(max(quotients), 1.0 + 1e-9)
        norm = TwoSlopeNorm(vmin=0.0, vcenter=1.0, vmax=vmax)
        collection = PatchCollection(squares, cmap="YlOrBr", norm=norm)
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


def render_cities_map(
    matches: Iterable[CityMatch],
    output_path: Path,
    display_name: str,
    projector: Projector | None = None,
) -> int:
    """Render the ``cities`` result as a filled comune choropleth to ``output_path``.

    Fills each comune's boundary (reprojected to the metric CRS via ``projector``,
    default :class:`~strade.geometry.Projector`) warm when it contains a matching
    street/square, cold otherwise — the geometric counterpart to the CSV.

    ``matches`` is consumed once; each :class:`~strade.cities.CityMatch` carries a
    WGS84 boundary (``match.area.geometry``) and its ``matched`` flag. Comuni with
    empty geometry are skipped and ``MultiPolygon`` comuni are drawn per part.
    ``display_name`` labels the plot. Returns the number of comuni drawn. Saved
    (not shown) via the ``Agg`` backend so the command stays headless.
    """
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PatchCollection
    from matplotlib.patches import Patch
    from matplotlib.patches import Polygon as MplPolygon

    if projector is None:
        projector = Projector()

    # Warm = matched, cold = not matched; deliberate two-colour choropleth.
    matched_color = "#d73027"  # warm red
    unmatched_color = "#4575b4"  # cold blue

    fig, ax = plt.subplots(figsize=(10, 12))

    matched_patches: list[MplPolygon] = []
    unmatched_patches: list[MplPolygon] = []
    drawn = 0
    for match in matches:
        geometry = match.area.geometry
        if geometry is None or geometry.is_empty:
            continue
        projected = projector.transform_geometry(geometry)
        # A comune may be a single Polygon or a MultiPolygon; iterate the parts
        # uniformly so each ring becomes its own filled patch.
        parts = getattr(projected, "geoms", [projected])
        bucket = matched_patches if match.matched else unmatched_patches
        for part in parts:
            if part.is_empty:
                continue
            bucket.append(MplPolygon(list(part.exterior.coords)))
        drawn += 1

    if unmatched_patches:
        ax.add_collection(
            PatchCollection(
                unmatched_patches,
                facecolor=unmatched_color,
                edgecolor="white",
                linewidth=0.2,
            )
        )
    if matched_patches:
        ax.add_collection(
            PatchCollection(
                matched_patches,
                facecolor=matched_color,
                edgecolor="white",
                linewidth=0.2,
            )
        )

    if matched_patches or unmatched_patches:
        ax.legend(
            handles=[
                Patch(facecolor=matched_color, edgecolor="white", label="matched"),
                Patch(facecolor=unmatched_color, edgecolor="white", label="not matched"),
            ],
            loc="lower right",
            fontsize="small",
        )
        ax.autoscale_view()
    else:
        ax.text(
            0.5,
            0.5,
            "no comune boundaries to map",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )

    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title(f"Comuni matching '{display_name}'")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return drawn
