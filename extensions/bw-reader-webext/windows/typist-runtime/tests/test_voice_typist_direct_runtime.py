from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import typist_ipc as IPC  # noqa: E402
import voice_typist as VT  # noqa: E402


def page_event(*, seq: int = 1, event_id: str = "0123456789abcdef") -> dict:
    return {
        "v": 1,
        "seq": seq,
        "type": "page.context",
        "ts": 1,
        "id": event_id,
        "stable": True,
        "book_id": "book.pdf",
        "file": "book.pdf",
        "page": 3,
        "title": "Book",
        "page_context": {
            "text": (
                f"{IPC.MARK_L}HIGHLIGHT{IPC.MARK_R}"
                "正文含 \\⟦ 和反斜杠 \\\\"
                f"{IPC.MARK_L}/HIGHLIGHT{IPC.MARK_R}"
            ),
            "reason": "dwell",
            "truncated": False,
            "visual": {"page_image": "/page/3", "has_ink": True},
        },
    }


def request(event: dict, session: str = "0011223344556677") -> dict:
    return {
        "contract": IPC.CONTRACT,
        "requestId": f"request-{event['seq']}",
        "sessionId": session,
        "action": "context",
        "event": event,
    }


class FakeTypist:
    def __init__(self, result: VT.SubmitResult | None = None) -> None:
        self.result = result or VT.SubmitResult(True, "delivered")
        self.calls: list[tuple[str, str, str, str]] = []

    def submit(self, text, event_id, event_type, source):
        self.calls.append((text, event_id, event_type, source))
        return self.result


class DirectFormatterTest(unittest.TestCase):
    def test_page_text_unescapes_body_but_preserves_structure(self) -> None:
        rendered = VT.format_context_event(page_event())
        self.assertIn(f"{IPC.MARK_L}HIGHLIGHT{IPC.MARK_R}", rendered)
        self.assertIn(f"正文含 {IPC.MARK_L} 和反斜杠 \\", rendered)
        self.assertTrue(rendered.startswith(VT.MARKER + "\nPAGE |"))
        self.assertTrue(rendered.endswith("\n" + VT.MARKER_END))

    def test_focus_cancel_and_drawing_states_are_explicit(self) -> None:
        cancel = {
            "v": 1,
            "seq": 2,
            "type": "focus",
            "ts": 2,
            "id": "1111111111111111",
            "action": "cancel",
            "cancelledObject": {
                "kind": "text",
                "ref": {"id": "selection-1"},
            },
        }
        pending = {
            "v": 1,
            "seq": 3,
            "type": "drawing",
            "ts": 3,
            "id": "2222222222222222",
            "state": "pending",
            "file": "book.pdf",
            "page": 3,
            "drawingRevision": None,
            "ref": None,
        }
        self.assertIn("CLEAR | sid=selection-1", VT.format_context_event(cancel))
        self.assertIn("DRAWING_PENDING", VT.format_context_event(pending))

    def test_independent_command_success_is_completely_silent(self) -> None:
        event = {
            "v": 1,
            "seq": 4,
            "type": "command",
            "ts": 4,
            "id": "3333333333333333",
            "ok": True,
            "emitsEvent": False,
        }
        self.assertEqual(VT.format_context_event(event), "")


