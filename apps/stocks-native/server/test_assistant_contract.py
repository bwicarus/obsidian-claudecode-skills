import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from assistant_contract import contract, sync_contract


class AssistantContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.marker = Path(self.directory.name) / "conversation.capabilities.json"
        self.rpc = AsyncMock(return_value={})

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_same_conversation_is_upgraded_once_without_starting_another_turn(self):
        text, digest = contract("Current application instructions")
        self.assertTrue(await sync_contract(self.rpc, "existing-thread", self.marker, text, digest))
        method, payload = self.rpc.call_args.args
        self.assertEqual(method, "thread/inject_items")
        self.assertEqual(payload["threadId"], "existing-thread")
        self.assertEqual(payload["items"][0]["role"], "developer")
        self.assertFalse(await sync_contract(self.rpc, "existing-thread", self.marker, text, digest))
        self.rpc.assert_awaited_once()

    async def test_failed_injection_does_not_record_success_or_replace_prior_version(self):
        previous = {"threadId": "existing-thread", "digest": "prior"}
        self.marker.write_text(json.dumps(previous))
        self.rpc.side_effect = RuntimeError("transport unavailable")
        with self.assertRaises(RuntimeError):
            await sync_contract(self.rpc, "existing-thread", self.marker, "new", "next")
        self.assertEqual(json.loads(self.marker.read_text()), previous)

    async def test_new_contract_and_new_thread_each_receive_their_own_update(self):
        for thread, version in (("thread-a", "v1"), ("thread-a", "v2"), ("thread-b", "v2")):
            self.assertTrue(await sync_contract(self.rpc, thread, self.marker, version, version))
        self.assertEqual(self.rpc.await_count, 3)
        self.assertEqual(json.loads(self.marker.read_text()), {"threadId": "thread-b", "digest": "v2"})

    async def test_interrupted_marker_write_can_be_recovered_without_changing_thread(self):
        self.marker.write_text("{")
        self.assertTrue(await sync_contract(self.rpc, "same-thread", self.marker, "current", "v2"))
        self.assertEqual(json.loads(self.marker.read_text())["threadId"], "same-thread")
