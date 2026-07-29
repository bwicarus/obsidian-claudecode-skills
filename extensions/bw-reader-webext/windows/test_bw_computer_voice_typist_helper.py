from __future__ import annotations

from pathlib import Path
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


WINDOWS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(WINDOWS_ROOT))

from bw_computer_voice_supervisor import (  # noqa: E402
    DEFAULT_TYPING_LAUNCHER,
    DEFAULT_TYPING_SCRIPT,
    SupervisorError,
    TypistState,
)
from bw_computer_voice_typist_helper import (  # noqa: E402
    _default_stop_runner,
    main,
    stop_if_owned,
)


def state(
    *,
    running: bool,
    pid: int | None,
    start_file_time_utc: int = 133700000000000000,
) -> TypistState:
    return TypistState(
        running=running,
        pid=pid,
        process_start_file_time_utc=(
            start_file_time_utc if running else None
        ),
        paused=False,
        emergency_stop=False,
    )


class FakeLauncher:
    def __init__(
        self,
        statuses: list[TypistState],
        *,
        launcher_path: Path = DEFAULT_TYPING_LAUNCHER,
    ) -> None:
        self._statuses = iter(statuses)
        self.launcher_path = launcher_path
        self.status_calls = 0

    def verified_status(self) -> TypistState:
        self.status_calls += 1
        return next(self._statuses)


