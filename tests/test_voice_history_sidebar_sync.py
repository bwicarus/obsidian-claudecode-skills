"""Isolated contracts for the packaged voice-history sidebar synchronizer.

All inputs and durable files are temporary. Publishers are fakes, so this test
module cannot contact a Reader service or any external machine.
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

    def test_capture_lease_never_sends_pending_turn_to_another_reader_source(self):
        self.recent([msg("user", "old-u"), msg("assistant", "old-a")])
        snapshot = self.root / "reader-context-snapshot.json"

        def write_snapshot(source, revision, page):
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
                        "file": "Books/book.pdf",
                        "page": page,
                        "stable": True,
                        "sourceInstanceId": source,
                    },
                },
            )

        write_snapshot("app-reader-a", 30, 4)
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

        write_snapshot("extension-reader-b", 31, 0)
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
                                        {"type": "text", "text": "查一下天气"}
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


if __name__ == "__main__":
    unittest.main()
