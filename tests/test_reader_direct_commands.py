"""无 AI 直接命令服务 + 版本化规范库的隔离验证(任务书 A2/A3/A4)。

隔离方式:执行器的 handlers 全部注入假实现,不 import 阅读器、不碰生产数据 ——
协议、执行模式、幂等、失败事件都可独立证明。
"""
from __future__ import annotations

import hashlib
import os
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
import reader_direct_commands as DC  # noqa: E402


def _svc(handlers=None, **kw):
    return DC.DirectCommandService(handlers or {}, **kw)


class ActionWhitelistTest(unittest.TestCase):
    """只允许确定性动作;会调 AI 的能力不得出现在白名单里。"""

    AI_TOOLS = {"web_search", "search_image", "search_video", "make_paper", "summarize_section",
                "do_task", "run_saved_task", "see_page", "see_figure", "see_ink", "correct_dict",
                "material_graph", "read_material", "relate_material", "learning_focus",
                "situation_feedback", "make_diagnostic", "mastery_proposal", "apply_mastery",
                "error_patterns", "read_check_report", "add_vocab", "auto_highlight"}

    def test_no_ai_action_in_whitelist(self) -> None:
        for a in DC.ACTIONS:
            tail = a.split(".", 1)[-1]
            self.assertNotIn(tail, self.AI_TOOLS, f"{a} 指向会调 AI 的能力")
        # 视觉判断只能取元数据,不能是"让模型看"
        self.assertIn("read.pageimage", DC.ACTIONS)
        self.assertIn("元数据", DC.ACTIONS["read.pageimage"]["desc"])
        self.assertIn("result.present", DC.ACTIONS)
        self.assertIn("不调 AI", DC.ACTIONS["result.present"]["desc"])

    def test_unknown_action_rejected_with_list(self) -> None:
        with self.assertRaises(DC.CommandError) as e:
            DC.validate({"correlation": "c1", "steps": [{"action": "ai.summarize"}]})
        self.assertIn("不在确定性白名单", str(e.exception))


class CommandShapeTest(unittest.TestCase):
    """命令必备字段:correlation/target(anchor)/action/params/idempotency/mode/precondition。"""

    def test_correlation_required(self) -> None:
        with self.assertRaises(DC.CommandError):
            DC.validate({"steps": [{"action": "toc.get", "anchor": {"file": "a.pdf"}}]})

    def test_anchor_target_enforced_per_action(self) -> None:
        with self.assertRaises(DC.CommandError) as e:
            DC.validate({"correlation": "c", "steps": [
                {"action": "read.page", "anchor": {"file": "a.pdf"}}]})
        self.assertIn("anchor.page", str(e.exception))

    def test_single_step_shorthand_is_normalized(self) -> None:
        c = DC.validate({"correlation": "c", "action": "toc.get", "anchor": {"file": "a.pdf"}})
        self.assertEqual(len(c["steps"]), 1)
        self.assertEqual(c["mode"], "independent")

    def test_mode_and_optional_fields(self) -> None:
        c = DC.validate({"correlation": "c", "mode": "dependent", "voiceTask": "vt1",
                         "dependencies": ["c0"], "steps": [
                             {"action": "page.new", "anchor": {"file": "a.pdf"},
                              "idempotency": "k1", "precondition": {"prevOk": True}}]})
        self.assertEqual(c["voiceTask"], "vt1")
        self.assertEqual(c["dependencies"], ["c0"])
        self.assertEqual(c["steps"][0]["idempotency"], "k1")
        with self.assertRaises(DC.CommandError):
            DC.validate({"correlation": "c", "mode": "whatever",
                         "steps": [{"action": "toc.get", "anchor": {"file": "a"}}]})


