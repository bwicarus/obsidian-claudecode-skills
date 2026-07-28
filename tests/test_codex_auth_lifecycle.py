"""Offline contracts for Codex auth snapshots and app-server retirement.

Every process/thread boundary in this module is faked.  These tests must never
start a real Codex app-server or inspect the developer's real credential files.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import assistant  # noqa: E402
import reader_sidecar_store  # noqa: E402


_REAL_THREAD = threading.Thread


class _FakeStdin:
    def write(self, _payload: str) -> None:
        raise AssertionError("offline lifecycle tests must mock every RPC write")

    def flush(self) -> None:
        raise AssertionError("offline lifecycle tests must mock every RPC flush")


class _FakeProcess:
    def __init__(self, stdout_lines=()):
        self.stdin = _FakeStdin()
        self.stdout = list(stdout_lines)
        self.returncode = None
        self.terminate_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15


class _DormantReaderThread:
    """Record reader creation without executing its target."""

    instances = []

    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True


class CodexAuthBootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        self.source = root / "primary" / "auth.json"
        self.reader_home = root / "reader-home"
        self.reader_cwd = root / "reader-cwd"
        self.source.parent.mkdir(parents=True)

        self._patchers = [
            patch.object(assistant, "_CODEX_AUTH_SOURCE", self.source),
            patch.object(assistant, "_CODEX_RC_HOME", self.reader_home),
            patch.object(assistant, "_CODEX_RC_CWD", self.reader_cwd),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _auth_payload(label: str) -> bytes:
        return (
            json.dumps(
                {"tokens": {"access_token": label}},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def test_valid_primary_auth_is_atomically_synced_with_mode_0600(self) -> None:
        primary = self._auth_payload("primary-new")
        self.source.write_bytes(primary)
        self.reader_home.mkdir()
        (self.reader_home / "auth.json").write_bytes(
            self._auth_payload("reader-old")
        )

        with patch.object(
            reader_sidecar_store,
            "atomic_write_bytes",
            wraps=reader_sidecar_store.atomic_write_bytes,
        ) as atomic_write:
            result = assistant._codex_rc_bootstrap()

        target = self.reader_home / "auth.json"
        self.assertEqual(target.read_bytes(), primary)
        self.assertEqual(
            stat.S_IMODE(target.stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(self.reader_home.stat().st_mode),
            0o700,
        )
        self.assertEqual(result["generation"], hashlib.sha256(primary).hexdigest())
        self.assertTrue(result["changed"])
        self.assertEqual(result["error"], "")
        atomic_write.assert_called_once_with(target, primary, mode=0o600)

    def test_invalid_primary_auth_never_overwrites_valid_reader_snapshot(self) -> None:
        self.source.write_bytes(b'{"tokens":')
        self.reader_home.mkdir()
        reader = self._auth_payload("last-known-good")
        target = self.reader_home / "auth.json"
        target.write_bytes(reader)

        with patch.object(
            reader_sidecar_store,
            "atomic_write_bytes",
            wraps=reader_sidecar_store.atomic_write_bytes,
        ) as atomic_write:
            result = assistant._codex_rc_bootstrap()

        self.assertEqual(target.read_bytes(), reader)
        self.assertEqual(result["generation"], hashlib.sha256(reader).hexdigest())
        self.assertFalse(result["changed"])
        self.assertEqual(
            result["error"],
            "主 Codex 认证暂不可读取，沿用最后有效快照",
        )
        atomic_write.assert_not_called()


class CodexAppAuthLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        _DormantReaderThread.instances.clear()

    @staticmethod
    def _bootstrap_for(generation):
        return {
            "generation": generation(),
            "changed": False,
            "error": "",
        }

    def test_auth_change_blocks_new_work_then_last_close_retires_old_process(
        self,
    ) -> None:
        generation = ["auth-v1"]
        processes = []

        def fake_popen(*_args, **_kwargs):
            process = _FakeProcess()
            processes.append(process)
            return process

        client = assistant._CodexApp()
        client._rpc = Mock(return_value={})
        client._notify = Mock()

        with (
            patch.object(
                assistant,
                "_codex_rc_bootstrap",
                side_effect=lambda: self._bootstrap_for(lambda: generation[0]),
            ),
            patch.object(assistant.subprocess, "Popen", side_effect=fake_popen),
            patch.object(assistant.threading, "Thread", _DormantReaderThread),
        ):
            self.assertEqual(client._ensure(), "auth-v1")
            process = processes[0]
            client._turns["open-thread"] = queue.Queue()
            generation[0] = "auth-v2"

            with self.assertRaisesRegex(
                RuntimeError,
                "等待当前多轮会话结束后切换",
            ):
                client.thread_start()

            self.assertIs(client._p, process)
            self.assertEqual(process.terminate_calls, 0)
            self.assertEqual(client._auth_generation, "auth-v1")
            self.assertEqual(client._restart_generation, "auth-v2")
            self.assertEqual(len(processes), 1)
            self.assertEqual(
                [call.args[0] for call in client._rpc.call_args_list],
                ["initialize"],
            )

            client.thread_close("open-thread")

        self.assertIsNone(client._p)
        self.assertEqual(client._auth_generation, "")
        self.assertEqual(process.terminate_calls, 1)

    def test_stale_reader_epoch_cannot_consume_current_pending_or_turn_queue(
        self,
    ) -> None:
        cases = (
            (
                '{"id":201,"result":{"ok":true}}\n',
                "pending",
            ),
            (
                '{"method":"turn/completed","params":'
                '{"threadId":"current-thread","turn":{"status":"completed"}}}\n',
                "turn",
            ),
        )
        for line, route in cases:
            with self.subTest(route=route):
                client = assistant._CodexApp()
                process = _FakeProcess([line])
                current_queue = queue.Queue()
                client._p = process
                client._epoch = 2
                if route == "pending":
                    client._pending[201] = current_queue
                else:
                    client._turns["current-thread"] = current_queue

                client._reader(process, epoch=1)

                self.assertTrue(current_queue.empty())
                if route == "pending":
                    self.assertIs(client._pending[201], current_queue)
                else:
                    self.assertIs(
                        client._turns["current-thread"],
                        current_queue,
                    )

    def test_concurrent_first_ensure_initializes_exactly_one_process(self) -> None:
        worker_count = 8
        start = threading.Barrier(worker_count)
        results = []
        errors = []
        processes = []
        rpc_methods = []
        result_lock = threading.Lock()

        def fake_popen(*_args, **_kwargs):
            process = _FakeProcess()
            processes.append(process)
            return process

        def fake_rpc(method, _params, timeout=20):
            with result_lock:
                rpc_methods.append((method, timeout))
            if method != "initialize":
                raise AssertionError(f"unexpected RPC: {method}")
            time.sleep(0.03)
            return {}

        client = assistant._CodexApp()
        client._rpc = fake_rpc
        client._notify = Mock()

        def worker():
            try:
                start.wait(timeout=2)
                value = client._ensure()
                with result_lock:
                    results.append(value)
            except BaseException as exc:  # preserve worker failures for main test
                with result_lock:
                    errors.append(exc)

        with (
            patch.object(
                assistant,
                "_codex_rc_bootstrap",
                return_value={
                    "generation": "auth-v1",
                    "changed": False,
                    "error": "",
                },
            ),
            patch.object(assistant.subprocess, "Popen", side_effect=fake_popen),
            patch.object(assistant.threading, "Thread", _DormantReaderThread),
        ):
            workers = [_REAL_THREAD(target=worker) for _ in range(worker_count)]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(timeout=3)

        self.assertFalse(any(thread.is_alive() for thread in workers))
        self.assertEqual(errors, [])
        self.assertEqual(results, ["auth-v1"] * worker_count)
        self.assertEqual(len(processes), 1)
        self.assertEqual(rpc_methods, [("initialize", 15)])
        client._notify.assert_called_once_with("initialized", {})
        self.assertEqual(len(_DormantReaderThread.instances), 1)
        self.assertTrue(_DormantReaderThread.instances[0].started)


if __name__ == "__main__":
    unittest.main()
