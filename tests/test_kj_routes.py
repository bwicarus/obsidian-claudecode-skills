"""/kj/api/* 路由：登录守卫 + 一条完整的出题→判分链路走 HTTP。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
sys.path.insert(0, str(ROOT / "scripts"))

import kj_nodes  # noqa: E402
from kj.service import KJService  # noqa: E402


class KJRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kjr"))
        kj_nodes._SVC = KJService(self.tmp / "kj.db", render=False, actor="test")
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="test")
        kj_nodes.register_kj_nodes(app)
        self.client = app.test_client()

    def tearDown(self) -> None:
        kj_nodes._SVC.close()
        kj_nodes._SVC = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self) -> None:
        with self.client.session_transaction() as s:
            s["user_id"] = 7

    def test_requires_login(self):
        self.assertEqual(self.client.get("/kj/api/search?q=x").status_code, 401)
        self.assertEqual(self.client.post("/kj/api/register", json={"type": "node", "name": "x"}).status_code, 401)

    def test_full_flow_over_http(self):
        self.login()
        a = self.client.post("/kj/api/register", json={"type": "node", "name": "向量空间", "aliases": ["vector space"]}).get_json()
        b = self.client.post("/kj/api/register", json={"type": "node", "name": "向量加法"}).get_json()
        self.assertTrue(a["ok"] and b["ok"])
        rel = self.client.post("/kj/api/relation", json={"from": b["node_id"], "to": a["node_id"], "relation_type": "prereq", "evidence": "定义用到"}).get_json()
        self.assertTrue(rel["ok"], rel)
        bad = self.client.post("/kj/api/relation", json={"from": a["node_id"], "to": b["node_id"], "relation_type": "prereq", "evidence": "环"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.get_json()["code"], "prereq_cycle")
        d = self.client.post("/kj/api/register", json={"type": "definition", "node_id": a["node_id"], "text": "八条公理",
                                                       "source": {"kind": "pdf", "book": "LADR", "page": 12}}).get_json()
        self.assertTrue(d["ok"], d)
        q = self.client.post("/kj/api/quiz", json={"target_node": a["node_id"], "items": [
            {"item_id": "p1", "question": "?", "node_ids": [b["node_id"]]}, {"item_id": "p2", "question": "?", "node_ids": [b["node_id"]]}]}).get_json()
        self.assertTrue(q["ok"], q)
        res = self.client.post(f"/kj/api/quiz/{q['quiz_id']}/result", json={"results": [{"item_id": "p1", "result": "wrong"}, {"item_id": "p2", "result": "wrong"}]}).get_json()
        self.assertEqual(res["conclusion"], "prereq_weak")
        detail = self.client.get(f"/kj/api/node/{a['node_id']}").get_json()
        self.assertEqual(detail["readiness"]["readiness"], "needs_basics")
        self.assertEqual(detail["readiness"]["weak_prereqs"][0]["id"], b["node_id"])
        self.assertEqual(self.client.get("/kj/api/node/kj:0000000000").status_code, 404)
        s = self.client.get("/kj/api/search?q=vector").get_json()
        self.assertEqual(s["local"][0]["id"], a["node_id"])
        sa = self.client.post("/kj/api/self-assess", json={"node_id": b["node_id"], "value": 0.9}).get_json()
        self.assertTrue(sa["ok"], sa)
        self.assertEqual(self.client.get(f"/kj/api/node/{a['node_id']}").get_json()["readiness"]["readiness"], "ready")
        stats = self.client.get("/kj/api/stats").get_json()
        self.assertEqual((stats["nodes"], stats["prereq_relations"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