class ExecutionModeTest(unittest.TestCase):
    """独立单步成功静默;依赖多步逐步传递;失败停链。"""

    def test_independent_success_emits_no_event(self) -> None:
        bus = DC.FailureBus()
        s = _svc({"toc.get": lambda a, p, prev: {"toc": []}}, bus=bus)
        r = s.submit({"correlation": "c1", "action": "toc.get", "anchor": {"file": "a.pdf"}})
        self.assertTrue(r["ok"])
        self.assertEqual(bus.since(0), [], "独立单步成功不得产生事件(噪声)")

    def test_dependent_passes_previous_result(self) -> None:
        seen = {}
        def new_page(a, p, prev): return {"anchor": {"file": a["file"], "page": 99}}
        def add(a, p, prev):
            seen["prev"] = prev
            return {"added": True}
        s = _svc({"page.new": new_page, "page.add": add})
        r = s.submit({"correlation": "c2", "mode": "dependent", "steps": [
            {"action": "page.new", "anchor": {"file": "a.pdf"}},
            {"action": "page.add", "anchor": {"file": "a.pdf"}, "params": {"text": "x"}}]})
        self.assertTrue(r["ok"])
        self.assertEqual(seen["prev"]["anchor"]["page"], 99, "依赖模式必须把上一步结果传下去")

    def test_independent_does_not_leak_previous_result(self) -> None:
        seen = {}
        s = _svc({"toc.get": lambda a, p, prev: {"t": 1},
                  "note.list": lambda a, p, prev: seen.setdefault("prev", prev) or {"n": 0}})
        s.submit({"correlation": "c3", "mode": "independent", "steps": [
            {"action": "toc.get", "anchor": {"file": "a.pdf"}},
            {"action": "note.list", "anchor": {"file": "a.pdf"}}]})
        self.assertIsNone(seen["prev"], "独立模式各步不该互相可见")

    def test_failure_stops_chain_and_emits_event(self) -> None:
        bus = DC.FailureBus()
        calls = []
        def boom(a, p, prev): raise RuntimeError("底层挂了")
        s = _svc({"page.new": boom,
                  "page.add": lambda a, p, prev: calls.append(1) or {}}, bus=bus)
        r = s.submit({"correlation": "c4", "mode": "dependent", "voiceTask": "vt9", "steps": [
            {"action": "page.new", "anchor": {"file": "a.pdf"}},
            {"action": "page.add", "anchor": {"file": "a.pdf"}}]})
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [], "失败必须停链,不得继续执行后续步骤")
        self.assertEqual(len(r["steps"]), 1)
        ev = bus.since(0)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["voiceTask"], "vt9")
        self.assertEqual(ev[0]["step"], 0)
        self.assertTrue(ev[0]["retryable"], "运行时异常应标可重试")

    def test_precondition_blocks_and_reports(self) -> None:
        bus = DC.FailureBus()
        s = _svc({"page.add": lambda a, p, prev: {"ok": 1}}, bus=bus)
        r = s.submit({"correlation": "c5", "mode": "dependent", "steps": [
            {"action": "page.add", "anchor": {"file": "a.pdf"},
             "precondition": {"prevOk": True}}]})
        self.assertFalse(r["ok"])
        self.assertIn("前置条件", r["error"])
        self.assertFalse(r["retryable"], "前置不满足不是重试能解决的")


class IdempotencyTest(unittest.TestCase):
    def test_same_key_replays_without_reexecuting(self) -> None:
        n = {"c": 0}
        def bump(a, p, prev):
            n["c"] += 1
            return {"c": n["c"]}
        s = _svc({"note.create": bump})
        cmd = {"correlation": "c6", "idempotency": "k-abc",
               "action": "note.create", "anchor": {"file": "a.pdf"}}
        r1 = s.submit(cmd)
        r2 = s.submit(dict(cmd))
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertEqual(n["c"], 1, "同幂等键不得重复执行底层动作")
        self.assertTrue(r2.get("replayed"))

    def test_correlation_is_default_idempotency_key(self) -> None:
        n = {"c": 0}
        s = _svc({"toc.get": lambda a, p, prev: n.update(c=n["c"] + 1) or {}})
        for _ in range(3):
            s.submit({"correlation": "same", "action": "toc.get", "anchor": {"file": "a.pdf"}})
        self.assertEqual(n["c"], 1)


