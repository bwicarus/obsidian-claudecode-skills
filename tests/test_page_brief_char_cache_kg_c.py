from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "kg"))

import gen_page_brief  # noqa: E402


def _chars(text: str) -> list[dict]:
    out = []
    x = 10.0
    for character in text:
        if character == " ":
            out.append({"sp": True, "c": " ", "x0": x, "x1": x + 4,
                        "y0": 10, "y1": 20})
            x += 4
            continue
        out.append({"sp": False, "c": character, "x0": x, "x1": x + 8,
                    "y0": 10, "y1": 20})
        x += 8
    return out


class PageBriefCharCacheKgCTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "pdf-char-cache"
        self.cache.mkdir()
        self.pdf = self.root / "current.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "CURRENT PDF EVIDENCE")
        document.save(str(self.pdf))
        document.close()
        os.utime(self.pdf, (1_700_000_100, 1_700_000_100))
        self.mtime = int(self.pdf.stat().st_mtime)
        self.rel = "resources/books/current.pdf"
        self.sha = hashlib.sha1(self.rel.encode("utf-8")).hexdigest()[:16]
        self.cache_patch = patch.object(gen_page_brief, "CHAR_CACHE", self.cache)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.temp.cleanup()

    def _write_cache(
        self,
        *,
        mtime: int,
        text: str,
        language: str = "zh",
    ) -> Path:
        path = self.cache / f"{self.sha}-p1-{mtime}-{language}.json"
        path.write_text(
            json.dumps({"chars": _chars(text), "cver": 11}),
            "utf-8",
        )
        return path

    def test_stale_only_cache_is_ignored_and_current_pdf_text_is_used(self):
        self._write_cache(mtime=self.mtime - 1, text="STALE EVIDENCE")

        self.assertEqual(
            gen_page_brief._char_cache_text(self.rel, 1, self.mtime),
            "",
        )
        page_text = gen_page_brief._page_text(
            self.pdf,
            self.rel,
            1,
        )
        self.assertIn("CURRENT PDF EVIDENCE", page_text)
        self.assertNotIn("STALE EVIDENCE", page_text)

    def test_exact_current_mtime_cache_is_still_preferred(self):
        self._write_cache(mtime=self.mtime - 1, text="STALE EVIDENCE")
        self._write_cache(mtime=self.mtime, text="CURRENT CACHE EVIDENCE")

        page_text = gen_page_brief._page_text(
            self.pdf,
            self.rel,
            1,
        )

        self.assertEqual(page_text, "CURRENT CACHE EVIDENCE")

    def test_corrupt_current_cache_never_falls_back_to_stale_cache(self):
        self._write_cache(mtime=self.mtime - 1, text="STALE EVIDENCE")
        current = self.cache / f"{self.sha}-p1-{self.mtime}-zh.json"
        current.write_text("{not-json", "utf-8")

        page_text = gen_page_brief._page_text(
            self.pdf,
            self.rel,
            1,
        )

        self.assertIn("CURRENT PDF EVIDENCE", page_text)
        self.assertNotIn("STALE EVIDENCE", page_text)

    def test_unknown_current_mtime_refuses_all_cache_generations(self):
        self._write_cache(mtime=0, text="UNPROVEN EVIDENCE")

        self.assertEqual(
            gen_page_brief._char_cache_text(self.rel, 1, 0),
            "",
        )


if __name__ == "__main__":
    unittest.main()
