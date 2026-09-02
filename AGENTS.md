## Project overview

`strade` is a CLI that reads an OpenStreetMap dump and produces a distinct list
of named Italian streets. It targets bilingual regions (e.g. Valle d'Aosta),
where one physical street appears under several surface forms, and collapses
those variants together.

The tool runs in two stages that hand off through a SQLite checkpoint database,
so a long run is resumable after an interruption:

1. **`extract`** — stream-parse the dump and store named highway ways.
2. **`join`** — group the stored ways by street and join fragments into distinct
   streets.

A helper **`prefixes`** command scans a dump for candidate street-type words to
extend the normalizer's prefix list. A helper **`threshold`** command reports "almost joined" way pairs whose distance is between the current threshold and its double, with the distance, to help tune `-t`. A helper **`map`** command plots where a target `norm_name` is proportionally most common: it bins the joined streets into a square metric grid and colours each cell by the share of its streets carrying that key, so the result is normalized against street density rather than being a plain density/population map.

### Technical decisions

Use modern Python:
- `uv` for dependecies, environment and run code
- use type annotations and `ruff` to check and format
- prefer pathlib
- string generation with t-strings
- no external test framework, use `unittest`

If a edit change something written here, ask the user if he wants to update AGENTS.md.

### Pipeline / module map (`strade/`)

- `cli.py` — argument parsing and the `run_xxx` orchestration;
  console entry point `main` (also reachable via `main.py`).
- `parser.py` — streaming pyosmium reader; yields `HighwayWay` for each way with
  a `highway` tag, resolving node coordinates in one pass. Supports resume via a
  way-id cursor.
- `collector.py` — routes named ways to storage and counts unnamed ones during
  the extract pass. Grouping is deliberately deferred to the join side.
- `store.py` — SQLite schema, connection (WAL), serialization, `WayWriter`,
  `StreetWriter` (persists a group's streets and its done-marker in one
  transaction), `DoneSet`, header/count metadata, `read_groups` (streams
  ways ordered by normalization key, yielding one `NameGroup` per contiguous
  run), and `read_street_points` / `count_streets` (streams joined streets as
  representative points for the `map` command).
- `normalize.py` — derives the language/type-agnostic grouping key: strips
  street-type prefixes (Italian + French) and folds to lowercase ASCII
  letters/digits, so bilingual and prefix variants collapse to one key.
- `joiner.py` — joins a `NameGroup`'s ways into `Street`s via union-find using
  two rules: Certain_Join (shared OSM node id) and Heuristic_Join (projected
  geometries within a meters threshold, coarse-filtered with an STRtree). Also
  finds and prints borderline candidate pairs for the `threshold` command.
- `geometry.py` — `Projector` transforms WGS84 lon/lat to a metric projected
  CRS so distances are measured in meters (`transform_point` for a singl
  coordinate, used by `map`).
- `mapper.py` — the `map` command's pure aggregation and rendering: `build_grid`
  bins streets into a square metric grid and computes each cell's target share,
  and `render_map` draws the coloured grid to an image with matplotlib.
- `writer.py` — prints the top street-group summary.
- `models.py` — core dataclasses: `NodeRef`, `HighwayWay`, `NameGroup`, `Street`.
- `prefixes.py` — first-word scan for discovering unhandled street-type prefixes.
- `reporter.py` — non-fatal warning/progress sink and exit-code aggregation.
- `validation.py` — input-path/format validation (`InputError`, `SupportedFormat`).

### Key design points

- **Resumability**: both stages track progress in the database (way-id cursor for
  `extract`, per-group done-markers for `join`) and commit output with its marker
  in a single transaction, so a crash never leaves half-written state.
- **Streaming**: the full dataset is never materialized in memory; grouping is
  done by reading ways in normalization-key order.
- **Key vs. display name**: the normalization key groups streets and is the
  stable resume marker; a representative raw name is chosen for display so the
  lossy key never reaches the output.

### Dependencies

`osmium` (pyosmium), `pyproj`, `shapely`, `tqdm`, `matplotlib` (the `map`
command's rendering). Python >= 3.13.