class TargetAppPolicyTest(unittest.TestCase):
    def test_composer_readback_accepts_only_newline_encoding_differences(self) -> None:
        payload = "[[READER_SYNC]]\n正文\n[[/READER_SYNC]]"
        windows_readback = payload.replace("\n", "\r\n")
        self.assertTrue(VT.composer_text_matches(payload, windows_readback))
        self.assertFalse(VT.composer_text_matches(payload, windows_readback + "\r\n"))
        self.assertTrue(VT.composer_text_matches(
            payload,
            windows_readback + "\r\n",
            allow_trailing_newline=True,
        ))
        self.assertFalse(VT.composer_text_matches(payload, windows_readback + "x"))
        self.assertFalse(VT.composer_text_matches(payload, None))

    def test_packaged_apps_are_distinguished_by_full_image_path(self) -> None:
        codex = (
            r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0.0.0_x64__2p2nqsd0c76g0"
            r"\app\ChatGPT.exe"
        )
        classic = (
            r"C:\Program Files\WindowsApps\OpenAI.ChatGPT-Desktop_1.0.0.0_x64__2p2nqsd0c76g0"
            r"\app\ChatGPT Classic.exe"
        )
        stale_classic_name = (
            r"C:\Program Files\WindowsApps\OpenAI.ChatGPT-Desktop_1.0.0.0_x64__2p2nqsd0c76g0"
            r"\app\ChatGPT.exe"
        )
        self.assertTrue(VT.target_process_path_matches(codex, "codex-desktop"))
        self.assertFalse(VT.target_process_path_matches(codex, "chatgpt-classic"))
        self.assertTrue(VT.target_process_path_matches(classic, "chatgpt-classic"))
        self.assertFalse(VT.target_process_path_matches(classic, "codex-desktop"))
        self.assertFalse(
            VT.target_process_path_matches(stale_classic_name, "chatgpt-classic")
        )

    def test_classic_uses_exact_uia_controls_without_codex_session_checks(self) -> None:
        cfg = VT.apply_target_app(
            copy.deepcopy(VT.DEFAULT_CONFIG_BODY),
            "chatgpt-classic",
        )
        self.assertEqual(cfg["target"]["app_kind"], "chatgpt-classic")
        self.assertEqual(cfg["target"]["process_name"], "ChatGPT Classic")
        self.assertEqual(cfg["target"]["session_mode"], "follow_active")
        self.assertIsNone(cfg["target"]["session_id"])
        self.assertIsNone(cfg["hotkeys"]["copy_session_id"])
        self.assertEqual(cfg["verification"]["method"], "none")
        self.assertFalse(cfg["safety"]["require_transcript_verification"])
        self.assertEqual(VT.ClassicComposerAutomation.COMPOSER_ID, "prompt-textarea")
        self.assertEqual(
            VT.ClassicComposerAutomation.SEND_ID,
            "composer-submit-button",
        )

        codex_cfg = VT.apply_target_app(cfg, "codex-desktop")
        self.assertEqual(codex_cfg["target"]["process_name"], "ChatGPT")


class DurableRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(VT.DEFAULT_CONFIG_BODY)
        # These cases are about queue ordering and coalescing; the once-per
        # session preamble would prepend an extra delivery to every one of
        # them. It has its own tests below.
        self.cfg["session_preamble"]["enabled"] = False
        self.log = VT.AuditLog(self.root / "typist.jsonl")
        self.state = VT.RunState(self.root / "state")
        self.typist = FakeTypist()
        self.queue_path = (
            self.root
            / "state"
            / "typist-ipc-queue.json"
        )
        self.queue = VT.SubmitQueue(
            self.typist,  # type: ignore[arg-type]
            self.log,
            self.cfg,
            state_path=self.queue_path,
        )
        self.ledger = IPC.EventLedger(
            self.root / "state" / "typist-ipc-ledger.json",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def handoff(self, event, session_id, sequence):
        return VT.enqueue_ipc_event(
            event,
            session_id,
            sequence,
            queue=self.queue,
            log=self.log,
            state=self.state,
            cfg=self.cfg,
        )

    @staticmethod
    def focus_event(*, seq: int, event_id: str, text: str) -> dict:
        return {
            "v": 1,
            "seq": seq,
            "type": "focus",
            "ts": seq,
            "id": event_id,
            "action": "replace",
            "kind": "text",
            "ref": {"text": text},
        }

    def commit(self, event, session_id, sequence, outcome):
        return VT.commit_ipc_event(
            event,
            session_id,
            sequence,
            outcome,
            queue=self.queue,
            log=self.log,
        )

    def handle(self, event: dict) -> dict:
        return IPC.handle_request(
            request(event),
            self.ledger,
            validate_text=IPC.unescape_annotated_text,
            on_event=self.handoff,
            after_record=self.commit,
        )

    def durable_status(
        self,
        *,
        state_dir: Path | None = None,
    ) -> tuple[int, dict, str]:
        config_path = self.root / "voice-typist.config.json"
        VT.save_config(config_path, self.cfg)
        output = io.StringIO()
        args = SimpleNamespace(
            config=config_path,
            state_dir=state_dir or (self.root / "state"),
        )
        with contextlib.redirect_stdout(output):
            code = VT.cmd_queue_status(args)
        raw = output.getvalue().strip()
        return code, json.loads(raw), raw

    def test_ack_follows_durable_queue_and_survives_restart(self) -> None:
        response = self.handle(page_event())
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["payload"]["outcome"], "accepted")
        self.assertTrue(self.queue_path.is_file())
        payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["receipts"],
            [{
                "sessionId": "0011223344556677",
                "eventId": page_event()["id"],
                "seq": 1,
            }],
        )
        reopened = VT.SubmitQueue(
            self.typist,  # type: ignore[arg-type]
            self.log,
            self.cfg,
            state_path=self.queue_path,
        )
        self.assertEqual(len(reopened.items), 1)
        self.assertEqual(reopened.items[0].event_id, page_event()["id"])
        self.assertTrue(reopened.items[0].committed)

    def test_focus_updates_coalesce_to_latest_settled_value(self) -> None:
        first = self.focus_event(
            seq=1,
            event_id="1111111111111111",
            text="为",
        )
        latest = self.focus_event(
            seq=2,
            event_id="2222222222222222",
            text="为了",
        )
        self.handle(first)
        self.handle(latest)

        self.assertEqual(len(self.queue.items), 1)
        self.assertEqual(self.queue.items[0].event_id, latest["id"])
        self.assertIn("为了", self.queue.items[0].text)
        self.assertEqual(
            self.queue.items[0].coalesce_key,
            "0011223344556677:focus",
        )
        self.assertGreater(self.queue.items[0].queued_at, 0)
        self.assertIsNone(self.queue.pump())
        self.queue.items[0].queued_at -= self.queue.settle + 0.1
        delivered = self.queue.pump()
        self.assertIsNotNone(delivered)
        self.assertEqual(self.typist.calls[0][1], latest["id"])

    def test_focus_update_never_replaces_delivery_in_progress(self) -> None:
        first = self.focus_event(
            seq=1,
            event_id="1111111111111111",
            text="为",
        )
        latest = self.focus_event(
            seq=2,
            event_id="2222222222222222",
            text="为了",
        )
        self.handle(first)
        self.queue.items[0].delivery_started = True
        self.handle(latest)

        self.assertEqual(len(self.queue.items), 2)
        self.assertEqual(self.queue.items[0].event_id, first["id"])
        self.assertTrue(self.queue.items[0].delivery_started)
        self.assertEqual(self.queue.items[1].event_id, latest["id"])

        # A transient HOLD makes the first item replaceable again. A newer
        # focus must collapse both stale waiting states, never leave [new, old].
        self.queue.items[0].delivery_started = False
        newest = self.focus_event(
            seq=3,
            event_id="3333333333333333",
            text="为了更快",
        )
        self.handle(newest)
        self.assertEqual(len(self.queue.items), 1)
        self.assertEqual(self.queue.items[0].event_id, newest["id"])

    def test_queue_persist_failure_returns_retryable_without_advancing_ledger(self) -> None:
        with patch.object(
            self.queue,
            "_persist",
            side_effect=RuntimeError("disk unavailable"),
        ):
            response = self.handle(page_event())
        self.assertFalse(response["ok"])
        self.assertTrue(response["error"]["retryable"])
        self.assertEqual(
            response["error"]["code"],
            "BW_TYPIST_IPC_HANDOFF_FAILED",
        )
        self.assertEqual(self.ledger.cursor("0011223344556677"), 0)
        self.assertEqual(len(self.queue.items), 0)

    def test_full_queue_never_drops_an_acknowledged_old_event(self) -> None:
        self.queue.max_size = 1
        first = self.handle(page_event())
        second_event = page_event(
            seq=2,
            event_id="fedcba9876543210",
        )
        second = self.handle(second_event)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertTrue(second["error"]["retryable"])
        self.assertEqual(len(self.queue.items), 1)
        self.assertEqual(self.queue.items[0].event_id, page_event()["id"])
        self.assertEqual(self.ledger.cursor("0011223344556677"), 1)

    def test_successful_delivery_durably_removes_queue_head(self) -> None:
        self.handle(page_event())
        result = self.queue.pump()
        self.assertIsNotNone(result)
        self.assertTrue(result.ok)
        self.assertEqual(len(self.queue.items), 0)
        payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["items"], [])

    def test_ledger_failure_keeps_item_staged_and_unpumpable_until_retry(self) -> None:
        original_record = self.ledger.record
        with patch.object(
            self.ledger,
            "record",
            side_effect=IPC.LedgerError("disk unavailable"),
        ):
            failed = self.handle(page_event())
        self.assertFalse(failed["ok"])
        self.assertEqual(len(self.queue.items), 1)
        self.assertFalse(self.queue.items[0].committed)
        self.assertIsNone(self.queue.pump())
        self.assertEqual(self.typist.calls, [])

        with patch.object(self.ledger, "record", side_effect=original_record):
            retried = self.handle(page_event())
        self.assertTrue(retried["ok"], retried)
        self.assertTrue(self.queue.items[0].committed)
        delivered = self.queue.pump()
        self.assertIsNotNone(delivered)
        self.assertEqual(len(self.typist.calls), 1)

    def test_commit_failure_retries_as_duplicate_without_restaging(self) -> None:
        original_commit = self.queue.commit
        with patch.object(
            self.queue,
            "commit",
            side_effect=RuntimeError("queue commit unavailable"),
        ):
            failed = self.handle(page_event())
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["error"]["retryable"])
        self.assertEqual(self.ledger.cursor("0011223344556677"), 1)
        self.assertEqual(len(self.queue.items), 1)
        self.assertFalse(self.queue.items[0].committed)
        with patch.object(self.queue, "commit", side_effect=original_commit):
            retried = self.handle(page_event())
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(retried["payload"]["outcome"], "duplicate")
        self.assertEqual(len(self.queue.items), 1)
        self.assertTrue(self.queue.items[0].committed)

    def test_accepted_text_missing_staged_item_is_retryable_failure(self) -> None:
        first = IPC.handle_request(
            request(page_event()),
            self.ledger,
            validate_text=IPC.unescape_annotated_text,
            on_event=lambda *_args: True,
            after_record=self.commit,
        )
        self.assertFalse(first["ok"])
        self.assertTrue(first["error"]["retryable"])
        self.assertEqual(
            first["error"]["code"],
            "BW_TYPIST_IPC_HANDOFF_FAILED",
        )
        self.assertEqual(len(self.queue.items), 0)
        # The ledger is now duplicate, but it must not turn that into a false
        # success: this queue never issued a staging receipt for the identity.
        second = self.handle(page_event())
        self.assertFalse(second["ok"])
        self.assertTrue(second["error"]["retryable"])
        self.assertEqual(
            second["error"]["code"],
            "BW_TYPIST_IPC_HANDOFF_FAILED",
        )
        self.assertEqual(len(self.queue.items), 0)

    def test_duplicate_missing_item_is_idempotent_after_delivery(self) -> None:
        first = self.handle(page_event())
        self.assertTrue(first["ok"])
        self.assertIsNotNone(self.queue.pump())
        self.assertEqual(len(self.queue.items), 0)
        duplicate = self.handle(page_event())
        self.assertTrue(duplicate["ok"], duplicate)
        self.assertEqual(duplicate["payload"]["outcome"], "duplicate")
        self.assertEqual(len(self.queue.items), 0)

    def test_full_ack_order_is_stage_then_ledger_then_commit(self) -> None:
        order: list[str] = []
        original_enqueue = self.queue.enqueue
        original_record = self.ledger.record
        original_commit = self.queue.commit

        def enqueue(item):
            result = original_enqueue(item)
            order.append("stage")
            return result

        def record(*args):
            result = original_record(*args)
            order.append("ledger")
            return result

        def commit(*args):
            result = original_commit(*args)
            order.append("commit")
            return result

        with (
            patch.object(self.queue, "enqueue", side_effect=enqueue),
            patch.object(self.ledger, "record", side_effect=record),
            patch.object(self.queue, "commit", side_effect=commit),
        ):
            response = self.handle(page_event())
        self.assertTrue(response["ok"], response)
        self.assertEqual(order, ["stage", "ledger", "commit"])

    def test_loaded_old_session_waits_and_is_discarded_by_new_session(self) -> None:
        self.handle(page_event())
        reopened = VT.SubmitQueue(
            self.typist,  # type: ignore[arg-type]
            self.log,
            self.cfg,
            state_path=self.queue_path,
        )
        self.assertIsNone(reopened.pump())
        self.assertEqual(self.typist.calls, [])
        reopened.activate_session("8899aabbccddeeff")
        self.assertEqual(len(reopened.items), 0)
        payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["items"], [])
        self.assertEqual(len(payload["receipts"]), 1)

    def test_interrupted_ui_delivery_is_never_retried_blindly(self) -> None:
        self.handle(page_event())

        def interrupted(*_args):
            self.typist.calls.append(("started", "", "", ""))
            raise RuntimeError("process interrupted during UI submit")

        with patch.object(self.typist, "submit", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.queue.pump()
        self.assertTrue(self.queue.items[0].delivery_started)

        reopened_typist = FakeTypist()
        reopened = VT.SubmitQueue(
            reopened_typist,  # type: ignore[arg-type]
            self.log,
            self.cfg,
            state_path=self.queue_path,
        )
        reopened.activate_session("0011223344556677")
        self.assertIsNone(reopened.pump())
        self.assertEqual(reopened_typist.calls, [])
        status = reopened.status_snapshot()
        self.assertEqual(
            status["queue_blocked_reason"],
            "delivery_uncertain",
        )
        self.assertEqual(status["blocked_event_id"], page_event()["id"])

        reopened.resolve_uncertain(
            "0011223344556677",
            page_event()["id"],
            1,
            delivered=False,
        )
        self.assertIsNone(
            reopened.status_snapshot()["queue_blocked_reason"]
        )
        delivered = reopened.pump()
        self.assertIsNotNone(delivered)
        self.assertTrue(delivered.ok)

    def test_queue_status_uses_durable_uncertain_identity_not_stale_status(
        self,
    ) -> None:
        self.handle(page_event())
        with patch.object(
            self.typist,
            "submit",
            side_effect=RuntimeError("interrupted after delivery_started"),
        ):
            with self.assertRaises(RuntimeError):
                self.queue.pump()
        self.state.write_status({
            "running": False,
            "queue_depth": 0,
            "queue_blocked_reason": None,
            "blocked_session_id": None,
            "blocked_event_id": None,
            "blocked_sequence": None,
        })

        code, envelope, raw = self.durable_status()

        self.assertEqual(code, 0)
        self.assertEqual(envelope["contract"], VT.QUEUE_STATUS_CONTRACT)
        self.assertEqual(envelope["queueContract"], VT.QUEUE_CONTRACT)
        self.assertEqual(
            envelope["payload"],
            {
                "queue_depth": 1,
                "queue_blocked_reason": "delivery_uncertain",
                "blocked_session_id": "0011223344556677",
                "blocked_event_id": page_event()["id"],
                "blocked_sequence": 1,
            },
        )
        self.assertNotIn("正文", raw)
        self.assertNotIn("receipts", raw)

    def test_queue_status_fresh_state_is_empty_and_read_only(self) -> None:
        fresh = self.root / "never-created"

        code, envelope, _raw = self.durable_status(state_dir=fresh)

        self.assertEqual(code, 0)
        self.assertEqual(envelope["payload"]["queue_depth"], 0)
        self.assertIsNone(
            envelope["payload"]["queue_blocked_reason"],
        )
        self.assertFalse(fresh.exists())

    def test_queue_status_fresh_install_needs_no_config_or_writes(
        self,
    ) -> None:
        config_path = self.root / "not-created.config.json"
        state_dir = self.root / "not-created-state"
        output = io.StringIO()
        args = SimpleNamespace(
            config=config_path,
            state_dir=state_dir,
        )

        with contextlib.redirect_stdout(output):
            code = VT.cmd_queue_status(args)

        envelope = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(envelope["payload"]["queue_depth"], 0)
        self.assertIsNone(envelope["payload"]["queue_blocked_reason"])
        self.assertFalse(config_path.exists())
        self.assertFalse(state_dir.exists())

    def test_queue_status_missing_config_rejects_durable_state(
        self,
    ) -> None:
        state_dir = self.root / "durable-without-config"
        state_dir.mkdir()
        (state_dir / "typist-ipc-queue.json").write_text(
            json.dumps({
                "contract": VT.QUEUE_CONTRACT,
                "items": [],
                "receipts": [],
            }),
            encoding="utf-8",
        )
        args = SimpleNamespace(
            config=self.root / "missing.config.json",
            state_dir=state_dir,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "config is missing while durable state exists",
        ):
            VT.cmd_queue_status(args)

    def test_silent_event_persists_empty_queue_witness_before_ledger(self) -> None:
        silent = {
            "v": 1,
            "seq": 1,
            "type": "command",
            "ts": 1,
            "id": "silent-command-1",
            "ok": True,
            "emitsEvent": False,
        }

        response = self.handle(silent)

        self.assertTrue(response["ok"], response)
        payload = json.loads(
            self.queue_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["receipts"], [])

    def test_queue_status_rejects_missing_queue_witness_for_ledger(
        self,
    ) -> None:
        state_dir = self.root / "missing-witness"
        state_dir.mkdir()
        (state_dir / "typist-ipc-ledger.json").write_text(
            "{}",
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError):
            self.durable_status(state_dir=state_dir)

    def test_queue_status_rejects_corrupt_queue(self) -> None:
        state_dir = self.root / "corrupt-status"
        state_dir.mkdir()
        (state_dir / "typist-ipc-queue.json").write_text(
            "{bad",
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError):
            self.durable_status(state_dir=state_dir)

    def test_non_head_delivery_started_fails_closed(self) -> None:
        self.handle(page_event())
        self.handle(page_event(
            seq=2,
            event_id="fedcba9876543210",
        ))
        payload = json.loads(
            self.queue_path.read_text(encoding="utf-8"),
        )
        payload["items"][0]["deliveryStarted"] = False
        payload["items"][1]["deliveryStarted"] = True
        self.queue_path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        with self.assertRaises(RuntimeError):
            VT.SubmitQueue.inspect_status(
                self.queue_path,
                max_size=self.queue.max_size,
            )

    def test_new_session_waits_until_old_ui_submit_finishes(self) -> None:
        self.handle(page_event())
        submit_entered = threading.Event()
        release_submit = threading.Event()

        def blocked_submit(*args):
            submit_entered.set()
            self.assertTrue(release_submit.wait(timeout=2.0))
            return VT.SubmitResult(True, "delivered")

        pump = threading.Thread(
            target=self.queue.pump,
            daemon=True,
        )
        switch_done = threading.Event()

        def switch_session():
            self.queue.activate_session("8899aabbccddeeff")
            switch_done.set()

        with patch.object(
            self.typist,
            "submit",
            side_effect=blocked_submit,
        ):
            pump.start()
            self.assertTrue(submit_entered.wait(timeout=1.0))
            switch = threading.Thread(
                target=switch_session,
                daemon=True,
            )
            switch.start()
            time.sleep(0.05)
            self.assertFalse(switch_done.is_set())
            release_submit.set()
            pump.join(timeout=2.0)
            switch.join(timeout=2.0)
        self.assertFalse(pump.is_alive())
        self.assertTrue(switch_done.is_set())
        self.assertEqual(len(self.queue.items), 0)

    def test_conflicting_event_id_binding_in_queue_fails_closed(self) -> None:
        first = VT.PendingItem(
            text="a",
            event_id="same",
            event_type="focus",
            source="test",
            session_id="session",
            sequence=1,
        )
        second = VT.PendingItem(
            text="b",
            event_id="same",
            event_type="focus",
            source="test",
            session_id="session",
            sequence=2,
        )
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_path.write_text(
            json.dumps({
                "contract": VT.QUEUE_CONTRACT,
                "items": [
                    VT.SubmitQueue._record(first),
                    VT.SubmitQueue._record(second),
                ],
                "receipts": [
                    VT.SubmitQueue._receipt_record(
                        ("session", "same", 1),
                    ),
                    VT.SubmitQueue._receipt_record(
                        ("session", "same", 2),
                    ),
                ],
            }),
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError):
            VT.SubmitQueue(
                self.typist,  # type: ignore[arg-type]
                self.log,
                self.cfg,
                state_path=self.queue_path,
            )

    def test_queue_item_without_staging_receipt_fails_closed(self) -> None:
        item = VT.PendingItem(
            text="a",
            event_id="event",
            event_type="focus",
            source="test",
            session_id="session",
            sequence=1,
        )
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_path.write_text(
            json.dumps({
                "contract": VT.QUEUE_CONTRACT,
                "items": [VT.SubmitQueue._record(item)],
                "receipts": [],
            }),
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeError):
            VT.SubmitQueue(
                self.typist,  # type: ignore[arg-type]
                self.log,
                self.cfg,
                state_path=self.queue_path,
            )

    def test_corrupt_durable_queue_fails_closed(self) -> None:
        self.queue_path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            VT.SubmitQueue(
                self.typist,  # type: ignore[arg-type]
                self.log,
                self.cfg,
                state_path=self.queue_path,
            )

    def test_process_generation_probe_rejects_pid_reuse(self) -> None:
        started = VT.process_start_file_time_utc(os.getpid())
        self.assertIsInstance(started, int)
        self.assertGreater(started, 0)
        self.assertTrue(
            VT.process_generation_alive(os.getpid(), started)
        )
        self.assertFalse(
            VT.process_generation_alive(os.getpid(), started + 1)
        )

    def test_process_generation_probe_rejects_exited_process_with_open_handle(
        self,
    ) -> None:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.read(1)",
            ],
            stdin=subprocess.PIPE,
        )
        try:
            started = VT.process_start_file_time_utc(child.pid)
            self.assertIsInstance(started, int)
            self.assertGreater(started, 0)
            self.assertTrue(VT.process_generation_alive(child.pid, started))

            self.assertIsNotNone(child.stdin)
            child.stdin.close()
            child.wait(timeout=5)

            # Keep the Popen object (and therefore its Windows process handle)
            # alive: creation time alone still identifies the exited object.
            self.assertFalse(VT.process_generation_alive(child.pid, started))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_bridge_owner_exit_stops_managed_typist_without_idle_timeout(
        self,
    ) -> None:
        config_path = self.root / "voice-typist.config.json"
        VT.save_config(config_path, self.cfg)
        args = SimpleNamespace(
            config=config_path,
            log=self.root / "owner-watchdog.jsonl",
            state_dir=self.root / "owner-state",
            clear_stop=False,
            dry_run=True,
            ipc_connect_timeout=0.05,
            poll=0.2,
            idle_exit_seconds=0.0,
            owner_process_id=2147483647,
            owner_process_start_file_time_utc=1,
        )
        with patch.object(
            IPC,
            "connect_pipe",
            side_effect=OSError("no pipe"),
        ):
            self.assertEqual(VT.cmd_run(args), 0)
        status = json.loads(
            (args.state_dir / "status.json").read_text(encoding="utf-8")
        )
        self.assertFalse(status["running"])
        self.assertEqual(status["reason"], "owner_generation_exited")


