"""Explicit app call contracts with local SQLite only; no APNs or model calls."""
import json
import tempfile
import unittest

from monitoring import MonitorService
from monitor_tools import dispatch_call_safe
from notification_delivery import NotificationDelivery


class CallToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = 1_789_876_800.0
        self.service = MonitorService(self.directory.name, clock=lambda: self.now)
        self.delivery = NotificationDelivery(self.directory.name, clock=lambda: self.now)
        self.delivery.register("alice", "private-ipad-identifier", {
            "pushToken": "a" * 64, "voipToken": "b" * 64, "notificationsEnabled": True,
        })

    def tearDown(self):
        self.directory.cleanup()

    def request(self, request_id="call-intent", **kwargs):
        return dispatch_call_safe(self.service, self.delivery, "alice", "request", {
            "requestId": request_id, "title": "行情提醒", "text": "这是带日期的测试行情。",
            "code": "000628", **kwargs,
        }, push_configured=True)

    def status(self, notice_id, owner="alice"):
        return dispatch_call_safe(self.service, self.delivery, owner, "status", {
            "notificationId": notice_id,
        }, push_configured=True)

    def test_request_is_owned_idempotent_and_creates_no_call_before_dispatch(self):
        first = self.request()
        second = self.request()
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"]["state"], "queued")
        self.assertTrue(second["result"]["replayed"])
        self.assertEqual(first["result"]["notificationId"], second["result"]["notificationId"])
        self.assertEqual(first["result"]["revision"], second["result"]["revision"])
        notices = self.service.library("alice")["notifications"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["deliveryMode"], "call")
        self.assertEqual(notices[0]["aiState"], "complete")
        self.assertEqual(self.service.library("bob")["notifications"], [])
        with self.delivery.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)
        conflict = self.request(text="其他意图不能复用相同请求号")
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "request_id_conflict")

    def test_owner_cannot_be_supplied_at_any_depth(self):
        for injected in ({"owner": "bob"}, {"extra": [{"OWNER_ID": "bob"}]},
                         {"title": {"ownerId": "bob"}}):
            with self.subTest(injected=injected):
                result = self.request(**injected)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "owner_not_allowed")
        self.assertEqual(self.service.library("alice")["notifications"], [])

    def test_status_cannot_read_another_accounts_notice(self):
        notice_id = self.request()["result"]["notificationId"]
        result = self.status(notice_id, owner="bob")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "notification_not_found")
        self.assertNotIn(notice_id, json.dumps(result))

    def test_status_does_not_expose_device_identifiers_or_tokens(self):
        notice_id = self.request()["result"]["notificationId"]
        self.delivery.create_call("alice", "private-ipad-identifier", notice_id)
        for request in (None, {"notificationId": notice_id}):
            result = dispatch_call_safe(self.service, self.delivery, "alice", "status", request,
                                        push_configured=True)
            self.assertTrue(result["ok"])
            serialized = json.dumps(result)
            for private in ("private-ipad-identifier", "a" * 64, "b" * 64, '"device"', '"owner"'):
                self.assertNotIn(private, serialized)

    def test_unavailable_does_not_create_notice(self):
        for owner, configured, reason in (("alice", False, "push_not_configured"),
                                           ("bob", True, "no_registered_call_device")):
            with self.subTest(reason=reason):
                result = dispatch_call_safe(self.service, self.delivery, owner, "request", {
                    "requestId": "unavailable", "text": "不能创建无法投递的来电",
                }, push_configured=configured)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "call_unavailable")
                self.assertEqual(result["error"]["detail"]["reason"], reason)
                self.assertEqual(self.service.library(owner)["notifications"], [])

    def test_active_voice_waits_without_hanging_up_or_creating_a_call(self):
        self.delivery.voice_presence("alice", "private-ipad-identifier")
        result = self.request()["result"]
        self.assertEqual(result["state"], "waiting_for_current_voice")
        self.assertTrue(result["activeVoice"])
        self.assertTrue(self.delivery.has_voice("alice"))
        self.assertNotIn("callId", result)
        self.delivery.voice_presence("alice", "private-ipad-identifier", connected=False)
        self.assertEqual(self.status(result["notificationId"])["result"]["state"], "queued")

    def test_push_acceptance_is_not_answer_and_answer_is_not_audio(self):
        notice_id = self.request()["result"]["notificationId"]
        call = self.delivery.create_call("alice", "private-ipad-identifier", notice_id)
        self.assertEqual(self.status(notice_id)["result"]["state"], "submitting")
        self.service.mark_delivery("alice", notice_id, {"call": "push_accepted"})
        accepted = self.status(notice_id)["result"]
        self.assertEqual(accepted["state"], "push_accepted")
        self.assertFalse(accepted["answered"])
        self.assertFalse(accepted["audioSubmitted"])
        self.delivery.call_receipt("alice", "private-ipad-identifier", notice_id, call["callId"], "answered")
        answered = self.status(notice_id)["result"]
        self.assertEqual(answered["state"], "answered")
        self.assertTrue(answered["answered"])
        self.assertFalse(answered["audioSubmitted"])
        self.delivery.receipt("alice", notice_id, "spoken", "account", "submitted")
        self.assertTrue(self.status(notice_id)["result"]["audioSubmitted"])
        self.delivery.receipt("alice", notice_id, "spoken", "account", "failed")
        self.assertFalse(self.status(notice_id)["result"]["audioSubmitted"])

    def test_wait_expires_at_ten_minutes_and_resolve_cancels_pending_call(self):
        expired = self.request("expires")["result"]["notificationId"]
        cancelled = self.request("cancelled")["result"]["notificationId"]
        self.now += 599
        self.assertEqual(self.status(expired)["result"]["state"], "queued")
        self.service.mutate("alice", {"requestId": "resolve", "operation": "notification.resolve", "id": cancelled})
        self.assertEqual(self.status(cancelled)["result"]["state"], "cancelled")
        self.now += 1
        self.assertEqual(self.status(expired)["result"]["state"], "expired")
        self.assertEqual(self.status(cancelled)["result"]["state"], "cancelled")

    def test_missed_declined_and_ended_are_reported_without_redial(self):
        for outcome in ("missed", "declined", "ended"):
            with self.subTest(outcome=outcome):
                notice_id = self.request(outcome)["result"]["notificationId"]
                call = self.delivery.create_call("alice", "private-ipad-identifier", notice_id)
                if outcome == "missed":
                    self.now = call["expiresAt"]
                elif outcome == "ended":
                    self.delivery.call_receipt("alice", "private-ipad-identifier", notice_id, call["callId"], "answered")
                    self.now += 600
                else:
                    self.delivery.call_receipt("alice", "private-ipad-identifier", notice_id, call["callId"], outcome)
                self.assertEqual(self.status(notice_id)["result"]["state"], outcome)
                self.assertIsNone(self.delivery.create_call("alice", "private-ipad-identifier", notice_id))

    def test_status_rejects_regular_notice_and_invalid_requests(self):
        notice = self.service.mutate("alice", {"requestId": "regular", "operation": "notification.create",
                                               "notification": {"title": "普通通知", "body": "测试"}})
        self.assertEqual(self.status(notice["notificationId"])["error"]["code"], "not_call_request")
        for action, request in (("request", []), ("status", {"notificationId": []}),
                                ("status", {"unexpected": True}), ("request", {"requestId": "x", "text": "x", "at": "tomorrow"}),
                                ("invalid", None)):
            with self.subTest(action=action, request=request):
                result = dispatch_call_safe(self.service, self.delivery, "alice", action, request,
                                            push_configured=True)
                self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
