"""Server contracts for installed-client OpenAI Realtime WebRTC setup."""

from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import assistant  # noqa: E402


class _OpenAIResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "value": "ek_" + "e" * 48,
            "expires_at": int(time.time()) + 90,
        }


class RealtimeDirectCredentialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="reader-rtc-client-secret-"
        )
        self.addCleanup(self.temp.cleanup)
        self.previous_ticket_path = assistant._VOICE_TICKET_KEY
        assistant._VOICE_TICKET_KEY = Path(self.temp.name) / "voice-ticket.key"
        self.addCleanup(
            setattr,
            assistant,
            "_VOICE_TICKET_KEY",
            self.previous_ticket_path,
        )

        app = Flask(__name__)
        app.secret_key = "realtime-direct-contract"
        app.register_blueprint(assistant.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = "alice"

    @staticmethod
    def session_config():
        return {
            "type": "realtime",
            "model": "gpt-realtime-2.1-mini",
            "instructions": "bounded test session",
        }

    def test_client_secret_is_short_lived_server_configured_and_no_store(self):
        long_lived_key = "sk-project-must-remain-server-side"
        with (
            patch.object(assistant, "_voice_budget_gate", return_value=(True, 0.0)),
            patch.object(
                assistant,
                "_build_rtc_session",
                return_value=(self.session_config(), 24000, True),
            ),
            patch.object(
                assistant, "_openai_realtime_key", return_value=long_lived_key
            ),
            patch("requests.post", return_value=_OpenAIResponse()) as openai_post,
        ):
            response = self.client.post(
                "/api/assistant/rtc-client-secret",
                json={"file": "book.pdf", "page": 7},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        payload = response.get_json()
        self.assertEqual(payload["client_secret"], "ek_" + "e" * 48)
        self.assertEqual(payload["model"], "gpt-realtime-2.1-mini")
        self.assertTrue(payload["rt_image"])
        self.assertNotIn(long_lived_key, response.get_data(as_text=True))
        self.assertTrue(assistant._verify_rtc_bind_grant("alice", payload["bind_grant"]))

        request = openai_post.call_args
        self.assertEqual(
            request.args[0],
            "https://api.openai.com/v1/realtime/client_secrets",
        )
        self.assertEqual(
            request.kwargs["headers"]["Authorization"],
            f"Bearer {long_lived_key}",
        )
        self.assertEqual(request.kwargs["json"]["session"], self.session_config())
        self.assertEqual(
            request.kwargs["json"]["expires_after"],
            {"anchor": "created_at", "seconds": 90},
        )

    def test_bind_grant_is_account_scoped_and_mints_only_control_ticket(self):
        grant = assistant._rtc_bind_grant("alice")
        call_id = "rtc_directcontract123"
        response = self.client.post(
            "/api/assistant/rtc-bind",
            json={"call_id": call_id, "bind_grant": grant},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["call_id"], call_id)
        self.assertEqual(payload["uid"], "alice")
        self.assertRegex(payload["ticket"], r"^[a-f0-9]{32}$")
        self.assertNotIn("client_secret", payload)

        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = "bob"
        rejected = self.client.post(
            "/api/assistant/rtc-bind",
            json={"call_id": call_id, "bind_grant": grant},
        )
        self.assertEqual(rejected.status_code, 403)

    def test_client_secret_still_obeys_authentication_and_budget_gate(self):
        anonymous_app = Flask("realtime-direct-anonymous")
        anonymous_app.secret_key = "realtime-direct-contract"
        anonymous_app.register_blueprint(assistant.bp)
        anonymous = anonymous_app.test_client()
        self.assertEqual(
            anonymous.post("/api/assistant/rtc-client-secret", json={}).status_code,
            401,
        )

        with patch.object(
            assistant, "_voice_budget_gate", return_value=(False, 5.25)
        ):
            limited = self.client.post(
                "/api/assistant/rtc-client-secret", json={}
            )
        self.assertEqual(limited.status_code, 429)

    def test_native_config_returns_no_key_after_explicit_authenticated_post(self):
        with patch.object(
            assistant,
            "_build_rtc_session",
            return_value=(self.session_config(), 24000, True),
        ):
            rejected = self.client.post(
                "/api/assistant/native-realtime-config",
                json={},
            )
            response = self.client.post(
                "/api/assistant/native-realtime-config",
                json={"contract": "reader-native-realtime-config/1"},
            )

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        payload = response.get_json()
        self.assertEqual(
            payload["contract"],
            "reader-native-realtime-config/1",
        )
        self.assertNotIn("api_key", payload)
        self.assertEqual(payload["session"], self.session_config())
        self.assertEqual(payload["compact_tokens"], 24000)
        self.assertTrue(payload["rt_image"])

        anonymous_app = Flask("native-realtime-config-anonymous")
        anonymous_app.secret_key = "realtime-direct-contract"
        anonymous_app.register_blueprint(assistant.bp)
        anonymous = anonymous_app.test_client()
        unauthenticated = anonymous.post(
            "/api/assistant/native-realtime-config",
            json={"contract": "reader-native-realtime-config/1"},
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(
            unauthenticated.headers.get("Cache-Control"),
            "no-store",
        )

    def test_image_sideband_uses_direct_calls_short_lived_identity(self):
        calls = []

        class FakeSocket:
            def __init__(self):
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def send(self, payload):
                self.sent.append(json.loads(payload))

            def recv(self, timeout=None):
                return json.dumps({"type": "conversation.item.created"})

        def fake_connect(url, **kwargs):
            socket = FakeSocket()
            calls.append((url, kwargs, socket))
            return socket

        websockets_module = types.ModuleType("websockets")
        sync_module = types.ModuleType("websockets.sync")
        client_module = types.ModuleType("websockets.sync.client")
        client_module.connect = fake_connect
        websockets_module.sync = sync_module
        sync_module.client = client_module
        ephemeral = "ek_" + "s" * 48

        with patch.dict(
            sys.modules,
            {
                "websockets": websockets_module,
                "websockets.sync": sync_module,
                "websockets.sync.client": client_module,
            },
        ):
            ok = assistant._rtc_sideband_images(
                "rtc_directcontract123",
                [{"media_type": "image/png", "b64": "YWJj"}],
                auth_key=ephemeral,
            )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        url, kwargs, socket = calls[0]
        self.assertEqual(
            url,
            "wss://api.openai.com/v1/realtime?call_id=rtc_directcontract123",
        )
        self.assertEqual(
            kwargs["additional_headers"]["Authorization"],
            f"Bearer {ephemeral}",
        )
        self.assertEqual(socket.sent[0]["type"], "conversation.item.create")
        image = socket.sent[0]["item"]["content"][0]
        self.assertEqual(image["type"], "input_image")
        self.assertEqual(image["image_url"], "data:image/png;base64,YWJj")

    def test_direct_call_hangup_uses_the_same_short_lived_identity(self):
        ephemeral = "ek_" + "h" * 48
        response = types.SimpleNamespace(status_code=200)
        with (
            patch.object(
                assistant,
                "_RTC_KEY_PATH",
                Path(self.temp.name) / "must-not-be-read.json",
            ),
            patch("requests.post", return_value=response) as hangup,
        ):
            result = self.client.post(
                "/api/assistant/rtc-hangup",
                json={
                    "call_id": "rtc_directcontract123",
                    "rtc_sideband_secret": ephemeral,
                },
            )

        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()["ok"])
        request = hangup.call_args
        self.assertEqual(
            request.args[0],
            "https://api.openai.com/v1/realtime/calls/rtc_directcontract123/hangup",
        )
        self.assertEqual(
            request.kwargs["headers"]["Authorization"],
            f"Bearer {ephemeral}",
        )


if __name__ == "__main__":
    unittest.main()
