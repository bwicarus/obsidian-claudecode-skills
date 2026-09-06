"""KJ 公共目录（Wikidata）与 Anki 接线。全部离线：在线取数与 AnkiConnect 都用假函数。"""
from __future__ import annotations

import gzip
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from kj import anki_sync as AK  # noqa: E402
from kj import wikidata as WD  # noqa: E402
from kj.service import KJService  # noqa: E402


class WikidataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kjw"))
        self.svc = KJService(self.tmp / "kj.db", render=False, actor="test")
        self.L = self.svc.ledger

    def tearDown(self) -> None:
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_import_minimal_index_with_filters(self):
        rows = [
            {"id": "Q125977", "labels": {"en": "vector space", "zh": "向量空间"}, "descriptions": {"en": "algebraic structure"},
             "aliases": {"en": ["linear space"]}, "relations": [["P279", "Q1936384", "normal"], ["P527", "Q207643", "normal"]]},
            {"id": "Q207643", "labels": {"en": "linear map"}, "descriptions": {}, "aliases": {}, "relations": [["P31", "Q1936384", "deprecated"]]},
            {"id": "Q1936384", "labels": {"en": "mathematical concept", "zh": "数学概念"}, "descriptions": {}, "aliases": {}, "relations": []},
        ]
        p = self.tmp / "minimal-index.jsonl.gz"
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        stats = WD.import_minimal_index(self.L, p, require_lang="zh")
        self.assertEqual((stats["seen"], stats["kept"]), (3, 2))
        self.assertIsNone(WD.entity(self.L, "Q207643"))
        WD.import_minimal_index(self.L, p, only_qids={"Q207643"})
        self.assertEqual(WD.claims_of(self.L, "Q207643"), [])   # deprecated 不进
        e = WD.entity(self.L, "Q125977")
        self.assertEqual((e["label"], e["aliases"]["en"]), ("向量空间", ["linear space"]))
        self.assertEqual([p["qid"] for p in WD.path_up(self.L, "Q125977")], ["Q1936384"])
        hits = WD.search_public(self.L, "linear")
        self.assertEqual({h["qid"] for h in hits}, {"Q125977", "Q207643"})

    def test_parse_entity_json_and_fetch_via_fake_http(self):
        doc = {"entities": {"Q1": {"labels": {"en": {"language": "en", "value": "one"}, "zh-hans": {"language": "zh-hans", "value": "一"}},
                                   "descriptions": {}, "aliases": {"ja": [{"language": "ja", "value": "いち"}]},
                                   "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q2"}}}, "rank": "normal"}],
                                              "P1082": [{"mainsnak": {"datavalue": {"value": {"amount": "+1"}}}, "rank": "normal"}]}}}}
        e = WD.fetch_entity(self.L, "Q1", fetcher=lambda url, t: doc)
        self.assertEqual((e["label_zh"], e["aliases"]["ja"]), ("一", ["いち"]))
        self.assertEqual(WD.claims_of(self.L, "Q1"), [("P31", "Q2", "normal")])
        self.assertEqual(WD.fetch_entity(self.L, "Q1", fetcher=lambda url, t: {"entities": {}})["qid"], "Q1")  # 已有就不再取

    def test_bind_qid_generates_and_retracts_auto_relations(self):
        WD.upsert_entity(self.L, "Q125977", {"en": "vector space"}, {}, {}, [("P527", "Q207643", "normal")], source="test")
        WD.upsert_entity(self.L, "Q207643", {"en": "linear map"}, {}, {}, [], source="test")
        WD.upsert_entity(self.L, "Q99", {"en": "other"}, {}, {}, [("P279", "Q207643", "normal")], source="test")
        a = self.svc.create_node(name="向量空间", qid="Q125977")["node_id"]
        c = self.svc.create_node(name="线性映射")["node_id"]
        r = self.svc.bind_qid(c, "Q207643", fetch_public=False)
        self.assertEqual(r["auto_relations"], 1)
        rels = self.L.relations(c)
        self.assertEqual([(x["from_id"], x["to_id"], x["type"], x["origin"]) for x in rels], [(c, a, "part_of", "wikidata")])
        self.assertEqual(self.svc.bind_qid(a, "Q207643", fetch_public=False)["code"], "qid_taken")
        r2 = self.svc.bind_qid(c, "Q99", fetch_public=False)
        self.assertEqual((r2["retracted_auto_relations"], r2["auto_relations"]), (1, 0))
        self.assertEqual(self.L.relations(c), [])
        self.assertEqual(len(self.L.relations(c, status="retracted")), 1)
        self.assertEqual(self.svc.create_node(name="重复", qid="Q99")["code"], "qid_taken")
        # 公共关系永不生成 prereq
        self.assertTrue(all(t != "prereq" for t, _ in WD.PROP_MAP.values()))


class AnkiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kja"))
        self.svc = KJService(self.tmp / "kj.db", render=False, actor="test")
        self.a = self.svc.create_node(name="A")["node_id"]
        self.calls: list[tuple] = []

    def tearDown(self) -> None:
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_request(self, url, action, params=None, timeout=15):
        self.calls.append((action, params))
        return {"createDeck": 1, "addNote": 1001, "findCards": [5001], "changeDeck": None,
                "notesInfo": [{"noteId": 1001, "cards": [5001]}],
                "cardsInfo": [{"cardId": 5001, "type": 2, "queue": 2, "interval": 30, "mod": 1_700_000_000, "reps": 5, "lapses": 0, "factor": 2500}]}[action]

    def test_make_card_binds_node_and_changes_deck(self):
        r = self.svc.make_card(node_ids=[self.a], front="Q", back="A", request=self.fake_request)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["anki_note_id"], r["anki_card_ids"], r["card_key"]), (1001, [5001], "anki:1001"))
        self.assertEqual([c[0] for c in self.calls], ["createDeck", "addNote", "findCards", "changeDeck"])
        self.assertEqual(self.calls[1][1]["note"]["deckName"], "KJ")
        self.assertIn("kj::kj_" + self.a.split(":")[1], self.calls[1][1]["note"]["tags"])
        back = self.calls[1][1]["note"]["fields"]["Back"]
        self.assertTrue(back.startswith("A<hr>"), back)
        self.assertIn('href="obsidian://open?vault=', back)        # 复习卡 UI 的来源栏据此显示"打开节点"
        self.assertIn("&amp;file=KJ/", back)
        self.assertEqual(self.svc.make_card(node_ids=[], front="Q", back="A", request=self.fake_request)["code"], "missing_node")
        self.assertEqual(self.svc.node(self.a)["cards"][0]["anki_note_id"], 1001)

    def test_sync_snapshots_feeds_mastery_and_dedupes(self):
        self.svc.make_card(node_ids=[self.a], front="Q", back="A", request=self.fake_request)
        res = self.svc.anki_sync(request=self.fake_request, fsrs=lambda url, ids: {})
        self.assertEqual((res["cards"], res["snapshots"], res["nodes"]), (1, 1, [self.a]))
        m = self.svc.ledger.mastery_row(self.a)
        self.assertIsNotNone(m["value"])
        self.assertEqual(m["detail"]["signals"][-1]["kind"], "anki")
        again = self.svc.anki_sync(request=self.fake_request, fsrs=lambda url, ids: {})
        self.assertEqual(again["snapshots"], 0)
        self.assertEqual(AK.card_ids_of(self.svc.ledger, "anki:1001"), [5001])


if __name__ == "__main__":
    unittest.main()
