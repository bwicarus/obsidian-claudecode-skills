#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手表 CallKit 网络豁免实验的服务端旁证（第 0 步）。

## 这个实验在验证什么

TN3135 的豁免②：「CallKit 通话进行中，低层网络解禁」（watchOS 9+）。
`URLSessionWebSocketTask` 被同一篇明确归进 low-level networking。
所以「手表直连 Windows 语音桥」成不成立，等价于**这条豁免在熄屏 / 放下手腕
之后是否存续** —— 而这一点 Apple 从未文档化，两份调研给了相反的断言且都
没有证据。只能实测。

## 为什么需要服务端旁证

手表侧拿不到实时控制台：
- 挂着 Xcode 调试器时 app 不会被挂起，那测的是调试器不是系统；
- 而不挂调试器就没有 stdout。

所以真相只能从**服务端看到的心跳流**读出来：连接哪一刻建立、最后一帧是
什么时候到的、熄屏之后还有没有帧。这一份记录比手表上任何自述都可信。

## ⚠ 判据不是「掉没掉线」

已有开发者报告：豁免生效期间 network path 本身就会周期性
withdrawn/restored。所以**一次断开不等于豁免失效**。这里记的是完整时间序列，
判断留给看数据的人 —— 与 references/evidence-quality-lessons.md 同一个道理：
采集要能分辨，别在采集阶段就把结论写死。

## 隐私

只收心跳帧（序号 + 时间戳 + 手表自报的状态字），**不收音频**。
实验完就把这个服务停掉、把 Funnel 那条 path 撤掉。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import time

import websockets

DEFAULT_PORT = 8799
LOG = pathlib.Path.home() / "watch-probe.jsonl"


def record(event: dict) -> None:
    event["atMs"] = int(time.time() * 1000)
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    # 同时打到 stdout，跟着 systemd/journal 走 —— 两个出口，
    # 哪个先被看到都行。
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def handle(connection) -> None:
    peer = getattr(connection, "remote_address", None)
    session = {"peer": str(peer), "opened": time.time()}
    record({"event": "open", "peer": str(peer)})
    frames = 0
    last = time.time()
    try:
        async for message in connection:
            frames += 1
            now = time.time()
            gap = now - last
            last = now
            payload: dict = {}
            if isinstance(message, str):
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    payload = {"raw": message[:120]}
            else:
                payload = {"bytes": len(message)}
            # 只在**间隔异常**或每 50 帧记一条 —— 全记会把日志淹掉，
            # 而间隔本身就是这个实验要看的东西。
            if gap > 2.0 or frames % 50 == 1:
                record({
                    "event": "frame",
                    "n": frames,
                    "gapSeconds": round(gap, 2),
                    "watch": payload,
                })
            # 回声，让手表侧也能确认双向通。
            await connection.send(json.dumps({"echo": frames}))
    except websockets.exceptions.ConnectionClosed as error:
        record({
            "event": "close",
            "frames": frames,
            "heldSeconds": round(time.time() - session["opened"], 1),
            "code": error.code,
            "reason": str(error.reason)[:120],
        })
    except Exception as error:  # noqa: BLE001
        record({
            "event": "error",
            "frames": frames,
            "heldSeconds": round(time.time() - session["opened"], 1),
            "detail": "%s: %s" % (type(error).__name__, error),
        })
    else:
        record({
            "event": "close",
            "frames": frames,
            "heldSeconds": round(time.time() - session["opened"], 1),
            "code": None,
            "reason": "iterator ended",
        })


async def main_async(port: int) -> None:
    record({"event": "listening", "port": port})
    async with websockets.serve(
        handle, "127.0.0.1", port,
        # 心跳每 2 秒一帧，所以 20 秒没消息就是真没了。
        ping_interval=20, ping_timeout=20, max_size=64 * 1024,
    ):
        await asyncio.Future()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--report", action="store_true",
                        help="不起服务，只读日志出一份结论")
    args = parser.parse_args()
    if args.report:
        return report()
    try:
        asyncio.run(main_async(args.port))
    except KeyboardInterrupt:
        pass
    return 0


def report() -> int:
    """把时间序列读成人能判断的形状。

    ⚠ 刻意**不下结论**：只把「连接持续了多久、中间断过几次、最长的一段
    静默有多长」摆出来。豁免存不存续要人看着这些数字判，因为「周期性抖动」
    与「豁免被收回」在单点上长得一样。
    """
    if not LOG.is_file():
        print("还没有数据（%s 不存在）" % LOG)
        return 2
    sessions: list[dict] = []
    current: dict | None = None
    worst_gap = 0.0
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "open":
            current = {"start": row["atMs"], "frames": 0, "maxGap": 0.0}
        elif row.get("event") == "frame" and current is not None:
            current["frames"] = row.get("n", current["frames"])
            gap = float(row.get("gapSeconds") or 0)
            current["maxGap"] = max(current["maxGap"], gap)
            worst_gap = max(worst_gap, gap)
        elif row.get("event") in ("close", "error") and current is not None:
            current["heldSeconds"] = row.get("heldSeconds")
            current["end"] = row["atMs"]
            sessions.append(current)
            current = None
    if current is not None:
        current["heldSeconds"] = None       # 还连着
        sessions.append(current)

    print("共 %d 次连接" % len(sessions))
    for index, one in enumerate(sessions, start=1):
        held = one.get("heldSeconds")
        print("  #%d  帧 %-5d  持续 %-8s  最长静默 %.1fs" % (
            index, one["frames"],
            ("仍连着" if held is None else "%.0fs" % held),
            one["maxGap"]))
    print()
    print("怎么读：")
    print("  · 熄屏后帧还在继续 → 豁免在熄屏后存续，方案 A 可行")
    print("  · 熄屏那一刻起再无帧 → 豁免只在亮屏时有，形态退化成抬腕才能说话")
    print("  · 中间有几秒静默但又恢复 → 是已知的周期性抖动，不算失效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
