"""The bounded background analyzer uses text RPC only; all processes are fake."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from monitor_ai import explain_signal


class MonitorAITests(unittest.IsolatedAsyncioTestCase):
    def process(self, responses):
        sent = []
        process = SimpleNamespace(returncode=None,
            stdin=SimpleNamespace(write=lambda data: sent.append(json.loads(data)), drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(side_effect=[
                (json.dumps(value) + "\n").encode() if value is not None else b"" for value in responses])),
            terminate=Mock(), kill=Mock(), wait=AsyncMock(return_value=0))
        return process, sent

    async def test_final_text_and_exit_never_open_realtime_or_run_tools(self):
        process, sent = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thread-1"}}},
            {"id": 3, "result": {"turn": {"id": "turn-1"}}},
            {"id": "unexpected-tool", "method": "item/tool/call", "params": {"name": "anything"}},
            {"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1",
                "item": {"id": "answer", "type": "agentMessage", "phase": "final", "text": "触发价格阈值，行情时间为 10:00。"}}},
            {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}},
        ])
        with tempfile.TemporaryDirectory() as root, patch("monitor_ai.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            answer = await explain_signal({"id": "notice", "code": "600000", "body": "price > 10"}, Path(root))
        self.assertEqual(answer, "触发价格阈值，行情时间为 10:00。")
        self.assertEqual([message["method"] for message in sent if "method" in message],
                         ["initialize", "initialized", "thread/start", "turn/start"])
        self.assertEqual(sent[-1]["error"]["code"], -32601)
        self.assertTrue(sent[2]["params"]["ephemeral"])
        self.assertFalse(sent[2]["params"]["config"]["features.shell_tool"])
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_rpc_error_and_eof_both_close_process(self):
        for responses in ([{"id": 1, "error": {"message": "failed"}}], [None]):
            with self.subTest(responses=responses):
                process, sent = self.process(responses)
                with tempfile.TemporaryDirectory() as root, patch("monitor_ai.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
                    with self.assertRaises(RuntimeError):
                        await explain_signal({"id": "notice"}, Path(root))
                self.assertEqual([message["method"] for message in sent], ["initialize"])
                process.terminate.assert_called_once()
                process.wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
