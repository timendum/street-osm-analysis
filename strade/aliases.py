"""Parse and validate a hand-curated street-alias file.

The automatic normalizer (:mod:`strade.normalize`) collapses bilingual and
type-prefix variants of one street onto a single ``norm_name`` key, but it
cannot catch spelling variants that come from bad OSM data — ``carlomarx``,
``karlmarx``, ``marx``, ``kmarx`` all name the same street yet key differently.

After ``join`` has produced the ``streets`` table, a user reviews the
``street_groups`` output, spots such variants, and lists them in a plain-text
alias file. Each line maps one *variant* key to its *canonical* key::

    # Karl Marx, however OSM spelled him
    carlomarx=karlmarx
    carlmarx=karlmarx
    marx=karlmarx
    kmarx=karlmarx

Blank lines and ``#`` comments are ignored, and surrounding whitespace on each
side of the ``=`` is trimmed. This module only parses and validates the file
into a ``{old: new}`` mapping; applying it to the database lives in
:func:`strade.store.apply_aliases`, and the ``alias`` command orchestrates the
two (see :func:`strade.cli.run_alias`).

Validation is deliberately strict so an ambiguous file never reaches the
database: a malformed line, a duplicate variant, or an *inconsistent* mapping
(a canonical key that is itself listed as a variant elsewhere, e.g. ``a=b`` with
``b=c``) raises :class:`AliasError` and halts the command before any overwrite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Character separating the variant key from its canonical key on each line.
_SEP = "="

# Lines starting with this (after stripping) are comments and are skipped.
_COMMENT = "#"


class AliasError(Exception):
    """A fatal problem with the alias file that must halt the command.

    Raised for a malformed line (no ``=``, or an empty variant/canonical side),
    a duplicate variant key that would fold two ways, or an inconsistent mapping
    where a canonical key is itself listed as a variant. The command reports the
    message and exits non-zero without touching the database.
    """


def parse_alias_file(path: Path) -> dict[str, str]:
    """Parse an alias file at ``path`` into a ``{variant: canonical}`` mapping.

    Reads the file line by line: blank lines and ``#`` comment lines are
    skipped, and every remaining line must contain a single ``old=new`` mapping.
    The line is split on the first ``=`` only, both sides are whitespace-trimmed,
    and a self-map (``x=x``) is dropped as a no-op. The result maps each variant
    key to its canonical key.

    Raises:
        AliasError: if a line has no ``=``, if either side is empty after
            trimming, or if the same variant key appears more than once (a
            variant cannot fold two different ways).

    Note:
        This validates each line in isolation. Cross-line consistency (a
        canonical key that is also a variant) is checked separately by
        :func:`check_consistency` so the caller can run it against the freshly
        parsed mapping before touching the database.
    """
    mapping: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(_COMMENT):
            continue
        if _SEP not in line:
            raise AliasError(
                f"{path}:{line_number}: expected 'old{_SEP}new', got {raw_line!r}"
            )
        old_part, new_part = line.split(_SEP, 1)
        old = old_part.strip()
        new = new_part.strip()
        if not old or not new:
            raise AliasError(
                f"{path}:{line_number}: both sides of '{_SEP}' must be non-empty, "
                f"got {raw_line!r}"
            )
        if old == new:
            # A key mapped to itself changes nothing; drop it so it is not
            # mistaken for an inconsistency (a value equal to its own key).
            continue
        if old in mapping:
            raise AliasError(
                f"{path}:{line_number}: duplicate variant key {old!r} "
                f"(already maps to {mapping[old]!r})"
            )
        mapping[old] = new
    return mapping


def check_consistency(mapping: dict[str, str]) -> None:
    """Raise :class:`AliasError` if any canonical key is also a variant key.

    A mapping is inconsistent when a value (canonical key) also appears as a key
    (variant), for example ``{"a": "b", "b": "c"}``: ``a`` is told to become
    ``b`` while ``b`` itself becomes ``c``, so applying the file as written would
    leave ``a`` at the non-canonical ``b``. Rather than resolve such chains
    silently, the mapping is rejected so the user fixes the file. The error names
    every offending canonical key so the file can be corrected in one pass.

    A self-map (``x=x``) never reaches here because :func:`parse_alias_file`
    drops it, so an ``x`` appearing on both sides is genuinely a chain.
    """
    keys = set(mapping)
    offenders = sorted(value for value in mapping.values() if value in keys)
    if offenders:
        joined = ", ".join(offenders)
        raise AliasError(
            "inconsistent alias file: canonical key(s) also listed as a variant: "
            f"{joined}"
        )


def unknown_keys(mapping: dict[str, str], existing_keys: set[str]) -> set[str]:
    """Return the variant keys in ``mapping`` that are absent from the data.

    ``existing_keys`` is the set of ``norm_name`` values actually present in the
    ``streets`` table. A variant key not among them relabels nothing; the caller
    reports these as a non-fatal warning, since a shared alias file may list
    streets that exist in some regions but not the one being processed.
    """
    return set(mapping) - existing_keys
