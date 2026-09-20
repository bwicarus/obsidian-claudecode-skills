"""Delivery contracts using SQLite and fake transports; never contact APNs/AI."""
import os
from pathlib import Path
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from monitoring import MonitorService
from monitor_runtime import MonitorRuntime
from notification_delivery import NotificationDelivery


TOKENS = {"pushToken": "a" * 64, "voipToken": "b" * 64, "notificationsEnabled": True}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.delivery = NotificationDelivery(self.directory.name, clock=lambda: self.now)
        self.delivery.register("alice", "ipad", TOKENS)

    def tearDown(self):
        self.directory.cleanup()

    def test_account_switch_invalidates_old_calls_and_clears_old_tokens(self):
        call = self.delivery.create_call("alice", "ipad", "event-1")
        self.assertTrue(self.delivery.call("alice", "ipad", call["callId"])["valid"])
        self.delivery.register("bob", "ipad", {})
        self.assertEqual(self.delivery.devices("alice"), [])
        device = self.delivery.devices("bob")[0]
        self.assertFalse(device["enabled"])
        self.assertIsNone(device["push"])
        self.assertIsNone(device["voip"])
        self.assertFalse(self.delivery.call("alice", "ipad", call["callId"])["valid"])
        self.assertFalse(self.delivery.call("bob", "ipad", call["callId"])["valid"])
        self.assertIsNone(self.delivery.create_call("alice", "ipad", "event-2"))
        self.delivery.register("bob", "ipad", TOKENS)
        new_call = self.delivery.create_call("bob", "ipad", "bob-event")
        self.assertTrue(self.delivery.call("bob", "ipad", new_call["callId"])["valid"])

    def test_concurrent_instances_reserve_exactly_one_call_and_do_not_redial(self):
        other = NotificationDelivery(self.directory.name, clock=lambda: self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(service.create_call, "alice", "ipad", "event-1")
                     for service in (self.delivery, other)]
            values = [call.result() for call in calls]
        self.assertEqual(sum(value is not None for value in values), 1)
        self.assertIsNone(self.delivery.create_call("alice", "ipad", "event-2"))
        self.now += 46
        self.assertIsNone(other.create_call("alice", "ipad", "event-1"))
        self.assertIsNotNone(other.create_call("alice", "ipad", "event-2"))

    def test_ringing_timeout_answer_deadline_and_idempotent_answer(self):
        missed = self.delivery.create_call("alice", "ipad", "missed")
        self.now = missed["expiresAt"] + 1
        self.assertFalse(self.delivery.call("alice", "ipad", missed["callId"])["valid"])
        with self.assertRaises(ValueError):
            self.delivery.call_receipt("alice", "ipad", "missed", missed["callId"], "answered")
        answered = self.delivery.create_call("alice", "ipad", "answered")
        self.delivery.call_receipt("alice", "ipad", "answered", answered["callId"], "answered")
        original = self.delivery.call("alice", "ipad", answered["callId"])
        self.now += 30
        self.delivery.call_receipt("alice", "ipad", "answered", answered["callId"], "answered")
        self.assertEqual(self.delivery.call("alice", "ipad", answered["callId"])["expiresAt"], original["expiresAt"])
        self.now = original["expiresAt"] + 1
        self.assertFalse(self.delivery.call("alice", "ipad", answered["callId"])["valid"])
        self.assertIsNotNone(self.delivery.create_call("alice", "ipad", "later"))

    def test_call_receipts_require_exact_owner_device_and_notice(self):
        call = self.delivery.create_call("alice", "ipad", "event-1")
        for owner, device, notice in (("bob", "ipad", "event-1"),
                                      ("alice", "phone", "event-1"), ("alice", "ipad", "wrong")):
            with self.assertRaises(ValueError):
                self.delivery.call_receipt(owner, device, notice, call["callId"], "answered")
        self.delivery.call_receipt("alice", "ipad", "event-1", call["callId"], "declined")
        self.assertFalse(self.delivery.call("alice", "ipad", call["callId"])["valid"])
        self.assertIsNone(self.delivery.create_call("alice", "ipad", "event-1"))
        self.assertIsNone(self.delivery.receipt_for("bob", "event-1", "call", "ipad"))
        self.assertEqual(self.delivery.receipt_for("alice", "event-1", "call", "ipad")["outcome"], "declined")

    def test_push_retries_are_bounded_and_failed_speech_is_never_replayed(self):
        for attempt in range(3):
            self.assertTrue(self.delivery.reserve_delivery("alice", "event-1", "push", "ipad"))
            self.assertFalse(self.delivery.reserve_delivery("alice", "event-1", "push", "ipad"))
            self.delivery.receipt("alice", "event-1", "push", "ipad", "failed")
            self.assertFalse(self.delivery.reserve_delivery("alice", "event-1", "push", "ipad"))
            self.now += 61
        self.assertFalse(self.delivery.reserve_delivery("alice", "event-1", "push", "ipad"))
        self.assertEqual(self.delivery.receipt_for("alice", "event-1", "push", "ipad")["attempts"], 3)
        self.assertTrue(self.delivery.reserve_delivery("alice", "event-1", "spoken", "account"))
        self.delivery.receipt("alice", "event-1", "spoken", "account", "failed")
        self.now += 600
        self.assertFalse(self.delivery.reserve_delivery("alice", "event-1", "spoken", "account"))
        self.assertTrue(self.delivery.reserve_delivery("bob", "event-1", "spoken", "account"))

    def test_unregister_clears_both_transports_and_ends_existing_call(self):
        call = self.delivery.create_call("alice", "ipad", "event-1")
        self.delivery.register("alice", "ipad", {"pushToken": None, "voipToken": None})
        device = self.delivery.devices("alice")[0]
        self.assertFalse(device["enabled"])
        self.assertIsNone(device["push"])
        self.assertIsNone(device["voip"])
        self.assertFalse(self.delivery.call("alice", "ipad", call["callId"])["valid"])

    def test_late_old_account_presence_cannot_reclaim_device_binding(self):
        self.delivery.register("bob", "ipad", TOKENS)
        result = self.delivery.presence("alice", "ipad", True)
        self.assertFalse(result["success"])
        self.assertEqual(self.delivery.devices("alice"), [])
        self.assertEqual(self.delivery.devices("bob")[0]["owner"], "bob")

    def test_shared_leases_and_budgets_survive_replacement(self):
        other = NotificationDelivery(self.directory.name, clock=lambda: self.now)
        self.assertTrue(self.delivery.lease(seconds=30))
        self.assertFalse(other.lease(seconds=30))
        self.assertTrue(self.delivery.budget("same-stock", 300))
        self.assertFalse(other.budget("same-stock", 300))
        self.now += 31
        self.assertTrue(other.lease(seconds=30))
        self.assertFalse(self.delivery.lease(seconds=30))
        self.assertFalse(other.budget("same-stock", 300))

    def test_combined_budgets_do_not_consume_a_free_budget_if_another_is_blocked(self):
        self.assertTrue(self.delivery.budget("global", 30))
        self.assertFalse(self.delivery.budgets({"stock": 300, "global": 30}))
        self.assertTrue(self.delivery.budget("stock", 300))
        self.assertFalse(self.delivery.budgets({"other-global": 30, "stock": 300}))
        self.assertTrue(self.delivery.budget("other-global", 30))

    def test_voice_presence_crosses_instances_expires_and_old_disconnect_cannot_erase_new(self):
        other = NotificationDelivery(self.directory.name, clock=lambda: self.now)
        self.delivery.voice_presence("alice", "ipad")
        self.assertTrue(other.has_voice("alice"))
        self.assertFalse(other.has_voice("bob"))
        self.now += 31
        self.assertFalse(other.has_voice("alice"))
        other.voice_presence("alice", "ipad")
        self.delivery.voice_presence("alice", "ipad", False)
        self.assertTrue(self.delivery.has_voice("alice"))
        other.voice_presence("alice", "ipad", False)
        self.assertFalse(self.delivery.has_voice("alice"))


class DeliveryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.service = MonitorService(self.directory.name, clock=lambda: self.now)
        self.delivery = NotificationDelivery(self.directory.name, clock=lambda: self.now)
        self.delivery.register("alice", "ipad", TOKENS)
        self.delivery.configured = Mock(return_value=True)
        self.delivery.push = AsyncMock(return_value=True)
        self.app = {"monitor": self.service, "notifications": self.delivery, "voices": {}}
        self.runtime = MonitorRuntime(self.app)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def notice(self, request_id="event-1", **overrides):
        return self.service.mutate("alice", {"requestId": request_id, "operation": "notification.create",
            "notification": {"title": "规则提醒", "body": "触发条件", "code": "600000", "severity": "urgent", **overrides}})["library"]["notifications"][0]

    def call_count(self):
        with self.delivery.db() as db:
            return db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]

    async def test_read_or_resolved_notices_do_not_start_voice_or_push(self):
        notice = self.notice()
        self.service.mutate("alice", {"requestId": "read", "operation": "notification.read", "id": notice["id"]})
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.delivery.push.assert_not_awaited()
        self.assertEqual(len(self.service.library("alice")["notifications"]), 1)
        self.service.mutate("alice", {"requestId": "resolved", "operation": "notification.resolve", "id": notice["id"]})
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)

    async def test_existing_voice_gets_one_announcement_and_never_an_incoming_call(self):
        notice = self.notice()
        session = SimpleNamespace(closed=False, selection_owner="alice", last_activity=10,
            can_announce_notification=Mock(return_value=True), announce_notification=AsyncMock())
        self.app["voices"]["ipad"] = (SimpleNamespace(closed=False), session)
        await self.runtime.deliver()
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        session.announce_notification.assert_awaited_once()
        self.assertEqual(session.announce_notification.call_args.args[0]["id"], notice["id"])
        self.assertEqual(self.service.get_notification("alice", notice["id"])["status"], "unread")

    async def test_busy_voice_defers_announcement_and_never_dials(self):
        self.notice()
        session = SimpleNamespace(closed=False, selection_owner="alice", last_activity=10,
            can_announce_notification=Mock(return_value=False), announce_notification=AsyncMock())
        self.app["voices"]["ipad"] = (SimpleNamespace(closed=False), session)
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        session.announce_notification.assert_not_awaited()
        session.can_announce_notification.return_value = True
        await self.runtime.deliver()
        session.announce_notification.assert_awaited_once()

    async def test_voice_on_another_gateway_suppresses_call_until_presence_expires(self):
        self.notice()
        other = NotificationDelivery(self.directory.name, clock=lambda: self.now)
        other.voice_presence("alice", "ipad")
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.now += 31
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 1)

    async def test_other_account_voice_cannot_receive_notice_or_suppress_urgent_call(self):
        self.notice()
        session = SimpleNamespace(closed=False, selection_owner="bob", last_activity=10,
            can_announce_notification=Mock(return_value=True), announce_notification=AsyncMock())
        self.app["voices"]["bobs-ipad"] = (SimpleNamespace(closed=False), session)
        await self.runtime.deliver()
        await self.runtime.deliver()
        session.announce_notification.assert_not_awaited()
        self.assertEqual(self.call_count(), 1)
        self.assertEqual(sum(len(call.args) == 3 for call in self.delivery.push.await_args_list), 1)

    async def test_foreground_or_old_notice_keeps_visual_record_without_call(self):
        notice = self.notice()
        self.delivery.presence("alice", "ipad", True)
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.delivery.push.assert_not_awaited()
        self.assertEqual(self.service.get_notification("alice", notice["id"])["status"], "unread")
        self.delivery.presence("alice", "ipad", False)
        self.service.mutate("alice", {"requestId": "finish", "operation": "notification.resolve", "id": notice["id"]})
        self.now -= 400
        self.notice("old-event")
        self.now += 400
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.delivery.push.assert_not_awaited()

    async def test_explicit_call_dials_foreground_even_when_severity_is_normal(self):
        notice = self.notice(deliveryMode="call", severity="normal")
        self.delivery.presence("alice", "ipad", True)
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 1)
        self.delivery.push.assert_awaited_once()
        self.assertEqual(len(self.delivery.push.await_args.args), 3)
        current = self.service.get_notification("alice", notice["id"])
        self.assertEqual(current["status"], "unread")
        self.assertEqual(current["delivery"]["call"], "push_accepted")

    async def test_explicit_call_waits_for_local_voice_without_announcing_or_hanging_up(self):
        notice = self.notice(deliveryMode="call")
        self.delivery.presence("alice", "ipad", True)
        session = SimpleNamespace(closed=False, selection_owner="alice", last_activity=10,
            can_announce_notification=Mock(return_value=True), announce_notification=AsyncMock(), close=AsyncMock())
        self.app["voices"]["ipad"] = (SimpleNamespace(closed=False), session)
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        session.announce_notification.assert_not_awaited()
        session.close.assert_not_awaited()
        self.assertEqual(self.service.get_notification("alice", notice["id"])["delivery"]["call"], "queued_busy")
        self.app["voices"].clear()
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 1)
        session.announce_notification.assert_not_awaited()
        session.close.assert_not_awaited()

    async def test_explicit_call_waits_for_shared_voice_even_when_notice_is_read(self):
        notice = self.notice(deliveryMode="call")
        self.delivery.presence("alice", "ipad", True)
        other = NotificationDelivery(self.directory.name, clock=lambda: self.now)
        other.voice_presence("alice", "ipad")
        self.service.mutate("alice", {"requestId": "read", "operation": "notification.read", "id": notice["id"]})
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.assertEqual(self.service.get_notification("alice", notice["id"])["delivery"]["call"], "queued_busy")
        other.voice_presence("alice", "ipad", False)
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 1)
        self.assertEqual(self.service.get_notification("alice", notice["id"])["status"], "read")

    async def test_resolving_explicit_call_cancels_queued_delivery(self):
        notice = self.notice(deliveryMode="call")
        self.delivery.presence("alice", "ipad", True)
        self.delivery.voice_presence("alice", "ipad")
        await self.runtime.deliver()
        self.service.mutate("alice", {"requestId": "resolve", "operation": "notification.resolve", "id": notice["id"]})
        self.delivery.voice_presence("alice", "ipad", False)
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.delivery.push.assert_not_awaited()

    async def test_explicit_call_rechecks_resolution_after_pending_snapshot(self):
        notice = self.notice(deliveryMode="call")
        self.delivery.presence("alice", "ipad", True)
        pending = self.service.pending_notifications()
        self.service.mutate("alice", {"requestId": "resolve", "operation": "notification.resolve", "id": notice["id"]})
        with patch.object(self.service, "pending_notifications", return_value=pending):
            await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.delivery.push.assert_not_awaited()

    async def test_explicit_call_can_wait_past_five_minutes_but_expires_at_ten(self):
        first = self.notice(deliveryMode="call")
        self.now += 599
        self.delivery.presence("alice", "ipad", True)
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 1)
        self.assertEqual(self.service.get_notification("alice", first["id"])["delivery"]["call"], "push_accepted")
        self.service.mutate("alice", {"requestId": "resolve-first", "operation": "notification.resolve", "id": first["id"]})
        second = self.notice("second", deliveryMode="call")
        self.now += 600
        await self.runtime.deliver()
        current = self.service.get_notification("alice", second["id"])
        self.assertEqual(self.call_count(), 1)
        self.assertEqual(current["status"], "unread")
        self.assertEqual(current["delivery"]["call"], "expired")

    async def test_explicit_call_expired_while_voice_active_never_dials_after_disconnect(self):
        notice = self.notice(deliveryMode="call")
        self.delivery.presence("alice", "ipad", True)
        self.delivery.voice_presence("alice", "ipad")
        await self.runtime.deliver()
        self.now += 600
        self.delivery.voice_presence("alice", "ipad", False)
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.assertEqual(self.service.get_notification("alice", notice["id"])["delivery"]["call"], "expired")
        self.delivery.push.assert_not_awaited()

    async def test_explicit_call_never_redials_after_failed_push_or_an_expired_ring(self):
        for success in (False, True):
            with self.subTest(success=success):
                notice = self.notice("once-" + str(success), deliveryMode="call")
                self.delivery.presence("alice", "ipad", True)
                self.delivery.push.return_value = success
                self.delivery.push.reset_mock()
                await self.runtime.deliver()
                self.now += 46
                self.delivery.presence("alice", "ipad", True)
                await self.runtime.deliver()
                self.delivery.push.assert_awaited_once()
                self.assertEqual(self.service.get_notification("alice", notice["id"])["delivery"]["call"],
                                 "push_accepted" if success else "failed")
                self.service.mutate("alice", {"requestId": "resolve-" + str(success),
                    "operation": "notification.resolve", "id": notice["id"]})

    async def test_auto_normal_notice_pushes_without_calling(self):
        self.notice(deliveryMode="auto", severity="normal")
        await self.runtime.deliver()
        self.assertEqual(self.call_count(), 0)
        self.delivery.push.assert_awaited_once()
        self.assertEqual(len(self.delivery.push.await_args.args), 2)


class PushBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_apns_configuration_error_keeps_token_but_invalid_token_is_removed(self):
        for reason, should_remove in (("BadTopic", False), ("BadDeviceToken", True)):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                delivery = NotificationDelivery(root)
                delivery.register("alice", "ipad", TOKENS)
                key = Path(root) / "test-key.p8"
                key.write_text("not a real key")
                response = SimpleNamespace(status_code=400, json=lambda: {"reason": reason})
                client = AsyncMock()
                client.__aenter__.return_value = client
                client.post.return_value = response
                fake_httpx = SimpleNamespace(AsyncClient=Mock(return_value=client))
                fake_jwt = SimpleNamespace(encode=Mock(return_value="not a real token"))
                with patch.dict(sys.modules, {"httpx": fake_httpx, "jwt": fake_jwt}), patch.dict(os.environ, {
                    "STOCKS_APNS_KEY_FILE": str(key), "STOCKS_APNS_KEY_ID": "test", "STOCKS_APNS_TEAM_ID": "test"}):
                    self.assertFalse(await delivery.push(delivery.devices("alice")[0], {"id": "n", "title": "t", "body": "b"}))
                device = delivery.devices("alice")[0]
                self.assertEqual(device["push"] is None, should_remove)
                self.assertEqual(device["voip"], TOKENS["voipToken"])

    async def test_stale_device_snapshot_cannot_send_after_account_rebind(self):
        with tempfile.TemporaryDirectory() as root:
            delivery = NotificationDelivery(root)
            delivery.register("alice", "ipad", TOKENS)
            old = delivery.devices("alice")[0]
            delivery.register("bob", "ipad", TOKENS)
            key = Path(root) / "test-key.p8"
            key.write_text("not a real key")
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.post.return_value = SimpleNamespace(status_code=200)
            factory = Mock(return_value=client)
            fake_httpx = SimpleNamespace(AsyncClient=factory)
            fake_jwt = SimpleNamespace(encode=Mock(return_value="not a real token"))
            with patch.dict(sys.modules, {"httpx": fake_httpx, "jwt": fake_jwt}), patch.dict(os.environ, {
                "STOCKS_APNS_KEY_FILE": str(key), "STOCKS_APNS_KEY_ID": "test", "STOCKS_APNS_TEAM_ID": "test"}):
                sent = await delivery.push(old, {"id": "n", "title": "private", "body": "private"})
            self.assertFalse(sent)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
