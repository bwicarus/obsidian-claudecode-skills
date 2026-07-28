from __future__ import annotations

from pathlib import Path
from io import BytesIO
import json
import sys
import tempfile
import unittest

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from computer_voice_pairing import (  # noqa: E402
    PAIRING_CONTRACT,
    SIGNAL_CONTRACT,
)
from computer_voice_bridge import CONTRACT as BRIDGE_CONTRACT  # noqa: E402
from computer_voice_routes import register_computer_voice  # noqa: E402


DEVICE_ID = "windows-codex"
DEVICE_TOKEN = "device-token-" + "a" * 48


class ComputerVoiceRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.identity = None
        self.app = Flask(__name__)
        self.app.secret_key = "test-secret-key-with-enough-entropy"
        self.app.config["COMPUTER_VOICE_REQUIRE_TLS"] = False
        self.app.extensions["reader_storage_identity_resolver"] = (
            lambda: self.identity
        )
        register_computer_voice(
            self.app,
            root=self.temp.name,
            pepper=b"r" * 32,
        )
        self.client = self.app.test_client()

    def pairing_body(self, **extra):
        return {"contract": PAIRING_CONTRACT, **extra}

    def signal_body(self, **extra):
        return {"contract": SIGNAL_CONTRACT, **extra}

    def bridge_body(self, **extra):
        return {"contract": BRIDGE_CONTRACT, **extra}

    @staticmethod
    def device_headers():
        return {
            "Authorization": f"BWComputerVoice {DEVICE_TOKEN}",
            "X-BW-Computer-Voice-Device-Id": DEVICE_ID,
        }

    @staticmethod
    def heartbeat(*, active=False, rtc=False):
        return {
            "app": {"kind": "codex-desktop", "ready": True},
            "voiceStart": {
                "localOptIn": True,
                "shortcutConfigured": True,
            },
            "capture": {
                "microphoneAvailable": True,
                "outputScope": "process-only",
                "outputTarget": "codex-desktop",
                "active": active,
            },
            "media": {
                "nativeHostReady": True,
                "mediaHostReady": True,
                "rtcConnected": rtc,
            },
            "companion": {
                "kind": "voice-typist",
                "launcherAvailable": True,
                "running": active and rtc,
            },
            "bridgeVersion": "0.1.0-test",
        }

    @staticmethod
    def offer(signal_id="offer-1"):
        return {
            "signalId": signal_id,
            "kind": "offer",
            "payload": {
                "type": "offer",
                "sdp": (
                    "v=0\r\n"
                    "o=- 1 1 IN IP4 127.0.0.1\r\n"
                    "s=-\r\n"
                    "t=0 0\r\n"
                    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                    "a=mid:0\r\n"
                    "a=sendrecv\r\n"
                    "a=rtcp-mux\r\n"
                    "a=rtpmap:111 opus/48000/2\r\n"
                ),
            },
        }

    @staticmethod
    def answer(signal_id="answer-1"):
        value = ComputerVoiceRoutesTest.offer(signal_id)
        value["kind"] = "answer"
        value["payload"]["type"] = "answer"
        return value

    def pair(self):
        self.identity = {
            "user_id": 1,
            "storage_namespace": "acct-v1-" + "a" * 64,
        }
        started = self.client.post(
            "/api/reader/computer-voice/pairings",
            json=self.pairing_body(),
        )
        self.assertEqual(started.status_code, 201)
        pairing = started.get_json()
        consumed = self.client.post(
            "/api/reader/computer-voice/pairings/consume",
            json=self.pairing_body(
                pairId=pairing["pairId"],
                pairingCode=pairing["pairingCode"],
                deviceId=DEVICE_ID,
                deviceToken=DEVICE_TOKEN,
            ),
        )
        self.assertEqual(consumed.status_code, 200)
        self.assertNotIn("deviceToken", consumed.get_json())
        return pairing

    def test_pairing_requires_reader_identity_but_consume_uses_one_time_code(self):
        denied = self.client.post(
            "/api/reader/computer-voice/pairings",
            json=self.pairing_body(),
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["contract"], PAIRING_CONTRACT)
        self.assertEqual(
            denied.get_json()["code"],
            "BW_COMPUTER_VOICE_PAIRING_AUTH",
        )
        pairing = self.pair()
        replayed = self.client.post(
            "/api/reader/computer-voice/pairings/consume",
            json=self.pairing_body(
                pairId=pairing["pairId"],
                pairingCode=pairing["pairingCode"],
                deviceId=DEVICE_ID,
                deviceToken=DEVICE_TOKEN,
            ),
        )
        self.assertTrue(replayed.get_json()["replayed"])

        devices = self.client.get(
            "/api/reader/computer-voice/devices"
        )
        self.assertEqual(
            devices.get_json()["devices"][0]["deviceId"],
            DEVICE_ID,
        )
        self.assertEqual(devices.headers["Cache-Control"], (
            "no-store, private, max-age=0"
        ))
        revoked = self.client.delete(
            f"/api/reader/computer-voice/devices/{DEVICE_ID}",
            json=self.pairing_body(),
        )
        self.assertEqual(revoked.get_json()["state"], "revoked")
        listed = self.client.get(
            "/api/reader/computer-voice/devices"
        ).get_json()["devices"]
        self.assertEqual(listed[0]["state"], "revoked")
        self.assertIn("revokedAt", listed[0])
        forgotten = self.client.delete(
            f"/api/reader/computer-voice/devices/{DEVICE_ID}/record",
            json=self.pairing_body(),
        )
        self.assertEqual(forgotten.get_json()["state"], "forgotten")
        self.assertEqual(
            self.client.get(
                "/api/reader/computer-voice/devices"
            ).get_json()["devices"],
            [],
        )

    def test_authenticated_reader_and_device_exchange_audio_only_signalling(self):
        self.pair()
        opened = self.client.post(
            "/api/reader/computer-voice/sessions",
            json=self.signal_body(deviceId=DEVICE_ID),
        )
        self.assertEqual(opened.status_code, 201)
        session_id = opened.get_json()["sessionId"]
        reader = self.client.post(
            f"/api/reader/computer-voice/sessions/{session_id}/signals",
            json=self.signal_body(signals=[self.offer()], cursor=0),
        )
        self.assertEqual(reader.status_code, 200)

        device_headers = {
            "Authorization": f"BWComputerVoice {DEVICE_TOKEN}",
            "X-BW-Computer-Voice-Device-Id": DEVICE_ID,
        }
        device = self.client.post(
            (
                "/api/reader/computer-voice/device/sessions/"
                f"{session_id}/signals"
            ),
            headers=device_headers,
            json=self.signal_body(signals=[], cursor=0),
        )
        self.assertEqual(device.status_code, 200)
        self.assertEqual(device.get_json()["signals"][0]["kind"], "offer")
        cursor = device.get_json()["cursor"]
        answered = self.client.post(
            (
                "/api/reader/computer-voice/device/sessions/"
                f"{session_id}/signals"
            ),
            headers=device_headers,
            json=self.signal_body(
                signals=[self.answer()],
                cursor=cursor,
            ),
        )
        self.assertEqual(answered.status_code, 200)
        received = self.client.post(
            f"/api/reader/computer-voice/sessions/{session_id}/signals",
            json=self.signal_body(
                signals=[],
                cursor=reader.get_json()["cursor"],
            ),
        )
        self.assertEqual(received.get_json()["signals"][0]["kind"], "answer")

        wrong = self.client.post(
            (
                "/api/reader/computer-voice/device/sessions/"
                f"{session_id}/signals"
            ),
            headers={
                **device_headers,
                "Authorization": (
                    "BWComputerVoice device-token-" + "b" * 48
                ),
            },
            json=self.signal_body(signals=[], cursor=0),
        )
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.get_json()["contract"], SIGNAL_CONTRACT)
        self.assertEqual(
            wrong.get_json()["code"],
            "BW_COMPUTER_VOICE_DEVICE_AUTH",
        )

    def test_routes_reject_extra_fields_and_non_audio_sdp(self):
        self.pair()
        bad = self.client.post(
            "/api/reader/computer-voice/sessions",
            json=self.signal_body(
                deviceId=DEVICE_ID,
                audio="AAAA",
            ),
        )
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.get_json()["contract"], SIGNAL_CONTRACT)
        opened = self.client.post(
            "/api/reader/computer-voice/sessions",
            json=self.signal_body(deviceId=DEVICE_ID),
        ).get_json()
        smuggled = self.offer("x-pcm")
        smuggled["payload"]["sdp"] += "a=x-pcm:" + "A" * 400 + "\r\n"
        denied = self.client.post(
            (
                "/api/reader/computer-voice/sessions/"
                f"{opened['sessionId']}/signals"
            ),
            json=self.signal_body(signals=[smuggled], cursor=0),
        )
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(
            denied.get_json()["code"],
            "BW_COMPUTER_VOICE_SIGNAL_MEDIA_SCOPE",
        )
        self.assertEqual(denied.get_json()["contract"], SIGNAL_CONTRACT)

    def test_unknown_length_body_is_bounded_before_json_decode(self):
        payload = json.dumps({
            "contract": PAIRING_CONTRACT,
            "padding": "A" * (90 * 1024),
        }).encode("utf-8")
        response = self.client.open(
            "/api/reader/computer-voice/pairings/consume",
            method="POST",
            input_stream=BytesIO(payload),
            content_type="application/json",
            environ_overrides={
                "CONTENT_LENGTH": "",
                "wsgi.input_terminated": True,
            },
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.get_json()["code"],
            "BW_COMPUTER_VOICE_PAIRING_TOO_LARGE",
        )
        self.assertEqual(
            response.get_json()["contract"],
            PAIRING_CONTRACT,
        )

    def test_plain_http_fails_closed_when_tls_gate_is_enabled(self):
        self.app.config["COMPUTER_VOICE_REQUIRE_TLS"] = True
        response = self.client.post(
            "/api/reader/computer-voice/pairings",
            json=self.pairing_body(),
            base_url="http://reader.invalid",
        )
        self.assertEqual(response.status_code, 426)
        self.assertEqual(
            response.get_json()["code"],
            "BW_COMPUTER_VOICE_TRANSPORT_SECURITY",
        )
        self.assertEqual(
            response.get_json()["contract"],
            PAIRING_CONTRACT,
        )

    def test_reader_start_device_claim_and_ack_are_one_shot(self):
        self.pair()
        heartbeat = self.client.post(
            "/api/reader/computer-voice/device/heartbeat",
            json=self.bridge_body(heartbeat=self.heartbeat()),
            headers=self.device_headers(),
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(heartbeat.get_json()["state"], "ready")

        status = self.client.get(
            f"/api/reader/computer-voice/devices/{DEVICE_ID}/status"
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["state"], "ready")

        started = self.client.post(
            f"/api/reader/computer-voice/devices/{DEVICE_ID}/start",
            json=self.bridge_body(),
        )
        self.assertEqual(started.status_code, 202)
        payload = started.get_json()
        self.assertEqual(payload["contract"], BRIDGE_CONTRACT)
        self.assertEqual(payload["session"]["contract"], SIGNAL_CONTRACT)
        self.assertNotIn("nonce", payload["command"])

        claimed = self.client.post(
            "/api/reader/computer-voice/device/commands/claim",
            json=self.bridge_body(),
            headers=self.device_headers(),
        )
        self.assertEqual(claimed.status_code, 200)
        claimed_payload = claimed.get_json()
        self.assertEqual(
            claimed_payload["sessionId"],
            payload["session"]["sessionId"],
        )
        self.assertEqual(
            claimed_payload["command"]["commandId"],
            payload["command"]["commandId"],
        )
        self.assertIn("nonce", claimed_payload["command"])

        active = self.client.post(
            "/api/reader/computer-voice/device/heartbeat",
            json=self.bridge_body(
                heartbeat=self.heartbeat(active=True, rtc=True)
            ),
            headers=self.device_headers(),
        )
        self.assertTrue(active.get_json()["captureActive"])

        acknowledged = self.client.post(
            "/api/reader/computer-voice/device/commands/"
            f"{claimed_payload['command']['commandId']}/ack",
            json=self.bridge_body(
                nonce=claimed_payload["command"]["nonce"],
                result="started",
            ),
            headers=self.device_headers(),
        )
        self.assertEqual(acknowledged.status_code, 200)
        self.assertEqual(
            acknowledged.get_json()["state"],
            "acknowledged",
        )

        empty = self.client.post(
            "/api/reader/computer-voice/device/commands/claim",
            json=self.bridge_body(),
            headers=self.device_headers(),
        )
        self.assertIsNone(empty.get_json()["command"])

    def test_start_fails_closed_until_fresh_device_readiness(self):
        self.pair()
        denied = self.client.post(
            f"/api/reader/computer-voice/devices/{DEVICE_ID}/start",
            json=self.bridge_body(),
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(
            denied.get_json()["code"],
            "BW_COMPUTER_VOICE_NOT_READY",
        )

        wrong_scope = self.heartbeat()
        wrong_scope["capture"]["outputScope"] = "system-wide"
        rejected = self.client.post(
            "/api/reader/computer-voice/device/heartbeat",
            json=self.bridge_body(heartbeat=wrong_scope),
            headers=self.device_headers(),
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(
            rejected.get_json()["code"],
            "BW_COMPUTER_VOICE_PROCESS_OUTPUT_REQUIRED",
        )


if __name__ == "__main__":
    unittest.main()
