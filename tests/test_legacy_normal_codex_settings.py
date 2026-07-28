from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "_client" / "core"
SERVER = ROOT / "_server_deploy"
for directory in (str(CORE), str(SERVER)):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import ai_backends  # noqa: E402
import qa_browser  # noqa: E402


def _catalog_payload():
    return {
        "variants": [
            "gpt-5.6-luna",
            "gpt-5.4-mini",
            "gpt-5.3-codex-spark",
        ],
        "capabilities": {
            "gpt-5.6-luna": {
                "available": True,
                "catalog_advertised": True,
                "selectable": True,
                "depths": ["low", "medium", "max"],
                "service_tiers": ["standard", "priority"],
                "priority": True,
                "fast": True,
                "reason": "",
            },
            "gpt-5.4-mini": {
                "available": True,
                "catalog_advertised": True,
                "selectable": True,
                "depths": ["low", "high"],
                "service_tiers": ["standard"],
                "priority": False,
                "fast": False,
                "reason": "",
            },
            "gpt-5.3-codex-spark": {
                "available": False,
                "catalog_advertised": False,
                "selectable": True,
                "depths": ["low", "medium"],
                "service_tiers": [],
                "priority": False,
                "fast": False,
                "reason": "Spark 兼容型号；实时目录未声明 priority/Fast",
            },
        },
        "fast_models": ["gpt-5.6-luna"],
        "depths_by_model": {
            "gpt-5.6-luna": ["low", "medium", "max"],
            "gpt-5.4-mini": ["low", "high"],
            "gpt-5.3-codex-spark": ["low", "medium"],
        },
        "verified": True,
        "error": "",
    }


class LegacyNormalCodexSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="legacy-normal-codex-")
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "state" / "server-config.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            json.dumps({
                "ai_backend": "codex_cli",
                "ai": {
                    "claude_cli": {
                        "command": "claude",
                        "model": "opus",
                        "effort": "low",
                    },
                    "codex_cli": {
                        "command": "codex",
                        "model": "gpt-5.6-luna",
                        "effort": "medium",
                        "fast": True,
                    },
                },
            }),
            encoding="utf-8",
        )
        self.action_pref = self.root / "state" / "assistant-action-prefs.json"
        self.action_pref.write_text('{"kept":"card-improve"}', encoding="utf-8")
        self.old_project = os.environ.get("CLAUDE_PROJECT")
        self.old_get_cfg = qa_browser._GET_CFG
        self.old_assistant_module = qa_browser._reader_assistant_module
        os.environ["CLAUDE_PROJECT"] = str(self.root)
        qa_browser._GET_CFG = lambda: json.loads(
            self.config_path.read_text("utf-8")
        )
        qa_browser._reader_assistant_module = lambda: types.SimpleNamespace(
            _codex_catalog_payload=_catalog_payload,
        )

    def tearDown(self):
        if self.old_project is None:
            os.environ.pop("CLAUDE_PROJECT", None)
        else:
            os.environ["CLAUDE_PROJECT"] = self.old_project
        qa_browser._GET_CFG = self.old_get_cfg
        qa_browser._reader_assistant_module = self.old_assistant_module
        self.tmp.cleanup()

    def _save(self, **overrides):
        body = {
            "backend": "codex_cli",
            "claude": {"model": "opus", "effort": "low"},
            "codex": {
                "model": "gpt-5.6-luna",
                "effort": "medium",
                "fast": True,
            },
        }
        for key, value in overrides.items():
            if key == "codex":
                body["codex"].update(value)
            else:
                body[key] = value
        return qa_browser._save_ai_settings_from_ui(body)

    def test_load_keeps_compatibility_spark_selectable_without_fast(self):
        data = qa_browser._load_ai_settings_for_ui()

        self.assertEqual(data["codex"]["model"], "gpt-5.6-luna")
        self.assertEqual(data["codex"]["effort"], "medium")
        self.assertIs(data["codex"]["fast"], True)
        self.assertEqual(
            data["codex_catalog"]["depths_by_model"]["gpt-5.6-luna"],
            ["low", "medium", "max"],
        )
        spark = data["codex_catalog"]["capabilities"][
            "gpt-5.3-codex-spark"
        ]
        self.assertIs(spark["available"], False)
        self.assertIs(spark["catalog_advertised"], False)
        self.assertIs(spark["selectable"], True)
        self.assertIs(spark["priority"], False)
        self.assertIn(
            "gpt-5.3-codex-spark",
            data["codex_catalog"]["variants"],
        )

    def test_spark_is_injected_as_selectable_compatibility_model(self):
        payload = _catalog_payload()
        payload["variants"] = ["gpt-5.6-luna"]
        payload["capabilities"].pop("gpt-5.3-codex-spark")
        payload["depths_by_model"].pop("gpt-5.3-codex-spark")
        qa_browser._reader_assistant_module = lambda: types.SimpleNamespace(
            _codex_catalog_payload=lambda: payload,
            _CODEX_DEPTHS=("low", "medium", "high", "xhigh"),
        )

        data = qa_browser._load_ai_settings_for_ui()
        spark = data["codex_catalog"]["capabilities"][
            "gpt-5.3-codex-spark"
        ]

        self.assertIn(
            "gpt-5.3-codex-spark",
            data["codex_catalog"]["variants"],
        )
        self.assertIs(spark["available"], False)
        self.assertIs(spark["catalog_advertised"], False)
        self.assertIs(spark["selectable"], True)
        self.assertIs(spark["priority"], False)
        self.assertEqual(
            spark["reason"],
            "Spark 兼容型号；实时目录未声明 priority/Fast",
        )

    def test_save_is_separate_from_card_action_pref_and_strict_boolean(self):
        before = self.action_pref.read_bytes()
        result = self._save(codex={"fast": "true"})

        self.assertTrue(result["ok"])
        stored = json.loads(self.config_path.read_text("utf-8"))
        self.assertIs(stored["ai"]["codex_cli"]["fast"], False)
        self.assertIs(result["codex"]["fast"], False)
        self.assertEqual(self.action_pref.read_bytes(), before)

    def test_supported_fast_and_model_specific_effort_persist(self):
        result = self._save()

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["codex"],
            {
                "model": "gpt-5.6-luna",
                "effort": "medium",
                "fast": True,
            },
        )

    def test_unselectable_model_and_wrong_model_effort_fail_without_write(self):
        before = self.config_path.read_bytes()

        missing = self._save(codex={
            "model": "",
            "effort": "",
            "fast": False,
        })
        payload = _catalog_payload()
        payload["variants"].append("gpt-disabled")
        payload["capabilities"]["gpt-disabled"] = {
            "available": True,
            "catalog_advertised": True,
            "selectable": False,
            "depths": ["low"],
            "service_tiers": [],
            "priority": False,
            "fast": False,
            "reason": "策略禁止选择此型号",
        }
        qa_browser._reader_assistant_module = lambda: types.SimpleNamespace(
            _codex_catalog_payload=lambda: payload,
        )
        unselectable = self._save(codex={
            "model": "gpt-disabled",
            "effort": "low",
            "fast": False,
        })
        wrong_effort = self._save(codex={
            "model": "gpt-5.4-mini",
            "effort": "medium",
            "fast": False,
        })
        fake_fast = self._save(codex={
            "model": "gpt-5.4-mini",
            "effort": "low",
            "fast": True,
        })

        self.assertFalse(missing["ok"])
        self.assertFalse(unselectable["ok"])
        self.assertEqual(
            unselectable["error"],
            "策略禁止选择此型号，未保存",
        )
        self.assertFalse(wrong_effort["ok"])
        self.assertFalse(fake_fast["ok"])
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_spark_persists_normally_but_never_invents_fast(self):
        result = self._save(codex={
            "model": "gpt-5.3-codex-spark",
            "effort": "low",
            "fast": False,
        })
        fake_fast = self._save(codex={
            "model": "gpt-5.3-codex-spark",
            "effort": "low",
            "fast": True,
        })

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["codex"]["model"],
            "gpt-5.3-codex-spark",
        )
        self.assertIs(result["codex"]["fast"], False)
        self.assertFalse(fake_fast["ok"])

    def test_non_codex_backend_cannot_leave_normal_fast_enabled(self):
        result = self._save(backend="claude_cli")

        self.assertTrue(result["ok"])
        stored = json.loads(self.config_path.read_text("utf-8"))
        self.assertEqual(stored["ai_backend"], "claude_cli")
        self.assertIs(stored["ai"]["codex_cli"]["fast"], False)
        self.assertIs(result["codex"]["fast"], False)

    def test_frontend_has_dynamic_model_depth_and_independent_fast_controls(self):
        html = qa_browser.HTML
        self.assertIn('id="s-codex-model"', html)
        self.assertIn('id="s-codex-effort"', html)
        self.assertIn('id="s-codex-fast"', html)
        self.assertIn("normalCodexCatalog.capabilities", html)
        self.assertIn("rebindNormalCodexDepth", html)
        self.assertIn("cap.selectable !== true", html)
        self.assertIn("cap.reason", html)
        self.assertNotIn("当前账号未开放此型号", html)
        self.assertIn("current.fast === true", html)
        self.assertIn("普通截图问答（旧设置）", html)
        self.assertIn("复习卡改进（与阅读器共用）", html)


