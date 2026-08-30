#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判断依据一把抓：AI 决定「现在该不该说、怎么说」时跑的**唯一**一条命令。

    python judgment_basis.py          # 给人/AI 读的紧凑文本
    python judgment_basis.py --json   # 机器可读

## 为什么存在（用户 2026-08-30 拍板）

> 这些信息不应该是 AI 主动去查，而应该是被自动整合到一个地方，AI 只
> 需要调用一个获取判断依据的工具就可以得到所有这些数据，不然就可能会
> 变成很多轮甚至自己发明轮子。

判断要用的依据散在五六个文件里（地点、阅读焦点、语音链路、通话、
复习积压、待办…）。让 AI 逐个去翻，轻则多花好几轮，重则它临场发明
一条自己的取数路径 —— 而临场发明的路径没有任何人测过。
这里把全部依据聚成一份，**每一项自带新鲜度**。

## 纪律（每一条都有来历）

- **只读、零聚合以外的推断**：本工具不替 AI 下结论（"他在忙"是判断，
  不是数据）。它只把事实和各自的年龄摆出来。
- **「不知道」和「否」分开说**：文件缺失 = 不知道，不是"没有/不在"。
  把两者混起来，会让"没有数据"悄悄变成一个方向的结论。
- **摄像头只列清单不拍照**：拍照是「按一下才拍一张」（用户 2026-08-27
  拍板，摄像头不进快照）。这里告诉你有哪几台、想看用什么工具 ——
  自动拍等于每次判断都拍一张家里的照片。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def bridge_runtime() -> Path:
    """桥的 runtime 目录。⚠ 跟 BWReader 根**不是同一个目录**（voip_push
    在这上面栽过两次），地点/通话的文件在这边。"""
    return (Path(os.environ.get("USERPROFILE") or Path.home())
            / "bw-computer-voice-bridge" / "runtime")