class ClassicSelectionCoalescingTests(DurableRuntimeTest):
    """Selecting once must produce one message, not page -> selection -> page.

    Every Classic delivery is a visible message in the user's own conversation
    and there is no rollout verification to fall back on, so redundant sends are
    the expensive failure mode. These run against the real durable queue rather
    than a stub so the coalescing itself is exercised.
    """

    SESSION = "0011223344556677"

    def _classic(self) -> None:
        self.cfg["target"]["app_kind"] = "chatgpt-classic"

    def _pending(self):
        return [
            (item.event_type, item.coalesce_key)
            for item in self.queue.items
        ]

    def test_classic_selection_supersedes_the_page_queued_before_it(self):
        self._classic()
        self.handoff(page_event(seq=1, event_id="a" * 16), self.SESSION, 1)
        self.commit(
            page_event(seq=1, event_id="a" * 16), self.SESSION, 1, "accepted")
        self.handoff(
            self.focus_event(seq=2, event_id="b" * 16, text="one sentence"),
            self.SESSION,
            2,
        )
        self.commit(
            self.focus_event(seq=2, event_id="b" * 16, text="one sentence"),
            self.SESSION,
            2,
            "accepted",
        )
        # The page report is replaced, not queued alongside the selection.
        self.assertEqual(self._pending(), [("focus", f"{self.SESSION}:reading")])

    def test_classic_page_after_a_selection_is_suppressed(self):
        self._classic()
        focus = self.focus_event(seq=1, event_id="c" * 16, text="one sentence")
        self.handoff(focus, self.SESSION, 1)
        self.commit(focus, self.SESSION, 1, "accepted")
        self.handoff(page_event(seq=2, event_id="d" * 16), self.SESSION, 2)
        self.assertEqual(self._pending(), [("focus", f"{self.SESSION}:reading")])
        self.assertIn(
            "page_context_suppressed_for_focus",
            self.log.path.read_text(encoding="utf-8"),
        )

    def test_classic_page_is_kept_when_nothing_is_selected(self):
        self._classic()
        self.handoff(page_event(seq=1, event_id="e" * 16), self.SESSION, 1)
        self.assertEqual(
            self._pending(),
            [("page.context", f"{self.SESSION}:reading")],
        )

    def test_codex_ordering_is_untouched(self):
        # Same sequence under Codex must still yield two separate messages and
        # the original focus key.
        self.cfg["target"]["app_kind"] = "codex-desktop"
        self.handoff(page_event(seq=1, event_id="f" * 16), self.SESSION, 1)
        self.commit(
            page_event(seq=1, event_id="f" * 16), self.SESSION, 1, "accepted")
        focus = self.focus_event(seq=2, event_id="0" * 16, text="one sentence")
        self.handoff(focus, self.SESSION, 2)
        self.commit(focus, self.SESSION, 2, "accepted")
        self.assertEqual(
            self._pending(),
            [
                ("page.context", None),
                ("focus", f"{self.SESSION}:focus"),
            ],
        )


