"""Contracts for the cached longest-first CJK matcher used by web-vocab."""

from __future__ import annotations

from pathlib import Path
import re
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import html_reader  # noqa: E402


def _legacy_matches(text: str, words: list[str]) -> list[tuple[int, int, str]]:
    """Reference the former nested-loop overlap policy exactly."""
    occupied: set[int] = set()
    accepted: list[tuple[int, int, str]] = []
    for word in words:
        for match in re.finditer(re.escape(word), text):
            if any(index in occupied for index in range(match.start(), match.end())):
                continue
            occupied.update(range(match.start(), match.end()))
            accepted.append((match.start(), match.end(), word))
    return accepted


class WebVocabCjkMatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_key = html_reader._WEB_CJK_MATCHER_KEY
        self.old_matcher = html_reader._WEB_CJK_MATCHER
        html_reader._WEB_CJK_MATCHER_KEY = None
        html_reader._WEB_CJK_MATCHER = None

    def tearDown(self) -> None:
        html_reader._WEB_CJK_MATCHER_KEY = self.old_key
        html_reader._WEB_CJK_MATCHER = self.old_matcher

    def test_matches_legacy_longest_first_nonoverlap_policy(self) -> None:
        # The first short match starts earlier, but the later three-character
        # form must still win because the production form list is longest-first.
        words = ["東京都", "京都", "東京", "京東", "都"]
        text = "京東京都と東京都京都"
        expected = _legacy_matches(text, words)

        actual = html_reader._web_cjk_matcher(words).find_nonoverlapping(text)

        self.assertEqual(actual, expected)
        self.assertIn((1, 4, "東京都"), actual)
        self.assertNotIn((0, 2, "京東"), actual)

    def test_repeated_occurrences_remain_nonoverlapping(self) -> None:
        words = ["没落する", "没落", "落する", "貿易"]
        text = "没落する没落していく貿易"

        actual = html_reader._web_cjk_matcher(words).find_nonoverlapping(text)

        self.assertEqual(actual, _legacy_matches(text, words))
        covered: set[int] = set()
        for start, end, _word in actual:
            self.assertFalse(covered.intersection(range(start, end)))
            covered.update(range(start, end))

    def test_rejected_match_still_advances_like_re_finditer(self) -> None:
        # "xxa" owns 0..3.  Legacy re.finditer("aa", "xxaaa") reports 2..4,
        # which is rejected, then advances to 4 and never reports 3..5.
        words = ["xxa", "aa"]
        text = "xxaaa"

        self.assertEqual(
            html_reader._web_cjk_matcher(words).find_nonoverlapping(text),
            _legacy_matches(text, words),
        )

    def test_matcher_is_reused_until_the_vocab_form_set_changes(self) -> None:
        first = html_reader._web_cjk_matcher(["東京都", "東京"])
        same = html_reader._web_cjk_matcher(["東京都", "東京"])
        changed = html_reader._web_cjk_matcher(["東京都", "東京", "京都"])

        self.assertIs(first, same)
        self.assertIsNot(first, changed)

    def test_warm_matcher_handles_eighty_sentences_with_wide_budget(self) -> None:
        # This intentionally leaves ample room for slow CI.  The former route
        # executed 800 regex searches for every sentence and exceeded one
        # second on the development host; the trie walks each sentence once.
        words = ["語" + chr(0x3400 + index) for index in range(800)]
        matcher = html_reader._web_cjk_matcher(words)
        texts = [
            "これは研究と学習についての長い日本語の文章で、認知科学の進歩を説明しています。"
            for _ in range(80)
        ]

        started = time.perf_counter()
        results = [matcher.find_nonoverlapping(text) for text in texts]
        elapsed = time.perf_counter() - started

        self.assertTrue(all(not result for result in results))
        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
