from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions" / "bw-reader-webext" / "windows"))

from bw_computer_voice_supervisor import (  # noqa: E402
    BRIDGE_COMMAND_CONTRACT,
    CommandReceiptStore,
    CombinedVoiceStartCoordinator,
    LocalBridgeConfig,
    START_ACTION,
    SupervisorError,
    TypistProcess,
    VoiceTypistLauncher,
)


LAUNCHER = Path(
    r"C:\Users\bwica\bw-reader-context\reader-bridge\voice-typist-launcher.ps1"
)
SCRIPT = Path(
    r"C:\Users\bwica\bw-reader-context\reader-bridge\voice_typist.py"
)


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def status_json(*, running=False, pid=None, paused=False, emergency=False):
    return json.dumps(
        {
            "running": running,
            "pid": pid,
            "paused": paused,
            "emergencyStop": emergency,
        }
    )


def typist_process(pid=41, session_id=1):
    return TypistProcess(
        pid=pid,
        session_id=session_id,
        command_line=f'python.exe "{SCRIPT}" run --journal-url https://example.invalid',
    )


class VoiceTypistLauncherTest(unittest.TestCase):
    def make_launcher(self, outputs, probes):
        calls = []
        output_iter = iter(outputs)
        probe_iter = iter(probes)

        def runner(argv, timeout):
            calls.append((tuple(argv), timeout))
            return next(output_iter)

        launcher = VoiceTypistLauncher(
            launcher_path=LAUNCHER,
            typist_script=SCRIPT,
            runner=runner,
            process_probe=lambda: next(probe_iter),
            session_id_provider=lambda: 1,
        )
        return launcher, calls

    def test_running_instance_is_reused_without_start(self):
        launcher, calls = self.make_launcher(
            [completed([], stdout=status_json(running=True, pid=41))],
            [[typist_process()]],
        )
        result = launcher.ensure_running()
        self.assertEqual(result["result"], "already-running")
        self.assertEqual(result["pid"], 41)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][-1], "Status")
        self.assertNotIn("Stop", calls[0][0])

    def test_absent_instance_is_started_and_verified(self):
        launcher, calls = self.make_launcher(
            [
                completed([], stdout=status_json()),
                completed([], stdout='{"running":true,"pid":52}'),
                completed([], stdout=status_json(running=True, pid=52)),
            ],
            [[], [typist_process(pid=52)]],
        )
        result = launcher.ensure_running()
        self.assertEqual(result["result"], "started")
        self.assertEqual([call[0][-1] for call in calls], ["Status", "Start", "Status"])
        self.assertTrue(all("-ExecutionPolicy" in call[0] for call in calls))
        self.assertTrue(all(str(LAUNCHER) in call[0] for call in calls))

    def test_already_running_race_needs_postcondition_not_error_text(self):
        launcher, _ = self.make_launcher(
            [
                completed([], stdout=status_json()),
                completed([], returncode=1, stderr="Typist already running"),
                completed([], stdout=status_json(running=True, pid=63)),
            ],
            [[], [typist_process(pid=63)]],
        )
        result = launcher.ensure_running()
        self.assertEqual(result["result"], "raced-running")

    def test_paused_and_estop_are_not_cleared_automatically(self):
        for status, expected in (
            (status_json(paused=True), "BW_COMPUTER_VOICE_TYPIST_PAUSED"),
            (status_json(emergency=True), "BW_COMPUTER_VOICE_TYPIST_ESTOP"),
        ):
            with self.subTest(expected=expected):
                launcher, calls = self.make_launcher(
                    [completed([], stdout=status)],
                    [[]],
                )
                with self.assertRaises(SupervisorError) as caught:
                    launcher.ensure_running()
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual([call[0][-1] for call in calls], ["Status"])

    def test_orphan_mismatch_and_multiple_processes_fail_closed(self):
        cases = (
            (
                status_json(),
                [typist_process(pid=70)],
                "BW_COMPUTER_VOICE_TYPIST_ORPHAN",
            ),
            (
                status_json(running=True, pid=70),
                [typist_process(pid=71)],
                "BW_COMPUTER_VOICE_TYPIST_PID_MISMATCH",
            ),
            (
                status_json(running=True, pid=70),
                [typist_process(pid=70), typist_process(pid=71)],
                "BW_COMPUTER_VOICE_TYPIST_AMBIGUOUS",
            ),
        )
        for status, processes, expected in cases:
            with self.subTest(expected=expected):
                launcher, _ = self.make_launcher(
                    [completed([], stdout=status)],
                    [processes],
                )
                with self.assertRaises(SupervisorError) as caught:
                    launcher.verified_status()
                self.assertEqual(caught.exception.code, expected)

    def test_session_zero_is_rejected_before_process_control(self):
        launcher = VoiceTypistLauncher(
            launcher_path=LAUNCHER,
            typist_script=SCRIPT,
            runner=lambda argv, timeout: completed(
                argv,
                stdout=status_json(),
            ),
            process_probe=lambda: [],
            session_id_provider=lambda: 0,
        )
        with self.assertRaises(SupervisorError) as caught:
            launcher.verified_status()
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_NON_INTERACTIVE")

    def test_destructive_typist_actions_are_not_available(self):
        calls = []
        launcher = VoiceTypistLauncher(
            launcher_path=LAUNCHER,
            typist_script=SCRIPT,
            runner=lambda argv, timeout: calls.append(tuple(argv))
            or completed(argv),
            process_probe=lambda: [],
            session_id_provider=lambda: 1,
        )
        with self.assertRaises(SupervisorError) as caught:
            launcher._invoke("Stop", timeout=1.0)
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_ACTION_DENIED")
        self.assertEqual(calls, [])