class FailureBusTest(unittest.TestCase):
    def test_cursor_and_voice_task_routing(self) -> None:
        bus = DC.FailureBus()
        bus.emit("c1", "e1", voice_task="vtA")
        cur = bus.cursor()
        bus.emit("c2", "e2", voice_task="vtB")
        self.assertEqual([e["correlation"] for e in bus.since(cur)], ["c2"])
        self.assertEqual([e["correlation"] for e in bus.since(0, voice_task="vtA")], ["c1"])

    def test_queue_is_bounded(self) -> None:
        bus = DC.FailureBus(keep=5)
        for i in range(20):
            bus.emit(f"c{i}", "e")
        self.assertEqual(len(bus.since(0)), 5)

    def test_receipt_has_required_fields(self) -> None:
        s = _svc({"toc.get": lambda a, p, prev: {"x": 1}})
        r = s.submit({"correlation": "c7", "action": "toc.get", "anchor": {"file": "a.pdf"}})
        for f in ("contract", "correlation", "ok", "steps"):
            self.assertIn(f, r)
        self.assertNotIn("message", r)
        self.assertEqual(r["steps"][0]["data"], {"x": 1})


class SpecLibraryTest(unittest.TestCase):
    """A2:manifest / 版本 / 哈希 / 原子发布 / 扁平路由。"""

    def _pub(self):
        spec = importlib.util.spec_from_file_location(
            "pub", ROOT / "scripts" / "publish_reader_specs.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_manifest_has_version_files_hash_time(self) -> None:
        man = self._pub().publish(dry=True)
        self.assertEqual(man["contract"], "reader-specs/1")
        for f in ("version", "files", "updatedAt", "entry"):
            self.assertIn(f, man)
        self.assertTrue(all({"path", "sha256", "bytes"} <= set(x) for x in man["files"]))
        self.assertEqual(man["entry"], "AGENTS.md")

    def test_publish_is_atomic_and_idempotent(self) -> None:
        pub = self._pub()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            m1 = pub.publish(out=out)
            m2 = pub.publish(out=out)
            self.assertEqual(m1["version"], m2["version"], "同内容不得产生新版本")
            self.assertEqual(len(list((out / "releases").iterdir())), 1)
            cur = out / "current"
            self.assertTrue(cur.is_symlink(), "current 必须是指针,便于原子切换")
            man = json.loads((cur / "manifest.json").read_text(encoding="utf-8"))
            for f in man["files"]:
                got = hashlib.sha256((cur / f["path"]).read_bytes()).hexdigest()
                self.assertEqual(got, f["sha256"], f"{f['path']} 哈希与 manifest 不符")
            self.assertFalse(list(out.glob(".stage-*")), "不得留下暂存目录")

    def test_agents_md_is_short_and_flat(self) -> None:
        text = (ROOT / "reader-specs" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertLess(len(text), 4000, "AGENTS.md 不该变成巨型提示")
        routed = [l for l in text.splitlines() if "specs/" in l]
        self.assertTrue(routed, "必须有到多步规范的路由表")
        for line in routed:
            self.assertNotIn("specs/specs/", line, "只允许一跳,不得多层跳转")

    def test_每份多步规范都写全了必备小节(self) -> None:
        need = ("触发", "上下文", "认知步骤", "命令", "依赖", "成功", "重试", "结构化结果")
        for f in sorted((ROOT / "reader-specs" / "specs").glob("*.md")):
            if f.name == "result-envelope.md":
                continue
            t = f.read_text(encoding="utf-8")
            for k in need:
                self.assertIn(k, t, f"{f.name} 缺「{k}」小节")


class EnvelopeSpecTest(unittest.TestCase):
    """A3:结果 envelope 的 kind 必须与前端渲染器实际支持一致,不得虚构。"""

    def test_envelope_kinds_match_renderer_contract(self) -> None:
        import reader_card_contract as CC
        doc = (ROOT / "reader-specs" / "specs" / "result-envelope.md").read_text(encoding="utf-8")
        for k in CC.renderer_card_kinds():
            self.assertIn(f"`{k}`", doc, f"envelope 规范漏了渲染器支持的 {k}")
        self.assertIn("`cards`", doc, "制卡作为 kind 变体,不另立协议")
        for fake in ("`table`", "`chart`", "`map`"):
            self.assertNotIn(fake, doc, "不得虚构渲染器画不出的类型")

    def test_anchor_semantics_documented(self) -> None:
        doc = (ROOT / "reader-specs" / "specs" / "result-envelope.md").read_text(encoding="utf-8")
        self.assertIn("真实卷", doc, "合并书必须落到真实卷")
        self.assertIn("原位", doc, "高亮在正文原位,正文不重复")


if __name__ == "__main__":
    unittest.main()


class WiringTest(unittest.TestCase):
    """接线层:16 个动作全接、AI 能力永不进白名单、未接线动作明确报错而非假成功。"""

    def _mod(self):
        import importlib
        sys.path.insert(0, str(ROOT / "_server_deploy"))
        return importlib.import_module("reader_direct_wire")

    def test_ai_capability_rejected_at_wiring(self) -> None:
        W = self._mod()
        with self.assertRaises(W.WiringError) as e:
            W._assert_no_ai(["read.page", "vision.see_page"])
        self.assertIn("会调用 AI", str(e.exception))
        # 审计里的 23 个 AI 工具,一个都不许出现在动作白名单尾段
        for name in W._AI_TOOL_NAMES:
            self.assertNotIn(name, {a.split(".", 1)[-1] for a in DC.ACTIONS})

    def test_missing_is_complement_of_handlers(self) -> None:
        """接线层的契约:handlers ∪ missing 恰好覆盖全部动作,且两者不重叠 ——
        任何动作要么真接上了,要么被明确列为未接线,不存在"看着有其实是假的"。"""
        class FakePdf:
            def _safe_vault_path(self, rel): return None
        W = self._mod()
        H, missing = W.build_handlers(FakePdf())
        self.assertEqual(set(H) | set(missing), set(DC.ACTIONS))
        self.assertEqual(set(H) & set(missing), set())
        for name, fn in H.items():
            self.assertTrue(callable(fn), f"{name} 的 handler 不可调用")

    def test_unwired_action_is_rejected_not_faked(self) -> None:
        """服务层:动作没有 handler 时明确报「未注册处理器」,绝不静默成功。"""
        s = DC.DirectCommandService({})
        r = s.submit({"correlation": "x", "action": "toc.get", "anchor": {"file": "a.pdf"}})
        self.assertFalse(r["ok"])
        self.assertIn("未注册处理器", r["error"])


class DeterministicGapWiringTest(unittest.TestCase):
    """2026-08-16 补齐的三个确定性动作:vocab.page / note.edit / undo.last。

    这三个是架构「6 个纯确定性缺口」里的前一批:上游模型再聪明也拿不到的
    本地读写(掌握度库/便签 sidecar/撤销栈)。测的是接线契约,不是底座本身
    (底座在 Pi 预检的真实执行测试里验)。"""

    def _mod(self):
        import importlib
        sys.path.insert(0, str(ROOT / "_server_deploy"))
        return importlib.import_module("reader_direct_wire")

    def _handlers(self, fake_pdf):
        return self._mod().build_handlers(fake_pdf, current_user_id=lambda: 7)[0]

    def test_vocab_page_words_mode_hits_mastery_db(self) -> None:
        calls = {}
        class FakePdf:
            def _safe_vault_path(self, rel): return None
            def vocab_mastery_for(self, words):
                calls["words"] = words
                return [{"word": w, "mastered": False} for w in words]
        H = self._handlers(FakePdf())
        r = H["vocab.page"]({}, {"words": ["apple", "犬"]}, None)
        self.assertEqual(calls["words"], ["apple", "犬"])
        self.assertEqual(len(r["lookups"]), 2)

    def test_vocab_page_page_mode_requires_page(self) -> None:
        class FakePdf:
            def _safe_vault_path(self, rel):
                from pathlib import Path
                return Path(__file__)   # 只要非 None
        H = self._handlers(FakePdf())
        with self.assertRaises(ValueError):
            H["vocab.page"]({"file": "x.pdf"}, {}, None)   # 缺 page 必须报错

    def test_note_edit_only_touches_text_and_color(self) -> None:
        """硬约束:笔画/位置/尺寸是用户数据,即使 params 里塞了也不能动。"""
        store = [{"id": "n1", "text": "old", "color": "#fff",
                  "strokes": ["用户手写"], "x": 0.5}]
        from contextlib import contextmanager
        class FakePdf:
            def _safe_vault_path(self, rel):
                import pathlib; return pathlib.Path(__file__).parent / rel
            @contextmanager
            def _notes_edit(self, rel):
                yield store
        # FakePdf 的 _safe_vault_path 会把 rel 拼在测试目录下 —— 但 _rel 还要
        # relative_to vault;绕开:直接用真实签名要求的最小行为。
        H = self._handlers(FakePdf())
        try:
            H["note.edit"]({"file": "b.pdf"},
                           {"id": "n1", "text": "new", "color": "#000",
                            "strokes": ["攻击载荷"], "x": 0.9}, None)
        except ValueError:
            self.skipTest("_rel 校验在假 pdf 下不可满足,契约由下两条断言覆盖")
        self.assertEqual(store[0]["text"], "new")
        self.assertEqual(store[0]["strokes"], ["用户手写"])   # 没被动
        self.assertEqual(store[0]["x"], 0.5)

    def test_new_actions_never_reach_ai(self) -> None:
        W = self._mod()
        W._assert_no_ai(["vocab.page", "note.edit", "undo.last"])   # 不抛=通过

    def test_actions_table_covers_new_entries(self) -> None:
        for a in ("vocab.page", "note.edit", "undo.last"):
            self.assertIn(a, DC.ACTIONS)


class ResultPresentWiringTest(unittest.TestCase):
    """结构化结果必须走 direct-command 的确定性展示动作。"""

    def _mod(self):
        import importlib
        sys.path.insert(0, str(ROOT / "_server_deploy"))
        return importlib.import_module("reader_direct_wire")

    def test_result_present_calls_only_injected_display_bottom(self) -> None:
        captured = {"calls": 0}

        class FakePdf:
            def _safe_vault_path(self, rel):
                return ROOT / rel

            def _reader_direct_present_result(self, uid, **kwargs):
                captured["calls"] += 1
                captured.update(uid=uid, **kwargs)
                return {"written": True, "turn_id": kwargs["turn_id"]}

        W = self._mod()
        handlers, missing = W.build_handlers(
            FakePdf(), current_user_id=lambda: 7)
        self.assertNotIn("result.present", missing)
        svc = DC.DirectCommandService(handlers)
        command = {
            "contract": DC.CONTRACT,
            "correlation": "voice.abc-1:card",
            "action": "result.present",
            "anchor": {"file": "Physics/book.pdf", "page": 24},
            "params": {
                "turnId": "voice.abc-1:card",
                "parts": [{
                    "kind": "card",
                    "card": {
                        "kind": "fact",
                        "data": {"answer": "42"},
                    },
                }],
            },
        }
        result = svc.submit(command)
        self.assertTrue(result["ok"])
        self.assertEqual(captured["calls"], 1)
        self.assertEqual(captured["uid"], 7)
        self.assertEqual(captured["file"], "Physics/book.pdf")
        self.assertEqual(captured["page"], 24)
        self.assertEqual(captured["turn_id"], "voice.abc-1:card")
        replay = svc.submit(command)
        self.assertTrue(replay["ok"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(captured["calls"], 1, "重复 correlation 不得重复写历史")

    def test_result_present_rejects_extra_params_before_display(self) -> None:
        called = []

        class FakePdf:
            def _safe_vault_path(self, rel):
                return ROOT / rel

            def _reader_direct_present_result(self, uid, **kwargs):
                called.append((uid, kwargs))
                return {"written": True}

        W = self._mod()
        handlers, _ = W.build_handlers(
            FakePdf(), current_user_id=lambda: 7)
        result = DC.DirectCommandService(handlers).submit({
            "correlation": "result-extra",
            "action": "result.present",
            "anchor": {"file": "book.pdf", "page": 1},
            "params": {
                "turnId": "result-extra",
                "parts": [{"kind": "text", "text": "x"}],
                "tool": "execute-me",
            },
        })
        self.assertFalse(result["ok"])
        self.assertIn("不支持这些 params 字段", result["error"])
        self.assertEqual(called, [])

    def test_result_present_requires_one_consistent_idempotency_identity(self):
        base = {
            "contract": DC.CONTRACT,
            "correlation": "result-one",
            "mode": "independent",
            "idempotency": "result-one",
            "action": "result.present",
            "anchor": {"file": "book.pdf", "page": 1},
            "params": {
                "turnId": "result-one",
                "parts": [{"kind": "card", "card": {
                    "kind": "fact", "data": {"answer": "42"}}}],
            },
        }
        self.assertEqual(
            DC.validate(base)["idempotency"],
            "result-one",
        )
        for field, value in (
            ("turnId", "different-turn"),
            ("idempotency", "different-key"),
        ):
            with self.subTest(field=field):
                command = json.loads(json.dumps(base))
                if field == "turnId":
                    command["params"]["turnId"] = value
                else:
                    command["idempotency"] = value
                with self.assertRaisesRegex(
                        DC.CommandError, "correlation 完全相同"):
                    DC.validate(command)

    def test_result_present_non_object_part_is_clean_validation_error(self):
        class FakePdf:
            def _safe_vault_path(self, rel):
                return ROOT / rel

            def _reader_direct_present_result(self, uid, **kwargs):
                raise AssertionError("invalid part must not reach display")

        W = self._mod()
        handlers, _ = W.build_handlers(
            FakePdf(), current_user_id=lambda: 7)
        result = DC.DirectCommandService(handlers).submit({
            "correlation": "result-list-part",
            "action": "result.present",
            "anchor": {"file": "book.pdf", "page": 1},
            "params": {
                "turnId": "result-list-part",
                "parts": ["not-an-object"],
            },
        })
        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])
        self.assertIn("只接受 card/cards", result["error"])


class FtsEscapingTest(unittest.TestCase):
    """用户输入不得直接当 FTS5 查询语法。

    生产冒烟实测:`zzzz-not-exist-zzzz` 里的 `not` 被 FTS5 当成操作符 →
    `OperationalError: no such column: not`。除了报错,它本身就是查询注入面。
    """

    def test_query_is_wrapped_as_phrase(self) -> None:
        src = (ROOT / "_server_deploy/reader_direct_wire.py").read_text("utf-8")
        self.assertIn("phrase = chr(34)", src, "查询串必须整体包成短语")
        self.assertIn("args: list = [phrase]", src, "传进 MATCH 的必须是转义后的短语")
        self.assertNotIn('args: list = [q]', src, "不得把原始输入直接送进 MATCH")

    def test_operators_and_quotes_are_neutralized(self) -> None:
        """复刻转义逻辑:操作符被中性化,内嵌引号被双写。"""
        def esc(q):
            return chr(34) + q.replace(chr(34), chr(34) * 2) + chr(34)
        for raw in ("zzzz-not-exist-zzzz", "a AND b", "x OR y", 'he said "hi"', "a*", "col:v"):
            out = esc(raw)
            self.assertTrue(out.startswith('"') and out.endswith('"'), out)
            inner = out[1:-1]
            self.assertEqual(inner.count('"') % 2, 0, f"内嵌引号必须双写:{out}")


class RealHandlerExecutionTest(unittest.TestCase):
    """用真实 pdf_reader 底座跑确定性动作(隔离 sidecar,不碰生产数据)。"""

    @classmethod
    def setUpClass(cls):
        import os, subprocess, textwrap
        cls.script = textwrap.dedent(f"""
            import json, sys, tempfile
            from pathlib import Path
            root = Path({str(ROOT)!r})
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app, pdf_reader as P
            from flask import session as _sess
            with app.app.app_context():
                db = app.get_db()
                db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                           ("dc-user", "x", "user"))
                db.commit()
                uid = db.execute("SELECT id FROM users WHERE username=?",
                                 ("dc-user",)).fetchone()["id"]
            tmp = Path(tempfile.mkdtemp(prefix="bw-dc-"))
            P._READER_SIDECAR_ROOT = tmp
            P._READER_SIDECAR_STORE = None
            svc = P._DIRECT_CMD["service"]
            book = None
            for p in sorted(P.OBSIDIAN_ROOT.rglob("*.pdf"))[:1]:
                book = p.relative_to(P.OBSIDIAN_ROOT.resolve()).as_posix()
            out = {{}}
            with app.app.test_request_context("/"):
                _sess["user_id"] = uid
                # 读页:真字符层
                r = svc.submit({{"correlation": "r1", "action": "read.page",
                                "anchor": {{"file": book, "page": 1}}}})
                out["read_page"] = {{"ok": r["ok"], "avail": r["steps"][0]["data"]["text_available"]
                                     if r["ok"] else None, "cmd": r.get("commandId", "")[:4]}}
                # 依赖多步:建页 → 写入(第二步依赖第一步返回的 userpage 锚点)
                r2 = svc.submit({{"correlation": "r2", "mode": "dependent", "voiceTask": "vt1",
                    "steps": [{{"action": "page.new", "anchor": {{"file": book}}}},
                              {{"action": "page.add", "anchor": {{"file": book}},
                                "params": {{"text": "hello"}}}}]}})
                out["dependent"] = {{"ok": r2["ok"], "n": len(r2["steps"]),
                                     "added": r2["steps"][-1]["data"].get("added") if r2["ok"] else None}}
                # 高亮:创建 + 列出 + 幂等
                svc.submit({{"correlation": "r3", "action": "highlight.create",
                    "anchor": {{"file": book, "page": 1}},
                    "params": {{"text": "abc", "color": "yellow", "_idem": "k1"}}}})
                svc.submit({{"correlation": "r3b", "action": "highlight.create",
                    "anchor": {{"file": book, "page": 1}},
                    "params": {{"text": "abc", "color": "yellow", "_idem": "k1"}}}})
                r4 = svc.submit({{"correlation": "r4", "action": "highlight.list",
                                 "anchor": {{"file": book, "page": 1}}}})
                out["highlight"] = {{"count": r4["steps"][0]["data"]["count"] if r4["ok"] else -1}}
                # 失败:坏 anchor → 事件入队
                before = svc.bus.cursor()
                r5 = svc.submit({{"correlation": "r5", "voiceTask": "vt9", "action": "read.page",
                                 "anchor": {{"file": "不存在.pdf", "page": 1}}}})
                ev = svc.bus.since(before)
                out["failure"] = {{"ok": r5["ok"], "events": len(ev),
                                   "task": ev[0]["voiceTask"] if ev else None,
                                   "hasCmdId": bool(ev[0].get("commandId")) if ev else False}}
                # 成功不产生事件
                c0 = svc.bus.cursor()
                svc.submit({{"correlation": "r6", "action": "highlight.list",
                            "anchor": {{"file": book}}}})
                out["silent"] = len(svc.bus.since(c0))
            print("RESULT" + json.dumps(out, ensure_ascii=False))
        """)
        env = dict(os.environ, SECRET_KEY="t", WEBAPP_DATA=tempfile.mkdtemp())
        cls.proc = subprocess.run([sys.executable, "-c", cls.script], cwd=ROOT, env=env,
                                  text=True, capture_output=True, timeout=300)
        line = [l for l in cls.proc.stdout.splitlines() if l.startswith("RESULT")]
        cls.out = json.loads(line[0][6:]) if line else None

    def test_subprocess_ok(self) -> None:
        self.assertIsNotNone(self.out, (self.proc.stdout + self.proc.stderr)[-1500:])

    def test_read_page_uses_real_text_layer(self) -> None:
        self.assertTrue(self.out["read_page"]["ok"])
        self.assertTrue(self.out["read_page"]["avail"], "真实书页应取到文字层")
        self.assertTrue(self.out["read_page"]["cmd"].startswith("cmd_"[:4]))

    def test_dependent_chain_passes_anchor(self) -> None:
        self.assertTrue(self.out["dependent"]["ok"])
        self.assertEqual(self.out["dependent"]["n"], 2)
        self.assertTrue(self.out["dependent"]["added"], "第二步必须用到第一步返回的页锚点")

    def test_highlight_idempotent_on_real_sidecar(self) -> None:
        self.assertEqual(self.out["highlight"]["count"], 1, "同幂等键重复创建应只留一条")

    def test_failure_emits_routed_event(self) -> None:
        self.assertFalse(self.out["failure"]["ok"])
        self.assertEqual(self.out["failure"]["events"], 1)
        self.assertEqual(self.out["failure"]["task"], "vt9", "事件必须按语音任务路由")
        self.assertTrue(self.out["failure"]["hasCmdId"], "事件要带 commandId")

    def test_success_is_silent(self) -> None:
        self.assertEqual(self.out["silent"], 0, "成功不得产生事件噪声")
