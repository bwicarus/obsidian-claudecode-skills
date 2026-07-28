#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 侧桥接客户端:外部助手 → Pi 阅读器的**唯一**出口(方向 B 的发起端)。

放在 Windows 上跑(Python 3.9+,零第三方依赖)。它只做三件事:
  1. 把高层内容包成 envelope —— 调用方不拼路径、不编 parts、不管书页(Pi 侧自动取当前书页)
  2. 复用**已认证的 SSH 连接**(OpenSSH ControlMaster):第一次握手后连接留在后台,
     后续调用直接搭车,省掉每次 1s 左右的 TCP+握手
  3. 空闲 60 秒自动关闭连接;异常时**只重连一次**,再失败就明确报错(不无限重试)

为什么要 ControlMaster:每轮语音回复都新建一条 SSH = 每次都付握手成本,
用户会听到明显停顿;而长期保持一条不关的连接又会在网络切换后变成僵尸。
`ControlPersist=60` 正好是"热的时候复用、凉了自己收"。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

PI_HOST = os.environ.get("BW_PI_HOST", "bwicarus@bwicarus.taile44d0c.ts.net")
REMOTE = os.environ.get("BW_BRIDGE_REMOTE", "/home/bwicarus/claude/scripts/reader_bridge.py")
IDLE_S = int(os.environ.get("BW_BRIDGE_IDLE_S", "60"))    # 空闲多久自动断(ControlPersist)
CONNECT_TIMEOUT_S = 10
CALL_TIMEOUT_S = 45

_CTL_DIR = Path(os.environ.get("TEMP", "/tmp")) / "bw-bridge-ssh"


class BridgeError(RuntimeError):
    """连接/执行失败。消息里必须能看出是哪一层出的问题。"""


def _ssh_base() -> list[str]:
    _CTL_DIR.mkdir(parents=True, exist_ok=True)
    # %C = 主机/端口/用户的哈希:一个目标一条复用连接,不会互相串
    ctl = _CTL_DIR / "cm-%C"
    return [
        "ssh",
        "-o", "BatchMode=yes",                      # 不弹交互式密码框(没配好密钥就直接失败)
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={ctl}",
        "-o", f"ControlPersist={IDLE_S}",           # 空闲 60s 后主控自动退出
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        PI_HOST,
    ]


def _drop_master() -> None:
    """把可能已经失效的复用连接踢掉,让下一次重新握手(重连前必做)。"""
    try:
        subprocess.run(_ssh_base()[:-1] + ["-O", "exit", PI_HOST],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def _run_once(env: dict) -> dict:
    payload = json.dumps(env, ensure_ascii=False)
    proc = subprocess.run(
        _ssh_base() + ["python3", REMOTE, "--stdin"],
        input=payload, text=True, capture_output=True, timeout=CALL_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise BridgeError(f"SSH 执行失败(exit {proc.returncode}):"
                          f"{(proc.stderr or proc.stdout or '').strip()[:400]}")
    out = (proc.stdout or "").strip()
    try:
        return json.loads(out)
    except Exception:
        raise BridgeError(f"远端返回的不是 JSON:{out[:400]}") from None


def call(kind: str, payload: dict, *, request_id: str | None = None,
         file: str | None = None, page: int | None = None) -> dict:
    """发一条 envelope。失败时**重连一次**,仍失败则明确报错(不静默吞、不无限重试)。"""
    env = {"version": 1, "kind": kind,
           "request_id": request_id or f"win-{int(time.time())}-{uuid.uuid4().hex[:8]}",
           "payload": payload}
    if file:
        env["file"] = file
    if page is not None:
        env["page"] = page
    try:
        return _run_once(env)
    except Exception as first:
        _drop_master()                     # 复用连接可能已死(睡眠/换网)→ 丢掉重来
        try:
            r = _run_once(env)
            r["_reconnected"] = True       # 让上层知道这轮走了重连(便于观察网络质量)
            return r
        except Exception as second:
            raise BridgeError(
                "桥接不可用(已重连一次仍失败)。\n"
                f"  首次:{first}\n  重连:{second}\n"
                f"  目标:{PI_HOST} · 远端脚本:{REMOTE}\n"
                "  排查:① Tailscale 是否在线 ② 免密密钥是否仍有效 ③ Pi 上 webapp 是否 active"
            ) from second


def say(text: str, cards: list | None = None, flashcards: list | None = None,
        result: dict | None = None, user: str | None = None) -> dict:
    """最常用的一条:把这一轮的回复(可带工具卡)写进侧栏。书/页/编号全自动。"""
    p: dict = {"text": text}
    if cards:
        p["cards"] = cards
    if flashcards:
        p["flashcards"] = flashcards
    if result:
        p["result"] = result
    if user:
        p["user_utterance"] = user
    return call("assistant_turn", p)


def main() -> int:
    ap = argparse.ArgumentParser(description="Windows→Pi 阅读器桥接客户端")
    ap.add_argument("--kind", default="assistant_turn")
    ap.add_argument("--text", help="assistant_turn 的回复正文")
    ap.add_argument("--json", help="完整 payload(JSON);给了它就忽略 --text")
    ap.add_argument("--ping", action="store_true", help="只测连通性")
    a = ap.parse_args()
    try:
        if a.ping:
            r = call("ping", {})
        elif a.json:
            r = call(a.kind, json.loads(a.json))
        elif a.text:
            r = say(a.text)
        else:
            ap.error("要么 --text,要么 --json,要么 --ping")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    except BridgeError as e:
        print(f"[bridge] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
