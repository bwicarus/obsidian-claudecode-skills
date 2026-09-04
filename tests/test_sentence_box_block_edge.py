"""句子框不许按**整页**右缘判断"这一行短不短"。

用户 2026-09-05 实锤：漫画气泡里「これらの問題を地域住民が自分たちで総合的に」
的句子框始终接不上下一行「そして平等に解決していこうってわけだ」。

根因不是块判定，也不是行距：那两行本来就同 `bk=2`，行距 1.47 倍字高也没到 1.5 的阈值。
真凶是 `_build_unmastered_sentences` 里的「短行=独立行」启发式 ——
它拿**整页**右缘量：那一页整页 right_edge=2205，而气泡每行只到 941~1060，
每一行都比页右缘短 56~80%，远超 30% 阈值，于是每一行都被判成独立短行而断句。
分栏、气泡、侧栏……只要正文不占满页宽，这条启发式就必然误伤。

修法：按**这一块（bk）自己的**右缘量。段末短行仍然照断（它比本块最宽行短），
而正常换行不再被冤枉。

夹具用的是那一页的真实几何（p000005 的 bk=2 两行）。
"""

from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
if sys.platform == "win32" and "fcntl" not in sys.modules:
    stub = types.ModuleType("fcntl")
    stub.LOCK_EX, stub.LOCK_SH, stub.LOCK_NB, stub.LOCK_UN = 1, 2, 4, 8
    stub.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = stub

import pdf_reader  # noqa: E402


# 那一页真实的几何（p000005，气泡 bk=2 的两行 + 页面右侧另一栏把 right_edge 拉到 2205）
LINE_A = "これらの問題を地域住民が自分たちで総合的に"
LINE_B = "そして平等に解決していこうってわけだ"
FAR_COLUMN = "右の段の文字列"


def _chars() -> list[dict]:
    out: list[dict] = []
    word_id = 0

    def emit(text: str, bk: int, x_start: float, y0: float, y1: float,
             step: float) -> None:
        nonlocal word_id
        x = x_start
        for ch in text:
            out.append({
                "c": ch, "bk": bk, "w": word_id,
                "x0": x, "y0": y0, "x1": x + step, "y1": y1, "sp": False,
            })
            x += step
            word_id += 1

    # 气泡第三行：从 60 排到 1060.4（本块最宽的一行）
    emit(LINE_A, 2, 60.0, 248.1, 289.1, (1060.4 - 60.0) / len(LINE_A))
    # 气泡第四行：行距 57.1，字高 38.7 → 1.47 倍，没到 1.5 的段落阈值
    emit(LINE_B, 2, 60.0, 305.2, 344.0, (908.2 - 60.0) / len(LINE_B))
    # 页面右侧另一栏：它把整页 right_edge 推到 2205，这正是误伤的来源
    emit(FAR_COLUMN, 9, 1900.0, 900.0, 940.0, (2205.6 - 1900.0) / len(FAR_COLUMN))
    return out


class SentenceBoxUsesBlockEdgeTest(unittest.TestCase):
    def setUp(self) -> None:
        module = types.ModuleType("vocab_index")
        # 气泡里的词都算"查过但没掌握"，好让这句够格成框。
        # 夹具里每个字符自成一个 w（分词 token），所以索引也按单字给 ——
        # 这条测试要验的是**行边界**，不是分词，别让分词细节把它挡在门外。
        module.index = lambda: {
            word: {"lemma": word, "label_slug": "new", "mastery": 0.1}
            for word in list("問題地域住民自分総合平等解決本文見出")
        }
        self._saved = sys.modules.get("vocab_index")
        sys.modules["vocab_index"] = module
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            sys.modules.pop("vocab_index", None)
        else:
            sys.modules["vocab_index"] = self._saved

    def test_bubble_lines_stay_in_one_sentence(self) -> None:
        sentences = pdf_reader._build_unmastered_sentences(
            _chars(), threshold=1, min_words=1)
        joined = [s.get("text", "").replace("\n", "").replace(" ", "")
                  for s in sentences]
        hit = [t for t in joined if "これらの問題" in t]
        self.assertTrue(hit, "没找到气泡那一句，实际句子：" + repr(joined))
        self.assertIn(
            "そして平等に解決していこうってわけだ", hit[0],
            "句子框必须接上下一行；按整页右缘判定时它会停在「総合的に」")

    def test_a_short_line_inside_the_same_block_still_breaks(self) -> None:
        # 不能矫枉过正：本块内明显没顶到**本块**右缘的行（段末/居中）仍要断。
        chars: list[dict] = []
        word_id = 0

        def emit(text, bk, x_start, y0, y1, step):
            nonlocal word_id
            x = x_start
            for ch in text:
                chars.append({"c": ch, "bk": bk, "w": word_id,
                              "x0": x, "y0": y0, "x1": x + step, "y1": y1,
                              "sp": False})
                x += step
                word_id += 1

        # 第一行只到 300，本块最宽行到 1000 → 短了 70% → 断
        emit("みじかい見出し", 4, 60.0, 100.0, 140.0, (300.0 - 60.0) / 7)
        emit("ここから本文が始まって右まで届く一行です", 4, 60.0, 158.0, 198.0,
             (1000.0 - 60.0) / 20)
        sentences = pdf_reader._build_unmastered_sentences(
            chars, threshold=0, min_words=0)
        texts = [s.get("text", "").replace("\n", "").replace(" ", "")
                 for s in sentences]
        self.assertFalse(
            any("みじかい見出し" in t and "ここから本文" in t for t in texts),
            "本块内的短行仍要断句，实际：" + repr(texts))


if __name__ == "__main__":
    unittest.main()
