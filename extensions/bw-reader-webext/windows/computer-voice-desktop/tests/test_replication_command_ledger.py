from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from replication_command_ledger import (  # noqa: E402
    MAX_ENVELOPE_BYTES,
    ReplicationCommandConflict,
    ReplicationCommandError,
    ReplicationCommandLedger,
    validate_command_envelope,
)


def envelope(**overrides):
    op = {
        "mutationId": "mut-v2-" + "1" * 32,
        "url": "/pdf/api/highlights",
        "method": "POST",
        "body": {"file": "localbook:localbook-" + "b" * 64, "page": 3},
    }
    op.update(overrides.pop("op", {}))
    value = {
        "contract": "replication-command/1",
        "deviceId": "native-app-v1-" + "a" * 32,
        "replicationBookId": "repbook-" + "c" * 32,
        "actor": "user",
        "op": op,
    }
    value.update(overrides)
    return value


class EnvelopeValidationTests(unittest.TestCase):
    def test_accepts_all_device_families_and_actors(self) -> None:
        for family in ("native-app", "pwa-install", "server-node"):
            for actor in ("user", "ai", "system"):
                value = envelope(
                    deviceId=f"{family}-v1-" + "a" * 32, actor=actor,
                    op={"mutationId": "mut-v2-" + "2" * 32},
                )
                self.assertIs(validate_command_envelope(value), value)

    def test_rejects_malformed_envelopes(self) -> None:
        cases = [
            envelope(contract="command-outbox/2"),
            envelope(deviceId="windows-v1-" + "a" * 32),
            envelope(deviceId="src_" + "a" * 12),  # sourceInstanceId 形状
            envelope(replicationBookId="book_" + "a" * 32),
            envelope(actor="assistant"),
            envelope(op={"mutationId": "mut-v1-" + "1" * 32}),
            envelope(op={"url": "https://evil.example/x"}),
            envelope(op={"url": "/pdf/api/../secrets"}),
            envelope(op={"url": "//pdf/api/highlights"}),
            envelope(op={"method": "GET"}),
            envelope(op={"body": []}),
            envelope(extra="field"),
        ]
        for case in cases:
            with self.assertRaises(ReplicationCommandError, msg=json.dumps(case)[:120]):
                validate_command_envelope(case)

    def test_missing_op_key_is_rejected(self) -> None:
        value = envelope()
        del value["op"]["body"]
        with self.assertRaises(ReplicationCommandError):
            validate_command_envelope(value)

    def test_oversized_envelope_is_rejected(self) -> None:
        value = envelope(op={"body": {"text": "x" * (MAX_ENVELOPE_BYTES + 1)}})
        with self.assertRaises(ReplicationCommandError):
            validate_command_envelope(value)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "ledger.sqlite3"
        self.now = 5_000
        self.ledger = ReplicationCommandLedger(
            self.path, clock_utc_ms=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def test_append_assigns_monotonic_cursor_and_roundtrips(self) -> None:
        first = self.ledger.append(envelope())
        second = self.ledger.append(envelope(op={"mutationId": "mut-v2-" + "2" * 32}))
        self.assertEqual((first.cursor, second.cursor), (1, 2))
        self.assertEqual(self.ledger.head_cursor(), 2)
        self.assertEqual(first.envelope(), envelope())
        self.assertEqual(first.received_at_utc_ms, 5_000)

    def test_replay_returns_original_row_without_new_cursor(self) -> None:
        first = self.ledger.append(envelope())
        replay = self.ledger.append(envelope())
        self.assertEqual(replay.cursor, first.cursor)
        self.assertTrue(replay.replayed)
        self.assertFalse(first.replayed)
        self.assertEqual(self.ledger.head_cursor(), 1)

    def test_same_mutation_id_different_payload_is_a_loud_conflict(self) -> None:
        self.ledger.append(envelope())
        changed = envelope(op={"body": {"file": "x", "page": 4}})
        with self.assertRaises(ReplicationCommandConflict):
            self.ledger.append(changed)
        self.assertEqual(self.ledger.head_cursor(), 1)

    def test_entries_after_pages_in_cursor_order(self) -> None:
        for index in range(5):
            self.ledger.append(envelope(
                op={"mutationId": "mut-v2-" + format(index, "032x")},
            ))
        page = self.ledger.entries_after(2, limit=2)
        self.assertEqual([entry.cursor for entry in page], [3, 4])
        self.assertEqual(self.ledger.entries_after(5), [])
        with self.assertRaises(ReplicationCommandError):
            self.ledger.entries_after(-1)

    def test_ledger_persists_across_reopen(self) -> None:
        self.ledger.append(envelope())
        self.ledger.close()
        reopened = ReplicationCommandLedger(self.path)
        try:
            self.assertEqual(reopened.head_cursor(), 1)
            entry = reopened.entries_after(0)[0]
            self.assertEqual(entry.mutation_id, "mut-v2-" + "1" * 32)
            self.assertEqual(entry.actor, "user")
        finally:
            reopened.close()

    def test_invalid_envelope_never_lands_in_the_ledger(self) -> None:
        with self.assertRaises(ReplicationCommandError):
            self.ledger.append(envelope(actor="assistant"))
        self.assertEqual(self.ledger.head_cursor(), 0)

    def test_received_at_override_is_persisted(self) -> None:
        entry = self.ledger.append(envelope(), received_at_utc_ms=42)
        self.assertEqual(entry.received_at_utc_ms, 42)
        with self.assertRaises(ReplicationCommandError):
            self.ledger.append(
                envelope(op={"mutationId": "mut-v2-" + "9" * 32}),
                received_at_utc_ms=True,
            )


def spool_line(value, received_at=7_000):
    return json.dumps({
        "contract": "replication-spool-line/1",
        "receivedAtUtcMs": received_at,
        "envelope": value,
    })


class SpoolIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.spool = root / "replication-spool"
        self.spool.mkdir()
        self.ledger = ReplicationCommandLedger(root / "ledger.sqlite3")

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def _write(self, name: str, lines: list[str]) -> Path:
        path = self.spool / name
        path.write_text("\n".join(lines) + "\n", "utf-8")
        return path

    def test_ingest_counts_duplicates_as_replayed_once_in_ledger(self) -> None:
        self._write("inbox-20260823.jsonl", [
            spool_line(envelope()),
            spool_line(envelope()),  # C# 崩在 ack 前重投出的重复行
            spool_line(envelope(op={"mutationId": "mut-v2-" + "2" * 32})),
        ])
        report = self.ledger.ingest_spool_directory(self.spool)
        self.assertEqual(report["ingested"], 2)
        self.assertEqual(report["replayed"], 1)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["invalid"], [])
        self.assertEqual(self.ledger.head_cursor(), 2)
        entry = self.ledger.entries_after(0)[0]
        self.assertEqual(entry.received_at_utc_ms, 7_000)

    def test_poison_lines_are_reported_loudly_but_do_not_block_the_rest(self) -> None:
        conflicting = envelope(op={"body": {"file": "changed", "page": 9}})
        self._write("inbox-20260823.jsonl", [
            spool_line(envelope()),
            "not json at all",
            spool_line({"contract": "wrong/1"}),
            spool_line(conflicting),  # 同 mutationId 不同内容
            spool_line(envelope(op={"mutationId": "mut-v2-" + "3" * 32})),
        ])
        report = self.ledger.ingest_spool_directory(self.spool)
        self.assertEqual(report["ingested"], 2)
        self.assertEqual(len(report["invalid"]), 2)
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["conflicts"][0]["line"], 4)
        self.assertEqual(self.ledger.head_cursor(), 2)

    def test_compact_deletes_only_past_fully_ingested_segments(self) -> None:
        self._write("inbox-20260823.jsonl", [spool_line(envelope())])
        self._write("inbox-20260824.jsonl", [
            spool_line(envelope(op={"mutationId": "mut-v2-" + "4" * 32})),
        ])
        self._write("inbox-20260822.jsonl", ["not json"])
        outcome = self.ledger.compact_spool(self.spool, today_utc="20260824")
        self.assertEqual(outcome["deleted"], ["inbox-20260823.jsonl"])
        kept = {item["file"]: item["reason"] for item in outcome["kept"]}
        self.assertEqual(kept["inbox-20260824.jsonl"], "active-day")
        self.assertEqual(kept["inbox-20260822.jsonl"], "poison-lines")
        self.assertFalse((self.spool / "inbox-20260823.jsonl").exists())
        self.assertTrue((self.spool / "inbox-20260822.jsonl").exists())
        # compact 的重扫本身就把段入了账
        self.assertEqual(self.ledger.head_cursor(), 1)


if __name__ == "__main__":
    unittest.main()
