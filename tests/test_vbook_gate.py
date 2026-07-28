#!/usr/bin/env python3
"""转换层v2 第3步竖切测试:服务端咽喉端到端——翻译正确性 + **磁盘落盘断言**(全局页只允许写进正确成员)
+ fail-closed(501)/stale(409)/range(400)。用 .sandbox 临时分卷组(不入列表/索引),teardown 全清。"""
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "_server_deploy"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

try:
    import fitz
except Exception:
    fitz = None


def _mk_pdf(path, pages):
    d = fitz.open()
    for i in range(pages):
        pg = d.new_page(width=200, height=280)
        pg.insert_text((20, 40), "gatepage %d unique" % (i + 1))
    d.save(str(path))
    d.close()


@unittest.skipIf(fitz is None, "需要 PyMuPDF")
class VbookGateSlice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("SECRET_KEY", "t")
        os.environ.setdefault("WEBAPP_DATA", tempfile.mkdtemp())
        os.environ.setdefault("CLAUDE_PROJECT", str(ROOT))
        import app as A
        import vbook as VB
        import book_groups as BG
        cls.A, cls.VB, cls.BG = A, VB, BG
        cls.PR = sys.modules.get("pdf_reader")
        cls._old_pdf_obsidian_root = cls.PR.OBSIDIAN_ROOT
        cls.PR.OBSIDIAN_ROOT = BG.VAULT
        cls._old_claim_authorizer = A.app.extensions.get(
            "reader_legacy_sidecar_claim_authorizer"
        )
        A.app.extensions["reader_legacy_sidecar_claim_authorizer"] = (
            lambda _identity: False
        )
        cls.dir = BG.VAULT / "资源" / "uploads" / ".sandbox"
        cls.dir.mkdir(parents=True, exist_ok=True)
        cls.r1 = "资源/uploads/.sandbox/门测part1.pdf"
        cls.r2 = "资源/uploads/.sandbox/门测part2.pdf"
        _mk_pdf(BG.VAULT / cls.r1, 2)
        _mk_pdf(BG.VAULT / cls.r2, 3)
        g = VB.refresh(cls.r1)
        assert g and g["total"] == 5
        cls.g = g
        cls.ref = VB.VIEW_PREFIX + g["group_id"]
        cls.c = A.app.test_client()
        with A.app.app_context():
            db = A.get_db()
            db.execute(
                "INSERT OR IGNORE INTO users(username,password_hash,role) "
                "VALUES(?,?,?)",
                ("vbook-gate-test", "x", "user"),
            )
            db.commit()
            row = db.execute(
                "SELECT id FROM users WHERE username=?",
                ("vbook-gate-test",),
            ).fetchone()
            cls.user_id = int(row["id"])
            cls.storage_namespace = A._reader_storage_namespace(cls.user_id)
        from reader_sidecar_store import ReaderStorageIdentity
        cls.identity = ReaderStorageIdentity(
            cls.user_id,
            cls.storage_namespace,
        )
        with cls.c.session_transaction() as s:
            s["user_id"] = cls.user_id
            s["username"] = "vbook-gate-test"

    @classmethod
    def tearDownClass(cls):
        cls.PR.OBSIDIAN_ROOT = cls._old_pdf_obsidian_root
        cls.A.app.extensions["reader_legacy_sidecar_claim_authorizer"] = (
            cls._old_claim_authorizer
        )
        for r in (cls.r1, cls.r2):
            try:
                (cls.BG.VAULT / r).unlink()
            except OSError:
                pass
            try:
                cls.PR._hl_path(r, cls.identity).unlink()
            except Exception:
                pass
        try:
            data = json.loads(cls.VB.STORE.read_text("utf-8"))
            data.get("groups", {}).pop(cls.g["group_id"], None)
            cls.VB.STORE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        except Exception:
            pass

    def _q(self, path, **kw):
        return path + "?" + urllib.parse.urlencode(kw)

    def test_1_book_meta_merged(self):
        d = self.c.get(self._q("/pdf/api/book-meta", file=self.ref)).get_json()
        self.assertTrue(d.get("ok"))
        self.assertEqual(d["page_count"], 5)
        self.assertEqual(d["vbook"]["revision"], self.g["revision"])
        self.assertEqual(len(d["vbook"]["members"]), 2)

    def test_2_page_chars_equivalence(self):
        via_v = self.c.get(self._q("/pdf/api/page-chars", file=self.ref, page=4)).get_json()
        direct = self.c.get(self._q("/pdf/api/page-chars", file=self.r2, page=2)).get_json()
        self.assertEqual(json.dumps(via_v, sort_keys=True), json.dumps(direct, sort_keys=True),
                         "vbook 全局4 必须与 part2 局部2 完全等价")

    def test_3_write_lands_in_correct_member_on_disk(self):
        marker = "vbslice标记高亮"
        r = self.c.post("/pdf/api/highlights", json={
            "file": self.ref, "page": 4,   # 全局4 = part2 局部2
            "rects": [[10, 10, 60, 30]], "color": "#fff59d", "text": marker})
        self.assertTrue(r.get_json().get("ok"), r.get_json())
        # 磁盘断言:只允许落在 part2 sidecar,且 page 记的是局部 2
        hl2 = self.PR._hl_path(self.r2, self.identity)
        self.assertTrue(hl2.exists(), "高亮必须落 part2 的 sidecar")
        raw2 = hl2.read_text("utf-8")
        self.assertIn(marker, raw2)
        self.assertIn('"page": 2', raw2.replace('"page":2', '"page": 2'))
        hl1 = self.PR._hl_path(self.r1, self.identity)
        self.assertFalse(hl1.exists() and marker in hl1.read_text("utf-8"),
                         "part1 sidecar 绝不能被污染")
        # 读回:同一条高亮——vbook 视角页码=全局4,直连成员=局部2(v2 语义:视图全局/真相局部)
        via_v = self.c.get(self._q("/pdf/api/highlights", file=self.ref)).get_json()
        direct = self.c.get(self._q("/pdf/api/highlights", file=self.r2)).get_json()
        hv = next(h for h in via_v["highlights"] if h.get("text") == marker)
        hd = next(h for h in direct["highlights"] if h.get("text") == marker)
        self.assertEqual(hv["id"], hd["id"])
        self.assertEqual((hv["page"], hd["page"]), (4, 2))

    def test_4_failclosed_stale_range_unknown(self):
        # 未适配端点收到 vbook → 501(绝不静默写错卷)
        r = self.c.post("/pdf/api/compress-async", json={"file": self.ref})
        self.assertEqual(r.status_code, 501)
        self.assertEqual(r.get_json().get("error"), "vbook_unadapted")
        # stale revision → 409
        r = self.c.get(self._q("/pdf/api/page-chars", file=self.ref, page=1, vrev="r_deadbeef00"))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json().get("error"), "manifest_stale")
        # 越界 → 400
        r = self.c.get(self._q("/pdf/api/page-chars", file=self.ref, page=99))
        self.assertEqual(r.status_code, 400)
        # 未知组 → 404
        r = self.c.get(self._q("/pdf/api/page-chars", file="vbook:g_nope000000", page=1))
        self.assertEqual(r.status_code, 404)

    def test_45_fanin_and_id_ops(self):
        # 整本 GET 扇入:vbook 视角看到全组高亮,页码=全局
        d = self.c.get(self._q("/pdf/api/highlights", file=self.ref)).get_json()
        self.assertTrue(d.get("ok"))
        pages = [h["page"] for h in d["highlights"] if h.get("text") == "vbslice标记高亮"]
        self.assertEqual(pages, [4], "扇入页码必须是全局 4(part2 局部2+offset2)")
        hid = next(h["id"] for h in d["highlights"] if h.get("text") == "vbslice标记高亮")
        # PATCH 按 id 跨卷定位
        r = self.c.patch("/pdf/api/highlights", json={"file": self.ref, "id": hid, "note": "vb改"})
        self.assertTrue(r.get_json().get("ok"), r.get_json())
        self.assertIn(
            "vb改",
            self.PR._hl_path(self.r2, self.identity).read_text("utf-8"),
        )
        # DELETE 按 id 跨卷定位 → part2 sidecar 清空
        r = self.c.delete("/pdf/api/highlights", json={"file": self.ref, "id": hid})
        self.assertTrue(r.get_json().get("ok"))
        self.assertNotIn(
            "vbslice标记高亮",
            self.PR._hl_path(self.r2, self.identity).read_text("utf-8"),
        )

    def test_46_notes_full_cycle(self):
        # 建在全局5(=part2 局部3):anchor.page 由 handler 翻译
        r = self.c.post("/pdf/api/notes", json={"file": self.ref,
                        "anchor": {"kind": "pdf", "page": 5, "x": 0.3, "y": 0.4}, "text": "vb便签"})
        d = r.get_json()
        self.assertTrue(d.get("ok"), d)
        nid = d["id"]
        raw2 = (
            self.PR._notes_path(self.r2, self.identity).read_text("utf-8")
            if hasattr(self.PR, "_notes_path")
            else None
        )
        # 便签 sidecar 路径按 _notes_load 读回验证(不依赖内部路径函数名)
        import json as _j
        notes2 = self.PR._notes_load(self.r2, self.identity)
        self.assertTrue(any(n["id"] == nid and n["anchor"]["page"] == 3 for n in notes2),
                        "便签必须落 part2、anchor.page=局部3")
        self.assertFalse(
            any(
                n.get("id") == nid
                for n in self.PR._notes_load(self.r1, self.identity)
            )
        )
        # GET 扇入:anchor.page 反译回全局 5
        d = self.c.get(self._q("/pdf/api/notes", file=self.ref)).get_json()
        self.assertTrue(any(n["id"] == nid and n["anchor"]["page"] == 5 for n in d["notes"]))
        # PATCH 同卷移动 ok;跨卷移动 501
        r = self.c.patch("/pdf/api/notes", json={"file": self.ref, "id": nid,
                        "anchor": {"kind": "pdf", "page": 4, "x": 0.1, "y": 0.1}})
        self.assertTrue(r.get_json().get("ok"), r.get_json())
        r = self.c.patch("/pdf/api/notes", json={"file": self.ref, "id": nid,
                        "anchor": {"kind": "pdf", "page": 1, "x": 0.1, "y": 0.1}})
        self.assertEqual(r.status_code, 501, "跨卷移动便签应拒绝")
        # DELETE
        r = self.c.delete(self._q("/pdf/api/notes", file=self.ref, id=nid))
        self.assertTrue(r.get_json().get("ok"))
        self.assertFalse(
            any(
                n.get("id") == nid
                for n in self.PR._notes_load(self.r2, self.identity)
            )
        )

    def test_47_userpages_adapted(self):
        """2026-07-19 语义反转:合并书 userpages 从"禁用"改为完整适配(用户实锤:
        默认入口=合并视图,禁用等于编辑按钮消失/页面固定化/建页失效)。
        POST 按全局 after 定位成员卷、局部化落盘;GET 扇入并全局化。"""
        r = self.c.post("/pdf/api/userpages", json={"file": self.ref, "after": 4, "md": "x"})
        self.assertEqual(r.status_code, 200, r.get_json())
        pid = r.get_json()["id"]
        try:
            # 全局 after=4 → 第 2 卷(每卷 2 页)局部 after=2
            recs = self.PR._upages_load(self.r2)
            hit = [x for x in recs if x.get("id") == pid]
            self.assertEqual([x.get("after") for x in hit], [2], "应落盘第2卷、局部 after")
            self.assertFalse(any(x.get("id") == pid for x in self.PR._upages_load(self.r1)),
                             "第1卷不该有")
            d = self.c.get(self._q("/pdf/api/userpages", file=self.ref)).get_json()
            mine = [p for p in d["pages"] if p["id"] == pid]
            self.assertEqual([p.get("after") for p in mine], [4], "GET 应报全局 after")
        finally:
            self.c.delete(self._q("/pdf/api/userpages", file=self.ref, id=pid))
        self.assertFalse(any(x.get("id") == pid for x in self.PR._upages_load(self.r2)),
                         "DELETE 应跨卷定位清理")

    def test_5_view_opens(self):
        r = self.c.get(self._q("/pdf/view", file=self.ref))
        self.assertEqual(r.status_code, 200)
        h = r.data.decode("utf-8")
        self.assertIn("合卷", h)
        self.assertIn(self.ref, h)


if __name__ == "__main__":
    unittest.main()
