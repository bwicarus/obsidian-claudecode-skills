"""语音后台 Agent 的 Codex Fast 参数合同。

Fast 是 action-pref 的逐动作选择；缺省、假值或非布尔输入都不能把后台
Codex CLI 隐式提升到 priority。Claude 主路径和 Codex→Claude 兜底同样
不得携带 Codex 的 service tier。
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "_server_deploy"))

import voice  # noqa: E402


def _service_tier_args(cmd: list[str]) -> list[str]:
    return [part for part in cmd if "service_tier" in part]


class VoiceAgentCodexCommandFastTest(unittest.TestCase):
    def test_default_does_not_inherit_priority_from_environment(self) -> None:
        with mock.patch.dict(
            os.environ, {"AGENT_CODEX_TIER": "priority"}, clear=False
        ):
            cmd = voice._agent_codex_cmd("完成任务")

        self.assertEqual(_service_tier_args(cmd), [])
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertIn("read-only", cmd)
        self.assertIn("features.shell_tool=false", cmd)
        mcp = next(part for part in cmd if part.startswith("mcp_servers.bwapp="))
        self.assertIn('default_tools_approval_mode="approve"', mcp)
        self.assertIn(
            'disabled_tools=["assistant_log_chat","assistant_history"]', mcp
        )

    def test_explicit_true_adds_exact_priority_tier_once(self) -> None:
        with mock.patch.object(
            voice, "_agent_codex_fast_ok", return_value=True
        ):
            cmd = voice._agent_codex_cmd(
                "完成任务",
                model="gpt-5.6-luna",
                effort="medium",
                fast=True,
            )

        self.assertEqual(
            _service_tier_args(cmd),
            ['service_tier="priority"'],
        )
        self.assertIn('model="gpt-5.6-luna"', cmd)
        self.assertIn('model_reasoning_effort="medium"', cmd)

    def test_false_and_non_boolean_values_fail_closed(self) -> None:
        for value in (False, None, "true", "false", 1, 0):
            with self.subTest(value=value):
                with mock.patch.object(
                    voice, "_agent_codex_fast_ok", return_value=True
                ):
                    cmd = voice._agent_codex_cmd("完成任务", fast=value)
                self.assertEqual(_service_tier_args(cmd), [])

    def test_explicit_fast_still_requires_live_model_capability(self) -> None:
        with mock.patch.object(
            voice, "_agent_codex_fast_ok", return_value=False
        ):
            cmd = voice._agent_codex_cmd(
                "完成任务",
                model="gpt-5.3-codex-spark",
                fast=True,
            )
        self.assertEqual(_service_tier_args(cmd), [])

    def test_agent_run_cli_forwards_fast_only_to_codex_command(self) -> None:
        class _Proc:
            pid = 321
            stdout: list[str] = []

            def wait(self, timeout=None):
                return 0

        with (
            mock.patch.object(
                voice, "_agent_codex_cmd", return_value=["codex", "exec"]
            ) as make_cmd,
            mock.patch.object(voice.Path, "read_text", return_value="test-token"),
            mock.patch.object(voice.subprocess, "Popen", return_value=_Proc()),
            mock.patch.object(voice, "_vtask_set"),
        ):
            result = voice._agent_run_cli(
                "codex", "任务", "系统", "task-id", [],
                model="gpt-5.6-luna", effort="low", fast=True,
            )

        self.assertEqual(result, "")
        make_cmd.assert_called_once_with(
            "任务\n\n系统",
            model="gpt-5.6-luna",
            effort="low",
            fast=True,
        )


class VoiceAgentTaskFastRoutingTest(unittest.TestCase):
    @staticmethod
    def _run_task(params: dict, answers: list[str]) -> list[tuple]:
        calls: list[tuple] = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return answers.pop(0)

        fake_assistant = types.SimpleNamespace(
            _convo_load=lambda _uid: [],
            _creation_enabled=lambda _uid, _kind: False,
            _creation_add=lambda *args, **kwargs: None,
        )
        fake_runtime = types.SimpleNamespace(
            list_recipes=lambda: [],
            recipe_log_run=lambda *args, **kwargs: None,
        )
        with (
            mock.patch.dict(
                sys.modules,
                {"assistant": fake_assistant, "task_runtime": fake_runtime},
            ),
            mock.patch.object(voice, "_agent_run_cli", side_effect=fake_run),
            mock.patch.object(voice, "_agent_registry_prompt", return_value="目录"),
            mock.patch.object(voice, "_vtask_set"),
        ):
            voice._task_agent(
                "task-id",
                {"instruction": "请完成测试任务", **params},
                {},
                "https://example.invalid",
            )
        return calls

    def test_primary_codex_consumes_validated_fast_bool(self) -> None:
        [call] = self._run_task(
            {
                "backend": "codex",
                "model": "gpt-5.6-luna",
                "effort": "low",
                "fast": True,
            },
            ["完成"],
        )
        args, kwargs = call
        self.assertEqual(args[0], "codex")
        self.assertIs(kwargs["fast"], True)

    def test_string_true_cannot_enable_primary_codex_fast(self) -> None:
        [call] = self._run_task(
            {"backend": "codex", "fast": "true"},
            ["完成"],
        )
        self.assertIs(call[1]["fast"], False)

    def test_codex_failure_falls_back_to_claude_without_fast(self) -> None:
        calls = self._run_task(
            {"backend": "codex", "fast": True},
            ["", "Claude 完成"],
        )
        self.assertEqual([call[0][0] for call in calls], ["codex", "claude"])
        self.assertIs(calls[0][1]["fast"], True)
        self.assertIs(calls[1][1]["fast"], False)

    def test_claude_failure_codex_fallback_keeps_explicit_fast(self) -> None:
        calls = self._run_task(
            {"backend": "claude", "fast": True},
            ["", "Codex 完成"],
        )
        self.assertEqual([call[0][0] for call in calls], ["claude", "codex"])
        self.assertIs(calls[0][1]["fast"], False)
        self.assertIs(calls[1][1]["fast"], True)


if __name__ == "__main__":
    unittest.main()