class FakeCapture:
    def __init__(self, *, valid=True, owned=True, rtc_connected=None):
        self.valid = valid
        self.owned = owned
        self.rtc_connected = (
            valid if rtc_connected is None else bool(rtc_connected)
        )
        self.started = []
        self.rollbacks = 0

    def ensure_started(self, root_process_id):
        self.started.append(root_process_id)
        return {
            "active": self.valid,
            "microphoneActive": self.valid,
            "scope": "process-only",
            "rootProcessId": root_process_id,
            "outputTarget": "chatgpt-desktop",
            "nativeHostReady": self.valid,
            "mediaHostReady": self.valid,
            "rtcConnected": self.rtc_connected,
            "owned": self.owned,
        }

    def rollback_if_owned(self):
        self.rollbacks += 1


class FakeTypist:
    def __init__(self, *, running=True):
        self.running = running
        self.calls = 0

    def ensure_running(self):
        self.calls += 1
        if not self.running:
            raise SupervisorError(
                "BW_COMPUTER_VOICE_TYPIST_START_FAILED",
                "not running",
            )
        return {"running": True, "result": "already-running"}


class CombinedVoiceStartCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.receipts = CommandReceiptStore(
            Path(self.temp.name) / "command-receipts.json"
        )
        self.now = 1_800_000_000.0

    def command(self, **overrides):
        value = {
            "contract": BRIDGE_COMMAND_CONTRACT,
            "commandId": "start-command-00000001",
            "nonce": "server-only-nonce",
            "action": START_ACTION,
            "expiresAt": self.now + 10,
        }
        value.update(overrides)
        return value

    def coordinator(self, *, capture=None, typist=None, shortcut=None, app=None):
        return CombinedVoiceStartCoordinator(
            app_probe=lambda: app
            or {
                "ready": True,
                "rootProcessId": 31180,
                "appKind": "chatgpt-desktop",
            },
            capture=capture or FakeCapture(),
            typist=typist or FakeTypist(),
            shortcut_sender=shortcut or (lambda: True),
            receipts=self.receipts,
            clock=lambda: self.now,
        )

    def test_combined_start_and_redelivery_send_shortcut_once(self):
        calls = []
        coordinator = self.coordinator(shortcut=lambda: calls.append("send") or True)
        first = coordinator.execute(self.command())
        second = coordinator.execute(self.command())
        self.assertEqual(first["result"], "started")
        self.assertTrue(first["captureActive"])
        self.assertTrue(first["rtcConnected"])
        self.assertEqual(first["typist"]["result"], "already-running")
        self.assertEqual(second["result"], "duplicate-suppressed")
        self.assertFalse(second["shortcutSent"])
        self.assertEqual(calls, ["send"])

        saved = json.loads(
            (Path(self.temp.name) / "command-receipts.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            saved["commands"]["start-command-00000001"]["state"],
            "started",
        )
        self.assertNotIn("nonce", repr(saved).lower())

    def test_typist_failure_rolls_back_owned_capture_and_sends_no_shortcut(self):
        capture = FakeCapture(owned=True)
        sent = []
        coordinator = self.coordinator(
            capture=capture,
            typist=FakeTypist(running=False),
            shortcut=lambda: sent.append(True) or True,
        )
        with self.assertRaises(SupervisorError) as caught:
            coordinator.execute(self.command())
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_TYPIST_START_FAILED",
        )
        self.assertEqual(capture.rollbacks, 1)
        self.assertEqual(sent, [])

    def test_rtc_must_be_connected_before_typist_or_shortcut(self):
        capture = FakeCapture(owned=True, rtc_connected=False)
        typist = FakeTypist()
        sent = []
        coordinator = self.coordinator(
            capture=capture,
            typist=typist,
            shortcut=lambda: sent.append(True) or True,
        )
        with self.assertRaises(SupervisorError) as caught:
            coordinator.execute(self.command())
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_CAPTURE_NOT_READY",
        )
        self.assertEqual(capture.rollbacks, 1)
        self.assertEqual(typist.calls, 0)
        self.assertEqual(sent, [])

    def test_shortcut_failure_is_durable_and_never_retried(self):
        capture = FakeCapture(owned=True)
        sent = []
        coordinator = self.coordinator(
            capture=capture,
            shortcut=lambda: sent.append(True) or False,
        )
        with self.assertRaises(SupervisorError) as caught:
            coordinator.execute(self.command())
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_SHORTCUT_FAILED")
        self.assertEqual(capture.rollbacks, 1)
        replay = coordinator.execute(self.command())
        self.assertEqual(replay["result"], "duplicate-suppressed")
        self.assertEqual(sent, [True])

    def test_expired_or_not_ready_command_has_zero_local_side_effects(self):
        capture = FakeCapture()
        typist = FakeTypist()
        sent = []
        coordinator = self.coordinator(
            capture=capture,
            typist=typist,
            shortcut=lambda: sent.append(True) or True,
        )
        with self.assertRaises(SupervisorError) as expired:
            coordinator.execute(self.command(expiresAt=self.now))
        self.assertEqual(expired.exception.code, "BW_COMPUTER_VOICE_COMMAND_EXPIRED")
        self.assertEqual(capture.started, [])
        self.assertEqual(typist.calls, 0)
        self.assertEqual(sent, [])

        coordinator = self.coordinator(
            capture=capture,
            typist=typist,
            shortcut=lambda: sent.append(True) or True,
            app={"ready": False, "reason": "no-visible-window"},
        )
        with self.assertRaises(SupervisorError) as not_ready:
            coordinator.execute(self.command(commandId="start-command-00000002"))
        self.assertEqual(
            not_ready.exception.code,
            "BW_COMPUTER_VOICE_APP_NOT_READY",
        )
        self.assertEqual(capture.started, [])

    def test_invalid_receipt_file_fails_closed(self):
        self.receipts.path.write_text("not-json", encoding="utf-8")
        coordinator = self.coordinator()
        with self.assertRaises(SupervisorError) as caught:
            coordinator.execute(self.command())
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_RECEIPT_INVALID")

    def test_server_cannot_override_local_launcher_or_shortcut(self):
        capture = FakeCapture()
        sent = []
        coordinator = self.coordinator(
            capture=capture,
            shortcut=lambda: sent.append(True) or True,
        )
        command = self.command()
        command["launcherPath"] = r"C:\untrusted.ps1"
        with self.assertRaises(SupervisorError) as caught:
            coordinator.execute(command)
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_COMMAND_INVALID")
        self.assertEqual(capture.started, [])
        self.assertEqual(sent, [])


class LocalBridgeConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "computer-voice.config.json"

    def write(self, **overrides):
        value = {
            "contract": "reader-computer-voice-local-config/1",
            "localOptIn": True,
            "voiceStartShortcut": "Ctrl+Shift+C",
            "appKind": "chatgpt-desktop",
            "outputScope": "process-only",
            "companionLauncher": str(LAUNCHER),
        }
        value.update(overrides)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_user_shortcut_and_process_only_scope_load(self):
        self.write()
        config = LocalBridgeConfig.load(self.path)
        self.assertTrue(config.local_opt_in)
        self.assertEqual(config.voice_start_shortcut, ("ctrl", "shift", "c"))
        self.assertEqual(config.app_kind, "chatgpt-desktop")
        self.assertEqual(config.companion_launcher, LAUNCHER)

    def test_shipped_example_records_shortcut_but_keeps_opt_in_off(self):
        example = (
            ROOT
            / "extensions"
            / "bw-reader-webext"
            / "windows"
            / "computer-voice.config.example.json"
        )
        config = LocalBridgeConfig.load(example)
        self.assertFalse(config.local_opt_in)
        self.assertEqual(config.voice_start_shortcut, ("ctrl", "shift", "c"))
        self.assertEqual(config.app_kind, "chatgpt-desktop")
        self.assertEqual(config.companion_launcher, LAUNCHER)

    def test_system_output_single_key_and_remote_path_fail_closed(self):
        cases = (
            (
                {"outputScope": "system-wide"},
                "BW_COMPUTER_VOICE_PROCESS_OUTPUT_REQUIRED",
            ),
            (
                {"voiceStartShortcut": "c"},
                "BW_COMPUTER_VOICE_CONFIG_INVALID",
            ),
            (
                {"companionLauncher": r"C:\untrusted.ps1"},
                "BW_COMPUTER_VOICE_CONFIG_INVALID",
            ),
        )
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                self.write(**overrides)
                with self.assertRaises(SupervisorError) as caught:
                    LocalBridgeConfig.load(self.path)
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
