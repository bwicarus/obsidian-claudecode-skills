"""跨端事件 fixture 的合同验证:**用真实服务代码重放**,逐字段比对。

为什么要这样而不是只写一份文档:fixture 一旦靠人手维护就会和实现漂移,
而跨端消费方(Windows 监听器)是照 fixture 写的 —— 漂移的代价由对面承担。
这里让 `FocusState` / `DrawingRevisions` / `DirectCommandService` 真的跑一遍,
产出与 fixture 不符就红。改实现不改 fixture,同样红。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
import reader_outgoing_context as OC   # noqa: E402
import reader_direct_commands as DC    # noqa: E402

FIX = ROOT / "reader-specs" / "fixtures" / "outgoing-events.jsonl"
DOC = ROOT / "reader-specs" / "fixtures" / "README.md"
BOOK = "资源/books/示例.pdf"


def _lines():
    return [json.loads(l) for l in FIX.read_text("utf-8").splitlines() if l.strip()]


def _by(t, **kw):
    for e in _lines():
        if e["type"] == t and all(e.get(k) == v for k, v in kw.items()):
            return e
    raise AssertionError(f"fixture 缺 {t} {kw}")


class FixtureShapeTest(unittest.TestCase):
    def test_is_valid_jsonl_and_versioned(self) -> None:
        evs = _lines()
        self.assertTrue(evs)
        for e in evs:
            self.assertEqual(e["v"], 1, "每行都要带版本号,消费方才能安全升级")
            self.assertIn("type", e)

    def test_covers_all_required_forms(self) -> None:
        types = {(e["type"], e.get("action") or e.get("state") or
                  ("silent" if e.get("emitsEvent") is False else "")) for e in _lines()}
        for need in [("page.context", ""), ("focus", "set"), ("focus", "replace"), ("focus", "cancel"),
                     ("drawing", "pending"), ("drawing", "stable"),
                     ("command", "silent"), ("command-failed", "")]:
            self.assertIn(need, types, f"fixture 缺形态 {need}")

    def test_contract_doc_lists_every_type(self) -> None:
        doc = DOC.read_text("utf-8")
        for t in {e["type"] for e in _lines()}:
            self.assertIn(t, doc, f"字段契约漏了 {t}")


class ReplayWithRealCodeTest(unittest.TestCase):
    """核心:真实实现跑一遍,输出必须与 fixture 对得上。"""

    def test_focus_set_replace_cancel(self) -> None:
        f = OC.FocusState()
        s1 = _by("focus", action="set")
        r1 = f.set(s1["kind"], s1["ref"], now=100.0)
        self.assertEqual(r1["seq"], s1["seq"])
        self.assertEqual(f.get(now=100.1)["focus"]["ref"], s1["ref"])

        s2 = _by("focus", action="replace")
        r2 = f.set(s2["kind"], s2["ref"], now=101.0)
        self.assertEqual(r2["seq"], s2["seq"], "替换必须让 seq 递增")
        self.assertEqual(f.get(now=101.1)["focus"]["kind"], "card")

        s3 = _by("focus", action="cancel")
        f.cancel(now=102.0)
        g = f.get(now=102.1)
        self.assertEqual(g["state"], "cancelled")
        self.assertIsNone(g["focus"], "取消后不得再给 focus")
        self.assertEqual(g["cancelledObject"], s3["cancelledObject"],
                         "被取消对象要与 fixture 逐字段一致")

    def test_drawing_pending_then_stable(self) -> None:
        dr = OC.DrawingRevisions(stable_s=1.0)
        pend = _by("drawing", state="pending")
        st0 = dr.observe(BOOK, 12, {"strokes": [[1, 2]]}, now=200.0)
        self.assertFalse(st0["stable"])
        self.assertIsNone(st0["drawingRevision"], "pending 不得给版本")
        self.assertIsNone(st0["ref"], "pending 不得给引用")
        self.assertEqual(st0["file"], pend["file"])

        stable = _by("drawing", state="stable")
        st1 = dr.observe(BOOK, 12, {"strokes": [[1, 2]]}, now=201.0)
        self.assertTrue(st1["stable"])
        self.assertRegex(st1["drawingRevision"], r"^dr_[0-9a-f]{16}$",
                         "版本号格式必须与 fixture 占位符 dr_<16hex> 一致")
        self.assertEqual(set(st1["ref"]), set(stable["ref"]), "引用字段集要一致")
        self.assertEqual(st1["ref"]["kind"], "drawing")

    def test_independent_success_is_silent(self) -> None:
        line = _by("command")
        self.assertFalse(line["emitsEvent"])
        bus = DC.FailureBus()
        svc = DC.DirectCommandService({"toc.get": lambda a, p, prev: {"toc": []}}, bus=bus)
        r = svc.submit({"correlation": line["correlation"], "mode": line["mode"],
                        "action": "toc.get", "anchor": {"file": "a.pdf"}})
        self.assertTrue(r["ok"])
        self.assertEqual(bus.since(0), [], "独立成功必须零事件 —— fixture 就是这么写的")

    def test_failure_event_matches_fixture_fields(self) -> None:
        fx = _by("command-failed")
        bus = DC.FailureBus()
        def boom(a, p, prev):
            raise RuntimeError("底层不可用")
        svc = DC.DirectCommandService({"toc.get": boom}, bus=bus)
        r = svc.submit({"correlation": fx["correlation"], "voiceTask": fx["taskId"],
                        "action": "toc.get", "anchor": {"file": "a.pdf"}})
        self.assertFalse(r["ok"])
        ev = bus.since(0)[0]
        self.assertEqual(ev["correlation"], fx["correlation"])
        self.assertEqual(ev["voiceTask"], fx["taskId"], "必须按语音任务路由")
        self.assertEqual(ev["step"], fx["step"])
        self.assertEqual(ev["retryable"], fx["retryable"], "运行时异常应标可重试")
        self.assertEqual(ev["error"], fx["error"], "错误文案要与 fixture 一致")
        self.assertRegex(ev["commandId"], r"^cmd_[0-9a-f]{12}$",
                         "commandId 格式须与 fixture 占位符 cmd_<12hex> 一致")

    def test_drawing_focus_ref_is_minimal(self) -> None:
        ref = _by("focus", kind="drawing")["ref"]
        self.assertEqual(set(ref), {"file", "page", "drawingRevision", "region"})
        for heavy in ("strokes", "image", "data"):
            self.assertNotIn(heavy, ref, f"绘图焦点引用不得携带 {heavy}")


class PageContextReplayTest(unittest.TestCase):
    """整页上下文这条也要真实现重放 —— 它是 Windows 唯一能拿到正文的通道。"""

    class _Pdf:
        def _safe_vault_path(self, rel):
            return Path("/vault") / rel

        def _page_text_clean(self, ap, rel, page, limit=None):
            return "这一页的完整文字层……"

        def _ink_load(self, rel):
            return {}

    def test_matches_fixture_field_by_field(self) -> None:
        fx = _by("page.context")
        self.assertEqual(fx.get("event"), fx["type"],
                         "type/event 必须同名:少一个就会被只认另一个的消费方 fail-closed 丢掉")
        self.assertIs(fx["stable"], True, "产生点已判定停留,发出来的就该是稳定态")
        ctx = OC.build_page_context(self._Pdf(), BOOK, 12)
        self.assertEqual(set(ctx), set(fx["page_context"]), "字段集要与 fixture 一致")
        self.assertEqual(ctx["text"], fx["page_context"]["text"])
        self.assertEqual(ctx["text_source"], fx["page_context"]["text_source"])
        self.assertEqual(ctx["reason"], "dwell")
        self.assertEqual(ctx["visual"]["page_image"], fx["page_context"]["visual"]["page_image"])

    def test_visual_carries_no_bytes(self) -> None:
        v = _by("page.context")["page_context"]["visual"]
        self.assertEqual(set(v), {"page_image", "has_ink"})
        self.assertTrue(str(v["page_image"]).startswith("/pdf/api/page-image"),
                        "视觉资源只能是引用 URL,不许内联字节")


class FixtureIsPublishedTest(unittest.TestCase):
    """fixture 必须随版本化规范库一起发布(带哈希),Windows 才能确定性拉取。"""

    def test_included_in_spec_manifest(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pub", ROOT / "scripts" / "publish_reader_specs.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        man = m.publish(dry=True)
        paths = {f["path"] for f in man["files"]}
        self.assertIn("fixtures/README.md", paths, "字段契约要进 manifest")

    def test_jsonl_fixture_is_in_manifest_with_hash(self) -> None:
        """fixture 本体必须带哈希进 manifest,消费方才能确定性拉取并校验。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pub2", ROOT / "scripts" / "publish_reader_specs.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        man = m.publish(dry=True)
        rec = [f for f in man["files"] if f["path"] == "fixtures/outgoing-events.jsonl"]
        self.assertTrue(rec, "fixture .jsonl 未进 manifest → Windows 无法确定性拉取")
        self.assertRegex(rec[0]["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
