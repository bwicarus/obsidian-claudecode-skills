#!/usr/bin/env python3
"""部署浏览器 E2E 必须覆盖真实插图路由，但不能改写派生状态。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
os.environ.setdefault("CLAUDE_PROJECT", str(ROOT))
os.environ.setdefault("WEBAPP_DATA", tempfile.mkdtemp())

import pdf_reader  # noqa: E402
import reader_sync_relay  # noqa: E402


class ReaderDeployE2EReadOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__)
        self.sidecar = {
            "figures_geom": [
                {
                    "page": 35,
                    "caption": "示意图",
                    "desc": "说明",
                    "bbox": [0.1, 0.2, 0.3, 0.4],
                    "fbox": [0.1, 0.2, 0.3, 0.4],
                }
            ],
            "figures": [],
        }

    def _request(self, *, deployment_probe: bool):
        headers = (
            {"X-BW-Reader-Deployment-Probe": "1"}
            if deployment_probe
            else {}
        )
        data = copy.deepcopy(self.sidecar)
        with (
            patch.object(
                pdf_reader,
                "_safe_vault_path",
                return_value=Path("/tmp/e2e-book.pdf"),
            ),
            patch.object(pdf_reader, "_book_fig_enabled", return_value=True),
            patch.object(pdf_reader, "_fig_load_abs", return_value=data),
            patch.object(pdf_reader, "_fig_save_abs") as save,
            self.app.test_request_context(
                "/pdf/api/page-figures?file=e2e-book.pdf&page=35",
                headers=headers,
            ),
        ):
            response = pdf_reader.pdf_api_page_figures()
        return response.get_json(), data, save

    def test_deployment_probe_computes_badge_without_persisting(self) -> None:
        payload, data, save = self._request(deployment_probe=True)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["figures"][0]["badge"], [0.3, 0.2])
        self.assertEqual(data["figures_geom"][0]["badge"], [0.3, 0.2])
        save.assert_not_called()

    def test_normal_reader_request_keeps_lazy_persistence(self) -> None:
        payload, data, save = self._request(deployment_probe=False)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["figures"][0]["badge"], [0.3, 0.2])
        save.assert_called_once()
        self.assertIs(save.call_args.args[1], data)

    def test_deployment_probe_validates_prefs_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="bw-reader-deploy-prefs-"
        ) as temp:
            path = Path(temp) / "prefs.json"
            original = '{"keep":1}'
            path.write_text(original, "utf-8")
            with (
                patch.object(pdf_reader, "_prefs_path", return_value=path),
                self.app.test_request_context(
                    "/pdf/api/prefs",
                    method="POST",
                    json={"patch": {"new": 2}},
                    headers={"X-BW-Reader-Deployment-Probe": "1"},
                ),
            ):
                response = pdf_reader.pdf_api_prefs()
            self.assertTrue(response.get_json()["deploymentProbe"])
            self.assertEqual(path.read_text("utf-8"), original)

    def test_normal_prefs_request_still_persists(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="bw-reader-normal-prefs-"
        ) as temp:
            path = Path(temp) / "prefs.json"
            path.write_text('{"keep":1}', "utf-8")
            with (
                patch.object(pdf_reader, "_prefs_path", return_value=path),
                self.app.test_request_context(
                    "/pdf/api/prefs",
                    method="POST",
                    json={"patch": {"new": 2}},
                ),
            ):
                response = pdf_reader.pdf_api_prefs()
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(
                json.loads(path.read_text("utf-8")),
                {"keep": 1, "new": 2},
            )

    def test_deployment_probe_makes_dictionary_lookup_read_only(self) -> None:
        with self.app.test_request_context(
            "/pdf/api/dict-quick?word=test",
            headers={"X-BW-Reader-Deployment-Probe": "1"},
        ):
            self.assertTrue(pdf_reader._reader_deployment_probe())
        source = (ROOT / "_server_deploy" / "pdf_reader.py").read_text("utf-8")
        self.assertIn(
            'request.args.get("prewarm") == "1"\n'
            "        or _reader_deployment_probe()",
            source,
        )

    def test_deployment_probe_rejects_every_sync_mutation_before_storage(
        self,
    ) -> None:
        app = Flask("reader-deploy-sync-probe")
        app.secret_key = "test-only"
        app.register_blueprint(reader_sync_relay.bp)
        endpoints = (
            "/api/reader/sync/owner/claim",
            "/api/reader/sync/owner/renew",
            "/api/reader/sync/owner/release",
            "/api/reader/sync/signal",
            "/api/reader/sync/exchange",
            "/api/reader/sync/snapshot",
        )
        with patch.object(reader_sync_relay, "_connection") as connection:
            client = app.test_client()
            for endpoint in endpoints:
                response = client.post(
                    endpoint,
                    headers={"X-BW-Reader-Deployment-Probe": "1"},
                )
                self.assertEqual(response.status_code, 503, endpoint)
                self.assertEqual(
                    response.get_json()["code"],
                    "BW_SYNC_DEPLOYMENT_PROBE_READ_ONLY",
                )
            connection.assert_not_called()

    def test_browser_e2e_sets_the_read_only_probe_header(self) -> None:
        source = (ROOT / "scripts" / "reader_e2e.py").read_text("utf-8")
        self.assertIn(
            'DEPLOYMENT_PROBE_HEADER = "X-BW-Reader-Deployment-Probe"',
            source,
        )
        self.assertIn(
            'extra_http_headers={DEPLOYMENT_PROBE_HEADER: "1"}',
            source,
        )


if __name__ == "__main__":
    unittest.main()
