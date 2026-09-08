#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""situation_signals — 情境信号的**封闭词汇表**：AI 判断和自动触发共用的同一套事实。

    python situation_signals.py            # 人/AI 读的紧凑文本
    python situation_signals.py --json     # 机器可读
    python situation_signals.py --vocab    # 只列有哪些信号、各自值域

## 为什么存在（用户 2026-09-08 拍板）

> 关于分类我的想法其实是我们提供各种判断用的接口，然后 codex 自己使用这些接口
> 绑定各种功能，只是我们要提供一些自动触发用的工具，就像到家自动发信类型，需要
> 提供给 ai 比如到达某地后满足条件返回信号，或者是起床后满足条件等
>
> 不这样做 codex 每次都会自己创建一个新的工具，会很混乱

这是**两类原语里的第一类**：判断接口。第二类（自动触发）在 `situation_triggers.py`，
它的条件只能用这里的信号名 —— 词汇表封闭正是"别每次发明一个新工具"的实现手段。

`judgment_basis.py` 也从这里取数。三个消费者（AI 现场判断、通知路由、自动触发）
读同一份事实，才不会出现"板子说他在家、触发器说他不在"这种自相矛盾。

## 纪律

- **「不知道」和「否」分开**：信号读不到时 `known=False`，值是 None。
  把两者混起来会让"没有数据"悄悄变成一个方向的结论 —— 触发器那边更严重：
  `known=False` 一律**视为条件不成立**，绝不因为读不到就当成立。
- **只报事实，不下结论**："他在忙"是判断不是信号，这里不产出这种东西。
- **每个信号自带年龄**：位置是 30 分钟前的还是 30 秒前的，判断意义完全不同。
- **词汇表是封闭的**：加信号要改这个文件（并同步 `SIGNALS` 里的说明），
  不能由调用方临时塞一个名字进来。这条约束就是这个模块存在的理由。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

CONTRACT = "situation-signals/1"

#: App 上报的在场信号（耳机/前台/设备）落点。由 ReaderPresenceSignal.cs 写。
PRESENCE_FILE_NAME = "presence-signal.json"
#: 在场信号多久算过期：耳机拔了 App 会立刻再报一次，超过这个岁数说明 App 没在跑。
PRESENCE_FRESH_MINUTES = 30.0


def default_root() -> Path:
    """ReaderPC 的本地数据根（%LOCALAPPDATA%\\BWReader）。"""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def bridge_runtime() -> Path:
    """桥的 runtime 目录。⚠ 与 BWReader 根**不是同一个目录** —— 位置/通话在这边。"""
    return (Path(os.environ.get("USERPROFILE") or Path.home())
            / "bw-computer-voice-bridge" / "runtime")


