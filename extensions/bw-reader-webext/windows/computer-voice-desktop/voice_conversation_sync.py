#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音对话 → 助手历史（2026-09-13 重做）。

这一版把三件事分开，也只做这三件事：

1. **语音真的开了之后，按证据找到语音所在的对话。** 证据 = 哪条线程的 rollout 在收
   `transcript_segment`（转写）或 `<realtime_delegation>`（语音模型委托）。Codex 自己那个
   `realtime-voice-most-recent-thread` 指针记的是"最近被创建/切到前台的线程"，一次语音
   会话里会在 chat / chat-2 / chat-3 之间跳（2026-09-13 实测：指针指着刚建的 chat-3，
   转写全进了老线程）。旧同步器信指针，指针一动就报 lease changed 整段停发 —— 三条线程
   一整天 0 发布。这里指针只在完全没证据时兜底。
2. **找到的线程写进一个明确的绑定文件**（`~/.codex/voice-thread-binding.json`），对话历史、
   提示板推送、语音入口指令都读它 —— 用户拍板：所有绑定都在"语音开了 + 找到对话"之后。
3. **把这条线程的轮次写进 Flask 的助手历史**（`/api/assistant/log`，Bearer 令牌）。全软件
   只有一条助手历史（用户 2026-09-13 拍板：不分书、不分模式、不分文字/语音），侧栏打开时
   从 Flask 拉，所以这里不再往设备推、不再绑书、不再有"激活基线"—— 最近 BACKFILL_WINDOW
   内没写过的轮次一律补上，丢了的自然回来。

不做的事：不碰 Reader 输出管道，不判断用户在看哪本书，不判断 App 在不在线。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import voice_history_sidebar_sync as _legacy

VERSION = 1
BINDING_CONTRACT = "reader-voice-thread-binding/1"
STATE_CONTRACT = "reader-voice-conversation-sync/1"

