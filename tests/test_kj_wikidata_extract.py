"""Wikidata dump 流式提取：行 → 最简行、语言过滤、截断 bz2 也能读、产物能被 wikidata-import 吃下。"""
from __future__ import annotations

import bz2
import gzip
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from kj import wikidata as WD  # noqa: E402
from kj import wikidata_extract as X  # noqa: E402
from kj.service import KJService  # noqa: E402


def _item(qid: str, labels: dict, claims: dict | None = None, kind: str = "item", pad: int = 0) -> dict:
    return {"type": kind, "id": qid, "labels": {k: {"language": k, "value": v} for k, v in labels.items()},
            "descriptions": ({"en": {"language": "en", "value": ("x%s " % qid) * pad}} if pad else {}),
            "aliases": {"en": [{"language": "en", "value": qid + "-alias"}]},
            "claims": {p: [{"mainsnak": {"datavalue": {"value": {"id": t}}}, "rank": "normal"} for t in targets]
                       for p, targets in (claims or {}).items()}}


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kjx"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_work_filters_and_minimizes(self):
        lines = [b"[", json.dumps(_item("Q1", {"en": "one", "zh": "一"}, {"P279": ["Q9"]})).encode() + b",",
                 json.dumps(_item("Q2", {"en": "two"}, {"P31": ["Q9"], "P1082": []})).encode() + b",",
                 json.dumps(_item("P5", {"en": "prop"}, kind="property")).encode() + b",",
                 json.dumps(_item("Q3", {"ja": "さん"})).encode(), b"]"]
        items, rels, degrees, min_bytes, kept, kept_n = X._work(lines, ("zh", "ja"))
        self.assertEqual((items, rels, kept_n), (3, 2, 2))
        self.assertEqual(sorted(json.loads(k)["id"] for k in kept), ["Q1", "Q3"])
        row = json.loads(kept[0])
        self.assertEqual(row["relations"], [["P279", "Q9", "normal"]])
        self.assertEqual(row["aliases"], {"en": ["Q1-alias"]})
        self.assertGreater(min_bytes, 0)
        self.assertEqual(sorted(degrees), [0, 1, 1])

    def test_extract_truncated_dump_and_import(self):
        # 3000 行、每行几百字节的随机化填充 → 压缩后跨多个 bz2 块；截掉尾部只损失最后一块（真 dump 没下完就是这样）
        import random
        rnd = random.Random(7)
        rows = []
        for i in range(1, 3001):
            r = _item(f"Q{i}", {"en": f"e{i}", "zh": f"中{i}"} if i % 2 else {"en": f"e{i}"}, {"P279": ["Q1"]})
            r["descriptions"] = {"en": {"language": "en", "value": "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(600))}}
            rows.append(r)
        body = "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n]\n"
        data = bz2.compress(body.encode("utf-8"), compresslevel=1)   # level 1 = 100k 块，小文件也能有多块
        dump = self.tmp / "d.json.bz2"
        dump.write_bytes(data[: len(data) - 2000])         # 故意截断：模拟没下完的文件
        stats = X.extract(dump, self.tmp / "out", workers=2, keep_langs=("zh",), batch_lines=64)
        self.assertGreater(stats["items"], 100)
        self.assertLess(stats["items"], 3000)
        self.assertGreater(stats["kept"], 0)
        self.assertEqual(stats["phase"], "complete")
        self.assertTrue((self.tmp / "out" / "extract-status.json").exists())
        out = Path(stats["out"])
        with gzip.open(out, "rt", encoding="utf-8") as fh:
            kept_rows = [json.loads(l) for l in fh if l.strip()]
        self.assertTrue(all(r["labels"].get("zh") for r in kept_rows))
        # 并行按块模式：同一份截断文件，结果应与顺序模式一致（最后半块解不开 → failed_blocks ≥ 1，不抛）
        par = X.extract_parallel(dump, self.tmp / "out-par", workers=2, keep_langs=("zh",), group_blocks=3, tag="par")
        # 顺序模式会把截断的最后一块里已解出的半截也吐出来，并行模式整块丢弃 → 并行 ≤ 顺序，且是其前缀（顺序完全一致）
        self.assertGreater(par["items"], 100)
        self.assertLessEqual(par["items"], stats["items"])
        self.assertGreaterEqual(par["failed_blocks"], 1)
        self.assertGreater(par["blocks"], 3)
        with gzip.open(par["out"], "rt", encoding="utf-8") as fh:
            par_ids = [json.loads(l)["id"] for l in fh if l.strip()]
        seq_ids = [r["id"] for r in kept_rows]
        self.assertEqual(par_ids, seq_ids[:len(par_ids)])
        self.assertGreater(len(par_ids), 50)
        # 完整文件：并行 == 全量 3000 条，一个块都不该失败
        full = self.tmp / "full.json.bz2"
        full.write_bytes(data)
        st_full = X.extract_parallel(full, self.tmp / "out-full", workers=2, keep_langs=("zh",), group_blocks=5)
        self.assertEqual((st_full["items"], st_full["failed_blocks"], st_full["streams"]), (3000, 0, 1))
        svc = KJService(self.tmp / "kj.db", render=False)
        try:
            res = svc.wikidata_import(str(out))
            self.assertEqual(res["kept"], len(kept_rows))
            self.assertTrue(res["ok"])
            self.assertTrue((self.tmp / "kj-public.db").exists())         # 公共目录独立文件
            self.assertEqual(svc.ledger.count("public_entities"), len(kept_rows))
            hit = WD.search_public(svc.ledger, "中3")
            self.assertEqual(hit[0]["qid"], "Q3")
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
