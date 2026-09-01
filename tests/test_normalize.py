"""Tests for :func:`strade.normalize.normalize_name`."""

from __future__ import annotations

import unittest

from strade.normalize import normalize_name


class NormalizeNameTest(unittest.TestCase):
    def test_bilingual_and_italian_forms_share_a_key(self) -> None:
        # The motivating case: a bilingual "Viale - Avenue ..." form and the
        # Italian-only form must collapse to the same grouping key.
        self.assertEqual(
            normalize_name("Viale - Avenue Giuseppe Garibaldi"),
            normalize_name("Viale Giuseppe Garibaldi"),
        )
        self.assertEqual(
            normalize_name("Viale - Avenue Giuseppe Garibaldi"),
            "giuseppegaribaldi",
        )

    def test_separator_variants_share_a_key(self) -> None:
        # Slash and hyphen separators between the two type words tokenize the same.
        self.assertEqual(
            normalize_name("Corso / Avenue Père-Laurent"),
            normalize_name("Corso - Avenue Père-Laurent"),
        )

    def test_diacritics_are_folded(self) -> None:
        self.assertEqual(normalize_name("Rue Père-Laurent"), "perelaurent")
        self.assertEqual(normalize_name("Place Émile Chanoux"), "emilechanoux")

    def test_type_word_without_prefix_still_matches_prefixed_form(self) -> None:
        # "Conseil des Commis" (no type word) vs "Viale - Avenue Conseil des Commis".
        self.assertEqual(
            normalize_name("Conseil des Commis"),
            normalize_name("Viale - Avenue Conseil des Commis"),
        )

    def test_digits_are_kept_and_punctuation_is_dropped(self) -> None:
        # Digits are kept in the key (so "Via 4 Novembre" is not merged with
        # "Via Novembre") while punctuation and separators are dropped. The
        # leading type word is stripped; only the *leading* type word is removed,
        # so a type word that begins the second (French) half of a full bilingual
        # translation is kept as part of the key. That is intentional: forms like
        # "Via Torino - Rue de Turin" name the street twice in different words and
        # are genuinely distinct keys, so they should not silently merge.
        self.assertEqual(
            normalize_name("Via 1° Maggio - Rue 1er Mai"),
            "1maggiorue1ermai",
        )

    def test_digits_distinguish_otherwise_equal_names(self) -> None:
        # Keeping digits means a numbered street does not collapse onto the
        # same key as the unnumbered form.
        self.assertNotEqual(
            normalize_name("Via 4 Novembre"),
            normalize_name("Via Novembre"),
        )

    def test_type_only_name_falls_back_to_full_letters(self) -> None:
        # A name that is nothing but a type word must not collapse to the empty
        # key (which would merge every such name); it keeps its own letters.
        self.assertEqual(normalize_name("Via"), "via")
        self.assertEqual(normalize_name("Piazza"), "piazza")
        self.assertNotEqual(normalize_name("Via"), normalize_name("Piazza"))

    def test_case_insensitive(self) -> None:
        self.assertEqual(
            normalize_name("VIALE giuseppe GARIBALDI"),
            normalize_name("Viale Giuseppe Garibaldi"),
        )

    def test_distinct_streets_keep_distinct_keys(self) -> None:
        # Normalization must not over-merge genuinely different streets.
        self.assertNotEqual(
            normalize_name("Via Roma"),
            normalize_name("Via Milano"),
        )


if __name__ == "__main__":
    unittest.main()
