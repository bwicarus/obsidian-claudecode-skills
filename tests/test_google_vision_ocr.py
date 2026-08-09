from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import google_vision_ocr  # noqa: E402


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"responses": [{}]}


class GoogleVisionCredentialTransportTest(unittest.TestCase):
    def test_api_key_is_sent_in_header_not_exception_visible_url(self) -> None:
        with patch.object(
            google_vision_ocr.requests,
            "post",
            return_value=_Response(),
        ) as post:
            result = google_vision_ocr.ocr_one_page("test-secret", b"image")

        self.assertEqual(result, {"chars": [], "text": ""})
        url = post.call_args.args[0]
        self.assertNotIn("test-secret", url)
        self.assertNotIn("key=", url)
        self.assertEqual(
            post.call_args.kwargs["headers"],
            {"X-Goog-Api-Key": "test-secret"},
        )


if __name__ == "__main__":
    unittest.main()
