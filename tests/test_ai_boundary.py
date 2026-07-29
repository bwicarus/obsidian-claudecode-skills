"""无 AI 直接命令通道的边界回归(2026-07-29 一次误判后补)。

背景:我曾把 `add_vocab` 与 `search_image` 从 `_AI_TOOL_NAMES` 移出,理由是
"逐行核对无 AI 调用"。实际三处都会调 AI:
  · assistant.py `_t_search_image` 落空时调 `_gemini_text` 规范化检索词
  · image_search.py `search_openai_img` 调 OpenAI Responses(gpt-4.1-mini)
  · add_vocab 的旧助手链经在线例句翻译落到 AI 后端

**根因是核对方法**:当时 grep 的是 `ask|_ai|claude|codex|prompt`,漏了 `gemini`
与 `openai`。用一组厂商关键词"证明没有 AI"是不成立的 —— 后端可以是任何供应商,
兜底分支还常藏在正常路径之后。本文件把结论与方法都固化成测试。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
import reader_direct_wire as W  # noqa: E402

# 已知会出现在 AI 调用点的供应商/入口标记。**这个清单本身不足以证明"无 AI"**,
# 它只用于"发现 AI"这一个方向:命中即说明有,未命中不代表没有。
AI_MARKERS = (
    "gemini", "openai", "anthropic", "claude", "codex", "gpt-",
    "ai_client", "_ai_call", "_ai_stream", "generativelanguage",
)


class BlacklistRegressionTest(unittest.TestCase):
    """这两个名字被误移除过一次;钉住,避免再犯。"""

    def test_search_image_stays_blacklisted(self) -> None:
        self.assertIn("search_image", W._AI_TOOL_NAMES,
                      "落空时会调 _gemini_text,且底层 search_openai_img 调 OpenAI")

    def test_add_vocab_stays_blacklisted(self) -> None:
        self.assertIn("add_vocab", W._AI_TOOL_NAMES,
                      "旧助手链经在线例句翻译可落到 AI 后端")

    # 审计 §1.1 当前认定会再次调用 AI 的全部工具。**逐个钉住**,不是只数个数 ——
    # 只断言 len>=23 的话,删掉一个危险项再补一个无害名字仍会全绿(Codex 12:39 指出)。
    KNOWN_AI_TOOLS = frozenset({
        "web_search", "search_image", "search_video", "make_paper", "summarize_section",
        "do_task", "run_saved_task", "see_page", "see_figure", "see_ink", "correct_dict",
        "material_graph", "read_material", "relate_material", "learning_focus",
        "situation_feedback", "make_diagnostic", "mastery_proposal", "apply_mastery",
        "error_patterns", "read_check_report", "add_vocab", "auto_highlight",
    })

    def test_no_known_ai_tool_is_ever_removed(self) -> None:
        """按名字比对集合。移除任何一项都必须先改这里,改这里就会被 review 看见。"""
        missing = self.KNOWN_AI_TOOLS - set(W._AI_TOOL_NAMES)
        self.assertEqual(missing, set(), f"这些会调 AI 的工具被移出了黑名单:{sorted(missing)}")

    def test_additions_are_allowed_but_visible(self) -> None:
        """允许新增(发现新的 AI 路径就该加),但新增项必须能被这里看见。"""
        extra = set(W._AI_TOOL_NAMES) - self.KNOWN_AI_TOOLS
        self.assertEqual(extra, set(),
                         f"黑名单新增了 {sorted(extra)};确认无误后把它们加进 KNOWN_AI_TOOLS")


class AssertNoAiSemanticsTest(unittest.TestCase):
    """固化 `_assert_no_ai` 到底拦什么 —— 我当初正是误解了它才去改黑名单。"""

    def test_it_blocks_bare_old_tool_names(self) -> None:
        with self.assertRaises(W.WiringError):
            W._assert_no_ai(["search_image"])

    def test_it_blocks_dotted_action_whose_tail_is_an_ai_tool(self) -> None:
        with self.assertRaises(W.WiringError):
            W._assert_no_ai(["img.search_image"])

    def test_new_action_names_were_never_blocked(self) -> None:
        """`vocab.add` 的 tail 是 `add`,从来不会被 `add_vocab` 拦住。

        当初为了让 vocab.add 能注册而去动黑名单,是**多余**的 —— 它本就不受影响。
        改一处安全表之前,先验证它到底拦不拦。
        """
        W._assert_no_ai(["vocab.add", "recall.creation", "section.read"])  # 不抛即通过


class DeterministicBaseHasNoAiMarkerTest(unittest.TestCase):
    """直接命令自己的两个模块必须干净 —— 它们是通道的执行面。

    注意这是**单向**判据:命中即失败,未命中不足以证明无 AI(见文件头)。
    要新增确定性底座时,仍必须读完整条函数体、特别是 fallback 分支。
    """

    def test_direct_command_modules_contain_no_ai_call(self) -> None:
        for name in ("reader_direct_commands.py", "reader_direct_wire.py"):
            src = (ROOT / "_server_deploy" / name).read_text("utf-8").lower()
            # 去掉注释行:本文件的说明性文字里就含这些词。
            body = "\n".join(ln for ln in src.splitlines()
                             if not ln.strip().startswith("#"))
            for marker in ("_gemini_text", "_openai_responses", "ai_client",
                           "_ai_call", "_ai_call_stream"):
                self.assertNotIn(marker, body, f"{name} 出现 AI 调用标记 {marker}")


if __name__ == "__main__":
    unittest.main()
