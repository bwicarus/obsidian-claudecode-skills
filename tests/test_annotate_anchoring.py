"""高亮锚进正文的合同。

背景：PDF 正文按视觉行插换行（`_page_text_clean` 每个视觉行末一个 `\n`），
而高亮文本来自字符层选择，天然是连续的。原实现把待查文本压成单行、却拿去在
保留换行的正文里 find，于是**只要高亮跨了行就一条都锚不上** —— 全部落进
unanchored，正文里看不到任何标记。

这不是理论推演：核实时实跑过 `'第一行文字\n第二行继续'` + `'文字 第二行'`，
两种写法都返回 whitespace_mismatch。

原代码是知道这件事的，注释写着「此时不做近似定位(会错位)」。那个顾虑是对的
——但空白无关匹配不必是近似的：把规范化后的位置映射回原文即可，一一对应。

另一件要紧的事：同一段文字在一页里出现两次时，原实现取第一处且不吭声，
于是标记落在错的地方而没有人知道。位置错了要能被看见。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "_server_deploy" / "reader_outgoing_context.py"


def _load():
    spec = importlib.util.spec_from_file_location("outgoing_anchor_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OC = _load() if MODULE_PATH.exists() else None


def hl(text, **extra):
    return dict({"text": text}, **extra)


@unittest.skipIf(OC is None, "模块不在此工作树")
class AnchorTest(unittest.TestCase):
    def test_highlight_spanning_a_line_break_is_anchored(self):
        # 这一条是整个改动的理由：PDF 里跨行的高亮此前 100% 丢失。
        body = "第一行文字\n第二行继续"
        out, un = OC.annotate_page_text(body, [hl("文字 第二行")])
        self.assertEqual(un, [], f"跨行高亮不该落进 unanchored：{un}")
        self.assertIn("⟦HIGHLIGHT⟧", out)
        self.assertIn("⟦/HIGHLIGHT⟧", out)

    def test_the_line_break_survives_inside_the_mark(self):
        # 正文只出现一次是这个模块的铁律。包裹跨行高亮时,原文的换行必须
        # 原样留在标记内部,不能被替换成空格 —— 那会悄悄改写用户的书。
        body = "第一行文字\n第二行继续"
        out, _ = OC.annotate_page_text(body, [hl("文字 第二行")])
        inner = out.split("⟦HIGHLIGHT⟧", 1)[1].split("⟦/HIGHLIGHT⟧", 1)[0]
        self.assertIn("\n", inner, "换行必须原样保留")
        self.assertEqual(
            OC.unescape_marks(out).replace("⟦HIGHLIGHT⟧", "").replace("⟦/HIGHLIGHT⟧", ""),
            body,
            "去掉标记后必须逐字等于原文",
        )

    def test_highlight_text_written_with_a_newline_also_anchors(self):
        # 用户的高亮文本可能带换行,也可能带空格,两种都要认。
        body = "第一行文字\n第二行继续"
        out, un = OC.annotate_page_text(body, [hl("文字\n第二行")])
        self.assertEqual(un, [])
        self.assertIn("⟦HIGHLIGHT⟧", out)

    def test_exact_match_still_works(self):
        body = "整段在同一行里"
        out, un = OC.annotate_page_text(body, [hl("同一行")])
        self.assertEqual(un, [])
        self.assertEqual(out, "整段在⟦HIGHLIGHT⟧同一行⟦/HIGHLIGHT⟧里")

    def test_multiple_spaces_and_tabs_collapse(self):
        body = "前面   \t  后面"
        out, un = OC.annotate_page_text(body, [hl("前面 后面")])
        self.assertEqual(un, [])
        self.assertIn("⟦HIGHLIGHT⟧", out)

    def test_collapsed_whitespace_does_not_shift_the_marks(self):
        """折叠会让压缩后的下标短于原文，标记必须落回原文的位置。

        这一条是位置映射的**唯一**真正考验：跨行那些用例里一个 `\\n` 压成一个
        空格，长度不变、下标恰好一一对应，即使拿压缩后的下标直接当原文下标
        也照样通过。只有连续空白被折叠时，两套下标才会错开。
        """
        body = "开头 甲   乙 结尾"          # 「甲」「乙」之间三个空格
        out, un = OC.annotate_page_text(body, [hl("甲 乙")])
        self.assertEqual(un, [])
        inner = out.split("⟦HIGHLIGHT⟧", 1)[1].split("⟦/HIGHLIGHT⟧", 1)[0]
        self.assertEqual(inner, "甲   乙",
                         "标记必须盖住原文那一段，含被折叠的空白")
        self.assertEqual(
            out.replace("⟦HIGHLIGHT⟧", "").replace("⟦/HIGHLIGHT⟧", ""),
            body,
            "去掉标记后必须逐字等于原文",
        )

    def test_mark_lands_after_a_collapsed_run_earlier_in_the_line(self):
        """折叠发生在命中**之前**时，起点必须相应后移。

        这一条钉住的是起点：前面每折叠掉一个空白，压缩下标就比原文下标少一，
        直接拿来用会让标记整体左移，盖住前一个词的尾巴。
        """
        body = "前面    有很多空格 目标词 结尾"
        out, un = OC.annotate_page_text(body, [hl("目标词")])
        self.assertEqual(un, [])
        inner = out.split("⟦HIGHLIGHT⟧", 1)[1].split("⟦/HIGHLIGHT⟧", 1)[0]
        self.assertEqual(inner, "目标词")
        self.assertEqual(
            out.replace("⟦HIGHLIGHT⟧", "").replace("⟦/HIGHLIGHT⟧", ""), body)

    def test_truly_absent_text_is_still_reported(self):
        # 修好空白问题不等于什么都能匹配上。找不到仍要如实回报,
        # 否则下一个真的锚定失败会被这次的宽松掩盖。
        body = "这一页没有那句话"
        _, un = OC.annotate_page_text(body, [hl("完全不同的内容")])
        self.assertEqual(len(un), 1)
        self.assertEqual(un[0]["_reason"], "not_found_in_page_text")

    def test_repeated_text_is_marked_ambiguous(self):
        # 同一句出现两次时取第一处 —— 可能标在错的地方。位置可能错要说出来,
        # 否则 AI 会把标记位置当成确定的事实去引用。
        body = "重复的句子。中间。重复的句子。"
        out, un = OC.annotate_page_text(body, [hl("重复的句子")])
        self.assertIn("⟦HIGHLIGHT", out, "仍然要标出来")
        self.assertTrue(
            any(h.get("_reason") == "ambiguous_position" for h in un)
            or 'ambiguous="1"' in out,
            "重复命中必须在某处留下痕迹",
        )

    def test_overlapping_highlights_keep_one(self):
        body = "一二三四五六"
        out, un = OC.annotate_page_text(body, [hl("二三四"), hl("三四五")])
        self.assertEqual(out.count("⟦HIGHLIGHT"), 1, "边界不能交叉")
        self.assertTrue(
            any(h["_reason"] == "overlaps_earlier_highlight" for h in un))

    def test_marks_in_the_page_itself_are_escaped(self):
        # 书上原本就印着 ⟦ 时,不转义就会被消费方当成标记。
        body = "书里写着 ⟦ 这个符号"
        out, _ = OC.annotate_page_text(body, [])
        self.assertIn("\\⟦", out)
        self.assertEqual(OC.unescape_marks(out), body)

    def test_color_and_note_survive(self):
        body = "第一行文字\n第二行继续"
        out, _ = OC.annotate_page_text(
            body, [hl("文字 第二行", color="#ffd54a", note="备注")])
        self.assertIn('color="#ffd54a"', out)
        self.assertIn('note="备注"', out)

    def test_empty_page_reports_every_highlight(self):
        _, un = OC.annotate_page_text("", [hl("甲"), hl("乙")])
        self.assertEqual([h["_reason"] for h in un], ["empty_text"] * 2)



class EmbedsIntegrationTest(unittest.TestCase):
    """`build_page_context` 连着真实 embeds 走一遍。

    核实发现这条合成路径**从未被测过**：既有测试的 pdf 替身根本没有高亮
    loader，所以那一批用例全跑在「embeds 静默为空」的分支上。于是「读 sidecar
    失败」与「这页没有标注」返回得一模一样，谁也没发现。
    """

    class _Pdf:
        def __init__(self, *, text="", highlights=None, notes=None,
                     hl_boom=False):
            self._text = text
            self._hl = highlights or []
            self._notes = notes or []
            self._hl_boom = hl_boom

        def _safe_vault_path(self, rel):
            return Path("/vault") / rel

        def _page_text_clean(self, ap, rel, page, limit=None):
            return self._text

        def _epub_section_paragraphs(self, rel, idx):
            return []

        def _ink_load(self, rel):
            return {}

        def _hl_load(self, rel):
            if self._hl_boom:
                raise RuntimeError("sidecar 坏了")
            return {"highlights": self._hl}

        def _notes_load(self, rel):
            return self._notes

    def test_real_highlight_reaches_the_page_text(self):
        pdf = self._Pdf(
            text="第一行文字\n第二行继续",
            highlights=[{"page": "12", "text": "文字 第二行", "color": "#ffd54a"}],
        )
        ctx = OC.build_page_context(pdf, "书/a.pdf", 12)
        self.assertIn("⟦HIGHLIGHT", ctx["text"], "跨行高亮要真的出现在正文里")
        self.assertEqual(ctx["embeds"]["highlights"], 1)
        self.assertEqual(ctx["embeds"]["unanchored"], [])

    def test_sidecar_failure_is_distinguishable_from_a_clean_page(self):
        clean = OC.build_page_context(self._Pdf(text="正文"), "书/a.pdf", 12)
        broken = OC.build_page_context(
            self._Pdf(text="正文", hl_boom=True), "书/a.pdf", 12)
        self.assertEqual(clean["embeds"]["highlights"], 0)
        self.assertNotIn("error", clean["embeds"], "干净的一页不该带错误")
        self.assertIn("error", broken["embeds"],
                      "读不出来必须出声,否则与'这页没标注'无从分辨")
        self.assertIn("sidecar 坏了", broken["embeds"]["error"])

    def test_body_survives_a_broken_sidecar(self):
        # 正文是主线,标注是增强。sidecar 坏了不该让用户连正文都拿不到。
        ctx = OC.build_page_context(
            self._Pdf(text="这一页的正文", hl_boom=True), "书/a.pdf", 12)
        self.assertEqual(ctx["text"], "这一页的正文")
        self.assertTrue(ctx["text_available"])

    def test_partial_failure_keeps_the_half_that_worked(self):
        """高亮读不出、便签正常 —— 这才是新增归因真正覆盖的场景。

        整个 _page_embeds 抛出时，build_page_context 外层的兜底本来就会接住并
        写 error。但两类 sidecar 是**分开读**的：高亮那半失败时旧代码只是把
        列表清空然后继续往下走，外层兜底压根不会触发，于是便签照常出现、
        高亮凭空消失、没有任何一处说得出为什么。
        """
        pdf = self._Pdf(
            text="正文",
            hl_boom=True,
            notes=[{"id": "n1", "anchor": {"page": 12}, "text": "便签内容"}],
        )
        ctx = OC.build_page_context(pdf, "书/a.pdf", 12)
        self.assertEqual(ctx["embeds"]["blocks"], 1, "能读到的那半要保住")
        self.assertIn("⟦CARD_START", ctx["text"])
        self.assertIn("highlights:", ctx["embeds"].get("error", ""),
                      "读不出的那半要指名道姓，不能只说'出错了'")

    def test_notes_become_trailing_blocks(self):
        pdf = self._Pdf(
            text="正文",
            notes=[{"id": "n1", "anchor": {"page": 12},
                    "card": {"cards": [{"front": "Q", "back": "A"}]}}],
        )
        ctx = OC.build_page_context(pdf, "书/a.pdf", 12)
        self.assertIn('⟦CARD_START type="anki"', ctx["text"])
        self.assertEqual(ctx["embeds"]["blocks"], 1)

if __name__ == "__main__":
    unittest.main()
