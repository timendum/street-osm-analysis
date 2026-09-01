"""Normalize street names to a grouping key that ignores type and language.

Valle d'Aosta is officially bilingual, so OSM carries the same street under
several surface forms that all name the *same* physical street:

- an Italian-only form: ``Viale Giuseppe Garibaldi``
- a bilingual form with both street types: ``Viale - Avenue Giuseppe Garibaldi``
- separator variants: ``Corso / Avenue Père-Laurent`` vs ``Corso - Avenue …``

Grouping on the raw name (the pipeline's original behavior) keeps these apart, so
the ways of one street never join. This module derives a *normalization key* that
collapses those variants onto one value by:

1. stripping the leading street-type word(s) (``Via``, ``Viale``, ``Piazza``,
   ``Rue``, ``Avenue`` …), repeatedly, so a bilingual ``Viale - Avenue`` prefix is
   removed in full; then
2. reducing the remainder to lowercase ASCII letters and digits — diacritics are
   folded (``é`` → ``e``), and every separator and space is dropped while digits
   are kept.

So ``Viale - Avenue Giuseppe Garibaldi`` and ``Viale Giuseppe Garibaldi`` both key
to ``giuseppegaribaldi``. The key is only ever used to *group*; the human-readable
name shown for a street is a representative raw name chosen elsewhere, so this lossy
key never reaches the output.
"""

from __future__ import annotations

import unicodedata

# Leading street-type words to strip, in Italian and French. Compared after
# case-folding and diacritic removal, so entries are lowercase ASCII. A word is
# only stripped when it appears at the start of the (remaining) name, and
# stripping repeats so bilingual prefixes like "Viale - Avenue" are removed whole.
_PREFIXES: frozenset[str] = frozenset(
    {
        # Italian
        "asse",
        "autostrada",
        "borgata",
        "cavalcavia",
        "ciclabile",
        "ciclopedonale",
        "ciclovia",
        "circonvallazione",
        "corso",
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
        "mulattiera",
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
        "rotatoria",
        "rotonda",
        "salita",
        "sentiero",
        "sp",
        "ss",
        "strada",
        "stradone",
        "svincolo",
        "tangenziale",
        "traversa",
        "variante",
        "via",
        "viale",
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
    """Fold ``text`` to lowercase and drop diacritics, keeping the letters.

    Decomposes accented characters (NFKD) and discards the combining marks, so
    ``é`` becomes ``e`` and ``ô`` becomes ``o``. Non-letters are left in place
    here; callers decide what to keep.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_marks.casefold()


def _strip_prefixes(tokens: list[str]) -> list[str]:
    """Drop leading tokens that are known street-type words.

    Each token is folded to ASCII before the membership test, so ``Viale``,
    ``viale`` and ``Località`` all match. Stripping stops at the first token that
    is not a type word, which is the start of the actual street name. This runs
    left to right so a bilingual ``Viale - Avenue`` prefix (two type words) is
    removed in full.
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

    Splits the name on whitespace and separator punctuation, strips leading
    street-type words (Italian and French), then concatenates the remaining
    tokens as lowercase ASCII letters and digits. Diacritics are folded and
    spaces and punctuation are dropped, while digits are kept, so bilingual and
    type-prefixed variants of one street collapse to the same value::

        "Viale - Avenue Giuseppe Garibaldi" -> "giuseppegaribaldi"
        "Viale Giuseppe Garibaldi"          -> "giuseppegaribaldi"
        "Corso / Avenue Père-Laurent"       -> "perelaurent"

    If stripping the type words would leave nothing (the name was only a type
    word, e.g. ``"Via"``), the key falls back to the ASCII letters of the whole
    original name so distinct type-only names are not all merged into one empty
    key.
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
    """Return the folded ASCII-letter form of a name's first word, or ``None``.

    Splits ``name`` the same way :func:`normalize_name` does and folds the first
    token to lowercase ASCII letters and digits (diacritics dropped). Returns
    ``None`` when the name has no first token with letters or digits. This is the
    candidate a caller compares against :data:`_PREFIXES` to discover street-type
    words that are not yet stripped.
    """
    for token in _split_tokens(name):
        letters = _letters_only(token)
        if letters:
            return letters
    return None


def is_known_prefix(word: str) -> bool:
    """Return whether ``word`` is already a known street-type prefix.

    ``word`` is expected to be the folded ASCII form produced by
    :func:`first_word`; the comparison against :data:`_PREFIXES` is exact.
    """
    return word in _PREFIXES


def _split_tokens(name: str) -> list[str]:
    """Split ``name`` into tokens on any non-alphanumeric character.

    Runs of separators (spaces, hyphens, slashes, dots) collapse so empty tokens
    are never produced. Letters and digits within a token are preserved; the
    prefix test folds each token to ASCII, and ``_letters_only`` keeps letters and
    digits when building the key.
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

    Diacritics are folded (``é`` → ``e``) and every other character (spaces,
    punctuation, separators) is dropped, but ASCII digits are kept: a house-number
    or highway code such as ``A5`` or ``1°`` contributes ``a5`` / ``1`` to the key
    so ``Via 4 Novembre`` and ``Via Novembre`` do not collapse together.
    """
    folded = _fold_ascii(token)
    return "".join(ch for ch in folded if "a" <= ch <= "z" or "0" <= ch <= "9")