class SessionPreambleTests(unittest.TestCase):
    """The behaviour rules must reach the app before any reading context.

    Without them the target answers every synced page, which during a voice
    call means talking over the reader. Deliberately not derived from
    DurableRuntimeTest: inheriting would re-run its whole suite with the
    preamble enabled, and those cases count deliveries.
    """

    SESSION = "0011223344556677"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(VT.DEFAULT_CONFIG_BODY)
        self.log = VT.AuditLog(self.root / "typist.jsonl")
        self.typist = FakeTypist()
        self._build_queue()

    def _build_queue(self) -> None:
        self.queue = VT.SubmitQueue(
            self.typist,  # type: ignore[arg-type]
            self.log,
            self.cfg,
            state_path=self.root / "state" / "typist-ipc-queue.json",
        )

    def _deliver_one_page(self, session: str, seq: int, event_id: str) -> None:
        self.queue.activate_session(session)
        self.queue.enqueue(VT.PendingItem(
            text=VT.MARKER + "\nPAGE | body\n" + VT.MARKER_END,
            event_id=event_id,
            event_type="page.context",
            source="test",
            session_id=session,
            sequence=seq,
            committed=True,
        ))
        self.queue.pump()

    def test_preamble_precedes_the_first_payload(self):
        self._deliver_one_page(self.SESSION, 1, "a" * 16)
        self.assertGreaterEqual(len(self.typist.calls), 2)
        first_text, _, first_type, _ = self.typist.calls[0]
        self.assertEqual(first_type, "session.preamble")
        self.assertTrue(first_text.startswith(VT.MARKER))
        self.assertIn("不要回复任何内容", first_text)
        # The real page follows it, not the other way round.
        self.assertEqual(self.typist.calls[1][2], "page.context")

    def test_preamble_is_sent_once_per_session(self):
        self._deliver_one_page(self.SESSION, 1, "a" * 16)
        self._deliver_one_page(self.SESSION, 2, "b" * 16)
        preambles = [
            call for call in self.typist.calls
            if call[2] == "session.preamble"
        ]
        self.assertEqual(len(preambles), 1)

    def test_a_new_session_gets_its_own_preamble(self):
        self._deliver_one_page(self.SESSION, 1, "a" * 16)
        self._deliver_one_page("8877665544332211", 1, "c" * 16)
        preambles = [
            call for call in self.typist.calls
            if call[2] == "session.preamble"
        ]
        self.assertEqual(len(preambles), 2)

    def test_disabling_it_sends_nothing_extra(self):
        self.cfg["session_preamble"]["enabled"] = False
        self._build_queue()
        self._deliver_one_page(self.SESSION, 1, "a" * 16)
        self.assertTrue(
            all(call[2] != "session.preamble" for call in self.typist.calls)
        )

    def test_preamble_fits_the_payload_limit(self):
        self.assertLessEqual(
            len(VT.SESSION_PREAMBLE_TEXT),
            VT.DEFAULT_CONFIG_BODY["limits"]["max_payload_chars"],
        )
        self.assertTrue(VT.SESSION_PREAMBLE_TEXT.endswith(VT.MARKER_END))


