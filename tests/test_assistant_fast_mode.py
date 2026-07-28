"""Offline contracts for the per-action Codex Fast/service-tier boundary."""

from __future__ import annotations

from pathlib import Path
import copy
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import assistant  # noqa: E402


class _EmptyCodexApp:
    def __init__(self):
        self.calls = []

    def ask(self, *args, **kwargs):
        self.calls.append((args, dict(kwargs)))
        return None


class AssistantFastPreferenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="assistant-fast-contract-"
        )
        root = Path(self.tmp.name)
        self.old_paths = assistant._AP_PATH, assistant._APF_PATH
        self.old_codex_catalog = copy.deepcopy(
            assistant._codex_catalog_cache
        )
        assistant._AP_PATH = root / "action-prefs.json"
        assistant._APF_PATH = root / "profiles.json"
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update({
            "ts": time.time(),
            "verified": True,
            "error": "",
            "models": {
                "gpt-5.6-luna": {
                    "available": True,
                    "depths": ["low", "medium", "high", "xhigh", "max"],
                    "service_tiers": ["priority"],
                    "priority": True,
                    "fast": True,
                },
                "gpt-5.6-sol": {
                    "available": True,
                    "depths": [
                        "low", "medium", "high", "xhigh", "max", "ultra",
                    ],
                    "service_tiers": ["priority"],
                    "priority": True,
                    "fast": True,
                },
                "gpt-5.3-codex-spark": {
                    "available": False,
                    "selectable": True,
                    "catalog_advertised": False,
                    "depths": ["low", "medium"],
                    "depths_verified": False,
                    "service_tiers": [],
                    "priority": False,
                    "fast": False,
                    "reason": (
                        "Spark 兼容型号；实时目录未声明 priority/Fast"
                    ),
                },
            },
        })

        app = Flask(__name__)
        app.secret_key = "fast-contract"
        app.register_blueprint(assistant.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = "alice"

    def tearDown(self):
        assistant._AP_PATH, assistant._APF_PATH = self.old_paths
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update(self.old_codex_catalog)
        self.tmp.cleanup()

    def _save(self, *, backend="codex", variant="gpt-5.6-luna", fast=True):
        return self.client.post(
            "/api/assistant/action-pref",
            json={
                "action": "card_improve",
                "backend": backend,
                "variant": variant,
                "depth": "low" if backend == "codex" else "think",
                "fast": fast,
            },
        )

    def test_catalog_lists_spark_but_marks_it_not_fast(self):
        with patch.object(assistant, "_gemini_models", return_value=[]):
            response = self.client.get("/api/assistant/action-prefs")
        self.assertEqual(response.status_code, 200)
        catalog = response.get_json()["catalog"]
        self.assertIn(
            "gpt-5.3-codex-spark",
            catalog["variants"]["codex"],
        )
        self.assertNotIn(
            "gpt-5.3-codex-spark",
            catalog["fast_models"],
        )
        self.assertEqual(
            catalog["codex_capabilities"]["gpt-5.3-codex-spark"],
            {
                "available": False,
                "selectable": True,
                "catalog_advertised": False,
                "depths": ["low", "medium"],
                "depths_verified": False,
                "service_tiers": [],
                "fast": False,
                "priority": False,
                "reason": (
                    "Spark 兼容型号；实时目录未声明 priority/Fast"
                ),
            },
        )
        self.assertEqual(
            catalog["codex_depths_by_model"]["gpt-5.6-sol"][-1],
            "ultra",
        )
        self.assertEqual(
            catalog["codex_depths_by_model"]["gpt-5.6-luna"][-1],
            "max",
        )
        for model in catalog["fast_models"]:
            self.assertTrue(
                catalog["codex_capabilities"][model]["fast"]
            )

    def test_only_literal_true_on_supported_codex_model_can_persist_fast(self):
        supported = self._save(fast=True)
        self.assertTrue(supported.get_json()["pref"]["fast"])

        for value in ("true", 1, ["true"], {"value": True}, False, None):
            with self.subTest(value=value):
                response = self._save(fast=value)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.get_json()["pref"]["fast"])

        spark = self._save(
            variant="gpt-5.3-codex-spark",
            fast=True,
        )
        self.assertEqual(
            spark.get_json()["pref"]["variant"],
            "gpt-5.3-codex-spark",
        )
        self.assertIs(spark.get_json()["pref"]["fast"], False)

        gemini = self._save(
            backend="gemini",
            variant="gemini-3.5-flash",
            fast=True,
        )
        self.assertFalse(gemini.get_json()["pref"]["fast"])

    def test_normalization_and_force_override_fail_closed_on_non_bool(self):
        for value in ("true", 1, ["true"], {"value": True}):
            with self.subTest(value=value):
                normalized = assistant._ap_norm({
                    "backend": "codex",
                    "variant": "gpt-5.6-luna",
                    "depth": "low",
                    "fast": value,
                })
                self.assertFalse(normalized["fast"])
                forced = assistant._resolve(
                    "card_improve",
                    "alice",
                    force={
                        "backend": "codex",
                        "variant": "gpt-5.6-luna",
                        "depth": "low",
                        "fast": value,
                    },
                )
                self.assertFalse(forced["fast"])

    def test_shared_model_controls_render_fast_only_from_literal_true(self):
        assistant_ui = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-assistant.js"
        ).read_text("utf-8")
        tool_ui = (
            ROOT / "_server_deploy" / "static" / "pdf" / "rc-toolchip.js"
        ).read_text("utf-8")
        self.assertGreaterEqual(
            assistant_ui.count("var fastOn = cur.fast === true"),
            2,
        )
        self.assertGreaterEqual(
            assistant_ui.count("fastOn = def.fast === true"),
            2,
        )
        self.assertGreaterEqual(
            assistant_ui.count("cat.codex_depths_by_model || {}"),
            2,
        )
        self.assertGreaterEqual(
            assistant_ui.count("'— 复习与卡片改进 —'"),
            2,
        )
        self.assertGreaterEqual(
            assistant_ui.count(
                "_renderActs(['card_improve', 'agent', 'paper', "
                "'dictation_grade'])"
            ),
            2,
        )
        self.assertIn("supportsFast && c.fast === true", tool_ui)
        self.assertIn("md.codex_depths_by_model || {}", tool_ui)
        self.assertIn("caps[model].selectable !== true", tool_ui)
        self.assertIn("cap.reason", tool_ui)
        self.assertIn("caps[model].selectable !== true", assistant_ui)
        self.assertIn("cap.reason", assistant_ui)
        self.assertNotIn("当前账号未开放", tool_ui)
        self.assertNotIn("当前账号未开放", assistant_ui)

    def test_live_model_list_is_the_capability_source_of_truth(self):
        rows = [
            {
                "id": "gpt-5.6-sol",
                "hidden": False,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "ultra"},
                ],
                "serviceTiers": [{"id": "priority"}],
            },
            {
                "id": "gpt-5.2",
                "hidden": False,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium"},
                ],
                "serviceTiers": [],
            },
            {
                "id": "gpt-hidden",
                "hidden": True,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                ],
                "serviceTiers": [{"id": "priority"}],
            },
        ]
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update({
            "ts": 0.0, "verified": False, "models": {}, "error": "",
        })
        with patch.object(
            assistant._codex_app,
            "model_catalog",
            return_value=(rows, "auth-live-catalog"),
        ):
            payload = assistant._codex_catalog_payload()

        self.assertIn("gpt-5.2", payload["variants"])
        self.assertNotIn("gpt-hidden", payload["variants"])
        self.assertEqual(
            payload["depths_by_model"]["gpt-5.6-sol"],
            ["low", "ultra"],
        )
        self.assertIn("gpt-5.6-sol", payload["fast_models"])
        self.assertNotIn("gpt-5.2", payload["fast_models"])
        self.assertFalse(
            payload["capabilities"][
                "gpt-5.3-codex-spark"
            ]["available"]
        )


