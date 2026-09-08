#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""situation_triggers — 自动触发原语：**一个工具绑定所有情境规则**，不是每种情境一个工具。

    python situation_triggers.py --list
    python situation_triggers.py --vocab
    python situation_triggers.py --add --name 到家做新卡 \
        --when '{"place":"home","awake":true,"review_new":{"gte":10}}' \
        --title "有 10 张新卡可以做了" --ai-action "问他现在做不做，答应就开始逐张过" \
        --recur daily
    python situation_triggers.py --explain 到家做新卡     # 为什么现在没触发
    python situation_triggers.py --remove 到家做新卡
    python situation_triggers.py --evaluate               # 手动跑一轮

## 为什么存在（用户 2026-09-08 拍板）

> 我们提供各种判断用的接口，然后 codex 自己使用这些接口绑定各种功能，只是我们要
> 提供一些自动触发用的工具，就像到家自动发信类型，需要提供给 ai 比如到达某地后
> 满足条件返回信号，或者是起床后满足条件等
>
> 不这样做 codex 每次都会自己创建一个新的工具，会很混乱

两类原语的第二类。条件只能用 `situation_signals.SIGNALS` 里的名字 —— 词汇表封闭 +
出口唯一，加起来才是"别每次发明一个新工具"。AI 要做的只是**声明一条规则**。

## 「到达某地」和「起床后」为什么不需要各自一个专用条件

因为它们是**同一件事的边沿**：

  到家   = place 信号从非 home 变成 home
  起床后 = awake 信号从 false 变成 true

所以引擎只做一件事：**上升沿检测**。条件本身写成"状态的合取"，什么时候算"刚发生"
由引擎负责。多一个 `arrived_at` 谓词只会造出第二条判定路径，而两条路径迟早不一致。

## 纪律（每条都有来历）

- **上升沿，不是电平**：条件成立期间只在**变成成立的那一刻**触发一次。
  否则"在家 + 有新卡"会每轮对账都催一遍。
- **注册时先记基线，不立刻触发**：注册那一刻条件已经成立的，记下 `lastMatch=true`
  但不 fire。已经成立的事 AI 当场就能做，不需要绕一趟触发器；而"一注册就响"
  会让 AI 每次试探性注册都吵到用户一次。
- **信号不可用 = 条件不成立**（判定在 `situation_signals.matches` 一处）。
  读不到位置不等于"不在家"。
- **出口唯一**：触发只会**建一条通知**，走既有的 NotificationStore + deliver 档。
  不执行任意命令 —— 那既是安全问题，也会立刻长出第二套"AI 自己发明的动作"。
- **说得出为什么没响**：`--explain` 逐条列出当前判定。一条注册了却从不触发的规则，
  如果没有地方能解释原因，它跟不存在没有区别（silent-failure-lessons 第五条）。
- **规则本身有寿命**：`--expires-hours` 到期自动清理，`once` 触发后即删。
  没有寿命的规则表会越攒越多，最后没人知道哪条还该留着。
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

import situation_signals

CONTRACT = "situation-triggers/1"
TRIGGERS_FILE_NAME = "situation-triggers.json"

#: 规则表上限。到顶就拒绝新建并**说清楚** —— 静默丢弃会让 AI 以为绑好了。
MAX_TRIGGERS = 40
#: 一条规则最多几个条件。再多说明它想表达的不是"情境"而是一段程序。
MAX_CONDITIONS = 6
MAX_NAME = 40
MAX_TEXT = 400
#: 复发档。once=响一次就删；daily=每天最多一次；always=每个上升沿都响。
RECUR_MODES = ("once", "daily", "always")
#: 触发出来的通知默认多久过期。触发的东西都是时效性的，过了就该消失。
DEFAULT_END_HOURS = 12.0


class TriggerError(RuntimeError):
    """规则非法。⚠ 一律抛出去让调用方看见，绝不静默改成一个"合理默认"。"""


def default_root() -> Path:
    return situation_signals.default_root()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _day_key(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000.0))


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _lock(root: Path):
    """复用通知表那把跨进程锁的实现 —— 两把不同的锁实现迟早行为不一致。

    ReaderPC 每轮 evaluate 与 AI 侧 CLI 的 --add 会并发读-改-写这张表，
    没有锁就是丢失更新（通知表 2026-08-26 已经实锤栽过一次）。
    """
    import replication_notifications
    return replication_notifications._FileLock(  # noqa: SLF001
        root / (TRIGGERS_FILE_NAME + ".lock"))


