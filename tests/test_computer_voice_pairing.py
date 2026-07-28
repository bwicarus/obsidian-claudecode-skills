from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from computer_voice_pairing import (  # noqa: E402
    ComputerVoicePairingError,
    ComputerVoicePairingStore,
    ComputerVoiceSignalBroker,
    MAX_PENDING_PAIRINGS_PER_ACCOUNT,
    MAX_ACTIVE_DEVICES_PER_ACCOUNT,
    MAX_SIGNAL_BYTES_PER_SESSION,
    MAX_SIGNAL_MESSAGES_PER_SESSION,
    MAX_SIGNAL_SESSIONS_PER_DEVICE,
    PAIRING_MAX_ATTEMPTS,
    PAIRING_REPLAY_TTL_SECONDS,
    PAIRING_TTL_SECONDS,
    SIGNAL_TTL_SECONDS,
)


TOKEN_A = "device-token-" + "a" * 48
TOKEN_B = "device-token-" + "b" * 48
CODE_A = "ABCDEFGH2345"
CODE_B = "JKLMNPQR6789"
CHROMIUM_AUDIO_SDP_SHAPE = (
    "v=0\r\n"
    "o=- 4611733050159179922 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0\r\n"
    "a=extmap-allow-mixed\r\n"
    "a=msid-semantic: WMS 123e4567-e89b-12d3-a456-426614174000\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 102 0 8 13 110 126\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:abcd\r\n"
    "a=ice-pwd:abcdefghijklmnopqrstuvwx\r\n"
    "a=ice-options:trickle\r\n"
    "a=fingerprint:sha-256 "
    + ":".join(["AA"] * 32)
    + "\r\n"
    "a=setup:actpass\r\n"
    "a=mid:0\r\n"
    "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n"
    "a=sendrecv\r\n"
    "a=msid:123e4567-e89b-12d3-a456-426614174000 abcdef\r\n"
    "a=rtcp-mux\r\n"
    "a=rtcp-rsize\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtcp-fb:111 transport-cc\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
    "a=ssrc:123456789 cname:abcdefgh\r\n"
)
# Captured from the dedicated Windows `BW Codex Chrome Test` profile with
# Chrome/150.0.7871.187 on 2026-07-28.  The probe used two recvonly audio
# transceivers and never called getUserMedia.  Origin, ICE and fingerprint
# values are deterministic redactions; the line/codec shape is unchanged.
WINDOWS_CHROME_150_TWO_AUDIO_OFFER = (
    "v=0\r\n"
    "o=- 1 2 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1\r\n"
    "a=extmap-allow-mixed\r\n"
    "a=msid-semantic: WMS\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:Test\r\n"
    "a=ice-pwd:abcdefghijklmnopqrstuvwx\r\n"
    "a=ice-options:trickle\r\n"
    "a=fingerprint:sha-256 "
    + ":".join(["AA"] * 32)
    + "\r\n"
    "a=setup:actpass\r\n"
    "a=mid:0\r\n"
    "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n"
    "a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time\r\n"
    "a=extmap:3 http://www.ietf.org/id/"
    "draft-holmer-rmcat-transport-wide-cc-extensions-01\r\n"
    "a=extmap:4 urn:ietf:params:rtp-hdrext:sdes:mid\r\n"
    "a=recvonly\r\n"
    "a=rtcp-mux\r\n"
    "a=rtcp-rsize\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtcp-fb:111 transport-cc\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
    "a=rtpmap:63 red/48000/2\r\n"
    "a=fmtp:63 111/111\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:13 CN/8000\r\n"
    "a=rtpmap:110 telephone-event/48000\r\n"
    "a=rtpmap:126 telephone-event/8000\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:Test\r\n"
    "a=ice-pwd:abcdefghijklmnopqrstuvwx\r\n"
    "a=ice-options:trickle\r\n"
    "a=fingerprint:sha-256 "
    + ":".join(["AA"] * 32)
    + "\r\n"
    "a=setup:actpass\r\n"
    "a=mid:1\r\n"
    "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n"
    "a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time\r\n"
    "a=extmap:3 http://www.ietf.org/id/"
    "draft-holmer-rmcat-transport-wide-cc-extensions-01\r\n"
    "a=extmap:4 urn:ietf:params:rtp-hdrext:sdes:mid\r\n"
    "a=recvonly\r\n"
    "a=rtcp-mux\r\n"
    "a=rtcp-rsize\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=rtcp-fb:111 transport-cc\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
    "a=rtpmap:63 red/48000/2\r\n"
    "a=fmtp:63 111/111\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:13 CN/8000\r\n"
    "a=rtpmap:110 telephone-event/48000\r\n"
    "a=rtpmap:126 telephone-event/8000\r\n"
)


class ComputerVoicePairingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "computer-voice.sqlite3"
        self.now = 1_800_000_000.0
        ids = iter(
            [
                "pair-token-0000000000000001",
                "pair-token-0000000000000002",
                "pair-token-0000000000000003",
            ]
        )
        codes = iter([CODE_A, CODE_B, "RSTUVWXY2345"])
        self.store = ComputerVoicePairingStore(
            self.path,
            pepper=b"p" * 32,
            clock=lambda: self.now,
            id_factory=lambda: next(ids),
            code_factory=lambda: next(codes),
        )

    def begin(self, account="account-a"):
        return self.store.begin_pairing(account)

    def pair(
        self,
        pairing,
        *,
        code=CODE_A,
        device="windows-codex",
        token=TOKEN_A,
    ):
        return self.store.consume_pairing(
            pairing["pairId"],
            code,
            device,
            token,
        )

    def test_hash_only_pairing_and_lost_response_retry(self):
        pairing = self.begin()
        receipt = self.pair(pairing)
        replay = self.pair(pairing)
        self.assertEqual(receipt["state"], "paired")
        self.assertFalse(receipt["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertNotIn("pairingCode", receipt)
        self.assertNotIn("deviceToken", receipt)

        raw = self.path.read_bytes()
        self.assertNotIn(CODE_A.encode(), raw)
        self.assertNotIn(TOKEN_A.encode(), raw)
        with contextlib.closing(sqlite3.connect(self.path)) as connection:
            code_hash = connection.execute(
                "SELECT code_hmac FROM computer_voice_pairings"
            ).fetchone()[0]
            token_hash = connection.execute(
                "SELECT token_hmac FROM computer_voice_devices"
            ).fetchone()[0]
        self.assertRegex(code_hash, r"^[a-f0-9]{64}$")
        self.assertRegex(token_hash, r"^[a-f0-9]{64}$")

        for _ in range(PAIRING_MAX_ATTEMPTS + 1):
            with self.assertRaises(ComputerVoicePairingError) as caught:
                self.pair(pairing, code=CODE_B)
            self.assertEqual(
                caught.exception.code,
                "BW_COMPUTER_VOICE_PAIRING_REUSED",
            )
        self.assertTrue(self.pair(pairing)["replayed"])

        restarted = ComputerVoicePairingStore(
            self.path,
            pepper=b"p" * 32,
            clock=lambda: self.now,
        )
        self.assertEqual(
            restarted.authenticate_device("windows-codex", TOKEN_A)["account"],
            "account-a",
        )
        self.assertEqual(
            restarted.list_devices("account-a"),
            [{
                "deviceId": "windows-codex",
                "pairedAt": int(self.now),
                "state": "active",
            }],
        )

    def test_wrong_code_locks_and_expired_code_never_pairs(self):
        pairing = self.begin()
        for attempt in range(PAIRING_MAX_ATTEMPTS):
            with self.assertRaises(ComputerVoicePairingError) as caught:
                self.pair(pairing, code=CODE_B)
            expected = (
                "BW_COMPUTER_VOICE_PAIRING_LOCKED"
                if attempt == PAIRING_MAX_ATTEMPTS - 1
                else "BW_COMPUTER_VOICE_PAIRING_AUTH"
            )
            self.assertEqual(caught.exception.code, expected)
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.pair(pairing)
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_PAIRING_LOCKED",
        )

        expired = self.begin("account-b")
        self.now += PAIRING_TTL_SECONDS
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.pair(
                expired,
                code=CODE_B,
                device="windows-expired",
                token=TOKEN_B,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_PAIRING_EXPIRED",
        )

    def test_device_id_cannot_be_rebound_or_silently_rekeyed(self):
        first = self.begin("account-a")
        self.pair(first)
        second = self.begin("account-b")
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.pair(
                second,
                code=CODE_B,
                token=TOKEN_B,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_DEVICE_OWNERSHIP",
        )
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.pair(first, token=TOKEN_B)
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_PAIRING_REUSED",
        )
        self.assertEqual(
            self.store.authenticate_device("windows-codex", TOKEN_A)["account"],
            "account-a",
        )

    def test_revoke_fails_closed_for_auth_and_account_lookup(self):
        pairing = self.begin()
        self.pair(pairing)
        result = self.store.revoke_device("account-a", "windows-codex")
        self.assertEqual(result["state"], "revoked")
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.store.authenticate_device("windows-codex", TOKEN_A)
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_DEVICE_AUTH")
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.store.require_account_device("account-a", "windows-codex")
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_DEVICE_UNAVAILABLE",
        )
        replacement = self.begin()
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.pair(
                replacement,
                code=CODE_B,
                device="windows-codex",
                token=TOKEN_B,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_DEVICE_OWNERSHIP",
        )
        self.assertEqual(
            self.store.list_devices("account-a")[0]["state"],
            "revoked",
        )
        with self.assertRaises(ComputerVoicePairingError):
            self.store.forget_revoked_device(
                "account-b",
                "windows-codex",
            )
        forgotten = self.store.forget_revoked_device(
            "account-a",
            "windows-codex",
        )
        self.assertEqual(forgotten["state"], "forgotten")
        self.assertEqual(self.store.list_devices("account-a"), [])
        repaired = self.pair(
            replacement,
            code=CODE_B,
            device="windows-codex",
            token=TOKEN_B,
        )
        self.assertEqual(repaired["state"], "paired")

    def test_pending_pairings_are_bounded_per_account(self):
        ids = iter(
            f"pair-capacity-{index:024d}"
            for index in range(MAX_PENDING_PAIRINGS_PER_ACCOUNT + 2)
        )
        bounded = ComputerVoicePairingStore(
            Path(self.temp.name) / "bounded.sqlite3",
            pepper=b"b" * 32,
            clock=lambda: self.now,
            id_factory=lambda: next(ids),
            code_factory=lambda: CODE_A,
        )
        for _ in range(MAX_PENDING_PAIRINGS_PER_ACCOUNT):
            bounded.begin_pairing("account-capacity")
        with self.assertRaises(ComputerVoicePairingError) as caught:
            bounded.begin_pairing("account-capacity")
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_PAIRING_RATE_LIMIT",
        )

        self.now += PAIRING_TTL_SECONDS
        self.assertEqual(
            bounded.begin_pairing("account-capacity")["state"],
            "pending",
        )

    def test_pairing_replay_rows_expire_but_device_auth_survives(self):
        pairing = self.begin()
        self.pair(pairing)
        self.now += PAIRING_REPLAY_TTL_SECONDS + 1
        self.begin("account-cleanup")
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.pair(pairing)
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_PAIRING_UNAVAILABLE",
        )
        self.assertEqual(
            self.store.authenticate_device("windows-codex", TOKEN_A)[
                "account"
            ],
            "account-a",
        )

    def test_active_devices_are_bounded_per_account(self):
        ids = iter(
            f"device-capacity-pair-{index:016d}"
            for index in range(MAX_ACTIVE_DEVICES_PER_ACCOUNT + 2)
        )
        bounded = ComputerVoicePairingStore(
            Path(self.temp.name) / "device-capacity.sqlite3",
            pepper=b"d" * 32,
            clock=lambda: self.now,
            id_factory=lambda: next(ids),
            code_factory=lambda: CODE_A,
        )
        for index in range(MAX_ACTIVE_DEVICES_PER_ACCOUNT):
            pairing = bounded.begin_pairing("account-capacity")
            bounded.consume_pairing(
                pairing["pairId"],
                CODE_A,
                f"windows-{index}",
                "device-token-" + chr(65 + index) * 48,
            )
        pairing = bounded.begin_pairing("account-capacity")
        with self.assertRaises(ComputerVoicePairingError) as caught:
            bounded.consume_pairing(
                pairing["pairId"],
                CODE_A,
                "windows-over-capacity",
                "device-token-" + "Z" * 48,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_DEVICE_CAPACITY",
        )

    def test_legacy_generation_migration_is_single_transaction(self):
        legacy_path = Path(self.temp.name) / "legacy-generation.sqlite3"
        with contextlib.closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE computer_voice_devices (
                  device_id TEXT PRIMARY KEY,
                  account_id TEXT NOT NULL,
                  token_hmac TEXT NOT NULL,
                  paired_at INTEGER NOT NULL,
                  revoked_at INTEGER
                );
                INSERT INTO computer_voice_devices(
                  device_id,account_id,token_hmac,paired_at,revoked_at
                ) VALUES(
                  'legacy-device','account-a','legacy-hmac',123,NULL
                );
                """
            )
        gate = threading.Barrier(2)
        generations = []
        generation_lock = threading.Lock()

        def initialize(label):
            gate.wait(timeout=5)

            def generation():
                with generation_lock:
                    generations.append(label)
                return f"credential-{label}"

            ComputerVoicePairingStore(
                legacy_path,
                pepper=b"m" * 32,
                generation_factory=generation,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(initialize, "generation-a"),
                pool.submit(initialize, "generation-b"),
            ]
            for future in futures:
                future.result(timeout=10)

        with contextlib.closing(sqlite3.connect(legacy_path)) as connection:
            generation = connection.execute(
                "SELECT credential_generation "
                "FROM computer_voice_devices WHERE device_id='legacy-device'"
            ).fetchone()[0]
        self.assertIn(
            generation,
            {"credential-generation-a", "credential-generation-b"},
        )
        self.assertEqual(len(generations), 1)


class ComputerVoiceSignalBrokerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_800_000_000.0
        self.store = ComputerVoicePairingStore(
            Path(self.temp.name) / "computer-voice.sqlite3",
            pepper=b"q" * 32,
            clock=lambda: self.now,
            id_factory=lambda: "pair-token-0000000000000001",
            code_factory=lambda: CODE_A,
        )
        pairing = self.store.begin_pairing("account-a")
        self.store.consume_pairing(
            pairing["pairId"],
            CODE_A,
            "windows-codex",
            TOKEN_A,
        )
        self.broker = ComputerVoiceSignalBroker(
            self.store,
            clock=lambda: self.now,
            id_factory=lambda: "session-token-0000000000001",
        )
        self.session = self.broker.open_session(
            "account-a",
            "windows-codex",
        )

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
                ),
            },
        }

    @staticmethod
    def answer(signal_id="answer-1"):
        return {
            "signalId": signal_id,
            "kind": "answer",
            "payload": {
                "type": "answer",
                "sdp": (
                    "v=0\r\n"
                    "o=- 1 1 IN IP4 127.0.0.1\r\n"
                    "s=-\r\n"
                    "t=0 0\r\n"
                    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                ),
            },
        }

    def test_offer_answer_round_trip_and_idempotent_signal(self):
        sent = self.broker.exchange_reader(
            "account-a",
            self.session["sessionId"],
            signals=[self.offer()],
            cursor=0,
        )
        self.assertEqual(sent["ackedSignalIds"], ["offer-1"])
        self.assertEqual(sent["signals"], [])
        received = self.broker.exchange_device(
            "windows-codex",
            TOKEN_A,
            self.session["sessionId"],
            signals=[],
            cursor=0,
        )
        self.assertEqual(received["signals"][0]["kind"], "offer")
        device_cursor = received["cursor"]
        answered = self.broker.exchange_device(
            "windows-codex",
            TOKEN_A,
            self.session["sessionId"],
            signals=[self.answer()],
            cursor=device_cursor,
        )
        self.assertEqual(answered["ackedSignalIds"], ["answer-1"])
        reader = self.broker.exchange_reader(
            "account-a",
            self.session["sessionId"],
            signals=[self.offer()],
            cursor=sent["cursor"],
        )
        self.assertEqual(reader["ackedSignalIds"], ["offer-1"])
        self.assertEqual(reader["signals"][0]["kind"], "answer")
        self.assertNotIn("deviceToken", reader)

    def test_identity_cursor_expiry_and_reused_id_fail_closed(self):
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.broker.exchange_device(
                "windows-codex",
                TOKEN_B,
                self.session["sessionId"],
                signals=[],
                cursor=0,
            )
        self.assertEqual(caught.exception.code, "BW_COMPUTER_VOICE_DEVICE_AUTH")
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.broker.exchange_reader(
                "account-b",
                self.session["sessionId"],
                signals=[],
                cursor=0,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_UNAVAILABLE",
        )
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.broker.exchange_reader(
                "account-a",
                self.session["sessionId"],
                signals=[],
                cursor=1,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_CURSOR",
        )

        self.broker.exchange_reader(
            "account-a",
            self.session["sessionId"],
            signals=[self.offer()],
            cursor=0,
        )
        changed = self.offer()
        changed["payload"]["sdp"] += "a=sendonly\r\n"
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.broker.exchange_reader(
                "account-a",
                self.session["sessionId"],
                signals=[changed],
                cursor=1,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_ID_REUSE",
        )

        self.now += SIGNAL_TTL_SECONDS
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.broker.exchange_reader(
                "account-a",
                self.session["sessionId"],
                signals=[],
                cursor=1,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_UNAVAILABLE",
        )

    def test_signal_schema_cannot_smuggle_audio_or_arbitrary_fields(self):
        cases = [
            {
                **self.offer(),
                "payload": {
                    "type": "offer",
                    "sdp": "v=0\r\n",
                    "audio": "base64-pcm-is-forbidden",
                },
            },
            {
                "signalId": "raw-audio",
                "kind": "audio",
                "payload": {"pcm": "AAAA"},
            },
            {
                "signalId": "bad-ice",
                "kind": "ice",
                "payload": {
                    "candidate": "candidate:1",
                    "sdpMid": "0",
                    "sdpMLineIndex": 0,
                    "bytes": "AAAA",
                },
            },
            {
                **self.offer("data-channel"),
                "payload": {
                    "type": "offer",
                    "sdp": (
                        "v=0\r\n"
                        "o=- 1 1 IN IP4 127.0.0.1\r\n"
                        "s=-\r\n"
                        "t=0 0\r\n"
                        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
                    ),
                },
            },
            {
                **self.offer("video-channel"),
                "payload": {
                    "type": "offer",
                    "sdp": (
                        "v=0\r\n"
                        "o=- 1 1 IN IP4 127.0.0.1\r\n"
                        "s=-\r\n"
                        "t=0 0\r\n"
                        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
                    ),
                },
            },
            {
                **self.offer("inline-pcm"),
                "payload": {
                    "type": "offer",
                    "sdp": (
                        "v=0\r\n"
                        "o=- 1 1 IN IP4 127.0.0.1\r\n"
                        "s=-\r\n"
                        "t=0 0\r\n"
                        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                        "a=x-payload:data:audio/pcm;base64,AAAA\r\n"
                    ),
                },
            },
            {
                "signalId": "huge-mid",
                "kind": "ice",
                "payload": {
                    "candidate": "candidate:1",
                    "sdpMid": "a" * 65,
                    "sdpMLineIndex": 0,
                },
            },
            {
                "signalId": "fake-candidate-pcm",
                "kind": "ice",
                "payload": {
                    "candidate": "PCM:" + "A" * 900,
                    "sdpMid": "0",
                    "sdpMLineIndex": 0,
                },
            },
            {
                **self.offer("unknown-sdp-attribute"),
                "payload": {
                    "type": "offer",
                    "sdp": (
                        "v=0\r\n"
                        "o=- 1 1 IN IP4 127.0.0.1\r\n"
                        "s=-\r\n"
                        "t=0 0\r\n"
                        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                        + "".join(
                            f"a=x-pcm:{'A' * 400}{index}\r\n"
                            for index in range(20)
                        )
                    ),
                },
            },
        ]
        for signal in cases:
            with self.subTest(signal=signal["signalId"]):
                with self.assertRaises(ComputerVoicePairingError) as caught:
                    self.broker.exchange_reader(
                        "account-a",
                        self.session["sessionId"],
                        signals=[signal],
                        cursor=0,
                    )
                self.assertEqual(
                    caught.exception.code,
                    (
                        "BW_COMPUTER_VOICE_SIGNAL_MEDIA_SCOPE"
                        if signal["signalId"] in {
                            "data-channel",
                            "video-channel",
                            "inline-pcm",
                            "unknown-sdp-attribute",
                        }
                        else "BW_COMPUTER_VOICE_SIGNAL_INVALID"
                    ),
                )

    def test_valid_bounded_ice_candidate_round_trip(self):
        candidate = {
            "signalId": "ice-1",
            "kind": "ice",
            "payload": {
                "candidate": (
                    "candidate:1 1 udp 2122260223 192.168.1.2 54321 "
                    "typ host generation 0 ufrag abc network-cost 999"
                ),
                "sdpMid": "0",
                "sdpMLineIndex": 0,
            },
        }
        sent = self.broker.exchange_reader(
            "account-a",
            self.session["sessionId"],
            signals=[candidate],
            cursor=0,
        )
        self.assertEqual(sent["ackedSignalIds"], ["ice-1"])
        received = self.broker.exchange_device(
            "windows-codex",
            TOKEN_A,
            self.session["sessionId"],
            signals=[],
            cursor=0,
        )
        self.assertEqual(
            received["signals"][0]["payload"]["candidate"],
            candidate["payload"]["candidate"],
        )

    def test_chromium_audio_sdp_shape_and_common_ice_shapes(self):
        for index, (signal_id, sdp) in enumerate((
            ("chromium-shape", CHROMIUM_AUDIO_SDP_SHAPE),
            (
                "windows-chrome-150-two-audio",
                WINDOWS_CHROME_150_TWO_AUDIO_OFFER,
            ),
        )):
            offer = self.offer(signal_id)
            offer["payload"]["sdp"] = sdp
            sent = self.broker.exchange_reader(
                "account-a",
                self.session["sessionId"],
                signals=[offer],
                cursor=index,
            )
            self.assertEqual(sent["ackedSignalIds"], [signal_id])
        candidates = [
            (
                "candidate:842163049 1 udp 1677734910 192.0.2.10 "
                "55000 typ srflx raddr 0.0.0.0 rport 9 generation 0 "
                "ufrag AbCd network-cost 999"
            ),
            (
                "candidate:1 1 TCP 2122260223 "
                "123e4567-e89b-12d3-a456-426614174000.local 9 "
                "typ host tcptype active generation 0"
            ),
        ]
        for index, value in enumerate(candidates):
            signal = {
                "signalId": f"common-ice-{index}",
                "kind": "ice",
                "payload": {
                    "candidate": value,
                    "sdpMid": "0",
                    "sdpMLineIndex": 0,
                },
            }
            result = self.broker.exchange_reader(
                "account-a",
                self.session["sessionId"],
                signals=[signal],
                cursor=sent["cursor"] + index,
            )
            self.assertEqual(
                result["ackedSignalIds"],
                [signal["signalId"]],
            )

    def test_fake_media_protocol_and_session_text_are_rejected(self):
        fake_protocol = self.offer("fake-media-proto")
        fake_protocol["payload"]["sdp"] = (
            "v=0\r\n"
            "o=- 1 1 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
            "m=audio 9 XYZRTP/SAVPFAKE 111\r\n"
        )
        session_payload = self.offer("session-payload")
        session_payload["payload"]["sdp"] = (
            "v=0\r\n"
            "o=- 1 1 IN IP4 127.0.0.1\r\n"
            f"s={'A' * 400}\r\n"
            "t=0 0\r\n"
            "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        )
        for signal in (fake_protocol, session_payload):
            with self.assertRaises(ComputerVoicePairingError) as caught:
                self.broker.exchange_reader(
                    "account-a",
                    self.session["sessionId"],
                    signals=[signal],
                    cursor=0,
                )
            self.assertEqual(
                caught.exception.code,
                "BW_COMPUTER_VOICE_SIGNAL_MEDIA_SCOPE",
            )

    def test_repaired_device_cannot_inherit_previous_generation_session(self):
        pair_ids = iter([
            "pair-generation-old-00000001",
            "pair-generation-new-00000002",
        ])
        codes = iter([CODE_A, CODE_B])
        generations = iter([
            "credential-generation-old",
            "credential-generation-new",
        ])
        store = ComputerVoicePairingStore(
            Path(self.temp.name) / "generation.sqlite3",
            pepper=b"g" * 32,
            clock=lambda: self.now,
            id_factory=lambda: next(pair_ids),
            code_factory=lambda: next(codes),
            generation_factory=lambda: next(generations),
        )
        old_pairing = store.begin_pairing("account-a")
        store.consume_pairing(
            old_pairing["pairId"],
            CODE_A,
            "windows-same-id",
            TOKEN_A,
        )
        broker = ComputerVoiceSignalBroker(
            store,
            clock=lambda: self.now,
            id_factory=lambda: "generation-session-00000001",
        )
        session = broker.open_session(
            "account-a",
            "windows-same-id",
        )
        broker.exchange_reader(
            "account-a",
            session["sessionId"],
            signals=[self.offer("old-offer")],
            cursor=0,
        )
        store.revoke_device("account-a", "windows-same-id")
        store.forget_revoked_device("account-a", "windows-same-id")
        new_pairing = store.begin_pairing("account-a")
        store.consume_pairing(
            new_pairing["pairId"],
            CODE_B,
            "windows-same-id",
            TOKEN_B,
        )
        with self.assertRaises(ComputerVoicePairingError) as caught:
            broker.exchange_device(
                "windows-same-id",
                TOKEN_B,
                session["sessionId"],
                signals=[],
                cursor=0,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_UNAVAILABLE",
        )
        with self.assertRaises(ComputerVoicePairingError) as caught:
            broker.exchange_reader(
                "account-a",
                session["sessionId"],
                signals=[],
                cursor=0,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_UNAVAILABLE",
        )

    def test_failed_batch_is_atomic_and_returned_payload_is_detached(self):
        first = self.offer("new-before-error")
        self.broker.exchange_reader(
            "account-a",
            self.session["sessionId"],
            signals=[self.offer("existing")],
            cursor=0,
        )
        changed = self.offer("existing")
        changed["payload"]["sdp"] += "a=sendonly\r\n"
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.broker.exchange_reader(
                "account-a",
                self.session["sessionId"],
                signals=[first, changed],
                cursor=1,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_ID_REUSE",
        )
        device = self.broker.exchange_device(
            "windows-codex",
            TOKEN_A,
            self.session["sessionId"],
            signals=[],
            cursor=1,
        )
        self.assertEqual(device["signals"], [])
        self.assertEqual(device["cursor"], 1)

        detached = self.broker.exchange_device(
            "windows-codex",
            TOKEN_A,
            self.session["sessionId"],
            signals=[],
            cursor=0,
        )
        detached["signals"][0]["payload"]["sdp"] = "mutated"
        reread = self.broker.exchange_device(
            "windows-codex",
            TOKEN_A,
            self.session["sessionId"],
            signals=[],
            cursor=0,
        )
        self.assertTrue(
            reread["signals"][0]["payload"]["sdp"].startswith("v=0")
        )

    def test_session_count_and_message_capacity_are_bounded(self):
        ids = iter(
            f"session-capacity-{index:024d}"
            for index in range(MAX_SIGNAL_SESSIONS_PER_DEVICE + 2)
        )
        broker = ComputerVoiceSignalBroker(
            self.store,
            clock=lambda: self.now,
            id_factory=lambda: next(ids),
        )
        for _ in range(MAX_SIGNAL_SESSIONS_PER_DEVICE):
            broker.open_session("account-a", "windows-codex")
        with self.assertRaises(ComputerVoicePairingError) as caught:
            broker.open_session("account-a", "windows-codex")
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_CAPACITY",
        )

        for index in range(MAX_SIGNAL_MESSAGES_PER_SESSION):
            self.broker.exchange_reader(
                "account-a",
                self.session["sessionId"],
                signals=[self.offer(f"bounded-{index}")],
                cursor=index,
            )
        with self.assertRaises(ComputerVoicePairingError) as caught:
            self.broker.exchange_reader(
                "account-a",
                self.session["sessionId"],
                signals=[self.offer("one-too-many")],
                cursor=MAX_SIGNAL_MESSAGES_PER_SESSION,
            )
        self.assertEqual(
            caught.exception.code,
            "BW_COMPUTER_VOICE_SIGNAL_CAPACITY",
        )
        self.assertGreater(MAX_SIGNAL_BYTES_PER_SESSION, 0)


if __name__ == "__main__":
    unittest.main()
