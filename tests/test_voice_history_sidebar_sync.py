"""Isolated contracts for the packaged voice-history sidebar synchronizer.

All inputs and durable files are temporary. Publishers are fakes, so this test
module cannot contact a Reader service or any external machine.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = (
    ROOT
    / "extensions"
    / "bw-reader-webext"
    / "windows"
    / "computer-voice-desktop"
)
SPEC = importlib.util.spec_from_file_location(
    "voice_history_sidebar_sync",
    RUNTIME_ROOT / "voice_history_sidebar_sync.py",
)
assert SPEC and SPEC.loader
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)
SIDEBAR_SPEC = importlib.util.spec_from_file_location(
    "sidebar_bridge_client_contract_test",
    RUNTIME_ROOT / "sidebar_bridge_client.py",
)
assert SIDEBAR_SPEC and SIDEBAR_SPEC.loader
SIDEBAR = importlib.util.module_from_spec(SIDEBAR_SPEC)
SIDEBAR_SPEC.loader.exec_module(SIDEBAR)

THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def msg(role: str, text: str) -> dict[str, str]:
    return {"role": role, "text": text}


class VoiceHistorySidebarSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        self.global_state = root / "global.json"
        self.continuity = root / "continuity.json"
        self.state = root / "state.json"
        self.archive = root / "archive.json"
        self.bind()

    @staticmethod
    def write(path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def bind(self, thread=THREAD, host="local") -> None:
        self.write(
            self.global_state,
            {
                "other": True,
                "electron-persisted-atom-state": {
                    "other": "allowed",
                    SYNC.RECENT_THREAD_KEY: {
                        "conversationId": thread,
                        "hostId": host,
                    },
                },
            },
        )

    def recent(self, items, thread=THREAD) -> None:
        self.write(
            self.continuity,
            {"version": 1, "threads": {thread: {"items": items}}},
        )

    def sync(self, **kwargs):
        return SYNC.sync_once(
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
            **kwargs,
        )

    def archived(self):
        return json.loads(self.archive.read_text(encoding="utf-8"))[
            "threads"
        ][THREAD]

    def test_binding_tolerates_unknown_codex_fields(self):
        # 2026-09-06:Codex 正式版给最近线程记录加了 isEverydayWorkMode,
        # 旧的"精确 schema"检查把它当成读不到 → 退回上一次绑定的旧线程。
        self.write(
            self.global_state,
            {
                "electron-persisted-atom-state": {
                    SYNC.RECENT_THREAD_KEY: {
                        "conversationId": THREAD,
                        "hostId": "local",
                        "version": 3,
                        "isEverydayWorkMode": False,
                        "someFutureField": {"nested": True},
                    },
                },
            },
        )
        self.recent([msg("user", "u"), msg("assistant", "a")])
        result = self.sync()
        self.assertEqual(result["threadId"], THREAD)
        self.assertIsNone(result.get("error"))
        self.assertFalse(result["stale"])

    def test_binding_still_requires_selection_fields(self):
        self.write(
            self.global_state,
            {
                "electron-persisted-atom-state": {
                    SYNC.RECENT_THREAD_KEY: {"hostId": "local"},
                },
            },
        )
        self.recent([msg("user", "u"), msg("assistant", "a")])
        result = self.sync()
        self.assertTrue(result["stale"])
        self.assertIn("missing=['conversationId']", result["error"])

    def test_exact_local_uuid_binding_never_guesses_another_thread(self):
        self.bind(host="remote")
        self.recent([msg("user", "x")])
        self.assertIsNone(self.sync()["threadId"])
        self.assertFalse(self.archive.exists())

        self.bind()
        self.recent([msg("user", "other")], thread=OTHER)
        result = self.sync()
        self.assertEqual(result["threadId"], THREAD)
        self.assertEqual(result["items"], 0)

    def test_longest_overlap_extends_pending_turn(self):
        self.recent(
            [msg("user", "u1"), msg("assistant", "a1"), msg("user", "u2")]
        )
        first = self.sync()
        self.assertEqual((first["items"], first["pairs"]), (3, 1))

        self.recent(
            [msg("user", "u2"), msg("assistant", "a2"), msg("user", "u3")]
        )
        second = self.sync()
        self.assertEqual((second["items"], second["pairs"]), (5, 2))
        self.assertFalse(second["gap"])
        self.assertEqual(self.archived()["gaps"], [])

    def test_no_overlap_records_gap_and_pairing_does_not_cross_it(self):
        self.recent(
            [msg("user", "u1"), msg("assistant", "a1"), msg("user", "lost")]
        )
        self.sync()
        self.recent(
            [
                msg("assistant", "orphan"),
                msg("user", "u2"),
                msg("assistant", "a2"),
            ]
        )
        result = self.sync()
        self.assertTrue(result["gap"])
        self.assertEqual(result["pairs"], 2)
        self.assertEqual(self.archived()["gaps"], [3])

    def test_strict_schema_uses_last_good_and_keeps_archive(self):
        self.recent([msg("user", "u"), msg("assistant", "a")])
        self.sync()
        before = self.archive.read_bytes()
        self.write(
            self.continuity,
            {
                "version": 1,
                "threads": {
                    THREAD: {
                        "items": [
                            {
                                "role": "assistant",
                                "text": "reasoning",
                                "unexpected": True,
                            }
                        ]
                    }
                },
            },
        )
        result = self.sync()
        self.assertTrue(result["stale"])
        self.assertTrue(result["gap"])
        self.assertIn("schema mismatch", result["error"])
        self.assertEqual(self.archive.read_bytes(), before)

    def test_dry_run_does_not_call_publisher(self):
        self.recent([msg("user", "u"), msg("assistant", "a")])
        calls = []

        def fake(*args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

        result = self.sync(publisher=fake)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["pending"], 1)
        self.assertEqual(calls, [])

    def test_publish_is_complete_idempotent_and_failure_is_retryable(self):
        self.recent(
            [
                msg("user", "u1"),
                msg("assistant", "a1"),
                msg("user", "unfinished"),
            ]
        )
        rejected = []

        def reject(*args, **kwargs):
            rejected.append((args, kwargs))
            return {"ok": False}

        failed = self.sync(publish=True, publisher=reject)
        self.assertEqual((failed["published"], failed["pending"]), (0, 1))
        self.assertEqual(len(rejected), 1)
        rejected_id = rejected[0][1]["request_id"]

        accepted = []

        def accept(*args, **kwargs):
            accepted.append((args, kwargs))
            return {"ok": True}

        recovered = self.sync(
            publish=True,
            publisher=accept,
            file="book.pdf",
            page=3,
        )
        self.assertEqual((recovered["published"], recovered["pending"]), (1, 0))
        self.assertEqual(accepted[0][0], (
            "assistant_turn",
            {"text": "a1", "user_utterance": "u1"},
        ))
        self.assertEqual(accepted[0][1]["request_id"], rejected_id)
        self.assertEqual(accepted[0][1]["file"], "book.pdf")
        self.assertEqual(accepted[0][1]["page"], 3)

        replay = self.sync(publish=True, publisher=accept)
        self.assertEqual(replay["published"], 0)
        self.assertEqual(len(accepted), 1)

    def test_oversized_source_uses_last_good(self):
        self.recent([msg("user", "u"), msg("assistant", "a")])
        self.sync()
        before = self.archive.read_bytes()
        self.continuity.write_bytes(b" " * (SYNC.MAX_CONTINUITY_BYTES + 1))
        result = self.sync()
        self.assertTrue(result["stale"])
        self.assertIn("exceeds", result["error"])
        self.assertEqual(self.archive.read_bytes(), before)

    def test_capture_bound_sync_publishes_only_active_and_late_tail_turns(self):
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        snapshot = self.root / "reader-context-snapshot.json"
        self.write(
            snapshot,
            {
                "schema": "reader-context-snapshot/1",
                "revision": 17,
                "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                "activeReading": {
                    "fresh": True,
                    "ageSec": 0,
                    "sourceInstanceId": "app-reader-1",
                },
                "contextStatus": "ready",
                "currentPage": {
                    "file": "Books/book.pdf",
                    "page": 7,
                    "stable": True,
                    "sourceInstanceId": "app-reader-1",
                },
            },
        )
        calls = []

        def accept(*args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=accept,
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
            snapshot_path=snapshot,
        )
        self.assertIsNone(
            syncer.observe(
                service_online=True,
                capture_active=False,
                snapshot_mode=True,
            )
        )
        armed = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertTrue(armed["dryRun"])
        self.assertEqual(armed["published"], 0)
        self.assertEqual(calls, [])
        unchanged = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(unchanged["published"], 0)
        self.assertEqual(calls, [])

        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "u1"),
                msg("assistant", "a1"),
            ]
        )
        active = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(active["published"], 1)
        self.assertEqual(calls[0][0][0], "assistant_turn")
        self.assertEqual(
            calls[0][0][1],
            {"text": "a1", "user_utterance": "u1"},
        )
        self.assertEqual(calls[0][1]["file"], "Books/book.pdf")
        self.assertEqual(calls[0][1]["page"], 7)
        self.assertEqual(calls[0][1]["source_instance_id"], "app-reader-1")
        self.assertEqual(calls[0][1]["snapshot_revision"], 17)
        self.assertEqual(calls[0][1]["thread_id"], THREAD)

        # Binding changes mid-call cannot publish another thread.
        self.bind(OTHER)
        self.recent(
            [msg("user", "other-u"), msg("assistant", "other-a")],
            thread=OTHER,
        )
        changed = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertTrue(changed["stale"])
        self.assertIn("lease changed", changed["error"])
        self.assertEqual(len(calls), 1)

        # The continuity writer can land just after captureActive becomes
        # false.  The bounded tail must pick it up without replaying a1.
        self.bind()
        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "u1"),
                msg("assistant", "a1"),
                msg("user", "u2"),
                msg("assistant", "a2"),
            ]
        )
        tail = syncer.observe(
            service_online=True,
            capture_active=False,
            snapshot_mode=True,
        )
        self.assertEqual(tail["published"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[1][0],
            (
                "assistant_turn",
                {"text": "a2", "user_utterance": "u2"},
            ),
        )
        self.assertNotIn("result", calls[1][0][1])
        syncer.observe(
            service_online=True,
            capture_active=False,
            snapshot_mode=True,
        )
        syncer.observe(
            service_online=True,
            capture_active=False,
            snapshot_mode=True,
        )
        self.assertEqual(len(calls), 2)

    def test_activation_watermark_rejects_pair_with_pre_activation_user(self):
        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "pre-activation-unfinished"),
            ]
        )
        calls = []

        def accept(*args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=accept,
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
        )
        syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "pre-activation-unfinished"),
                msg("assistant", "must-not-publish"),
            ]
        )
        result = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(result["published"], 0)
        self.assertEqual(calls, [])

        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "pre-activation-unfinished"),
                msg("assistant", "must-not-publish"),
                msg("user", "new-u"),
                msg("assistant", "new-a"),
            ]
        )
        result = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(result["published"], 1)
        self.assertEqual(
            calls[0][0],
            (
                "assistant_turn",
                {"text": "new-a", "user_utterance": "new-u"},
            ),
        )

    def test_active_voice_generation_change_rebinds_history_thread(self):
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=lambda *_args, **_kwargs: {"ok": True},
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
        )
        first = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
            capture_generation=101,
        )
        self.assertEqual(first["threadId"], THREAD)

        self.bind(OTHER)
        self.recent(
            [msg("user", "other-old-u"), msg("assistant", "other-old-a")],
            thread=OTHER,
        )
        rebound = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
            capture_generation=202,
        )
        self.assertEqual(rebound["threadId"], OTHER)
        self.assertEqual(syncer._capture_generation, 202)

    def _route_snapshot_writer(self, snapshot):
        def write_snapshot(source, revision, page, file="Books/book.pdf"):
            self.write(
                snapshot,
                {
                    "schema": "reader-context-snapshot/1",
                    "revision": revision,
                    "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                    "activeReading": {
                        "fresh": True,
                        "ageSec": 0,
                        "sourceInstanceId": source,
                    },
                    "contextStatus": "ready",
                    "currentPage": {
                        "file": file,
                        "page": page,
                        "stable": True,
                        "sourceInstanceId": source,
                    },
                },
            )

        return write_snapshot

    def _armed_route_syncer(self, snapshot, calls):
        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or {"ok": True}
            ),
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
            snapshot_path=snapshot,
        )
        syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "new-u"),
                msg("assistant", "new-a"),
            ]
        )
        return syncer

    def test_capture_lease_never_sends_pending_turn_to_another_book(self):
        # 另一本书上的 Reader 源(不管哪个表面)先扣住,等原来那本回来再发。
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        snapshot = self.root / "reader-context-snapshot.json"
        write_snapshot = self._route_snapshot_writer(snapshot)
        write_snapshot("app-reader-a", 30, 4)
        calls = []
        syncer = self._armed_route_syncer(snapshot, calls)

        write_snapshot("extension-reader-b", 31, 0, file="Books/other.pdf")
        withheld = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(calls, [])
        self.assertEqual(withheld["pending"], 1)
        self.assertTrue(withheld["stale"])
        self.assertIn("source changed", withheld["error"])

        write_snapshot("app-reader-a", 32, 5)
        delivered = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(delivered["published"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["source_instance_id"], "app-reader-a")
        self.assertEqual(calls[0][1]["snapshot_revision"], 32)
        self.assertEqual(calls[0][1]["page"], 5)

    def test_capture_lease_follows_same_book_reader_reconnect(self):
        # 2026-09-06:App 切后台再回来会换 sourceInstanceId,旧的永远不回来。
        # 同一本书换了连接必须直接改绑,否则整段通话的记录都扣到结束。
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        snapshot = self.root / "reader-context-snapshot.json"
        write_snapshot = self._route_snapshot_writer(snapshot)
        write_snapshot("source-first", 30, 4)
        calls = []
        syncer = self._armed_route_syncer(snapshot, calls)

        write_snapshot("source-reconnected", 31, 6)
        delivered = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(delivered["published"], 1)
        self.assertIsNone(delivered.get("error"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["source_instance_id"], "source-reconnected")
        self.assertEqual(calls[0][1]["page"], 6)
        self.assertEqual(syncer._lease_source_instance_id, "source-reconnected")

    def test_capture_lease_follows_user_to_another_book_after_grace(self):
        # 换了书先扣 ROUTE_REBIND_GRACE_SECONDS;过了还没回来就跟着用户走,不丢。
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        snapshot = self.root / "reader-context-snapshot.json"
        write_snapshot = self._route_snapshot_writer(snapshot)
        write_snapshot("app-reader-a", 30, 4)
        calls = []
        syncer = self._armed_route_syncer(snapshot, calls)

        write_snapshot("app-reader-c", 31, 2, file="Books/other.pdf")
        withheld = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(calls, [])
        self.assertIn("source changed", withheld["error"])
        self.assertIsNotNone(syncer._route_mismatch_since)

        syncer._route_mismatch_since -= SYNC.ROUTE_REBIND_GRACE_SECONDS + 1
        delivered = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(delivered["published"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["page"], 2)
        self.assertEqual(syncer._lease_source_file, "Books/other.pdf")

    def test_activation_watermark_handles_non_alternating_existing_roles(self):
        baseline = [
            msg("user", "u0"),
            msg("assistant", "a0"),
            msg("user", "u1"),
            msg("user", "u2"),
            msg("assistant", "a2"),
            msg("assistant", "a3"),
            msg("user", "u4"),
            msg("user", "u5"),
            msg("assistant", "a5"),
            msg("assistant", "a6"),
        ]
        self.recent(baseline)
        calls = []
        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or {"ok": True}
            ),
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
        )
        armed = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(armed["items"], len(baseline))
        self.assertEqual(calls, [])

        self.recent(
            baseline
            + [msg("user", "new-u"), msg("assistant", "new-a")]
        )
        result = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(result["published"], 1)
        self.assertEqual(
            calls[0][0],
            (
                "assistant_turn",
                {"text": "new-a", "user_utterance": "new-u"},
            ),
        )

    def test_snapshot_mode_and_online_status_are_fail_closed_gates(self):
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        calls = []
        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or {"ok": True}
            ),
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
        )
        self.assertIsNone(
            syncer.observe(
                service_online=True,
                capture_active=True,
                snapshot_mode=False,
            )
        )
        self.assertFalse(self.archive.exists())
        self.assertIsNone(
            syncer.observe(
                service_online=False,
                capture_active=True,
                snapshot_mode=True,
            )
        )
        self.assertFalse(self.archive.exists())
        self.assertEqual(calls, [])
        syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "new-u"),
                msg("assistant", "new-a"),
            ]
        )
        self.assertIsNone(
            syncer.observe(
                service_online=True,
                capture_active=False,
                snapshot_mode=False,
            )
        )
        self.assertEqual(calls, [])

    def test_snapshot_anchor_is_optional_and_fail_closed(self):
        snapshot = self.root / "snapshot.json"
        now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
        self.write(
            snapshot,
            {
                "schema": "reader-context-snapshot/1",
                "updatedAtUtc": (now - timedelta(seconds=2)).isoformat(),
                "activeReading": {
                    "file": "Books/a.pdf",
                    "page": 2,
                    "fresh": True,
                    "ageSec": 2,
                },
                "contextStatus": "ready",
                "currentPage": {
                    "file": "Books/a.pdf",
                    "page": 2,
                    "stable": True,
                },
            },
        )
        self.assertEqual(
            SYNC.snapshot_anchor(snapshot, now=now),
            ("Books/a.pdf", 2),
        )
        value = json.loads(snapshot.read_text(encoding="utf-8"))
        value["contextStatus"] = "pending"
        self.write(snapshot, value)
        self.assertEqual(
            SYNC.snapshot_anchor(snapshot, now=now),
            (None, None),
        )
        value["contextStatus"] = "ready"
        value["updatedAtUtc"] = (
            now - SYNC.SNAPSHOT_ANCHOR_MAX_AGE - timedelta(seconds=1)
        ).isoformat()
        self.write(snapshot, value)
        self.assertEqual(
            SYNC.snapshot_anchor(snapshot, now=now),
            (None, None),
        )
        value["updatedAtUtc"] = now.isoformat()
        value["activeReading"]["fresh"] = False
        self.write(snapshot, value)
        self.assertEqual(
            SYNC.snapshot_anchor(snapshot, now=now),
            (None, None),
        )
        value["activeReading"]["fresh"] = True
        value["currentPage"]["file"] = "../secret.pdf"
        self.write(snapshot, value)
        self.assertEqual(
            SYNC.snapshot_anchor(snapshot, now=now),
            (None, None),
        )

    def test_reader_sync_payload_is_rejected_before_publish_or_transport(self):
        self.recent(
            [
                msg("user", "normal"),
                msg("assistant", "[[READER_SYNC]] hidden"),
            ]
        )
        calls = []
        result = self.sync(
            publish=True,
            publisher=lambda *args, **kwargs: calls.append(
                (args, kwargs)
            ),
        )
        self.assertTrue(result["stale"])
        self.assertIn("forbidden", result["error"])
        self.assertEqual(calls, [])

        with patch.object(SIDEBAR, "_run_once") as run_once:
            with self.assertRaises(SIDEBAR.SidebarBridgeError):
                SIDEBAR.call(
                    "assistant_turn",
                    {
                        "text": "safe",
                        "user_utterance": "[[/READER_SYNC]]",
                    },
                )
        run_once.assert_not_called()

    def test_sidebar_uses_exact_local_reader_output_contract(self):
        captured = []

        def send(envelope):
            captured.append(envelope)
            return {
                "contract": "reader-realtime-output/1",
                "type": "output-response",
                "ok": True,
            }

        with patch.object(SIDEBAR, "_run_once", side_effect=send):
            result = SIDEBAR.call(
                "assistant_turn",
                {"text": "answer", "user_utterance": "question"},
                request_id="vh:turn-1",
                file="https://example.test/article",
                page=0,
                source_instance_id="source-1",
                snapshot_revision=12,
                thread_id=THREAD,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(
            captured,
            [
                {
                    "contract": "reader-realtime-output/1",
                    "type": "output-request",
                    "correlation": "vh:turn-1",
                    "sourceInstanceId": "source-1",
                    "snapshotRevision": 12,
                    "file": "https://example.test/article",
                    "page": 0,
                    "kind": "assistant-turn",
                    "payload": {
                        "threadId": THREAD,
                        "user": "question",
                        "assistant": "answer",
                    },
                }
            ],
        )

        with patch.object(SIDEBAR, "_run_once") as run_once:
            with self.assertRaises(SIDEBAR.SidebarBridgeError):
                SIDEBAR.call(
                    "assistant_turn",
                    {"text": "a", "user_utterance": "u"},
                    request_id="请求-1",
                    file="book.pdf",
                    page=1,
                    source_instance_id="source-1",
                    snapshot_revision=12,
                    thread_id=THREAD,
                )
        run_once.assert_not_called()

    def test_snapshot_output_identity_supports_app_and_web_pages(self):
        snapshot = self.root / "output-snapshot.json"
        now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
        value = {
            "schema": "reader-context-snapshot/1",
            "revision": 8,
            "updatedAtUtc": (now - timedelta(seconds=1)).isoformat(),
            "contextStatus": "ready",
            "activeReading": {
                "fresh": True,
                "sourceInstanceId": "web-source",
            },
            "currentPage": {
                "stable": True,
                "sourceInstanceId": "web-source",
                "file": "https://example.test/article",
                "page": 0,
            },
        }
        self.write(snapshot, value)
        self.assertEqual(
            SYNC.snapshot_output_identity(snapshot, now=now),
            {
                "source_instance_id": "web-source",
                "snapshot_revision": 8,
                "file": "https://example.test/article",
                "page": 0,
            },
        )
        value["revision"] = 9
        value["activeReading"]["sourceInstanceId"] = "app-source"
        value["currentPage"].update(
            {
                "sourceInstanceId": "app-source",
                "file": "Books/book.pdf",
                "page": 12,
            }
        )
        self.write(snapshot, value)
        self.assertEqual(
            SYNC.snapshot_output_identity(snapshot, now=now),
            {
                "source_instance_id": "app-source",
                "snapshot_revision": 9,
                "file": "Books/book.pdf",
                "page": 12,
            },
        )
        value["activeReading"]["sourceInstanceId"] = "other-source"
        self.write(snapshot, value)
        self.assertIsNone(SYNC.snapshot_output_identity(snapshot, now=now))

    def test_capture_publish_failure_is_bounded_by_poll_backoff(self):
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        attempts = []

        def reject(*args, **kwargs):
            attempts.append((args, kwargs))
            return {"ok": False}

        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=reject,
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
        )
        syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "new-u"),
                msg("assistant", "new-a"),
            ]
        )
        failed = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(failed["pending"], 1)
        self.assertEqual(len(attempts), 1)
        for _ in range(SYNC.PUBLISH_FAILURE_BACKOFF_POLLS):
            syncer.observe(
                service_online=True,
                capture_active=True,
                snapshot_mode=True,
            )
        self.assertEqual(len(attempts), 1)
        syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(len(attempts), 2)

    def test_structured_projection_uses_only_final_answer_and_real_tools(self):
        projected = SYNC._project_codex_thread(
            {
                "thread": {
                    "id": THREAD,
                    "turns": [
                        {
                            "id": "turn-1",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "id": "user-1",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": (
                                                "<realtime_delegation>"
                                                "<input>查一下天气</input>"
                                                "<transcript_delta>"
                                                "assistant: 我现在去研究一下"
                                                "</transcript_delta>"
                                                "</realtime_delegation>"
                                            ),
                                        }
                                    ],
                                },
                                {
                                    "type": "agentMessage",
                                    "phase": "commentary",
                                    "text": "我现在去研究一下",
                                },
                                {
                                    "type": "mcpToolCall",
                                    "server": "reader",
                                    "tool": "current_page",
                                    "status": "completed",
                                    "durationMs": 42,
                                    "result": {"private": "must not be copied"},
                                },
                                {
                                    "type": "webSearch",
                                    "query": "东京天气",
                                    "results": [{"title": "天气"}],
                                },
                                {
                                    "type": "mcpToolCall",
                                    "server": "reader",
                                    "tool": "private_action",
                                    "status": "failed",
                                    "error": "CAPABILITY_SECRET must stay local",
                                },
                                {
                                    "type": "agentMessage",
                                    "phase": "final_answer",
                                    "text": "东京明天晴，最高 28 度。",
                                },
                            ],
                        }
                    ],
                }
            },
            THREAD,
        )
        self.assertEqual(len(projected["seenRequestIds"]), 1)
        self.assertEqual(len(projected["segments"]), 1)
        segment = projected["segments"][0]
        self.assertEqual(segment["user"], "查一下天气")
        self.assertEqual(segment["assistant"], "东京明天晴，最高 28 度。")
        self.assertNotIn("我现在去研究一下", str(segment))
        self.assertEqual(
            segment["tools"],
            [
                {
                    "status": "done",
                    "tool": "reader.current_page",
                    "label": "工具：reader.current_page",
                    "detail": "完成 · 42 ms",
                },
                {
                    "status": "done",
                    "tool": "web.search",
                    "label": "网页搜索：东京天气",
                    "detail": "搜索完成 · 1 个结果",
                },
                {
                    "status": "error",
                    "tool": "reader.private_action",
                    "label": "工具：reader.private_action",
                    "detail": "执行失败",
                },
            ],
        )
        self.assertNotIn("must not be copied", str(segment))
        self.assertNotIn("CAPABILITY_SECRET", str(segment))

    def test_structured_restart_recovers_unacked_finals_and_tools_once(self):
        # The continuity-era worker acknowledged a short commentary response
        # and then died before the authoritative final answer was mirrored.
        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-final"),
                msg("user", "crash-u"),
                msg("assistant", "我去研究一下"),
            ]
        )
        legacy_calls = []
        seeded = self.sync(
            publish=True,
            publisher=lambda *args, **kwargs: (
                legacy_calls.append((args, kwargs)) or {"ok": True}
            ),
        )
        self.assertEqual(seeded["published"], 2)

        snapshot = self.root / "reader-context-snapshot.json"
        self.write(
            snapshot,
            {
                "schema": "reader-context-snapshot/1",
                "revision": 91,
                "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                "contextStatus": "ready",
                "activeReading": {
                    "fresh": True,
                    "sourceInstanceId": "app-reader-recovery",
                },
                "currentPage": {
                    "stable": True,
                    "sourceInstanceId": "app-reader-recovery",
                    "file": "Books/recovery.pdf",
                    "page": 19,
                },
            },
        )

        def user_item(item_id, text):
            return {
                "type": "userMessage",
                "id": item_id,
                "content": [{"type": "text", "text": text}],
            }

        class FakeHistoryClient:
            def __init__(self, value):
                self.value = value

            def read_thread(self, thread_id):
                return self.value

            def close(self):
                pass

        later_turns = [
            {
                "id": f"later-turn-{index}",
                "items": [
                    user_item(f"later-user-{index}", f"later-u-{index}"),
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": f"later-final-{index}",
                    },
                ],
            }
            for index in range(SYNC.MAX_STRUCTURED_PUBLISH_PER_POLL + 1)
        ]
        history_value = {
            "thread": {
                "id": THREAD,
                "turns": [
                    {
                        "id": "old-turn",
                        "items": [
                            user_item("old-user", "old-u"),
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "old-final",
                            },
                        ],
                    },
                    {
                        "id": "crash-turn",
                        "items": [
                            user_item(
                                "crash-user",
                                "<realtime_delegation><input>crash-u</input>"
                                "<transcript_delta>assistant: 我去研究一下"
                                "</transcript_delta></realtime_delegation>",
                            ),
                            {
                                "type": "agentMessage",
                                "phase": "commentary",
                                "text": "我去研究一下",
                            },
                            {
                                "type": "mcpToolCall",
                                "server": "reader",
                                "tool": "current_page",
                                "status": "completed",
                            },
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "[COMPLETE] crash-final",
                            },
                        ],
                    },
                ] + later_turns,
            }
        }
        calls = []
        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or {"ok": True}
            ),
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
            snapshot_path=snapshot,
            structured_history_client=FakeHistoryClient(history_value),
        )
        armed = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        expected = 1 + len(later_turns)
        self.assertEqual(armed["pending"], expected)
        delivered = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(
            delivered["published"],
            SYNC.MAX_STRUCTURED_PUBLISH_PER_POLL,
        )
        self.assertEqual(delivered["pending"], expected - delivered["published"])
        drained = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(drained["published"], expected - delivered["published"])
        self.assertEqual(drained["pending"], 0)
        self.assertEqual(
            [call[0][0] for call in calls].count("assistant_turn"),
            expected,
        )
        self.assertEqual(
            [call[0][0] for call in calls].count("tool_status"),
            1,
        )
        self.assertEqual(calls[0][0][1]["text"], "[COMPLETE] crash-final")
        self.assertEqual(calls[0][0][1]["user_utterance"], "crash-u")
        self.assertEqual(calls[-1][0][1]["text"], "later-final-8")
        self.assertNotIn(
            "我去研究一下",
            [
                call[0][1]["text"]
                for call in calls
                if call[0][0] == "assistant_turn"
            ],
        )
        self.assertEqual(calls[1][0][1]["tool"], "reader.current_page")

        # A second ReaderPC process starts on the same still-active capture.
        # Durable structured request ids prevent any already ACKed turn from
        # being sent again.
        replay_calls = []
        restarted = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=lambda *args, **kwargs: (
                replay_calls.append((args, kwargs)) or {"ok": True}
            ),
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
            snapshot_path=snapshot,
            structured_history_client=FakeHistoryClient(history_value),
        )
        rearmed = restarted.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(rearmed["pending"], 0)
        restarted.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(replay_calls, [])

    def test_structured_restart_recovery_window_is_bounded(self):
        limit = SYNC.MAX_STRUCTURED_RECOVERY_REQUESTS
        ids = [f"vh2:11111111:{index:024x}" for index in range(limit + 6)]
        segments = [
            {
                "requestId": request_id,
                "user": f"u-{index}",
                "assistant": f"a-{index}",
                "tools": [],
            }
            for index, request_id in enumerate(ids)
        ]
        baseline, pending = SYNC._structured_recovery_baseline(
            projection={
                "threadId": THREAD,
                "seenRequestIds": ids,
                "segments": segments,
            },
            state={
                "version": SYNC.VERSION,
                "lastGood": None,
                "published": {ids[0]: True},
            },
            archive={"version": SYNC.VERSION, "threads": {}},
            thread_id=THREAD,
        )
        self.assertEqual(pending, limit)
        self.assertIn(ids[0], baseline)
        self.assertIn(ids[5], baseline)
        self.assertNotIn(ids[6], baseline)
        self.assertNotIn(ids[-1], baseline)

    def test_published_ack_window_prunes_oldest_and_self_heals_old_state(self):
        overflow = {
            f"vh2:11111111:{index:024x}": True
            for index in range(SYNC.MAX_PUBLISHED + 1)
        }
        state = SYNC._validate_state({
            "version": SYNC.VERSION,
            "lastGood": None,
            "published": overflow,
        })
        self.assertEqual(len(state["published"]), SYNC.MAX_PUBLISHED)
        self.assertNotIn(next(iter(overflow)), state["published"])
        newest = "vh2:22222222:" + "f" * 24
        SYNC._remember_published(state, newest)
        self.assertEqual(len(state["published"]), SYNC.MAX_PUBLISHED)
        self.assertIn(newest, state["published"])
        self.assertNotIn(list(overflow)[1], state["published"])

    def test_structured_fuzzy_recovery_does_not_skip_repeated_prompt(self):
        repeated = "同じ質問"
        self.recent(
            [
                msg("user", repeated),
                msg("assistant", "調べてみるね"),
            ]
        )
        seeded = self.sync(
            publish=True,
            publisher=lambda *args, **kwargs: {"ok": True},
        )
        self.assertEqual(seeded["published"], 1)
        state = SYNC._validate_state(
            json.loads(self.state.read_text(encoding="utf-8"))
        )
        archive = SYNC._validate_archive(
            json.loads(self.archive.read_text(encoding="utf-8"))
        )
        ids = [
            f"vh2:11111111:{index:024x}"
            for index in range(3)
        ]
        segments = [
            {
                "requestId": ids[0],
                "user": repeated,
                "assistant": "最初の完全回答",
                "tools": [],
            },
            {
                "requestId": ids[1],
                "user": "間に落ちた質問",
                "assistant": "間に落ちた回答",
                "tools": [],
            },
            {
                "requestId": ids[2],
                "user": repeated,
                "assistant": "二回目の完全回答",
                "tools": [],
            },
        ]
        baseline, pending = SYNC._structured_recovery_baseline(
            projection={
                "threadId": THREAD,
                "seenRequestIds": ids,
                "segments": segments,
            },
            state=state,
            archive=archive,
            thread_id=THREAD,
        )
        self.assertEqual(pending, 3)
        self.assertNotIn(ids[0], baseline)
        self.assertNotIn(ids[1], baseline)
        self.assertNotIn(ids[2], baseline)

    def test_structured_capture_skips_pre_activation_user_and_publishes_parts(self):
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        snapshot = self.root / "reader-context-snapshot.json"
        self.write(
            snapshot,
            {
                "schema": "reader-context-snapshot/1",
                "revision": 71,
                "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                "contextStatus": "ready",
                "activeReading": {
                    "fresh": True,
                    "sourceInstanceId": "app-reader-1",
                },
                "currentPage": {
                    "stable": True,
                    "sourceInstanceId": "app-reader-1",
                    "file": "Books/book.pdf",
                    "page": 8,
                },
            },
        )

        def user_item(item_id, text):
            return {
                "type": "userMessage",
                "id": item_id,
                "content": [{"type": "text", "text": text}],
            }

        class FakeHistoryClient:
            def __init__(self, value):
                self.value = value
                self.reads = 0
                self.closes = 0

            def read_thread(self, thread_id):
                self.reads += 1
                return self.value

            def close(self):
                self.closes += 1

        initial = {
            "thread": {
                "id": THREAD,
                "turns": [
                    {
                        "id": "old-turn",
                        "items": [
                            user_item("old-user", "old-u"),
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "old-a",
                            },
                        ],
                    },
                    {
                        "id": "pending-turn",
                        "items": [
                            user_item("pending-user", "激活前已经提问"),
                            {
                                "type": "agentMessage",
                                "phase": "commentary",
                                "text": "我去研究一下",
                            },
                        ],
                    },
                ],
            }
        }
        history = FakeHistoryClient(initial)
        calls = []
        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or {"ok": True}
            ),
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
            snapshot_path=snapshot,
            structured_history_client=history,
        )
        armed = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(armed["published"], 0)
        self.assertEqual(calls, [])

        history.value = {
            "thread": {
                "id": THREAD,
                "turns": initial["thread"]["turns"][:-1]
                + [
                    {
                        "id": "pending-turn",
                        "items": [
                            user_item("pending-user", "激活前已经提问"),
                            {
                                "type": "agentMessage",
                                "phase": "commentary",
                                "text": "我去研究一下",
                            },
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "激活前问题的完整答案",
                            },
                        ],
                    },
                    {
                        "id": "new-turn",
                        "items": [
                            user_item("new-user", "新问题"),
                            {
                                "type": "agentMessage",
                                "phase": "commentary",
                                "text": "请稍等",
                            },
                            {
                                "type": "mcpToolCall",
                                "server": "reader",
                                "tool": "selection",
                                "status": "completed",
                            },
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "这是新问题的完整答案",
                            },
                        ],
                    },
                ],
            }
        }
        self.recent(
            [
                msg("user", "old-u"),
                msg("assistant", "old-a"),
                msg("user", "新问题"),
                msg("assistant", "这是新问题的完整答案"),
            ]
        )
        delivered = syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(delivered["published"], 1)
        self.assertEqual([call[0][0] for call in calls], [
            "assistant_turn", "tool_status",
        ])
        self.assertEqual(
            calls[0][0][1],
            {
                "text": "这是新问题的完整答案",
                "user_utterance": "新问题",
            },
        )
        self.assertEqual(calls[1][0][1]["tool"], "reader.selection")
        self.assertEqual(calls[0][1]["source_instance_id"], "app-reader-1")
        self.assertEqual(calls[0][1]["snapshot_revision"], 71)
        self.assertTrue(calls[1][1]["request_id"].endswith(":t0"))
        self.assertNotIn("激活前问题的完整答案", str(calls))

        syncer.observe(
            service_online=True,
            capture_active=True,
            snapshot_mode=True,
        )
        self.assertEqual(len(calls), 2)

    def test_sidebar_tool_status_uses_exact_local_output_contract(self):
        captured = []
        with patch.object(
            SIDEBAR,
            "_run_once",
            side_effect=lambda envelope: (
                captured.append(envelope)
                or {"ok": True}
            ),
        ):
            result = SIDEBAR.call(
                "tool_status",
                {
                    "status": "done",
                    "tool": "reader.selection",
                    "label": "工具：reader.selection",
                    "detail": "完成 · 42 ms",
                },
                request_id="vh2:11111111:abc:t0",
                file="Books/book.pdf",
                page=8,
                source_instance_id="app-reader-1",
                snapshot_revision=71,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(captured[0]["kind"], "tool-status")
        self.assertEqual(
            captured[0]["payload"],
            {
                "status": "done",
                "tool": "reader.selection",
                "label": "工具：reader.selection",
                "detail": "完成 · 42 ms",
            },
        )
        with patch.object(SIDEBAR, "_run_once") as run_once:
            with self.assertRaises(SIDEBAR.SidebarBridgeError):
                SIDEBAR.call(
                    "tool_status",
                    {
                        "status": "done",
                        "tool": "reader.selection",
                        "label": "[[READER_SYNC]]",
                        "detail": None,
                    },
                    request_id="vh2:11111111:abc:t0",
                    file="Books/book.pdf",
                    page=8,
                    source_instance_id="app-reader-1",
                    snapshot_revision=71,
                )
        run_once.assert_not_called()

    def test_reader_mcp_tools_use_readable_sidebar_labels(self):
        snapshot = SYNC._project_tool(
            {
                "type": "mcpToolCall",
                "server": "reader_snapshot",
                "tool": "reader_context_snapshot",
                "status": "completed",
            }
        )
        anki = SYNC._project_tool(
            {
                "type": "mcpToolCall",
                "server": "reader_snapshot",
                "tool": "reader_anki_draft",
                "status": "completed",
            }
        )
        page_card_edit = SYNC._project_tool(
            {
                "type": "mcpToolCall",
                "server": "reader_snapshot",
                "tool": "reader_page_card_edit",
                "status": "completed",
            }
        )
        unknown = SYNC._project_tool(
            {
                "type": "mcpToolCall",
                "server": "other_server",
                "tool": "private_action",
                "status": "completed",
            }
        )

        self.assertEqual(snapshot["tool"], "reader_snapshot.reader_context_snapshot")
        self.assertEqual(snapshot["label"], "读取页面")
        self.assertEqual(anki["tool"], "reader_snapshot.reader_anki_draft")
        self.assertEqual(anki["label"], "制卡")
        self.assertEqual(
            page_card_edit["tool"],
            "reader_snapshot.reader_page_card_edit",
        )
        self.assertEqual(page_card_edit["label"], "修改卡片")
        self.assertEqual(unknown["tool"], "other_server.private_action")
        self.assertEqual(unknown["label"], "工具：other_server.private_action")


class StructuredHistoryWindowTest(unittest.TestCase):
    """用户 2026-09-05:「设置上限就好，比如最多到目前为止 100 条之类的，还有就是
    app 有清空按钮不是么，按下后之前的聊天记录就不需要了」。

    侧栏历史是有界列表，所以投影只看线程末尾这么多轮 —— 更早的轮次既不补发，
    也不参与"哪些已发过"的比对。这同时拆掉了 MAX_CODEX_TURNS 那道墙：一条恒定
    增长的线程总会越过任何固定轮次上限，那只是把同一次停摆推迟几十天。
    """

    @staticmethod
    def _turn(index, with_id=True):
        turn = {
            "items": [
                {
                    "type": "userMessage",
                    "id": "u%d" % index,
                    "content": [{"type": "text", "text": "问题 %d" % index}],
                },
                {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "回答 %d" % index,
                },
            ],
        }
        if with_id:
            turn["id"] = "t%d" % index
        return turn

    def _project(self, count, with_id=True):
        return SYNC._project_codex_thread(
            {
                "thread": {
                    "id": THREAD,
                    "turns": [self._turn(i, with_id) for i in range(count)],
                }
            },
            THREAD,
        )

    def test_only_the_last_window_is_projected(self):
        self.assertEqual(SYNC.MAX_CODEX_PROJECTED_TURNS, 100)
        projected = self._project(150)
        self.assertEqual(len(projected["segments"]), 100)
        self.assertEqual(len(projected["seenRequestIds"]), 100)
        self.assertEqual(projected["segments"][0]["user"], "问题 50")
        self.assertEqual(projected["segments"][-1]["user"], "问题 149")

    def test_turn_without_id_keeps_its_identity_as_the_thread_grows(self):
        # 切片后从 0 重新数，会让没有 id 的轮次靠 `turn-<index>` 兜底的身份漂移，
        # 同一轮于是被当成新的重复发一次。绝对下标是这条契约的全部内容。
        first = self._project(150, with_id=False)
        grown = self._project(160, with_id=False)
        shared = set(first["seenRequestIds"]) & set(grown["seenRequestIds"])
        self.assertEqual(len(shared), 90, "重叠的 90 轮必须逐条同身份")

    def test_a_very_long_thread_is_no_longer_rejected(self):
        projected = self._project(SYNC.MAX_CODEX_PROJECTED_TURNS * 3)
        self.assertEqual(
            len(projected["segments"]), SYNC.MAX_CODEX_PROJECTED_TURNS
        )
        self.assertGreater(SYNC.MAX_CODEX_TURNS, 5_000)


class StructuredReadDegradationTest(unittest.TestCase):
    """结构化源读不到 ≠ 聊天记录必须停（2026-09-04 就是这么停了好几天）。

    读不到时退回连续性文件（有界，10 条滚动窗口）继续发，只损失"窗口滚掉的轮次
    还能补回来"这一项，并如实标 degraded。再配失败退避，免得每 0.75s 重读一遍
    整条线程（app-server 每次都要整读磁盘上 157 MiB 的 rollout）。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.global_state = self.root / "global.json"
        self.continuity = self.root / "continuity.json"
        self.state = self.root / "state.json"
        self.archive = self.root / "archive.json"
        self.write(
            self.global_state,
            {
                "electron-persisted-atom-state": {
                    SYNC.RECENT_THREAD_KEY: {
                        "conversationId": THREAD,
                        "hostId": "local",
                    },
                },
            },
        )

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def recent(self, items):
        self.write(
            self.continuity,
            {"version": 1, "threads": {THREAD: {"items": items}}},
        )

    class FailingHistoryClient:
        def __init__(self):
            self.calls = 0

        def read_thread(self, thread_id):
            self.calls += 1
            raise SYNC.CodexAppServerError(
                "Codex app-server response too large: 36991863 bytes"
                " > 33554432 cap"
            )

        def close(self):
            pass

    def _snapshot(self):
        path = self.root / "reader-context-snapshot.json"
        self.write(
            path,
            {
                "schema": "reader-context-snapshot/1",
                "revision": 7,
                "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                "contextStatus": "ready",
                "activeReading": {
                    "fresh": True,
                    "sourceInstanceId": "app-reader-degraded",
                },
                "currentPage": {
                    "stable": True,
                    "sourceInstanceId": "app-reader-degraded",
                    "file": "Books/degraded.pdf",
                    "page": 3,
                },
            },
        )
        return path

    def test_history_keeps_flowing_when_the_thread_is_too_large_to_read(self):
        self.recent([msg("user", "u1"), msg("assistant", "a1")])
        calls = []
        client = self.FailingHistoryClient()
        syncer = SYNC.CaptureBoundHistorySynchronizer(
            root=self.root,
            publisher=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or {"ok": True}
            ),
            global_state_path=self.global_state,
            continuity_path=self.continuity,
            state_path=self.state,
            archive_path=self.archive,
            snapshot_path=self._snapshot(),
            structured_history_client=client,
        )
        armed = syncer.observe(
            service_online=True, capture_active=True, snapshot_mode=True
        )
        self.assertIsNotNone(armed)
        self.assertIn("structured history unavailable", armed.get("error", ""))
        self.assertEqual(client.calls, 1)

        # 新一轮对话落进连续性文件 —— 结构化源仍读不到，但话必须发出去。
        self.recent(
            [
                msg("user", "u1"),
                msg("assistant", "a1"),
                msg("user", "u2"),
                msg("assistant", "a2"),
            ]
        )
        degraded = syncer.observe(
            service_online=True, capture_active=True, snapshot_mode=True
        )
        self.assertEqual(degraded["published"], 1, "降级后仍要把新一轮发出去")
        self.assertIn("cooling down", degraded["degraded"])
        self.assertEqual(
            [call[0][0] for call in calls].count("assistant_turn"), 1
        )
        self.assertEqual(calls[-1][0][1]["text"], "a2")
        self.assertEqual(calls[-1][0][1]["user_utterance"], "u2")
        # 失败退避期间不再重读整条线程
        self.assertEqual(client.calls, 1)


