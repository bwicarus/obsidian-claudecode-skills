"""KG 只读导出的行为合同。

这个端点的全部意义是让电脑上的副本可信。两件事因此不能出错：

  · **只出图，不出掌握度**。掌握度跟人走、两边都会写；混进这条只读通道，
    电脑侧会拿到一份看似权威、实则可能过时的学习状态，而它没有任何办法
    知道自己拿到的是旧的。
  · **修订号按内容算**。重跑一次建图但内容没变，不该让所有副本重下一遍；
    反过来，内容变了必须变号，否则副本会一直用着旧图而自以为最新。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "_server_deploy" / "kg_export.py"

try:
    import flask
except Exception:  # pragma: no cover
    flask = None


def _load():
    spec = importlib.util.spec_from_file_location("kg_export_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


KG = _load() if MODULE_PATH.exists() else None

SAMPLE = {
    "book": "LADR",
    "note_prefix": "000-",
    "pdf": "资源/books/000-LADR/000-LADR4eChinese.pdf",
    "nodes": [
        {
            "id": "ladr.l2.1", "name": "直和", "level": 2, "pages": [12],
            "summary": "和中每元素唯一表示", "numeric_label": "1.41",
            # 以下都属于学习者，不该出现在只读图里
            "mastery": 0.8, "mastery_level": 4, "mastery_inferred": True,
            "state": "mastered", "containing_notes": ["000-abc.md"],
            "note_ref": "000-abc.md", "note_ref_ai_verified": True,
            "card_refs": [123], "has_cards": True,
        },
    ],
    "edges": [{"from": "ladr.l2.1", "to": "ladr.l2.2", "kind": "prereq"}],
    "_note_to_covered_l2": {"000-abc.md": ["ladr.l2.1"]},
    "_rejected_links": {"000-x.md": ["ladr.l2.9"]},
    "_archive_suggestions": ["ladr.l2.7"],
}


@unittest.skipIf(KG is None, "模块不在此工作树")
class GraphOnlyTest(unittest.TestCase):
    def test_learner_fields_are_stripped(self):
        graph = KG.graph_only(SAMPLE)
        node = graph["nodes"][0]
        for field in (
            "mastery", "mastery_level", "mastery_inferred", "state",
            "containing_notes", "note_ref", "note_ref_ai_verified",
            "card_refs", "has_cards",
        ):
            self.assertNotIn(
                field, node,
                f"{field} 跟人走，混进只读图会让副本无法判断自己是否过时",
            )
        for field in ("_note_to_covered_l2", "_rejected_links", "_archive_suggestions"):
            self.assertNotIn(field, graph)

    def test_graph_structure_survives(self):
        # 剥掉学习者字段之后，图本身必须完整 —— 否则副本失去了它存在的理由。
        graph = KG.graph_only(SAMPLE)
        node = graph["nodes"][0]
        for field in ("id", "name", "level", "pages", "summary", "numeric_label"):
            self.assertIn(field, node)
        self.assertEqual(graph["edges"], SAMPLE["edges"])
        self.assertEqual(graph["book"], "LADR")
        self.assertEqual(graph["pdf"], SAMPLE["pdf"])

    def test_revision_ignores_learner_changes(self):
        # 只是又复习了一遍，图没变 —— 不该让所有副本重下。
        other = json.loads(json.dumps(SAMPLE))
        other["nodes"][0]["mastery"] = 0.1
        other["nodes"][0]["state"] = "unlockable"
        other["_rejected_links"] = {}
        self.assertEqual(
            KG.revision_of(KG.graph_only(SAMPLE)),
            KG.revision_of(KG.graph_only(other)),
        )

    def test_revision_changes_when_graph_changes(self):
        # 反过来：图变了必须变号，否则副本会一直用旧图而自以为最新。
        other = json.loads(json.dumps(SAMPLE))
        other["nodes"][0]["summary"] = "改写过的说明"
        self.assertNotEqual(
            KG.revision_of(KG.graph_only(SAMPLE)),
            KG.revision_of(KG.graph_only(other)),
        )

    def test_revision_is_order_independent(self):
        # 序列化顺序不该影响修订号，否则重写一次文件就"变了"。
        other = dict(reversed(list(SAMPLE.items())))
        self.assertEqual(
            KG.revision_of(KG.graph_only(SAMPLE)),
            KG.revision_of(KG.graph_only(other)),
        )


@unittest.skipIf(KG is None or flask is None, "需要 flask")
class EndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "knowledge_graph").mkdir()
        (root / "knowledge_graph" / "LADR.json").write_text(
            json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
        (root / "knowledge_graph" / "broken.json").write_text(
            "{ not json", encoding="utf-8")
        self._original = KG._kg_dir
        KG._kg_dir = lambda: root / "knowledge_graph"

        app = flask.Flask(__name__)
        self.authorized = True
        KG.register_kg_export(app, lambda: self.authorized)
        self.client = app.test_client()

    def tearDown(self):
        KG._kg_dir = self._original
        self._tmp.cleanup()

    def test_index_lists_books_with_revisions(self):
        payload = self.client.get("/api/kg/index").get_json()
        self.assertTrue(payload["ok"])
        books = {b["book"]: b for b in payload["books"]}
        self.assertIn("LADR", books)
        self.assertEqual(books["LADR"]["nodes"], 1)
        self.assertTrue(books["LADR"]["revision"])

    def test_one_broken_book_does_not_hide_the_others(self):
        # 整个索引失败会让电脑侧以为一本书都没有 —— 那比少一本更糟。
        books = {b["book"]: b for b in
                 self.client.get("/api/kg/index").get_json()["books"]}
        self.assertEqual(books["broken"].get("error"), "unreadable")
        self.assertIn("LADR", books)

    def test_graph_download_excludes_learner_fields(self):
        payload = self.client.get("/api/kg/graph/LADR").get_json()
        self.assertFalse(payload["unchanged"])
        self.assertNotIn("mastery", payload["graph"]["nodes"][0])

    def test_since_avoids_retransmission(self):
        first = self.client.get("/api/kg/graph/LADR").get_json()
        again = self.client.get(
            f"/api/kg/graph/LADR?since={first['revision']}").get_json()
        self.assertTrue(again["unchanged"])
        self.assertNotIn("graph", again, "未变时不该重传图")

    def test_requires_authorization(self):
        self.authorized = False
        self.assertEqual(self.client.get("/api/kg/index").status_code, 401)
        self.assertEqual(self.client.get("/api/kg/graph/LADR").status_code, 401)

    def test_book_name_cannot_escape_the_directory(self):
        # 直接测校验函数，不经 HTTP —— Flask 的 <book> 本来就不匹配含 / 的值，
        # 走路由测出来的 404 是框架给的，我的校验有没有都一样，
        # 那样的断言在测框架而不是测这段代码。
        import werkzeug.exceptions
        for name in ("..", "../secret", "..\secret", ".hidden", "", "x" * 201):
            with self.subTest(name=name):
                with self.assertRaises(werkzeug.exceptions.HTTPException,
                                       msg=f"{name!r} 应被拒绝"):
                    with flask.Flask(__name__).test_request_context():
                        KG._safe_book(name)

    def test_ordinary_book_name_passes(self):
        # 反向：正常书名不能被误伤，否则这道校验会让所有书都下不下来。
        with flask.Flask(__name__).test_request_context():
            for name in ("LADR", "000-LADR", "线性代数", "a.b"):
                self.assertEqual(KG._safe_book(name), name)

    def test_missing_book_is_404_not_empty_graph(self):
        # 回一个空图会被读成"这本书没有知识点"，那是一句错的断言。
        self.assertEqual(
            self.client.get("/api/kg/graph/nonexistent").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class NonBookFilesTest(unittest.TestCase):
    """目录里不止书。

    建图会留下 `.bak` / `.pre` / `.scan` 快照，另有一份 `kg_audit.json`。
    它们结构与图相同，所以不会解析失败 —— 只会静静地变成书架上多出来的
    几本不存在的书，然后被同步到电脑上，然后被 AI 当成用户在读的书。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "knowledge_graph"
        self.dir.mkdir(parents=True)
        self._original = KG._kg_dir
        KG._kg_dir = lambda: self.dir
        for name in ("LADR.json", "LADR.bak.json", "LADR.pre.json",
                     "LADR.scan.json", "kg_audit.json"):
            (self.dir / name).write_text(
                json.dumps({"book": "x", "nodes": [], "edges": []}),
                encoding="utf-8")

    def tearDown(self):
        KG._kg_dir = self._original
        self._tmp.cleanup()

    def test_index_lists_only_real_books(self):
        names = [p.stem for p in KG._book_files(self.dir)]
        self.assertEqual(names, ["LADR"],
                         "备份与审计文件不是书，不该出现在书架上")

    def test_named_lookup_agrees_with_the_index(self):
        # 索引说没有、直接点名却拿得到，是两个端点各说各话。
        for excluded in ("LADR.bak", "kg_audit", "LADR.scan"):
            with self.subTest(book=excluded):
                with self.assertRaises(Exception):
                    KG._safe_book(excluded)
        self.assertEqual(KG._safe_book("LADR"), "LADR")
