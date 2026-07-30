"""出向上下文(A5):绘图版本稳定判定 + 焦点/取消。隔离验证,不碰生产数据。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
import reader_outgoing_context as OC  # noqa: E402


class DrawingRevisionTest(unittest.TestCase):
    """停笔约 1 秒才升版本;未稳定不给引用;旧版本立即失效。"""

    def test_unstable_gives_no_reference(self) -> None:
        dr = OC.DrawingRevisions(stable_s=1.0)
        st = dr.observe("a.pdf", 1, {"s": [1]}, now=100.0)
        self.assertFalse(st["stable"])
        self.assertIsNone(st["drawingRevision"], "未稳定时不得给版本号")
        self.assertIsNone(st["ref"], "未稳定时不得给引用(否则上游会拿到半截图)")
        self.assertAlmostEqual(st["pendingSince"], 0.0, places=2)

    def test_becomes_stable_after_quiet_window(self) -> None:
        dr = OC.DrawingRevisions(stable_s=1.0)
        dr.observe("a.pdf", 1, {"s": [1]}, now=100.0)
        self.assertFalse(dr.observe("a.pdf", 1, {"s": [1]}, now=100.5)["stable"], "0.5s 还不算稳定")
        st = dr.observe("a.pdf", 1, {"s": [1]}, now=101.0)
        self.assertTrue(st["stable"])
        self.assertTrue(st["drawingRevision"].startswith("dr_"))
        self.assertEqual(st["ref"]["kind"], "drawing")
        self.assertEqual(st["ref"]["revision"], st["drawingRevision"])

    def test_new_stroke_invalidates_old_revision_immediately(self) -> None:
        """继续画 → 旧版本立刻失效,绝不能让上游拿着旧图当当前。"""
        dr = OC.DrawingRevisions(stable_s=1.0)
        dr.observe("a.pdf", 1, {"s": [1]}, now=100.0)
        old = dr.observe("a.pdf", 1, {"s": [1]}, now=101.0)["drawingRevision"]
        st = dr.observe("a.pdf", 1, {"s": [1, 2]}, now=101.2)
        self.assertFalse(st["stable"])
        self.assertIsNone(st["drawingRevision"], "内容一变,旧版本必须立即失效")
        new = dr.observe("a.pdf", 1, {"s": [1, 2]}, now=102.3)["drawingRevision"]
        self.assertTrue(new and new != old, "新稳定版本应与旧版本不同")

    def test_revision_is_content_derived_not_timestamp(self) -> None:
        """同一幅图反复观察不该来回换号(否则上游会误以为图变了)。"""
        dr = OC.DrawingRevisions(stable_s=1.0)
        dr.observe("a.pdf", 1, {"s": [1]}, now=100.0)
        r1 = dr.observe("a.pdf", 1, {"s": [1]}, now=101.0)["drawingRevision"]
        r2 = dr.observe("a.pdf", 1, {"s": [1]}, now=105.0)["drawingRevision"]
        self.assertEqual(r1, r2)

    def test_pages_are_independent(self) -> None:
        dr = OC.DrawingRevisions(stable_s=1.0)
        dr.observe("a.pdf", 1, {"s": [1]}, now=100.0)
        dr.observe("a.pdf", 2, {"s": [9]}, now=100.0)
        r1 = dr.observe("a.pdf", 1, {"s": [1]}, now=101.0)["drawingRevision"]
        r2 = dr.observe("a.pdf", 2, {"s": [9]}, now=101.0)["drawingRevision"]
        self.assertNotEqual(r1, r2, "不同页的绘图版本不得混同")

    def test_no_ink_is_permanently_empty_and_preserves_page_type(self) -> None:
        dr = OC.DrawingRevisions(stable_s=1.0)
        for empty in (None, {}, []):
            first = dr.observe("a.pdf", 7, empty, now=100.0)
            later = dr.observe("a.pdf", 7, empty, now=500.0)
            for state in (first, later):
                self.assertEqual(state["freshness"], "none")
                self.assertTrue(state["empty"])
                self.assertFalse(state["inProgress"])
                self.assertFalse(state["stable"])
                self.assertIsNone(state["drawingRevision"])
                self.assertIsNone(state["pendingSince"])
                self.assertIsNone(state["ref"])
                self.assertEqual(state["page"], 7)

    def test_latest_observation_controls_equivalent_page_type(self) -> None:
        dr = OC.DrawingRevisions(stable_s=1.0)
        dr.observe("a.pdf", "7", {"s": [1]}, now=100.0)
        state = dr.observe("a.pdf", 7, {"s": [1]}, now=101.0)
        self.assertEqual(state["page"], 7)
        self.assertEqual(state["ref"]["page"], 7)

    def test_empty_observation_immediately_clears_stable_drawing(self) -> None:
        dr = OC.DrawingRevisions(stable_s=1.0)
        dr.observe("a.pdf", 7, {"s": [1]}, now=100.0)
        stable = dr.observe("a.pdf", 7, {"s": [1]}, now=101.0)
        self.assertTrue(stable["stable"])
        self.assertIsNotNone(stable["ref"])

        cleared = dr.observe("a.pdf", 7, None, now=101.1)
        self.assertEqual(cleared["freshness"], "none")
        self.assertTrue(cleared["empty"])
        self.assertFalse(cleared["stable"])
        self.assertIsNone(cleared["drawingRevision"])
        self.assertIsNone(cleared["ref"])


class FocusStateTest(unittest.TestCase):
    """四类焦点 + 显式取消;取消与陈旧都不得被当作当前。"""

    def test_all_kinds_accepted(self) -> None:
        f = OC.FocusState()
        for k in ("text", "image", "card", "drawing", "region"):
            f.set(k, {"id": "x"}, now=100.0)
            self.assertEqual(f.get(now=100.1)["focus"]["kind"], k)

    def test_unknown_kind_rejected(self) -> None:
        f = OC.FocusState()
        with self.assertRaises(ValueError) as e:
            f.set("hologram", {"id": "x"})
        self.assertIn("focus.kind", str(e.exception))
        with self.assertRaises(ValueError):
            f.set("text", {})

    def test_cancel_is_explicit_not_silent(self) -> None:
        f = OC.FocusState()
        f.set("card", {"cid": "c1"}, now=100.0)
        f.cancel(now=101.0)
        g = f.get(now=101.1)
        self.assertEqual(g["state"], "cancelled")
        self.assertIsNone(g["focus"], "取消后不得再给出 focus")
        self.assertEqual(g["cancelledObject"]["ref"]["cid"], "c1",
                         "要说清被取消的是哪个对象,而不是字段消失")
        self.assertIn("不要再当作当前选中", g["note"])

    def test_never_reported_differs_from_no_focus(self) -> None:
        g = OC.FocusState().get()
        self.assertEqual(g["state"], "never")
        self.assertIn("不是", g["note"], "「从未上报」与「没有焦点」必须可分辨")

    def test_stale_focus_is_not_current(self) -> None:
        f = OC.FocusState(fresh_s=10.0)
        f.set("text", {"t": "abc"}, now=100.0)
        g = f.get(now=200.0)
        self.assertEqual(g["state"], "stale")
        self.assertIsNone(g["focus"])
        self.assertEqual(g["lastObject"]["ref"]["t"], "abc")

    def test_reset_after_cancel(self) -> None:
        f = OC.FocusState()
        f.set("text", {"t": "a"}, now=100.0)
        f.cancel(now=101.0)
        f.set("image", {"src": "u"}, now=102.0)
        g = f.get(now=102.1)
        self.assertEqual(g["state"], "active")
        self.assertEqual(g["focus"]["kind"], "image")

    def test_seq_increases_for_change_detection(self) -> None:
        f = OC.FocusState()
        a = f.set("text", {"t": "1"}, now=100.0)["seq"]
        b = f.set("text", {"t": "2"}, now=100.5)["seq"]
        self.assertGreater(b, a, "seq 用于上游判断「本地版本变了才注入」")


class NoAiAndCompatTest(unittest.TestCase):
    """确定性 + 与 direct-command 失败事件兼容。"""

    def test_module_has_no_ai_dependency(self) -> None:
        src = (ROOT / "_server_deploy" / "reader_outgoing_context.py").read_text("utf-8")
        for bad in ("ai_client", "_ask(", "gemini", "openai", "claude", "codex", "reader_stream"):
            self.assertNotIn(bad, src, f"出向上下文不得依赖 AI:{bad}")

    def test_does_not_write_legacy_paths(self) -> None:
        """只读墨迹 sidecar;不得调用任何既有写函数(避免拖累旧路径)。"""
        src = (ROOT / "_server_deploy" / "reader_outgoing_context.py").read_text("utf-8")
        for w in ("_ink_save", "_hl_save", "_notes_save", "_upages_save", "atomic_write_json"):
            self.assertNotIn(w, src, f"本模块不该写 {w}")
        self.assertIn("_ink_load", src, "应当只读墨迹内容")

    def test_contract_names_are_distinct_from_command_bus(self) -> None:
        import reader_direct_commands as DC
        self.assertNotEqual(OC.CONTRACT, DC.CONTRACT, "两类事件契约名必须可区分")
        self.assertTrue(OC.CONTRACT.startswith("reader-outgoing-context/"))

    def test_routes_registered_without_touching_old(self) -> None:
        src = (ROOT / "_server_deploy" / "reader_outgoing_context.py").read_text("utf-8")
        for r in ("/api/outgoing/drawing", "/api/outgoing/focus", "/api/outgoing/state"):
            self.assertIn(r, src)
        self.assertIn("add_url_rule", src, "必须是增量挂载,不得改写既有路由")


class FrontendWiringTest(unittest.TestCase):
    """前端接线合同:唯一实现在共享层;三宿主复用;HTML 无绘图自然降级。"""

    S = ROOT / "_server_deploy" / "static" / "pdf"

    def _read(self, name):
        return (self.S / name).read_text("utf-8")

    def test_single_implementation_in_shared_layer(self) -> None:
        core = self._read("rc-core.js")
        for k in ("RC.outgoing", "_OG_DRAW_MS", "drawingTouched", "outgoing: _outgoing"):
            self.assertIn(k.replace("RC.outgoing", "_outgoing"), core.replace("RC.outgoing", "_outgoing"))
        self.assertIn("_OG_DRAW_MS = 1000", core, "绘图合并窗必须是约 1 秒")

    def test_focus_wired_in_all_three_hosts(self) -> None:
        for f in ("reader.src/16-caret-select.js", "epub-html.js", "html-reader.js"):
            t = self._read(f)
            self.assertIn("outgoing", t, f"{f} 未接焦点通道")
            self.assertIn(".cancel()", t, f"{f} 缺显式取消 —— 否则旧焦点会被当成现状")

    def test_drawing_wired_only_where_ink_exists(self) -> None:
        self.assertIn("drawingTouched", self._read("pdf-tail.js"), "PDF 墨迹未接")
        self.assertIn("drawingTouched", self._read("epub-html.js"), "EPUB 墨迹未接")
        html = self._read("html-reader.js")
        self.assertIn("没有墨迹层", html, "HTML 应显式说明降级,而不是悄悄少一块")

    def test_drawing_hooked_at_save_funnel_not_per_stroke_network(self) -> None:
        """接在既有保存漏斗上:每笔都调是安全的,网络合并交给共享层。"""
        for f in ("pdf-tail.js", "epub-html.js"):
            t = self._read(f)
            i_hook = t.index("drawingTouched")
            self.assertIn("_inkSave", t[max(0, i_hook - 400): i_hook + 200],
                          f"{f} 的绘图钩子应挂在墨迹保存漏斗附近")

    def test_gated_by_same_switch_no_new_toggle(self) -> None:
        core = self._read("rc-core.js")
        i = core.index("var _outgoing")
        seg = core[core.index("var _og = {"): i]
        self.assertIn("_ctxOn()", seg, "出向上下文必须复用同一把总开关,不新增开关")
        self.assertNotIn("eph-outgoing", core, "不得为此另立 localStorage 开关")


class FocusHostWiringTest(unittest.TestCase):
    """卡片/图片焦点埋点:接在**统一的选中出入口**,而不是每种交互各写一套。"""

    def _vc(self):
        return (ROOT / "_server_deploy/static/pdf/rc-voicecall.js").read_text("utf-8")

    def test_wired_at_single_funnel_not_per_interaction(self) -> None:
        t = self._vc()
        self.assertIn("function _pinRemember", t)
        self.assertIn("function _pinForget", t)
        i_set = t.index("RC.outgoing.focus(")
        i_fun = t.index("function _pinRemember")
        self.assertLess(abs(i_set - i_fun), 1200,
                        "focus 应挂在 _pinRemember 内 —— 卡片/图片/视频都经此,接一处不会漏")
        self.assertEqual(t.count("RC.outgoing.focus("), 1, "只应有一个设焦点调用点")
        self.assertEqual(t.count("RC.outgoing.cancel()"), 1, "只应有一个取消调用点")

    def test_cancel_wired_at_deselect(self) -> None:
        t = self._vc()
        seg = t[t.index("function _pinForget"): t.index("function _pinForget") + 500]
        self.assertIn("RC.outgoing.cancel()", seg,
                      "取消选中必须显式取消焦点,否则上游会拿已取消对象当现状")

    def test_kind_mapping_is_stable_and_closed(self) -> None:
        t = self._vc()
        seg = t[t.index("function _outgoingKindOf"):]
        seg = seg[:seg.index("function _pinRemember")]
        for k in ("image-item", "video-item", "ink", "drawing", "region"):
            self.assertIn(f"'{k}'", seg, f"kind 映射缺 {k}")
        self.assertIn("return 'card'", seg, "未识别对象归为 card,不新增类型、不猜")
        # 映射出的 kind 必须都在服务端登记的集合里
        allowed = set(OC.FOCUS_KINDS)
        import re
        produced = set(re.findall(r"return '([a-z]+)'", seg))
        self.assertTrue(produced <= allowed, f"映射产生了服务端不认的 kind:{produced - allowed}")

    def test_payload_is_minimal(self) -> None:
        """只带稳定引用 + 最小语义,不塞正文/图本身。"""
        t = self._vc()
        seg = t[t.index("RC.outgoing.focus("): t.index("RC.outgoing.focus(") + 420]
        for f in ("id:", "cid:", "label:", "brief:"):
            self.assertIn(f, seg)
        self.assertIn("slice(0, 160)", seg, "摘要必须截断,避免大 payload")
        for heavy in ("innerHTML", "toDataURL", "base64", "strokes"):
            self.assertNotIn(heavy, seg, f"焦点 payload 不得包含 {heavy}")

    def test_drawing_producer_absent_but_funnel_ready(self) -> None:
        """绘图区目前**没有**既有的选中交互(长按加上下文只覆盖高亮/便签/图)。
        本轮不为它新造手势(会碰指针状态机);但统一出入口已能识别 drawing,
        将来有生产者即自动生效。"""
        t = self._vc()
        self.assertIn("return 'drawing'", t, "出入口要能识别绘图对象")
        self.assertIn("data-ink-region", t, "DOM 兜底识别在位")


class DrawingFocusWiringTest(unittest.TestCase):
    """绘图区长按焦点:不碰指针状态机、两宿主都绑、切页丢弃。"""

    S = ROOT / "_server_deploy" / "static" / "pdf"

    def test_never_intercepts_pen_or_gestures(self) -> None:
        core = (self.S / "rc-core.js").read_text("utf-8")
        seg = core[core.index("bindDrawingFocus:"): core.index("dropDrawingFocus:")]
        self.assertIn("pointerType === 'pen'", seg, "笔必须直接 return —— 画笔零影响")
        self.assertNotIn("preventDefault", seg, "不得 preventDefault(会干扰滚动/画笔)")
        self.assertNotIn("stopPropagation", seg, "不得 stopPropagation(会吞掉既有手势)")
        self.assertEqual(seg.count("passive: true"), 3, "三组监听都必须 passive")

    def test_long_press_thresholds_and_cancel(self) -> None:
        core = (self.S / "rc-core.js").read_text("utf-8")
        seg = core[core.index("bindDrawingFocus:"): core.index("dropDrawingFocus:")]
        self.assertIn("LONG_MS = 500", seg)
        self.assertIn("MOVE_PX = 10", seg, "位移超阈值=滚页,不能当长按")
        self.assertIn("_outgoing.cancel()", seg, "再长按同一块=取消")
        self.assertIn("!c.hasInk", seg, "目标失效(无墨迹)不得设空焦点")

    def test_signature_uses_one_algorithm(self) -> None:
        """签名必须与 focus() 同一算法算 —— 首版两处不同,导致"再长按取消"永远失效。"""
        core = (self.S / "rc-core.js").read_text("utf-8")
        self.assertIn("function _ogSig(", core)
        self.assertEqual(core.count("_ogSig("), 3, "一个定义 + 两个调用点,不许各算各的")

    def test_payload_has_no_strokes(self) -> None:
        core = (self.S / "rc-core.js").read_text("utf-8")
        seg = core[core.index("bindDrawingFocus:"): core.index("dropDrawingFocus:")]
        self.assertIn("drawingRevision", seg)
        for heavy in ("strokes", "toDataURL", "base64", "__inkStrokes"):
            self.assertNotIn(heavy, seg, f"焦点引用不得携带 {heavy}")

    def test_both_ink_hosts_bound(self) -> None:
        self.assertEqual((self.S / "reader.src/04-render.js").read_text("utf-8")
                         .count("bindDrawingFocus"), 2, "PDF 两条渲染路径都要绑")
        self.assertIn("bindDrawingFocus", (self.S / "epub-html.js").read_text("utf-8"))

    def test_page_change_drops_focus(self) -> None:
        for f in ("reader.src/02-position.js", "epub-html.js"):
            self.assertIn("dropDrawingFocus", (self.S / f).read_text("utf-8"),
                          f"{f} 切页未丢弃绘图焦点")


class JournalTest(unittest.TestCase):
    """不可变事件日志:游标、保留上限、间隙告知、损坏 fail-closed。"""

    def _jr(self, tmp):
        return OC.OutgoingJournal(lambda: Path(tmp) / "j.jsonl")

    def test_append_and_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            jr.append("focus", {"action": "set", "kind": "text"})
            jr.append("drawing", {"state": "stable", "drawingRevision": "dr_x"})
            r = jr.since(0)
            self.assertEqual([e["seq"] for e in r["events"]], [1, 2], "seq 必须单调")
            self.assertEqual(r["cursor"], 2)
            self.assertEqual(jr.since(1)["events"][0]["seq"], 2, "游标之后才返回")
            self.assertEqual(jr.since(2)["events"], [], "没有新事件时返回空,但 cursor 仍在")
            for e in r["events"]:
                self.assertEqual(e["v"], 1)
                self.assertIn("id", e)
                self.assertIn("ts", e)

    def test_retention_and_gap_is_announced(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            jr.KEEP = 5
            for i in range(12):
                jr.append("focus", {"i": i})
            r = jr.since(1)
            self.assertTrue(r["gap"], "游标落后于保留窗口必须**明说**,不能假装连续")
            self.assertIn("重新对齐", r["note"])
            self.assertEqual(jr.since(r["head"] - 1)["gap"], False)

    def test_corrupt_journal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            jr.append("focus", {"a": 1})
            (Path(t) / "j.jsonl").write_text('{"seq":1}\n{坏行}\n', encoding="utf-8")
            with self.assertRaises(ValueError) as e:
                jr.since(0)
            self.assertIn("损坏", str(e.exception))
            self.assertIn("拒绝返回部分结果", str(e.exception))

    def test_atomic_write_leaves_no_partial(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            jr.append("focus", {"a": 1})
            self.assertFalse(list(Path(t).glob("*.tmp")), "不得留下半截临时文件")

    def test_route_and_policy_registered(self) -> None:
        src = (ROOT / "_server_deploy/reader_outgoing_context.py").read_text("utf-8")
        self.assertIn("/api/outgoing/journal", src)
        pol = (ROOT / "_server_deploy/vbook_route_policy.py").read_text("utf-8")
        for ep in ("pdf_api_outgoing_journal", "pdf_api_direct_command",
                   "pdf_api_outgoing_focus", "pdf_api_outgoing_drawing",
                   "pdf_api_outgoing_state", "pdf_api_direct_events"):
            self.assertIn(ep, pol, f"{ep} 未声明 vbook 策略")

    def test_new_modules_in_deploy_manifest(self) -> None:
        """新服务端模块必须进部署清单 —— 否则依赖方上线而模块不上线(已踩过一次)。"""
        man = (ROOT / "scripts/reader_deploy_manifest.py").read_text("utf-8")
        for m in ("reader_card_contract.py", "reader_direct_commands.py",
                  "reader_direct_wire.py", "reader_outgoing_context.py"):
            self.assertIn(m, man, f"{m} 未进部署清单")


class CharLayerSelectionWiringTest(unittest.TestCase):
    """char-layer 自定义选中必须通知出向漏斗。

    真实故障(2026-07-28 用户实测):p23 选中文字后 journal 无 focus、活动记录 selection=''。
    根因=PDF 选中走 char-layer(画在 sel-overlay,**不产生原生 selection**),
    而 `checkSelection` 只挂在 mouseup/touchend/selectionchange 上 —— 两者没有交点。
    """

    S = ROOT / "_server_deploy" / "static" / "pdf"

    def test_hook_at_single_universal_notify_point(self) -> None:
        src = (self.S / "reader.src/14-textlayer-legacy.js").read_text("utf-8")
        i = src.index("function _updateSelPreview(text)")
        seg = src[i:i + 1400]
        self.assertIn("_ctxSelReport(text)", seg,
                      "必须在选中变更的唯一全覆盖通知点挂钩")
        self.assertIn("typeof _ctxSelReport === 'function'", seg,
                      "要做存在性守卫 —— 该函数定义在更后面的分片里")

    def test_no_second_selection_mechanism(self) -> None:
        """只挂一处;不得在各个提交/清空点散落补丁(那才是第二套机制)。"""
        total = 0
        for f in sorted((self.S / "reader.src").glob("*.js")):
            total += f.read_text("utf-8").count("_ctxSelReport(")
        # 16-caret-select.js 内:1 定义 + 3 调用(原生分支/空分支/char 残留分支)
        # 14-textlayer-legacy.js 内:本次新增 1 处
        self.assertEqual(total, 5, f"_ctxSelReport 调用点总数应为 5,实际 {total}")

    def test_clear_path_sends_explicit_cancel(self) -> None:
        """清空时传空串 → _ctxSelReport 内部走 cancel 分支(显式取消,不是省略字段)。"""
        src = (self.S / "reader.src/16-caret-select.js").read_text("utf-8")
        i = src.index("function _ctxSelReport(txt)")
        seg = src[i:i + 900]
        self.assertIn("RC?.outgoing?.cancel()", seg, "空串必须显式 cancel")
        self.assertIn("RC?.outgoing?.focus('text'", seg, "非空必须发 text 焦点")

    def test_hook_file_is_a_build_input(self) -> None:
        """钩子所在文件必须在构建输入里。

        ⚠ 不要断言仓库根的 `static/pdf/reader.js` —— 部署脚本现在把 reader.src/*.js
        拼进 `$STAGE_DIR/generated/reader.js` 再安装到 nginx 目录,**仓库副本已不再更新**
        (实测:生产含钩子=1,仓库副本=0)。拿仓库副本当"构建产物"会得出反向结论。
        """
        sh = (ROOT / "scripts" / "deploy_reader.sh").read_text("utf-8")
        self.assertIn('cat "${READER_PARTS[@]}" > "$STAGE_DIR/generated/reader.js"', sh,
                      "构建方式变了,本测试的前提需同步更新")
        self.assertIn("reader.src", sh, "构建输入应来自 reader.src")
        self.assertTrue((self.S / "reader.src" / "14-textlayer-legacy.js").is_file(),
                        "钩子文件必须在 reader.src 下才会被拼进产物")


class LongPollTest(unittest.TestCase):
    """长轮询:延迟从"半个轮询周期"降到毫秒级,但不能变成线程杀手。

    背景实测:服务端取一次只要 13-28ms,而 2-5s 轮询让 99% 的延迟耗在等待上。
    护栏源自本项目真实事故:SSE 每条流独占一个线程,8 条打死全站。
    """

    def _jr(self, tmp):
        return OC.OutgoingJournal(lambda: Path(tmp) / "j.jsonl")

    def test_returns_immediately_when_events_exist(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            jr.append("focus", {"a": 1})
            t0 = time.time()
            r = jr.wait_since(0, 100, wait_s=5)
            self.assertEqual(len(r["events"]), 1)
            self.assertLess(time.time() - t0, 0.3, "已有事件不得白等")
            self.assertEqual(r["waited"], 0.0)

    def test_wakes_up_on_append_from_another_thread(self) -> None:
        """核心价值:事件一到就醒,而不是等到下一个轮询周期。"""
        import threading as _th
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            def later():
                time.sleep(0.4)
                jr.append("drawing", {"state": "stable"})
            _th.Thread(target=later, daemon=True).start()
            t0 = time.time()
            r = jr.wait_since(0, 100, wait_s=10)
            dt = time.time() - t0
            self.assertEqual(len(r["events"]), 1)
            self.assertLess(dt, 2.0, f"应在事件到达后很快返回,实际 {dt:.2f}s")
            self.assertGreater(dt, 0.3, "不该在事件产生前就返回")

    def test_times_out_without_events(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            t0 = time.time()
            r = jr.wait_since(0, 100, wait_s=0.6)
            dt = time.time() - t0
            self.assertEqual(r["events"], [])
            self.assertGreaterEqual(dt, 0.5)
            self.assertLess(dt, 2.5)

    def test_wait_is_clamped(self) -> None:
        self.assertEqual(OC.OutgoingJournal.MAX_WAIT_S, 25.0)
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            jr.MAX_WAIT_S = 0.4
            t0 = time.time()
            jr.wait_since(0, 100, wait_s=999)
            self.assertLess(time.time() - t0, 2.0, "wait 必须被夹紧,不能听客户端的")

    def test_concurrency_cap_returns_instead_of_queueing(self) -> None:
        """超并发**立即返回**并标记,而不是排队占线程 —— 这是防线程饥饿的关键。"""
        import threading as _th
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            jr.MAX_WAITERS = 1
            hold = _th.Thread(target=lambda: jr.wait_since(0, 100, wait_s=1.2), daemon=True)
            hold.start()
            time.sleep(0.25)
            t0 = time.time()
            r = jr.wait_since(0, 100, wait_s=5)
            self.assertLess(time.time() - t0, 0.5, "超并发不得排队")
            self.assertTrue(r.get("waitDenied"))
            self.assertIn("退回定时轮询", r["note"])
            hold.join(timeout=3)

    def test_waiter_count_released_after_return(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            jr.wait_since(0, 100, wait_s=0.3)
            self.assertEqual(jr._waiters, 0, "等待计数必须归零,否则会永久占满名额")

    def test_no_write_lock_held_while_waiting(self) -> None:
        """等待期间不得持有写锁 —— 否则 append 会被阻塞,长轮询反而拖垮写入。"""
        import threading as _th
        with tempfile.TemporaryDirectory() as t:
            jr = self._jr(t)
            _th.Thread(target=lambda: jr.wait_since(0, 100, wait_s=1.0), daemon=True).start()
            time.sleep(0.2)
            t0 = time.time()
            jr.append("focus", {"x": 1})     # 等待中仍必须能写
            self.assertLess(time.time() - t0, 0.5, "append 被长轮询阻塞了")

    def test_route_accepts_wait_param(self) -> None:
        src = (ROOT / "_server_deploy/reader_outgoing_context.py").read_text("utf-8")
        self.assertIn('request.args.get("wait")', src)
        self.assertIn("wait_since(since, limit, wait)", src)
        self.assertIn("since/limit/wait 必须是数字", src, "参数错误要 fail-closed 报 400")


class PageContextBuildTest(unittest.TestCase):
    """整页上下文产生器:正文来自文字层、视觉只给引用、取不到时如实降级。"""

    class _Pdf:
        """最小 pdf 模块替身(只提供 build_page_context 真正用到的四个符号)。"""

        def __init__(self, *, text="", ink=None, boom=False, epub_paras=None):
            self._text, self._ink, self._boom = text, ink or {}, boom
            self._paras = epub_paras or []

        def _safe_vault_path(self, rel):
            return None if rel == "缺失.pdf" else Path("/vault") / rel

        def _page_text_clean(self, ap, rel, page, limit=None):
            if self._boom:
                raise RuntimeError("字符层坏了")
            return self._text

        def _epub_section_paragraphs(self, rel, idx):
            return self._paras

        def _ink_load(self, rel):
            return self._ink

    def test_pdf_text_layer_becomes_full_page_text(self) -> None:
        ctx = OC.build_page_context(self._Pdf(text="第一段\n第二段"), "书/a.pdf", 12)
        self.assertTrue(ctx["text_available"])
        self.assertEqual(ctx["text"], "第一段\n第二段")
        self.assertIn("字符层", ctx["text_source"])
        self.assertFalse(ctx["truncated"])
        self.assertIsNone(ctx["fallback_reason"])
        self.assertEqual(ctx["reason"], "dwell")

    def test_epub_uses_section_paragraphs(self) -> None:
        ctx = OC.build_page_context(self._Pdf(epub_paras=["甲", "乙"]), "书/b.epub", 3)
        self.assertTrue(ctx["text_available"])
        self.assertEqual(ctx["text"], "甲\n\n乙")
        self.assertIn("epub", ctx["text_source"])

    def test_scanned_page_degrades_explicitly(self) -> None:
        """扫描页没文字层 —— 必须照发事件并说明原因,不能静默当成"没这回事"。"""
        ctx = OC.build_page_context(self._Pdf(text="   "), "书/a.pdf", 5)
        self.assertFalse(ctx["text_available"])
        self.assertEqual(ctx["text"], "")
        self.assertIn("扫描", ctx["fallback_reason"])
        self.assertIsNotNone(ctx["visual"]["page_image"], "没文字层时更要给页图引用")

    def test_extraction_failure_is_reported_not_swallowed(self) -> None:
        ctx = OC.build_page_context(self._Pdf(boom=True), "书/a.pdf", 5)
        self.assertFalse(ctx["text_available"])
        self.assertIn("RuntimeError", ctx["fallback_reason"])

    def test_missing_file_reported(self) -> None:
        ctx = OC.build_page_context(self._Pdf(), "缺失.pdf", 1)
        self.assertFalse(ctx["text_available"])
        self.assertIn("不可解析", ctx["fallback_reason"])

    def test_truncation_is_flagged(self) -> None:
        big = "字" * (OC.PAGE_TEXT_LIMIT + 500)
        ctx = OC.build_page_context(self._Pdf(text=big), "书/a.pdf", 1)
        self.assertEqual(len(ctx["text"]), OC.PAGE_TEXT_LIMIT)
        self.assertTrue(ctx["truncated"], "截断必须显式告知,否则上游会以为读到了整页")

    def test_visual_is_reference_only(self) -> None:
        """视觉资源只给引用:journal 要小,图由消费方按需取(禁止塞 base64 字节)。"""
        ctx = OC.build_page_context(
            self._Pdf(text="x", ink={"pages": {"7": [{"d": "..."}]}}), "书 名/a.pdf", 7)
        v = ctx["visual"]
        self.assertIn("/pdf/api/page-image?file=", v["page_image"])
        self.assertIn("page=7", v["page_image"])
        self.assertNotIn(" ", v["page_image"], "路径必须 URL 编码")
        self.assertTrue(v["has_ink"])
        self.assertEqual(v["has_ink"], not v["drawing"]["empty"])
        blob = repr(ctx)
        self.assertNotIn("base64", blob)
        self.assertLess(len(blob), 6000, "单条事件必须保持紧凑")

    def test_no_ink_page_reports_false(self) -> None:
        ctx = OC.build_page_context(self._Pdf(text="x", ink={"pages": {"9": [1]}}), "a.pdf", 7)
        self.assertFalse(ctx["visual"]["has_ink"])
        drawing = ctx["visual"]["drawing"]
        self.assertEqual(ctx["visual"]["has_ink"], not drawing["empty"])
        self.assertEqual(drawing["freshness"], "none")
        self.assertTrue(drawing["empty"])
        self.assertFalse(drawing["inProgress"])
        self.assertFalse(drawing["stable"])
        self.assertIsNone(drawing["drawingRevision"])
        self.assertIsNone(drawing["pendingSince"])
        self.assertIsNone(drawing["ref"])
        self.assertEqual(drawing["page"], 7)


class PageContextGateTest(unittest.TestCase):
    """产生点的门:连续翻页不注入、停留发一次、选区补齐整页背景。

    只读校验 pdf_reader 里的门逻辑源码(该模块整体导入需要完整 Flask 应用环境,
    这里不引入生产依赖;行为侧由前端 harness + 上面的产生器测试覆盖)。
    """

    SRC = (ROOT / "_server_deploy/pdf_reader.py").read_text("utf-8")

    def test_gate_requires_dwell_or_selection(self) -> None:
        self.assertIn('if reason != "dwell" and not has_sel:', self.SRC,
                      "纯翻页(无 dwell、无选区)必须直接返回,否则会逐页注入")

    def test_dedupe_key_includes_selection(self) -> None:
        self.assertIn("hashlib.sha1(sel.encode())", self.SRC,
                      "选区变化必须换键,否则选中后拿不到整页背景补齐")

    def test_emits_page_context_kind(self) -> None:
        self.assertIn('jr.append("page.context"', self.SRC)
        self.assertIn('"stable": True', self.SRC)
        self.assertIn('"book_id": rel', self.SRC, "Windows 注入器按 book_id 取书")
        self.assertIn('"text_available": ctx["text_available"]', self.SRC)

    def test_failure_never_breaks_reading_position(self) -> None:
        i = self.SRC.index("def _maybe_emit_page_context")
        body = self.SRC[i:i + 2500]
        self.assertIn("except Exception:", body, "出向通道故障不得影响续读位置写入")


if __name__ == "__main__":
    unittest.main()
