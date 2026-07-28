import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from scripts.kg import audit_edges as AE


class _MutableStatusService:
    def __init__(self, status):
        self._status = status
        self._lock = threading.Lock()

    def set_status(self, status):
        with self._lock:
            self._status = status

    def mutation_status(self, _mutation_id):
        with self._lock:
            return {"status": self._status}


class AuditLogOutboxTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.log_path = self.root / "attention" / "edge-audit-log.jsonl"
        self.outbox_path = self.root / "attention" / "edge-audit-outbox"
        self._patches = [
            mock.patch.object(AE, "LOG", self.log_path),
            mock.patch.object(AE, "AUDIT_OUTBOX", self.outbox_path),
        ]
        for patcher in self._patches:
            patcher.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        self._temporary.cleanup()

    @staticmethod
    def _logs():
        return [
            {
                "ts": 123,
                "edge": "concept:a|concept:b|related",
                "edge_id": "edge-1",
                "verdict": "keep",
                "reason": "证据充分",
                "was": {"kind": "related", "status": "auto"},
            }
        ]

    def _read_log_rows(self):
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text("utf-8").split("\n")
            if line.strip()
        ]

    def test_applied_outbox_concurrent_flush_appends_exactly_once(self):
        mutation_id = "audit-edges:concurrent"
        staged = AE._stage_audit_logs(mutation_id, self._logs())
        service = _MutableStatusService("applied")

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(AE._flush_audit_outbox, service)
                for _ in range(16)
            ]
            for future in futures:
                future.result()

        rows = self._read_log_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(mutation_id + ":edge-1", rows[0]["entry_id"])
        self.assertEqual(mutation_id, rows[0]["mutation_id"])
        self.assertFalse(staged.exists())
        self.assertEqual([], list(self.outbox_path.glob("*.json")))

    def test_absent_is_retained_then_applied_is_flushed(self):
        mutation_id = "audit-edges:deferred"
        staged = AE._stage_audit_logs(mutation_id, self._logs())
        staged_bytes = staged.read_bytes()
        service = _MutableStatusService("absent")

        AE._flush_audit_outbox(service)

        self.assertTrue(staged.exists())
        self.assertEqual(staged_bytes, staged.read_bytes())
        self.assertFalse(self.log_path.exists())

        retried_logs = self._logs()
        retried_logs[0]["ts"] = 999
        self.assertEqual(
            staged,
            AE._stage_audit_logs(mutation_id, retried_logs),
        )
        self.assertEqual(staged_bytes, staged.read_bytes())
        changed_logs = self._logs()
        changed_logs[0]["verdict"] = "remove"
        with self.assertRaisesRegex(RuntimeError, "不同日志 payload"):
            AE._stage_audit_logs(mutation_id, changed_logs)

        service.set_status("applied")
        AE._flush_audit_outbox(service)

        rows = self._read_log_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(mutation_id + ":edge-1", rows[0]["entry_id"])
        self.assertFalse(staged.exists())

    def test_corrupt_outbox_fails_closed_without_mutating_files(self):
        self.outbox_path.mkdir(parents=True)
        corrupt = self.outbox_path / "corrupt.json"
        corrupt_bytes = b'{"contract":"kg-edge-audit-outbox/1",'
        corrupt.write_bytes(corrupt_bytes)
        service = _MutableStatusService("applied")

        with self.assertRaisesRegex(RuntimeError, "outbox"):
            AE._flush_audit_outbox(service)

        self.assertEqual(corrupt_bytes, corrupt.read_bytes())
        self.assertFalse(self.log_path.exists())

    def test_corrupt_existing_log_fails_closed_and_retains_payload(self):
        mutation_id = "audit-edges:log-corrupt"
        staged = AE._stage_audit_logs(mutation_id, self._logs())
        staged_bytes = staged.read_bytes()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_log = b'{"entry_id":'
        self.log_path.write_bytes(corrupt_log)
        service = _MutableStatusService("applied")

        with self.assertRaisesRegex(RuntimeError, "log"):
            AE._flush_audit_outbox(service)

        self.assertTrue(staged.exists())
        self.assertEqual(staged_bytes, staged.read_bytes())
        self.assertEqual(corrupt_log, self.log_path.read_bytes())

    def test_tampered_entry_identity_is_not_silently_discarded(self):
        mutation_id = "audit-edges:tampered-entry"
        staged = AE._stage_audit_logs(mutation_id, self._logs())
        payload = json.loads(staged.read_text("utf-8"))
        payload["entries"][0]["entry_id"] = ""
        payload["payloadDigest"] = AE._audit_outbox_digest(payload)
        staged.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "utf-8",
        )
        staged_bytes = staged.read_bytes()
        service = _MutableStatusService("applied")

        with self.assertRaisesRegex(RuntimeError, "entry"):
            AE._flush_audit_outbox(service)

        self.assertTrue(staged.exists())
        self.assertEqual(staged_bytes, staged.read_bytes())
        self.assertFalse(self.log_path.exists())

    def test_unicode_line_separators_inside_log_are_not_record_boundaries(self):
        service = _MutableStatusService("applied")
        unicode_logs = self._logs()
        unicode_logs[0]["reason"] = "left\u0085middle\u2028next\u2029right"
        AE._stage_audit_logs("audit-edges:unicode", unicode_logs)
        AE._flush_audit_outbox(service)
        second_logs = self._logs()
        second_logs[0]["edge_id"] = "edge-2"
        AE._stage_audit_logs("audit-edges:after-unicode", second_logs)
        AE._flush_audit_outbox(service)

        rows = self._read_log_rows()
        self.assertEqual(2, len(rows))
        self.assertEqual(unicode_logs[0]["reason"], rows[0]["reason"])
        self.assertEqual("edge-2", rows[1]["edge_id"])


if __name__ == "__main__":
    unittest.main()
