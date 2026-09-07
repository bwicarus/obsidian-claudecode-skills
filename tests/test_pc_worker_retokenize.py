"""PC 预处理 worker:页缓存命中但分词 schema 落后 → 只重分词(2026-09-07 实锤:重跑预处理发布的还是旧分词)。"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "scripts", ROOT / "_server_deploy"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import reader_pc_preprocess_worker as pcw  # noqa: E402
import reader_book_ocr_worker as core  # noqa: E402


def pipeline():
    obj = pcw.QualityPipeline.__new__(pcw.QualityPipeline)
    obj.project_root = ROOT
    obj._core = None
    return obj


def ascii_chars():
    # 纯英文页:_tokenize_chars 的非假名分支不需要 fugashi
    return [
        {"c": "a", "w": -1, "x0": 0, "x1": 1, "y0": 0, "y1": 1, "bk": 1},
        {"c": "b", "w": -1, "x0": 1, "x1": 2, "y0": 0, "y1": 1, "bk": 1},
        {"c": " ", "sp": 1, "w": -1, "x0": 2, "x1": 3, "y0": 0, "y1": 1, "bk": 1},
        {"c": "c", "w": -1, "x0": 3, "x1": 4, "y0": 0, "y1": 1, "bk": 1},
    ]


class RetokenizeIfStaleTests(unittest.TestCase):
    def test_stale_schema_page_is_retokenized_in_place(self):
        page = {"tokenized": True, "tokenizeSchema": core._TOKENIZE_SCHEMA - 1, "chars": ascii_chars(), "layout": None}
        self.assertTrue(pipeline().retokenize_if_stale(page))
        self.assertEqual(page["tokenizeSchema"], core._TOKENIZE_SCHEMA)
        ws = [c["w"] for c in page["chars"] if not c.get("sp")]
        self.assertTrue(all(isinstance(w, int) and w >= 0 for w in ws), ws)
        self.assertEqual(ws[0], ws[1], "ab 是一个词")

    def test_current_schema_or_untokenized_pages_are_left_alone(self):
        page = {"tokenized": True, "tokenizeSchema": core._TOKENIZE_SCHEMA, "chars": ascii_chars()}
        self.assertFalse(pipeline().retokenize_if_stale(page))
        page = {"tokenized": False, "tokenizeSchema": 1, "chars": ascii_chars()}
        self.assertFalse(pipeline().retokenize_if_stale(page))
        self.assertFalse(pipeline().retokenize_if_stale({"tokenized": True, "tokenizeSchema": 1, "chars": "nope"}))


if __name__ == "__main__":
    unittest.main()
