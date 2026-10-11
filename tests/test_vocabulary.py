from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from transcriber.vocabulary import MAX_PROMPT_LENGTH, VocabularyError, VocabularyStore


class VocabularyStoreTests(unittest.TestCase):
    def test_add_list_remove_are_persistent_and_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = VocabularyStore(directory)
            terms, changed = store.add("  CTranslate2  ")
            self.assertTrue(changed)
            self.assertEqual(terms, ["CTranslate2"])
            terms, changed = store.add("ctranslate2")
            self.assertFalse(changed)
            self.assertEqual(terms, ["CTranslate2"])
            self.assertEqual(VocabularyStore(directory).list(), ["CTranslate2"])
            terms, changed = store.remove("CTRANSLATE2")
            self.assertTrue(changed)
            self.assertEqual(terms, [])

    def test_initial_prompt_is_bounded_and_empty_when_dictionary_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = VocabularyStore(directory)
            self.assertIsNone(store.initial_prompt())
            for index in range(20):
                store.add(f"term-{index}-" + "x" * 80)
            prompt = store.initial_prompt()
            self.assertIsNotNone(prompt)
            self.assertLessEqual(len(prompt), MAX_PROMPT_LENGTH)

    def test_invalid_terms_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = VocabularyStore(Path(directory))
            for term in ("", "  ", "invalid\nterm", "x" * 129):
                with self.subTest(term=term[:12]), self.assertRaises(VocabularyError):
                    store.add(term)


if __name__ == "__main__":
    unittest.main()
