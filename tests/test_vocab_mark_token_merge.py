"""生词下划线不许画在更长的、用户已认识的词的中间。

用户 2026-09-05 实锤：`②食糧確保と適切な栄養` 里，「栄養」明明已掌握（查过 9 次、
mastery 100），却在「養」下面单独画了一条下划线。

根因不是掌握度判错，而是**下划线完全信任分词边界**：这一页的分词把「栄養」切成了
栄|養，而「養」恰好是 2026-05-31 单查过一次的独立词条（state/jp-vocab.json 里
養 looks=1、栄養 looks=9）。于是「栄」查不到→不画，「養」查得到→画一条。

修法不去追分词：**单个汉字的 token 先试着并进后一个 token 再查生词库**，
合起来能查到就用长的那条。这样 栄+養 → 栄養 → mastered → 客户端过滤掉，不画。

⚠ 同一条规则在 App 本地那一遍（native-local-runtime.localVocabMarks）也有一份，
  由 tests/reader_contract/sentence-and-vocab-marks.contract.test.mjs 钉住 ——
  两边都画下划线，只改一处等于没改。
"""

from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
if sys.platform == "win32" and "fcntl" not in sys.modules:
    fcntl_stub = types.ModuleType("fcntl")
    fcntl_stub.LOCK_EX = 1
    fcntl_stub.LOCK_SH = 2
    fcntl_stub.LOCK_NB = 4
    fcntl_stub.LOCK_UN = 8
    fcntl_stub.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = fcntl_stub

import pdf_reader  # noqa: E402


def chars_for(text: str, token_sizes: list[int]) -> list[dict]:
    """按 token_sizes 给每个字符分配 w（分词 id），模拟不同的切法。"""
    out: list[dict] = []
    x = 0.0
    for word_id, size in enumerate(token_sizes):
        for _ in range(size):
            out.append({
                "c": text[len(out)], "w": word_id,
                "x0": x, "y0": 0.0, "x1": x + 10.0, "y1": 20.0, "sp": False,
            })
            x += 10.0
    return out


class VocabMarkTokenMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._idx = pdf_reader._vocab_idx
        self._inflect = pdf_reader._jp_inflection
        pdf_reader._jp_inflection = lambda value: {}
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        pdf_reader._vocab_idx = self._idx
        pdf_reader._jp_inflection = self._inflect

    def _use(self, index: dict) -> None:
        pdf_reader._vocab_idx = lambda: index

    def test_split_kanji_inside_a_mastered_word_is_not_marked(self) -> None:
        # 用户那一页的真实情况：栄養 已掌握，養 单查过一次。
        self._use({
            "栄養": {"lemma": "栄養", "label_slug": "mastered", "mastery": 1.0},
            "養": {"lemma": "養", "label_slug": "new", "mastery": 0.1},
        })
        marks = pdf_reader._build_jp_vocab_marks(chars_for("栄養", [1, 1]))
        self.assertEqual([m["word"] for m in marks], ["栄養"])
        self.assertEqual(marks[0]["label_slug"], "mastered")
        self.assertFalse(
            any(m["word"] == "養" for m in marks),
            "「養」不该在已掌握的「栄養」里单独画线")

    def test_whole_token_path_is_unchanged(self) -> None:
        self._use({
            "栄養": {"lemma": "栄養", "label_slug": "mastered", "mastery": 1.0},
        })
        marks = pdf_reader._build_jp_vocab_marks(chars_for("栄養", [2]))
        self.assertEqual([m["word"] for m in marks], ["栄養"])

    def test_single_kanji_still_marked_when_the_merge_is_unknown(self) -> None:
        # 不能误伤：合起来查不到就还是那个单字词。
        self._use({"養": {"lemma": "養", "label_slug": "new", "mastery": 0.1}})
        marks = pdf_reader._build_jp_vocab_marks(chars_for("養う", [1, 1]))
        self.assertEqual([m["word"] for m in marks], ["養"])

    def test_merged_word_keeps_its_own_rects(self) -> None:
        # 合并后画的是**两个字**的框，不是原来那一个字的。
        self._use({
            "栄養": {"lemma": "栄養", "label_slug": "new", "mastery": 0.2},
        })
        marks = pdf_reader._build_jp_vocab_marks(chars_for("栄養", [1, 1]))
        self.assertEqual(len(marks), 1)
        rect = marks[0]["rects"][0]
        self.assertEqual(rect[0], 0.0, "左边界应是「栄」的左边")
        self.assertEqual(rect[2], 20.0, "右边界应是「養」的右边")


if __name__ == "__main__":
    unittest.main()
