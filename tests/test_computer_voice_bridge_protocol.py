from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from computer_voice_bridge import (  # noqa: E402
    CONTRACT,
    ComputerVoiceBridgeError,
    ComputerVoiceBridgeRegistry,
    READY_LEASE_SECONDS,
    START_COMMAND_TTL_SECONDS,
)


class ComputerVoiceBridgeProtocolTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_780_000_000.0
        self.serial = 0

        def next_token():
            self.serial += 1
            return f"test-token-{self.serial:024d}"

        self.registry = ComputerVoiceBridgeRegistry(
            clock=lambda: self.now,
            token_factory=next_token,
        )
        self.registry.provision_device("account-a", "windows-codex")

    @staticmethod
    def ready_report(**overrides):
        value = {
            "app": {"kind": "chatgpt-desktop", "ready": True},
            "voiceStart": {"localOptIn": True, "shortcutConfigured": True},
            "capture": {
                "microphoneAvailable": True,
                "outputScope": "process-only",
                "outputTarget": "chatgpt-desktop",
                "active": False,
            },
            "media": {
                "nativeHostReady": True,
                "mediaHostReady": True,
                "rtcConnected": False,
            },
            "companion": {
                "kind": "voice-typist",
                "launcherAvailable": True,
                "running": False,
            },
            "bridgeVersion": "0.1.0-test",
        }
        for key, nested in overrides.items():
            value[key].update(nested)
        return value

    def heartbeat(self, **overrides):
        return self.registry.report_heartbeat(
            "account-a",
            "windows-codex",
            self.ready_report(**overrides),
        )

    def test_ready_heartbeat_exposes_only_safe_browser_status(self):
        status = self.heartbeat()
        self.assertEqual(status["contract"], CONTRACT)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["appKind"], "chatgpt-desktop")
        self.assertFalse(status["captureActive"])
        self.assertTrue(status["media"]["hostReady"])
        self.assertFalse(status["media"]["rtcConnected"])
        self.assertEqual(status["companion"]["kind"], "voice-typist")
        self.assertTrue(status["companion"]["launcherAvailable"])
        self.assertFalse(status["companion"]["running"])
        self.assertIsNone(status["start"])
        flattened = repr(status).lower()
        self.assertNotIn("shortcut", flattened)
        self.assertNotIn("nonce", flattened)
        self.assertNotIn("outputtarget", flattened)

    def test_phone_button_command_is_one_shot_and_browser_never_gets_nonce(self):
        self.heartbeat()
        first = self.registry.request_voice_start("account-a", "windows-codex")
        duplicate = self.registry.request_voice_start("account-a", "windows-codex")
        self.assertEqual(first["result"], "queued")
        self.assertEqual(duplicate["result"], "already-pending")
        self.assertEqual(first["commandId"], duplicate["commandId"])
        self.assertNotIn("nonce", first)

        command = self.registry.claim_voice_start("account-a", "windows-codex")
        self.assertEqual(command["action"], "start-computer-voice")
        self.assertIn("nonce", command)
        self.assertNotIn("shortcut", command)
        self.assertEqual(
            self.registry.claim_voice_start("account-a", "windows-codex"),
            command,
        )

        self.heartbeat(
            capture={
                "microphoneAvailable": True,
                "outputScope": "process-only",
                "outputTarget": "chatgpt-desktop",
                "active": True,
            },
            media={
                "nativeHostReady": True,
                "mediaHostReady": True,
                "rtcConnected": True,
            },
            companion={
                "kind": "voice-typist",
                "launcherAvailable": True,
                "running": True,
            },
        )
        ack = self.registry.acknowledge_voice_start(
            "account-a",
            "windows-codex",
            command["commandId"],
            command["nonce"],
            "started",
        )
        self.assertEqual(ack["state"], "acknowledged")
        self.assertEqual(ack["result"], "acknowledged")
        self.assertEqual(
            self.registry.browser_status("account-a", "windows-codex")["start"]["result"],
            "started",
        )

    def test_not_ready_never_queues_or_claims_a_shortcut(self):
        self.heartbeat(app={"kind": "chatgpt-desktop", "ready": False})
        status = self.registry.browser_status("account-a", "windows-codex")
        self.assertEqual(status["state"], "online-not-ready")
        self.assertEqual(status["reason"], "app-not-ready")
        with self.assertRaisesRegex(ComputerVoiceBridgeError, "未就绪") as caught:
            self.registry.request_voice_start("account-a", "windows-codex")
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_NOT_READY")
        self.assertIsNone(self.registry.claim_voice_start("account-a", "windows-codex"))

    def test_opt_in_shortcut_mic_and_process_only_capture_are_all_required(self):
        cases = (
            (
                {"voiceStart": {"localOptIn": False, "shortcutConfigured": True}},
                "local-opt-in-required",
            ),
            (
                {"voiceStart": {"localOptIn": True, "shortcutConfigured": False}},
                "shortcut-not-configured",
            ),
            (
                {"capture": {
                    "microphoneAvailable": False,
                    "outputScope": "process-only",
                    "outputTarget": "chatgpt-desktop",
                    "active": False,
                }},
                "microphone-unavailable",
            ),
            (
                {"companion": {
                    "kind": "voice-typist",
                    "launcherAvailable": False,
                    "running": False,
                }},
                "voice-typist-launcher-unavailable",
            ),
            (
                {"media": {
                    "nativeHostReady": False,
                    "mediaHostReady": True,
                    "rtcConnected": False,
                }},
                "native-host-unavailable",
            ),
            (
                {"media": {
                    "nativeHostReady": True,
                    "mediaHostReady": False,
                    "rtcConnected": False,
                }},
                "media-host-unavailable",
            ),
        )
        for updates, reason in cases:
            with self.subTest(reason=reason):
                status = self.heartbeat(**updates)
                self.assertEqual(status["state"], "online-not-ready")
                self.assertEqual(status["reason"], reason)

        with self.assertRaises(ComputerVoiceBridgeError) as caught:
            self.heartbeat(capture={
                "microphoneAvailable": True,
                "outputScope": "system-wide",
                "outputTarget": "chatgpt-desktop",
                "active": False,
            })
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_PROCESS_OUTPUT_REQUIRED",
        )

    def test_started_ack_requires_capture_rtc_and_typist_postcondition(self):
        self.heartbeat()
        queued = self.registry.request_voice_start("account-a", "windows-codex")
        command = self.registry.claim_voice_start("account-a", "windows-codex")
        self.assertEqual(command["commandId"], queued["commandId"])

        with self.assertRaises(ComputerVoiceBridgeError) as incomplete:
            self.registry.acknowledge_voice_start(
                "account-a",
                "windows-codex",
                command["commandId"],
                command["nonce"],
                "started",
            )
        self.assertEqual(
            incomplete.exception.code,
            "BW_COMPUTER_VOICE_START_INCOMPLETE",
        )

        self.heartbeat(
            capture={
                "microphoneAvailable": True,
                "outputScope": "process-only",
                "outputTarget": "chatgpt-desktop",
                "active": True,
            },
            companion={
                "kind": "voice-typist",
                "launcherAvailable": True,
                "running": False,
            },
        )
        with self.assertRaises(ComputerVoiceBridgeError) as typist_missing:
            self.registry.acknowledge_voice_start(
                "account-a",
                "windows-codex",
                command["commandId"],
                command["nonce"],
                "started",
            )
        self.assertEqual(
            typist_missing.exception.code,
            "BW_COMPUTER_VOICE_START_INCOMPLETE",
        )

        self.heartbeat(
            capture={
                "microphoneAvailable": True,
                "outputScope": "process-only",
                "outputTarget": "chatgpt-desktop",
                "active": True,
            },
            companion={
                "kind": "voice-typist",
                "launcherAvailable": True,
                "running": True,
            },
        )
        with self.assertRaises(ComputerVoiceBridgeError) as rtc_missing:
            self.registry.acknowledge_voice_start(
                "account-a",
                "windows-codex",
                command["commandId"],
                command["nonce"],
                "started",
            )
        self.assertEqual(
            rtc_missing.exception.code,
            "BW_COMPUTER_VOICE_START_INCOMPLETE",
        )

        self.heartbeat(
            capture={
                "microphoneAvailable": True,
                "outputScope": "process-only",
                "outputTarget": "chatgpt-desktop",
                "active": True,
            },
            media={
                "nativeHostReady": True,
                "mediaHostReady": True,
                "rtcConnected": True,
            },
            companion={
                "kind": "voice-typist",
                "launcherAvailable": True,
                "running": True,
            },
        )
        ack = self.registry.acknowledge_voice_start(
            "account-a",
            "windows-codex",
            command["commandId"],
            command["nonce"],
            "started",
        )
        self.assertEqual(ack["state"], "acknowledged")

    def test_expired_heartbeat_and_command_fail_closed(self):
        self.heartbeat()
        queued = self.registry.request_voice_start("account-a", "windows-codex")
        self.now += READY_LEASE_SECONDS + 0.1
        status = self.registry.browser_status("account-a", "windows-codex")
        self.assertEqual(status["state"], "offline")
        self.assertIsNone(self.registry.claim_voice_start("account-a", "windows-codex"))
        with self.assertRaises(ComputerVoiceBridgeError) as caught:
            self.registry.request_voice_start("account-a", "windows-codex")
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_NOT_READY")

        self.heartbeat()
        queued = self.registry.request_voice_start("account-a", "windows-codex")
        command = self.registry.claim_voice_start("account-a", "windows-codex")
        self.assertEqual(command["commandId"], queued["commandId"])
        self.now += START_COMMAND_TTL_SECONDS + 0.1
        with self.assertRaises(ComputerVoiceBridgeError) as caught:
            self.registry.acknowledge_voice_start(
                "account-a", "windows-codex", command["commandId"], command["nonce"], "started"
            )
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_COMMAND_EXPIRED")

    def test_account_and_nonce_mismatch_cannot_observe_or_confirm_command(self):
        self.registry.provision_device("account-b", "windows-other")
        self.heartbeat()
        queued = self.registry.request_voice_start("account-a", "windows-codex")
        with self.assertRaises(ComputerVoiceBridgeError) as foreign:
            self.registry.browser_status("account-b", "windows-codex")
        self.assertEqual(foreign.exception.code, "BW_COMPUTER_VOICE_DEVICE_UNAVAILABLE")
        with self.assertRaises(ComputerVoiceBridgeError) as bad_nonce:
            self.registry.acknowledge_voice_start(
                "account-a", "windows-codex", queued["commandId"], "wrong", "started"
            )
        self.assertEqual(bad_nonce.exception.code, "BW_COMPUTER_VOICE_COMMAND_AUTH")


if __name__ == "__main__":
    unittest.main()