class CodexAppServerResponseBoundsTest(unittest.TestCase):
    """2026-09-04 实锤:用了 21 天的语音线程 thread/read 回 35.3 MB,撞破当时的
    32 MB 上限 → 结构化回填每次都失败 → 聊天记录一直同步不到 App。
    而三种失败共用一句 "response invalid",日志里看不出到底是哪一种 ——
    这条契约钉的就是"报错必须说得出原始值"。"""

    class _StubProcess:
        def __init__(self):
            self.stdin = self

        def poll(self):
            return None

        def write(self, _text):
            return None

        def flush(self):
            return None

        def close(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    def _client(self, line):
        client = SYNC.CodexAppServerHistoryClient()
        client._process = self._StubProcess()
        client._stdout.put(line)
        return client

    def test_oversized_response_reports_the_actual_size(self):
        line = json.dumps({"id": 1, "result": {"pad": "y" * 200}}) + "\n"
        with patch.object(SYNC, "MAX_CODEX_RESPONSE_BYTES", 64):
            client = self._client(line)
            with self.assertRaises(SYNC.CodexAppServerError) as caught:
                client._request("thread/read", {})
        message = str(caught.exception)
        self.assertIn("too large", message)
        self.assertIn(str(len(line.encode("utf-8"))), message)
        self.assertIn("64", message)

    def test_blank_and_undecodable_are_not_the_same_error(self):
        client = self._client("\n")
        with self.assertRaises(SYNC.CodexAppServerError) as blank:
            client._request("thread/read", {})
        self.assertIn("blank", str(blank.exception))
        client = self._client("{not json\n")
        with self.assertRaises(SYNC.CodexAppServerError) as broken:
            client._request("thread/read", {})
        self.assertIn("undecodable", str(broken.exception))

    def test_success_records_response_size_for_the_read_cooldown(self):
        line = json.dumps({"id": 1, "result": {"thread": {}}}) + "\n"
        client = self._client(line)
        self.assertEqual(client._request("thread/read", {}), {"thread": {}})
        self.assertEqual(client.last_response_bytes, len(line.encode("utf-8")))

    def test_read_cooldown_scales_with_thread_size(self):
        # 小线程照旧(历史近实时到达);大线程不再按 0.75s 的节奏反复整读 ——
        # app-server 那一侧每次都要重读磁盘上 157 MiB 的 rollout。
        cooldown = SYNC.CaptureBoundHistorySynchronizer._structured_read_cooldown_seconds

        class _Holder:
            def __init__(self, size):
                self.structured_history_client = types.SimpleNamespace(
                    last_response_bytes=size
                )

        self.assertEqual(cooldown(_Holder(1024)), 0.0)
        self.assertEqual(
            cooldown(_Holder(SYNC.STRUCTURED_READ_LARGE_BYTES)),
            SYNC.STRUCTURED_READ_LARGE_COOLDOWN_SECONDS,
        )
        self.assertEqual(
            cooldown(_Holder(SYNC.STRUCTURED_READ_HUGE_BYTES)),
            SYNC.STRUCTURED_READ_HUGE_COOLDOWN_SECONDS,
        )
        # 实测的那条线程(35.3 MB)必须落在最长冷却档,且不再超过传输上限
        self.assertEqual(
            cooldown(_Holder(36_991_863)),
            SYNC.STRUCTURED_READ_HUGE_COOLDOWN_SECONDS,
        )
        self.assertGreater(SYNC.MAX_CODEX_RESPONSE_BYTES, 36_991_863)


if __name__ == "__main__":
    unittest.main()
