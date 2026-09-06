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

    def test_legacy_public_tables_in_main_db_are_migrated_to_pub(self):
        """拆库前公共目录建在主账本里；重开时要搬进 kj-public.db 并删掉主库副本，否则遮住 pub 表、导入写错地方。"""
        import sqlite3
        self.svc.close()
        raw = sqlite3.connect(str(self.tmp / "kj.db"))
        raw.executescript("""
            CREATE TABLE public_entities(qid TEXT PRIMARY KEY, label_en TEXT, label_zh TEXT, label_ja TEXT,
                desc_en TEXT, desc_zh TEXT, desc_ja TEXT, aliases_json TEXT, fetched_at INTEGER, source TEXT);
            CREATE TABLE public_claims(qid TEXT, prop TEXT, target TEXT, rank TEXT, PRIMARY KEY(qid, prop, target));
            CREATE VIRTUAL TABLE public_fts USING fts5(qid UNINDEXED, labels, tokenize='trigram');
            INSERT INTO public_entities VALUES('Q42','Douglas Adams','道格拉斯·亚当斯','','writer','','','{}',1,'old');
            INSERT INTO public_claims VALUES('Q42','P31','Q5','normal');
        """)
        raw.commit(); raw.close()
        self.svc = KJService(self.tmp / "kj.db", render=False, actor="test")
        L = self.svc.ledger
        self.assertIsNone(L.db.execute("SELECT 1 FROM main.sqlite_master WHERE name='public_entities'").fetchone())
        e = WD.entity(L, "Q42")
        self.assertEqual((e["label"], e["source"]), ("道格拉斯·亚当斯", "old"))
        self.assertEqual(WD.claims_of(L, "Q42"), [("P31", "Q5", "normal")])
        self.assertEqual([h["qid"] for h in WD.search_public(L, "Douglas")], ["Q42"])
        # 之后的导入落到 pub（带 search_text 列），不再撞旧表
        p = self.tmp / "mini.jsonl"
        p.write_text(json.dumps({"id": "Q7", "labels": {"en": "seven", "zh": "七"}, "descriptions": {}, "aliases": {}, "relations": []}) + "\n", "utf-8")
        res = self.svc.wikidata_import(str(p))
        self.assertTrue(res["ok"], res)
        self.assertEqual(L.count("public_entities"), 2)

    def test_search_ranking_prefers_concepts_over_papers_pages_and_new_ids(self):
        """同名候选：精确标签优先；论文/消歧义页/单字条目压后；同分按 Q 号数值（老条目多为核心概念）。繁体标签当别名可被简体命中。"""
        WD.upsert_entity(self.L, "Q109753558", {"zh": "熵"}, {"zh": "CJK 中日韓文字"}, {}, [("P31", "Q53764738", "normal")], source="t")
        WD.upsert_entity(self.L, "Q45003", {"zh": "熵", "en": "entropy"}, {"zh": "热力学概念"}, {}, [("P31", "Q1936384", "normal")], source="t")
        WD.upsert_entity(self.L, "Q67189383", {"zh": "熵分光光度法研究"}, {}, {}, [("P31", "Q13442814", "normal")], source="t")
        WD.upsert_entity(self.L, "Q1", {"zh": "熵值"}, {}, {}, [("P31", "Q4167410", "normal")], source="t")
        WD.upsert_entity(self.L, "Q2397319", {"zh": "牛頓第二運動定律", "zh-hans": "牛顿第二运动定律", "en": "Newton's second law"}, {}, {}, [], source="t")
        hits = [h["qid"] for h in WD.search_public(self.L, "熵", limit=4)]
        self.assertEqual(hits[0], "Q45003")
        self.assertLess(hits.index("Q45003"), hits.index("Q109753558"))
        self.assertLess(hits.index("Q109753558"), hits.index("Q67189383"))
        self.assertEqual([h["qid"] for h in WD.search_public(self.L, "牛顿第二运动定律", limit=2)][0], "Q2397319")   # 简体别名命中繁体条目
        self.assertEqual([h["qid"] for h in WD.search_public(self.L, "牛顿第二定律", limit=2)][0], "Q2397319")      # 缩短前缀兜底
        self.assertEqual(WD.entity(self.L, "Q2397319")["aliases"]["zh"], ["牛顿第二运动定律"])
        # 标签是查询词的前缀（"拉格朗日乘数" ⊂ "拉格朗日乘数法"）要赢过标题里含这串字的论文——论文有精确前缀也不行
        WD.upsert_entity(self.L, "Q598870", {"zh": "拉格朗日乘数", "en": "Lagrange multiplier"}, {}, {}, [], source="t")
        WD.upsert_entity(self.L, "Q99408725", {"zh": "拉格朗日乘数法计算大坝可靠度"}, {}, {}, [("P31", "Q13442814", "normal")], source="t")
        self.assertEqual([h["qid"] for h in WD.search_public(self.L, "拉格朗日乘数法", limit=2)][0], "Q598870")
        if WD._zhconv is not None:
            # 目录里只有繁体 zh 标签、没有 zh-hans 的条目（导数/极限/编译器都是），简体查询靠查询侧简繁变体命中
            WD.upsert_entity(self.L, "Q29175", {"zh": "導數", "en": "derivative"}, {}, {}, [], source="t")
            WD.upsert_entity(self.L, "Q67189384", {"zh": "导数分光光度法"}, {}, {}, [("P31", "Q13442814", "normal")], source="t")
            self.assertEqual([h["qid"] for h in WD.search_public(self.L, "导数", limit=2)][0], "Q29175")
            WD.upsert_entity(self.L, "Q7240943", {"zh": "現在進行式", "en": "present continuous"}, {}, {}, [], source="t")
            WD.upsert_entity(self.L, "Q121417949", {"zh": "现在进行时─青年设计师访谈"}, {}, {}, [("P31", "Q13442814", "normal")], source="t")
            self.assertEqual([h["qid"] for h in WD.search_public(self.L, "现在进行时", limit=2)][0], "Q7240943")   # 时/式 不同字：缩短前缀 + 繁体变体

    def test_bind_qid_backfills_trilingual_aliases_and_retracts_on_rebind(self):
        """绑编号 → 实体三语名称/别名回填成 origin=wikidata 的别名（本名不重复进）；用户改别名不冲掉它们；换绑收回旧的、进新的；解绑清空；重放一致。"""
        from kj import register as R
        WD.upsert_entity(self.L, "Q3553768", {"zh": "特征值", "en": "eigenvalue", "ja": "固有値"}, {"en": "scale factor of an eigenvector"},
                         {"zh": ["本征值"], "en": ["characteristic value"]}, [("P31", "Q1936384", "normal")], source="t")
        WD.upsert_entity(self.L, "Q178546", {"zh": "行列式", "en": "determinant"}, {"zh": "方阵的标量", "en": "scalar of a square matrix"}, {}, [], source="t")
        nid = self.svc.create_node(name="特征值", aliases=["eigen"], qid="Q3553768")["node_id"]
        al = {a["alias"]: a for a in self.L.aliases(nid)}
        self.assertEqual(set(al), {"eigen", "eigenvalue", "固有値", "本征值", "characteristic value"})
        self.assertEqual((al["eigenvalue"]["origin"], al["eigenvalue"]["lang"], al["eigen"]["origin"]), ("wikidata", "en", ""))
        self.assertEqual(self.svc.search("eigenvalue")["local"][0]["id"], nid)      # 第二本书用英文名能搜到本地节点
        self.svc.update_node(nid, aliases=["ev"])                                    # 用户整体改别名，回填的不动
        names = {a["alias"] for a in self.L.aliases(nid)}
        self.assertIn("固有値", names); self.assertNotIn("eigen", names); self.assertIn("ev", names)
        r = self.svc.bind_qid(nid, "Q178546", fetch_public=False)                    # 换绑：旧回填收回、新回填进来
        self.assertEqual(r["aliases_synced"], 2)
        names = {a["alias"] for a in self.L.aliases(nid)}
        self.assertEqual(names, {"ev", "行列式", "determinant"})
        self.L.rebuild()
        self.assertEqual({a["alias"] for a in self.L.aliases(nid)}, names)           # 投影可从事件重放
        R.unbind_qid(self.L, nid)
        self.assertEqual({a["alias"] for a in self.L.aliases(nid)}, {"ev"})
        # 搜索候选带核对字段：英文标签/英文简述/别名样本；空字段不带
        h = WD.search_public(self.L, "特征值", limit=1)[0]
        self.assertEqual((h["label_en"], h["aliases"]), ("eigenvalue", ["本征值", "characteristic value"]))
        self.assertEqual(h["description"], "scale factor of an eigenvector"); self.assertNotIn("description_en", h)   # 没中文简述时 description 就是英文，不重复带
        h2 = WD.search_public(self.L, "行列式", limit=1)[0]
        self.assertEqual((h2["description"], h2["description_en"]), ("方阵的标量", "scalar of a square matrix"))

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

    def test_ingest_bridge_bindings_binds_notes_and_appends_provenance(self):
        """桥确认入库后写的 JSONL → 卡绑到节点、背面补 obsidian 深链；重复吸收不重复绑；节点不存在落 unresolved。"""
        b = self.svc.create_node(name="B")["node_id"]
        path = self.tmp / "kj-card-bindings.jsonl"
        lines = [
            {"contract": "kj-card-binding/1", "aid": "fc_" + "a" * 32, "draftId": "draft-" + "b" * 32, "cardIndex": 0,
             "nodeIds": [self.a, b], "noteIds": [7001], "cardIds": [8001], "type": "basic", "front": "Q1", "back": "A1"},
            {"contract": "kj-card-binding/1", "aid": "fc_" + "c" * 32, "draftId": "draft-" + "d" * 32, "cardIndex": 0,
             "nodeIds": ["kj:ZZZZZZZZZZ"], "noteIds": [7002], "cardIds": [8002], "type": "basic", "front": "Q2", "back": "A2"},
        ]
        path.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines), "utf-8")
        updates: list = []

        def fake(url, action, params=None, timeout=15):
            if action == "notesInfo":
                return [{"noteId": 7001, "fields": {"Front": {"value": "Q1", "order": 0}, "Back": {"value": "A1", "order": 1}}}]
            if action == "updateNoteFields":
                updates.append(params); return None
            raise AssertionError(action)
        res = self.svc.ingest_bindings(str(path), request=fake)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["lines"], res["bound"], res["unresolved"]), (2, 1, 1))
        self.assertEqual(sorted(res["nodes"]), sorted([self.a, b]))
        self.assertEqual(res["provenance_updated"], 1)
        self.assertIn('obsidian://open?vault=', updates[0]["note"]["fields"]["Back"])
        self.assertEqual(sorted(self.svc.ledger.cards_of(self.a)[0]["node_ids"]), sorted([self.a, b]))
        self.assertTrue((self.tmp / "unresolved-bindings.jsonl").exists())
        again = self.svc.ingest_bindings(str(path), request=fake)
        self.assertEqual(again["lines"], 0)                     # 游标前进，不重复处理
        with path.open("a", encoding="utf-8") as fh:               # 追加一行 → 只处理新行
            fh.write(json.dumps({"nodeIds": [self.a], "noteIds": [7001], "cardIds": [8001], "front": "Q1", "back": "A1"}) + "\n")
        third = self.svc.ingest_bindings(str(path), request=fake, add_provenance=False)
        self.assertEqual((third["lines"], third["bound"]), (1, 1))
        self.assertEqual(self.svc.ledger.count("cards"), 1)       # 同一 note 仍是一张卡

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
