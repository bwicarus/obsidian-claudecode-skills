import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_collaboration.py"


class AgentCollaborationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()
        conn = sqlite3.connect(self.state / "mailbox.sqlite3")
        try:
            conn.executescript(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    reply_to TEXT,
                    acknowledged_at TEXT
                );
                INSERT INTO messages
                (id,created_at,sender,recipient,subject,body,scope,reply_to,acknowledged_at)
                VALUES
                ('legacy-1','2026-07-29T00:00:00+00:00','codex','claude',
                 'Legacy handoff','Read-only archive item.','tests/',NULL,NULL);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--state-dir", str(self.state), "--json", *args],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def test_status_and_inbox_are_read_only_archive_views(self):
        before = (self.state / "mailbox.sqlite3").read_bytes()
        status = self.call("status")
        inbox = self.call("inbox", "--agent", "claude")
        after = (self.state / "mailbox.sqlite3").read_bytes()
        self.assertTrue(status["retired"])
        self.assertTrue(status["read_only"])
        self.assertEqual(status["historical_unacknowledged"]["claude"], 1)
        self.assertEqual(inbox[0]["id"], "legacy-1")
        self.assertEqual(before, after)

    def test_mutating_and_wait_commands_are_retired(self):
        commands = [
            ("init",),
            ("send", "--from", "claude", "--to", "codex", "--subject", "Q", "--body", "Question"),
            ("ack", "--agent", "claude", "legacy-1"),
            ("reply", "--from", "claude", "--body", "Reviewed", "legacy-1"),
            ("wait", "--agent", "claude", "--timeout", "0"),
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), "--state-dir", str(self.state), *command],
                    text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 3)
                self.assertIn("legacy SQLite collaboration mailbox is retired", result.stderr)
        self.assertEqual(
            self.call("status")["historical_unacknowledged"]["claude"],
            1,
        )

    def test_missing_archive_fails_without_creating_it(self):
        missing = Path(self.temp.name) / "missing"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--state-dir", str(missing), "status"],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("archive does not exist", result.stderr)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