class VoiceTypistHelperLeaseTest(unittest.TestCase):
    def test_exact_owned_pid_calls_fixed_launcher_stop_and_verifies_exit(self):
        launcher = FakeLauncher(
            [
                state(running=True, pid=4512),
                state(running=False, pid=None),
            ]
        )
        stop_calls: list[tuple[Path, int, int]] = []

        def stop_runner(
            path: Path,
            expected_pid: int,
            expected_start_file_time_utc: int,
        ) -> subprocess.CompletedProcess[str]:
            stop_calls.append((
                path,
                expected_pid,
                expected_start_file_time_utc,
            ))
            return subprocess.CompletedProcess([], 0, "stopped", "")

        result = stop_if_owned(
            4512,
            133700000000000000,
            launcher=launcher,  # type: ignore[arg-type]
            stop_runner=stop_runner,
        )

        self.assertEqual(result["result"], "stopped")
        self.assertTrue(result["stopped"])
        self.assertEqual(launcher.status_calls, 2)
        self.assertEqual(
            stop_calls,
            [(
                WINDOWS_ROOT / "typist-runtime" / "voice-typist-launcher.ps1",
                4512,
                133700000000000000,
            )],
        )

    def test_default_runtime_paths_are_module_relative_without_external_fallback(self):
        self.assertEqual(
            DEFAULT_TYPING_LAUNCHER,
            WINDOWS_ROOT / "typist-runtime" / "voice-typist-launcher.ps1",
        )
        self.assertEqual(
            DEFAULT_TYPING_SCRIPT,
            WINDOWS_ROOT / "typist-runtime" / "voice_typist.py",
        )
        self.assertNotIn("bw-reader-context", str(DEFAULT_TYPING_LAUNCHER))
        self.assertNotIn("bw-reader-context", str(DEFAULT_TYPING_SCRIPT))

    def test_canonical_launcher_is_direct_v3_and_requires_second_pid_fence(self):
        source = DEFAULT_TYPING_LAUNCHER.read_text(encoding="utf-8-sig")
        self.assertIn("$install = $PSScriptRoot", source)
        self.assertIn("[int]$ExpectedPid = 0", source)
        self.assertIn("[long]$ExpectedStartFileTimeUtc = 0", source)
        self.assertIn("[int]$OwnerPid = 0", source)
        self.assertIn("[long]$OwnerStartFileTimeUtc = 0", source)
        self.assertIn("$(if ($OwnerPid -gt 0) { '0' } else { '600' })", source)
        self.assertIn("'--owner-process-id'", source)
        self.assertIn("$ownerArgumentsMatch", source)
        self.assertIn("'ResolveUncertain'", source)
        self.assertIn("'--launcher-confirmed-stopped'", source)
        self.assertIn("'queue-status'", source)
        self.assertIn(
            "'reader-voice-typist-queue-status/1'",
            source,
        )
        self.assertIn(
            "'reader-voice-typist-queue/3'",
            source,
        )
        self.assertIn("Get-TypistProcess -Strict", source)
        self.assertIn(
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            source,
        )
        self.assertIn(
            '"Local\\BWReaderVoiceTypistLifecycle-v3-$currentUserSid"',
            source,
        )
        self.assertIn("function Invoke-WithTypistLifecycleLock", source)
        self.assertIn("[System.Threading.Mutex]::new", source)
        self.assertIn(
            "catch [System.Threading.AbandonedMutexException]",
            source,
        )
        self.assertIn("$mutex.ReleaseMutex()", source)
        self.assertIn("$mutex.Dispose()", source)
        self.assertEqual(
            source.count("        Invoke-WithTypistLifecycleLock {"),
            3,
        )
        start_block = source[
            source.index("    'Start' {"):
            source.index("    'Stop' {")
        ]
        stop_block = source[
            source.index("    'Stop' {"):
            source.index("    'Pause'")
        ]
        resolve_block = source[source.index("    'ResolveUncertain' {"):]
        self.assertIn("Invoke-WithTypistLifecycleLock {", start_block)
        self.assertIn("$existing = Get-TypistProcess -Strict", start_block)
        self.assertIn("Invoke-WithTypistLifecycleLock {", stop_block)
        self.assertIn("Invoke-WithTypistLifecycleLock {", resolve_block)
        self.assertIn("Get-TypistProcess -Strict", resolve_block)
        self.assertNotIn(".IndexOf($script", source)
        self.assertNotIn("bw-reader-context", source)
        self.assertNotIn("JournalUrl", source)
        self.assertNotIn("--journal-url", source)
        self.assertNotIn("'--clear-stop'", source)

    def test_default_stop_runner_passes_expected_pid_to_launcher(self):
        with patch(
            "bw_computer_voice_typist_helper.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            _default_stop_runner(
                DEFAULT_TYPING_LAUNCHER,
                4512,
                133700000000000000,
            )
        argv = run.call_args.args[0]
        self.assertEqual(
            argv[argv.index("-ExpectedPid") + 1],
            "4512",
        )
        self.assertEqual(
            argv[argv.index("-ExpectedStartFileTimeUtc") + 1],
            "133700000000000000",
        )

    def test_stop_uses_the_verified_controller_launcher_path(self):
        launcher_path = WINDOWS_ROOT / "isolated-runtime" / "launcher.ps1"
        launcher = FakeLauncher(
            [
                state(running=True, pid=4512),
                state(running=False, pid=None),
            ],
            launcher_path=launcher_path,
        )
        stop_calls: list[tuple[Path, int, int]] = []

        result = stop_if_owned(
            4512,
            133700000000000000,
            launcher=launcher,  # type: ignore[arg-type]
            stop_runner=lambda path, expected_pid, expected_start: stop_calls.append(
                (path, expected_pid, expected_start)
            )
            or subprocess.CompletedProcess([], 0, "", ""),
        )

        self.assertEqual(result["result"], "stopped")
        self.assertEqual(
            stop_calls,
            [(launcher_path, 4512, 133700000000000000)],
        )

    def test_expected_pid_mismatch_fails_closed_without_stop(self):
        launcher = FakeLauncher([state(running=True, pid=9002)])
        stop_calls: list[tuple[Path, int, int]] = []

        with self.assertRaises(SupervisorError) as caught:
            stop_if_owned(
                9001,
                133700000000000000,
                launcher=launcher,  # type: ignore[arg-type]
                stop_runner=lambda path, expected_pid, expected_start: stop_calls.append(
                    (path, expected_pid, expected_start)
                )
                or subprocess.CompletedProcess([], 0, "", ""),
            )

        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_TYPIST_LEASE_MISMATCH",
        )
        self.assertEqual(stop_calls, [])
        self.assertEqual(launcher.status_calls, 1)

    def test_reused_pid_with_different_start_time_fails_closed_without_stop(self):
        launcher = FakeLauncher([
            state(
                running=True,
                pid=4512,
                start_file_time_utc=133700000000000001,
            )
        ])
        stop_calls: list[tuple[Path, int, int]] = []

        with self.assertRaises(SupervisorError) as caught:
            stop_if_owned(
                4512,
                133700000000000000,
                launcher=launcher,  # type: ignore[arg-type]
                stop_runner=lambda path, expected_pid, expected_start: stop_calls.append(
                    (path, expected_pid, expected_start)
                )
                or subprocess.CompletedProcess([], 0, "", ""),
            )

        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_TYPIST_LEASE_MISMATCH",
        )
        self.assertEqual(stop_calls, [])
        self.assertEqual(launcher.status_calls, 1)

    def test_already_stopped_lease_is_idempotent_without_stop(self):
        launcher = FakeLauncher([state(running=False, pid=None)])
        stop_calls: list[tuple[Path, int, int]] = []

        result = stop_if_owned(
            4512,
            133700000000000000,
            launcher=launcher,  # type: ignore[arg-type]
            stop_runner=lambda path, expected_pid, expected_start: stop_calls.append(
                (path, expected_pid, expected_start)
            )
            or subprocess.CompletedProcess([], 0, "", ""),
        )

        self.assertEqual(result["result"], "already-stopped")
        self.assertFalse(result["stopped"])
        self.assertEqual(stop_calls, [])

    def test_stop_failure_does_not_claim_success(self):
        launcher = FakeLauncher([state(running=True, pid=4512)])

        with self.assertRaises(SupervisorError) as caught:
            stop_if_owned(
                4512,
                133700000000000000,
                launcher=launcher,  # type: ignore[arg-type]
                stop_runner=lambda _path, _expected_pid, _expected_start: subprocess.CompletedProcess(
                    [],
                    1,
                    "",
                    "failed",
                ),
            )

        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_TYPIST_STOP_FAILED",
        )
        self.assertEqual(launcher.status_calls, 1)

    def test_invalid_argv_is_rejected_without_launcher_action(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            returncode = main([
                "--stop-if-owned",
                "4512",
                "133700000000000000",
                "extra",
            ])

        self.assertEqual(returncode, 64)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["code"],
            "BW_COMPUTER_VOICE_ACTION_DENIED",
        )


