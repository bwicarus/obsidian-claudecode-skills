"""正文锚定标记的转义/反转义硬合同(Codex 2026-07-29 07:37 冻结)。

消费端(Windows typist、C# relay)必须与本文件的参考实现逐位一致:
单次左到右扫描,只反转 `\\\\`→`\\`、`\\⟦`→`⟦`、`\\⟧`→`⟧`;
未知序列原样保留;悬空反斜杠 fail closed。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
import reader_outgoing_context as OC  # noqa: E402

L, R = OC.MARK_L, OC.MARK_R
BS = "\\"


class RoundTripTest(unittest.TestCase):
    """escape → unescape 必须还原原文,一个字符都不能差。"""

    CASES = (
        "普通正文,没有任何特殊字符",
        f"正文里有 {L} 和 {R} 两个保留符号",
        f"单个反斜杠 {BS} 在正文里",
        f"反斜杠紧跟标记 {BS}{L} —— 最容易被二次反转义的串",
        f"双反斜杠 {BS}{BS}{L}",
        f"结尾就是反斜杠 {BS}",
        f"未知转义序列 {BS}n 应原样保留",
        f"{L}{L}{R}{R}{BS}{BS}",
        "",
        "多行\n正文\n带 " + L,
    )

    def test_round_trip_restores_source(self) -> None:
        for src in self.CASES:
            with self.subTest(src=src):
                self.assertEqual(OC.unescape_marks(OC._escape_marks(src)), src)

    def test_escaped_text_has_no_bare_marks(self) -> None:
        """转义后正文里不该再有裸标记字符,否则消费端会把它当边界。"""
        for src in self.CASES:
            esc = OC._escape_marks(src)
            i = 0
            while i < len(esc):
                if esc[i] == BS:
                    i += 2          # 跳过转义对
                    continue
                self.assertNotIn(esc[i], (L, R),
                                 f"转义后仍有裸标记:{src!r} -> {esc!r}")
                i += 1


class ChainedReplaceTest(unittest.TestCase):
    """为什么必须单次扫描 —— 实测澄清。

    Codex 07:37 的合同禁止"连续 replace 造成二次反转义"。实测发现:**因为转义时
    先处理反斜杠再处理标记,反转义用连续 replace 在 round-trip 上恰好等价**,两种
    顺序都能还原。所以禁令的理由不是"会还原错",而是下面这条:

        `replace` 无法 fail closed。

    悬空反斜杠、未知序列这些"不该出现的输入",`replace` 只会静默产出一个看似正常的
    字符串;而合同要求消费端遇到它们必须停下来。结论仍然是单次扫描,但理由要记准,
    否则将来有人发现 round-trip 等价就会以为这条合同可以放宽。
    """

    def test_chained_replace_happens_to_round_trip(self) -> None:
        for src in (f"{BS}{L}", f"{BS}{BS}{L}", f"{L}{BS}", f"{BS}n{L}"):
            with self.subTest(src=src):
                esc = OC._escape_marks(src)
                naive = esc.replace(BS + L, L).replace(BS + R, R).replace(BS + BS, BS)
                self.assertEqual(naive, src, "如果这里开始不等,合同理由要重写")
                self.assertEqual(OC.unescape_marks(esc), src)

    def test_only_single_pass_can_fail_closed(self) -> None:
        """这才是禁用 replace 的真正原因:坏输入必须炸,不能静默产出。"""
        bad = "正文结尾是裸反斜杠 " + BS
        silent = bad.replace(BS + L, L).replace(BS + R, R).replace(BS + BS, BS)
        self.assertEqual(silent, bad, "replace 对坏输入毫无反应")
        with self.assertRaises(OC.MarkEscapeError):
            OC.unescape_marks(bad)


class FailClosedTest(unittest.TestCase):
    def test_dangling_backslash_raises(self) -> None:
        with self.assertRaises(OC.MarkEscapeError):
            OC.unescape_marks("正文结尾是裸反斜杠 " + BS)

    def test_unknown_sequence_is_preserved_verbatim(self) -> None:
        self.assertEqual(OC.unescape_marks(f"{BS}n"), f"{BS}n")
        self.assertEqual(OC.unescape_marks(f"a{BS}tb"), f"a{BS}tb")

    def test_known_sequences_unescape_once_only(self) -> None:
        self.assertEqual(OC.unescape_marks(f"{BS}{BS}{L}"), f"{BS}{L}")
        self.assertEqual(OC.unescape_marks(f"{BS}{L}"), L)
        self.assertEqual(OC.unescape_marks(f"{BS}{R}"), R)


class AnnotatedPayloadTest(unittest.TestCase):
    """标注后的正文:标记本身不可被反转义吃掉,正文 token 必须能还原。"""

    def test_marks_survive_and_body_restores(self) -> None:
        src = f"前 {L} 中 {R} 后"          # 正文自带两个保留符号
        out, un = OC.annotate_page_text(src, [{"text": "中"}])
        self.assertEqual(un, [])
        self.assertIn(f"{L}HIGHLIGHT{R}中{L}/HIGHLIGHT{R}", out)
        # 剥掉我们插入的结构标记后,剩下的正文 token 反转义应还原原文
        body = (out.replace(f"{L}HIGHLIGHT{R}", "")
                   .replace(f"{L}/HIGHLIGHT{R}", ""))
        self.assertEqual(OC.unescape_marks(body), src)


if __name__ == "__main__":
    unittest.main()
