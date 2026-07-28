"""Offline contracts for the safe Codex app-server protocol probe."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_appserver_probe",
    ROOT / "scripts" / "codex_appserver_probe.py",
)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(probe)


def passing_report(realtime: dict | None = None) -> dict:
    return {
        "auth": "chatgpt",
        "security": {
            "api_key_environment_forwarded": False,
            "auth_file_read_by_probe": False,
            "audio_sent": False,
            "raw_rpc_or_stderr_printed": False,
            "filesystem_reads_allowed": True,
            "thread_ephemeral_requested": True,
            "sandbox_write_policy": "read-only",
            "working_directory": "empty_temporary_directory",
            "trusted_local_config_required": True,
            "user_config_isolated": False,
        },
        "exec": {"jsonl_events": True},
        "schema": {"realtime_methods": ["thread/realtime/start"]},
        "live": {
            "initialize": "ok",
            "thread_start": {"status": "ok", "ephemeral": True},
            "text_turn": {"status": "completed", "marker_exact": True},
            "invalid_interrupt": {"kind": "invalid_params"},
            "interrupt": {"rpc_status": "ok", "status": "interrupted"},
            "unknown_method": {"kind": "request_rejected"},
            "tool_events": {"count": 0, "asserted_zero": True},
            "realtime": realtime
            or {
                "status": "authentication_required",
                "error_class": "api_key_required",
            },
        },
    }


class ProbeEvaluationTest(unittest.TestCase):
    def test_auth_blocked_realtime_is_a_valid_capability_probe_result(self) -> None:
        self.assertTrue(probe.evaluate(passing_report(), live_requested=True))

    def test_available_realtime_requires_successful_safe_stop(self) -> None:
        available = passing_report(
            {
                "status": "capability_available",
                "stop_request_accepted": True,
                "closed_observed": True,
                "safe_stop": True,
            }
        )
        self.assertTrue(probe.evaluate(available, live_requested=True))

        for field in (
            "stop_request_accepted",
            "closed_observed",
            "safe_stop",
        ):
            with self.subTest(field=field):
                unsafe = copy.deepcopy(available)
                unsafe["live"]["realtime"][field] = False
                self.assertFalse(probe.evaluate(unsafe, live_requested=True))

    def test_ephemeral_exact_marker_and_zero_tools_are_invariants(self) -> None:
        cases = (
            ("ephemeral", ("thread_start", "ephemeral"), False),
            ("exact marker", ("text_turn", "marker_exact"), False),
            ("zero tools", ("tool_events", "count"), 1),
        )
        for label, (section, key), value in cases:
            with self.subTest(label=label):
                report = passing_report()
                report["live"][section][key] = value
                self.assertFalse(probe.evaluate(report, live_requested=True))

    def test_security_contract_checks_cwd_sandbox_and_config_boundary(self) -> None:
        cases = (
            ("thread_ephemeral_requested", False),
            ("sandbox_write_policy", "workspace-write"),
            ("working_directory", "project_directory"),
            ("trusted_local_config_required", False),
            ("user_config_isolated", True),
        )
        for key, value in cases:
            with self.subTest(key=key):
                report = passing_report()
                report["security"][key] = value
                self.assertFalse(probe.evaluate(report, live_requested=True))


class ProbeSelectionAndToolAuditTest(unittest.TestCase):
    def test_version_selection_uses_explicit_priority_not_enum_order(self) -> None:
        self.assertEqual(
            probe.first_supported(
                probe.REALTIME_VERSION_PRIORITY,
                ["v2", "v1", "v3"],
            ),
            "v3",
        )
        self.assertIsNone(
            probe.first_supported(probe.REALTIME_VERSION_PRIORITY, ["v9"])
        )

    def test_tool_audit_ignores_startup_but_counts_all_tool_item_names(self) -> None:
        rpc = probe.RpcProcess(["codex"], "/tmp")
        rpc._record_tool_event(
            {
                "method": "mcpServer/startupStatus/updated",
                "params": {"name": "example", "status": "ready"},
            }
        )
        self.assertEqual(rpc.tool_event_count, 0)

        rpc._record_tool_event(
            {
                "method": "item/started",
                "params": {"item": {"type": "mcpToolCall"}},
            }
        )
        self.assertEqual(rpc.tool_event_count, 1)
        self.assertIn("mcptoolcall", rpc.tool_event_kinds)

        for item_type in (
            "collabAgentToolCall",
            "collabToolCall",
            "imageView",
        ):
            with self.subTest(item_type=item_type):
                before = rpc.tool_event_count
                rpc._record_tool_event(
                    {
                        "method": "item/started",
                        "params": {"item": {"type": item_type}},
                    }
                )
                self.assertEqual(rpc.tool_event_count, before + 1)
                self.assertIn(item_type.lower(), rpc.tool_event_kinds)


class RealtimeStopTest(unittest.TestCase):
    def test_safe_stop_requires_acknowledgement_and_closed_event(self) -> None:
        class FakeRpc:
            def __init__(self):
                self.requests = []

            def request(self, method, params, timeout):
                self.requests.append((method, params, timeout))
                return {}

            def wait_event(self, predicate, timeout, label):
                event = {
                    "method": "thread/realtime/closed",
                    "params": {"threadId": "thread-probe"},
                }
                if not predicate(event):
                    raise AssertionError("closed predicate rejected matching event")
                return event

        rpc = FakeRpc()
        result = probe.stop_realtime_safely(rpc, "thread-probe", 30)
        self.assertEqual(
            result,
            {
                "stop_request_accepted": True,
                "closed_observed": True,
                "safe_stop": True,
            },
        )
        self.assertEqual(
            rpc.requests,
            [
                (
                    "thread/realtime/stop",
                    {"threadId": "thread-probe"},
                    15,
                )
            ],
        )

    def test_acknowledgement_without_closed_event_is_not_safe_stop(self) -> None:
        class FakeRpc:
            def request(self, method, params, timeout):
                return {}

            def wait_event(self, predicate, timeout, label):
                raise probe.ProbeFailure("closed event timed out")

        result = probe.stop_realtime_safely(
            FakeRpc(), "thread-probe", 30
        )
        self.assertEqual(
            result,
            {
                "stop_request_accepted": True,
                "closed_observed": False,
                "safe_stop": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
