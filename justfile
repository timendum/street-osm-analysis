default: help

help:
  @just --list --unsorted

extract *ARGS:
  uv run strade extract {{ARGS}}

join *ARGS:
  uv run strade join {{ARGS}}

prefixes *ARGS:
  uv run strade prefixes {{ARGS}}

threshold *ARGS:
  uv run strade threshold {{ARGS}}

# run tests
test *ARGS:
  uv run python -m unittest discover -s tests -p "test_*.py" {{ARGS}}

# sanity checks
checks: lint typecheck test fmt-check

lint *ARGS:
  uv run ruff check {{ARGS}}

fmt *ARGS:
  uv run ruff format {{ARGS}}

fmt-check *ARGS:
  uv run ruff format --check {{ARGS}}

format: fmt

typecheck *ARGS:
  uv run ty check {{ARGS}}
