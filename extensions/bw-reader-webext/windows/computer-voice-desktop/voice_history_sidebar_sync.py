#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Archive the active local Codex Voice thread and optionally mirror turns.

The two Codex files are treated as a small, untrusted shared-memory surface:

* ``.codex-global-state.json`` selects one exact local UUID.
* ``realtime-voice-continuity.json`` supplies the recent window for that UUID.

The recent window is appended with longest suffix/prefix overlap.  A window
with no overlap is kept, but an explicit gap boundary prevents a user message
on one side from being paired with an assistant message on the other.

Publishing is opt-in.  Importing this module and the default CLI invocation do
not import ``bridge_client`` or make a network call.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import html
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


VERSION = 1
RECENT_THREAD_KEY = "realtime-voice-most-recent-thread"

# 16 MiB（原 1 MiB）。2026-09-06 Codex 正式版把 .codex-global-state.json 写到了 1.5 MB，
# 超限被当成"读不到"，同步器退回上一次绑定的 Beta 旧线程，聊天记录就此停更 —— 而日志里
# 一个字都没有。这个文件是整份 JSON，只能整读；上限只防畸形文件，不该卡在正常增长上。
MAX_GLOBAL_STATE_BYTES = 16 * 1024 * 1024
MAX_CONTINUITY_BYTES = 4 * 1024 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_THREADS = 128
MAX_RECENT_ITEMS = 20
MAX_ARCHIVE_ITEMS = 10_000
MAX_TEXT_CHARS = 4_000
MAX_CODEX_TEXT_CHARS = 8_000
# 2026-09-04 实测:一条用了 21 天的语音线程 thread/read 回 35.3 MB(1017 turns),
# 直接撞破当时的 32 MB 上限 → "Codex app-server response invalid" → 结构化回填每次都失败
# → `_structured_baseline` 保持 None → should_read 恒真 → 每 15s 重读一遍磁盘上 157 MiB
# 的 rollout。聊天记录一直同步不到 App 就是这么来的。
# ⚠ 抬上限是**推迟**不是治愈:响应字节按约 1.7 MB/天长(九成是投影根本不要的 mcpToolCall
#   载荷), 128 MB 大约再撑一个多月, 之后 MAX_CODEX_TURNS 也会撞上。
#   治本要么轮换语音线程(新线程 = 新 rollout), 要么等 app-server 提供分页读
#   (当前 ThreadReadParams 只有 threadId/includeTurns, 没有 limit/cursor)。
MAX_CODEX_RESPONSE_BYTES = 128 * 1024 * 1024
# 响应大到这个量级就不该按 0.75s 的节奏反复整读(见 _structured_read_cooldown_seconds)。
STRUCTURED_READ_LARGE_BYTES = 8 * 1024 * 1024
STRUCTURED_READ_HUGE_BYTES = 24 * 1024 * 1024
STRUCTURED_READ_LARGE_COOLDOWN_SECONDS = 5.0
STRUCTURED_READ_HUGE_COOLDOWN_SECONDS = 15.0
# 侧栏历史是**有界列表**（用户 2026-09-05：「设置上限就好，比如最多到目前为止 100 条
# 之类的，还有就是 app 有清空按钮不是么，按下后之前的聊天记录就不需要了」）。
# 所以投影只看线程末尾这么多轮：更早的轮次既不补发，也不参与"哪些已发过"的比对。
MAX_CODEX_PROJECTED_TURNS = 100
# 只是防畸形载荷的天花板，**不再**是"轮次太多就整条拒收"那道墙（那样约 80 天后
# 又会以同一形态停摆：一条恒定增长的线程总会越过任何固定轮次上限）。
MAX_CODEX_TURNS = 200_000
# 结构化源读失败后的退避。期间走连续性文件那条路继续发 —— 但别每 0.75s 重读一次
# 整条线程：app-server 每次都要整读磁盘上的 rollout（实测 157 MiB / 2.2s）。
STRUCTURED_READ_FAILURE_COOLDOWN_SECONDS = 60.0
MAX_CODEX_ITEMS_PER_TURN = 2_000
MAX_PROJECTED_TOOLS = 64
MAX_STRUCTURED_RECOVERY_REQUESTS = 32
MAX_STRUCTURED_PUBLISH_PER_POLL = 8
MAX_PUBLISHED = MAX_ARCHIVE_ITEMS // 2
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
SNAPSHOT_ANCHOR_MAX_AGE = timedelta(minutes=3)
HISTORY_POLL_SECONDS = 0.75
FINAL_TAIL_POLLS = 3
OFFLINE_EXIT_POLLS = 40
PUBLISH_FAILURE_BACKOFF_POLLS = 20
STRUCTURED_HISTORY_FOLLOWUP_POLLS = 2
CODEX_APP_SERVER_TIMEOUT_SECONDS = 6.0
ERROR_ALREADY_EXISTS = 183
READER_SYNC_MARKERS = ("[[READER_SYNC]]", "[[/READER_SYNC]]")

Publisher = Callable[..., dict[str, Any]]
StatusProvider = Callable[[], Any]


class CodexAppServerError(RuntimeError):
    """The local read-only Codex app-server request could not complete."""