def _load(path: Path) -> dict[str, Any] | None:
    """读一个 JSON；读不出=None。调用方负责把 None 说成「不知道」——
    **绝不**默默补一个默认值，那会把"没有数据"变成一个方向的结论。"""
    try:
        value = json.loads(path.read_text("utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _age_min(ms: Any, now_ms: int) -> int | None:
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return max(0, (now_ms - ms) // 60_000)


def collect(root: Path, runtime: Path) -> dict[str, Any]:
    """全部判断依据，机器可读。字段名就是含义，值里带各自的年龄。"""
    now_ms = int(time.time() * 1000)
    basis: dict[str, Any] = {"contract": "judgment-basis/1", "atUtcMs": now_ms}

    # ── 地点（在家/在公司 → 该不该现在开口的第一个问题）
    place = _load(runtime / "current-place.json")
    if place is None:
        # 文件不存在 = 从来没有过定位记录（2026-08-30 起的语义），
        # **不是**"他不在家"。
        basis["place"] = {"known": False}
    else:
        basis["place"] = {
            "known": True,
            "state": place.get("state"),
            "alias": place.get("alias"),
            "ageMinutes": _age_min(place.get("observedAtUtcMs"), now_ms),
        }

    # ── ReaderPC 状态文件（阅读焦点 + 语音链路，一个文件两份依据）
    status = _load(root / "readerpc-server.status.json")
    heartbeat_age = (
        _age_min(status.get("updatedAtEpochMs"), now_ms)
        if status else None)
    # ⚠ 心跳先说：状态文件本身停更时，里面的每个字段都是过去时 ——
    # 不说这一句，下面的"语音已连"就可能是几小时前的旧话冒充现状。
    basis["readerpcHeartbeatAgeMinutes"] = heartbeat_age
    voice = (status or {}).get("voice") or {}
    basis["voice"] = {
        "known": bool(status),
        "readerConnected": voice.get("readerConnected"),
        "captureActive": voice.get("captureActive"),
        "codexVoiceActive": voice.get("codexVoiceActive"),
    }
    context = (status or {}).get("readerContext") or {}
    basis["reading"] = {
        # ⚠ 判据是**标题非空**，不是 available：实测 reader 断开后
        # available 还是 true 而 kind/title 是空串 —— 按 available 判会
        # 渲出「阅读：?「?」」这种既不是有也不是没有的话。
        "known": bool(str(context.get("title") or "").strip()),
        "kind": context.get("kind"),
        "title": context.get("title"),
        "ageMinutes": _age_min(context.get("updatedAtEpochMs"), now_ms),
    }

    # ── 最近一通电话（刚打过没人接的话，短时间内别再吵）
    calls = ((_load(runtime / "voip-calls.json") or {}).get("calls")
             or {})
    latest = None
    for ntf_id, record in calls.items():
        if not isinstance(record, dict):
            continue
        at = record.get("lastAtUtcMs") or 0
        if latest is None or at > latest[1]:
            latest = (ntf_id, at, record.get("outcome"))
    basis["lastCall"] = (
        {"known": False} if latest is None else {
            "known": True,
            "ntfId": latest[0],
            "outcome": latest[2],
            "ageMinutes": _age_min(latest[1], now_ms),
        })

    # ── 复习积压（数字，档位化的那行在慢板上；这里给原始值）
    apply_status = _load(root / "replication-apply.status.json")
    review = (((apply_status or {}).get("notifications") or {})
              .get("reviewDue") or {})
    basis["reviewDue"] = {
        "known": bool(apply_status),
        "due": review.get("due"),
        "new": review.get("new"),
        "ageMinutes": (
            _age_min((apply_status or {}).get("atUtcMs"), now_ms)),
    }

    # ── 待办概况（audience=user；逐条内容在慢板/CLI，这里只给规模）
    items = ((_load(root / "notifications.json") or {}).get("items")
             or [])
    user_items = [
        one for one in items
        if isinstance(one, dict) and one.get("audience") == "user"
        and one.get("state") in ("pending", "acknowledged")
    ]
    basis["todos"] = {
        "pending": sum(
            1 for one in user_items if one.get("state") == "pending"),
        "acknowledged": sum(
            1 for one in user_items
            if one.get("state") == "acknowledged"),
    }

    # ── 摄像头：只列有哪些。**这里不拍、永远不自动拍**。
    sources = ((_load(root / "camera-sources.json") or {}).get("sources")
               or [])
    basis["cameras"] = [
        {"id": one.get("id"), "label": one.get("label")}
        for one in sources if isinstance(one, dict)
    ]
    return basis


def render(basis: dict[str, Any]) -> str:
    """给人/AI 读的紧凑版。规矩：一行一个依据，「不知道」直说。"""
    lines: list[str] = []

    place = basis["place"]
    if not place["known"]:
        lines.append("地点：不知道（从来没有过定位记录 —— 不是「不在家」）")
    else:
        name = place.get("alias") or {
            "home": "家", "work": "工作地点"}.get(
            place.get("state") or "", "别处（没命名过）")
        age = place.get("ageMinutes")
        stale = ("" if age is None or age <= 30
                 else f"（{age} 分钟前的旧记录，不是刚测的）")
        lines.append(f"地点：{name}{stale}")

    heartbeat = basis.get("readerpcHeartbeatAgeMinutes")
    if heartbeat is None:
        lines.append("ReaderPC 状态：读不到 —— 下面语音/阅读两行都无从谈起")
    elif heartbeat > 3:
        lines.append(
            f"⚠ ReaderPC 状态文件已 {heartbeat} 分钟没更新 —— "
            "下面语音/阅读两行是那时的旧话，别当现状")

    voice = basis["voice"]
    if not voice["known"]:
        lines.append("语音：不知道")
    elif voice.get("readerConnected") and voice.get("captureActive"):
        lines.append("语音：链路已连（现在开口他能听到）")
    else:
        lines.append("语音：未连接（现在开口他听不到；要出声得等他连上，"
                     "或走电话）")

    reading = basis["reading"]
    if not reading["known"]:
        lines.append("阅读：没有活跃的书/页面")
    else:
        age = reading.get("ageMinutes")
        lines.append(
            f"阅读：{reading.get('kind') or '?'}「{reading.get('title') or '?'}」"
            + (f"，{age} 分钟前有动静" if age is not None else ""))

    call = basis["lastCall"]
    if call["known"]:
        lines.append(
            f"最近一通电话：{call.get('outcome')}，"
            f"{call.get('ageMinutes')} 分钟前（{call.get('ntfId')}）")

    review = basis["reviewDue"]
    if review["known"] and isinstance(review.get("due"), int):
        lines.append(
            f"复习：到期 {review['due']} 张、新卡 {review.get('new') or 0} 张")
    else:
        lines.append("复习：不知道（对账状态读不到）")

    todos = basis["todos"]
    lines.append(
        f"待办：pending {todos['pending']} 条、已确认未完成 "
        f"{todos['acknowledged']} 条（逐条看慢板或 "
        "replication_notifications.py list）")

    cameras = basis["cameras"]
    if cameras:
        labels = "、".join(
            f"{one.get('label') or one.get('id')}" for one in cameras)
        lines.append(
            f"摄像头：{len(cameras)} 台可拍（{labels}）—— 这里**没有**画面，"
            "要看现场用 reader_camera_snap 当场拍，拍了才有")
    else:
        lines.append("摄像头：没有登记任何一台")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="判断依据一把抓（只读，一条命令取全）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--runtime", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    basis = collect(
        Path(args.root) if args.root else default_root(),
        Path(args.runtime) if args.runtime else bridge_runtime())
    if args.json:
        print(json.dumps(basis, ensure_ascii=False, indent=1))
    else:
        print(render(basis), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