def _empty_table() -> dict[str, Any]:
    return {"contract": CONTRACT, "triggers": [], "lastEvaluatedAtUtcMs": None}


def load(root: Path | None = None) -> dict[str, Any]:
    """读规则表。读不出来返回空表 —— 但**不写回**，别让一次读失败清空真数据。"""
    path = (root or default_root()) / TRIGGERS_FILE_NAME
    try:
        value = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError):
        return _empty_table()
    if not isinstance(value, dict) or not isinstance(value.get("triggers"), list):
        return _empty_table()
    value.setdefault("contract", CONTRACT)
    value.setdefault("lastEvaluatedAtUtcMs", None)
    return value


def _save(root: Path, table: dict[str, Any]) -> None:
    _atomic_write_json(root / TRIGGERS_FILE_NAME, table)


def validate_when(when: Any) -> dict[str, Any]:
    """条件的封闭校验。非法就抛 —— 词汇表外的名字**必须**报错。

    静默忽略一个拼错的信号名，等于造出一条永远不成立的规则，
    而 AI 那边会以为绑好了。这正是"失败必须出声"的教科书场景。
    """
    if not isinstance(when, dict) or not when:
        raise TriggerError("when 必须是非空对象，如 {\"place\":\"home\"}")
    if len(when) > MAX_CONDITIONS:
        raise TriggerError(
            "一条规则最多 %d 个条件，收到 %d 个" % (MAX_CONDITIONS, len(when)))
    for name, expected in when.items():
        if name not in situation_signals.SIGNALS:
            raise TriggerError(
                "没有信号 %r。可用的：%s"
                % (name, "、".join(situation_signals.SIGNALS)))
        if isinstance(expected, dict):
            if len(expected) != 1:
                raise TriggerError("%s 的条件对象只能有一个比较器" % name)
            operator = next(iter(expected))
            if operator not in ("not", "in", "gte", "lte"):
                raise TriggerError(
                    "%s 用了不认识的比较器 %r（只有 not/in/gte/lte）"
                    % (name, operator))
        elif not isinstance(expected, (str, int, float, bool)):
            raise TriggerError("%s 的期望值只能是标量或比较器对象" % name)
    return dict(when)


def validate_then(then: Any) -> dict[str, Any]:
    import replication_notifications as rn
    if not isinstance(then, dict):
        raise TriggerError("then 必须是对象")
    # 动作（2026-09-08 用户：「触发某个或者复合条件后停下或者开始某些功能」）。
    # 能写成固定代码的就别绕经 AI —— 那是这一整套设计的出发点。
    action = str(then.get("action") or "").strip()
    action_params = then.get("actionParams") or {}
    if action:
        if not isinstance(action_params, dict):
            raise TriggerError("then.actionParams 必须是对象")
        import situation_actions
        try:
            # ⚠ **注册时就校验**，不等触发那一刻：动作名写错却存下来，
            # 表现是"规则响了、功能没动"，而没有一处会喊。
            situation_actions.validate(action, action_params)
        except situation_actions.ActionError as error:
            raise TriggerError(str(error)) from None
    title = str(then.get("title") or "").strip()
    if not title and not action:
        # 有动作时标题可省：机器自己做完的事不一定值得占用户一条通知。
        raise TriggerError("then 至少要有 title 或 action 之一")
    deliver = str(then.get("deliver") or "auto")
    if deliver not in rn._DELIVER_MODES:  # noqa: SLF001
        raise TriggerError(
            "then.deliver 只能是 %s 之一，收到 %r"
            % ("/".join(rn._DELIVER_MODES), deliver))  # noqa: SLF001
    audience = str(then.get("audience") or "user")
    if audience not in ("user", "ai"):
        raise TriggerError("then.audience 只能是 user 或 ai")
    # 通知**必须**带终止条件（NotificationStore 会拒绝没有 end 的条目：
    # 没有寿命的待办会永远挂着）。触发出来的通知天生是时效性的 ——
    # 「有 10 张新卡可以做了」明天就不成立了 —— 所以一律按小时过期。
    hours = then.get("endHours", DEFAULT_END_HOURS)
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise TriggerError("then.endHours 必须是数字（小时）") from None
    if not 0.5 <= hours <= 168:
        raise TriggerError("then.endHours 只接受 0.5~168 小时，收到 %r" % hours)
    return {
        "title": title[:MAX_TEXT],
        "body": str(then.get("body") or "")[:MAX_TEXT],
        "aiAction": str(then.get("aiAction") or "")[:MAX_TEXT],
        "deliver": deliver,
        "audience": audience,
        "endHours": hours,
        "action": action,
        "actionParams": dict(action_params) if action else {},
    }


