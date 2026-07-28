"""Focused, offline contracts for the Codex app-server client."""

from __future__ import annotations

import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import assistant  # noqa: E402


class CodexAppTurnStreamTest(unittest.TestCase):
    def _client(self, thread_id: str = "thread-probe"):
        client = assistant._CodexApp()
        client._turns[thread_id] = queue.Queue()
        return client

    def test_timeout_interrupts_exact_started_turn_with_rpc(self) -> None:
        client = self._client()
        calls: list[tuple[str, dict, int]] = []

        def fake_rpc(method, params, timeout=20):
            calls.append((method, dict(params), timeout))
            if method == "turn/start":
                return {"turn": {"id": "turn-probe"}}
            if method == "turn/interrupt":
                return {}
            self.fail(f"unexpected RPC: {method}")

        client._rpc = fake_rpc
        client._notify = Mock(
            side_effect=AssertionError("turn/interrupt must be an RPC request")
        )

        with self.assertRaisesRegex(RuntimeError, "codex turn 超时"):
            list(client.turn_stream("thread-probe", "hello", timeout=0))

        self.assertEqual(calls[0][0], "turn/start")
        self.assertEqual(
            calls[1],
            (
                "turn/interrupt",
                {"threadId": "thread-probe", "turnId": "turn-probe"},
                5,
            ),
        )
        client._notify.assert_not_called()

    def test_missing_turn_id_fails_before_waiting(self) -> None:
        client = self._client()
        client._rpc = Mock(return_value={"turn": {}})

        with self.assertRaisesRegex(RuntimeError, "turn/start 缺 turn id"):
            list(client.turn_stream("thread-probe", "hello", timeout=0))

        client._rpc.assert_called_once()

    def test_completed_turn_does_not_send_interrupt(self) -> None:
        client = self._client()
        client._turns["thread-probe"].put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-probe",
                    "turn": {"id": "turn-probe", "status": "completed"},
                },
            }
        )
        client._rpc = Mock(return_value={"turn": {"id": "turn-probe"}})

        self.assertEqual(
            list(client.turn_stream("thread-probe", "hello", timeout=1)),
            [],
        )
        client._rpc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
