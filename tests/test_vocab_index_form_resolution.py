"""Focused tests for unique vocab-card ownership of ambiguous surface forms."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "vocab"))

import vocab_index  # noqa: E402


def _note(
    lemma: str,
    *,
    forms: list[str],
    mastery: float,
    label: str,
    freq_bnc: int,
) -> str:
    form_lines = "\n".join(f"  - {form}" for form in forms)
    return (
        "---\n"
        f"word: {lemma}\n"
        f"lemma: {lemma}\n"
        "forms:\n"
        f"{form_lines}\n"
        f"freq_bnc: {freq_bnc}\n"
        f"mastery: {mastery}\n"
        f"mastery_label: {label}\n"
        "---\n"
        f"# {lemma}\n"
    )


class VocabIndexFormResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bw-vocab-index-")
        self.vault = Path(self.temp.name)
        self.old_vault = vocab_index.VAULT_ROOT
        self.old_cache = dict(vocab_index._CACHE)
        vocab_index.VAULT_ROOT = self.vault
        vocab_index._CACHE.update(
            data=None,
            vroot_mtime=0.0,
            ts_loaded=0.0,
        )
        vocab_index._ecdict_lemma_for_form.cache_clear()

    def tearDown(self) -> None:
        vocab_index.VAULT_ROOT = self.old_vault
        vocab_index._CACHE.clear()
        vocab_index._CACHE.update(self.old_cache)
        vocab_index._ecdict_lemma_for_form.cache_clear()
        self.temp.cleanup()

    def _write(self, bucket: str, lemma: str, text: str) -> None:
        path = self.vault / "资源" / "vocab" / bucket / f"{lemma}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")

    def test_dictionary_lemma_owns_was_instead_of_last_loaded_wa_card(self) -> None:
        self._write(
            "b",
            "be",
            _note(
                "be",
                forms=["be", "been", "being", "was"],
                mastery=1.0,
                label="掌握",
                freq_bnc=2,
            ),
        )
        # ECDICT also records the obscure abbreviation/noun "wa" with plural
        # "was".  This file is traversed after b/be.md and used to overwrite
        # the common verb before collision resolution was added.
        self._write(
            "w",
            "wa",
            _note(
                "wa",
                forms=["wa", "was"],
                mastery=0.0,
                label="完全不会",
                freq_bnc=11044,
            ),
        )

        with patch.object(
            vocab_index,
            "_ecdict_lemma_for_form",
            side_effect=lambda form: "be" if form == "was" else form,
        ):
            idx = vocab_index.index(force_reload=True)

        self.assertEqual(idx["was"]["lemma"], "be")
        self.assertEqual(idx["was"]["label_slug"], "mastered")
        self.assertEqual(idx["was"]["mastery"], 1.0)
        self.assertEqual(idx["wa"]["lemma"], "wa")
        self.assertEqual(idx["wa"]["label_slug"], "new")
        unmastered = {
            form
            for form, info in idx.items()
            if info["label_slug"] != "mastered"
        }
        self.assertNotIn("was", unmastered)

    def test_no_dictionary_fallback_is_frequency_based_and_order_independent(self) -> None:
        be = {"lemma": "be", "freq_bnc": 2}
        wa = {"lemma": "wa", "freq_bnc": 11044}
        with patch.object(vocab_index, "_ecdict_lemma_for_form", return_value=""):
            self.assertIs(vocab_index._english_form_owner("was", [be, wa]), be)
            self.assertIs(vocab_index._english_form_owner("was", [wa, be]), be)

    def test_exact_lemma_wins_when_dictionary_has_no_answer(self) -> None:
        exact = {"lemma": "custom", "freq_bnc": 0}
        derived = {"lemma": "custome", "freq_bnc": 1}
        with patch.object(vocab_index, "_ecdict_lemma_for_form", return_value=""):
            self.assertIs(
                vocab_index._english_form_owner("custom", [derived, exact]),
                exact,
            )


if __name__ == "__main__":
    unittest.main()