class LegacyCodexCliTransportTest(unittest.TestCase):
    def _run(self, settings):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            output = Path(cmd[cmd.index("--output-last-message") + 1])
            output.write_text("verified answer", encoding="utf-8")
            return types.SimpleNamespace(
                stdout="",
                stderr="",
                returncode=0,
            )

        capability = {
            "available": True,
            "depths": ["low", "medium", "max"],
            "service_tiers": ["standard", "priority"],
            "priority": True,
            "fast": True,
        }
        with mock.patch.object(
            ai_backends,
            "_verified_codex_capability",
            return_value=capability,
        ), mock.patch.object(ai_backends, "_run_hidden", side_effect=fake_run):
            text = ai_backends.CodexCli(settings).chat([
                {"role": "user", "content": "hello"},
            ])
        return text, captured

    def test_transport_passes_model_effort_and_verified_priority(self):
        text, captured = self._run({
            "command": "codex",
            "model": "gpt-5.6-luna",
            "effort": "max",
            "fast": True,
        })

        self.assertEqual(text, "verified answer")
        cmd = captured["cmd"]
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.6-luna")
        self.assertIn('model_reasoning_effort="max"', cmd)
        self.assertIn('service_tier="priority"', cmd)
        self.assertIn('sandbox_mode="read-only"', cmd)

    def test_string_fast_never_enables_priority(self):
        _text, captured = self._run({
            "command": "codex",
            "model": "gpt-5.6-luna",
            "effort": "low",
            "fast": "true",
        })

        self.assertNotIn('service_tier="priority"', captured["cmd"])

    def test_unavailable_model_fails_before_process_launch(self):
        with mock.patch.object(
            ai_backends,
            "_verified_codex_capability",
            side_effect=RuntimeError("not available"),
        ), mock.patch.object(ai_backends, "_run_hidden") as run:
            with self.assertRaisesRegex(RuntimeError, "not available"):
                ai_backends.CodexCli({
                    "command": "codex",
                    "model": "gpt-5.3-codex-spark",
                    "effort": "low",
                    "fast": False,
                }).chat([{"role": "user", "content": "hello"}])
        run.assert_not_called()

    def test_missing_model_fails_before_catalog_or_process(self):
        with mock.patch.object(
            ai_backends,
            "_verified_codex_capability",
        ) as capability, mock.patch.object(ai_backends, "_run_hidden") as run:
            with self.assertRaisesRegex(RuntimeError, "未选择"):
                ai_backends.CodexCli({
                    "command": "codex",
                    "model": "",
                    "effort": "",
                    "fast": False,
                }).chat([{"role": "user", "content": "hello"}])
        capability.assert_not_called()
        run.assert_not_called()

    def test_wrong_effort_and_unsupported_fast_fail_closed(self):
        no_fast = {
            "available": True,
            "depths": ["low", "high"],
            "service_tiers": ["standard"],
            "priority": False,
            "fast": False,
        }
        with mock.patch.object(
            ai_backends,
            "_verified_codex_capability",
            return_value=no_fast,
        ), mock.patch.object(ai_backends, "_run_hidden") as run:
            with self.assertRaisesRegex(RuntimeError, "思考深度"):
                ai_backends.CodexCli({
                    "command": "codex",
                    "model": "gpt-5.4-mini",
                    "effort": "medium",
                    "fast": False,
                }).chat([{"role": "user", "content": "hello"}])
            with self.assertRaisesRegex(RuntimeError, "不支持 priority"):
                ai_backends.CodexCli({
                    "command": "codex",
                    "model": "gpt-5.4-mini",
                    "effort": "low",
                    "fast": True,
                }).chat([{"role": "user", "content": "hello"}])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
