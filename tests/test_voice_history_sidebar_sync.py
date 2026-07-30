"""Isolated contracts for the packaged voice-history sidebar synchronizer.

All inputs and durable files are temporary. Publishers are fakes, so this test
module cannot contact SSH, Pi, or a Reader service.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
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
                "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                "activeReading": {"fresh": True, "ageSec": 0},
                "contextStatus": "ready",
                "currentPage": {
                    "file": "Books/book.pdf",
                    "page": 7,
                    "stable": True,
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


if __name__ == "__main__":
    unittest.main()
