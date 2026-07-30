"""快照「当前书/页」权威源 + 双向上下文同步总开关的契约测试。

背景(2026-07-26 用户实测的误判):快照把 `reader-positions.json` 里 ts 最大的一条当成
「当前在读」,于是两天前读过的《応用情報技術者》一直霸榜被当成当前;而 positions 里 ts
更新的是费恩曼、用户屏幕上其实是第三本书。根因是把「每本书各自的续读位置」当成了
「此刻活动的文档」——两码事。修复:引入 reader-active.json 作唯一权威源 + 新鲜度判定,
不新鲜就如实说未知,**绝不许退回历史表里挑一本冒充**。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _load_snapshot_module():
    spec = importlib.util.spec_from_file_location(
        "reader_context_snapshot", ROOT / "scripts" / "reader_context_snapshot.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SnapshotCurrentBookTest(unittest.TestCase):
    """快照怎么决定「当前在读」——本次 bug 的核心。"""

    HISTORY_BOOK = "資源/books/応用情報技術者.pdf"   # 历史里 ts 最大的那本(曾被误当成当前)
    ACTIVE_BOOK = "資源/books/feynman.pdf"          # 真正打开着的那本

    def _build(self, *, active: dict | None, sync_on: bool) -> str:
        snap = _load_snapshot_module()
        with tempfile.TemporaryDirectory(prefix="bw-ctx-snap-") as tmp:
            st = Path(tmp) / "state"
            st.mkdir()
            snap.ST = st
            snap.SIDECAR_ACCT = None   # 隔离:否则 sc() 会读到本机真实账户分区
            # 历史表:HISTORY_BOOK 的 ts 最大(还原真实现场)
            (st / "reader-positions.json").write_text(json.dumps({
                self.HISTORY_BOOK: {"kind": "pdf", "pos": 43, "ts": int(time.time()) - 4 * 86400},
                self.ACTIVE_BOOK: {"kind": "pdf", "pos": 57, "ts": int(time.time()) - 5 * 86400},
            }), encoding="utf-8")
            (st / "reader-context-sync.json").write_text(
                json.dumps({"enabled": sync_on}), encoding="utf-8")
            if active is not None:
                (st / "reader-active.json").write_text(json.dumps(active), encoding="utf-8")
            out = Path(tmp) / "out"
            snap.build(out)
            return (out / "context.md").read_text(encoding="utf-8")

    def _section_one(self, md: str) -> str:
        head = md.split("## 一、当前在读", 1)[1]
        return head.split("## 二、", 1)[0]

    def test_fresh_active_wins_over_newest_history_row(self) -> None:
        """有新鲜 active → 当前在读是它,而不是历史表里 ts 最大的那本。"""
        md = self._build(active={
            "kind": "pdf", "file": self.ACTIVE_BOOK, "pos": 57,
            "title": "费恩曼物理学讲义", "ts": int(time.time()) - 5,
        }, sync_on=True)
        one = self._section_one(md)
        self.assertIn(self.ACTIVE_BOOK, one)
        self.assertIn("🟢 实时", one)
        self.assertNotIn("応用情報技術者", one, "历史书不得出现在「当前在读」段")

    def test_stale_active_reports_unknown_not_a_history_book(self) -> None:
        """active 过期 → 明确说未知,不许拿历史书顶包。"""
        md = self._build(active={
            "kind": "pdf", "file": self.ACTIVE_BOOK, "pos": 57,
            "ts": int(time.time()) - 3600,
        }, sync_on=True)
        one = self._section_one(md)
        self.assertIn("当前在读：未知", one)
        self.assertNotIn("応用情報技術者", one)
        self.assertNotIn("🟢 实时", one)
        self.assertIn("过期", one)

    def test_no_active_at_all_is_the_original_bug(self) -> None:
        """完全没有 active(修复前的现场)→ 必须说未知,绝不能报《応用情報技術者》。"""
        md = self._build(active=None, sync_on=True)
        one = self._section_one(md)
        self.assertIn("当前在读：未知", one)
        self.assertNotIn("応用情報技術者", one,
                         "这正是修复前的错误行为:拿历史表最大 ts 冒充当前")
        self.assertNotIn(self.ACTIVE_BOOK, one)

    def test_switch_off_is_stated_and_explains_unknown(self) -> None:
        """开关关闭 → 抬头写明、未知的原因指向开关(而不是让人以为没在读书)。"""
        md = self._build(active=None, sync_on=False)
        self.assertIn("⚪ 已关闭", md)
        one = self._section_one(md)
        self.assertIn("当前在读：未知", one)
        self.assertIn("开关", one)

    def test_history_section_is_labelled_not_current(self) -> None:
        """历史段必须自带「不是当前在读」的标签,避免下游 agent 再次误读。"""
        md = self._build(active=None, sync_on=True)
        self.assertIn("不是当前在读", md)
        one = self._section_one(md)
        self.assertIn("不要拿下面「历史记录」里的任何一本当成用户此刻在看的书", one)

    def test_downstream_sections_stay_empty_when_current_unknown(self) -> None:
        """当前未知时,标注/图像等下游段落也不许拿某本书的数据填充。"""
        md = self._build(active=None, sync_on=True)
        # 小节编号在 2026-07-27 加入「当前页正文」后整体后移了一位(标注段=五)
        marks = md.split("## 五、本书标注概况", 1)[1].split("## 六、", 1)[0]
        self.assertIn("当前在读未知", marks)
        self.assertNotIn("応用情報技術者", marks)


class SidecarSourceTest(unittest.TestCase):
    """快照必须读账户分区的**实时** sidecar,而不是 state/ 下认领时冻结的 legacy 副本。

    这是「当前书误判」的第二个根因:legacy 里最新是 07-22 的《応用情報技術者》,
    账户实时那份其实是 07-26 的费恩曼——快照读错源,页码规则再对也没用。
    """

    def test_account_partition_wins_over_frozen_legacy_copy(self) -> None:
        snap = _load_snapshot_module()
        with tempfile.TemporaryDirectory(prefix="bw-ctx-src-") as tmp:
            st = Path(tmp) / "state"
            st.mkdir()
            acct = Path(tmp) / "by-user" / "1"
            acct.mkdir(parents=True)
            snap.ST = st
            snap.SIDECAR_ACCT = acct
            now = int(time.time())
            # legacy:冻结的旧世界
            (st / "reader-active.json").write_text(json.dumps({
                "kind": "pdf", "file": "旧/legacy.pdf", "pos": 43, "ts": now - 5}),
                encoding="utf-8")
            # 账户分区:真正在写的那份
            (acct / "reader-active.json").write_text(json.dumps({
                "kind": "pdf", "file": "新/account.pdf", "pos": 57, "ts": now - 5}),
                encoding="utf-8")
            (acct / "reader-context-sync.json").write_text('{"enabled": true}', encoding="utf-8")
            out = Path(tmp) / "out"
            snap.build(out)
            one = (out / "context.md").read_text(encoding="utf-8") \
                .split("## 一、当前在读", 1)[1].split("## 二、", 1)[0]
            self.assertIn("新/account.pdf", one)
            self.assertNotIn("旧/legacy.pdf", one, "读到了冻结的 legacy 副本")

    def test_falls_back_to_legacy_when_account_lacks_dataset(self) -> None:
        """尚未账户化的数据集(convo/cli-tasks 等)仍走 state/,行为不变。"""
        snap = _load_snapshot_module()
        with tempfile.TemporaryDirectory(prefix="bw-ctx-src2-") as tmp:
            st = Path(tmp) / "state"
            st.mkdir()
            acct = Path(tmp) / "by-user" / "1"
            acct.mkdir(parents=True)
            snap.ST = st
            snap.SIDECAR_ACCT = acct
            (st / "cli-tasks").mkdir()
            self.assertEqual(snap.sc("cli-tasks"), st / "cli-tasks")
            (acct / "reader-notes").mkdir()
            self.assertEqual(snap.sc("reader-notes"), acct / "reader-notes")


class VbookActiveTest(unittest.TestCase):
    """合并书(vbook):用户眼里在读的是「这本合并书第 N 页」,快照必须这么说。

    回归背景:上线当天用户开着合并书,`/api/active-reading` 被 vbook 网关按
    `vbook_unadapted` 打回 501(fail-closed),活动状态一条都没落库 → Windows 快照
    一直停在「未知」。同时内容类查询要落到**真实卷**,否则 book_sha 算的是不存在的路径。
    """

    def test_snapshot_shows_merged_identity_but_reads_member_volume(self) -> None:
        snap = _load_snapshot_module()
        with tempfile.TemporaryDirectory(prefix="bw-ctx-vb-") as tmp:
            st = Path(tmp) / "state"
            st.mkdir()
            snap.ST = st
            snap.SIDECAR_ACCT = None
            (st / "reader-context-sync.json").write_text('{"enabled": true}', encoding="utf-8")
            (st / "reader-active.json").write_text(json.dumps({
                "kind": "pdf", "file": "vbook:g_3e5d696e85", "pos": 31, "vbook": True,
                "member": "资源/books/某书 part2.pdf", "member_pos": 7,
                "ts": int(time.time()) - 3,
            }), encoding="utf-8")
            out = Path(tmp) / "out"
            snap.build(out)
            one = (out / "context.md").read_text(encoding="utf-8") \
                .split("## 一、当前在读", 1)[1].split("## 二、", 1)[0]
            self.assertIn("vbook:g_3e5d696e85", one)
            self.assertIn("合并书", one)
            self.assertIn("某书 part2.pdf", one, "必须给出真实卷,否则取正文/标注会落空")
            self.assertIn("第 7 页", one)

    def test_gate_allowlists_the_endpoint(self) -> None:
        """网关放行名单必须含本端点,否则合并书上报被 501 打死(本次真实故障)。"""
        src = (ROOT / "_server_deploy" / "pdf_reader.py").read_text(encoding="utf-8")
        head = src.split("_VB_ADAPTED", 1)[0]
        self.assertIn("pdf_reader.pdf_api_active_reading", head)


class ContextSyncApiTest(unittest.TestCase):
    """后端:总开关 + active-reading 端点的 fail-closed 与校验。"""

    @unittest.skipIf(
        os.name == "nt",
        "Pi Flask integration imports fcntl; run this endpoint test on Pi",
    )
    def test_switch_gates_writes_and_clears_on_disable(self) -> None:
        script = textwrap.dedent(
            f"""
            import json, sys, tempfile
            from pathlib import Path
            root = Path({str(ROOT)!r})
            sys.path[:0] = [str(root / "_server_deploy"), str(root / "scripts")]
            import app as module
            import pdf_reader as reader

            tmp = tempfile.mkdtemp(prefix="bw-ctx-sidecar-")
            reader._READER_SIDECAR_ROOT = Path(tmp)
            reader._READER_SIDECAR_STORE = None

            with module.app.app_context():
                db = module.get_db()
                db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                           ("ctx-user", "x", "user"))
                db.commit()
                uid = db.execute("SELECT id FROM users WHERE username=?",
                                 ("ctx-user",)).fetchone()["id"]

            # 找一本真实存在的 vault 书(_safe_vault_path 要能解析)
            book = None
            for p in sorted(reader.OBSIDIAN_ROOT.rglob("*.pdf"))[:1]:
                book = p.relative_to(reader.OBSIDIAN_ROOT.resolve()).as_posix()
            assert book, "vault 里找不到 pdf,无法测真实路径校验"

            c = module.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = uid

            # 1) 默认关
            r = c.get("/pdf/api/context-sync").get_json()
            assert r["ok"] and r["enabled"] is False, r
            assert r["deliveryMode"] == "legacy-inject", r

            # 2) 关着时上报 → fail-closed 409,且读不到任何 active
            r = c.post("/pdf/api/active-reading",
                       json={{"kind": "pdf", "file": book, "pos": 5}})
            assert r.status_code == 409, (r.status_code, r.get_json())
            assert c.get("/pdf/api/active-reading").get_json()["active"] is None

            # 3) 显式选 snapshot；旧客户端只传 enabled 时必须保留，不可静默切回旧注入
            mode = c.post("/pdf/api/context-sync", json={{
                "enabled": True, "deliveryMode": "snapshot-mcp"
            }}).get_json()
            assert mode["ok"] and mode["deliveryMode"] == "snapshot-mcp", mode
            mode = c.post("/pdf/api/context-sync", json={{"enabled": True}}).get_json()
            assert mode["deliveryMode"] == "snapshot-mcp", mode
            bad_mode = c.post("/pdf/api/context-sync", json={{
                "enabled": True, "deliveryMode": "both"
            }})
            assert bad_mode.status_code == 400, (bad_mode.status_code, bad_mode.get_json())
            assert c.get("/pdf/api/context-sync").get_json()["deliveryMode"] == "snapshot-mcp"

            # 4) 上报 → 立刻新鲜可读
            r = c.post("/pdf/api/active-reading",
                       json={{"kind": "pdf", "file": book, "pos": 57,
                             "title": "T", "selection": "S"}})
            assert r.status_code == 200, (r.status_code, r.get_json())
            ack = r.get_json()
            assert ack["canonical"] == {{
                "kind": "pdf", "file": book, "page": 57,
                "viewFile": None, "viewPage": None,
            }}, ack
            g = c.get("/pdf/api/active-reading").get_json()
            assert g["fresh"] is True and g["age_sec"] <= 2, g
            assert g["active"]["file"] == book and g["active"]["pos"] == 57, g
            assert g["active"]["title"] == "T" and g["active"]["selection"] == "S", g

            # 5) vbook ACK 必须同时给真实卷页和原视图坐标；客户端据此做精确绑定。
            class FakeVB:
                class VbookError(Exception):
                    pass

                @staticmethod
                def is_view_ref(value):
                    return value == "vbook:g_test"

                @staticmethod
                def get(value):
                    assert value == "vbook:g_test"
                    return {{}}

                @staticmethod
                def resolve_view(value, page, revision=None):
                    assert value == "vbook:g_test" and int(page) == 31
                    return book, 7

            original_vb = reader.VB
            reader.VB = FakeVB
            try:
                vb = c.post("/pdf/api/active-reading", json={{
                    "kind": "pdf", "file": "vbook:g_test", "pos": 31,
                    "selection": "V",
                }})
                assert vb.status_code == 200, (vb.status_code, vb.get_json())
                assert vb.get_json()["canonical"] == {{
                    "kind": "pdf", "file": book, "page": 7,
                    "viewFile": "vbook:g_test", "viewPage": 31,
                }}, vb.get_json()
            finally:
                reader.VB = original_vb

            # 6) 输入校验
            assert c.post("/pdf/api/active-reading",
                          json={{"kind": "bogus", "file": book}}).status_code == 400
            assert c.post("/pdf/api/active-reading",
                          json={{"kind": "pdf", "file": "../../etc/passwd"}}).status_code == 404
            assert c.post("/pdf/api/active-reading",
                          json={{"kind": "pdf", "file": book, "pos": "x"}}).status_code == 400
            assert c.post("/pdf/api/active-reading",
                          json={{"kind": "web", "url": "javascript:alert(1)"}}).status_code == 400
            assert c.post("/pdf/api/active-reading",
                          json={{"kind": "web", "url": "https://ex.com/a"}}).status_code == 200

            # 7) 关 → 活动状态被清空(否则快照会继续拿最后一条当「当前」)
            assert c.post("/pdf/api/context-sync", json={{"enabled": False}}).get_json()["ok"]
            g = c.get("/pdf/api/active-reading").get_json()
            assert g["enabled"] is False and g["active"] is None, g
            print("OK")
            """
        )
        with tempfile.TemporaryDirectory(prefix="bw-ctx-api-test-") as data:
            env = os.environ.copy()
            env.update(
                SECRET_KEY="ctx-test-secret-32-bytes-minimum",
                WEBAPP_DATA=data,
                SESSION_COOKIE_SECURE="0",
            )
            r = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                               text=True, capture_output=True, check=False)
        self.assertEqual(r.returncode, 0, msg=(r.stdout + "\n" + r.stderr).strip())


class FrontendContractTest(unittest.TestCase):
    """前端行为契约:在 node 里加载真实 rc-core.js 跑 gate/节流/在途/换书/关闭。"""

    def test_rc_ctxsync_behaviour(self) -> None:
        harness = ROOT / "tests" / "ctx_sync" / "rc_ctxsync_harness.js"
        r = subprocess.run(["node", str(harness)], cwd=ROOT, text=True,
                           capture_output=True, check=False)
        self.assertEqual(r.returncode, 0, msg=(r.stdout + "\n" + r.stderr).strip())
        self.assertIn("ALL PASS", r.stdout)


class PushDaemonGateTest(unittest.TestCase):
    """Pi→Windows 方向也归同一把开关管(不能出现前端停了、后台还在推)。"""

    def test_push_daemon_reads_same_switch(self) -> None:
        src = (ROOT / "scripts" / "push_reader_context_to_pc.py").read_text(encoding="utf-8")
        self.assertIn("SNAP._legacy_push_enabled()", src)
        self.assertIn("reader-active.json", src, "活动状态变化必须能触发推送")
        self.assertIn("reader-context-sync.json", src, "开关变化本身也要反映到快照")

    def test_snapshot_mode_disables_only_the_legacy_pi_push(self) -> None:
        snap = _load_snapshot_module()
        with tempfile.TemporaryDirectory(prefix="bw-ctx-mode-") as tmp:
            st = Path(tmp)
            snap.ST = st
            snap.SIDECAR_ACCT = None
            switch = st / "reader-context-sync.json"
            switch.write_text(
                json.dumps({"enabled": True}),
                encoding="utf-8",
            )
            self.assertTrue(
                snap._legacy_push_enabled(),
                "旧开关文件没有模式字段时必须保持原行为",
            )
            switch.write_text(
                json.dumps({
                    "enabled": True,
                    "deliveryMode": "legacy-inject",
                }),
                encoding="utf-8",
            )
            self.assertTrue(snap._legacy_push_enabled())
            switch.write_text(
                json.dumps({
                    "enabled": True,
                    "deliveryMode": "snapshot-mcp",
                }),
                encoding="utf-8",
            )
            self.assertFalse(
                snap._legacy_push_enabled(),
                "MCP 模式必须停止 Pi→Windows 的旧文字注入末端",
            )


if __name__ == "__main__":
    unittest.main()
