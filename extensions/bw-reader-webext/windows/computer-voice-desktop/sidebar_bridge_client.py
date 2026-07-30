"""Minimal packaged transport for Windows voice-history → Reader sidebar.

This is deliberately narrower than ``scripts/bridge_client.py``: it only
emits the already-existing ``assistant_turn`` envelope used by the history
synchronizer.  It does not validate or publish result cards and cannot call
MCP tools.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Any


PI_HOST = os.environ.get(
    "BW_PI_HOST",
    "pi",
)
REMOTE = os.environ.get(
    "BW_BRIDGE_REMOTE",
    "/home/bwicarus/claude/scripts/reader_bridge.py",
)
CONNECT_TIMEOUT_SECONDS = 10
CALL_TIMEOUT_SECONDS = 45
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_TEXT_CHARS = 4_000
READER_SYNC_MARKERS = ("[[READER_SYNC]]", "[[/READER_SYNC]]")


class SidebarBridgeError(RuntimeError):
    """The bounded direct-command transport failed."""


def _validate_turn_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != {
        "text",
        "user_utterance",
    }:
        raise SidebarBridgeError("assistant_turn payload 字段无效")
    normalized: dict[str, str] = {}
    for field in ("text", "user_utterance"):
        value = payload[field]
        if not isinstance(value, str):
            raise SidebarBridgeError(f"assistant_turn.{field} 必须是文本")
        value = value.strip()
        if (
            not value
            or len(value) > MAX_TEXT_CHARS
            or "\x00" in value
            or any(marker in value for marker in READER_SYNC_MARKERS)
        ):
            raise SidebarBridgeError(f"assistant_turn.{field} 文本无效")
        normalized[field] = value
    return normalized


def _ssh_base() -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        PI_HOST,
    ]


def _run_once(envelope: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run(
        _ssh_base() + ["python3", REMOTE],
        input=json.dumps(envelope, ensure_ascii=False),
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        timeout=CALL_TIMEOUT_SECONDS,
        check=False,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr or completed.stdout or ""
        ).strip()[:400]
        raise SidebarBridgeError(
            f"SSH 执行失败(exit {completed.returncode}):{detail}"
        )
    output = (completed.stdout or "").strip()
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SidebarBridgeError(
            f"远端返回的不是 JSON:{output[:400]}"
        ) from exc
    if not isinstance(value, dict):
        raise SidebarBridgeError("远端返回值不是 JSON 对象")
    return value


def call(
    kind: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    file: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Send one fixed bridge envelope, retrying one failed SSH launch once."""
    if kind != "assistant_turn":
        raise SidebarBridgeError("历史同步只允许 assistant_turn")
    turn_payload = _validate_turn_payload(payload)
    envelope: dict[str, Any] = {
        "version": 1,
        "kind": kind,
        "request_id": (
            request_id
            or f"win-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        ),
        "payload": turn_payload,
    }
    if file:
        envelope["file"] = file
    if page is not None:
        envelope["page"] = page
    try:
        return _run_once(envelope)
    except Exception as first:
        try:
            result = _run_once(envelope)
        except Exception as second:
            raise SidebarBridgeError(
                "桥接不可用（重连一次仍失败）："
                f"{type(first).__name__} / {type(second).__name__}"
            ) from second
        result["_reconnected"] = True
        return result
