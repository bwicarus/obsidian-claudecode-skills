"""KG 本地副本同步的行为合同。

这个脚本存在的意义是让电脑上的 AI 有一份可用的图。所以它的失败方式比成功路径
更要紧：

  · **拉不到时保留旧副本**。"截至昨天的图"远好过没有图 —— 后者会让 AI
    回答"这本书没有知识点"，那是一句关于用户书架的假话。
  · **状态要写下来**。副本必须能回答"我这份是什么时候的"。不知道自己多旧的
    数据，比明确过时的数据更危险。
  · **缺 token 要明说**。静默失败会被当成"服务端没有图谱"。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "sync_kg_from_pi.py"


def _load():
    spec = importlib.util.spec_from_file_location("sync_kg_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SYNC = _load() if MODULE_PATH.exists() else None

GRAPH = {
    "book": "LADR",
    "nodes": [{"id": "ladr.l2.1", "name": "直和", "level": 2, "pages": [12]}],
    "edges": [],
}


@unittest.skipIf(SYNC is None, "脚本不在此工作树")
class SyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "knowledge_graph").mkdir()
        self._original = SYNC.mirror_dir
        SYNC.mirror_dir = lambda: self.root / "knowledge_graph"
        self.responses = {}
        self.calls = []

        def fake_request(url, token, timeout):
            self.calls.append(url)
            for pattern, value in self.responses.items():
                if pattern in url:
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise RuntimeError(f"未预设的请求: {url}")

        self._original_request = SYNC._request
        SYNC._request = fake_request

    def tearDown(self):
        SYNC.mirror_dir = self._original
        SYNC._request = self._original_request
        self._tmp.cleanup()

    def _manifest(self):
        path = self.root / "knowledge_graph" / SYNC.MANIFEST_NAME
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def test_downloads_and_records_freshness(self):
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r1", "nodes": 1}]},
            "/api/kg/graph/LADR": {
                "ok": True, "book": "LADR", "revision": "r1",
                "unchanged": False, "graph": GRAPH},
        }
        self.assertEqual(SYNC.sync("https://pi/", "tok", 5, None), 0)
        saved = json.loads(
            (self.root / "knowledge_graph" / "LADR.json").read_text("utf-8"))
        self.assertEqual(saved["nodes"][0]["id"], "ladr.l2.1")
        entry = self._manifest()["books"]["LADR"]
        self.assertEqual(entry["revision"], "r1")
        self.assertEqual(entry["status"], "ok")
        self.assertGreater(entry["syncedAtEpochSeconds"], 0,
                           "副本必须能回答自己是什么时候的")

    def test_same_revision_skips_download(self):
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r1", "nodes": 1}]},
            "/api/kg/graph/LADR": {
                "ok": True, "revision": "r1", "unchanged": False, "graph": GRAPH},
        }
        SYNC.sync("https://pi/", "tok", 5, None)
        self.calls.clear()
        SYNC.sync("https://pi/", "tok", 5, None)
        self.assertTrue(
            all("/api/kg/graph/" not in url for url in self.calls),
            "修订号未变时不该重新下载整张图",
        )

    def test_failure_keeps_the_previous_copy(self):
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r1", "nodes": 1}]},
            "/api/kg/graph/LADR": {
                "ok": True, "revision": "r1", "unchanged": False, "graph": GRAPH},
        }
        SYNC.sync("https://pi/", "tok", 5, None)
        target = self.root / "knowledge_graph" / "LADR.json"
        before = target.read_text("utf-8")

        # 下一次服务端换了修订号但下载失败
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r2", "nodes": 1}]},
            "/api/kg/graph/LADR": RuntimeError("网络中断"),
        }
        code = SYNC.sync("https://pi/", "tok", 5, None)
        self.assertEqual(code, 2, "失败要能被调用方区分")
        self.assertEqual(target.read_text("utf-8"), before,
                         "拉不到时必须保留旧副本：没有图会让 AI 说这本书没有知识点")
        entry = self._manifest()["books"]["LADR"]
        self.assertEqual(entry["status"], "stale", "过时要标出来")
        self.assertIn("网络中断", entry["lastError"], "原因要留下")
        self.assertEqual(entry["revision"], "r1", "修订号仍是本地那份的")

    def test_server_side_unreadable_book_does_not_erase_local(self):
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r1", "nodes": 1}]},
            "/api/kg/graph/LADR": {
                "ok": True, "revision": "r1", "unchanged": False, "graph": GRAPH},
        }
        SYNC.sync("https://pi/", "tok", 5, None)
        target = self.root / "knowledge_graph" / "LADR.json"

        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "error": "unreadable"}]},
        }
        SYNC.sync("https://pi/", "tok", 5, None)
        self.assertTrue(target.is_file(), "服务端这本坏了，本地那份仍然有用")

    def test_unchanged_response_still_refreshes_freshness(self):
        # 服务端说"没变"也要更新时间戳，否则副本会显得越来越旧，
        # 而实际上刚刚确认过它是最新的。
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r9", "nodes": 1}]},
            "/api/kg/graph/LADR": {
                "ok": True, "revision": "r9", "unchanged": True},
        }
        SYNC.sync("https://pi/", "tok", 5, None)
        entry = self._manifest()["books"]["LADR"]
        self.assertEqual(entry["status"], "ok")
        self.assertGreater(entry["syncedAtEpochSeconds"], 0)

    def test_broken_manifest_is_rebuilt_not_fatal(self):
        (self.root / "knowledge_graph" / SYNC.MANIFEST_NAME).write_text(
            "{ not json", encoding="utf-8")
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r1", "nodes": 1}]},
            "/api/kg/graph/LADR": {
                "ok": True, "revision": "r1", "unchanged": False, "graph": GRAPH},
        }
        self.assertEqual(SYNC.sync("https://pi/", "tok", 5, None), 0,
                         "清单坏了只该导致一次全量重拉，不该让同步停摆")

    def test_empty_server_index_does_not_delete_anything(self):
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r1", "nodes": 1}]},
            "/api/kg/graph/LADR": {
                "ok": True, "revision": "r1", "unchanged": False, "graph": GRAPH},
        }
        SYNC.sync("https://pi/", "tok", 5, None)
        target = self.root / "knowledge_graph" / "LADR.json"

        # 服务端一本都没返回（可能是它那边出了问题）—— 绝不能据此清空本地。
        self.responses = {"/api/kg/index": {"ok": True, "books": []}}
        SYNC.sync("https://pi/", "tok", 5, None)
        self.assertTrue(target.is_file(),
                        "服务端返回空不等于用户没有书，不能据此删本地副本")

    def test_only_one_book(self):
        self.responses = {
            "/api/kg/index": {"ok": True, "books": [
                {"book": "LADR", "revision": "r1"},
                {"book": "OTHER", "revision": "r2"}]},
            "/api/kg/graph/LADR": {
                "ok": True, "revision": "r1", "unchanged": False, "graph": GRAPH},
        }
        SYNC.sync("https://pi/", "tok", 5, "LADR")
        self.assertTrue(
            all("OTHER" not in url for url in self.calls if "/graph/" in url))


if __name__ == "__main__":
    unittest.main()