def evaluate_when(
    when: dict[str, Any], payload: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    """(整条成立吗, 逐条判定明细)。明细是 --explain 的全部内容来源。"""
    details: list[dict[str, Any]] = []
    everything = True
    for name, expected in when.items():
        signal = payload["signals"].get(name) or situation_signals.unknown(
            "这一轮没读这个信号")
        ok, why = situation_signals.matches(signal, expected)
        details.append({
            "signal": name,
            "expected": expected,
            "actual": signal.get("value") if signal.get("known") else None,
            "known": bool(signal.get("known")),
            "ok": ok,
            "why": why,
        })
        if not ok:
            everything = False
    return everything, details


def add(
    root: Path | None = None,
    *,
    name: str,
    when: dict[str, Any],
    then: dict[str, Any],
    recur: str = "daily",
    expires_hours: float | None = None,
    created_by: str = "ai",
    runtime: Path | None = None,
) -> dict[str, Any]:
    """注册一条规则。**注册时求值一次当基线，成立也不触发**（见模块头纪律）。"""
    root = root or default_root()
    label = str(name or "").strip()
    if not label or len(label) > MAX_NAME:
        raise TriggerError("name 必需，且不超过 %d 字" % MAX_NAME)
    if recur not in RECUR_MODES:
        raise TriggerError(
            "recur 只能是 %s 之一，收到 %r" % ("/".join(RECUR_MODES), recur))
    checked_when = validate_when(when)
    checked_then = validate_then(then)
    now = _now_ms()
    payload = situation_signals.read_all(root, runtime)
    match, details = evaluate_when(checked_when, payload)
    record = {
        "id": "trg_" + uuid.uuid4().hex[:10],
        "name": label,
        "when": checked_when,
        "then": checked_then,
        "recur": recur,
        "createdAtUtcMs": now,
        "createdBy": created_by,
        "expiresAtUtcMs": (
            None if expires_hours is None
            else now + int(float(expires_hours) * 3_600_000)),
        # 基线：注册那一刻的判定。true 表示"已经成立"，所以要等它先变假再变真。
        "lastMatch": match,
        "lastFiredAtUtcMs": None,
        "lastFiredDay": None,
        "fireCount": 0,
    }
    with _lock(root):
        table = load(root)
        for one in table["triggers"]:
            if one.get("name") == label:
                raise TriggerError("已经有一条叫 %r 的规则了（先 --remove）" % label)
        if len(table["triggers"]) >= MAX_TRIGGERS:
            raise TriggerError(
                "规则表已满（%d 条上限）。先删掉不用的。" % MAX_TRIGGERS)
        table["triggers"].append(record)
        _save(root, table)
    # 把基线明细一起返回：AI 立刻能看出"我注册的条件现在是不是已经成立"。
    return {"trigger": record, "baselineMatch": match, "details": details}


def remove(root: Path | None = None, *, key: str) -> dict[str, Any] | None:
    """按 id 或 name 删。删不到返回 None —— 调用方负责说"没有这条"。"""
    root = root or default_root()
    with _lock(root):
        table = load(root)
        for index, one in enumerate(table["triggers"]):
            if one.get("id") == key or one.get("name") == key:
                removed = table["triggers"].pop(index)
                _save(root, table)
                return removed
    return None


def _fire(root: Path, trigger: dict[str, Any], now: int) -> str | None:
    """触发 = 建一条通知。返回通知 id；建不出来返回 None（原因写进 lastError）。

    ⚠ 出口只有这一个。aiAction 折进 body 而**不新开字段**：通知的字段表在
    导出/渲染/板子/侧栏各有副本，加一个字段要同步好几处，而"只放行不搬字段"
    的表现是「校验全过就是不生效」（CLAUDE.md 记过这个形态）。
    """
    import replication_notifications as rn
    then = trigger["then"]
    body = then.get("body") or ""
    action = then.get("aiAction") or ""
    if action:
        body = (body + "\n" if body else "") + "AI 该做的：" + action
    # 机器侧动作**先做**：它是确定性的，不该等通知建得成不成。
    # 做完把结果附在正文里，人和 AI 都看得见到底动了什么。
    machine = then.get("action") or ""
    fallback_title = ""
    if machine:
        import situation_actions
        try:
            situation_actions.run(
                machine, then.get("actionParams") or {}, root)
            trigger["lastActionAtUtcMs"] = now
            trigger.pop("lastActionError", None)
            body = (body + "\n" if body else "") + "已自动执行：" + machine
        except Exception as error:  # noqa: BLE001
            # 动作失败要**出声**并且照样把通知发出去 —— 否则表现是
            # "规则响了、功能没动、也没人知道"。
            trigger["lastActionError"] = str(error)[:200]
            body = ((body + "\n" if body else "")
                    + "⚠ 自动执行 " + machine + " 失败：" + str(error)[:120])
        if not str(then.get("title") or "").strip():
            # 没标题说明这条规则只想做事、不想打扰人。
            if "lastActionError" not in trigger:
                return "silent"          # 做成了就安静收工
            # 但**失败一定要留下通知**：静默失败的动作等于没有这个动作。
            # ⚠ 这里必须自己补一个标题 —— NotificationStore 会拒掉空标题，
            # 于是"失败也要说"会变成"什么都没说"（测试 2026-09-08 当场抓到）。
            fallback_title = "自动执行「%s」失败" % machine
    store = rn.NotificationStore(root)
    # dedupe_key 按复发档取：daily 同一天只留一条，避免边沿抖动堆出重复。
    dedupe = "trigger:%s" % trigger["id"]
    if trigger["recur"] == "daily":
        dedupe += ":" + _day_key(now)
    elif trigger["recur"] == "always":
        dedupe += ":%d" % now
    hours = float(then.get("endHours") or DEFAULT_END_HOURS)
    try:
        item = store.create(
            kind="situation-trigger",
            title=then.get("title") or fallback_title,
            body=body[:MAX_TEXT],
            source="trigger:" + trigger["name"],
            audience=then.get("audience") or "user",
            deliver=then.get("deliver") or "auto",
            dedupe_key=dedupe,
            # end 是必填：没有终止条件的条目会被 NotificationStore 拒绝。
            end="expires:%d" % (now + int(hours * 3_600_000)),
        )
        return str(item.get("id") or "")
    except Exception as error:  # noqa: BLE001
        trigger["lastError"] = str(error)[:200]
        return None


def evaluate(
    root: Path | None = None, runtime: Path | None = None,
) -> dict[str, Any]:
    """跑一轮：求值全部规则，上升沿的触发。每轮对账调一次。

    ⚠ 一条规则出错不连坐其它：它自己记 lastError，其余照常判。
    """
    root = root or default_root()
    now = _now_ms()
    table = load(root)
    if not table["triggers"]:
        # 空表也要记时刻：这样"引擎到底有没有在跑"永远答得出来。
        with _lock(root):
            table = load(root)
            table["lastEvaluatedAtUtcMs"] = now
            _save(root, table)
        return {"evaluated": 0, "fired": [], "expired": 0, "atUtcMs": now}
    payload = situation_signals.read_all(root, runtime)
    fired: list[dict[str, Any]] = []
    with _lock(root):
        table = load(root)
        keep: list[dict[str, Any]] = []
        expired = 0
        for trigger in table["triggers"]:
            deadline = trigger.get("expiresAtUtcMs")
            if isinstance(deadline, (int, float)) and deadline and now >= deadline:
                expired += 1
                continue
            try:
                when = trigger.get("when") or {}
                match, _details = evaluate_when(when, payload)
            except Exception as error:  # noqa: BLE001
                trigger["lastError"] = "求值失败：" + str(error)[:180]
                keep.append(trigger)
                continue
            rising = match and not trigger.get("lastMatch")
            trigger["lastMatch"] = match
            if not rising:
                keep.append(trigger)
                continue
            recur = trigger.get("recur") or "daily"
            if recur == "daily" and trigger.get("lastFiredDay") == _day_key(now):
                # 今天已经响过。lastMatch 已更新，明天同一个上升沿还能响。
                keep.append(trigger)
                continue
            outcome = _fire(root, trigger, now)
            # 三种结果要分清：通知 id = 建成了；"silent" = 动作做完了、
            # 这条规则本来就不想打扰人；空 = 真失败。
            # 把 "silent" 混进失败会让纯动作规则每轮重跑一次动作。
            notification_id = outcome if outcome and outcome != "silent" else ""
            if not outcome:
                # 建通知失败**不能吃掉这个上升沿**：把 lastMatch 退回假，
                # 下一轮条件还成立就再试一次。原因已记在 lastError 里，
                # --list 看得见 —— 宁可每轮重试并一直喊，也不要"响过一次
                # 但其实什么都没发生"（这种失败最贵：它看起来像成功）。
                trigger["lastMatch"] = False
            if outcome:
                trigger["lastFiredAtUtcMs"] = now
                trigger["lastFiredDay"] = _day_key(now)
                trigger["fireCount"] = int(trigger.get("fireCount") or 0) + 1
                trigger.pop("lastError", None)
                fired.append({
                    "id": trigger["id"], "name": trigger["name"],
                    "notificationId": notification_id,
                    "action": trigger["then"].get("action") or "",
                })
                if recur == "once":
                    continue  # 响过就删：一次性的规则留着只会让人猜它还灵不灵
            keep.append(trigger)
        table["triggers"] = keep
        table["lastEvaluatedAtUtcMs"] = now
        _save(root, table)
    return {
        "evaluated": len(table["triggers"]), "fired": fired,
        "expired": expired, "atUtcMs": now,
    }


def explain(
    root: Path | None = None, *, key: str, runtime: Path | None = None,
) -> dict[str, Any] | None:
    """一条规则**现在**为什么成立/不成立。逐条给出期望、实际、判定。"""
    root = root or default_root()
    table = load(root)
    trigger = next(
        (one for one in table["triggers"]
         if one.get("id") == key or one.get("name") == key), None)
    if trigger is None:
        return None
    payload = situation_signals.read_all(root, runtime)
    match, details = evaluate_when(trigger.get("when") or {}, payload)
    return {
        "trigger": trigger, "match": match, "details": details,
        "lastEvaluatedAtUtcMs": table.get("lastEvaluatedAtUtcMs"),
    }


def render_list(table: dict[str, Any]) -> str:
    triggers = table.get("triggers") or []
    stamp = table.get("lastEvaluatedAtUtcMs")
    lines = ["情境触发规则 %d 条（上次求值：%s）" % (
        len(triggers),
        "从来没有" if not stamp else time.strftime(
            "%m-%d %H:%M", time.localtime(stamp / 1000.0)))]
    if not triggers:
        lines.append("  （空）")
    for one in triggers:
        conditions = "，".join(
            "%s=%s" % (name, json.dumps(value, ensure_ascii=False))
            for name, value in (one.get("when") or {}).items())
        lines.append("  [%s] %s（%s）" % (
            one.get("id", "?"), one.get("name", "?"), one.get("recur", "?")))
        lines.append("      当 %s" % conditions)
        lines.append("      则 %s" % (one.get("then") or {}).get("title", "?"))
        state = "已成立，等它先变假" if one.get("lastMatch") else "尚未成立"
        fired = one.get("fireCount") or 0
        lines.append("      现状：%s；已触发 %d 次%s" % (
            state, fired,
            "" if not one.get("lastError") else "；上次出错：" + one["lastError"]))
    return "\n".join(lines)


def render_explain(value: dict[str, Any]) -> str:
    trigger = value["trigger"]
    lines = ["规则「%s」现在%s" % (
        trigger.get("name"), "成立" if value["match"] else "不成立")]
    for detail in value["details"]:
        mark = "✓" if detail["ok"] else "✗"
        actual = ("不知道" if not detail["known"]
                  else json.dumps(detail["actual"], ensure_ascii=False))
        lines.append("  %s %-18s 期望 %s，实际 %s —— %s" % (
            mark, detail["signal"],
            json.dumps(detail["expected"], ensure_ascii=False),
            actual, detail["why"]))
    if value["match"] and trigger.get("lastMatch"):
        # 最容易被误认为"坏了"的状态，所以专门说一句。
        lines.append("⚠ 条件成立、但上一轮也成立 —— 上升沿已经过去了，"
                     "要等它先变不成立再变成立才会再触发。")
    return "\n".join(lines)


def _action_params(args: Any) -> dict[str, Any]:
    """把 --do-* 收成动作参数。只放**给了的**那些 —— 补一个 None 进去会让
    situation_actions 那边把"没给"和"给了空"混在一起。"""
    params: dict[str, Any] = {}
    if getattr(args, "do_target", None) is not None:
        params["target"] = args.do_target
    if getattr(args, "do_minutes", None) is not None:
        params["minutes"] = args.do_minutes
    return params


def main() -> int:
    parser = argparse.ArgumentParser(description="情境自动触发规则")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--runtime", type=Path, default=None,
                        help="桥的 runtime 目录（位置信号在那边）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--vocab", action="store_true", help="可用信号和比较器")
    parser.add_argument("--evaluate", action="store_true", help="手动跑一轮")
    parser.add_argument("--explain", metavar="ID或名字")
    parser.add_argument("--remove", metavar="ID或名字")
    parser.add_argument("--add", action="store_true")
    parser.add_argument("--name")
    parser.add_argument("--when", help="JSON 对象，如 '{\"place\":\"home\"}'")
    parser.add_argument("--title")
    parser.add_argument("--body", default="")
    parser.add_argument("--ai-action", default="", dest="ai_action",
                        help="触发后 AI 该做什么（折进通知正文）")
    parser.add_argument("--do", default="", dest="machine_action",
                        help="触发后机器直接做的事（见 situation_actions.py --list）")
    parser.add_argument("--do-target", default=None,
                        help="--do 的目标，如计划任务名")
    parser.add_argument("--do-minutes", type=float, default=None,
                        help="--do 的时长，如 background.hold 按住多久")
    parser.add_argument("--deliver", default="auto")
    parser.add_argument("--audience", default="user")
    parser.add_argument("--recur", default="daily",
                        choices=list(RECUR_MODES))
    parser.add_argument("--expires-hours", type=float, default=None)
    args = parser.parse_args()
    root = args.root or default_root()

    def out(value: Any, text: str) -> int:
        print(json.dumps(value, ensure_ascii=False, indent=2)
              if args.json else text)
        return 0

    if args.vocab:
        return out({"signals": {name: {"summary": spec["summary"],
                                       "values": spec["values"]}
                                for name, spec
                                in situation_signals.SIGNALS.items()},
                    "recur": list(RECUR_MODES)},
                   situation_signals.vocab_text()
                   + "\n复发档：" + " / ".join(RECUR_MODES))
    if args.add:
        try:
            when = json.loads(args.when or "")
        except ValueError as error:
            print("--when 不是合法 JSON：%s" % error)
            return 2
        try:
            result = add(
                root, name=args.name or "", when=when,
                then={"title": args.title or "", "body": args.body,
                      "aiAction": args.ai_action, "deliver": args.deliver,
                      "audience": args.audience,
                      "action": args.machine_action,
                      "actionParams": _action_params(args)},
                recur=args.recur, expires_hours=args.expires_hours,
                created_by="ai", runtime=args.runtime)
        except TriggerError as error:
            print("拒绝：%s" % error)
            return 2
        note = ("注册时条件**已经成立** —— 已记为基线，要等它先变不成立再变成立才触发。"
                if result["baselineMatch"] else "注册时条件尚未成立，等它成立时触发。")
        return out(result, "已注册 [%s] %s\n%s" % (
            result["trigger"]["id"], result["trigger"]["name"], note))
    if args.remove:
        removed = remove(root, key=args.remove)
        if removed is None:
            print("没有叫 %r 的规则" % args.remove)
            return 1
        return out(removed, "已删除 [%s] %s" % (removed["id"], removed["name"]))
    if args.explain:
        value = explain(root, key=args.explain, runtime=args.runtime)
        if value is None:
            print("没有叫 %r 的规则" % args.explain)
            return 1
        return out(value, render_explain(value))
    if args.evaluate:
        result = evaluate(root, args.runtime)
        text = "求值 %d 条，触发 %d 条%s" % (
            result["evaluated"], len(result["fired"]),
            "" if not result["fired"] else "：" + "、".join(
                one["name"] for one in result["fired"]))
        if result["expired"]:
            text += "；清掉 %d 条过期规则" % result["expired"]
        return out(result, text)
    table = load(root)
    return out(table, render_list(table))


if __name__ == "__main__":
    raise SystemExit(main())