class CodexAppServerHistoryClient:
    """Small persistent read-only client for ``thread/read``.

    The child exists only during an owned capture lease.  Its stderr is
    discarded because it may contain private thread data, and no response
    content is written to ReaderPC logs.
    """

    def __init__(
        self,
        *,
        executable: Path | None = None,
        timeout_seconds: float = CODEX_APP_SERVER_TIMEOUT_SECONDS,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        self._initialized = False
        # 上一次成功响应的字节数。调用方据此决定重读节奏 —— 线程越大越不该频繁整读。
        self.last_response_bytes = 0

    @staticmethod
    def find_executable() -> Path | None:
        override = os.environ.get("BW_CODEX_APP_SERVER_EXECUTABLE")
        if override:
            candidate = Path(override)
            if candidate.is_absolute() and candidate.is_file():
                return candidate
            return None
        appdata = Path(os.environ.get("APPDATA") or "")
        package_root = (
            appdata
            / "npm"
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
        )
        candidates = (
            package_root
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe",
            package_root
            / "@openai"
            / "codex-win32-arm64"
            / "vendor"
            / "aarch64-pc-windows-msvc"
            / "bin"
            / "codex.exe",
        )
        return next((path for path in candidates if path.is_file()), None)

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        thread_id = _canonical_uuid(thread_id, "app-server.threadId")
        self._ensure_started()
        return self._request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
        )

    def close(self) -> None:
        process = self._process
        self._process = None
        self._initialized = False
        self._stdout = queue.Queue()
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _ensure_started(self) -> None:
        if (
            self._process is not None
            and self._process.poll() is None
            and self._initialized
        ):
            return
        self.close()
        executable = self.executable or self.find_executable()
        if executable is None:
            raise CodexAppServerError("Codex app-server executable unavailable")
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0
        )
        try:
            self._process = subprocess.Popen(
                [str(executable), "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise CodexAppServerError("Codex app-server start failed") from exc
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        threading.Thread(
            target=self._drain_stdout,
            args=(self._process.stdout, self._stdout),
            name="readerpc-codex-history-out",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._drain_stderr,
            args=(self._process.stderr,),
            name="readerpc-codex-history-err",
            daemon=True,
        ).start()
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "bw_reader_voice_history",
                    "title": "BW Reader Voice History",
                    "version": "2.0.0",
                }
            },
        )
        self._write({"method": "initialized"})
        self._initialized = True

    @staticmethod
    def _drain_stdout(
        stream: Any,
        destination: queue.Queue[str | None],
    ) -> None:
        try:
            for line in stream:
                destination.put(line)
        except (OSError, UnicodeError):
            pass
        finally:
            destination.put(None)

    @staticmethod
    def _drain_stderr(stream: Any) -> None:
        try:
            for _ in stream:
                pass
        except (OSError, UnicodeError):
            pass

    def _write(self, value: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise CodexAppServerError("Codex app-server is not running")
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        try:
            process.stdin.write(raw + "\n")
            process.stdin.flush()
        except (OSError, UnicodeError) as exc:
            raise CodexAppServerError("Codex app-server write failed") from exc

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._write({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise CodexAppServerError("Codex app-server response timed out")
            try:
                line = self._stdout.get(timeout=remaining)
            except queue.Empty as exc:
                self.close()
                raise CodexAppServerError(
                    "Codex app-server response timed out"
                ) from exc
            if line is None:
                self.close()
                raise CodexAppServerError("Codex app-server closed")
            raw = line.encode("utf-8")
            # ⚠ 三种情况必须分开说。合成一句 "response invalid" 让 2026-09-04 那次
            #   排查完全无从下手:日志上看不出是空行、超长、还是 JSON 坏了,
            #   而真相(35.3 MB > 32 MB 上限)只要报出字节数就一眼可见。
            if not raw.strip():
                self.close()
                raise CodexAppServerError("Codex app-server response blank")
            if len(raw) > MAX_CODEX_RESPONSE_BYTES:
                self.close()
                raise CodexAppServerError(
                    "Codex app-server response too large: %d bytes > %d cap"
                    % (len(raw), MAX_CODEX_RESPONSE_BYTES)
                )
            try:
                response = _decode_json(raw, "app-server-response")
            except SyncDataError as exc:
                self.close()
                raise CodexAppServerError(
                    "Codex app-server response undecodable at %d bytes"
                    % len(raw)
                ) from exc
            self.last_response_bytes = len(raw)
            if not isinstance(response, dict) or "id" not in response:
                continue
            if response.get("id") != request_id:
                self.close()
                raise CodexAppServerError("Codex app-server response mismatch")
            if "error" in response or not isinstance(response.get("result"), dict):
                self.close()
                raise CodexAppServerError("Codex app-server request failed")
            return response["result"]


class SyncDataError(ValueError):
    """A local input or durable state violated its bounded schema."""


def _contains_reader_sync_marker(text: str) -> bool:
    return any(marker in text for marker in READER_SYNC_MARKERS)


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SyncDataError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _reject_constant(value: str) -> None:
    raise SyncDataError(f"non-finite JSON number: {value}")


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        text = raw.decode("utf-8-sig", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except SyncDataError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncDataError(f"{label}: invalid UTF-8 JSON") from exc


def _read_bounded_json(path: Path, limit: int, label: str) -> Any:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise SyncDataError(f"{label}: unreadable") from exc
    if len(raw) > limit:
        raise SyncDataError(f"{label}: exceeds {limit} bytes")
    return _decode_json(raw, label)


def _exact_fields(
    value: dict[str, Any],
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        raise SyncDataError(
            f"{label}: schema mismatch"
            f" missing={sorted(missing)} extra={sorted(extra)}"
        )


def _canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise SyncDataError(f"{label}: UUID must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SyncDataError(f"{label}: invalid UUID") from exc
    canonical = str(parsed)
    if parsed.int == 0 or value != canonical:
        raise SyncDataError(f"{label}: UUID must be canonical and non-zero")
    return canonical


def _bounded_codex_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (
        not value
        or len(value) > limit
        or "\x00" in value
        or _contains_reader_sync_marker(value)
    ):
        return None
    return value


def _extract_realtime_input(value: str) -> str | None:
    """Return the user utterance from the local realtime delegation shell."""
    stripped = value.strip()
    if not stripped.startswith("<realtime_delegation>"):
        return stripped
    start = stripped.find("<input>")
    if start < 0:
        return None
    start += len("<input>")
    end = stripped.find("</input>", start)
    if end < start:
        return None
    return html.unescape(stripped[start:end]).strip()


def _codex_user_text(item: dict[str, Any]) -> str | None:
    content = item.get("content")
    if not isinstance(content, list) or len(content) > 256:
        return None
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = _bounded_codex_text(
            part.get("text"),
            limit=MAX_CODEX_TEXT_CHARS,
        )
        if text is not None:
            parts.append(text)
    combined = _extract_realtime_input("\n".join(parts))
    if combined is None:
        return None
    return _bounded_codex_text(combined, limit=MAX_CODEX_TEXT_CHARS)


def _codex_request_id(
    thread_id: str,
    turn_id: str,
    item_id: str,
    item_index: int,
    user_text: str,
) -> str:
    digest = hashlib.sha256(
        (
            thread_id
            + "\x00"
            + turn_id
            + "\x00"
            + item_id
            + "\x00"
            + str(item_index)
            + "\x00"
            + user_text
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"vh2:{thread_id[:8]}:{digest}"


def _tool_status(value: Any) -> str:
    if value in {"completed", "complete", "done", "success", "succeeded"}:
        return "done"
    if value in {"failed", "error"}:
        return "error"
    if value in {"cancelled", "canceled", "aborted"}:
        return "aborted"
    return "running"


def _safe_tool_name(value: Any, fallback: str) -> str:
    text = _bounded_codex_text(value, limit=160)
    if text is None:
        return fallback
    return "".join(
        char if char.isalnum() or char in "._:-" else "-"
        for char in text
    )[:160]


_READER_TOOL_LABELS = {
    "reader_context_snapshot": "读取页面",
    "reader_visual_image": "看页面图",
    "reader_browser_control": "操作阅读页面",
    "reader_highlight_text": "高亮",
    "reader_highlight_range": "高亮",
    "reader_anki_draft": "制卡",
    "reader_card": "显示 Reader 卡片",
    "reader_command": "执行 Reader 命令",
    "reader_undo_last": "撤销",
    "reader_note_create": "新建便签",
    "reader_note_edit": "编辑便签",
    "reader_page_cards": "查看页面卡片",
    "reader_page_card_read": "读取卡片内容",
    "reader_page_card_edit": "修改卡片",
    "reader_page_card_delete": "删除卡片",
    "reader_highlights": "看高亮",
    "reader_notes": "看便签",
    "reader_search": "搜索全书",
    "reader_toc": "查目录",
    "reader_page_text": "读取页面文字",
    "reader_make_note": "整理笔记",
    "reader_lookup_word": "查词典",
    "reader_mark_vocab": "加生词本",
    "reader_web_highlight": "高亮网页文字",
    "reader_web_note": "创建网页便签",
    "reader_capability_guide": "查看 Reader 能力指南",
}


def _tool_label(server: str, tool: str, fallback: str) -> str:
    # Keep protocol identities stable.  The synchronized Reader sidebar is a
    # presentation surface, so known Reader tools get the same concise labels
    # as Realtime while unknown MCP tools retain their exact technical name.
    if server in {"reader", "reader_snapshot"}:
        label = _READER_TOOL_LABELS.get(tool)
        if label is not None:
            return label
    return f"工具：{fallback}"


def _project_tool(item: dict[str, Any]) -> dict[str, str | None] | None:
    item_type = item.get("type")
    if item_type == "mcpToolCall":
        server = _safe_tool_name(item.get("server"), "mcp")
        tool = _safe_tool_name(item.get("tool"), "tool")
        name = f"{server}.{tool}"[:160]
        status = _tool_status(item.get("status"))
        duration = item.get("durationMs")
        detail = (
            "完成" if status == "done" else
            "已取消" if status == "aborted" else
            "执行失败" if status == "error" else
            "执行中"
        )
        if (
            not isinstance(duration, bool)
            and isinstance(duration, (int, float))
            and 0 <= duration <= 86_400_000
        ):
            detail += f" · {round(duration)} ms"
        return {
            "status": status,
            "tool": name,
            "label": _tool_label(server, tool, name)[:320],
            "detail": detail,
        }
    if item_type == "webSearch":
        query_text = _bounded_codex_text(item.get("query"), limit=240)
        results = item.get("results")
        count = len(results) if isinstance(results, list) else None
        detail = "搜索完成"
        if count is not None:
            detail += f" · {count} 个结果"
        return {
            "status": "done",
            "tool": "web.search",
            "label": (
                f"网页搜索：{query_text}" if query_text else "网页搜索"
            )[:320],
            "detail": detail,
        }
    if item_type == "commandExecution":
        status = _tool_status(item.get("status"))
        return {
            "status": status,
            "tool": "local.command",
            "label": "本地命令",
            "detail": (
                "完成" if status == "done" else
                "已取消" if status == "aborted" else
                "执行失败" if status == "error" else
                "执行中"
            ),
        }
    return None


def _project_codex_thread(
    result: Any,
    expected_thread_id: str,
) -> dict[str, Any]:
    """Project one authoritative Codex thread into completed user turns.

    Commentary and reasoning are intentionally ignored.  A segment becomes
    publishable only after an ``agentMessage`` explicitly declares
    ``phase=final_answer``.
    """
    expected_thread_id = _canonical_uuid(
        expected_thread_id,
        "app-server.expectedThreadId",
    )
    if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
        raise SyncDataError("app-server: thread result missing")
    thread = result["thread"]
    if thread.get("id") != expected_thread_id:
        raise SyncDataError("app-server: thread identity mismatch")
    turns = thread.get("turns")
    if not isinstance(turns, list) or len(turns) > MAX_CODEX_TURNS:
        raise SyncDataError("app-server: turns invalid")
    # 只投影线程**末尾** MAX_CODEX_PROJECTED_TURNS 轮。
    #
    # ⚠ `turn_index` 必须是**绝对**下标：没有 id 的轮次拿 `turn-<index>` 当身份，
    #   切片后从 0 重新数会让同一轮的 requestId 随线程增长而变 → 同一轮被重复发。
    index_offset = max(0, len(turns) - MAX_CODEX_PROJECTED_TURNS)
    if index_offset:
        turns = turns[index_offset:]

    seen_request_ids: list[str] = []
    segments: list[dict[str, Any]] = []
    for offset_index, turn in enumerate(turns):
        turn_index = index_offset + offset_index
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if (
            not isinstance(items, list)
            or len(items) > MAX_CODEX_ITEMS_PER_TURN
        ):
            continue
        raw_turn_id = turn.get("id")
        turn_id = (
            raw_turn_id
            if isinstance(raw_turn_id, str) and 0 < len(raw_turn_id) <= 256
            else f"turn-{turn_index}"
        )
        current: dict[str, Any] | None = None
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                user_text = _codex_user_text(item)
                if user_text is None:
                    current = None
                    continue
                raw_item_id = item.get("id")
                item_id = (
                    raw_item_id
                    if isinstance(raw_item_id, str) and 0 < len(raw_item_id) <= 256
                    else f"user-{item_index}"
                )
                request_id = _codex_request_id(
                    expected_thread_id,
                    turn_id,
                    item_id,
                    item_index,
                    user_text,
                )
                seen_request_ids.append(request_id)
                current = {
                    "requestId": request_id,
                    "user": user_text,
                    "tools": [],
                }
                continue
            if current is None:
                continue
            projected_tool = _project_tool(item)
            if projected_tool is not None:
                if len(current["tools"]) < MAX_PROJECTED_TOOLS:
                    current["tools"].append(projected_tool)
                continue
            if item_type != "agentMessage" or item.get("phase") != "final_answer":
                continue
            assistant = _bounded_codex_text(
                item.get("text"),
                limit=MAX_CODEX_TEXT_CHARS,
            )
            if assistant is not None:
                segments.append(
                    {
                        **current,
                        "assistant": assistant,
                    }
                )
            current = None
    return {
        "threadId": expected_thread_id,
        "seenRequestIds": seen_request_ids,
        "segments": segments,
    }


def _history_match_text(value: str, *, user: bool) -> str:
    """Normalize only enough text to align old Voice and Codex records.

    The old continuity file stores the spoken user text while ``thread/read``
    stores the enclosing realtime delegation.  Likewise, the authoritative
    final answer can carry the transport-only ``[COMPLETE]`` marker.  This
    helper is used solely to locate an acknowledged recovery boundary; the
    original strings are always what gets published.
    """
    text = value.strip()
    if user:
        extracted = _extract_realtime_input(text)
        if extracted is not None:
            text = extracted
    elif text.casefold().startswith("[complete]"):
        text = text[len("[COMPLETE]") :]
    return " ".join(text.split())


def _structured_recovery_baseline(
    *,
    projection: dict[str, Any],
    state: dict[str, Any],
    archive: dict[str, Any],
    thread_id: str,
) -> tuple[set[str], int]:
    """Choose a bounded, fail-closed replay window for one active thread.

    Exact ``vh2`` acknowledgements are the strongest cursor.  Installations
    upgraded from the continuity-based synchronizer only have ``vh`` ids, so
    the acknowledged archive pair supplies a one-time content boundary.  No
    durable acknowledgement means no recovery replay: the current thread is
    treated as the activation baseline exactly as before.
    """
    seen_ids = list(projection["seenRequestIds"])
    segments = list(projection["segments"])
    published = state["published"]
    seen_indexes = {
        request_id: index for index, request_id in enumerate(seen_ids)
    }
    recovery_starts: list[int] = []

    exact_indexes = [
        seen_indexes[request_id]
        for request_id in seen_ids
        if request_id in published
    ]
    if exact_indexes:
        recovery_starts.append(max(exact_indexes) + 1)

    archived_thread = archive["threads"].get(thread_id)
    if archived_thread is not None:
        acknowledged_pairs = [
            pair
            for pair in _pairs(thread_id, archived_thread)
            if pair["requestId"] in published
        ]
        segment_by_id = {
            segment["requestId"]: segment for segment in segments
        }
        # Prefer a strong user+final-answer boundary.  The newest exact match
        # is safe because both halves were acknowledged by the old worker.
        for pair in reversed(acknowledged_pairs):
            user_key = _history_match_text(pair["user"], user=True)
            assistant_key = _history_match_text(
                pair["assistant"], user=False
            )
            exact = [
                request_id
                for request_id in reversed(seen_ids)
                if request_id in segment_by_id
                and _history_match_text(
                    segment_by_id[request_id]["user"], user=True
                ) == user_key
                and _history_match_text(
                    segment_by_id[request_id]["assistant"], user=False
                ) == assistant_key
            ]
            if exact:
                recovery_starts.append(seen_indexes[exact[0]] + 1)
                break
        else:
            # A legacy commentary answer ("I will check") has no exact
            # authoritative final.  Repeated user prompts make the newest
            # user-only match unsafe: it could be a later unacknowledged turn.
            # Start at the earliest matching occurrence, accepting bounded
            # duplication rather than skipping a completed answer.
            for pair in reversed(acknowledged_pairs):
                user_key = _history_match_text(pair["user"], user=True)
                fuzzy = [
                    request_id
                    for request_id in seen_ids
                    if request_id in segment_by_id
                    and _history_match_text(
                        segment_by_id[request_id]["user"], user=True
                    ) == user_key
                ]
                if fuzzy:
                    recovery_starts.append(seen_indexes[fuzzy[0]])
                    break

    if not recovery_starts:
        return set(seen_ids), 0

    recovery_start = max(recovery_starts)
    recovery_ids = [
        request_id
        for request_id in seen_ids[recovery_start:]
        if request_id not in published
    ][-MAX_STRUCTURED_RECOVERY_REQUESTS:]
    recovery_set = set(recovery_ids)
    completed = sum(
        segment["requestId"] in recovery_set
        for segment in segments
    )
    return set(seen_ids) - recovery_set, completed


def _normalize_item(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SyncDataError(f"{label}: item must be an object")
    _exact_fields(value, {"role", "text"}, set(), label)
    role = value["role"]
    text = value["text"]
    if role not in {"user", "assistant"}:
        raise SyncDataError(f"{label}: unsupported role")
    if not isinstance(text, str):
        raise SyncDataError(f"{label}: text must be a string")
    text = text.strip()
    if not text or len(text) > MAX_TEXT_CHARS or "\x00" in text:
        raise SyncDataError(f"{label}: invalid text")
    if _contains_reader_sync_marker(text):
        raise SyncDataError(f"{label}: Reader sync payload is forbidden")
    return {"role": role, "text": text}


def _parse_binding(root: Any) -> tuple[str, dict[str, str] | None]:
    """Return ``bound``, ``unbound``, or raise for a malformed binding.

    Top-level Electron state is intentionally extensible, and so is the
    selected atom: it is Codex's own record, and every app update may add a
    field (2026-09-06 the stable app added ``isEverydayWorkMode`` and the old
    exact-schema check silently pinned the sidebar to a stale Beta thread).
    Only the two fields selection depends on are required and type-checked;
    unknown fields are ignored.
    """
    if not isinstance(root, dict):
        raise SyncDataError("global-state: root must be an object")
    persisted = root.get("electron-persisted-atom-state")
    if persisted is None:
        return "unbound", None
    if not isinstance(persisted, dict):
        raise SyncDataError("global-state: persisted atom must be an object")
    recent = persisted.get(RECENT_THREAD_KEY)
    if recent is None:
        return "unbound", None
    if not isinstance(recent, dict):
        raise SyncDataError("global-state: recent thread must be an object")
    missing = {"conversationId", "hostId"} - set(recent)
    if missing:
        raise SyncDataError(
            "global-state.recent-thread: schema mismatch"
            f" missing={sorted(missing)}"
        )
    if "version" in recent and (
        isinstance(recent["version"], bool)
        or not isinstance(recent["version"], int)
        or recent["version"] < 0
    ):
        raise SyncDataError("global-state: invalid binding version")
    host_id = recent["hostId"]
    if not isinstance(host_id, str):
        raise SyncDataError("global-state: hostId must be a string")
    thread_id = _canonical_uuid(
        recent["conversationId"], "global-state.conversationId"
    )
    if host_id != "local":
        return "unbound", None
    return "bound", {"conversationId": thread_id, "hostId": "local"}


def _parse_continuity(root: Any, selected_thread: str) -> list[dict[str, str]]:
    if not isinstance(root, dict):
        raise SyncDataError("continuity: root must be an object")
    _exact_fields(root, {"version", "threads"}, set(), "continuity")
    if (
        isinstance(root["version"], bool)
        or not isinstance(root["version"], int)
        or root["version"] != 1
    ):
        raise SyncDataError("continuity: version must be 1")
    threads = root["threads"]
    if not isinstance(threads, dict) or len(threads) > MAX_THREADS:
        raise SyncDataError("continuity: invalid threads object")

    selected: list[dict[str, str]] = []
    for raw_thread_id, thread in threads.items():
        thread_id = _canonical_uuid(raw_thread_id, "continuity.threadId")
        if not isinstance(thread, dict):
            raise SyncDataError(f"continuity.threads.{thread_id}: not an object")
        _exact_fields(
            thread,
            {"items"},
            set(),
            f"continuity.threads.{thread_id}",
        )
        items = thread["items"]
        if not isinstance(items, list) or len(items) > MAX_RECENT_ITEMS:
            raise SyncDataError(
                f"continuity.threads.{thread_id}: invalid items"
            )
        normalized = [
            _normalize_item(item, f"continuity.threads.{thread_id}.items[{i}]")
            for i, item in enumerate(items)
        ]
        if thread_id == selected_thread:
            selected = normalized
    return selected


def _default_state() -> dict[str, Any]:
    return {
        "version": VERSION,
        "lastGood": None,
        "published": {},
    }


def _default_archive() -> dict[str, Any]:
    return {"version": VERSION, "threads": {}}


def _validate_last_good(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SyncDataError("state.lastGood: must be null or an object")
    _exact_fields(
        value,
        {"binding", "threadId", "items"},
        set(),
        "state.lastGood",
    )
    binding = value["binding"]
    if not isinstance(binding, dict):
        raise SyncDataError("state.lastGood.binding: must be an object")
    _exact_fields(
        binding,
        {"conversationId", "hostId"},
        set(),
        "state.lastGood.binding",
    )
    thread_id = _canonical_uuid(value["threadId"], "state.lastGood.threadId")
    if (
        binding["conversationId"] != thread_id
        or binding["hostId"] != "local"
    ):
        raise SyncDataError("state.lastGood: binding mismatch")
    items = value["items"]
    if not isinstance(items, list) or len(items) > MAX_RECENT_ITEMS:
        raise SyncDataError("state.lastGood.items: invalid list")
    normalized = [
        _normalize_item(item, f"state.lastGood.items[{i}]")
        for i, item in enumerate(items)
    ]
    return {
        "binding": {"conversationId": thread_id, "hostId": "local"},
        "threadId": thread_id,
        "items": normalized,
    }


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SyncDataError("state: root must be an object")
    _exact_fields(value, {"version", "lastGood", "published"}, set(), "state")
    if value["version"] != VERSION:
        raise SyncDataError("state: unsupported version")
    published = value["published"]
    if not isinstance(published, dict):
        raise SyncDataError("state.published: invalid object")
    clean_published: dict[str, bool] = {}
    for request_id, marker in published.items():
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 64
            or marker is not True
        ):
            raise SyncDataError("state.published: invalid entry")
        clean_published[request_id] = True
    # Older workers could write the (MAX_PUBLISHED + 1)th acknowledgement and
    # then reject their own state forever on the next poll.  The file itself is
    # already bounded by MAX_STATE_BYTES, so validate every entry first and
    # retain only the newest bounded window to self-heal that state.
    if len(clean_published) > MAX_PUBLISHED:
        clean_published = dict(
            list(clean_published.items())[-MAX_PUBLISHED:]
        )
    return {
        "version": VERSION,
        "lastGood": _validate_last_good(value["lastGood"]),
        "published": clean_published,
    }


def _remember_published(state: dict[str, Any], request_id: str) -> None:
    """Append one ACK while keeping the durable dedupe window bounded."""
    published = state["published"]
    if request_id in published:
        return
    while len(published) >= MAX_PUBLISHED:
        del published[next(iter(published))]
    published[request_id] = True


def _validate_archive(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SyncDataError("archive: root must be an object")
    _exact_fields(value, {"version", "threads"}, set(), "archive")
    if value["version"] != VERSION:
        raise SyncDataError("archive: unsupported version")
    threads = value["threads"]
    if not isinstance(threads, dict) or len(threads) > MAX_THREADS:
        raise SyncDataError("archive.threads: invalid object")

    total_items = 0
    clean_threads: dict[str, Any] = {}
    for raw_thread_id, thread in threads.items():
        thread_id = _canonical_uuid(raw_thread_id, "archive.threadId")
        if not isinstance(thread, dict):
            raise SyncDataError(f"archive.threads.{thread_id}: not an object")
        _exact_fields(
            thread, {"items", "gaps"}, set(), f"archive.threads.{thread_id}"
        )
        items = thread["items"]
        if not isinstance(items, list):
            raise SyncDataError(f"archive.threads.{thread_id}.items: invalid")
        total_items += len(items)
        if total_items > MAX_ARCHIVE_ITEMS:
            raise SyncDataError("archive: too many items")
        clean_items = [
            _normalize_item(item, f"archive.threads.{thread_id}.items[{i}]")
            for i, item in enumerate(items)
        ]
        gaps = thread["gaps"]
        if not isinstance(gaps, list):
            raise SyncDataError(f"archive.threads.{thread_id}.gaps: invalid")
        clean_gaps: list[int] = []
        prior = 0
        for gap in gaps:
            if (
                isinstance(gap, bool)
                or not isinstance(gap, int)
                or gap <= 0
                or gap >= len(clean_items)
                or gap <= prior
            ):
                raise SyncDataError(
                    f"archive.threads.{thread_id}.gaps: invalid boundary"
                )
            clean_gaps.append(gap)
            prior = gap
        clean_threads[thread_id] = {
            "items": clean_items,
            "gaps": clean_gaps,
        }
    return {"version": VERSION, "threads": clean_threads}


def _load_durable(
    path: Path,
    limit: int,
    label: str,
    default: dict[str, Any],
    validator: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if not path.exists():
        return default
    return validator(_read_bounded_json(path, limit, label))


def _atomic_write_json(path: Path, value: dict[str, Any], limit: int) -> None:
    raw = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(raw) > limit:
        raise SyncDataError(f"{path.name}: serialized data exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _overlap(
    archived: list[dict[str, str]], recent: list[dict[str, str]]
) -> int:
    for size in range(min(len(archived), len(recent)), 0, -1):
        if archived[-size:] == recent[:size]:
            return size
    return 0


def _merge_recent(
    thread: dict[str, Any], recent: list[dict[str, str]]
) -> bool:
    archived = thread["items"]
    if not recent:
        return False
    if not archived:
        archived.extend(recent)
        return False
    overlap = _overlap(archived, recent)
    if overlap == 0:
        thread["gaps"].append(len(archived))
    archived.extend(recent[overlap:])
    if len(archived) > MAX_ARCHIVE_ITEMS:
        raise SyncDataError("archive: item limit reached")
    return overlap == 0


def _request_id(
    thread_id: str, assistant_index: int, user: str, assistant: str
) -> str:
    digest = hashlib.sha256(
        (user + "\x00" + assistant).encode("utf-8")
    ).hexdigest()[:12]
    request_id = f"vh:{thread_id}:{assistant_index}:{digest}"
    if len(request_id) > 64:
        raise SyncDataError("internal request_id exceeds bridge limit")
    return request_id


def _pairs(thread_id: str, thread: dict[str, Any]) -> list[dict[str, Any]]:
    items = thread["items"]
    boundaries = set(thread["gaps"])
    pairs: list[dict[str, Any]] = []
    for index in range(1, len(items)):
        if index in boundaries:
            continue
        user = items[index - 1]
        assistant = items[index]
        if user["role"] != "user" or assistant["role"] != "assistant":
            continue
        pairs.append(
            {
                "requestId": _request_id(
                    thread_id, index, user["text"], assistant["text"]
                ),
                "assistantIndex": index,
                "user": user["text"],
                "assistant": assistant["text"],
            }
        )
    return pairs


def _status(
    *,
    thread_id: str | None,
    items: int = 0,
    pairs: int = 0,
    gap: bool = False,
    pending: int = 0,
    published: int = 0,
    dry_run: bool = True,
    stale: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "threadId": thread_id,
        "items": items,
        "pairs": pairs,
        "gap": gap,
        "pending": pending,
        "published": published,
        "dryRun": dry_run,
        "stale": stale,
    }
    if error:
        out["error"] = error
    return out


def sync_once(
    *,
    global_state_path: Path,
    continuity_path: Path,
    state_path: Path,
    archive_path: Path,
    publish: bool = False,
    publisher: Publisher | None = None,
    file: str | None = None,
    page: int | str | None = None,
    source_instance_id: str | None = None,
    snapshot_revision: int | None = None,
    expected_thread_id: str | None = None,
    minimum_assistant_index: int = 1,
    allow_last_good: bool = True,
) -> dict[str, Any]:
    """Perform one bounded poll/archive/publish cycle.

    ``publisher`` has the same keyword contract as ``bridge_client.call`` and
    is injected by tests.  A false ``ok`` result or exception stops ordered
    publication; it never advances durable published state.  Capture-bound
    callers additionally pin ``expected_thread_id`` and set a per-call item
    watermark through ``minimum_assistant_index``; standalone archival keeps
    the historical defaults.
    """
    if (
        isinstance(minimum_assistant_index, bool)
        or not isinstance(minimum_assistant_index, int)
        or minimum_assistant_index < 1
    ):
        raise ValueError("minimum_assistant_index must be a positive integer")
    if expected_thread_id is not None:
        expected_thread_id = _canonical_uuid(
            expected_thread_id, "expected_thread_id"
        )
    try:
        state = _load_durable(
            state_path,
            MAX_STATE_BYTES,
            "state",
            _default_state(),
            _validate_state,
        )
        archive = _load_durable(
            archive_path,
            MAX_ARCHIVE_BYTES,
            "archive",
            _default_archive(),
            _validate_archive,
        )
    except SyncDataError as exc:
        return _status(
            thread_id=None,
            dry_run=not publish,
            gap=True,
            stale=True,
            error=str(exc),
        )

    stale = False
    errors: list[str] = []
    binding: dict[str, str] | None
    try:
        binding_status, binding = _parse_binding(
            _read_bounded_json(
                global_state_path,
                MAX_GLOBAL_STATE_BYTES,
                "global-state",
            )
        )
    except SyncDataError as exc:
        if not allow_last_good:
            return _status(
                thread_id=None,
                dry_run=not publish,
                gap=True,
                stale=True,
                error=str(exc),
            )
        last_good = state["lastGood"]
        if last_good is None:
            return _status(
                thread_id=None,
                dry_run=not publish,
                gap=True,
                stale=True,
                error=str(exc),
            )
        binding_status = "bound"
        binding = dict(last_good["binding"])
        stale = True
        errors.append(str(exc))

    if binding_status == "unbound" or binding is None:
        return _status(thread_id=None, dry_run=not publish)

    thread_id = binding["conversationId"]
    if expected_thread_id is not None and thread_id != expected_thread_id:
        return _status(
            thread_id=expected_thread_id,
            dry_run=not publish,
            gap=True,
            stale=True,
            error="active capture thread lease changed",
        )
    try:
        recent = _parse_continuity(
            _read_bounded_json(
                continuity_path,
                MAX_CONTINUITY_BYTES,
                "continuity",
            ),
            thread_id,
        )
        state["lastGood"] = {
            "binding": binding,
            "threadId": thread_id,
            "items": recent,
        }
    except SyncDataError as exc:
        if not allow_last_good:
            return _status(
                thread_id=thread_id,
                dry_run=not publish,
                gap=True,
                stale=True,
                error=str(exc),
            )
        last_good = state["lastGood"]
        if last_good is not None and last_good["threadId"] == thread_id:
            recent = list(last_good["items"])
        else:
            recent = []
        stale = True
        errors.append(str(exc))

    thread = archive["threads"].setdefault(
        thread_id, {"items": [], "gaps": []}
    )
    try:
        new_gap = _merge_recent(thread, recent)
        if sum(
            len(entry["items"]) for entry in archive["threads"].values()
        ) > MAX_ARCHIVE_ITEMS:
            raise SyncDataError("archive: total item limit reached")
        _atomic_write_json(archive_path, archive, MAX_ARCHIVE_BYTES)
        _atomic_write_json(state_path, state, MAX_STATE_BYTES)
    except (OSError, SyncDataError) as exc:
        return _status(
            thread_id=thread_id,
            items=len(thread["items"]),
            gap=True,
            dry_run=not publish,
            stale=True,
            error=f"durable-write: {exc}",
        )

    pairs = _pairs(thread_id, thread)
    pending = [
        pair
        for pair in pairs
        if (
            pair["assistantIndex"] >= minimum_assistant_index
            and pair["requestId"] not in state["published"]
        )
    ]
    published_count = 0
    publish_error: str | None = None
    if publish and pending:
        if publisher is None:
            publish_error = "publisher unavailable"
        else:
            for pair in pending:
                payload = {
                    "text": pair["assistant"],
                    "user_utterance": pair["user"],
                }
                try:
                    publish_kwargs: dict[str, Any] = {
                        "request_id": pair["requestId"],
                        "file": file,
                        "page": page,
                    }
                    if source_instance_id is not None:
                        publish_kwargs["source_instance_id"] = source_instance_id
                        publish_kwargs["thread_id"] = thread_id
                    if snapshot_revision is not None:
                        publish_kwargs["snapshot_revision"] = snapshot_revision
                    result = publisher(
                        "assistant_turn",
                        payload,
                        **publish_kwargs,
                    )
                    if not isinstance(result, dict) or result.get("ok") is not True:
                        publish_error = "publisher rejected assistant_turn"
                        break
                except Exception as exc:  # publisher defines its own error type
                    publish_error = f"publisher failed: {type(exc).__name__}"
                    break
                _remember_published(state, pair["requestId"])
                published_count += 1
                try:
                    _atomic_write_json(state_path, state, MAX_STATE_BYTES)
                except (OSError, SyncDataError) as exc:
                    publish_error = f"state-write-after-publish: {exc}"
                    break

    remaining = sum(
        pair["assistantIndex"] >= minimum_assistant_index
        and pair["requestId"] not in state["published"]
        for pair in pairs
    )
    all_errors = errors + ([publish_error] if publish_error else [])
    return _status(
        thread_id=thread_id,
        items=len(thread["items"]),
        pairs=len(pairs),
        gap=bool(thread["gaps"]) or new_gap or stale,
        pending=remaining,
        published=published_count,
        dry_run=not publish,
        stale=stale,
        error="; ".join(all_errors) if all_errors else None,
    )


def _default_paths() -> tuple[Path, Path, Path, Path]:
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    codex = profile / ".codex"
    return (
        codex / ".codex-global-state.json",
        codex / "realtime-voice-continuity.json",
        codex / "voice-history-sidebar-sync-state.json",
        codex / "voice-history-sidebar-sync-archive.json",
    )


def _bridge_publisher(*args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        from sidebar_bridge_client import call
    except ImportError:
        # The standalone scripts/ wrapper remains useful in source checkouts.
        from bridge_client import call

    return call(*args, **kwargs)


def snapshot_anchor(
    snapshot_path: Path,
    *,
    now: datetime | None = None,
) -> tuple[str | None, int | None]:
    """Read only a safe current-page anchor from the Windows snapshot.

    Failure is intentionally non-fatal for archival-only callers. No text,
    drawing, selection, or other snapshot content is forwarded through this
    path.
    """
    try:
        root = _read_bounded_json(
            snapshot_path,
            MAX_SNAPSHOT_BYTES,
            "reader-snapshot",
        )
    except SyncDataError:
        return None, None
    if (
        not isinstance(root, dict)
        or root.get("schema") != "reader-context-snapshot/1"
        or root.get("contextStatus") != "ready"
    ):
        return None, None
    raw_updated = root.get("updatedAtUtc")
    if not isinstance(raw_updated, str):
        return None, None
    try:
        updated = datetime.fromisoformat(raw_updated)
    except ValueError:
        return None, None
    if updated.tzinfo is None:
        return None, None
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        return None, None
    age = checked_at.astimezone(timezone.utc) - updated.astimezone(timezone.utc)
    if age < timedelta(0) or age > SNAPSHOT_ANCHOR_MAX_AGE:
        return None, None
    active = root.get("activeReading")
    if (
        not isinstance(active, dict)
        or active.get("fresh") is not True
        or isinstance(active.get("ageSec"), bool)
        or not isinstance(active.get("ageSec"), (int, float))
        or active["ageSec"] < 0
        or active["ageSec"] > SNAPSHOT_ANCHOR_MAX_AGE.total_seconds()
    ):
        return None, None
    current = root.get("currentPage")
    if (
        not isinstance(current, dict)
        or current.get("stable") is not True
    ):
        return None, None
    file = current.get("file")
    page = current.get("page")
    if (
        not isinstance(file, str)
        or not file
        or len(file) > 1024
        or file.startswith(("/", "\\"))
        or "\x00" in file
        or ":" in file
        or ".." in file.replace("\\", "/").split("/")
        or isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
    ):
        return None, None
    return file, page


def snapshot_output_identity(
    snapshot_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the exact online Reader source used by the local WSS router.

    Unlike the historical Pi anchor, ``file`` may be an HTTPS URL and page 0
    is valid for an ordinary browser tab.  No page text or drawing data leaves
    the snapshot file through this helper.
    """
    try:
        root = _read_bounded_json(
            snapshot_path,
            MAX_SNAPSHOT_BYTES,
            "reader-snapshot",
        )
    except SyncDataError:
        return None
    if (
        not isinstance(root, dict)
        or root.get("schema") != "reader-context-snapshot/1"
        or root.get("contextStatus") != "ready"
    ):
        return None
    raw_updated = root.get("updatedAtUtc")
    if not isinstance(raw_updated, str):
        return None
    try:
        updated = datetime.fromisoformat(raw_updated)
    except ValueError:
        return None
    checked_at = now or datetime.now(timezone.utc)
    if updated.tzinfo is None or checked_at.tzinfo is None:
        return None
    age = checked_at.astimezone(timezone.utc) - updated.astimezone(timezone.utc)
    if age < timedelta(0) or age > SNAPSHOT_ANCHOR_MAX_AGE:
        return None
    active = root.get("activeReading")
    current = root.get("currentPage")
    revision = root.get("revision")
    if (
        not isinstance(active, dict)
        or active.get("fresh") is not True
        or not isinstance(current, dict)
        or current.get("stable") is not True
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        return None
    source = current.get("sourceInstanceId")
    active_source = active.get("sourceInstanceId")
    file = current.get("file")
    page = current.get("page")
    if (
        not isinstance(source, str)
        or source != active_source
        or not 1 <= len(source) <= 160
        or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for ch in source)
        or not isinstance(file, str)
        or not file
        or len(file) > 4096
        or "\x00" in file
        or isinstance(page, bool)
        or not (
            isinstance(page, int) and page >= 0
            or isinstance(page, str) and 1 <= len(page) <= 256 and "\x00" not in page
        )
    ):
        return None
    return {
        "source_instance_id": source,
        "snapshot_revision": revision,
        "file": file,
        "page": page,
    }


class CaptureBoundHistorySynchronizer:
    """Publish chat pairs only while an owned voice capture is active.

    ``requestId`` generation and durable dedupe remain inside ``sync_once``.
    Three trailing polls after capture closes catch continuity-file writes
    that land just after the native status transition.
    """

    def __init__(
        self,
        *,
        root: Path,
        publisher: Publisher = _bridge_publisher,
        global_state_path: Path | None = None,
        continuity_path: Path | None = None,
        state_path: Path | None = None,
        archive_path: Path | None = None,
        snapshot_path: Path | None = None,
        structured_history_client: Any | None = None,
    ) -> None:
        defaults = _default_paths()
        self.global_state_path = global_state_path or defaults[0]
        self.continuity_path = continuity_path or defaults[1]
        self.state_path = state_path or defaults[2]
        self.archive_path = archive_path or defaults[3]
        self.snapshot_path = (
            snapshot_path
            or root / "runtime" / "reader-context-snapshot.json"
        )
        self.publisher = publisher
        self.structured_history_client = structured_history_client
        self.was_active = False
        self.tail_polls = 0
        self._lease_thread_id: str | None = None
        self._capture_generation: int | None = None
        self._minimum_assistant_index: int | None = None
        self._lease_source_instance_id: str | None = None
        self._failure_backoff_polls = 0
        self._structured_baseline: set[str] | None = None
        # 上一次结构化整读的时刻(monotonic)。大线程按 _structured_read_cooldown_seconds 限速。
        self._structured_read_at = 0.0
        # 上一次结构化整读**失败**的时刻。失败期间走连续性文件继续发,不反复重读整条线程。
        self._structured_failed_at = 0.0
        self._structured_signature: tuple[int, int] | None = None
        self._structured_followup_polls = 0
        self.last_result: dict[str, Any] | None = None
        # 上一次已写进日志的 last_result["error"]。同一条错误只出声一次,变了再出声。
        self._last_reported_error: str | None = None
        self._diag_path = root / "runtime" / "voice-history-sync.log"
        self._service_was_online = True

    def _diag(self, message: str) -> None:
        """诊断留痕(2026-08-26)。这个文件没有测试覆盖,失败历来只写进
        没人看的 last_result —— 侧栏「历史缺失严重」查了一轮才发现全链
        静默:桥抖动 cancel、结构化回填失败、采集门控翻转,一处日志
        都没有。此后每个状态翻转与失败都要在这里出声。"""
        try:
            self._diag_path.parent.mkdir(parents=True, exist_ok=True)
            if (self._diag_path.exists()
                    and self._diag_path.stat().st_size > 256 * 1024):
                lines = self._diag_path.read_text(
                    encoding="utf-8").splitlines()[-200:]
                self._diag_path.write_text(
                    "\n".join(lines) + "\n", encoding="utf-8")
            with open(self._diag_path, "a", encoding="utf-8") as handle:
                handle.write("%s\t%s\n" % (
                    datetime.now(timezone.utc).isoformat(), message))
        except OSError:
            pass

    def _release_lease(self) -> None:
        if self.structured_history_client is not None:
            try:
                self.structured_history_client.close()
            except Exception:
                pass
        self._lease_thread_id = None
        self._minimum_assistant_index = None
        self._lease_source_instance_id = None
        self._failure_backoff_polls = 0
        self._structured_baseline = None
        self._structured_signature = None
        self._structured_followup_polls = 0

    def cancel(self) -> None:
        """Drop the in-memory capture lease without publishing a final turn."""
        if self._lease_thread_id is not None:
            self._diag("cancel(): dropping lease thread=%s"
                       % self._lease_thread_id)
        self.was_active = False
        self.tail_polls = 0
        self._capture_generation = None
        self._release_lease()

    def _arm(self) -> dict[str, Any]:
        """Archive the activation boundary without publishing prior history."""
        self.last_result = sync_once(
            global_state_path=self.global_state_path,
            continuity_path=self.continuity_path,
            state_path=self.state_path,
            archive_path=self.archive_path,
            publish=False,
            allow_last_good=False,
        )
        thread_id = self.last_result.get("threadId")
        items = self.last_result.get("items")
        if (
            self.last_result.get("stale") is not False
            or not isinstance(thread_id, str)
            or isinstance(items, bool)
            or not isinstance(items, int)
            or items < 0
        ):
            self._release_lease()
            return self.last_result
        self._lease_thread_id = thread_id
        # Item indexes are zero-based.  Requiring assistant index N + 1
        # ensures that both sides of an adjacent user/assistant pair were
        # appended after a baseline containing N items.
        self._minimum_assistant_index = items + 1
        identity = snapshot_output_identity(self.snapshot_path)
        if identity is not None:
            self._lease_source_instance_id = identity[
                "source_instance_id"
            ]
        if self.structured_history_client is not None:
            self._structured_signature = self._continuity_signature()
            try:
                projection = _project_codex_thread(
                    self.structured_history_client.read_thread(thread_id),
                    thread_id,
                )
                state = _load_durable(
                    self.state_path,
                    MAX_STATE_BYTES,
                    "state",
                    _default_state(),
                    _validate_state,
                )
                archive = _load_durable(
                    self.archive_path,
                    MAX_ARCHIVE_BYTES,
                    "archive",
                    _default_archive(),
                    _validate_archive,
                )
                (
                    self._structured_baseline,
                    recovery_pending,
                ) = _structured_recovery_baseline(
                    projection=projection,
                    state=state,
                    archive=archive,
                    thread_id=thread_id,
                )
                self.last_result["items"] = len(
                    projection["seenRequestIds"]
                )
                self.last_result["pairs"] = len(
                    projection["segments"]
                )
                self.last_result["pending"] = recovery_pending
                if recovery_pending > 0:
                    self._structured_followup_polls = max(
                        self._structured_followup_polls,
                        1,
                    )
            except Exception as exc:
                self._structured_baseline = None
                self.last_result["stale"] = True
                self.last_result["error"] = (
                    "structured history unavailable: "
                    + type(exc).__name__
                )
                # 回填失败 = 窗口滚动的丢失将无法弥补 —— 必须出声。
                self._diag(
                    "structured recovery FAILED thread=%s: %s: %s" % (
                        thread_id, type(exc).__name__,
                        str(exc)[:160]))
                # 记下失败时刻:随后的每一轮先走连续性文件发,60s 后才再试整读。
                # 原来这里只置退避,而 baseline 仍是 None → should_read 恒真 →
                # 每 15s 重读一遍整条线程,聊天记录却一条都发不出去。
                self._structured_failed_at = time.monotonic()
        return self.last_result

    def _arm_current_generation(
        self,
        capture_generation: int | None,
    ) -> dict[str, Any]:
        """Arm one Voice generation, leaving a failed arm retryable."""

        self.was_active = True
        self.tail_polls = 0
        self._capture_generation = capture_generation
        result = self._arm()
        if self._lease_thread_id is None:
            self.was_active = False
            self._capture_generation = None
        return result

    def _structured_read_cooldown_seconds(self) -> float:
        """大线程的整读冷却。

        thread/read 没有分页,一次就是整条线程(2026-09-04 实测 35.3 MB / 2.2s,
        而 app-server 那一侧要重读磁盘上 157 MiB 的 rollout)。按 0.75s 的轮询
        节奏反复整读是纯浪费,也是用户感到"一直在读盘"的一部分。
        小线程不受影响 —— 它们本来就便宜,历史仍近实时到达。
        """
        client = self.structured_history_client
        size = getattr(client, "last_response_bytes", 0) if client else 0
        if size >= STRUCTURED_READ_HUGE_BYTES:
            return STRUCTURED_READ_HUGE_COOLDOWN_SECONDS
        if size >= STRUCTURED_READ_LARGE_BYTES:
            return STRUCTURED_READ_LARGE_COOLDOWN_SECONDS
        return 0.0

    def _continuity_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.continuity_path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _structured_route_identity(self) -> dict[str, Any] | None:
        identity = snapshot_output_identity(self.snapshot_path)
        if identity is not None and self._lease_source_instance_id is None:
            self._lease_source_instance_id = identity[
                "source_instance_id"
            ]
        return identity

    def _structured_sync(
        self,
        *,
        force_history_read: bool,
    ) -> dict[str, Any] | None:
        assert self._lease_thread_id is not None
        assert self._minimum_assistant_index is not None
        identity = self._structured_route_identity()
        source_matches_lease = (
            identity is None and self._lease_source_instance_id is None
            or identity is not None
            and identity.get("source_instance_id")
                == self._lease_source_instance_id
        )
        self.last_result = sync_once(
            global_state_path=self.global_state_path,
            continuity_path=self.continuity_path,
            state_path=self.state_path,
            archive_path=self.archive_path,
            publish=False,
            expected_thread_id=self._lease_thread_id,
            minimum_assistant_index=self._minimum_assistant_index,
            allow_last_good=False,
        )
        if not source_matches_lease:
            route_error = (
                "Reader output source unavailable during capture lease"
                if identity is None
                else "active Reader source changed during capture lease"
            )
            prior_error = self.last_result.get("error")
            self.last_result["stale"] = True
            self.last_result["error"] = (
                f"{prior_error}; {route_error}"
                if prior_error
                else route_error
            )
            return self.last_result
        if self.last_result.get("stale") is not False:
            return self.last_result
        # 结构化源刚失败过 → 这一轮别再整读，直接走连续性文件把话发出去。
        if self._structured_failed_at:
            waited = time.monotonic() - self._structured_failed_at
            if waited < STRUCTURED_READ_FAILURE_COOLDOWN_SECONDS:
                return self._continuity_publish(
                    identity,
                    "structured history cooling down %.0fs/%.0fs" % (
                        waited, STRUCTURED_READ_FAILURE_COOLDOWN_SECONDS
                    ),
                )

        signature = self._continuity_signature()
        changed = signature != self._structured_signature
        if changed:
            self._structured_signature = signature
            self._structured_followup_polls = (
                STRUCTURED_HISTORY_FOLLOWUP_POLLS
            )
        should_read = (
            force_history_read
            or changed
            or self._structured_followup_polls > 0
            or self._structured_baseline is None
        )
        if self._structured_followup_polls > 0:
            self._structured_followup_polls -= 1
        if not should_read:
            return self.last_result
        # 大线程限速。force_history_read(收尾/显式要求)不受限,否则会丢尾巴。
        cooldown = self._structured_read_cooldown_seconds()
        if cooldown > 0.0 and not force_history_read:
            waited = time.monotonic() - self._structured_read_at
            if waited < cooldown:
                # 出声,别让"这一轮什么都没做"看起来像没在跑。
                self._diag(
                    "structured read 冷却中 thread=%s 已等 %.1fs/%.1fs"
                    " (上次响应 %.1f MB)" % (
                        self._lease_thread_id,
                        waited,
                        cooldown,
                        getattr(
                            self.structured_history_client,
                            "last_response_bytes",
                            0,
                        ) / 1048576.0,
                    )
                )
                return self.last_result

        try:
            self._structured_read_at = time.monotonic()
            projection = _project_codex_thread(
                self.structured_history_client.read_thread(
                    self._lease_thread_id
                ),
                self._lease_thread_id,
            )
            self._structured_failed_at = 0.0
        except Exception as exc:
            # ⚠ 只记类名会把唯一有用的那句话丢掉(2026-09-04:"response too large:
            #   36991863 bytes > 33554432 cap" 才是答案, "CodexAppServerError" 什么都不是)。
            self._diag(
                "structured publish read FAILED thread=%s: %s: %s" % (
                    self._lease_thread_id, type(exc).__name__, str(exc)[:160]))
            self._structured_failed_at = time.monotonic()
            # 读不到结构化源 ≠ 聊天记录必须停。退回连续性文件继续发。
            return self._continuity_publish(
                identity,
                "structured history unavailable: " + type(exc).__name__,
            )

        seen_ids = projection["seenRequestIds"]
        segments = projection["segments"]
        self.last_result["items"] = len(seen_ids)
        self.last_result["pairs"] = len(segments)
        if self._structured_baseline is None:
            # If the authoritative source was unavailable while arming, the
            # first successful read becomes the fail-closed activation line.
            self._structured_baseline = set(seen_ids)
            self.last_result["pending"] = 0
            return self.last_result

        try:
            state = _load_durable(
                self.state_path,
                MAX_STATE_BYTES,
                "state",
                _default_state(),
                _validate_state,
            )
        except SyncDataError as exc:
            self.last_result["stale"] = True
            self.last_result["error"] = f"state: {exc}"
            return self.last_result
        pending = [
            segment
            for segment in segments
            if (
                segment["requestId"] not in self._structured_baseline
                and segment["requestId"] not in state["published"]
            )
        ]
        publish_batch = pending[:MAX_STRUCTURED_PUBLISH_PER_POLL]
        published_count = 0
        publish_error: str | None = None
        if identity is None and pending:
            publish_error = "Reader output source unavailable"
        else:
            for segment in publish_batch:
                request_id = segment["requestId"]
                common = {
                    "file": identity["file"] if identity else None,
                    "page": identity["page"] if identity else None,
                    "source_instance_id": (
                        identity["source_instance_id"] if identity else None
                    ),
                    "snapshot_revision": (
                        identity["snapshot_revision"] if identity else None
                    ),
                    "thread_id": self._lease_thread_id,
                }
                try:
                    response = self.publisher(
                        "assistant_turn",
                        {
                            "text": segment["assistant"],
                            "user_utterance": segment["user"],
                        },
                        request_id=request_id,
                        **common,
                    )
                    if (
                        not isinstance(response, dict)
                        or response.get("ok") is not True
                    ):
                        publish_error = "publisher rejected assistant_turn"
                        break
                    for tool_index, tool in enumerate(segment["tools"]):
                        response = self.publisher(
                            "tool_status",
                            tool,
                            request_id=f"{request_id}:t{tool_index}",
                            **common,
                        )
                        if (
                            not isinstance(response, dict)
                            or response.get("ok") is not True
                        ):
                            publish_error = "publisher rejected tool_status"
                            break
                    if publish_error:
                        break
                except Exception as exc:
                    publish_error = f"publisher failed: {type(exc).__name__}"
                    break
                _remember_published(state, request_id)
                try:
                    _atomic_write_json(
                        self.state_path,
                        state,
                        MAX_STATE_BYTES,
                    )
                except (OSError, SyncDataError) as exc:
                    publish_error = f"state-write-after-publish: {exc}"
                    break
                published_count += 1

        remaining = sum(
            segment["requestId"] not in self._structured_baseline
            and segment["requestId"] not in state["published"]
            for segment in segments
        )
        self.last_result["pending"] = remaining
        self.last_result["published"] = published_count
        if remaining > 0:
            self._structured_followup_polls = max(
                self._structured_followup_polls,
                1,
            )
        if publish_error:
            self.last_result["error"] = publish_error
            if published_count == 0 and remaining > 0:
                self._failure_backoff_polls = (
                    PUBLISH_FAILURE_BACKOFF_POLLS
                )
        else:
            self.last_result.pop("error", None)
            self._failure_backoff_polls = 0
        return self.last_result

    def _sync(
        self,
        *,
        force_history_read: bool = False,
    ) -> dict[str, Any] | None:
        if (
            self._lease_thread_id is None
            or self._minimum_assistant_index is None
        ):
            return None
        if self._failure_backoff_polls > 0:
            self._failure_backoff_polls -= 1
            return self.last_result
        if self.structured_history_client is not None:
            return self._structured_sync(
                force_history_read=force_history_read
            )
        identity = snapshot_output_identity(self.snapshot_path)
        if identity is not None and self._lease_source_instance_id is None:
            self._lease_source_instance_id = identity[
                "source_instance_id"
            ]
        source_matches_lease = (
            identity is None and self._lease_source_instance_id is None
            or identity is not None
            and identity.get("source_instance_id")
                == self._lease_source_instance_id
        )
        if not source_matches_lease:
            self.last_result = sync_once(
                global_state_path=self.global_state_path,
                continuity_path=self.continuity_path,
                state_path=self.state_path,
                archive_path=self.archive_path,
                publish=False,
                expected_thread_id=self._lease_thread_id,
                minimum_assistant_index=self._minimum_assistant_index,
                allow_last_good=False,
            )
            route_error = (
                "Reader output source unavailable during capture lease"
                if identity is None
                else "active Reader source changed during capture lease"
            )
            prior_error = self.last_result.get("error")
            self.last_result["stale"] = True
            self.last_result["error"] = (
                f"{prior_error}; {route_error}"
                if prior_error
                else route_error
            )
            self._failure_backoff_polls = 0
            return self.last_result
        return self._continuity_publish(identity)

    def _continuity_publish(
        self,
        identity: dict[str, Any] | None,
        degraded: str | None = None,
    ) -> dict[str, Any]:
        """从连续性文件（有界，10 条滚动窗口）发布这一轮。

        两处共用：没有结构化源时的常规路径，以及结构化源读不到时的降级路径。
        降级时 `degraded` 说明原因 —— 聊天记录仍在发，只是"窗口滚掉的轮次还能
        补回来"这一项能力此刻没有。**不要因为读不到整条线程就把历史整个停掉**：
        2026-09-04 那次就是这么停了好几天（35.3 MB 撞破传输上限）。
        """
        self.last_result = sync_once(
            global_state_path=self.global_state_path,
            continuity_path=self.continuity_path,
            state_path=self.state_path,
            archive_path=self.archive_path,
            publish=True,
            publisher=self.publisher,
            file=identity.get("file") if identity else None,
            page=identity.get("page") if identity else None,
            source_instance_id=(
                identity.get("source_instance_id") if identity else None
            ),
            snapshot_revision=(
                identity.get("snapshot_revision") if identity else None
            ),
            expected_thread_id=self._lease_thread_id,
            minimum_assistant_index=self._minimum_assistant_index,
            allow_last_good=False,
        )
        if degraded:
            self.last_result["degraded"] = degraded
            prior = self.last_result.get("error")
            self.last_result["error"] = (
                f"{prior}; {degraded}" if prior else degraded
            )
        if (
            self.last_result.get("error")
            and self.last_result.get("pending", 0) > 0
            and self.last_result.get("published", 0) == 0
            and not degraded
        ):
            self._failure_backoff_polls = (
                PUBLISH_FAILURE_BACKOFF_POLLS
            )
        else:
            self._failure_backoff_polls = 0
        return self.last_result

    def observe(
        self,
        *,
        service_online: bool,
        capture_active: bool,
        snapshot_mode: bool,
        capture_generation: int | None = None,
    ) -> dict[str, Any] | None:
        result = self._observe(
            service_online=service_online,
            capture_active=capture_active,
            snapshot_mode=snapshot_mode,
            capture_generation=capture_generation,
        )
        self._report_result_error()
        return result

    def _report_result_error(self) -> None:
        """last_result["error"] 变了就写日志。

        2026-09-06 教训:global-state 超 1 MiB 上限、随后又撞上 Codex 新字段,
        同步器都只把原因塞进 last_result["error"] 然后退回上一次绑定的旧线程 ——
        聊天记录停更了一整天,而日志里一个字都没有。"""
        error = None
        if isinstance(self.last_result, dict):
            raw = self.last_result.get("error")
            error = str(raw)[:300] if raw else None
        if error == self._last_reported_error:
            return
        self._last_reported_error = error
        if error:
            self._diag(
                "sync error thread=%s: %s" % (self._lease_thread_id, error)
            )
        else:
            self._diag("sync error cleared thread=%s" % self._lease_thread_id)

    def _observe(
        self,
        *,
        service_online: bool,
        capture_active: bool,
        snapshot_mode: bool,
        capture_generation: int | None = None,
    ) -> dict[str, Any] | None:
        if snapshot_mode is not True:
            self.cancel()
            return None
        if service_online is not True:
            # 桥每隔几分钟就自重启一次(慢性抖动,单日 50-250 次实测)。
            # 原来这里直接 cancel() —— 租约/代际/结构化基线全清,恢复后
            # 重新 arm;而上游 continuity 每线程只有 10 条滚动窗口,停摆
            # 期间滚过的轮次**永久丢失**(侧栏「历史缺失严重」的主耦合点)。
            # 桥离线只是「发布暂时不可达」,不是「会话结束」:保持全部
            # 状态,本轮跳过,桥回来后从原地继续。
            if self._service_was_online:
                self._service_was_online = False
                self._diag("service offline - holding sync state (was: cancel)")
            return None
        if not self._service_was_online:
            self._service_was_online = True
            self._diag("service back online - resuming from held state")
        active = bool(service_online and capture_active)
        if active:
            if not self.was_active:
                self._release_lease()
                return self._arm_current_generation(capture_generation)
            if (
                capture_generation is not None
                and self._capture_generation is not None
                and capture_generation != self._capture_generation
            ):
                previous_result = None
                if self._lease_thread_id is not None:
                    previous_result = self._sync(force_history_read=True)
                self._release_lease()
                return (
                    self._arm_current_generation(capture_generation)
                    or previous_result
                )
            if self._capture_generation is None:
                self._capture_generation = capture_generation
            self.was_active = True
            self.tail_polls = 0
            return self._sync()
        if self.was_active:
            self.was_active = False
            self._capture_generation = None
            if self._lease_thread_id is not None:
                self.tail_polls = FINAL_TAIL_POLLS
        if self.tail_polls > 0:
            self.tail_polls -= 1
            result = self._sync(force_history_read=True)
            if self.tail_polls == 0:
                self._release_lease()
            return result
        return None

    def finish(self) -> dict[str, Any] | None:
        if (
            self._lease_thread_id is not None
            and (self.was_active or self.tail_polls > 0)
        ):
            self.was_active = False
            self.tail_polls = 0
            result = self._sync(force_history_read=True)
            self._release_lease()
            return result
        self.cancel()
        return None


def monitor_capture_history(
    *,
    stop_event: threading.Event,
    status_provider: StatusProvider,
    enabled_provider: Callable[[], bool],
    synchronizer: CaptureBoundHistorySynchronizer,
    poll_seconds: float = HISTORY_POLL_SECONDS,
) -> None:
    """Poll strict native status until the owning bridge lifecycle stops."""
    try:
        while not stop_event.is_set():
            try:
                snapshot_mode = enabled_provider() is True
            except Exception:
                snapshot_mode = False
            try:
                status = status_provider()
                service_online = status.service_online is True
                capture_active = status.capture_active is True
                capture_generation = getattr(
                    status,
                    "capture_generation",
                    None,
                )
            except Exception:
                service_online = False
                capture_active = False
                capture_generation = None
            synchronizer.observe(
                service_online=service_online,
                capture_active=capture_active,
                snapshot_mode=snapshot_mode,
                capture_generation=capture_generation,
            )
            stop_event.wait(poll_seconds)
    finally:
        try:
            snapshot_mode = enabled_provider() is True
        except Exception:
            snapshot_mode = False
        if snapshot_mode:
            synchronizer.finish()
        else:
            synchronizer.cancel()


@contextmanager
def history_worker_lease(root: Path) -> Iterator[bool]:
    """Own one per-install Windows history worker, without a durable PID file."""
    if os.name != "nt":
        yield True
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    digest = hashlib.sha256(
        str(root.resolve()).casefold().encode("utf-8")
    ).hexdigest()[:20]
    handle = kernel32.CreateMutexW(
        None,
        True,
        f"Local\\BWReaderSidebarHistory-{digest}",
    )
    if not handle:
        yield False
        return
    try:
        yield ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    finally:
        kernel32.CloseHandle(handle)


def run_service_bound_history_worker(
    *,
    root: Path,
    status_provider: StatusProvider,
    enabled_provider: Callable[[], bool],
    publisher: Publisher = _bridge_publisher,
    poll_seconds: float = HISTORY_POLL_SECONDS,
    wait: Callable[[float], None] | None = None,
    max_cycles: int | None = None,
) -> int:
    """Run the packaged worker until opt-out or sustained service shutdown."""
    if max_cycles is not None and max_cycles <= 0:
        raise ValueError("max_cycles must be positive")
    waiter = wait or threading.Event().wait
    synchronizer = CaptureBoundHistorySynchronizer(
        root=root,
        publisher=publisher,
    )
    offline_polls = 0
    seen_online = False
    cycles = 0
    with history_worker_lease(root) as owned:
        if not owned:
            return 0
        try:
            while True:
                try:
                    snapshot_mode = enabled_provider() is True
                except Exception:
                    snapshot_mode = False
                if not snapshot_mode:
                    synchronizer.cancel()
                    break
                try:
                    status = status_provider()
                    service_online = status.service_online is True
                    capture_active = status.capture_active is True
                    capture_generation = getattr(
                        status,
                        "capture_generation",
                        None,
                    )
                except Exception:
                    service_online = False
                    capture_active = False
                    capture_generation = None
                synchronizer.observe(
                    service_online=service_online,
                    capture_active=capture_active,
                    snapshot_mode=True,
                    capture_generation=capture_generation,
                )
                if service_online:
                    seen_online = True
                    offline_polls = 0
                else:
                    offline_polls += 1
                    if seen_online and offline_polls >= OFFLINE_EXIT_POLLS:
                        break
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                waiter(poll_seconds)
        finally:
            try:
                snapshot_mode = enabled_provider() is True
            except Exception:
                snapshot_mode = False
            if snapshot_mode:
                synchronizer.finish()
            else:
                synchronizer.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    global_default, continuity_default, state_default, archive_default = (
        _default_paths()
    )
    parser = argparse.ArgumentParser(
        description="Archive local Codex Voice history; publish only on request"
    )
    parser.add_argument("--global-state", type=Path, default=global_default)
    parser.add_argument("--continuity", type=Path, default=continuity_default)
    parser.add_argument("--state", type=Path, default=state_default)
    parser.add_argument("--archive", type=Path, default=archive_default)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--file", help="optional Reader vault-relative file")
    parser.add_argument("--page", type=int, help="optional Reader page")
    args = parser.parse_args(argv)

    if args.page is not None and args.page < 1:
        status = _status(
            thread_id=None,
            dry_run=not args.publish,
            gap=True,
            error="page must be a positive integer",
        )
        print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
        return 2

    result = sync_once(
        global_state_path=args.global_state,
        continuity_path=args.continuity,
        state_path=args.state,
        archive_path=args.archive,
        publish=args.publish,
        publisher=_bridge_publisher if args.publish else None,
        file=args.file,
        page=args.page,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if "error" not in result else 2


if __name__ == "__main__":
    raise SystemExit(main())
