"""Ordinary-web ink must use the reader's existing view-shot tool path."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "_server_deploy"))

import assistant  # noqa: E402


class WebSeeInkContractTest(unittest.TestCase):
    def test_web_ink_reuses_shared_viewshot_result(self) -> None:
        expected = {"画面描述": "圈住的是图片"}
        context = {
            "file_rel": "web:https://example.test/article",
            "page": 1,
            "ink": [{"t": "pen", "c": "#ef4444", "w": 3, "p": [[0.2, 0.3], [0.5, 0.6]]}],
            "view_image": {"media_type": "image/jpeg", "b64": "encoded"},
        }

        with patch.object(
            assistant,
            "_viewshot_result",
            return_value=expected,
        ) as viewshot:
            result = assistant._t_see_ink({}, context)

        self.assertEqual(result, expected)
        viewshot.assert_called_once()
        self.assertIs(viewshot.call_args.args[0], context)
        self.assertIn("网页", viewshot.call_args.args[1])

    def test_web_ink_fails_clearly_without_frontend_composite(self) -> None:
        result = assistant._t_see_ink(
            {},
            {
                "file_rel": "web:https://example.test/article",
                "page": 1,
                "ink": [{"t": "pen", "p": [[0.2, 0.3], [0.5, 0.6]]}],
            },
        )

        self.assertIn("error", result)
        self.assertIn("前端合成截图", result["error"])

    def test_web_ink_rejects_empty_state_before_pdf_fallback(self) -> None:
        result = assistant._t_see_ink(
            {},
            {
                "file_rel": "web:https://example.test/article",
                "page": 1,
                "ink": [],
            },
        )

        self.assertIn("error", result)
        self.assertIn("没有手写笔迹", result["error"])


if __name__ == "__main__":
    unittest.main()
