"""voice_start_step — 语音入口的一次尝试（2026-09-09 用户拍板）。

外部程序按状态回报要求跑这个脚本时，它做**一次**尝试，然后如实回报结果。
成没成由调用方决定要不要再跑一次。

**为什么是一次性，而不是设一个"保持开着"的意图。**
保活是**持续**语义：设上之后语音一旦结束，收敛循环几秒内又会把它拉回来 ——
那会跟语音智能关闭互相打架，表现是"关了又开、来回抖"，最难查的那一种。
入口要的是"试一次，成了就成了，不成让调用方决定再试还是放弃"。

**守卫不在这里，在桥那边。**
已经在通话就不动作（那个快捷键是**切换**，按下去会挂断）；台账读不到也不动作
（那是"不知道"，不是"没在通话"）。守卫长在执行动作的那一侧，这样即使被连着
跑两次，第二次也不会做出相反的事。

**等待时长是暂定的，靠记录来调**（用户 2026-09-09：「一开始应该把时间搞得
长一点，一会查看他的记录就知道时间改多少合适了」）。所以每次尝试都往
``voice-start-attempts.jsonl`` 写一条带**实际耗时**的记录 —— 攒够之后照分布
把 ATTEMPT_TIMEOUT_SECONDS 收到该收的地方。宁可一开始等久些：等久了只是慢，
等短了会把本来会成功的那次判成失败，然后去按第二下 —— 而那一下可能正好
把刚起来的通话关掉。

退出码：0 = 已进入语音（或本来就在）；1 = 这次没成（可以再试一次）。
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import voice_autoclose

#: 一次尝试的等待上限。桥那边观察窗 10 秒、沉降 3 秒，这里**刻意放宽**，
#: 等真实分布出来再收。见模块头。
ATTEMPT_TIMEOUT_SECONDS = 60.0

ATTEMPTS_FILE_NAME = "voice-start-attempts.jsonl"
#: 记录保留条数。这是给调参用的样本，不是历史档案。
MAX_ATTEMPTS_KEPT = 400


def attempts_path(runtime: Path | None = None) -> Path:
    if runtime is not None:
        return runtime / ATTEMPTS_FILE_NAME
    root = os.environ.get("BW_BRIDGE_RUNTIME")
    base = (
        Path(root)
        if root
        else Path.home() / "bw-computer-voice-bridge" / "runtime"
    )
    return base / ATTEMPTS_FILE_NAME


def record_attempt(entry: dict[str, object],
                   runtime: Path | None = None) -> Path | None:
    """把这次尝试记下来。**记录失败不影响主流程** —— 但也不静默：
    调用方拿得到 None，据此知道这次没留下样本。"""
    path = attempts_path(runtime)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_ATTEMPTS_KEPT:
            path.write_text(
                "\n".join(lines[-MAX_ATTEMPTS_KEPT:]) + "\n",
                encoding="utf-8")
    except OSError:
        return None
    return path


def start_once(
    endpoint: str | None = None,
    timeout: float = ATTEMPT_TIMEOUT_SECONDS,
    runtime: Path | None = None,
    clock=None,
) -> dict[str, object]:
    """请桥按一次并等确认。返回桥的原样回答，外加这次的实际耗时。"""
    now = clock or time.monotonic
    url = endpoint or voice_autoclose.ENDPOINT
    data = json.dumps({"startVoiceOnce": True}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    started = now()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read() or b"{}")
        except ValueError:
            result = {"ok": False, "reason": "http",
                      "detail": "回应不是 JSON"}
    except OSError as error:
        result = {"ok": False, "reason": "unreachable",
                  "detail": "连不上桥：%s" % str(error)[:160]}
    elapsed = round(now() - started, 2)
    result["elapsedSeconds"] = elapsed
    # 样本：之后照这些数字把超时收到该收的地方。
    record_attempt({
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsedSeconds": elapsed,
        "timeoutSeconds": timeout,
        "pressed": result.get("pressed"),
        "confirmed": result.get("confirmed"),
        "reason": result.get("reason"),
    }, runtime)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="语音入口的一次尝试（已在通话中则不做任何动作）")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--timeout", type=float,
                        default=ATTEMPT_TIMEOUT_SECONDS)
    parser.add_argument("--runtime", type=Path, default=None)
    args = parser.parse_args(argv)
    result = start_once(args.endpoint, args.timeout, args.runtime)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("confirmed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