def _load(path: Path) -> dict[str, Any] | None:
    """读一个 JSON；读不出=None。**绝不**默默补默认值。"""
    try:
        value = json.loads(path.read_text("utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _age_minutes(ms: Any, now_ms: int) -> float | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return max(0.0, (now_ms - float(ms)) / 60000.0)


def unknown(why: str) -> dict[str, Any]:
    """一个读不到的信号。why 要说清是"没这个文件"还是"数据太旧"。"""
    return {"known": False, "value": None, "ageMinutes": None, "why": why}


def known(value: Any, age_minutes: float | None = None) -> dict[str, Any]:
    return {"known": True, "value": value, "ageMinutes": age_minutes}


# ─────────────────────────── 各信号的取数 ───────────────────────────
#
# 每个函数签名一致 (root, runtime, now_ms) -> 信号字典。加信号照抄这个形状。


def _sig_place(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    place = _load(runtime / "current-place.json")
    if place is None:
        # 文件不存在 = 从来没有过定位记录，**不是**"他不在家"。
        return unknown("还没有任何定位记录")
    age = _age_minutes(place.get("observedAtUtcMs"), now_ms)
    state = place.get("state")
    if not isinstance(state, str) or not state:
        return unknown("定位记录里没有 state")
    return known(state, age)


def _sig_place_alias(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    place = _load(runtime / "current-place.json")
    if place is None:
        return unknown("还没有任何定位记录")
    alias = place.get("alias")
    if not isinstance(alias, str) or not alias:
        # 有坐标但没起过名字：这是"不知道叫什么"，不是"不在任何地方"。
        return unknown("当前位置还没有别名")
    return known(alias, _age_minutes(place.get("observedAtUtcMs"), now_ms))


def _sig_awake(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    import replication_notifications as rn
    awake, why = rn.looks_awake(root)
    # looks_awake 判不出来时返回 True 并在 why 里说明 —— 那是**给通知用的**
    # 保守默认（宁可出声也不神秘消失）。信号这边不能照抄那个默认：
    # 触发器拿"按醒着处理"当真会在半夜 fire。所以读不到活动就报不知道。
    if "读不到" in why:
        return unknown(why)
    return dict(known(awake), why=why)


def _sig_idle_minutes(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    import replication_notifications as rn
    last = rn.last_user_activity_ms(root)
    if last is None:
        return unknown("账本里没有用户操作记录")
    return known(round(max(0.0, (now_ms - last) / 60000.0), 1))


def _sig_woke_at_hour(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    import replication_notifications as rn
    woke = rn.wake_time_today_ms(root)
    if woke is None:
        # 通宵没睡的日子本来就没有"起床"这回事 —— 报不知道，别编一个钟点。
        return unknown("今天找不到起床点（可能通宵，或没有活动记录）")
    local = time.localtime(woke / 1000.0)
    return known(local.tm_hour, _age_minutes(woke, now_ms))


def _sig_in_review_window(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    import replication_notifications as rn
    schedule = rn.review_schedule(root)
    start = int(schedule.get("wakeHour", rn.REVIEW_NEW_WINDOW_HOURS[0]))
    end = int(schedule.get("sleepHour", rn.REVIEW_NEW_WINDOW_HOURS[1]))
    hour = time.localtime(now_ms / 1000.0).tm_hour
    # 含头不含尾；end 可以是 24（当天结束）也可以跨零点（如 8→2）。
    inside = (start <= hour < end) if start < end else (hour >= start or hour < end)
    return dict(known(inside), window=[start, end], hour=hour)


def _sig_local_hour(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    return known(time.localtime(now_ms / 1000.0).tm_hour)


def _presence(root: Path, now_ms: int) -> tuple[dict[str, Any] | None, float | None]:
    """在场信号 + 它的岁数。太旧的当没有 —— 耳机状态是**现状**，旧值毫无意义。"""
    value = _load(root / PRESENCE_FILE_NAME)
    if value is None:
        return None, None
    age = _age_minutes(value.get("atMs"), now_ms)
    if age is None or age > PRESENCE_FRESH_MINUTES:
        return None, age
    return value, age


def _presence_missing(age: float | None) -> dict[str, Any]:
    return unknown("App 没报过在场信号"
                   if age is None else "在场信号已 %.0f 分钟没更新" % age)


def _sig_audio_route(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    value, age = _presence(root, now_ms)
    if value is None:
        return _presence_missing(age)
    route = value.get("audioRoute")
    if not isinstance(route, str) or not route:
        return unknown("在场信号里没有 audioRoute")
    return known(route, age)


def _sig_headphones(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    route = _sig_audio_route(root, runtime, now_ms)
    if not route["known"]:
        return route
    # speaker / receiver 都算"没戴"；其余（headphones / bluetooth / airplay / usb）算戴了。
    return known(route["value"] not in ("speaker", "receiver"), route["ageMinutes"])


def _sig_app_foreground(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    value, age = _presence(root, now_ms)
    if value is None:
        return _presence_missing(age)
    flag = value.get("foreground")
    if not isinstance(flag, bool):
        return unknown("在场信号里没有 foreground")
    return known(flag, age)


def _status(root: Path, now_ms: int) -> tuple[dict[str, Any] | None, float | None]:
    status = _load(root / "readerpc-server.status.json")
    if status is None:
        return None, None
    age = _age_minutes(status.get("updatedAtEpochMs"), now_ms)
    # 心跳陈旧时整个文件都是过去时 —— 里面每个字段都不能当现状用。
    if age is None or age > 5.0:
        return None, age
    return status, age


def _status_missing(age: float | None) -> dict[str, Any]:
    return unknown("ReaderPC 没在跑"
                   if age is None else "ReaderPC 心跳已 %.0f 分钟没更新" % age)


def _sig_voice_linked(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    status, age = _status(root, now_ms)
    if status is None:
        return _status_missing(age)
    voice = status.get("voice") or {}
    flag = voice.get("readerConnected")
    if not isinstance(flag, bool):
        return unknown("状态文件里没有 voice.readerConnected")
    return known(flag, age)


def _sig_reading_title(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    status, age = _status(root, now_ms)
    if status is None:
        return _status_missing(age)
    context = status.get("readerContext") or {}
    title = str(context.get("title") or "").strip()
    # 判据是**标题非空**而不是 available：reader 断开后 available 还是 true。
    #
    # ⚠ 空标题报的是 known("")，**不是** unknown：ReaderPC 在跑而没有在读的东西，
    # 这是一件"已知的事"，不是"不知道"。第一版把它写成 unknown，等于把自己
    # 定的纪律用反了 —— 而后果是实的：`{"reading_title": {"not": ""}}`
    # （在读点什么）这类条件永远不成立，因为不可用的信号一律判不成立。
    return known(title, _age_minutes(context.get("updatedAtEpochMs"), now_ms))


def _review_counts(root: Path) -> tuple[int, int] | None:
    """(到期, 新卡) 或 None=看不到数据域。

    ⚠ `count_due_cards` 在数据目录缺失时返回 (0, 0) —— 那是**给通知用的**
    宽容默认。信号这边必须把它掰回"不知道"：一条 `{"review_new": {"lte": 0}}`
    的规则会因为"看不到数据"而误以为"已经清空了"，然后触发一条假通知。
    这正是本模块开头禁止的那种错，所以在这里拦住。
    """
    if not (root / "replication-data").is_dir():
        return None
    import replication_notifications as rn
    return rn.count_due_cards(root)


def _sig_review_due(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    counts = _review_counts(root)
    return unknown("还没有复制过来的卡片数据") if counts is None else known(counts[0])


def _sig_review_new(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    counts = _review_counts(root)
    return unknown("还没有复制过来的卡片数据") if counts is None else known(counts[1])


#: AnkiConnect 的地址。只做 TCP 连通性探测，不发请求 ——
#: 信号要快（read_all 会把 15 个信号全读一遍），而"端口开着"已经足以
#: 区分「Anki 在跑」和「Anki 没开」这两种情况。
ANKI_CONNECT_HOST = "127.0.0.1"
ANKI_CONNECT_PORT = 8765


#: 阅读器上下文快照。复习模式的状态就投影在它的 activeReading.review 里 ——
#: 这条链早就存在（RC.review.snapshotState → 桥 DirectContextSnapshot 校验 → 落盘），
#: 2026-09-09 只是把它读成信号。
CONTEXT_SNAPSHOT_FILE_NAME = "reader-context-snapshot.json"
#: 快照多久算过期。阅读器每几十秒推一次，两分钟没动就别拿它当现状。
CONTEXT_FRESH_MINUTES = 2.0


def _review_state(runtime: Path, now_ms: int) -> tuple[dict[str, Any] | None, str]:
    """(复习投影, 说不出来时的原因)。

    三种「不知道」要分开：没有快照文件 / 阅读器没连（contextStatus=disabled）/
    快照太旧。混成一个会让「他没在复习」和「我看不见」变成同一句话，
    而触发器拿后者当前者就会在他正复习时判定没在复习。
    """
    value = _load(runtime / CONTEXT_SNAPSHOT_FILE_NAME)
    if value is None:
        return None, "还没有阅读器上下文快照"
    if value.get("contextStatus") in ("disabled", "pending"):
        return None, "阅读器没连上（快照 %s）" % value.get("contextStatus")
    active = value.get("activeReading")
    if not isinstance(active, dict):
        # 快照在、阅读器也连着，但没有在读的东西 —— 那就**确实**没在复习。
        return {}, ""
    return (active.get("review") if isinstance(active.get("review"), dict)
            else {}), ""


def _sig_reviewing(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    """他现在是不是在复习卡片（2026-09-09 用户要的"复习开始"信号）。

    用法是**边沿**：注册一条 `--when '{"reviewing": true}'` 的触发规则，
    他一进复习模式就会收到信号 —— 用户原话「在后方等待我主动点击那个复习
    模式后返回开始复习……对他来说这其实就只是一次工具调用」。

    ⚠ 「字段缺席 = 未进入复习模式」是快照链本来的语义（旧构建不发这个字段
    也是同一意思），所以这里把缺席读成 False 而不是 unknown。
    真正的 unknown 只有一种：快照本身不可用（见 `_review_state`）。
    """
    review, why = _review_state(runtime, now_ms)
    if review is None:
        return unknown(why)
    return known(bool(review))


def _sig_review_remaining(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    """这一轮复习还剩几张。没在复习时是 0（不是不知道）。"""
    review, why = _review_state(runtime, now_ms)
    if review is None:
        return unknown(why)
    if not review:
        return known(0)
    total = review.get("dueTotal")
    index = review.get("index")
    if not isinstance(total, int) or not isinstance(index, int):
        return unknown("复习投影里没有 dueTotal/index")
    return known(max(0, total - index))


def _sig_anki_reachable(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    """Anki 开着吗（2026-09-09）。

    为什么这是个情境信号而不是内部细节：**第一次评分完全依赖它**。
    Reader 自己不排期 —— `/pdf/api/review-answer` 必须有真实 Anki 卡号、
    走 AnkiConnect 的 answerCards。Anki 没开时，新卡导不出去、评不了分，
    而这台机器上 10 张卡就这么挂了 18 天没人发现。

    ⚠ 探的是**端口通不通**，不是 AnkiConnect 答不答得对。端口开着而插件
    坏掉的情形这里报"通" —— 那是另一种故障，不该由一个要跑得快的信号
    去承担；真正的失败会在导出回执里显形。
    """
    import socket
    try:
        with socket.create_connection(
                (ANKI_CONNECT_HOST, ANKI_CONNECT_PORT), timeout=0.4):
            return known(True)
    except OSError:
        # 连不上就是没开。这里**不报 unknown**：拒连是一个确定的事实，
        # 跟"读不到文件"不一样。
        return known(False)


def _sig_readerpc_running(root: Path, runtime: Path, now_ms: int) -> dict[str, Any]:
    status, age = _status(root, now_ms)
    return known(status is not None, age)


#: **封闭词汇表**。键 = 触发条件里能写的信号名；改这里才能加信号。
#: summary 是给 AI 读的一句话，values 是值域 —— 两者都会随 --vocab 端到 AI 面前，
#: 所以写反比不写更糟（AI 看到错的值域会直接放弃用这个信号）。
SIGNALS: dict[str, dict[str, Any]] = {
    "place": {
        "summary": "现在在哪（按位置别名归类）",
        "values": "home / work / out；没有定位记录时 known=false",
        "read": _sig_place,
    },
    "place_alias": {
        "summary": "当前位置的别名原文，如「家」「工作地点」",
        "values": "字符串；没起过名字时 known=false",
        "read": _sig_place_alias,
    },
    "awake": {
        "summary": "此刻像不像醒着（健康信号优先，设备活动兜底）",
        "values": "true / false",
        "read": _sig_awake,
    },
    "idle_minutes": {
        "summary": "距上一次用户操作多少分钟",
        "values": "数字（分钟）",
        "read": _sig_idle_minutes,
    },
    "woke_at_hour": {
        "summary": "今天几点起的（0-23）",
        "values": "整数 0-23；通宵/无记录时 known=false",
        "read": _sig_woke_at_hour,
    },
    "in_review_window": {
        "summary": "现在在不在复习时间窗内（起床~睡前）",
        "values": "true / false",
        "read": _sig_in_review_window,
    },
    "local_hour": {
        "summary": "本地时钟的小时（0-23）",
        "values": "整数 0-23",
        "read": _sig_local_hour,
    },
    "audio_route": {
        "summary": "App 的音频输出走哪儿",
        "values": "speaker / receiver / headphones / bluetooth / airplay / usb / other",
        "read": _sig_audio_route,
    },
    "headphones": {
        "summary": "戴着耳机吗（audio_route 折出来的布尔）",
        "values": "true / false",
        "read": _sig_headphones,
    },
    "app_foreground": {
        "summary": "App 是不是在前台",
        "values": "true / false",
        "read": _sig_app_foreground,
    },
    "voice_linked": {
        "summary": "语音链路通不通（阅读器已连）",
        "values": "true / false",
        "read": _sig_voice_linked,
    },
    "reading_title": {
        "summary": "正在读的东西的标题",
        "values": '字符串；什么都没在读时是空串（"在读点什么"写 {"not":""}）；'
                  "ReaderPC 没在跑时 known=false",
        "read": _sig_reading_title,
    },
    "review_due": {
        "summary": "到期待复习卡片数",
        "values": "整数",
        "read": _sig_review_due,
    },
    "review_new": {
        "summary": "还没复习过的新卡数",
        "values": "整数",
        "read": _sig_review_new,
    },
    "readerpc_running": {
        "summary": "PC 服务器软件在不在跑",
        "values": "true / false",
        "read": _sig_readerpc_running,
    },
    "reviewing": {
        "summary": "他现在在不在复习卡片",
        "values": "true / false；阅读器没连时 known=false",
        "read": _sig_reviewing,
    },
    "review_remaining": {
        "summary": "这一轮复习还剩几张",
        "values": "整数；没在复习时是 0",
        "read": _sig_review_remaining,
    },
    "anki_reachable": {
        "summary": "Anki 开着吗（第一次评分完全依赖它）",
        "values": "true / false",
        "read": _sig_anki_reachable,
    },
}


def read_all(
    root: Path | None = None,
    runtime: Path | None = None,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """全部（或指定的）信号。一个信号取数炸了不连坐其它 —— 它自己报 known=false。"""
    root = root or default_root()
    runtime = runtime or bridge_runtime()
    now_ms = int(time.time() * 1000)
    wanted = names or list(SIGNALS)
    signals: dict[str, Any] = {}
    for name in wanted:
        spec = SIGNALS.get(name)
        if spec is None:
            signals[name] = unknown("没有这个信号（词汇表是封闭的，见 --vocab）")
            continue
        try:
            signals[name] = spec["read"](root, runtime, now_ms)
        except Exception as error:  # noqa: BLE001
            # 一个信号读炸了必须**出声**：静默当成"不知道"会让触发器永远不 fire
            # 而没有一处说得出为什么（silent-failure-lessons 第五条）。
            signals[name] = unknown("取数失败：%s" % str(error)[:120])
    return {"contract": CONTRACT, "atUtcMs": now_ms, "signals": signals}


def matches(signal: dict[str, Any], expected: Any) -> tuple[bool, str]:
    """一个信号对一条期望的判定。(成立吗, 原因)

    ⚠ `known=False` 一律**不成立**：读不到位置不等于"不在家"。
    这是整套触发机制最容易被绕过的一条，所以判定只写在这一处。

    expected 的形状（封闭，多一种都不认）：
      标量            相等
      {"not": v}      不等
      {"in": [...]}   属于
      {"gte": n}      数值 >=
      {"lte": n}      数值 <=
    """
    if not signal.get("known"):
        return False, "信号不可用（%s）" % signal.get("why", "原因未记录")
    value = signal.get("value")
    if isinstance(expected, dict):
        if len(expected) != 1:
            return False, "条件对象只能有一个比较器"
        operator, operand = next(iter(expected.items()))
        if operator == "not":
            return value != operand, "%r != %r" % (value, operand)
        if operator == "in":
            if not isinstance(operand, list):
                return False, "in 的操作数必须是数组"
            return value in operand, "%r in %r" % (value, operand)
        if operator in ("gte", "lte"):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False, "%r 不是数字，比不了大小" % (value,)
            if not isinstance(operand, (int, float)) or isinstance(operand, bool):
                return False, "%s 的操作数必须是数字" % operator
            ok = value >= operand if operator == "gte" else value <= operand
            return ok, "%r %s %r" % (value, operator, operand)
        return False, "不认识的比较器 %r（只有 not/in/gte/lte）" % (operator,)
    return value == expected, "%r == %r" % (value, expected)


def vocab_text() -> str:
    """给 AI 看的词汇表。它读完就该知道能写什么，不必再去翻源码。"""
    lines = ["情境信号（触发条件只能用这些名字）："]
    for name, spec in SIGNALS.items():
        lines.append("  %-18s %s" % (name, spec["summary"]))
        lines.append("  %-18s   值域：%s" % ("", spec["values"]))
    lines.append("")
    lines.append('比较器：标量=相等 / {"not":v} / {"in":[..]} / {"gte":n} / {"lte":n}')
    lines.append("⚠ 信号读不到时条件一律**不成立**（不知道 != 否）。")
    return "\n".join(lines)


#: 紧凑行里每个信号怎么写成一小段。值 = (标签, 折成短语的函数)。
#:
#: 用户 2026-09-08：「这些信息要尽可能的紧凑和简洁防止造成混乱」。
#: 逐行列 15 个信号对 AI 是噪音 —— 它每次判断都要从一屏字里挑出有用的两三条。
#: 所以**默认就是一行**，逐行版留给 `--full`。
#:
#: ⚠ 紧凑不等于省掉"不知道"：读不到的信号写成 `名字?`，而不是不写。
#: 不写会让 AI 以为那一项是否定的 —— 那正是这个模块从头到尾在防的事。
_COMPACT: dict[str, Any] = {
    "place": lambda v: {"home": "在家", "work": "在工作"}.get(v, "在外(%s)" % v),
    "awake": lambda v: "醒着" if v else "像在睡",
    "local_hour": lambda v: "%d点" % v,
    "in_review_window": lambda v: "窗内" if v else "窗外",
    "headphones": lambda v: "戴耳机" if v else "没耳机",
    "voice_linked": lambda v: "语音通" if v else "语音断",
    "review_new": lambda v: "新卡%d" % v,
    "review_due": lambda v: "到期%d" % v,
    "reading_title": lambda v: ("在读《%s》" % v[:14]) if v else "未在读",
    "idle_minutes": lambda v: "闲%d分" % round(v),
    "readerpc_running": lambda v: "PC在" if v else "PC停",
    "anki_reachable": lambda v: "Anki在" if v else "Anki没开",
    "reviewing": lambda v: "正在复习" if v else "",
    "review_remaining": lambda v: "还剩%d张" % v if v else "",
}
#: 紧凑行里省略不写的（信息量低于占位成本）。**只省已知为真且无歧义的**。
#: Anki 开着是常态，不占位；**没开才说** —— 那正是新卡评不了分的原因。
#: 没在复习是常态，不占位；**正在复习才说**。剩几张同理。
_COMPACT_SKIP_WHEN = {"readerpc_running": True, "voice_linked": True,
                      "anki_reachable": True,
                      "reviewing": False, "review_remaining": 0}


def render_compact(payload: dict[str, Any]) -> str:
    """一行说完现在什么状况。给板子和 AI 的默认形态。

    读不到的信号收尾成 `名字?` 的形式一起列出 —— 缺项必须看得见，
    但不必每个占一行。
    """
    parts: list[str] = []
    unknown_names: list[str] = []
    for name, fold in _COMPACT.items():
        signal = payload["signals"].get(name)
        if signal is None:
            continue
        if not signal.get("known"):
            unknown_names.append(name)
            continue
        value = signal.get("value")
        if _COMPACT_SKIP_WHEN.get(name) == value:
            continue
        try:
            parts.append(fold(value))
        except Exception:  # noqa: BLE001
            parts.append("%s=%s" % (name, value))
    running = payload["signals"].get("readerpc_running") or {}
    if running.get("known") and running.get("value") is False:
        # PC 停着时语音链和在读什么**必然**不可用，再列一遍 ?xxx 是纯噪音 ——
        # "PC停"已经把原因说全了。紧凑的意思正是别重复同一件事。
        unknown_names = [one for one in unknown_names
                         if one not in ("voice_linked", "reading_title")]
    if ("reviewing" in unknown_names
            and "review_remaining" in unknown_names):
        # 同一个原因（阅读器没连）导致的两个未知，说一次就够 ——
        # 紧凑的意思正是别重复同一件事。
        unknown_names.remove("review_remaining")
    if unknown_names:
        parts.append("?" + "/".join(unknown_names))
    place = payload["signals"].get("place") or {}
    age = place.get("ageMinutes")
    if place.get("known") and isinstance(age, (int, float)) and age >= 60:
        # 位置旧到这个程度必须标出来：拿两小时前的位置当现状是最容易犯的错。
        parts.append("位置已%d分钟未更新" % round(age))
    return " ".join(parts) if parts else "什么都不知道"


def render(payload: dict[str, Any]) -> str:
    """一行一个信号，「不知道」直说。"""
    lines = ["情境信号（%s）：" % time.strftime(
        "%m-%d %H:%M", time.localtime(payload["atUtcMs"] / 1000.0))]
    for name, signal in payload["signals"].items():
        if not signal.get("known"):
            lines.append("  %-18s 不知道 —— %s" % (name, signal.get("why", "")))
            continue
        age = signal.get("ageMinutes")
        suffix = "" if age is None else "（%.0f 分钟前）" % age
        lines.append("  %-18s %s%s" % (name, signal.get("value"), suffix))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="情境信号词汇表")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--vocab", action="store_true", help="只列信号名和值域")
    parser.add_argument("--signal", action="append", help="只读指定信号（可重复）")
    parser.add_argument("--full", action="store_true",
                        help="逐行列出（含年龄和「为什么不知道」）；默认是紧凑一行")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--runtime", type=Path, default=None)
    args = parser.parse_args()
    if args.vocab:
        if args.json:
            print(json.dumps({
                "contract": CONTRACT,
                "signals": {name: {"summary": spec["summary"],
                                   "values": spec["values"]}
                            for name, spec in SIGNALS.items()},
            }, ensure_ascii=False, indent=2))
        else:
            print(vocab_text())
        return 0
    payload = read_all(args.root, args.runtime, args.signal)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        # 默认紧凑（用户 2026-09-08：信息要尽可能紧凑简洁防止混乱）；
        # 要逐条看年龄和"为什么不知道"时才用 --full。
        print(render(payload) if args.full else render_compact(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
