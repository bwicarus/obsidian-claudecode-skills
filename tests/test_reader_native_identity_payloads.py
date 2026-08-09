from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

if sys.platform == "win32" and "fcntl" not in sys.modules:
    fcntl_stub = types.ModuleType("fcntl")
    fcntl_stub.LOCK_EX = 1
    fcntl_stub.LOCK_SH = 2
    fcntl_stub.LOCK_NB = 4
    fcntl_stub.LOCK_UN = 8
    fcntl_stub.flock = lambda *_args, **_kwargs: None
    sys.modules["fcntl"] = fcntl_stub

import pdf_reader  # noqa: E402


class ReaderNativeIdentityPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__)
        self.app.secret_key = "test"

    def validate(self, body):
        with self.app.test_request_context(
            "/pdf/api/snippets-to-async",
            method="POST",
            base_url="https://reader.example/",
        ):
            with patch.object(pdf_reader, "_reader_uid", return_value="reader-test"):
                return pdf_reader._validate_snippets_body(body)

    def test_structured_pdf_source_becomes_server_origin_link(self) -> None:
        source_snippet = {"text": "欧拉公式", "source": "公式说明"}
        body = {
            "file": "数学/复分析.pdf",
            "page": 12,
            "source": {"kind": "pdf", "page": 12},
            "snippets": [source_snippet],
            "make_note": False,
            "make_anki": True,
        }
        params, error = self.validate(body)
        self.assertIsNone(error)
        self.assertEqual(params["source_file"], "数学/复分析.pdf")
        self.assertEqual(params["source_page"], 12)
        self.assertIn("https://reader.example/pdf/view?file=", params["snippets"][0]["text"])
        self.assertIn("&page=12", params["snippets"][0]["text"])
        self.assertNotIn("原文出处链接", source_snippet["text"], "caller input must not be mutated")

    def test_gateway_translated_top_level_page_wins_descriptive_source_page(self) -> None:
        params, error = self.validate({
            "file": "合集/成员.pdf",
            "page": 7,
            "source": {"kind": "pdf", "page": 91},
            "snippets": [{"text": "段落"}],
            "make_note": False,
            "make_anki": True,
        })
        self.assertIsNone(error)
        self.assertEqual(params["source_page"], 7)
        self.assertIn("&page=7", params["snippets"][0]["text"])

    def test_private_or_ambiguous_source_shapes_fail_closed(self) -> None:
        base = {
            "file": "书.pdf",
            "snippets": [{"text": "段落"}],
            "make_note": False,
            "make_anki": True,
        }
        for changes in (
            {"file": "../书.pdf"},
            {"source": {"kind": "pdf", "url": "http://127.0.0.1/private"}},
            {"source": {"kind": "unknown"}},
            {"page": True},
            {"page": 1.5},
        ):
            with self.subTest(changes=changes):
                _params, error = self.validate({**base, **changes})
                self.assertIsNotNone(error)
                self.assertEqual(error[1], 400)

        _params, error = self.validate([{"text": "not an object"}])
        self.assertEqual(error, ("请求结构无效", 400))


if __name__ == "__main__":
    unittest.main()
