#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex_push_register — 让 Codex 把自己登记为提示板主动推送的目标。

    python codex_push_register.py            # 只登记，不开推送
    python codex_push_register.py --enable   # 登记并打开推送
    python codex_push_register.py --off      # 关掉推送但保留绑定
    python codex_push_register.py --unregister
    python codex_push_register.py --status   # 现在绑的是谁、开没开

## 为什么这一步只能由 Codex 自己跑（用户 2026-09-09 问「自己打开是什么意思」）

推送要两样东西：命名管道地址和目标任务 id。两样都只存在于
**Codex 亲自启动的进程**的环境变量里（`CODEX_APP_TOOLS_PIPE_PATH` /
`CODEX_THREAD_ID`）。Claude 的会话里没有，独立启动的 ReaderPC 也没有 ——
2026-09-09 实测确认。所以谁都替它登记不了。

更要紧的是任务 id：它决定推给**哪一段对话**。哪一段是"当前这段"只有它自己
知道；旁人猜错了，消息就落进一段已经死掉的会话，而接口照样返回成功 ——
完全无声。

## 为什么做成固定命令而不是让它现拼 HTTP

用户 2026-09-08 定的规矩：「我们提供固定接口，然后 codex 自己使用这些接口
绑定各种功能……不这样做 codex 每次都会自己创建一个新的工具，会很混乱」。
手写一段 POST 正是那种每次都不一样的东西。

⚠ 这个脚本**必须由 Codex 启动**才有意义：它读的是自己进程的环境变量，
而那是从 Codex 继承来的。别人跑它只会得到"没有这两个变量"的明确报错。
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

#: 桥的地址。走 tailnet 与其它 reader-* 端点同一条路。
DEFAULT_ENDPOINT = "https://bwicarus-2.taile44d0c.ts.net/reader-codex-endpoint/v1"
PIPE_ENV = "CODEX_APP_TOOLS_PIPE_PATH"
THREAD_ENV = "CODEX_THREAD_ID"


def _post(endpoint: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        # 桥的 detail 说得清错在哪个字段 —— 原样端出来，别折成"失败"。
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {"detail": "（回应不是 JSON）"}
    except OSError as error:
        return 0, {"detail": "连不上桥：%s" % error}


def main() -> int:
    parser = argparse.ArgumentParser(description="登记提示板主动推送的目标任务")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--enable", action="store_true",
                        help="登记并**打开**推送（消费端停轮询之后才该用）")
    parser.add_argument("--off", action="store_true",
                        help="关掉推送但保留绑定")
    parser.add_argument("--unregister", action="store_true",
                        help="清掉绑定并关推送（值守结束时用）")
    parser.add_argument("--status", action="store_true",
                        help="只看现在绑的是谁、开没开")
    args = parser.parse_args()

    if args.unregister:
        code, reply = _post(args.endpoint, {"unregister": True})
        print("已注销绑定，推送已关" if code == 200
              else "注销失败（HTTP %s）：%s" % (code, reply.get("detail")))
        return 0 if code == 200 else 1

    pipe = (os.environ.get(PIPE_ENV) or "").strip()
    thread = (os.environ.get(THREAD_ENV) or "").strip()
    if not pipe or not thread:
        # 这不是"出错了"，是"你不是被 Codex 启动的"。说清楚，别让人以为链路坏了。
        print("拿不到 %s / %s。" % (PIPE_ENV, THREAD_ENV))
        print("这两个变量只有 Codex 亲自启动的进程才有 —— 这条命令要由 Codex 来跑，")
        print("它读的是自己进程的环境。别人跑它登记不了，也不该登记：")
        print("任务 id 决定推给哪一段对话，只有它自己知道当前是哪一段。")
        return 2

    body: dict = {"pipePath": pipe, "threadId": thread}
    if args.enable:
        body["enabled"] = True
    elif args.off:
        body["enabled"] = False
    # 都不给就**不动开关** —— 登记和"要不要推"是两件事。
    # 消费端还在轮询时顺手打开就是双发。

    code, reply = _post(args.endpoint, body)
    if code != 200:
        print("登记失败（HTTP %s）：%s" % (code, reply.get("detail")))
        return 1
    print("已登记：任务 %s" % reply.get("threadId"))
    print("推送开关：%s" % ("开" if reply.get("pushEnabled") else "关"))
    if not reply.get("pushEnabled") and not args.off:
        print("（要打开加 --enable；但先确认这边已经不再轮询板子，否则会双发）")
    note = reply.get("lastNote")
    if note:
        print("最近一次推送：%s" % note)
    if args.status:
        print("绑定到期时刻（毫秒）：%s" % reply.get("expiresAtMs"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
