"""AI 后端健康与自动改道(2026-09-07):Claude 登录失效冷却、Codex 默认模型与自动重试。"""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ai_client  # noqa: E402


class AIClientHealthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_health = ai_client._HEALTH_FILE
        self._orig_log = ai_client._LOG_FILE
        ai_client._HEALTH_FILE = Path(self._tmp.name) / "ai-health.json"
        ai_client._LOG_FILE = Path(self._tmp.name) / "ai_calls.log"
        ai_client._CLAUDE_FLAGGED[0] = None

    def tearDown(self):
        ai_client._HEALTH_FILE = self._orig_health
        ai_client._LOG_FILE = self._orig_log
        ai_client._CLAUDE_FLAGGED[0] = None
        self._tmp.cleanup()

    def test_auth_failure_detected_from_cli_line_or_stderr_only(self):
        self.assertTrue(ai_client._is_claude_auth_failure(
            "Failed to authenticate: OAuth session expired and could not be refreshed"))
        self.assertTrue(ai_client._is_claude_auth_failure("", "Error: OAuth session expired"))
        # 正文里讲到登录过期不算失败(否则一次讲解就把 Claude 冷却 10 分钟)
        self.assertFalse(ai_client._is_claude_auth_failure(
            "OAuth session expired is a common reason users need to sign in again."))
        self.assertFalse(ai_client._is_claude_auth_failure("ok", ""))

    def test_cooldown_routes_codex_first_then_probes_claude_and_recovers(self):
        ai_client._mark_claude_auth_failed("Failed to authenticate: OAuth session expired")
        self.assertTrue(ai_client.claude_in_cooldown())
        health = ai_client.ai_health()
        self.assertTrue(health["claude_in_cooldown"])
        self.assertIn("OAuth session expired", health["claude_auth_error"])

        calls = []
        result = ai_client.route(
            "auto-claude",
            lambda: calls.append("claude") or "ok-claude",
            lambda: calls.append("codex") or '{"zh":"金字塔"}',
        )
        self.assertEqual(result, '{"zh":"金字塔"}')
        self.assertEqual(calls, ["codex"], "冷却期内不该先干等 Claude")

        calls.clear()
        result = ai_client.route(
            "auto-claude",
            lambda: calls.append("claude") or "ok-claude",
            lambda: calls.append("codex") or "The model requires a newer version of Codex.",
        )
        self.assertEqual(result, "ok-claude")
        self.assertEqual(calls, ["codex", "claude"], "Codex 也挂才回头探 Claude")

        ai_client._mark_claude_ok()
        self.assertFalse(ai_client.claude_in_cooldown())
        self.assertIsNone(ai_client._health_read().get("claude_cooldown_until"))
        self.assertIsNotNone(ai_client._health_read().get("claude_recovered_at"))

    def test_no_cooldown_keeps_claude_first(self):
        calls = []
        result = ai_client.route(
            "auto-claude",
            lambda: calls.append("claude") or "ok-claude",
            lambda: calls.append("codex") or "unexpected",
        )
        self.assertEqual(result, "ok-claude")
        self.assertEqual(calls, ["claude"])

    def test_codex_needs_newer_cli_counts_as_unavailable(self):
        self.assertTrue(ai_client.is_backend_unavailable(
            "ERROR: The 'gpt-6-astra' model requires a newer version of Codex. Please update."))
        self.assertFalse(ai_client.is_backend_unavailable("Codex 是 OpenAI 的 CLI。"))

    def test_codex_model_defaults(self):
        self.assertEqual(ai_client.codex_model("gpt-5.5-mini"), "gpt-5.5-mini")
        with mock.patch.object(ai_client, "load_settings", return_value={"backend": "auto-claude", "model": ""}):
            self.assertEqual(ai_client.codex_model(""), ai_client.CODEX_DEFAULT_MODEL)
        with mock.patch.object(ai_client, "load_settings", return_value={"backend": "codex", "model": "gpt-5.5"}):
            self.assertEqual(ai_client.codex_model(""), "gpt-5.5")

    def test_codex_raw_retries_with_default_model_when_cli_too_old(self):
        seen = []

        def fake_exec(prompt, image_path, model):
            seen.append(model)
            if model != ai_client.CODEX_DEFAULT_MODEL:
                return "The 'gpt-6-astra' model requires a newer version of Codex."
            return '{"zh":"金字塔"}'

        with mock.patch.object(ai_client, "_codex_exec", side_effect=fake_exec):
            self.assertEqual(ai_client.codex_raw("q", model="gpt-6-astra"), '{"zh":"金字塔"}')
        self.assertEqual(seen, ["gpt-6-astra", ai_client.CODEX_DEFAULT_MODEL])

    def test_codex_raw_never_calls_cli_without_a_model(self):
        seen = []
        with mock.patch.object(ai_client, "_codex_exec", side_effect=lambda p, i, m: seen.append(m) or "x"), \
                mock.patch.object(ai_client, "load_settings", return_value={"backend": "auto-claude", "model": ""}):
            ai_client.codex_raw("q")
        self.assertEqual(seen, [ai_client.CODEX_DEFAULT_MODEL])

    def test_claude_env_injects_long_lived_token_only_when_file_present(self):
        token_file = Path(self._tmp.name) / "claude-code-oauth-token"
        with mock.patch.object(ai_client, "_CLAUDE_TOKEN_FILE", token_file), \
                mock.patch.dict(ai_client.os.environ, {}, clear=False):
            ai_client.os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            self.assertIsNone(ai_client._claude_env())
            token_file.write_text("sk-ant-oat01-test\n", encoding="utf-8")
            env = ai_client._claude_env()
            self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-test")

    def test_missing_claude_cli_is_unavailable_not_an_exception(self):
        with mock.patch.object(ai_client, "CLAUDE", r"Z:\nonexistent\claude.exe"):
            self.assertEqual(ai_client.claude_raw("q", first=True), "")
        log = ai_client._LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("CLI 不可执行", log)

    def test_default_claude_cli_prefers_an_existing_binary(self):
        import config
        self.assertTrue(
            config.CLAUDE_CLI == "claude" or os.path.exists(config.CLAUDE_CLI),
            f"config.CLAUDE_CLI 指向不存在的路径: {config.CLAUDE_CLI}",
        )


if __name__ == "__main__":
    unittest.main()
