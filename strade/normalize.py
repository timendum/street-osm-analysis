"""Derive a street-name grouping key that ignores street type and language.

Bilingual (Italian/French) OSM data carries one physical street under several
surface forms. The key collapses them by stripping leading street-type words and
folding the rest to lowercase ASCII, so ``Viale - Avenue Giuseppe Garibaldi`` and
``Viale Giuseppe Garibaldi`` both key to ``giuseppegaribaldi``. Used only for
grouping; display uses a raw name chosen elsewhere.
"""

from __future__ import annotations

import unicodedata

# Leading street-type words to strip (Italian + French), as lowercase ASCII to
# match the folded tokens. Stripped repeatedly from the name's start.
_PREFIXES: frozenset[str] = frozenset(
    {
        # Italian
        "asse",
        "autostrada",
        "borgata",
        "borgo",
        "calle",
        "cascina",
        "cavalcavia",
        "ciclabile",
        "ciclopedonale",
        "ciclovia",
        "circonvallazione",
        "contra",
        "contrada",
        "corso",
        "corte",
        "cortile",
        "degli",
        "dei",
        "del",
        "della",
        "delle",
        "di",
        "discesa",
        "frazione",
        "galleria",
        "largo",
        "localita",
        "località",
        "lungo",
        "lungolago",
        "lungomare",
        "mulattiera",
        "parco",
        "passaggio",
        "passeggiata",
        "per",
        "percorso",
        "piazza",
        "piazzale",
        "piazzetta",
        "pista",
        "ponte",
        "raccordo",
        "ronco",
        "rotatoria",
        "rotonda",
        "salita",
        "sentiero",
        "sp",
        "ss",
        "strada",
        "stradello",
        "streda",
        "stradone",
        "svincolo",
        "tangenziale",
        "traversa",
        "variante",
        "via",
        "viadotto",
        "viale",
        "vico",
        "vicolo",
        # French
        "allee",
        "allée",
        "avenue",
        "boulevard",
        "chemin",
        "hameau",
        "impasse",
        "localite",
        "localité",
        "montee",
        "montée",
        "place",
        "quai",
        "ru",
        "route",
        "rue",
        "ruelle",
    }
)


def _fold_ascii(text: str) -> str:
    """Fold ``text`` to lowercase and drop diacritics (``é`` -> ``e``).

    Non-letters are left in place; callers decide what to keep.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_marks.casefold()


def _strip_prefixes(tokens: list[str]) -> list[str]:
    """Drop leading tokens that are known street-type words.

    Stops at the first non-type-word token. Runs left to right, so a bilingual
    ``Viale - Avenue`` prefix (two type words) is removed in full.
    """
    start = 0
    for token in tokens:
        folded = _fold_ascii(token)
        if folded in _PREFIXES:
            start += 1
        else:
            break
    return tokens[start:]


def normalize_name(name: str) -> str:
    """Return the grouping key for a raw street ``name``.

    Strips leading street-type words, then concatenates the rest as lowercase
    ASCII letters and digits (diacritics folded, separators dropped)::

        "Viale - Avenue Giuseppe Garibaldi" -> "giuseppegaribaldi"
        "Corso / Avenue Père-Laurent"       -> "perelaurent"

    If stripping leaves nothing (name was only a type word, e.g. ``"Via"``),
    falls back to the whole name so type-only names don't all collapse to one key.
    """
    # Split on anything that is not a letter or digit so "Viale - Avenue" and
    # "Corso/Avenue" tokenize the same way regardless of the separator used.
    tokens = _split_tokens(name)
    kept = _strip_prefixes(tokens)
    if not kept:
        # Name was nothing but type words; fall back to the whole name's letters
        # rather than collapsing every such name onto the empty key.
        kept = tokens
    return "".join(_letters_only(token) for token in kept)


def first_word(name: str) -> str | None:
    """Return the folded ASCII form of a name's first word, or ``None`` if none.

    Used to discover street-type words not yet in :data:`_PREFIXES`.
    """
    for token in _split_tokens(name):
        letters = _letters_only(token)
        if letters:
            return letters
    return None


def is_known_prefix(word: str) -> bool:
    """Return whether ``word`` (folded ASCII) is a known street-type prefix."""
    return word in _PREFIXES


def _split_tokens(name: str) -> list[str]:
    """Split ``name`` into tokens on any non-alphanumeric character.

    Runs of separators collapse, so empty tokens are never produced.
    """
    tokens: list[str] = []
    current: list[str] = []
    for ch in name:
        if ch.isalnum():
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _letters_only(token: str) -> str:
    """Return ``token`` folded to lowercase ASCII letters and digits.

    Digits are kept so ``Via 4 Novembre`` and ``Via Novembre`` stay distinct.
    """
    folded = _fold_ascii(token)
    return "".join(ch for ch in folded if "a" <= ch <= "z" or "0" <= ch <= "9")