#: 补发窗口：绑定时把这段时间内没写过的轮次都写进历史（旧同步器"激活之前的全不发"的反面）。
BACKFILL_WINDOW_SECONDS = 12 * 3600
#: 单次绑定最多补发这么多轮，防止第一次跑把整条 45 轮的老线程一次倒进侧栏。
BACKFILL_MAX_TURNS = 24
#: 证据只看 rollout 尾部这么多字节：转写/委托都在最近，整读 3.5 MB 没必要。
EVIDENCE_TAIL_BYTES = 256 * 1024
#: 语音开始前这么久内的证据也算（语音刚开时第一句可能比麦克风台账早落盘）。
EVIDENCE_GRACE_SECONDS = 120.0
#: 多久看一次"语音在哪条线程"。stat 一把目录很便宜；读尾巴只在 mtime 变了才做。
LOCATE_INTERVAL_SECONDS = 2.0
#: 没证据时等多久才肯用指针兜底。
POINTER_FALLBACK_AFTER_SECONDS = 20.0
#: 语音结束后再多读几拍，接住最后一轮的 final_answer（实测中位 11 秒才落）。
FINAL_TAIL_POLLS = 8
#: 大线程整读的冷却：thread/read 没有分页，一次就是整条。
READ_COOLDOWN_SMALL = 2.0
READ_COOLDOWN_LARGE = 15.0
READ_COOLDOWN_HUGE = 45.0
LARGE_RESPONSE_BYTES = 4 * 1024 * 1024
HUGE_RESPONSE_BYTES = 16 * 1024 * 1024
#: 写历史的 HTTP 超时。Flask 在本机，5 秒不回就是它有事，不是网络。
WRITE_TIMEOUT_SECONDS = 5.0
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_WRITTEN_PER_THREAD = 4000

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ROLLOUT_UUID = re.compile(r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
_TURN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")


class VoiceConversationError(RuntimeError):
    """这一层自己能说清的失败。"""


# ── 证据：语音在哪条线程 ─────────────────────────────────────────

def _parse_ts(value: Any) -> float | None:
    """rollout 行的 timestamp 是 ISO8601（带 Z）。解析不了就当没有。"""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _rollout_thread_id(path: Path) -> str | None:
    match = _ROLLOUT_UUID.search(path.name)
    return match.group(1) if match else None


def scan_rollout_tail(path: Path, *, since_epoch: float,
                      tail_bytes: int = EVIDENCE_TAIL_BYTES) -> dict[str, Any] | None:
    """看一个 rollout 的尾巴里，since 之后最新的一条语音证据。

    返回 {"at": epoch, "kind": "transcript"|"delegation"}；没有就 None。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
                handle.readline()  # 丢掉被截断的半行
            raw = handle.read()
    except OSError:
        return None
    best: dict[str, Any] | None = None
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        if "transcript_segment" not in line and "realtime_delegation" not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        kind: str | None = None
        if row.get("type") == "realtime_item" and payload.get("type") == "transcript_segment":
            kind = "transcript"
        elif (
            row.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
            and "realtime_delegation" in line
        ):
            kind = "delegation"
        if kind is None:
            continue
        at = _parse_ts(row.get("timestamp"))
        if at is None or at < since_epoch:
            continue
        if best is None or at > best["at"]:
            best = {"at": at, "kind": kind}
    return best


def locate_voice_thread(sessions_dir: Path, *, since_epoch: float,
                        now: float | None = None) -> dict[str, Any] | None:
    """在 sessions 里找 since 之后最新收到语音证据的线程。

    只看 since 前一天到今天的日期目录、且 mtime ≥ since 的文件；
    返回 {"threadId", "evidenceAt", "evidenceKind", "rollout"}。
    """
    now = time.time() if now is None else now
    start_day = _dt.datetime.fromtimestamp(since_epoch - 86400).date()
    end_day = _dt.datetime.fromtimestamp(now + 3600).date()
    best: dict[str, Any] | None = None
    day = start_day
    while day <= end_day:
        folder = sessions_dir / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        day += _dt.timedelta(days=1)
        if not folder.is_dir():
            continue
        try:
            entries = list(folder.glob("rollout-*.jsonl"))
        except OSError:
            continue
        for path in entries:
            thread_id = _rollout_thread_id(path)
            if thread_id is None:
                continue
            try:
                if path.stat().st_mtime < since_epoch:
                    continue
            except OSError:
                continue
            evidence = scan_rollout_tail(path, since_epoch=since_epoch)
            if evidence is None:
                continue
            if best is None or evidence["at"] > best["evidenceAt"]:
                best = {
                    "threadId": thread_id,
                    "evidenceAt": evidence["at"],
                    "evidenceKind": evidence["kind"],
                    "rollout": path,
                }
    return best


def pointer_thread(global_state_path: Path) -> str | None:
    """Codex 自己的指针（兜底用）。读不到/没绑就 None。"""
    try:
        status, binding = _legacy._parse_binding(
            _legacy._read_bounded_json(
                global_state_path, _legacy.MAX_GLOBAL_STATE_BYTES, "global-state"))
    except _legacy.SyncDataError:
        return None
    if status != "bound" or binding is None:
        return None
    return binding["conversationId"]


# ── 绑定文件 ─────────────────────────────────────────────────────

def default_binding_path() -> Path:
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    return profile / ".codex" / "voice-thread-binding.json"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).isoformat()


def write_binding(path: Path, *, thread_id: str, source: str, bound_at: float,
                  evidence_at: float | None, evidence_kind: str | None,
                  capture_active: bool, capture_generation: int | None) -> dict[str, Any]:
    value = {
        "contract": BINDING_CONTRACT,
        "threadId": thread_id,
        "source": source,  # evidence | pointer
        "boundAtUtc": _iso(bound_at),
        "evidenceAtUtc": _iso(evidence_at),
        "evidenceKind": evidence_kind,
        "captureActive": bool(capture_active),
        "captureGeneration": capture_generation,
    }
    _legacy._atomic_write_json(path, value, 64 * 1024)
    return value


def read_binding(path: Path, *, max_age_seconds: float | None = None,
                 now: float | None = None) -> dict[str, Any] | None:
    """读绑定；contract 不对、线程不像 uuid、过期 → None。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("contract") != BINDING_CONTRACT:
        return None
    thread_id = value.get("threadId")
    if not isinstance(thread_id, str) or not _UUID.fullmatch(thread_id):
        return None
    if max_age_seconds is not None:
        bound_at = value.get("boundAtUtc")
        try:
            bound = _dt.datetime.fromisoformat(str(bound_at)).timestamp()
        except (TypeError, ValueError):
            return None
        now = time.time() if now is None else now
        if now - bound > max_age_seconds:
            return None
    return value


# ── 线程 → 轮次 ─────────────────────────────────────────────────

def project_turns(result: Any, thread_id: str) -> list[dict[str, Any]]:
    """app-server thread/read → 已完成的轮次（用户话 + 最终回答 + 工具 + 时长）。

    投影规则沿用旧同步器（userMessage 开始一轮、只认 phase=final_answer 的 agentMessage），
    requestId 也用同一个算法，所以旧的"已发布"记录仍然可比对。多出来的是 turn 级时刻：
    startedAt / completedAt / durationMs（app-server 给的是 epoch 秒）。
    """
    if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
        raise VoiceConversationError("app-server: thread result missing")
    thread = result["thread"]
    if thread.get("id") != thread_id:
        raise VoiceConversationError("app-server: thread id mismatch")
    turns_raw = thread.get("turns")
    if not isinstance(turns_raw, list):
        raise VoiceConversationError("app-server: turns invalid")
    out: list[dict[str, Any]] = []
    for turn_index, turn in enumerate(turns_raw):
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        raw_turn_id = turn.get("id")
        turn_id = raw_turn_id if isinstance(raw_turn_id, str) and 0 < len(raw_turn_id) <= 256 else f"turn-{turn_index}"
        started = turn.get("startedAt") if isinstance(turn.get("startedAt"), (int, float)) else None
        completed = turn.get("completedAt") if isinstance(turn.get("completedAt"), (int, float)) else None
        duration = turn.get("durationMs") if isinstance(turn.get("durationMs"), (int, float)) else None
        current: dict[str, Any] | None = None
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                user_text = _legacy._codex_user_text(item)
                if user_text is None:
                    current = None
                    continue
                raw_item_id = item.get("id")
                item_id = raw_item_id if isinstance(raw_item_id, str) and 0 < len(raw_item_id) <= 256 else f"user-{item_index}"
                current = {
                    "requestId": _legacy._codex_request_id(thread_id, turn_id, item_id, item_index, user_text),
                    "turnId": turn_id,
                    "user": user_text,
                    "tools": [],
                    "startedAt": started,
                    "completedAt": completed,
                    "durationMs": duration,
                }
                continue
            if current is None:
                continue
            tool = _legacy._project_tool(item)
            if tool is not None:
                ms = item.get("durationMs")
                tool["ms"] = int(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) and 0 <= ms <= 86_400_000 else None
                if len(current["tools"]) < _legacy.MAX_PROJECTED_TOOLS:
                    current["tools"].append(tool)
                continue
            if item_type != "agentMessage" or item.get("phase") != "final_answer":
                continue
            assistant = _legacy._bounded_codex_text(item.get("text"), limit=_legacy.MAX_CODEX_TEXT_CHARS)
            if assistant is not None:
                out.append({**current, "assistant": assistant})
            current = None
    return out


def turn_to_log_body(turn: dict[str, Any], thread_id: str) -> dict[str, Any]:
    """一轮 → /api/assistant/log 的 body。工具进 parts（kind=tool，ms=耗时），总耗时进 took_ms。"""
    parts: list[dict[str, Any]] = []
    for tool in turn.get("tools") or []:
        part: dict[str, Any] = {"kind": "tool", "tool": str(tool.get("tool") or "tool")[:160],
                                "label": str(tool.get("label") or "工具")[:320]}
        detail = tool.get("detail")
        if isinstance(detail, str) and detail:
            part["result"] = detail[:6000]
        if isinstance(tool.get("ms"), int):
            part["ms"] = tool["ms"]
        parts.append(part)
    body: dict[str, Any] = {
        "user": turn["user"],
        "assistant": _legacy._publish_assistant_text(turn["assistant"]),
        "via": "codex-voice",
        "turn_id": turn["requestId"],
        "thread_id": thread_id,
    }
    if parts:
        body["parts"] = parts
    if isinstance(turn.get("durationMs"), (int, float)):
        body["took_ms"] = int(turn["durationMs"])
    return body


# ── 写 Flask 历史 ────────────────────────────────────────────────

def default_token() -> str | None:
    """与 KjPageClient / mcp_server.py 同一把令牌：env MCP_WEBAPP_TOKEN，否则 ~/.config/mcp-webapp-token。"""
    env = (os.environ.get("MCP_WEBAPP_TOKEN") or "").strip()
    if env:
        return env
    try:
        text = (Path.home() / ".config" / "mcp-webapp-token").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


class FlaskHistoryWriter:
    """把一轮写进 Flask 的助手历史。只会写，不会读；失败抛 VoiceConversationError。"""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 opener: Callable[..., Any] | None = None) -> None:
        self.base_url = (base_url or os.environ.get("BW_READER_WEBAPP_URL") or "http://127.0.0.1:5000").rstrip("/")
        self._token = token
        self._opener = opener or urllib.request.urlopen

    def token(self) -> str | None:
        if self._token is None:
            self._token = default_token()
        return self._token

    def write_turn(self, turn: dict[str, Any], thread_id: str) -> dict[str, Any]:
        token = self.token()
        if not token:
            raise VoiceConversationError("没有 webapp 令牌（MCP_WEBAPP_TOKEN / ~/.config/mcp-webapp-token）")
        body = json.dumps(turn_to_log_body(turn, thread_id), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/assistant/log", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with self._opener(request, timeout=WRITE_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise VoiceConversationError("Flask 拒绝写历史 HTTP %s %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise VoiceConversationError("Flask 不可达: %s" % type(exc).__name__) from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise VoiceConversationError("Flask 回了非 JSON") from exc
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise VoiceConversationError("Flask 写历史失败: %s" % str(parsed)[:200])
        return parsed


# ── 同步器 ──────────────────────────────────────────────────────

def _default_state() -> dict[str, Any]:
    return {"contract": STATE_CONTRACT, "threads": {}}


class VoiceConversationSync:
    """语音期间：找线程 → 绑定 → 读 app-server → 写历史。接口与旧同步器一致（observe/finish/cancel）。"""

    def __init__(self, *, root: Path, sessions_dir: Path | None = None,
                 global_state_path: Path | None = None, binding_path: Path | None = None,
                 state_path: Path | None = None, writer: FlaskHistoryWriter | None = None,
                 history_client: Any | None = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        profile = Path(os.environ.get("USERPROFILE") or Path.home())
        self.root = root
        self.sessions_dir = sessions_dir or (profile / ".codex" / "sessions")
        self.global_state_path = global_state_path or (profile / ".codex" / ".codex-global-state.json")
        self.binding_path = binding_path or default_binding_path()
        self.state_path = state_path or (root / "voice-conversation-sync.json")
        self.writer = writer or FlaskHistoryWriter()
        self.history_client = history_client
        self._clock = clock
        self._monotonic = monotonic
        self._diag_path = root / "runtime" / "voice-conversation-sync.log"
        # 会话态
        self.was_active = False
        self.tail_polls = 0
        self._capture_started_at: float | None = None
        self._capture_generation: int | None = None
        self._service_was_online = True
        self._bound_thread: str | None = None
        self._binding_source: str | None = None
        self._evidence_at: float | None = None
        self._evidence_kind: str | None = None
        self._last_locate = -1e12  # 第一拍就找，不等间隔
        self._rollout_path: Path | None = None
        self._rollout_size = -1
        self._last_read_mono = -1e12  # 第一次读不受冷却限制
        self._last_response_bytes = 0
        self._backfilled_threads: set[str] = set()
        self._last_error: str | None = None
        self._reported_error: str | None = None
        self.last_result: dict[str, Any] = {"written": 0, "pending": 0}

    # ── 诊断 ──
    def _diag(self, message: str) -> None:
        try:
            self._diag_path.parent.mkdir(parents=True, exist_ok=True)
            if self._diag_path.exists() and self._diag_path.stat().st_size > 256 * 1024:
                lines = self._diag_path.read_text(encoding="utf-8").splitlines()[-200:]
                self._diag_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with open(self._diag_path, "a", encoding="utf-8") as handle:
                handle.write("%s\t%s\n" % (_dt.datetime.now(_dt.timezone.utc).isoformat(), message))
        except OSError:
            pass

    def _set_error(self, error: str | None) -> None:
        self._last_error = error
        if error != self._reported_error:
            self._reported_error = error
            self._diag("error cleared" if error is None else "error: " + error)

    def status(self) -> dict[str, Any]:
        """给 ReaderPC 状态文件的一节：绑到哪、凭什么、写了多少、最后一个错。"""
        return {
            "contract": STATE_CONTRACT,
            "captureActive": self.was_active,
            "boundThread": self._bound_thread,
            "bindingSource": self._binding_source,
            "evidenceAtUtc": _iso(self._evidence_at),
            "evidenceKind": self._evidence_kind,
            "written": self.last_result.get("written", 0),
            "pending": self.last_result.get("pending", 0),
            "lastError": self._last_error,
        }

    # ── 状态文件 ──
    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _default_state()
        if not isinstance(value, dict) or value.get("contract") != STATE_CONTRACT or not isinstance(value.get("threads"), dict):
            return _default_state()
        return value

    def _save_state(self, state: dict[str, Any]) -> None:
        _legacy._atomic_write_json(self.state_path, state, MAX_STATE_BYTES)

    # ── 生命周期 ──
    def observe(self, *, service_online: bool, capture_active: bool, snapshot_mode: bool,
                capture_generation: int | None = None) -> dict[str, Any] | None:
        if snapshot_mode is not True:
            self.cancel()
            return None
        if service_online is not True:
            # 桥抖动不是会话结束：保持状态，本拍跳过。
            if self._service_was_online:
                self._service_was_online = False
                self._diag("service offline - holding")
            return None
        if not self._service_was_online:
            self._service_was_online = True
            self._diag("service back online")
        if capture_active:
            if not self.was_active:
                self.was_active = True
                self.tail_polls = 0
                self._capture_started_at = self._clock()
                self._capture_generation = capture_generation
                self._diag("capture started generation=%s" % capture_generation)
            elif capture_generation is not None and self._capture_generation is not None and capture_generation != self._capture_generation:
                self._capture_generation = capture_generation
                self._capture_started_at = self._clock()
                self._diag("capture generation changed → %s" % capture_generation)
            return self._tick(final=False)
        if self.was_active:
            self.was_active = False
            self.tail_polls = FINAL_TAIL_POLLS
            self._diag("capture ended - %d tail polls" % FINAL_TAIL_POLLS)
        if self.tail_polls > 0:
            self.tail_polls -= 1
            result = self._tick(final=True)
            if self.tail_polls == 0:
                self._release()
            return result
        return None

    def finish(self) -> dict[str, Any] | None:
        if self._bound_thread is not None and (self.was_active or self.tail_polls > 0):
            self.was_active = False
            self.tail_polls = 0
            result = self._tick(final=True)
            self._release()
            return result
        self.cancel()
        return None

    def cancel(self) -> None:
        if self._bound_thread is not None:
            self._diag("cancel(): dropping thread=%s" % self._bound_thread)
        self.was_active = False
        self.tail_polls = 0
        self._release()

    def _release(self) -> None:
        if self.history_client is not None:
            try:
                self.history_client.close()
            except Exception:  # noqa: BLE001
                pass
        if self._bound_thread is not None:
            # 绑定文件留着（语音结束后推送仍要知道"最近那条"），只把 captureActive 翻成 false。
            try:
                write_binding(self.binding_path, thread_id=self._bound_thread, source=self._binding_source or "evidence",
                              bound_at=self._clock(), evidence_at=self._evidence_at, evidence_kind=self._evidence_kind,
                              capture_active=False, capture_generation=None)
            except Exception:  # noqa: BLE001
                pass
        self._bound_thread = None
        self._binding_source = None
        self._rollout_path = None
        self._rollout_size = -1
        self._capture_started_at = None
        self._capture_generation = None

    # ── 每拍 ──
    def _tick(self, *, final: bool) -> dict[str, Any]:
        try:
            changed = self._locate(force=final)
            self._read_and_write(force=final or changed)
            self._set_error(None)
        except VoiceConversationError as exc:
            self._set_error(str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 —— 任何没预料到的错都要出声，别静默停摆
            self._set_error("%s: %s" % (type(exc).__name__, str(exc)[:200]))
        return self.last_result

    def _locate(self, *, force: bool) -> bool:
        """找语音所在线程；换了就改绑。返回是否换绑。"""
        now_mono = self._monotonic()
        if not force and now_mono - self._last_locate < LOCATE_INTERVAL_SECONDS:
            return False
        self._last_locate = now_mono
        since = (self._capture_started_at or self._clock()) - EVIDENCE_GRACE_SECONDS
        found = locate_voice_thread(self.sessions_dir, since_epoch=since, now=self._clock())
        thread_id: str | None
        source: str
        if found is not None:
            thread_id, source = found["threadId"], "evidence"
            self._evidence_at, self._evidence_kind = found["evidenceAt"], found["evidenceKind"]
        elif self._bound_thread is None and self._capture_started_at is not None \
                and self._clock() - self._capture_started_at >= POINTER_FALLBACK_AFTER_SECONDS:
            thread_id, source = pointer_thread(self.global_state_path), "pointer"
        else:
            return False
        if thread_id is None or thread_id == self._bound_thread:
            return False
        # 换绑：证据永远压过指针；指针绑上之后一旦有证据也跟着走。
        self._diag("bind %s → %s (%s%s)" % (self._bound_thread, thread_id, source,
                                            "" if found is None else ", evidence=" + found["evidenceKind"]))
        self._bound_thread = thread_id
        self._binding_source = source
        self._rollout_path = found["rollout"] if found is not None else None
        self._rollout_size = -1
        write_binding(self.binding_path, thread_id=thread_id, source=source, bound_at=self._clock(),
                      evidence_at=self._evidence_at if source == "evidence" else None,
                      evidence_kind=self._evidence_kind if source == "evidence" else None,
                      capture_active=self.was_active, capture_generation=self._capture_generation)
        return True

    def _rollout_grew(self) -> bool:
        if self._rollout_path is None and self._bound_thread is not None:
            for candidate in self.sessions_dir.rglob("rollout-*-" + self._bound_thread + ".jsonl"):
                self._rollout_path = candidate
                break
        if self._rollout_path is None:
            return False
        try:
            size = self._rollout_path.stat().st_size
        except OSError:
            return False
        if size != self._rollout_size:
            self._rollout_size = size
            return True
        return False

    def _cooldown(self) -> float:
        if self._last_response_bytes >= HUGE_RESPONSE_BYTES:
            return READ_COOLDOWN_HUGE
        if self._last_response_bytes >= LARGE_RESPONSE_BYTES:
            return READ_COOLDOWN_LARGE
        return READ_COOLDOWN_SMALL

    def _read_and_write(self, *, force: bool) -> None:
        thread_id = self._bound_thread
        if thread_id is None or self.history_client is None:
            return
        grew = self._rollout_grew()
        if not (force or grew):
            return
        if not force and self._monotonic() - self._last_read_mono < self._cooldown():
            return
        self._last_read_mono = self._monotonic()
        result = self.history_client.read_thread(thread_id)
        self._last_response_bytes = int(getattr(self.history_client, "last_response_bytes", 0) or 0)
        turns = project_turns(result, thread_id)
        state = self._load_state()
        record = state["threads"].setdefault(thread_id, {"written": []})
        written = record.get("written") if isinstance(record.get("written"), list) else []
        seen = set(written)
        now = self._clock()
        pending = [t for t in turns if t["requestId"] not in seen
                   and (t.get("completedAt") is None or t["completedAt"] >= now - BACKFILL_WINDOW_SECONDS)]
        if thread_id not in self._backfilled_threads:
            # 第一次读这条线程：只补最近 BACKFILL_MAX_TURNS 轮，更早的算历史，不倒进侧栏。
            if len(pending) > BACKFILL_MAX_TURNS:
                skipped = pending[:-BACKFILL_MAX_TURNS]
                pending = pending[-BACKFILL_MAX_TURNS:]
                for t in skipped:
                    seen.add(t["requestId"])
                    written.append(t["requestId"])
            self._backfilled_threads.add(thread_id)
        count = 0
        error: str | None = None
        for turn in pending:
            try:
                self.writer.write_turn(turn, thread_id)
            except VoiceConversationError as exc:
                error = str(exc)
                break
            written.append(turn["requestId"])
            seen.add(turn["requestId"])
            count += 1
        record["written"] = written[-MAX_WRITTEN_PER_THREAD:]
        record["lastReadAtUtc"] = _iso(now)
        self._save_state(state)
        self.last_result = {"written": count, "pending": len(pending) - count, "turns": len(turns)}
        if count:
            self._diag("wrote %d turn(s) thread=%s (pending %d)" % (count, thread_id, len(pending) - count))
        if error:
            raise VoiceConversationError(error)


def main(argv: list[str] | None = None) -> int:
    """命令行：看现在语音在哪条线程、绑定文件说什么。不写任何东西。"""
    import argparse

    parser = argparse.ArgumentParser(description="语音对话同步：只读探针")
    parser.add_argument("--since-minutes", type=float, default=30.0)
    args = parser.parse_args(argv)
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    now = time.time()
    found = locate_voice_thread(profile / ".codex" / "sessions", since_epoch=now - args.since_minutes * 60, now=now)
    print(json.dumps({
        "found": None if found is None else {**found, "rollout": str(found["rollout"]), "evidenceAtUtc": _iso(found["evidenceAt"])},
        "pointer": pointer_thread(profile / ".codex" / ".codex-global-state.json"),
        "binding": read_binding(default_binding_path()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
