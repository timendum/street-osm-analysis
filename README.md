# strade

`strade` is a CLI that reads an OpenStreetMap dump and produces a distinct list
of named streets. It targets regions where one physical street appears under
several surface forms and collapses
those variants together.

Only the normalization step is locale-specific: it strips street-type prefixes
in Italian (and, to a lesser extent, French) to derive the grouping key. The
rest of the software — parsing, storage, geometry joining, and output — is
locale-independent.

> **Caveat:** the heuristic join measures distances in a projected metric CRS
> that defaults to EPSG:6875 (RDN2008 / Italy zone), which only covers Italy.
> The `-t` threshold in meters is accurate only within that zone; for regions
> outside Italy, change the projection to an appropriate metric CRS or distances
> will be distorted.

The tool runs in two stages that hand off through a SQLite checkpoint database,
so a long run is resumable after an interruption:

1. **`extract`** — stream-parse the dump and store named highway ways.
2. **`join`** — group the stored ways by street and join fragments into distinct
   streets.

## Requirements

- Python >= 3.13
- [`uv`](https://docs.astral.sh/uv/) for dependency management and running the tool
- Optionally [`just`](https://github.com/casey/just) to use the shortcut recipes

## Setup

Install the dependencies into a managed virtual environment:

```sh
uv sync
```

`uv run` (used below) will create and sync the environment automatically, so an
explicit `uv sync` is optional.

## Running

The pipeline runs in two stages that hand off through a SQLite checkpoint
database. Both stages are resumable: re-run the same command after an
interruption and it continues where it left off.

### 1. Extract

Stream-parse an OSM dump and store the named highway ways into the checkpoint
database:

```sh
uv run strade extract <input.osm.pbf> [database.db]
```

The database path is optional. When omitted it defaults to the dump path with
its OSM suffix replaced by `.db` (e.g. `aosta.osm.pbf` → `aosta.db`). You can
also pass it explicitly with `-d/--database`.

```sh
# derives aosta.db from the dump name
uv run strade extract aosta.osm.pbf
```

Supported input formats: `.osm.pbf`, `.osm.xml`, `.osm.bz2`, `.osm.gz`, `.osm`,
`.pbf`.

### 2. Join

Group the stored ways by street, join fragments into distinct streets, and print
the top street groups:

```sh
uv run strade join <database.db> [-t METERS]
```

`-t/--threshold` sets the proximity threshold in meters for the heuristic join.


## Helper commands

### `prefixes`

Scan a dump for candidate street-type words to extend the normalizer's prefix
list. Prints `count<tab>word` lines (most frequent first) to stdout, excluding
words the normalizer already strips:

```sh
uv run strade prefixes <input.osm.pbf>
```

### `threshold`

Report "almost joined" way pairs whose distance falls between the current
threshold and its double, with their distance, to help tune `-t`:

```sh
uv run strade threshold <database.db> [-t METERS]
```

## Verbosity

Every command accepts `-v/--verbose`, which can be repeated: `-v` enables info
output, `-vv` enables debug output. Progress and warnings go to stderr, keeping
stdout reserved for the exported results.

## Development

The `justfile` provides shortcuts for the common tasks:

```sh
just extract <input.osm.pbf>   # uv run strade extract ...
just join <database.db>        # uv run strade join ...
just prefixes <input.osm.pbf>  # uv run strade prefixes ...
just threshold <database.db>   # uv run strade threshold ...

just test                      # run the unittest suite
just lint                      # ruff check
just typecheck                 # ty check
just fmt                       # ruff format
just checks                    # lint + typecheck + test + fmt-check
```

Without `just`, the equivalent commands are:

```sh
uv run python -m unittest discover -s tests -p "test_*.py"
uv run ruff check
uv run ty check
uv run ruff format
```
