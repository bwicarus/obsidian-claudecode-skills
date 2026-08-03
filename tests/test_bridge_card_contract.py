"""外部助手 → 侧栏桥接闭环的契约测试(2026-07-27)。

覆盖用户拍板的六条:
  ① 卡片合法性只有一个来源=前端统一渲染器;桥接器/服务端都不许自带 kind 白名单
  ② assistant_turn 只收高层内容,书页/请求号自动取
  ③ 侧栏实时到达(SSE 既有总线 + 同一个 renderTurn,不新增对外服务)
  ④ 回执分「已写库」与「前端已渲染」
  ⑥ 快照分层 + 当前页正文文字优先(图片只作后备)
以及后补的:选区三态(有/无/未上报)、同步时序(默认即时,仅导航合并)。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
sys.path.insert(0, str(ROOT / "scripts"))
import reader_card_contract as CC  # noqa: E402


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class ContractSourceOfTruthTest(unittest.TestCase):
    """① kind 必须来自渲染器,而不是谁手写的一份表。"""

    def test_kinds_are_parsed_from_the_actual_renderers(self) -> None:
        parts = CC.renderer_part_kinds()
        cards = CC.renderer_card_kinds()
        # 与渲染器源码里的分支逐一对齐(改渲染器 → 这里自动跟着变)
        tc = (ROOT / "_server_deploy/static/pdf/rc-turncard.js").read_text("utf-8")
        vc = (ROOT / "_server_deploy/static/pdf/rc-voicecall.js").read_text("utf-8")
        for k in parts:
            self.assertIn(f"p.kind === '{k}'", tc, f"part kind {k} 在渲染器里不存在")
        for k in cards:
            if k != CC.FALLBACK_CARD_KIND:
                self.assertIn(f"k === '{k}'", vc, f"card kind {k} 在渲染器里不存在")
        self.assertIn("images", cards, "渲染器支持配图卡,契约必须认")
        self.assertIn("videos", cards)

    def test_no_hand_written_whitelists_left(self) -> None:
        """三处旧白名单必须已改为引用契约(桥接器自己更不许有一份)。"""
        asst = (ROOT / "_server_deploy/assistant.py").read_text("utf-8")
        self.assertNotIn('_EXT_CARD_KINDS = {', asst, "assistant.py 仍自带卡片白名单")
        self.assertIn("reader_card_contract", asst)
        bridge = (ROOT / "scripts/reader_bridge.py").read_text("utf-8")
        self.assertIn("reader_card_contract", bridge)
        # 桥接器里不得再出现卡型字面量集合
        self.assertNotRegex(bridge, r"\{\s*['\"]weather['\"]\s*,")

    def test_contract_gap_fails_closed(self) -> None:
        """渲染器多出一种 kind 而字段规格没补 → 拒绝写入并指出缺口(不放行渲染不出的卡)。"""
        self.assertEqual(CC.contract_gaps(), [], "当前契约不应有缺口")
        orig = dict(CC.CARD_FIELD_SPECS)
        try:
            CC.CARD_FIELD_SPECS.pop("images")
            self.assertIn("card:images", CC.contract_gaps())
            with self.assertRaises(CC.ContractError):
                CC.validate_parts([{"kind": "text", "text": "x"}])
        finally:
            CC.CARD_FIELD_SPECS.clear()
            CC.CARD_FIELD_SPECS.update(orig)


class CardValidationTest(unittest.TestCase):
    """未知/字段不合规必须明确拒绝,且错误要说清是哪张卡的哪个字段。"""

    def test_all_renderer_card_kinds_are_accepted(self) -> None:
        samples = {
            "weather": {"data": {"lo": 3, "hi": 9, "cond": "晴", "loc": "东京"}},
            "news": {"data": {"items": [{"t": "标题", "s": "摘要"}]}},
            "images": {"data": {"items": [{"url": "https://x/y.jpg", "title": "图"}]}},
            "videos": {"data": {"items": [{"title": "片", "channel": "台", "src": "bili"}]}},
            "fact": {"data": {"answer": "42"}},
            "general": {"data": {"text": "综合"}},
        }
        for k in CC.renderer_card_kinds():
            self.assertIn(k, samples, f"渲染器支持 {k} 但测试没有样例")
            out = CC.validate_card({"kind": k, **samples[k]})
            self.assertEqual(out["kind"], k)

    def test_unknown_kind_and_bad_fields_are_rejected_with_reason(self) -> None:
        with self.assertRaises(ValueError) as e1:
            CC.validate_card({"kind": "stonks", "data": {"text": "x"}})
        self.assertIn("渲染器画不出来", str(e1.exception))
        with self.assertRaises(ValueError) as e2:
            CC.validate_card({"kind": "fact", "data": {}})
        self.assertIn("answer", str(e2.exception), "错误要指出缺哪个字段")
        with self.assertRaises(ValueError) as e3:
            CC.validate_parts([{"kind": "text", "text": "   "}])
        self.assertIn("text", str(e3.exception))

    def test_extra_fields_are_stripped_not_stored(self) -> None:
        out = CC.validate_card({"kind": "fact", "data": {"answer": "a", "evil": "x"}, "onclick": "alert(1)"})
        self.assertNotIn("evil", out["data"])
        self.assertNotIn("onclick", out)

    def test_zero_cards_is_legal(self) -> None:
        """纯文本轮不许被强迫造卡(协议不得因没有卡片而失败)。"""
        self.assertEqual(CC.validate_parts([]), [])
        self.assertEqual(len(CC.validate_parts([{"kind": "text", "text": "只有一句话"}])), 1)


class SnapshotTextAndSelectionTest(unittest.TestCase):
    """⑥ 正文文字优先 + 选区三态。"""

    def _build(self, active: dict | None) -> str:
        snap = _load("snap_t", "scripts/reader_context_snapshot.py")
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp) / "state"; st.mkdir()
            snap.ST = st; snap.SIDECAR_ACCT = None
            (st / "reader-context-sync.json").write_text('{"enabled": true}', encoding="utf-8")
            if active is not None:
                (st / "reader-active.json").write_text(json.dumps(active), encoding="utf-8")
            out = Path(tmp) / "out"; snap.build(out)
            return (out / "context.md").read_text(encoding="utf-8")

    def test_text_section_always_declares_availability(self) -> None:
        md = self._build({"kind": "pdf", "file": "不存在/x.pdf", "pos": 3, "ts": int(time.time())})
        seg = md.split("## 三、当前页正文", 1)[1].split("## 四、", 1)[0]
        for f in ("text_available", "text_source", "fallback_reason"):
            self.assertIn(f, seg, f"缺 {f} → 会静默退化成只给图")
        self.assertIn("文件不存在", seg, "取不到正文要说清是哪一步")

    def test_images_section_is_explicitly_a_fallback(self) -> None:
        md = self._build({"kind": "pdf", "file": "不存在/x.pdf", "pos": 3, "ts": int(time.time())})
        self.assertIn("不是正文来源", md)
        self.assertLess(md.index("## 三、当前页正文"), md.index("## 四、图像"),
                        "正文必须排在图像之前")

    def test_selection_three_states(self) -> None:
        now = int(time.time())
        base = {"kind": "pdf", "file": "x.pdf", "pos": 1, "ts": now}
        has = self._build({**base, "selection": "选中的原文", "sel_page": 7})
        self.assertIn("选中的原文", has)
        self.assertIn("第 7 页", has)
        cleared = self._build({**base, "selection": "", "has_selection": False})
        self.assertIn("用户已取消选中", cleared)
        self.assertNotIn("选中的原文", cleared, "清空后不得残留旧选区")
        never = self._build(base)
        self.assertIn("未上报", never.split("## 二、", 1)[0])


class TimingContractTest(unittest.TestCase):
    """时序:默认即时,只有同书翻页才合并。"""

    def test_frontend_splits_nav_from_immediate(self) -> None:
        js = (ROOT / "_server_deploy/static/pdf/rc-core.js").read_text("utf-8")
        self.assertIn("_CTX_NAV_MS = 1000", js)
        self.assertIn("_CTX_NOW_MS = 0", js)
        self.assertIn("_ctxOnlyPosChanged", js, "必须逐字段比对才能把选区变化排除出导航")

    def test_selection_reports_immediately_and_on_clear(self) -> None:
        pdf = (ROOT / "_server_deploy/static/pdf/reader.src/16-caret-select.js").read_text("utf-8")
        self.assertIn("_ctxSelReport('')", pdf, "选区清空必须显式上报空串")
        self.assertIn("immediate: true", pdf)
        epub = (ROOT / "_server_deploy/static/pdf/epub-html.js").read_text("utf-8")
        self.assertIn("_ctxSelReport('')", epub)

    def test_daemon_classifies_nav_vs_immediate(self) -> None:
        py = (ROOT / "scripts/push_reader_context_to_pc.py").read_text("utf-8")
        self.assertIn("NAV_DEBOUNCE_S", py)
        self.assertIn("NOW_DEBOUNCE_S", py)
        self.assertIn("def _is_nav", py)

    def test_daemon_is_nav_only_for_pure_page_change(self) -> None:
        mod = _load("push_t", "scripts/push_reader_context_to_pc.py")
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp) / "state"; st.mkdir()
            mod.SNAP.ST = st; mod.SNAP.SIDECAR_ACCT = None
            f = st / "reader-active.json"
            p = mod.Pusher.__new__(mod.Pusher)
            p._last_active = {}
            def write(d): f.write_text(json.dumps(d), encoding="utf-8")
            write({"file": "a.pdf", "pos": 1, "selection": ""})
            self.assertFalse(p._is_nav("reader-active.json"), "首次没有基线 → 即时")
            write({"file": "a.pdf", "pos": 2, "selection": ""})
            self.assertTrue(p._is_nav("reader-active.json"), "同书仅翻页 → 合并")
            write({"file": "a.pdf", "pos": 3, "selection": "选中了"})
            self.assertFalse(p._is_nav("reader-active.json"), "选区变化 → 即时,不许被翻页窗拖住")
            write({"file": "b.pdf", "pos": 1, "selection": "选中了"})
            self.assertFalse(p._is_nav("reader-active.json"), "换书 → 即时")
            self.assertFalse(p._is_nav("reader-notes/x.json"), "非活动文件 → 即时")


class BridgeAndDeliveryTest(unittest.TestCase):
    """② 高层入参自动补书页;④ 回执分层;③ 实时到达接的是既有总线。"""

    def test_bridge_payload_is_high_level(self) -> None:
        src = (ROOT / "scripts/reader_bridge.py").read_text("utf-8")
        self.assertIn("def _active(", src, "书/页要自动取,不该让调用方拼")
        self.assertIn("act.get(\"member\")", src, "合并书要落到真实卷")
        for f in ("written", "delivery", "subscribers", "rendered"):
            self.assertIn(f, src, f"回执缺 {f} 层")

    def test_receipt_layers_are_distinct(self) -> None:
        src = (ROOT / "scripts/reader_bridge.py").read_text("utf-8")
        i_w, i_d = src.index('"written": True'), src.index('"delivery"')
        self.assertLess(i_w, i_d)
        self.assertIn("没有在线侧栏订阅", src, "0 订阅要说清不是失败")

    def test_publish_returns_delivered_count(self) -> None:
        ev = (ROOT / "_server_deploy/reader_events.py").read_text("utf-8")
        self.assertRegex(ev, r"return n\b")
        asst = (ROOT / "_server_deploy/assistant.py").read_text("utf-8")
        self.assertIn('"delivered": _delivered', asst)

    def test_live_append_reuses_the_one_renderer(self) -> None:
        js = (ROOT / "_server_deploy/static/pdf/rc-assistant.js").read_text("utf-8")
        self.assertIn("RC.turnCard.renderTurn('live'", js, "实时追加必须走同一个渲染器")
        self.assertIn("/pdf/api/turn-ack", js, "渲染完要回执")
        for host in ("pdf-tail.js", "epub-html.js"):
            h = (ROOT / "_server_deploy/static/pdf" / host).read_text("utf-8")
            self.assertIn("assistant-history", h, f"{host} 未接入既有 SSE 总线")
            self.assertIn("onHistoryEvent", h)

    def test_local_voice_turn_ignores_its_own_history_echo(self) -> None:
        js = (ROOT / "_server_deploy/static/pdf/rc-assistant.js").read_text("utf-8")
        mark = js.index("_liveSeen[_b.turn_id] = 1")
        post = js.index("fetch('/api/assistant/log'", mark)
        self.assertLess(mark, post, "本地轮次必须在落库广播前登记，避免自己的 SSE 回声重复渲染")
        self.assertIn("_liveSeen['u:' + _b.turn_id] = 1", js[mark:post])

    def test_no_new_outward_service(self) -> None:
        """只复用既有 reader-events,不许再开一个对外端口/长连接。"""
        js = (ROOT / "_server_deploy/static/pdf/rc-assistant.js").read_text("utf-8")
        self.assertNotIn("new EventSource", js, "侧栏不该自己再开一条 SSE")
        self.assertNotIn("new WebSocket", js)


if __name__ == "__main__":
    unittest.main()


class WindowsClientTest(unittest.TestCase):
    """⑤ Windows 客户端:复用已认证 SSH、空闲 60s 自动关、异常只重连一次。"""

    def setUp(self) -> None:
        self.src = (ROOT / "scripts/bridge_client.py").read_text("utf-8")

    def test_reuses_authenticated_connection(self) -> None:
        self.assertIn("ControlMaster=auto", self.src)
        self.assertIn("ControlPath", self.src)
        self.assertIn("%C", self.src, "复用连接要按目标分键,别把不同主机串一起")

    def test_idle_60s_auto_close(self) -> None:
        self.assertIn("ControlPersist", self.src)
        self.assertIn('BW_BRIDGE_IDLE_S", "60"', self.src)

    def test_reconnects_exactly_once_then_reports(self) -> None:
        self.assertIn("_drop_master", self.src, "重连前要先踢掉可能已死的复用连接")
        self.assertIn("已重连一次仍失败", self.src)
        # 只重连一次:call() 里 _run_once 恰好出现两次(首次 + 重连)
        self.assertEqual(self.src.count("_run_once(env)"), 2)
        self.assertIn("BatchMode=yes", self.src, "不能弹交互式密码框卡住无人值守调用")

    def test_caller_only_supplies_high_level_content(self) -> None:
        self.assertIn("def say(", self.src)
        self.assertIn("书/页/编号全自动", self.src)
        self.assertNotIn("/api/assistant/log", self.src, "客户端不该知道服务端路径")


class SnapshotTieringTest(unittest.TestCase):
    """⑥ context.md 分「当前」与「历史归档」两区,完整历史保留但不干扰现状判断。"""

    def test_two_tiers_marked(self) -> None:
        snap = _load("snap_tier", "scripts/reader_context_snapshot.py")
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp) / "state"; st.mkdir()
            snap.ST = st; snap.SIDECAR_ACCT = None
            (st / "reader-context-sync.json").write_text('{"enabled": true}', encoding="utf-8")
            out = Path(tmp) / "out"; snap.build(out)
            md = (out / "context.md").read_text(encoding="utf-8")
        self.assertIn("📌 当前上下文", md)
        self.assertIn("🗄 历史归档", md)
        self.assertLess(md.index("📌 当前上下文"), md.index("🗄 历史归档"))
        self.assertIn("不能用来推断用户现在在看什么", md)
        self.assertIn("不做摘要压缩", md, "完整历史必须保留")



class WriteRejectionTest(unittest.TestCase):
    """④ 失败要给具体原因:契约违规必须是 400 + 字段名,不能是 500,更不能静默丢。"""

    def test_contract_violation_returns_400_with_field(self) -> None:
        asst = (ROOT / "_server_deploy/assistant.py").read_text("utf-8")
        seg = asst.split("契约校验:", 1)[1][:900]
        # 2026-07-27 放宽为 except Exception:ContractError / FileNotFoundError
        # (生产读不到渲染器)也必须变成可读的 400,而不是 500。
        self.assertIn("except Exception", seg, "契约违规必须捕获,不能冒泡成 500")
        self.assertIn("), 400", seg)
        self.assertIn('"where": "parts"', seg)
        self.assertIn('"contract"', seg, "错误里要指明契约来源,便于调用方自查")

    def test_turn_ack_uses_this_modules_auth_idiom(self) -> None:
        """pdf_reader 没有 _logged_in();照搬 assistant.py 的写法会 NameError→500。"""
        pr = (ROOT / "_server_deploy/pdf_reader.py").read_text("utf-8")
        seg = pr.split("def pdf_api_turn_ack", 1)[1][:600]
        self.assertNotIn("_logged_in()", seg)
        self.assertIn('session.get("user_id")', seg)


class ExportScopeTest(unittest.TestCase):
    """导出必须与实现**同一闭包**——只比 IIFE 边界是不够的。

    2026-07-27 真实事故:`onHistoryEvent` 定义在 `mountPdfSidebar` 函数**内部**,而导出写在
    该函数外面 → 调用时 ReferenceError,被外层 `catch (e) {}` 吞掉,表现成
    「SSE 事件到达、什么都不渲染、也不报错」。我第一版自测只比较了两个 IIFE 的区间,
    因此判为"同作用域"而漏过。这里改成检查**嵌套宿主函数**是否一致。
    """

    JS = "_server_deploy/static/pdf/rc-assistant.js"

    def _lines(self):
        return (ROOT / self.JS).read_text("utf-8").splitlines()

    def test_export_is_inside_the_same_host_function(self) -> None:
        lines = self._lines()
        def find(pred):
            return next(i for i, l in enumerate(lines) if pred(l))
        mount = find(lambda l: "RC.assistant.mountPdfSidebar = function" in l)
        defn = find(lambda l: l.startswith("  function onHistoryEvent"))
        exp = find(lambda l: "RC.assistant.onHistoryEvent" in l and "=" in l)
        self.assertGreater(defn, mount, "前提变了:onHistoryEvent 不再定义在 mountPdfSidebar 内")
        self.assertGreater(exp, mount,
                           "导出写在 mountPdfSidebar 之外 → 运行时 ReferenceError 且被 catch 吞掉")

    def test_export_does_not_swallow_reference_errors_silently(self) -> None:
        """导出包装里不许再用空 catch 把 ReferenceError 吞掉——那正是这次难查的原因。"""
        line = next(l for l in self._lines()
                    if "RC.assistant.onHistoryEvent" in l and "=" in l)
        self.assertNotIn("try { return onHistoryEvent(ev); } catch (e) {}", line,
                         "包装内部不该再吞异常;异常应由 onHistoryEvent 自己的 try 处理")


class TestIsolationGuardTest(unittest.TestCase):
    """助手会话历史存在 CLAUDE_DIR/state,**不受 WEBAPP_DATA 控制**。

    2026-07-27 我的桥接 E2E 因此把一条测试轮写进了用户真实侧栏历史(已备份后逐条摘除)。
    这条守卫把那个前提固化下来:谁再写这类测试,必须显式隔离 _CONVO_DIR。
    """

    def test_convo_dir_is_not_under_webapp_data(self) -> None:
        asst = (ROOT / "_server_deploy/assistant.py").read_text("utf-8")
        self.assertRegex(asst, r"_CONVO_DIR\s*=\s*CLAUDE_DIR",
                         "会话目录若改到 WEBAPP_DATA 之下,本守卫与相关测试的隔离方式都要同步更新")



class ContractStaticPathTest(unittest.TestCase):
    """契约以渲染器为唯一来源 → 必须在**生产布局**下也找得到渲染器。

    2026-07-27 首次上线的真实事故:模块部署成功了,但生产 webapp 目录下没有 static/pdf
    (静态只由 nginx 从 /var/www/html/static 服务),一读渲染器就 FileNotFoundError,
    外部写入依然 500。本测试模拟"JS 不在模块旁"的布局。
    """

    def test_resolves_renderer_outside_module_dir(self) -> None:
        import importlib, os, shutil
        with tempfile.TemporaryDirectory() as tmp:
            far = Path(tmp) / "nginx-static" / "pdf"
            far.mkdir(parents=True)
            for n in ("rc-turncard.js", "rc-voicecall.js"):
                shutil.copy(ROOT / "_server_deploy/static/pdf" / n, far / n)
            old = os.environ.get("BW_READER_STATIC_PDF")
            os.environ["BW_READER_STATIC_PDF"] = str(far)
            try:
                m = importlib.reload(CC)
                self.assertEqual(m._static_root(), far)
                self.assertIn("images", m.renderer_card_kinds())
                self.assertEqual(m.contract_gaps(), [])
            finally:
                if old is None:
                    os.environ.pop("BW_READER_STATIC_PDF", None)
                else:
                    os.environ["BW_READER_STATIC_PDF"] = old
                importlib.reload(CC)

    def test_missing_renderer_fails_closed_with_paths(self) -> None:
        import importlib, os
        old = os.environ.get("BW_READER_STATIC_PDF")
        os.environ["BW_READER_STATIC_PDF"] = "/nonexistent"
        try:
            m = importlib.reload(CC)
            m._STATIC_CANDIDATES[:] = [Path("/nonexistent")]
            with self.assertRaises(m.ContractError) as e:
                m.renderer_card_kinds()
            self.assertIn("找不到统一渲染器", str(e.exception))
        finally:
            if old is None:
                os.environ.pop("BW_READER_STATIC_PDF", None)
            else:
                os.environ["BW_READER_STATIC_PDF"] = old
            importlib.reload(CC)

    def test_log_route_turns_any_contract_failure_into_400(self) -> None:
        asst = (ROOT / "_server_deploy/assistant.py").read_text("utf-8")
        seg = asst.split("契约校验:", 1)[1][:900]
        self.assertIn("except Exception as _ce", seg,
                      "只 catch ValueError 会让 ContractError/FileNotFoundError 漏成 500")
