# -*- coding: utf-8 -*-
"""整理套件的契约：AI 只填 flow.json；参数按工具 schema 校验、引用只能指向更早的步骤、
run.js 由模板生成、用轨迹干跑通过才算编好。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill_kit"))

import bw_skill_build as K  # noqa: E402

TOOLS = {
    "reader_context_snapshot": {"name": "reader_context_snapshot", "inputSchema": {
        "type": "object", "properties": {"brief": {"type": "boolean"}}, "additionalProperties": False}},
    "reader_card": {"name": "reader_card", "inputSchema": {
        "type": "object", "required": ["card"], "additionalProperties": False,
        "properties": {"card": {"type": "object", "required": ["kind", "title", "data"], "additionalProperties": False,
                                "properties": {"kind": {"type": "string", "enum": ["general", "fact"]},
                                               "title": {"type": "string", "maxLength": 80},
                                               "data": {"type": "object"}, "bind": {"type": "object"}}}}}},
}


def flow(**over):
    base = {"contract": K.FLOW_CONTRACT, "name": "demo-card", "summary": "做一张卡", "trigger": ["做张卡"],
            "steps": [
                {"id": "snap", "tool": "reader_context_snapshot", "args": {"brief": True}},
                {"id": "card", "tool": "reader_card",
                 "args": {"card": {"kind": "general", "title": {"$from": "snap", "path": "selectedItems[0].text"}, "data": {"text": "x"}}}},
            ]}
    base.update(over)
    return base


class LintTests(unittest.TestCase):
    def test_valid_flow_has_no_errors(self):
        self.assertEqual(K.lint_flow(flow(), TOOLS), [])

    def test_unknown_tool_and_bad_args_are_named(self):
        errors = K.lint_flow(flow(steps=[
            {"id": "a", "tool": "reader_nonexistent", "args": {}},
            {"id": "b", "tool": "reader_card", "args": {"card": {"kind": "weather", "title": "t", "data": {}, "extra": 1}}},
            {"id": "c", "tool": "reader_context_snapshot", "args": {"brief": "yes"}},
        ]), TOOLS)
        joined = "\n".join(errors)
        self.assertIn("reader_nonexistent", joined)
        self.assertIn("enum", joined)
        self.assertIn("多出字段 extra", joined)
        self.assertIn("类型应为 boolean", joined)

    def test_refs_must_point_backwards_and_ai_steps_need_prompt(self):
        errors = K.lint_flow(flow(steps=[
            {"id": "card", "tool": "reader_card", "args": {"card": {"$from": "later"}}},
            {"id": "later", "needs_ai": True},
            {"id": "use", "tool": "reader_card", "args": {"card": {"$ai": "nope"}}},
        ]), TOOLS)
        joined = "\n".join(errors)
        self.assertIn("还没跑到的步骤 'later'", joined)
        self.assertIn("必须有 prompt", joined)
        self.assertIn("$ai 引用的 'nope'", joined)

    def test_name_and_trigger_rules(self):
        errors = K.lint_flow(flow(name="Bad Name", trigger=[]), TOOLS)
        self.assertTrue(any("kebab-case" in e for e in errors))
        self.assertTrue(any("trigger" in e for e in errors))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def _trace(self, steps):
        p = self.dir / "trace.json"
        p.write_text(json.dumps({"steps": steps}, ensure_ascii=False), encoding="utf-8")
        return p

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_build_generates_run_js_and_dryrun_replays_trace(self):
        skill = self.dir / "demo-card"
        skill.mkdir()
        (skill / "flow.json").write_text(json.dumps(flow()), encoding="utf-8")
        trace = self._trace([
            {"kind": "mcp", "tool": "reader_context_snapshot", "raw": {"content": [{"type": "text", "text": json.dumps({"selectedItems": [{"text": "日本脳炎"}]})}]}},
            {"kind": "mcp", "tool": "reader_card", "raw": {"content": [{"type": "text", "text": json.dumps({"ok": True, "kind": "card"})}]}},
        ])
        result = K.build(skill, trace=trace, refresh_tools=False, do_dryrun=True, tools=TOOLS)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "dryrun-passed")
        self.assertEqual(result["dryrun"]["calls"], ["reader_context_snapshot", "reader_card"])
        run_js = (skill / "run.js").read_text(encoding="utf-8")
        self.assertIn('"name": "demo-card"', run_js)
        self.assertNotIn("__BW_FLOW__", run_js)
        md = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: demo-card", md)
        self.assertIn("<!-- bw-flow:run.js:begin -->", md)
        self.assertIn(run_js.strip()[:60], md)
        # 再 build 一次:SKILL.md 只回填代码块,不覆盖文字
        (skill / "SKILL.md").write_text(md.replace("# demo-card", "# 我改过的标题"), encoding="utf-8")
        again = K.build(skill, trace=trace, refresh_tools=False, do_dryrun=True, tools=TOOLS)
        self.assertEqual(again["skillMd"], "refilled")
        self.assertIn("# 我改过的标题", (skill / "SKILL.md").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_dryrun_fails_when_a_tool_reports_error(self):
        skill = self.dir / "demo-card"
        skill.mkdir()
        (skill / "flow.json").write_text(json.dumps(flow()), encoding="utf-8")
        trace = self._trace([
            {"kind": "mcp", "tool": "reader_context_snapshot", "raw": {"content": [{"type": "text", "text": json.dumps({"selectedItems": [{"text": "x"}]})}]}},
            {"kind": "mcp", "tool": "reader_card", "raw": {"isError": True, "content": [{"type": "text", "text": json.dumps({"ok": False, "error": "no"})}]}},
        ])
        result = K.build(skill, trace=trace, refresh_tools=False, do_dryrun=True, tools=TOOLS)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "dryrun")
        self.assertFalse((skill / "run.js").exists(), "没干跑通过就不落 run.js")

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_needs_ai_step_hands_off_and_resumes(self):
        skill = self.dir / "demo-ai"
        skill.mkdir()
        f = flow(name="demo-ai", steps=[
            {"id": "snap", "tool": "reader_context_snapshot", "args": {"brief": True}},
            {"id": "research", "needs_ai": True, "prompt": "查资料出卡", "input": {"sel": {"$from": "snap", "path": "selectedItems"}}},
            {"id": "card", "tool": "reader_card", "args": {"card": {"$ai": "research"}}},
        ])
        (skill / "flow.json").write_text(json.dumps(f), encoding="utf-8")
        trace = self._trace([{"kind": "mcp", "tool": "reader_context_snapshot",
                              "raw": {"content": [{"type": "text", "text": json.dumps({"selectedItems": [{"text": "x"}]})}]}}])
        result = K.build(skill, trace=trace, refresh_tools=False, do_dryrun=True, tools=TOOLS)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["dryrun"]["calls"], ["reader_context_snapshot"])
        self.assertTrue(any("bwFlowHandoff" in line for line in result["dryrun"]["log"]))

    def test_lint_failure_stops_before_generating(self):
        skill = self.dir / "bad"
        skill.mkdir()
        (skill / "flow.json").write_text(json.dumps(flow(steps=[{"id": "a", "tool": "nope", "args": {}}])), encoding="utf-8")
        result = K.build(skill, trace=None, refresh_tools=False, do_dryrun=False, tools=TOOLS)
        self.assertEqual(result["stage"], "lint")
        self.assertFalse((skill / "run.js").exists())


if __name__ == "__main__":
    unittest.main()
