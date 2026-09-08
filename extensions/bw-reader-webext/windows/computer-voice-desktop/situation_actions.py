#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""situation_actions — 触发能**做的事**：封闭的动作表。

    python situation_actions.py --list            # 有哪些动作
    python situation_actions.py --do background.hold --minutes 60
    python situation_actions.py --do task.disable --target "JP Dict Refresh"

## 为什么存在（用户 2026-09-08 说明思路）

> 我们其实现在在做的 codex 自己也能做，但是在使用时做很不稳定且各种工具调用
> 肯定没有固定化的代码稳定快速……不只是需要及时的获取数据，还牵扯到这些数据
> 变化为某个状态时自动触发的各种行为能力，比如触发某个或者复合条件后停下或者
> 开始某些功能

`situation_triggers` 原来只有一个出口：建一条通知，也就是**只能告诉人**。
这个模块补的是另一半：**机器自己就能确定性做完的事，不该绕经 AI**。
能写成固定代码的就写成固定代码 —— 这正是用户那句话的意思。

分工因此是：
  `--do <动作>`      机器立刻执行，结果确定，有测试
  `--ai-action <话>` 要判断、要说话、要看情况的，经快慢板交给 AI

## 铁律：进表的动作必须有**已验证会生效**的路径

一个"校验全过、其实什么都没发生"的动作比没有这个动作糟得多 ——
调用方以为做了，而链路上没有一处会报错。所以每加一个动作，先回答
"它凭什么生效"，答不上来就不加。

已经被这条规矩挡在表外的例子（2026-09-08 实查）：
  **改 `readerpc-server.config.json` 的 voiceEnabled / keepPcPreprocessingOnline**
  —— 那个文件只在 ReaderPC 启动时读一次（`load_preferences`），运行中改它
  要等下次重启才算数。托盘界面的复选框是靠 `command=` 回调当场应用的，
  不是靠文件监听。所以"用触发关掉语音"目前**做不到**，别把它写进表里假装能做。

## 安全边界

`task.disable` 只认白名单里的计划任务，而且**看门狗和引导任务永远不在白名单里**：
把 `BW ReaderPC Watchdog` 关掉，等于让一次崩溃变成永久停摆，而且下一次谁都
想不到去看计划任务。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

CONTRACT = "situation-actions/1"

#: 后台任务的"静一会儿"标记。readerpc_gate 读它 —— 它在的时候后台计划任务跳过。
BACKGROUND_HOLD_FILE_NAME = "background-hold.json"
#: 一次最多按住多久。没有上限的"静音"会变成永久停摆，而且没人记得去解除。
BACKGROUND_HOLD_MAX_MINUTES = 480

#: 允许被触发开关的计划任务。**只放批量/AI 类的重活**。
#:
#: ⚠ 看门狗（BW ReaderPC Watchdog）和引导任务（BW Computer Voice Setup）
#: 永远不进这张表：关掉看门狗会让一次崩溃变成永久停摆，而排查的人不会想到
#: 去翻计划任务。同理不放系统自带任务 —— 白名单存在的意义就是这个。
TASK_WHITELIST = (
    "JP Dict Refresh",
    "KJ Anki Sync",
    "Obsidian Anki 每日状态更新",
)


class ActionError(RuntimeError):
    """动作非法或执行失败。⚠ 一律抛出去，绝不静默当成成功。"""


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# ─────────────────────────── 后台任务闸 ───────────────────────────


def background_hold_until_ms(root: Path | None = None) -> int | None:
    """后台任务被按住到什么时候。没按住或已过期返回 None。"""
    path = (root or default_root()) / BACKGROUND_HOLD_FILE_NAME
    try:
        value = json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError):
        return None
    until = value.get("untilMs") if isinstance(value, dict) else None
    if not isinstance(until, (int, float)) or until <= _now_ms():
        return None
    return int(until)


def _do_background_hold(
    root: Path, *, minutes: float | None = None, **_ignored: Any,
) -> dict[str, Any]:
    """按住后台计划任务一段时间。

    用户 2026-09-08 的痛点就是这个：「今天凌晨我在使用电脑时……多次一瞬间弹出了
    终端框，而且电脑一瞬间变得卡顿」。人在用电脑时后台不该跑半小时的 AI 调用。

    凭什么生效：`readerpc_gate.readerpc_active()` 读这个文件，而所有后台计划任务
    开头都过那道闸。**有测试**（test_situation_actions + test_readerpc_gate）。
    """
    span = 60.0 if minutes is None else float(minutes)
    if not 1.0 <= span <= BACKGROUND_HOLD_MAX_MINUTES:
        raise ActionError(
            "minutes 只接受 1~%d，收到 %r" % (BACKGROUND_HOLD_MAX_MINUTES, span))
    until = _now_ms() + int(span * 60_000)
    _atomic_write_json(root / BACKGROUND_HOLD_FILE_NAME, {
        "contract": "reader-background-hold/1",
        "untilMs": until,
        "setAtMs": _now_ms(),
        "minutes": span,
    })
    return {"heldUntilMs": until, "minutes": span}


def _do_background_resume(root: Path, **_ignored: Any) -> dict[str, Any]:
    """立刻解除按住。删文件即可 —— 文件不在就是没按住。"""
    path = root / BACKGROUND_HOLD_FILE_NAME
    existed = path.is_file()
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise ActionError("删不掉按住标记：%s" % error) from None
    return {"wasHeld": existed}


# ─────────────────────────── 计划任务开关 ───────────────────────────


