import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.parse


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "vocab" / "anki_from_word.py"


def _load_module():
    module_dir = str(MODULE_PATH.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    spec = importlib.util.spec_from_file_location(
        "anki_from_word_source_url_contract",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VocabAnkiSourceUrlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_pdf_source_uses_one_encoded_file_and_page_query(self):
        source = "资源/books/A & B#100%? 学习.pdf"
        with mock.patch.dict(
            os.environ,
            {"WEBAPP_BASE_URL": "https://reader.example/"},
            clear=False,
        ):
            title, url = self.module._build_title_url([
                {"pdf": source, "page": 17},
            ])

        self.assertEqual(title, "A & B#100%? 学习.pdf · p.17")
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "reader.example")
        self.assertEqual(parsed.path, "/pdf/view")
        self.assertEqual(parsed.fragment, "")
        query = urllib.parse.parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        self.assertEqual(query, {"file": [source], "page": ["17"]})
        self.assertIn("%2F", parsed.query)
        self.assertIn("%26", parsed.query)
        self.assertIn("%23", parsed.query)
        self.assertIn("%25", parsed.query)
        self.assertIn("%3F", parsed.query)

    def test_missing_and_note_sources_keep_existing_empty_url_contract(self):
        self.assertEqual(self.module._build_title_url([]), ("", ""))
        self.assertEqual(
            self.module._build_title_url([{"note": "notes/000-linear.md"}]),
            ("000-linear", ""),
        )


if __name__ == "__main__":
    unittest.main()
