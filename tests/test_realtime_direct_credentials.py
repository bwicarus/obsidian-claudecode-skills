"""Server contracts for installed-client OpenAI Realtime WebRTC setup."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
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


if __name__ == "__main__":
    unittest.main()
