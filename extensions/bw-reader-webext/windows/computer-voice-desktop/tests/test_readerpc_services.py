from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from readerpc_services import (  # noqa: E402
    PC_OCR_STATUS_CONTRACT,
    PcOcrPaths,
    PcOcrServiceController,
    read_codex_voice_activity,
    read_reader_context_status,
    write_readerpc_status,
)


class FakeProbe:
    def __init__(self, values: dict[int, int | None]):
        self.values = values

    def start_file_time_utc(self, pid: int) -> int | None:
        return self.values.get(pid)


class ReaderPCServicesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        python = self.root / "venv" / "python.exe"
        worker = self.root / "project" / "scripts" / "reader_pc_preprocess_worker.py"
        python.parent.mkdir(parents=True)
        worker.parent.mkdir(parents=True)
        python.write_bytes(b"python")
        worker.write_text("pass\n", "utf-8")
        self.paths = PcOcrPaths(
            local_root=self.root,
            cache_root=self.root / "cache",
            status_file=self.root / "cache" / "worker-status.json",
            stdout_log=self.root / "logs" / "out.log",
            stderr_log=self.root / "logs" / "err.log",
            python_exe=python,
            project_root=self.root / "project",
            worker_script=worker,
            doclayout_model=self.root / "models" / "layout.pt",
            unimernet_model_dir=self.root / "models" / "unimernet",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_worker_is_online_only_for_exact_pid_generation(self) -> None:
        self.paths.status_file.parent.mkdir(parents=True)
        self.paths.status_file.write_text(
            json.dumps(
                {
                    "contract": PC_OCR_STATUS_CONTRACT,
                    "state": "idle",
                    "phase": "preparing",
                    "processId": 4242,
                    "processStartFileTimeUtc": 9001,
                    "gpu": {"deviceName": "RTX"},
                }
            ),
            "utf-8",
        )
        online = PcOcrServiceController(
            self.paths,
            process_probe=FakeProbe({4242: 9001}),
        ).status()
        stale = PcOcrServiceController(
            self.paths,
            process_probe=FakeProbe({4242: 9002}),
        ).status()
        self.assertTrue(online.running)
        self.assertEqual(online.state_label, "在线 · 空闲")
        self.assertFalse(stale.running)
        self.assertEqual(stale.state, "stale")

    def test_context_freshness_uses_received_timestamp(self) -> None:
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(
            json.dumps(
                {
                    "schema": "reader-context-snapshot/1",
                    "activeReading": {
                        "kind": "pdf",
                        "title": "book",
                        "receivedAtEpochMs": 100_000,
                    },
                }
            ),
            "utf-8",
        )
        fresh = read_reader_context_status(snapshot, now_epoch_ms=120_000)
        stale = read_reader_context_status(snapshot, now_epoch_ms=150_000)
        self.assertTrue(fresh.fresh)
        self.assertEqual(fresh.title, "book")
        self.assertFalse(stale.fresh)

    def test_codex_voice_activity_uses_exact_microphone_ledger_semantics(self) -> None:
        active = read_codex_voice_activity(lambda: (101, 100))
        stopped = read_codex_voice_activity(lambda: (101, 102))
        never_started = read_codex_voice_activity(lambda: (0, 0))
        self.assertEqual((active.status, active.active), ("available", True))
        self.assertEqual((stopped.status, stopped.active), ("available", False))
        self.assertEqual(
            (never_started.status, never_started.active),
            ("available", False),
        )

    def test_codex_voice_activity_distinguishes_unavailable_and_invalid(self) -> None:
        unavailable = read_codex_voice_activity(lambda: None)
        invalid = read_codex_voice_activity(lambda: (True, 0))
        denied = read_codex_voice_activity(
            lambda: (_ for _ in ()).throw(PermissionError("denied"))
        )
        self.assertEqual((unavailable.status, unavailable.active), ("unavailable", None))
        self.assertEqual((invalid.status, invalid.active), ("error", None))
        self.assertEqual((denied.status, denied.active), ("error", None))

    def test_unified_status_contains_no_process_paths_or_tokens(self) -> None:
        self.paths.status_file.parent.mkdir(parents=True)
        self.paths.status_file.write_text(
            json.dumps(
                {
                    "contract": PC_OCR_STATUS_CONTRACT,
                    "state": "idle",
                    "phase": "preparing",
                    "processId": 7,
                    "processStartFileTimeUtc": 11,
                    "workerId": "pc_test",
                }
            ),
            "utf-8",
        )
        pc = PcOcrServiceController(
            self.paths,
            process_probe=FakeProbe({7: 11}),
        ).status()
        context_path = self.root / "missing.json"
        context = read_reader_context_status(context_path)
        output = self.root / "readerpc.json"
        write_readerpc_status(
            output,
            voice={"online": True, "reason": "reader-connected"},
            context=context,
            pc_ocr=pc,
        )
        text = output.read_text("utf-8")
        self.assertIn('"contract": "readerpc-server-status/1"', text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("token", text.casefold())

    def test_supervised_worker_command_recycles_after_each_job(self) -> None:
        captured: list[str] = []
        probe = FakeProbe({})

        class Process:
            def poll(self):
                return None

        def popen(command, **_kwargs):
            captured.extend(command)
            probe.values[4242] = 9001
            self.paths.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.paths.status_file.write_text(
                json.dumps(
                    {
                        "contract": PC_OCR_STATUS_CONTRACT,
                        "state": "idle",
                        "phase": "preparing",
                        "processId": 4242,
                        "processStartFileTimeUtc": 9001,
                    }
                ),
                "utf-8",
            )
            return Process()

        status = PcOcrServiceController(
            self.paths,
            process_probe=probe,
            popen=popen,
        ).start()

        self.assertTrue(status.running)
        self.assertIn("--recycle-after-job", captured)


if __name__ == "__main__":
    unittest.main()
