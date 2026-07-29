from __future__ import annotations

from pathlib import Path
import contextlib
import io
import json
import subprocess
import sys
import unittest


WINDOWS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(WINDOWS_ROOT))

from bw_computer_voice_supervisor import SupervisorError, TypistState  # noqa: E402
from bw_computer_voice_typist_helper import main, stop_if_owned  # noqa: E402


def state(*, running: bool, pid: int | None) -> TypistState:
    return TypistState(
        running=running,
        pid=pid,
        paused=False,
        emergency_stop=False,
    )


class FakeLauncher:
    def __init__(self, statuses: list[TypistState]) -> None:
        self._statuses = iter(statuses)
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
        stop_calls: list[Path] = []

        def stop_runner(path: Path) -> subprocess.CompletedProcess[str]:
            stop_calls.append(path)
            return subprocess.CompletedProcess([], 0, "stopped", "")

        result = stop_if_owned(
            4512,
            launcher=launcher,  # type: ignore[arg-type]
            stop_runner=stop_runner,
        )

        self.assertEqual(result["result"], "stopped")
        self.assertTrue(result["stopped"])
        self.assertEqual(launcher.status_calls, 2)
        self.assertEqual(
            stop_calls,
            [
                Path(
                    r"C:\Users\bwica\bw-reader-context\reader-bridge"
                    r"\voice-typist-launcher.ps1"
                )
            ],
        )

    def test_expected_pid_mismatch_fails_closed_without_stop(self):
        launcher = FakeLauncher([state(running=True, pid=9002)])
        stop_calls: list[Path] = []

        with self.assertRaises(SupervisorError) as caught:
            stop_if_owned(
                9001,
                launcher=launcher,  # type: ignore[arg-type]
                stop_runner=lambda path: stop_calls.append(path)
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
        stop_calls: list[Path] = []

        result = stop_if_owned(
            4512,
            launcher=launcher,  # type: ignore[arg-type]
            stop_runner=lambda path: stop_calls.append(path)
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
                launcher=launcher,  # type: ignore[arg-type]
                stop_runner=lambda _path: subprocess.CompletedProcess(
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
            returncode = main(["--stop-if-owned", "4512", "extra"])

        self.assertEqual(returncode, 64)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["code"],
            "BW_COMPUTER_VOICE_ACTION_DENIED",
        )


if __name__ == "__main__":
    unittest.main()
