#!/usr/bin/env python3
"""Read-only archive viewer for the retired Claude/Codex SQLite mailbox."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

AGENTS = {"claude", "codex"}
RETIRED_MESSAGE = (
    "the legacy SQLite collaboration mailbox is retired; "
    "use BW AgentBridge Lite via the desktop formal launcher"
)


def default_state_dir() -> Path:
    override = os.environ.get("BW_AGENT_COLLAB_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "BWAgentCollaboration"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "bw-agent-collaboration"


def agent(value: str) -> str:
    value = value.lower().strip()
    if value not in AGENTS:
        raise ValueError("agent must be claude or codex")
    return value


class Mailbox:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.db = state_dir / "mailbox.sqlite3"

    def connect(self) -> sqlite3.Connection:
        if not self.db.is_file():
            raise ValueError("legacy mailbox archive does not exist: " + str(self.db))
        uri = self.db.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA query_only = ON")
        return conn

    def send(self, sender: str, recipient: str, subject: str, body: str, scope: str = "", reply_to: str | None = None) -> dict:
        raise ValueError(RETIRED_MESSAGE)

    def inbox(self, recipient: str, include_acknowledged: bool = False) -> list[dict]:
        recipient = agent(recipient)
        sql = "SELECT * FROM messages WHERE recipient=?"
        if not include_acknowledged:
            sql += " AND acknowledged_at IS NULL"
        sql += " ORDER BY created_at,id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, (recipient,))]

    def replies(self, message_id: str) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM messages WHERE reply_to=? ORDER BY created_at,id", (message_id,)
            )]

    def acknowledge(self, recipient: str, message_id: str) -> dict:
        raise ValueError(RETIRED_MESSAGE)

    def reply(self, sender: str, message_id: str, body: str, subject: str | None = None) -> dict:
        raise ValueError(RETIRED_MESSAGE)

    def status(self) -> dict:
        with self.connect() as conn:
            unread = {
                name: conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE recipient=? AND acknowledged_at IS NULL", (name,)
                ).fetchone()[0]
                for name in sorted(AGENTS)
            }
        return {
            "state_dir": str(self.state_dir),
            "database": str(self.db),
            "retired": True,
            "read_only": True,
            "historical_unacknowledged": unread,
        }


def output(value, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if not isinstance(value, list) and "id" not in value:
        print(json.dumps(value, ensure_ascii=False))
        return
    rows = value if isinstance(value, list) else [value]
    for row in rows:
        print("{id} | {created_at} | {sender} -> {recipient} | {subject}".format(**row))
        if row.get("scope"):
            print("  scope: " + row["scope"])
        print("  " + row.get("body", ""))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    send = commands.add_parser("send")
    send.add_argument("--from", dest="sender", required=True)
    send.add_argument("--to", dest="recipient", required=True)
    send.add_argument("--subject", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--scope", default="")
    inbox = commands.add_parser("inbox")
    inbox.add_argument("--agent", required=True)
    inbox.add_argument("--all", action="store_true")
    ack = commands.add_parser("ack")
    ack.add_argument("--agent", required=True)
    ack.add_argument("message_id")
    reply = commands.add_parser("reply")
    reply.add_argument("--from", dest="sender", required=True)
    reply.add_argument("--body", required=True)
    reply.add_argument("--subject")
    reply.add_argument("message_id")
    wait = commands.add_parser("wait")
    wait.add_argument("--agent", choices=sorted(AGENTS))
    wait.add_argument("--reply-to")
    wait.add_argument("--timeout", type=float, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    mailbox = Mailbox(args.state_dir)
    try:
        if args.command in {"init", "send", "ack", "reply", "wait"}:
            print("agent-collaboration: " + RETIRED_MESSAGE, file=sys.stderr)
            return 3
        if args.command == "status":
            output(mailbox.status(), args.json)
        elif args.command == "inbox":
            output(mailbox.inbox(args.agent, args.all), args.json)
    except (ValueError, sqlite3.Error) as error:
        print("agent-collaboration: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
