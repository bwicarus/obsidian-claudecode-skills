from __future__ import annotations

import copy
import json
import http.client
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "_client" / "core"
SERVER = ROOT / "_server_deploy"
for directory in (str(CORE), str(SERVER)):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import assistant  # noqa: E402
import card_improvement_runtime as runtime  # noqa: E402
import qa_browser  # noqa: E402


class LegacyCardModelSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="legacy-card-model-settings-"
        )
        root = Path(self.tmp.name)
        self.old_ap_path = assistant._AP_PATH
        self.old_apf_path = assistant._APF_PATH
        self.old_codex_catalog = copy.deepcopy(
            assistant._codex_catalog_cache
        )
        self.old_runtime_modules = qa_browser._card_improvement_runtime_modules
        self.old_setting = qa_browser._qa_setting
        assistant._AP_PATH = root / "assistant-action-prefs.json"
        assistant._APF_PATH = root / "assistant-pref-profiles.json"
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update({
            "ts": time.time(),
            "verified": True,
            "error": "",
            "models": {
                "gpt-5.6-luna": {
                    "available": True,
                    "catalog_advertised": True,
                    "selectable": True,
                    "depths": ["low", "medium", "high"],
                    "service_tiers": ["standard", "priority"],
                    "priority": True,
                    "fast": True,
                },
                "gpt-5.3-codex-spark": {
                    "available": False,
                    "catalog_advertised": False,
                    "selectable": True,
                    "depths": ["low", "medium"],
                    "service_tiers": ["standard"],
                    "priority": False,
                    "fast": False,
                    "reason": (
                        "Spark 兼容型号；实时目录未声明 priority/Fast"
                    ),
                },
            },
        })
        qa_browser._card_improvement_runtime_modules = (
            lambda: (runtime, assistant)
        )
        qa_browser._qa_setting = lambda key, default=None: (
            "qa-settings-user" if key == "qa_user_id" else default
        )

    def tearDown(self):
        assistant._AP_PATH = self.old_ap_path
        assistant._APF_PATH = self.old_apf_path
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update(self.old_codex_catalog)
        qa_browser._card_improvement_runtime_modules = self.old_runtime_modules
        qa_browser._qa_setting = self.old_setting
        self.tmp.cleanup()

    def _load(self):
        with mock.patch.object(
            assistant,
            "_gemini_models",
            return_value=["gemini-3.5-flash"],
        ):
            return qa_browser._load_card_improvement_settings_for_ui()

    def _save(self, **overrides):
        body = {
            "backend": "codex",
            "variant": "gpt-5.6-luna",
            "depth": "low",
            "fast": False,
        }
        body.update(overrides)
        with mock.patch.object(
            assistant,
            "_gemini_models",
            return_value=["gemini-3.5-flash"],
        ):
            return qa_browser._save_card_improvement_settings_from_ui(body)

    def test_ui_catalog_and_effective_value_come_from_assistant_registry(self):
        data = self._load()

        self.assertTrue(data["ok"])
        self.assertEqual(data["action"], "card_improve")
        self.assertEqual(
            data["effective"],
            assistant._AP_DEFAULTS["card_improve"] | {"fast": False},
        )
        self.assertEqual(
            data["catalog"]["variants"]["codex"],
            ["gpt-5.6-luna", "gpt-5.3-codex-spark"],
        )
        self.assertIn(
            "gpt-5.3-codex-spark",
            data["catalog"]["variants"]["codex"],
        )
        spark = data["catalog"]["codex_capabilities"][
            "gpt-5.3-codex-spark"
        ]
        self.assertIs(spark["available"], False)
        self.assertIs(spark["catalog_advertised"], False)
        self.assertIs(spark["selectable"], True)
        self.assertIs(spark["priority"], False)
        self.assertIs(spark["fast"], False)
        self.assertEqual(spark["depths"], ["low", "medium"])
        self.assertEqual(
            spark["reason"],
            "Spark 兼容型号；实时目录未声明 priority/Fast",
        )
        self.assertTrue(
            data["catalog"]["codex_capabilities"]["gpt-5.6-luna"]["fast"]
        )

    def test_save_writes_the_real_card_improve_action_pref(self):
        result = self._save(fast=True)

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["saved"],
            {
                "backend": "codex",
                "variant": "gpt-5.6-luna",
                "depth": "low",
                "fast": True,
            },
        )
        stored = json.loads(assistant._AP_PATH.read_text("utf-8"))
        self.assertEqual(
            stored["qa-settings-user"]["card_improve"],
            result["saved"],
        )
        self.assertEqual(
            assistant._resolve("card_improve", "qa-settings-user"),
            result["saved"],
        )

    def test_spark_is_selectable_but_cannot_persist_fake_fast(self):
        result = self._save(
            variant="gpt-5.3-codex-spark",
            fast=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["saved"]["variant"], "gpt-5.3-codex-spark")
        self.assertIs(result["saved"]["fast"], False)
        self.assertIs(result["effective"]["fast"], False)

    def test_unselectable_codex_model_stays_visible_but_fails_closed(self):
        unavailable = {
            "ts": time.time(),
            "verified": True,
            "error": "",
            "models": {
                "gpt-disabled": {
                    "available": True,
                    "catalog_advertised": True,
                    "selectable": False,
                    "depths": ["low"],
                    "service_tiers": [],
                    "priority": False,
                    "fast": False,
                    "reason": "策略禁止选择此型号",
                },
            },
        }
        with mock.patch.object(
            assistant, "_codex_catalog", return_value=unavailable
        ):
            data = self._load()
            rejected = self._save(
                variant="gpt-disabled",
                fast=False,
            )

        self.assertIn(
            "gpt-disabled",
            data["catalog"]["variants"]["codex"],
        )
        self.assertIs(
            data["catalog"]["codex_capabilities"][
                "gpt-disabled"
            ]["selectable"],
            False,
        )
        self.assertFalse(rejected["ok"])

    def test_invalid_profile_is_rejected_without_replacing_previous_value(self):
        first = self._save(fast=True)
        self.assertTrue(first["ok"])

        invalid = self._save(backend="not-a-backend")

        self.assertFalse(invalid["ok"])
        self.assertEqual(
            assistant._resolve("card_improve", "qa-settings-user"),
            first["saved"],
        )

    def test_readback_mismatch_cannot_be_reported_as_saved(self):
        with mock.patch.object(assistant, "_ap_set", return_value=None):
            result = self._save(fast=True)

        self.assertFalse(result["ok"])
        self.assertIn("未能写入", result["error"])

    def test_old_page_keeps_legacy_chat_settings_but_has_shared_card_endpoint(self):
        html = qa_browser.HTML
        self.assertIn("普通截图问答（旧设置）", html)
        self.assertIn("复习卡改进（与阅读器共用）", html)
        self.assertIn("api/card-improvement-settings", html)
        self.assertIn("cardImprovementCatalog.codex_capabilities", html)
        self.assertIn("cardFastSupported()", html)
        self.assertIn("op.disabled = disabled.includes(value)", html)
        self.assertIn("caps[value].selectable !== true", html)
        self.assertIn("caps[value].reason", html)
        self.assertNotIn("当前账号未开放此型号", html)
        self.assertIn("api/settings", html)

    def test_http_endpoint_reads_and_writes_the_shared_preference(self):
        server = qa_browser.ThreadedHTTPServer(
            ("127.0.0.1", 0),
            qa_browser.Handler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            server.server_address[1],
            timeout=5,
        )
        try:
            with mock.patch.object(
                assistant,
                "_gemini_models",
                return_value=["gemini-3.5-flash"],
            ):
                connection.request("GET", "/api/card-improvement-settings")
                response = connection.getresponse()
                loaded = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertIn(
                    "gpt-5.3-codex-spark",
                    loaded["catalog"]["variants"]["codex"],
                )

                body = json.dumps({
                    "backend": "codex",
                    "variant": "gpt-5.3-codex-spark",
                    "depth": "low",
                    "fast": True,
                })
                connection.request(
                    "POST",
                    "/api/card-improvement-settings",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                saved = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    saved["effective"]["variant"],
                    "gpt-5.3-codex-spark",
                )
                self.assertIs(saved["effective"]["fast"], False)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
