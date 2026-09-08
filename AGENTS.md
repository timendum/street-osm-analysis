## Project overview

`strade` is a CLI that reads an OpenStreetMap dump and produces a distinct list
of named Italian streets. It targets bilingual regions (e.g. Valle d'Aosta),
where one physical street appears under several surface forms, and collapses
those variants together.

### Description

The core pipeline runs in two stages that hand off through a SQLite checkpoint
database. The rest are helper commands.

The database defaults to the dump path with a `.db` extension, or is passed as
an optional positional argument.

- **`extract`** — stream-parse the dump and store named highway ways. A second
  pass also stores every named `place=square` element (node, closed way, or
  multipolygon relation), each reduced to a single representative point, in a
  separate `squares` table for the `cities` command.

  > **Resumable**: tracks a way-id cursor so a long run survives an interruption.

- **`join`** — group the stored ways by street and join fragments into distinct
  streets.

  > **Resumable**: tracks per-group done-markers so a long run survives an
  > interruption.

- **`prefixes`** (helper) — scans a dump for candidate street-type words to
  extend the normalizer's prefix list.

- **`threshold`** (helper) — reports "almost joined" way pairs whose distance is
  between the current threshold and its double, with the distance, to help tune
  `-t`.

- **`map`** — plots where a target `norm_name` is proportionally most
  common: it bins the joined streets into a square metric grid and colours each
  cell by the share of its streets carrying that key, so the result is
  normalized against street density rather than being a plain density/population
  map.

- **`alias`** (runs after `join`) — merges street-name variants the normalizer
  cannot catch (bad OSM data such as `carlomarx`, `karlmarx`, `marx`) using a
  hand-curated `old=new` file: it rewrites each variant `norm_name` in the
  `streets` table to its canonical key and rebuilds `street_groups`. The file
  defaults to `aliases.txt` beside the database (`-a` overrides).

- **`cities`** (runs after `extract`) — flags, for every `admin_level=8`
  comune, whether it contains at least one street or square whose name matches a
  file of SQLite `LIKE` patterns, via a point-in-polygon test against the comune
  boundaries read from the dump. Writes one CSV row per comune
  to a `<database>-<pattern_file>.csv` file beside the database.  
  An optional `-r/--region <osm_relation_id>` clips the report to a parent boundary (country `admin_level=2` / region `4`), dropping comuni that spill in from across the extract's cut; the hierarchy is hardcoded for Italy.

### Technical decisions

Use modern Python:
- `uv` for dependecies, environment and run code
- use type annotations and `ruff` to check and format
- prefer pathlib
- string generation with t-strings
- no external test framework, use `unittest`

If a edit change something written here, ask the user if he wants to update AGENTS.md.

### Module map (`strade/`)

- `cli.py` — argument parsing and the `run_xxx` orchestration.  
  Console entry point `main` (also reachable via `main.py`).
- `parser.py` — streaming pyosmium readers:
  - `parse_highways` yields a `HighwayWay` per `highway`-tagged way, resolving
  node coordinates in one pass and supporting resume via a way-id cursor.
  - `parse_admin_areas` yields a `CityArea` per administrative boundary at the
  levels `cities` reads (comune `8` plus parent `2`/`4`).
  - `parse_squares` yields a `Square` per named `place=square` element, reduced
  to a representative point.
- `collector.py` — routes named ways to storage and counts unnamed ones during
  the extract pass. Grouping is deliberately deferred to the join side.
- `store.py` — the SQLite checkpoint layer both stages hand off through: schema,
  WAL connection, way/coord (de)serialization, and the writer/reader helpers for
  ways, joined streets and squares. Owns the resume markers (extract's way-id
  cursor via header/count metadata, join's per-group `DoneSet`) and the reads
  the later commands query (`read_groups` in normalization-key order, street
  points, alias relabeling, `*_matching` pattern lookups).
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
- `models.py` — core dataclasses: `NodeRef`, `HighwayWay`, `NameGroup`, `Street`,
  `CityArea`, and `Square`.
- `prefixes.py` — first-word scan for discovering unhandled street-type prefixes.
- `aliases.py` — parses and validates the `alias` command's `old=new` file into a
  variant→canonical mapping, rejecting malformed, duplicate, or inconsistent
  mappings and reporting variant keys absent from the data.
- `patterns.py` — parses the `cities` command's LIKE-pattern file into a list of
  patterns (`parse_pattern_file`), skipping blank/`#` lines and rejecting an
  empty file (`PatternError`).
- `cities.py` — the `cities` command's pure aggregation: splits the parsed
  boundaries into comuni and parent (region/country) levels, optionally clipping
  to the comuni inside a `-r` parent, then wraps an STRtree over the kept comune
  boundaries for point-in-polygon lookup, flags each comune from the matched
  way/square points, and renders one CSV row per comune. Point containment uses
  the `intersects` predicate, since `STRtree.query` reads
  `query_geom.predicate(tree_geom)`.
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
- **Aliasing is a post-join relabel**: `alias` only rewrites `norm_name` on
  already-joined `streets` rows (never moving way ids or merging rows) and is not
  wired into `join`, so re-running `join` discards it and the `alias` command
  must be re-run.

### Dependencies

`osmium` (pyosmium), `pyproj`, `shapely`, `tqdm`, `matplotlib` (the `map`
and optionaly `cities` command's rendering). Python >= 3.13.
