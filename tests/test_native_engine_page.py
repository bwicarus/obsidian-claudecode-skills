# -*- coding: utf-8 -*-
"""native 引擎（有文字层的书不 OCR、只分词，2026-09-06）：字符层读取 + 分词的最小闭环。

用 PyMuPDF 现造一页含日文与英文的 PDF，走 reader_book_ocr_worker._native_page →
_vision_page_layout → _tokenize_chars，断言：字符结构与 Vision 页一致、非空白字符都拿到词 id、
日文按 fugashi 切出多于一个词、空白字符保留 w=-1。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_server_deploy"))

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

import reader_book_ocr_worker as worker  # noqa: E402

ALLOWED_CHAR_KEYS = {"c", "x0", "y0", "x1", "y1", "w", "bk", "b", "sp", "line", "conf", "vertical"}


@unittest.skipIf(fitz is None, "PyMuPDF unavailable")
class NativePageTest(unittest.TestCase):
    def _page(self, text: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "native.pdf"
        doc = fitz.open()
        page = doc.new_page(width=400, height=300)
        page.insert_text((40, 80), text, fontsize=14, fontname="japan")
        doc.save(str(path))
        doc.close()
        doc = fitz.open(str(path))
        self.addCleanup(doc.close)
        return doc[0]

    def test_text_layer_page_is_read_and_tokenized(self) -> None:
        page = self._page("エネルギーは保存される Hello world")
        chars, text, image_w, image_h = worker._native_page(page)
        self.assertTrue(chars, "有文字层的页必须读出字符")
        self.assertEqual(image_w, 400)
        self.assertEqual(image_h, 300)
        for char in chars:
            self.assertTrue(set(char) <= ALLOWED_CHAR_KEYS, char)
            self.assertEqual(char["w"], -1, "分词前 w 一律 -1")
        self.assertIn("エネルギー", text)
        layout = worker._vision_page_layout(chars, page_w=400.0, page_h=300.0)
        layout["textSource"] = "native"
        tokenized = worker._tokenize_chars(chars, layout)
        words = {c["w"] for c in tokenized if not c.get("sp")}
        self.assertNotIn(-1, words, "非空白字符都要拿到词 id")
        self.assertGreater(len(words), 3, "日文应被 fugashi 切成多个词，英文按空格切")
        for c in tokenized:
            if c.get("sp"):
                self.assertEqual(c["w"], -1)

    def test_scanned_page_without_text_layer_yields_nothing(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "blank.pdf"
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        doc.save(str(path))
        doc.close()
        doc = fitz.open(str(path))
        self.addCleanup(doc.close)
        chars, text, _, _ = worker._native_page(doc[0])
        self.assertEqual(chars, [])
        self.assertEqual(text, "")
        layout = worker._vision_page_layout(chars, page_w=200.0, page_h=200.0)
        self.assertEqual(layout["textSource"], "unavailable", "没有文字层的页不伪造")


if __name__ == "__main__":
    unittest.main()
