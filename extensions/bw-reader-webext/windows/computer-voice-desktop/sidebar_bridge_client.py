"""Windows voice-history → exact active Reader over the local output pipe.

The old implementation SSHed to the Pi and hoped that the Pi-side bridge could
resolve the current Reader.  The snapshot already names the exact live WSS
source, so the local ReaderPC service is now the only hop.  This module remains
deliberately narrow: history sync can publish assistant turns, not arbitrary
Reader actions.
"""
from __future__ import annotations

import json
import os
import struct
import time
from typing import Any


PIPE_NAME = "bw-reader-realtime-output-rpc-v1"
PIPE_PATH = r"\\.\pipe" + "\\" + PIPE_NAME
CONNECT_TIMEOUT_SECONDS = 3.0
MAX_FRAME_BYTES = 1024 * 1024
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


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise SidebarBridgeError("Reader 本地输出管道提前关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _run_once(envelope: dict[str, Any]) -> dict[str, Any]:
    if os.name != "nt":
        raise SidebarBridgeError("Reader 本地输出管道只在 Windows 可用")
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    if not 1 <= len(encoded) <= MAX_FRAME_BYTES:
        raise SidebarBridgeError("Reader 本地输出请求超过大小上限")
    deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
    while True:
        try:
            stream = open(PIPE_PATH, "r+b", buffering=0)
            break
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise SidebarBridgeError(
                    "Reader 本地输出服务未连接"
                ) from exc
            time.sleep(0.05)
    with stream:
        stream.write(struct.pack("<I", len(encoded)))
        stream.write(encoded)
        header = _read_exact(stream, 4)
        length = struct.unpack("<I", header)[0]
        if not 1 <= length <= MAX_FRAME_BYTES:
            raise SidebarBridgeError("Reader 本地输出回执长度无效")
        raw = _read_exact(stream, length)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SidebarBridgeError(
            "Reader 本地输出回执不是 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SidebarBridgeError("Reader 本地输出回执不是 JSON 对象")
    return value


def call(
    kind: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    file: str | None = None,
    page: int | str | None = None,
    source_instance_id: str | None = None,
    snapshot_revision: int | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Send one fixed assistant turn to the exact live Reader source."""
    if kind != "assistant_turn":
        raise SidebarBridgeError("历史同步只允许 assistant_turn")
    turn_payload = _validate_turn_payload(payload)
    correlation = request_id or ""
    if (
        not correlation
        or len(correlation) > 160
        or not all(
            ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for ch in correlation
        )
        or not source_instance_id
        or len(source_instance_id) > 160
        or not all(
            ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for ch in source_instance_id
        )
        or isinstance(snapshot_revision, bool)
        or not isinstance(snapshot_revision, int)
        or snapshot_revision < 0
        or not isinstance(file, str)
        or not file
        or len(file) > 4096
        or "\x00" in file
        or isinstance(page, bool)
        or not (
            isinstance(page, int) and page >= 0
            or isinstance(page, str) and 1 <= len(page) <= 256 and "\x00" not in page
        )
        or not isinstance(thread_id, str)
        or not thread_id
        or len(thread_id) > 160
        or not all(
            ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for ch in thread_id
        )
    ):
        raise SidebarBridgeError("当前 Reader 本地输出身份无效或已过期")
    envelope: dict[str, Any] = {
        "contract": "reader-realtime-output/1",
        "type": "output-request",
        "correlation": correlation,
        "sourceInstanceId": source_instance_id,
        "snapshotRevision": snapshot_revision,
        "file": file,
        "page": page,
        "kind": "assistant-turn",
        "payload": {
            "threadId": thread_id,
            "user": turn_payload["user_utterance"],
            "assistant": turn_payload["text"],
        },
    }
    try:
        result = _run_once(envelope)
    except Exception as first:
        try:
            result = _run_once(envelope)
        except Exception as second:
            raise SidebarBridgeError(
                "Reader 本地输出不可用（重连一次仍失败）："
                f"{type(first).__name__} / {type(second).__name__}"
            ) from second
        result["_reconnected"] = True
    if result.get("ok") is not True:
        raise SidebarBridgeError(
            str(result.get("code") or result.get("message") or
                "Reader 拒绝了聊天同步")[:400]
        )
    return result
