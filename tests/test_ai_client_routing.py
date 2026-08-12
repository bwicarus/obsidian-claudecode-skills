from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ai_client  # noqa: E402


class AIClientRoutingTests(unittest.TestCase):
    def test_auto_claude_falls_back_when_oauth_session_expired(self):
        calls = []

        def claude():
            calls.append("claude")
            return "Failed to authenticate: OAuth session expired and could not be refreshed"

        def codex():
            calls.append("codex")
            return '{"zh":"订购；调货"}'

        self.assertEqual(ai_client.route("auto-claude", claude, codex), '{"zh":"订购；调货"}')
        self.assertEqual(calls, ["claude", "codex"])

    def test_auto_route_keeps_an_ordinary_model_answer(self):
        calls = []

        def claude():
            calls.append("claude")
            return "The chapter explains why 401 Unauthorized can mean authentication failed."

        def codex():
            calls.append("codex")
            return "unexpected"

        self.assertEqual(
            ai_client.route("auto-claude", claude, codex),
            "The chapter explains why 401 Unauthorized can mean authentication failed.",
        )
        self.assertEqual(calls, ["claude"])

    def test_auto_route_keeps_explanation_that_starts_with_an_error_phrase(self):
        calls = []
        answer = "OAuth session expired is a common reason users need to sign in again."

        def claude():
            calls.append("claude")
            return answer

        def codex():
            calls.append("codex")
            return "unexpected"

        self.assertEqual(ai_client.route("auto-claude", claude, codex), answer)
        self.assertEqual(calls, ["claude"])

    def test_auto_route_falls_back_on_empty_provider_output(self):
        calls = []

        def claude():
            calls.append("claude")
            return "   "

        def codex():
            calls.append("codex")
            return '{"zh":"订购；调货"}'

        self.assertEqual(ai_client.route("auto-claude", claude, codex), '{"zh":"订购；调货"}')
        self.assertEqual(calls, ["claude", "codex"])

    def test_explicit_provider_does_not_reroute(self):
        calls = []

        def claude():
            calls.append("claude")
            return "Failed to authenticate: OAuth session expired"

        def codex():
            calls.append("codex")
            return "unexpected"

        self.assertEqual(
            ai_client.route("claude", claude, codex),
            "Failed to authenticate: OAuth session expired",
        )
        self.assertEqual(calls, ["claude"])


if __name__ == "__main__":
    unittest.main()