def _schtasks(target: str, enable: bool) -> None:
    if target not in TASK_WHITELIST:
        raise ActionError(
            "计划任务 %r 不在白名单里。允许的：%s"
            % (target, "、".join(TASK_WHITELIST)))
    flag = "/ENABLE" if enable else "/DISABLE"
    try:
        done = subprocess.run(
            ["schtasks", "/Change", "/TN", target, flag],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ActionError("schtasks 跑不起来：%s" % error) from None
    if done.returncode != 0:
        # 原样端出 schtasks 说的话：折成"操作失败"等于把唯一的线索丢掉。
        detail = (done.stderr or done.stdout or "").strip()[:200]
        raise ActionError("schtasks 返回 %d：%s" % (done.returncode, detail))


def _do_task_disable(
    root: Path, *, target: str | None = None, **_ignored: Any,
) -> dict[str, Any]:
    """关掉一个批量/AI 计划任务。凭什么生效：schtasks 当场改注册状态。"""
    if not target:
        raise ActionError("task.disable 要 target（计划任务名）")
    _schtasks(target, enable=False)
    return {"task": target, "state": "disabled"}


def _do_task_enable(
    root: Path, *, target: str | None = None, **_ignored: Any,
) -> dict[str, Any]:
    if not target:
        raise ActionError("task.enable 要 target（计划任务名）")
    _schtasks(target, enable=True)
    return {"task": target, "state": "enabled"}


#: **封闭动作表**。键 = `--do` 能写的名字。
#:
#: 每条都要填 `why`：它凭什么生效。这一栏不是注释，是准入条件 ——
#: 填不出来的动作不该存在（见模块头「铁律」）。
ACTIONS: dict[str, dict[str, Any]] = {
    "background.hold": {
        "summary": "按住后台计划任务一段时间（人在用电脑时别抢资源）",
        "params": "minutes（1~480，默认 60）",
        "why": "readerpc_gate 读 background-hold.json，所有后台任务开头都过那道闸",
        "run": _do_background_hold,
    },
    "background.resume": {
        "summary": "立刻解除按住",
        "params": "无",
        "why": "同上，删掉标记文件即恢复",
        "run": _do_background_resume,
    },
    "task.disable": {
        "summary": "关掉一个批量/AI 计划任务",
        "params": "target（必填，且必须在白名单里）",
        "why": "schtasks /Change /DISABLE 当场改注册状态",
        "run": _do_task_disable,
    },
    "task.enable": {
        "summary": "开回一个批量/AI 计划任务",
        "params": "target（必填，且必须在白名单里）",
        "why": "schtasks /Change /ENABLE 当场改注册状态",
        "run": _do_task_enable,
    },
}


def validate(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """注册时先校验，别等到触发那一刻才发现动作名写错。

    ⚠ 表外的名字**必须报错**：静默存下一条动作永远不生效的规则，
    等于让调用方以为绑好了。
    """
    spec = ACTIONS.get(name)
    if spec is None:
        raise ActionError(
            "没有动作 %r。可用的：%s" % (name, "、".join(ACTIONS)))
    values = dict(params or {})
    if name in ("task.disable", "task.enable"):
        target = str(values.get("target") or "")
        if target not in TASK_WHITELIST:
            raise ActionError(
                "%s 的 target 必须在白名单里：%s（收到 %r）"
                % (name, "、".join(TASK_WHITELIST), target))
    if name == "background.hold" and values.get("minutes") is not None:
        try:
            span = float(values["minutes"])
        except (TypeError, ValueError):
            raise ActionError("minutes 必须是数字") from None
        if not 1.0 <= span <= BACKGROUND_HOLD_MAX_MINUTES:
            raise ActionError(
                "minutes 只接受 1~%d" % BACKGROUND_HOLD_MAX_MINUTES)
        values["minutes"] = span
    return {"action": name, "params": values}


def run(
    name: str,
    params: dict[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """执行一个动作。非法或失败都抛 ActionError。"""
    checked = validate(name, params)
    spec = ACTIONS[name]
    result = spec["run"](root or default_root(), **checked["params"])
    return {"action": name, "ok": True, "result": result}


def vocab_text() -> str:
    """给 AI 看的动作表。"""
    lines = ["触发能做的动作（--do 只能写这些）："]
    for name, spec in ACTIONS.items():
        lines.append("  %-20s %s" % (name, spec["summary"]))
        lines.append("  %-20s   参数：%s" % ("", spec["params"]))
    lines.append("")
    lines.append("计划任务白名单：" + "、".join(TASK_WHITELIST))
    lines.append("⚠ 要判断、要说话、要看情况的事用 --ai-action，不是 --do。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="情境动作")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--do", metavar="动作名")
    parser.add_argument("--target", default=None, help="计划任务名")
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--status", action="store_true",
                        help="后台任务现在被按住了吗")
    args = parser.parse_args()
    root = args.root or default_root()

    if args.status:
        until = background_hold_until_ms(root)
        text = ("后台任务没被按住" if until is None
                else "后台任务按住到 " + time.strftime(
                    "%H:%M", time.localtime(until / 1000.0)))
        print(json.dumps({"heldUntilMs": until}, ensure_ascii=False)
              if args.json else text)
        return 0
    if args.list or not args.do:
        if args.json:
            print(json.dumps({
                "contract": CONTRACT,
                "actions": {name: {"summary": spec["summary"],
                                   "params": spec["params"],
                                   "why": spec["why"]}
                            for name, spec in ACTIONS.items()},
                "taskWhitelist": list(TASK_WHITELIST),
            }, ensure_ascii=False, indent=2))
        else:
            print(vocab_text())
        return 0
    params: dict[str, Any] = {}
    if args.target is not None:
        params["target"] = args.target
    if args.minutes is not None:
        params["minutes"] = args.minutes
    try:
        result = run(args.do, params, root)
    except ActionError as error:
        print("拒绝：%s" % error)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2)
          if args.json else "已执行 %s：%s" % (
              args.do, json.dumps(result["result"], ensure_ascii=False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
