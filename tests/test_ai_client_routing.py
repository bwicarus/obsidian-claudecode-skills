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
            return "The chapter discusses authentication failed attempts."

        def codex():
            calls.append("codex")
            return "unexpected"

        self.assertEqual(
            ai_client.route("auto-claude", claude, codex),
            "The chapter discusses authentication failed attempts.",
        )
        self.assertEqual(calls, ["claude"])

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
