"""voice_autoclose — 语音智能关闭（2026-09-09 用户拍板）。

**为什么只做关闭，不做启动。**
语音按**时间**计费，浪费全在"开着但没在说"。而启动至今无解：F24 是全局
切换，要先知道当前状态才敢按，而状态只能从麦克风台账读、有 5 秒级延迟 ——
读错就做反。用户原话：「虽然启动还是无法搞定，但是可以做到依靠主动推送
进行稳定关闭」。

**为什么关闭这次能做。**
Codex 有内置工具 ``end_realtime_voice_call``，而我们已有一条推送通道能把话
送进正在通话的那条线程。它是**有方向的**：对面不在通话时只是空转 ——
判断错的代价从"做反"降级成"白做一次"。对比 F24：按在"其实已挂断"上会
**反向开一通**并开始计费。

**三条铁律。**

1. **推送当执行器，台账只当校验器。** 传输成功 ≠ 已挂断。这一带最容易出的
   交待就是把"送出去了"说成"关掉了"。
2. **失败判定至少 GRACE_SECONDS。** 用户明确要求「不能太快，至少也要等个
   两分钟」。实测委派→最终回答 中位 11.1s / P90 36.3s，120 秒能盖住绝大多数。
3. **信号说"不知道"时不动手。** 关闭会打断人说话；不确定就不关，宁可多烧
   一会儿。只有 known 且成立的条件才算数。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

try:  # pragma: no cover - 非 Windows 上没有
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

CONTRACT = "reader-voice-autoclose/1"

#: 桥的地址。**复用登记器那份**，不在这里再写一遍 —— 两处各写一个地址，
#: 改了一处的表现是"注册好好的，挂断永远发不出去"。
#: ⚠ 只能走 tailnet 那条：回环 127.0.0.1:43128 会被 origin 闸以 403 拒掉
#: （2026-09-09 实测）。
# 有意再导出：readerpc_launcher 用 voice_autoclose.ENDPOINT，
# 而地址只在 codex_push_register 里写一份。写成显式赋值而不是
# `import ... as`，是为了让它是一次**真正的使用** —— pyflakes 不认
# noqa，而"为了消警告去删一行有用的代码"是更坏的结局。
import codex_push_register  # noqa: E402

ENDPOINT = codex_push_register.DEFAULT_ENDPOINT
import voice_status_receipt  # noqa: E402

#: 麦克风使用台账。与 C# 的 WindowsRegistryCodexVoiceActivitySource.RegistryPath
#: **必须是同一个键**：两边读同一份事实，判据也照抄（见 _ledger_active）。
LEDGER_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone\OpenAI.Codex_2p2nqsd0c76g0"
)
LEDGER_START_VALUE = "LastUsedTimeStart"
LEDGER_STOP_VALUE = "LastUsedTimeStop"

#: 送出挂断请求后，至少等这么久才允许说"没成"。用户拍的板。
GRACE_SECONDS = 120.0
#: 等待期间多久看一次台账。台账本身是 5 秒级粒度，再密没有意义。
POLL_SECONDS = 5.0
#: 推送最多试几次（第一次 + 重试一次）。再多就该让人知道了。
MAX_PUSH_ATTEMPTS = 2

#: 默认闲置阈值（分钟）。用户定的 20。
DEFAULT_IDLE_MINUTES = 20
#: 「长时间没在读」用的阈值（分钟）。读书中途翻个页不该被判成没在读。
DEFAULT_READER_IDLE_MINUTES = 20

#: 偏好键 → 默认值。设置页与这里共用这张表，别在两处各写一份。
PREFERENCE_DEFAULTS: dict[str, Any] = {
    "voiceAutoClose": False,            # 独立总开关；关 = 持续开启模式
    "voiceAutoCloseIdleMinutes": DEFAULT_IDLE_MINUTES,
    "voiceAutoCloseOnIdle": True,
    "voiceAutoCloseOnSleep": True,
    "voiceAutoCloseOnPlaceChange": True,
    "voiceAutoCloseOnReaderIdle": True,
}


def normalize_preferences(value: object) -> dict[str, Any]:
    """从任意偏好 dict 里取出这一组，缺的补默认、类型不对的回默认。

    ⚠ 字段表只在 PREFERENCE_DEFAULTS 里写一份：load/save/设置页都调这里。
    「同一张字段表抄两遍」正是这个仓库反复吃亏的形态。
    """
    source = value if isinstance(value, dict) else {}
    out: dict[str, Any] = {}
    for key, default in PREFERENCE_DEFAULTS.items():
        got = source.get(key, default)
        if isinstance(default, bool):
            out[key] = got is True
        else:
            # 分钟数：整数且落在有意义的范围内，否则回默认。
            out[key] = (
                int(got)
                if isinstance(got, int) and not isinstance(got, bool)
                and 1 <= got <= 480
                else default
            )
    return out


class VoiceAutoCloseError(RuntimeError):
    """这一带的失败一律带原因，不折成布尔。"""


# ── 台账（校验器）─────────────────────────────────────────────────────────
def read_ledger(
    open_key: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """读麦克风使用台账。

    返回 ``{"known": bool, "active": bool|None, "start": int, "stop": int,
    "why": str}``。⚠ 读不到时 ``known=False`` 且 ``active=None`` ——
    **不是** False。把"不知道"折成"没在通话"会让兜底那步在真通话时按下
    F24，而那正是要防的事故。
    """
    if winreg is None:
        return {"known": False, "active": None, "start": 0, "stop": 0,
                "why": "非 Windows，没有这个台账"}
    opener = open_key or winreg.OpenKey
    try:
        with opener(winreg.HKEY_CURRENT_USER, LEDGER_KEY) as key:
            start, _ = winreg.QueryValueEx(key, LEDGER_START_VALUE)
            stop, _ = winreg.QueryValueEx(key, LEDGER_STOP_VALUE)
    except OSError as error:
        return {"known": False, "active": None, "start": 0, "stop": 0,
                "why": "读不到台账：%s" % str(error)[:120]}
    start = int(start or 0)
    stop = int(stop or 0)
    return {"known": True, "active": _ledger_active(start, stop),
            "start": start, "stop": stop, "why": ""}


def _ledger_active(start: int, stop: int) -> bool:
    """与 C# 的 CodexVoiceActivitySnapshot.Active 逐字同义。

    ⚠ 这是同一条判据的第二份副本（另一份在 CodexVoiceActivity.cs）。改一处
    就要改两处 —— 契约测试会盯着这件事。
    """
    return start > 0 and (stop == 0 or start > stop)


# ── 条件（策略）───────────────────────────────────────────────────────────
def _known(signals: dict[str, Any], name: str) -> Any:
    """只认 known 的信号值；不知道就返回 None（于是条件不成立）。"""
    signal = signals.get(name) or {}
    return signal.get("value") if signal.get("known") else None


def _cond_idle(state: "AutoCloseState", signals, prefs, now_ms) -> str | None:
    limit = int(prefs.get("voiceAutoCloseIdleMinutes")
                or DEFAULT_IDLE_MINUTES)
    idle = _known(signals, "idle_minutes")
    if idle is None or idle < limit:
        return None
    return "闲置 %d 分钟（阈值 %d）" % (int(idle), limit)


def _cond_sleep(state: "AutoCloseState", signals, prefs, now_ms) -> str | None:
    awake = _known(signals, "awake")
    if awake is not False:
        return None
    return "已经睡着"


def _cond_place(state: "AutoCloseState", signals, prefs, now_ms) -> str | None:
    """离开通话开始时所在的地点。

    ⚠ 判的是**变化**不是某个特定值：从家到公司也算离开。所以要记住通话开始
    那一刻的地点 —— 只看当前值没法区分"一直在家"和"刚回到家"。
    """
    place = _known(signals, "place")
    if place is None or state.place_at_start is None:
        return None
    if place == state.place_at_start:
        return None
    return "已离开通话开始时的地点（%s → %s）" % (state.place_at_start, place)


def _cond_reader_idle(
    state: "AutoCloseState", signals, prefs, now_ms
) -> str | None:
    """阅读器关了，或者长时间没在读。

    ⚠ 「长时间」必须真的计时：读书中途翻页、切窗口都会让 reading_title 空
    一小会儿，拿瞬时值判会在人还在读的时候把语音掐了。
    """
    running = _known(signals, "readerpc_running")
    if running is False:
        return "阅读器已关闭"
    title = _known(signals, "reading_title")
    if title is None:
        return None
    if title:
        return None
    if state.not_reading_since_ms is None:
        return None
    minutes = (now_ms - state.not_reading_since_ms) / 60000.0
    limit = int(prefs.get("voiceAutoCloseIdleMinutes")
                or DEFAULT_READER_IDLE_MINUTES)
    if minutes < limit:
        return None
    return "已经 %d 分钟没在读（阈值 %d）" % (int(minutes), limit)


#: **封闭条件表**。键 = 偏好开关名；值 = 说明 + 判据。
#:
#: 用户 2026-09-09 选的四条，逐条由他勾选启停；设置页读的也是这张表。
CONDITIONS: dict[str, dict[str, Any]] = {
    "voiceAutoCloseOnIdle": {
        "label": "闲置超过设定分钟数",
        "signals": ("idle_minutes",),
        "evaluate": _cond_idle,
    },
    "voiceAutoCloseOnSleep": {
        "label": "进入睡眠",
        "signals": ("awake",),
        "evaluate": _cond_sleep,
    },
    "voiceAutoCloseOnPlaceChange": {
        "label": "离开通话开始时的地点",
        "signals": ("place",),
        "evaluate": _cond_place,
    },
    "voiceAutoCloseOnReaderIdle": {
        "label": "阅读器关了或长时间没在读",
        "signals": ("readerpc_running", "reading_title"),
        "evaluate": _cond_reader_idle,
    },
}


class AutoCloseState:
    """跨轮次的记忆。只在**这一通**通话内有效，挂断即清。"""

    def __init__(self) -> None:
        self.place_at_start: str | None = None
        self.not_reading_since_ms: int | None = None
        self.call_seen = False

    def observe(self, signals: dict[str, Any], now_ms: int,
                in_call: bool) -> None:
        if not in_call:
            self.__init__()          # 通话结束，忘掉这一通的记忆
            return
        if not self.call_seen:
            self.call_seen = True
            self.place_at_start = _known(signals, "place")
        title = _known(signals, "reading_title")
        if title:
            self.not_reading_since_ms = None
        elif title == "" and self.not_reading_since_ms is None:
            self.not_reading_since_ms = now_ms


def evaluate(
    state: AutoCloseState,
    signals: dict[str, Any],
    prefs: dict[str, Any],
    now_ms: int,
) -> str | None:
    """该不该关。返回原因，或 None（不关）。

    总开关关着 = 持续开启模式，这里永远返回 None。
    """
    if not prefs.get("voiceAutoClose"):
        return None
    for key, spec in CONDITIONS.items():
        if not prefs.get(key):
            continue
        reason = spec["evaluate"](state, signals, prefs, now_ms)
        if reason:
            return reason
    return None


# ── 执行（推送 → 等 → 重试 → 兜底）────────────────────────────────────────
def _post(endpoint: str, body: dict[str, Any], timeout: float = 15.0
          ) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {"detail": "（回应不是 JSON）"}
    except OSError as error:
        return 0, {"detail": "连不上桥：%s" % str(error)[:160]}


def codex_home() -> Path:
    """侧栏同步状态所在的目录。"""
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".codex"


def in_call_thread_id(root: Path | None = None) -> str:
    """正在通话的那条线程。

    ⚠ 不能用推送绑定里的 threadId：那是**提示板**推送的目标，通常不是通话
    那条（2026-09-09 实测绑定是 01a0847a 而通话是 01a08560）。侧栏同步一直
    在跟通话线程，读它的 lastGood 即可。

    ⚠ **文件在 `~/.codex/`**（2026-09-10 修）。原来调用方传的是 ReaderPC 的
    local_root（`%LOCALAPPDATA%/BWReader`），那儿根本没有这个文件 —— 于是
    这里永远返回空串，智能关闭永远走"拿不到通话线程 id"那条分支。当时的
    测试用 `tempfile` 目录自己造文件、再把同一个目录传进来，两边都对但**跟
    真实位置无关**，所以一直是绿的。默认值现在直接指向真实位置，参数只留给
    测试覆盖。

    ⚠ 同一份知识在 C# 侧还有一个实现
    （`DirectCodexVoiceControl.InCallThreadId`）——改一处必须改两处。
    """
    path = (root or codex_home()) / "voice-history-sidebar-sync-state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    last_good = value.get("lastGood") or {}
    thread_id = last_good.get("threadId")
    return thread_id if isinstance(thread_id, str) else ""


def query_status(
    *,
    endpoint: str,
    thread_id: str,
    runtime: Path | None = None,
    poster: Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    timeout_seconds: float = 90.0,
) -> dict[str, Any] | None:
    """问对面一次"你现在什么状态"，等它把回执写出来。

    ⚠ **有开销**：每问一次对面都要跑一轮。Codex 在交接里专门点了「不适合高频
    轮询」。所以这条只在**决策点**用一次 —— 绝不放进 5 秒的等待循环里。

    ⚠ 送出去 ≠ 已回答：`statusRequested` 只说明消息被接收。判断以**回执账本**
    为准（同样是 Codex 点的那条：「推送接口接受消息不等于状态回执已写入」）。
    """
    post = poster or _post
    sleep = sleeper or time.sleep
    now = clock or time.monotonic
    request_id = "vsq-" + uuid.uuid4().hex[:16]
    code, reply = post(endpoint, {
        "statusQuery": True,
        "threadId": thread_id,
        "requestId": request_id,
        "validSeconds": int(timeout_seconds),
    })
    if code != 200 or reply.get("statusRequested") is not True:
        return None
    deadline = now() + timeout_seconds
    while now() < deadline:
        sleep(POLL_SECONDS)
        receipt = voice_status_receipt.read_receipt(request_id, runtime)
        if receipt is not None:
            return receipt
    return None


def close_voice(
    *,
    endpoint: str,
    thread_id: str,
    reason: str,
    ledger_reader: Callable[[], dict[str, Any]] | None = None,
    poster: Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    grace_seconds: float = GRACE_SECONDS,
    allow_shortcut_fallback: bool = True,
    runtime: Path | None = None,
    status_query: Callable[..., dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """把这一通关掉。返回一份**说得清发生了什么**的回执。

    顺序（用户 2026-09-09 拍的）：推送 → 等 ≥grace 并轮询台账 → 重试一次 →
    再等 → 仍在通话才允许 F24 兜底。

    ⚠ 每一步的结论都以**台账**为准，不以传输成功为准。
    """
    read = ledger_reader or read_ledger
    post = poster or _post
    sleep = sleeper or time.sleep
    now = clock or time.monotonic

    steps: list[dict[str, Any]] = []
    request_id = "vac-" + uuid.uuid4().hex[:16]
    # 等待期间台账**有没有一次读得出来**。全程读不出 = 我们是瞎的，
    # 那时按 F24 是赌，而赌错的方向是"反向开一通并开始计费"。
    ledger_seen_known = False

    for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
        code, reply = post(endpoint, {
            "hangUpVoice": True,
            "threadId": thread_id,
            "reason": reason,
            "requestId": "%s#%d" % (request_id, attempt),
        })
        sent = code == 200 and reply.get("hangUpRequested") is True
        steps.append({"step": "push", "attempt": attempt, "http": code,
                      "sent": sent, "note": reply.get("note")
                      or reply.get("detail") or ""})
        # 等满宽限期，其间只要台账说结束了就立刻收工。
        deadline = now() + grace_seconds
        while now() < deadline:
            sleep(POLL_SECONDS)
            ledger = read()
            ledger_seen_known = ledger_seen_known or bool(ledger["known"])
            if ledger["known"] and ledger["active"] is False:
                steps.append({"step": "verify", "closed": True})
                return {"contract": CONTRACT, "closed": True,
                        "by": "push", "attempts": attempt,
                        "reason": reason, "steps": steps}
        steps.append({"step": "verify", "closed": False,
                      "waitedSeconds": round(grace_seconds, 1)})

    if not allow_shortcut_fallback:
        return {"contract": CONTRACT, "closed": False, "by": None,
                "reason": reason, "steps": steps,
                "note": "推送没关掉，且兜底被关闭"}

    # 台账全程读不出来 → 我们是瞎的。这时**问对面一次**（2026-09-09 Codex
    # 交接给出的状态回执通道），别直接去赌 F24。
    # ⚠ 只问这一次：每问一轮对面都要跑，Codex 点名"不适合高频轮询"。
    if not ledger_seen_known:
        ask = status_query or query_status
        receipt = ask(endpoint=endpoint, thread_id=thread_id, runtime=runtime)
        voice_status = (receipt or {}).get("voiceStatus")
        steps.append({"step": "ask", "answered": receipt is not None,
                      "voiceStatus": voice_status,
                      "observedAt": (receipt or {}).get("observedAt")})
        if voice_status == "ended":
            return {"contract": CONTRACT, "closed": True, "by": "receipt",
                    "reason": reason, "steps": steps}
        if voice_status != "active":
            # 还是不知道。不按 F24 —— 不知道时按下去可能反向开一通。
            return {"contract": CONTRACT, "closed": False, "by": None,
                    "reason": reason, "steps": steps,
                    "note": "台账读不到，对面也没说清在不在通话；不按 F24 赌"}

    # 兜底。⚠ 桥那边**会自己再读一次台账**才按 —— F24 是切换，按在"已挂断"
    # 上会反向开一通。这里不替它判断，也不因为自己刚读过就跳过它的复核。
    #
    # ⚠ 按了不等于关了（2026-09-09 实测撞到）：一次落在起通话后几秒的挂断被
    # Codex 的初始化吞掉，台账纹丝不动。桥现在按完会自己确认，没确认就**再按
    # 一次** —— 再按是安全的，因为它每次都先重查台账，已经挂断了就不按。
    pressed = False
    for round_index in (1, 2):
        code, reply = post(endpoint, {"hangUpVoiceFallback": True})
        pressed = code == 200 and reply.get("pressed") is True
        confirmed = reply.get("confirmed")
        steps.append({"step": "shortcut", "round": round_index,
                      "http": code, "pressed": pressed,
                      "confirmed": confirmed,
                      "note": reply.get("skipped") or reply.get("note")
                      or reply.get("detail") or ""})
        # skipped(没按)也算走完：台账说已经不在通话，或者读不到不该赌。
        if not pressed or confirmed is not False:
            break
    if not pressed:
        return {"contract": CONTRACT, "closed": False, "by": None,
                "reason": reason, "steps": steps}
    deadline = now() + grace_seconds
    while now() < deadline:
        sleep(POLL_SECONDS)
        ledger = read()
        if ledger["known"] and ledger["active"] is False:
            return {"contract": CONTRACT, "closed": True, "by": "shortcut",
                    "reason": reason, "steps": steps}
    return {"contract": CONTRACT, "closed": False, "by": None,
            "reason": reason, "steps": steps,
            "note": "按了 F24 台账仍显示在通话"}