class ComposerReadbackDiagnosticsTests(unittest.TestCase):
    """The diagnostics must be exact and must never carry the payload text.

    A wrong diagnostic is worse than none: it sends the next fix in the wrong
    direction, which is precisely how the CRLF theory survived several rounds.
    """

    def test_text_shape_counts_the_characters_serialisers_substitute(self):
        value = "a\r\nb\nc d e f\tg\n\nh"
        shape = VT.text_shape(value)
        self.assertEqual(shape["crlf"], 1)
        self.assertEqual(shape["cr"], 1)
        self.assertEqual(shape["lf"], 4)
        self.assertEqual(shape["nbsp"], 1)
        self.assertEqual(shape["u2028"], 1)
        self.assertEqual(shape["u2029"], 1)
        self.assertEqual(shape["tab"], 1)
        self.assertEqual(shape["blank_run"], 1)
        self.assertEqual(shape["len"], len(value))

    def test_text_shape_tolerates_missing_value(self):
        self.assertEqual(VT.text_shape(None), {"len": None})

    def test_shape_never_leaks_the_payload(self):
        secret = "私的な本文 with SECRET-TOKEN-42"
        serialised = json.dumps(VT.text_shape(secret), ensure_ascii=False)
        self.assertNotIn("SECRET-TOKEN-42", serialised)
        self.assertNotIn("私的な本文", serialised)

    def test_first_difference_ignores_pure_newline_encoding(self):
        payload = "one\ntwo\nthree"
        readback = "one\r\ntwo\r\nthree"
        diff = VT.first_difference(payload, readback)
        self.assertTrue(diff["normalized_equal"])
        self.assertEqual(
            diff["normalized_expected_len"],
            diff["normalized_actual_len"],
        )

    def test_first_difference_reports_code_points_not_text(self):
        # A serialiser turning the space into U+00A0 is invisible in a log that
        # prints characters, so the position is reported numerically instead.
        payload = "alpha beta"
        readback = "alpha beta"
        diff = VT.first_difference(payload, readback)
        self.assertFalse(diff["normalized_equal"])
        self.assertEqual(diff["index"], 5)
        self.assertEqual(diff["expected_cp"][0], 0x20)
        self.assertEqual(diff["actual_cp"][0], 0xA0)

    def test_first_difference_marks_extra_trailing_content(self):
        payload = "hello"
        diff = VT.first_difference(payload, "hello world")
        self.assertFalse(diff["normalized_equal"])
        self.assertEqual(diff["index"], len(payload))
        self.assertEqual(diff["normalized_actual_len"], len("hello world"))

    def test_first_difference_handles_missing_readback(self):
        diff = VT.first_difference("hello", None)
        self.assertIsNone(diff["index"])
        self.assertEqual(diff["reason"], "actual-missing")


if __name__ == "__main__":
    unittest.main()
