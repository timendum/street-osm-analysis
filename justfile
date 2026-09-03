set positional-arguments

default: help

help:
  @just --list --unsorted

extract *ARGS:
  uv run strade extract "$@"

join *ARGS:
  uv run strade join "$@"

map *ARGS:
  uv run strade map "$@"

alias *ARGS:
  uv run strade alias "$@"

top DB LIMIT="20":
  sqlite3 -box "{{DB}}" "SELECT count, norm_name, name FROM street_groups ORDER BY count DESC, norm_name ASC LIMIT {{LIMIT}};"

prefixes *ARGS:
  uv run strade prefixes "$@"

threshold *ARGS:
  uv run strade threshold "$@"

# run tests
test *ARGS:
  uv run python -m unittest discover -s tests -p "test_*.py" "$@"

# sanity checks
checks: lint typecheck test fmt-check

lint *ARGS:
  uv run ruff check "$@"

fmt *ARGS:
  uv run ruff format "$@"

fmt-check *ARGS:
  uv run ruff format --check "$@"

format: fmt

typecheck *ARGS:
  uv run ty check "$@"