@unittest.skipUnless(
    os.name == "nt" and shutil.which("powershell.exe"),
    "launcher lifecycle behavior requires Windows PowerShell",
)
class VoiceTypistLauncherLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            self.skipTest("LOCALAPPDATA is unavailable")
        expected_python = (
            Path(local_app_data)
            / "Programs"
            / "Python"
            / "Python313"
            / "python.exe"
        )
        try:
            same_python = (
                expected_python.resolve(strict=True)
                == Path(sys.executable).resolve(strict=True)
            )
        except OSError:
            same_python = False
        if not same_python:
            self.skipTest(
                "test interpreter is not the launcher's fixed Python runtime"
            )

    def _start_fake_runtime(
        self,
        *,
        corrupt_start_identity: bool = False,
    ) -> tuple[Path, Path, subprocess.Popen[str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name)
        launcher = runtime / "voice-typist-launcher.ps1"
        launcher.write_bytes(DEFAULT_TYPING_LAUNCHER.read_bytes())
        fake_script = runtime / "voice_typist.py"
        fake_script.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "import time\n"
            "if len(sys.argv) > 1:\n"
            "    Path(__file__).with_name('UNEXPECTED_INVOKE').write_text("
            "' '.join(sys.argv[1:]), encoding='utf-8')\n"
            "    raise SystemExit(91)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [sys.executable, str(fake_script)],
            creationflags=creation_flags,
            text=True,
        )

        def stop_fake_process() -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        self.addCleanup(stop_fake_process)
        start_time = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    f"(Get-Process -Id {process.pid}).StartTime."
                    "ToUniversalTime().ToFileTimeUtc()"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        start_file_time_utc = int(start_time.stdout.strip())
        if corrupt_start_identity:
            start_file_time_utc += 1
        (runtime / "voice-typist.pid").write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "startedAtFileTimeUtc": start_file_time_utc,
                    "script": str(fake_script),
                    "ownerPid": None,
                    "ownerStartFileTimeUtc": None,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return launcher, runtime, process

    def _resolve(self, launcher: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-Action",
                "ResolveUncertain",
                "-SessionId",
                "session-test",
                "-EventId",
                "event-test",
                "-Sequence",
                "1",
                "-DeliveryResolution",
                "Delivered",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def _stopped_status(
        self,
        fake_script_body: str,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name)
        launcher = runtime / "voice-typist-launcher.ps1"
        launcher.write_bytes(DEFAULT_TYPING_LAUNCHER.read_bytes())
        (runtime / "voice_typist.py").write_text(
            fake_script_body,
            encoding="utf-8",
        )
        state_dir = runtime / "state"
        state_dir.mkdir()
        (state_dir / "status.json").write_text(
            json.dumps({
                "running": False,
                "queue_depth": 0,
                "queue_blocked_reason": None,
                "blocked_session_id": None,
                "blocked_event_id": None,
                "blocked_sequence": None,
            }),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-Action",
                "Status",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result, runtime

    def test_resolve_uncertain_rejects_verified_running_process(self):
        launcher, runtime, process = self._start_fake_runtime()

        result = self._resolve(launcher)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Stop voice-typist before resolving an uncertain delivery",
            result.stderr,
        )
        self.assertIsNone(process.poll())
        self.assertFalse((runtime / "UNEXPECTED_INVOKE").exists())

    def test_resolve_uncertain_rejects_identity_error_instead_of_treating_stopped(self):
        launcher, runtime, process = self._start_fake_runtime(
            corrupt_start_identity=True,
        )

        result = self._resolve(launcher)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Typist process identity invalid", result.stderr)
        self.assertIsNone(process.poll())
        self.assertFalse((runtime / "UNEXPECTED_INVOKE").exists())

    def test_stopped_status_uses_durable_queue_identity(self):
        envelope = {
            "contract": "reader-voice-typist-queue-status/1",
            "ok": True,
            "queueContract": "reader-voice-typist-queue/3",
            "payload": {
                "queue_depth": 1,
                "queue_blocked_reason": "delivery_uncertain",
                "blocked_session_id": "session-test",
                "blocked_event_id": "event-test",
                "blocked_sequence": 7,
            },
        }
        result, _runtime = self._stopped_status(
            "import json\n"
            "import sys\n"
            "if 'queue-status' not in sys.argv:\n"
            "    raise SystemExit(92)\n"
            f"print({json.dumps(json.dumps(envelope))})\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertFalse(status["running"])
        self.assertEqual(status["queueDepth"], 1)
        self.assertEqual(
            status["queueBlockedReason"],
            "delivery_uncertain",
        )
        self.assertEqual(status["blockedSessionId"], "session-test")
        self.assertEqual(status["blockedEventId"], "event-test")
        self.assertEqual(status["blockedSequence"], 7)

    def test_stopped_status_fails_when_queue_inspection_fails(self):
        result, _runtime = self._stopped_status(
            "import sys\n"
            "if 'queue-status' in sys.argv:\n"
            "    raise SystemExit(91)\n"
            "raise SystemExit(92)\n"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "voice-typist command failed with exit code 91",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