class CodexFastTransportTest(unittest.TestCase):
    def test_app_server_service_tier_is_absent_by_default_and_exact_when_set(self):
        client = assistant._CodexApp()
        calls = []
        sequence = iter(("thread-default", "thread-priority"))

        def fake_rpc(method, params, timeout=20):
            calls.append((method, dict(params), timeout))
            return {"thread": {"id": next(sequence)}}

        client._ensure = Mock()
        client._rpc = fake_rpc

        self.assertEqual(client.thread_start("gpt-5.6-luna"), "thread-default")
        self.assertNotIn("serviceTier", calls[0][1])
        self.assertEqual(
            client.thread_start(
                "gpt-5.6-luna",
                service_tier="priority",
            ),
            "thread-priority",
        )
        self.assertEqual(calls[1][1]["serviceTier"], "priority")

        with self.assertRaisesRegex(ValueError, "priority"):
            client.thread_start(
                "gpt-5.6-luna",
                service_tier="fast-ish",
            )

    def test_turn_service_tier_is_absent_by_default_and_exact_when_set(self):
        client = assistant._CodexApp()
        client._turns["thread-probe"] = queue.Queue()
        calls = []
        turn_number = 0

        def fake_rpc(method, params, timeout=20):
            nonlocal turn_number
            self.assertEqual(method, "turn/start")
            turn_number += 1
            calls.append(dict(params))
            return {"turn": {"id": f"turn-{turn_number}"}}

        client._rpc = fake_rpc
        for service_tier in ("", "priority"):
            client._turns["thread-probe"].put({
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-probe",
                    "turn": {
                        "id": f"turn-{turn_number + 1}",
                        "status": "completed",
                    },
                },
            })
            self.assertEqual(
                list(client.turn_stream(
                    "thread-probe",
                    "hello",
                    timeout=1,
                    service_tier=service_tier,
                )),
                [],
            )

        self.assertNotIn("serviceTier", calls[0])
        self.assertEqual(calls[1]["serviceTier"], "priority")
        with self.assertRaisesRegex(ValueError, "priority"):
            list(client.turn_stream(
                "thread-probe",
                "hello",
                timeout=1,
                service_tier="fast-ish",
            ))

    def test_codex_text_enables_priority_only_for_supported_literal_true(self):
        app = _EmptyCodexApp()
        fallback = Mock(return_value="fallback")
        with (
            patch.object(assistant, "_codex_app", app),
            patch.object(assistant, "_codex_exec_text", fallback),
            patch.object(
                assistant,
                "_codex_fast_ok",
                side_effect=lambda model: model == "gpt-5.6-luna",
            ),
        ):
            self.assertEqual(
                assistant._codex_text(
                    "prompt",
                    model="gpt-5.6-luna",
                    fast=True,
                ),
                "fallback",
            )
            self.assertEqual(
                assistant._codex_text(
                    "prompt",
                    model="gpt-5.3-codex-spark",
                    fast=True,
                ),
                "fallback",
            )
            self.assertEqual(
                assistant._codex_text(
                    "prompt",
                    model="gpt-5.6-luna",
                    fast="true",
                ),
                "fallback",
            )

        self.assertEqual(
            [call[1]["service_tier"] for call in app.calls],
            ["priority", "", ""],
        )
        self.assertEqual(
            [
                call.kwargs["service_tier"]
                for call in fallback.call_args_list
            ],
            ["priority", "", ""],
        )

    def test_exec_adds_priority_flag_only_when_explicit(self):
        commands = []

        def fake_run(cmd, **_kwargs):
            commands.append(list(cmd))
            output = Path(cmd[cmd.index("-o") + 1])
            output.write_text("ok", "utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            patch.object(shutil, "which", return_value="/usr/bin/codex"),
            patch.object(assistant.subprocess, "run", side_effect=fake_run),
        ):
            self.assertEqual(
                assistant._codex_exec_text("prompt"),
                "ok",
            )
            self.assertEqual(
                assistant._codex_exec_text(
                    "prompt",
                    service_tier="priority",
                ),
                "ok",
            )

        tier_args = [
            [part for part in command if "service_tier" in part]
            for command in commands
        ]
        self.assertEqual(tier_args, [[], ['service_tier="priority"']])

    def test_exec_preserves_verified_max_and_ultra_efforts(self):
        commands = []

        def fake_run(cmd, **_kwargs):
            commands.append(list(cmd))
            output = Path(cmd[cmd.index("-o") + 1])
            output.write_text("ok", "utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            patch.object(shutil, "which", return_value="/usr/bin/codex"),
            patch.object(assistant.subprocess, "run", side_effect=fake_run),
        ):
            assistant._codex_exec_text(
                "prompt", model="gpt-5.6-luna", effort="max"
            )
            assistant._codex_exec_text(
                "prompt", model="gpt-5.6-sol", effort="ultra"
            )
        efforts = [
            next(part for part in cmd if "model_reasoning_effort" in part)
            for cmd in commands
        ]
        self.assertEqual(
            efforts,
            [
                'model_reasoning_effort="max"',
                'model_reasoning_effort="ultra"',
            ],
        )


if __name__ == "__main__":
    unittest.main()
