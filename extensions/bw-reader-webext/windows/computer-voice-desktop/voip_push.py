#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 iPad 打一通电话，**并且等到有结果为止**。

用户 2026-08-29 定的形状：

> 这个程序应该包办包括等待和重拨的工作，也就是说在语音 AI 看来就只是在
> 等待这个进程的结果。

所以这不是"推一下就返回"的工具，是一次**阻塞的通话尝试**：

    python voip_push.py call --ntf ntf-xxxx --title "垃圾今晚要放出去"

它会一直跑到出一个终局，然后打印一行 JSON：

    {"outcome": "answered"}    接通了 —— 你现在开口说
    {"outcome": "downgraded"}  被拒接、或两次没人接 —— 别再管了，走通知

**重拨不需要告诉你**：第一次没人接 → 等 5 分钟 → 再打一次 → 还没人接才
返回 downgraded。这整段时间你就是在等这个进程。

接通之后：说完话调 `hangup` 主动挂断；也可以等用户自己挂 ——
那时提示板会显示「已挂断」，你据此收尾。

## ⚠ --ntf 是必填的

每一通电话都必须说清楚"为哪条待办打的"。有了它，接听状态才能自动
回落到那条待办上（拒接→降级、没接→重拨）。**待办以外的电话需求**用
保留号 `misc`：那种电话不做自动降级，因为没有待办可降。

## ⚠ 这条通道只能真的响铃

iOS 13 起，每个 VoIP 推送都必须让 App 立刻向 CallKit 报一通来电，否则
App 被杀、后续推送被永久拒发。所以不要拿它做"更响的通知"，
也不要用它试探设备在不在线 —— 推一次就是响一次。

## ⚠ Production 环境

密钥在开发者后台配成 Production（不可更改），TestFlight 装的 App 走的
正是这个环境，主机名固定 api.push.apple.com。配错的表现是 APNs 返回
BadDeviceToken —— 看起来像 token 坏了，其实是环境不匹配。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

APNS_HOST = "api.push.apple.com"
TOKEN_FILE_NAME = "voip-token.json"
CALL_STATE_FILE_NAME = "voip-calls.json"
HANGUP_FILE_NAME = "voip-hangup.json"

#: 待办以外的电话用这个保留号（用户 2026-08-29 要的那个"特别的号码"）。
#:
#: ⚠ 它跟真待办的区别只有一条：**不做自动降级**（没有待办可降），
#: 也不被对账循环碰。重拨逻辑照旧 —— 没人接还是值得再打一次。
#:
#: ⚠ 必须跟 replication_apply._MISC_CALL_ID 一致。两边写岔的话，
#: misc 通话会被当成"某条不存在的待办"，记录每轮被删一次，
#: 而正阻塞着等结局的这个进程会读不到，当成没人接再打一遍 ——
#: 一通凭空多出来的电话，且没有任何一处报错。
MISC_CALL_ID = "misc"

_NTF_ID_PREFIX = "ntf-"

# 一通电话响多久算"没人接"。CallKit 的响铃周期约 30-45 秒，
# 留出余量再加上回报的往返。
_RING_TIMEOUT_SECONDS = 75

# 没人接 → 隔多久重拨。只重拨一次（用户定的）。
_RETRY_AFTER_SECONDS = 5 * 60

# JWT 有效期 Apple 规定最长 1 小时；低于 20 分钟频繁重签会被限流。
_TOKEN_TTL_SECONDS = 45 * 60

_cached_jwt: tuple[str, float] | None = None


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def _bridge_runtime() -> Path:
    """桥的 runtime 目录 —— 结局与挂断信号都由桥写在这里。

    ⚠ **写它的和读它的不是同一个目录**，这个坑我踩过两次
    （voip-token.json、voip-calls.json）：路由回 200、文件也写了，
    而读的一方一直看不到，表现是"永远不降级"且没有一处报错。
    """
    return (Path(os.environ.get("USERPROFILE") or Path.home())
            / "bw-computer-voice-bridge" / "runtime")


class VoipPushError(RuntimeError):
    pass


def _load_config(root: Path) -> dict[str, Any]:
    """读 APNs 配置。**缺什么就说缺什么** —— 一句"配置无效"会让人把
    三个字段全查一遍。"""
    path = root / "voip-push-config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise VoipPushError(
            "没有 %s。需要一个 JSON，含 keyId / teamId / keyPath / bundleId"
            % path) from error
    except json.JSONDecodeError as error:
        raise VoipPushError("%s 不是合法 JSON：%s" % (path, error)) from error
    missing = [k for k in ("keyId", "teamId", "keyPath", "bundleId")
               if not value.get(k)]
    if missing:
        raise VoipPushError("%s 缺字段：%s" % (path, "、".join(missing)))
    if not Path(value["keyPath"]).is_file():
        raise VoipPushError(
            "keyPath 指向的私钥不存在：%s（.p8 只能下载一次，丢了要重建）"
            % value["keyPath"])
    return value


def _provider_jwt(config: dict[str, Any]) -> str:
    global _cached_jwt
    now = time.time()
    if _cached_jwt and now < _cached_jwt[1]:
        return _cached_jwt[0]
    try:
        import jwt  # PyJWT
    except ImportError as error:
        raise VoipPushError(
            "缺 PyJWT：pip install pyjwt cryptography") from error
    token = jwt.encode(
        {"iss": config["teamId"], "iat": int(now)},
        Path(config["keyPath"]).read_text(encoding="utf-8"),
        algorithm="ES256",
        headers={"kid": config["keyId"]},
    )
    _cached_jwt = (token, now + _TOKEN_TTL_SECONDS)
    return token


def load_device_token(root: Path) -> str | None:
    """设备上报的 VoIP token。没有 = 打不进去，**不是可以忽略的状态**。"""
    newest: tuple[float, str] | None = None
    for path in (root / TOKEN_FILE_NAME,
                 _bridge_runtime() / TOKEN_FILE_NAME):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            token = value.get("token")
            if not isinstance(token, str) or not token:
                continue
            stamp = path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        # 两处都有就取最新 —— token 会变（重装/恢复备份/系统更新），
        # 拿旧的推等于推给一个已经不存在的设备。
        if newest is None or stamp > newest[0]:
            newest = (stamp, token)
    return newest[1] if newest else None


def _push(root: Path, title: str, reason: str, ntf_id: str) -> dict[str, Any]:
    """推一通来电（不等待）。"""
    config = _load_config(root)
    device_token = load_device_token(root)
    if not device_token:
        raise VoipPushError(
            "还没有设备 token。App 启动时会上报一次；没有它就永远打不进来，"
            "而且失败是静默的")
    try:
        import httpx
    except ImportError as error:
        raise VoipPushError(
            "缺 httpx（APNs 要 HTTP/2）：pip install 'httpx[http2]'"
        ) from error
    with httpx.Client(http2=True, timeout=15.0) as client:
        response = client.post(
            "https://%s/3/device/%s" % (APNS_HOST, device_token),
            headers={
                "authorization": "bearer " + _provider_jwt(config),
                # ⚠ VoIP 的 topic **必须**带 .voip 后缀，
                # 用裸 bundleId 会被 APNs 以 TopicDisallowed 拒掉。
                "apns-topic": config["bundleId"] + ".voip",
                "apns-push-type": "voip",
                "apns-priority": "10",
                "apns-expiration": "0",
            },
            json={
                "title": title[:80],
                "reason": reason[:200],
                "notificationId": ntf_id[:64],
            },
        )
    ok = response.status_code == 200
    detail = ""
    if not ok:
        try:
            detail = response.json().get("reason", "")
        except Exception:  # noqa: BLE001
            detail = response.text[:200]
    return {"ok": ok, "status": response.status_code, "reason": detail}


def _read_outcome(ntf_id: str, after_ms: int) -> str | None:
    """读这通电话的结局。只认 after_ms 之后写下的那条 —— 否则会把
    上一通电话的旧结局当成这一通的。"""
    try:
        value = json.loads(
            (_bridge_runtime() / CALL_STATE_FILE_NAME)
            .read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    record = (value.get("calls") or {}).get(ntf_id)
    if not isinstance(record, dict):
        return None
    if int(record.get("lastAtUtcMs") or 0) < after_ms:
        return None
    outcome = record.get("outcome")
    return outcome if isinstance(outcome, str) else None


def _wait_for_outcome(ntf_id: str, after_ms: int, deadline: float) -> str:
    """等一通电话的结局。超时算 unanswered。"""
    while time.time() < deadline:
        outcome = _read_outcome(ntf_id, after_ms)
        if outcome in ("answered", "declined", "unanswered"):
            return outcome
        time.sleep(1.0)
    # ⚠ 超时**当成没人接**，不是当成失败：设备可能根本没网、App 可能
    # 没来得及回报。两者对用户的意义一样 —— 他没接到 —— 而当成失败会让
    # 上层不知道该重拨还是该降级。
    return "unanswered"


def _require_valid_ntf(ntf_id: str) -> str:
    """`--ntf` 只接受两种取值。

    ⚠ 写错一个字的后果是**静默的**：电话照样打得出去、照样响，但结局会被
    记到一条不存在的待办上 —— 于是拒接不降级、没接不重拨，而没有一处报错。
    与其让它错得无声无息，不如在这里就停下。
    """
    value = (ntf_id or "").strip()
    if value == MISC_CALL_ID:
        return value
    if value.startswith(_NTF_ID_PREFIX) and len(value) > len(_NTF_ID_PREFIX):
        return value
    raise VoipPushError(
        "--ntf 只接受两种：真待办的编号（ntf-… ，用 "
        "replication_notifications.py list 查），或待办以外的电话用保留号 "
        "%r。收到 %r。\n"
        "⚠ 它是必填的：有了它，接听状态才能自动回落到那条待办上"
        "（拒接→降级、没接→重拨）。" % (MISC_CALL_ID, ntf_id))


def place_call(root: Path, ntf_id: str, title: str,
               reason: str = "") -> dict[str, Any]:
    """打电话并**等到有终局为止**。这是给语音 AI 用的那一个。

    返回 outcome：
        answered    接通了 —— 现在开口说
        downgraded  拒接、或两次没人接 —— 别再管，走通知

    ⚠ 重拨不返回给调用方：从第一次拨打、到等待、到第二次拨打，
    调用方就是在等这一个进程（用户 2026-08-29 明说）。
    """
    ntf_id = _require_valid_ntf(ntf_id)
    # ⚠ **拨号前先清掉残留的挂断信号。**
    #
    # 那个信号是"读一次就消失"的，但写了却没人来读时它会留着 ——
    # 通话没接通、进程被打断、或者有人手动跑了一次 hangup。
    # 下一通一接起来，App 的轮询立刻读到它，表现是**点了接听就消失**。
    #
    # 2026-08-29 我自己撞的：打电话前跑了一次 hangup，以为那是"清除"，
    # 其实那个子命令是"写入"。清在这里，因为**拨号这一刻**才是
    # "上一通确实已经结束"的证据。
    try:
        (_bridge_runtime() / HANGUP_FILE_NAME).unlink()
    except OSError:
        pass
    attempts = 0
    while True:
        attempts += 1
        started_ms = int(time.time() * 1000)
        pushed = _push(root, title, reason, ntf_id)
        if not pushed["ok"]:
            # APNs 拒了 —— 配置问题，重试一百次也一样。
            return {
                "outcome": "failed",
                "attempts": attempts,
                "status": pushed["status"],
                "reason": pushed["reason"],
            }
        outcome = _wait_for_outcome(
            ntf_id, started_ms, time.time() + _RING_TIMEOUT_SECONDS)
        if outcome == "answered":
            return {"outcome": "answered", "attempts": attempts}
        if outcome == "declined":
            # 主动拒接 = 明确的"现在别烦我"。再打一次是骚扰。
            return {"outcome": "downgraded", "attempts": attempts,
                    "reason": "declined"}
        if attempts >= 2:
            return {"outcome": "downgraded", "attempts": attempts,
                    "reason": "unanswered-twice"}
        # 没人接 —— 可能没听见。等一会儿再试一次。
        time.sleep(_RETRY_AFTER_SECONDS)


def request_hangup(root: Path) -> dict[str, Any]:
    """请 App 挂断当前这通电话（说完了）。

    ⚠ 这是"请求"不是"命令"：App 可能已经挂了、或者用户先挂了。
    那种情况下这里成功返回也没有副作用 —— 挂一个已经结束的电话是幂等的。
    """
    path = _bridge_runtime() / HANGUP_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"contract": "reader-voip-hangup/1",
                    "atUtcMs": int(time.time() * 1000)}),
        encoding="utf-8")
    return {"ok": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command")

    call = sub.add_parser("call", help="打电话并等到有结果（阻塞）")
    call.add_argument(
        "--ntf", required=True,
        help="为哪条待办打的。**必填**,两种取值:真待办编号 ntf-… ,"
             "或待办以外的电话用保留号 misc。有了它,接听状态才能自动回落到"
             "那条待办上(拒接→降级、没接→重拨)")
    call.add_argument("--title", required=True, help="来电界面上显示的一句话")
    call.add_argument("--reason", default="")

    sub.add_parser("hangup", help="主动挂断当前通话（说完之后）")
    sub.add_parser("check", help="只检查配置与 token，**不推送**")

    args = parser.parse_args()
    root = Path(args.root) if args.root else default_root()
    try:
        if args.command == "check" or args.command is None:
            config = _load_config(root)
            token = load_device_token(root)
            print(json.dumps({
                "ok": bool(token),
                "keyId": config["keyId"],
                "topic": config["bundleId"] + ".voip",
                "hasDeviceToken": bool(token),
                "note": "" if token else
                        "没有设备 token —— 装了新版 App 并启动一次才会有",
            }, ensure_ascii=False, indent=1))
            return 0 if token else 3
        if args.command == "hangup":
            print(json.dumps(request_hangup(root), ensure_ascii=False))
            return 0
        result = place_call(root, args.ntf, args.title, args.reason)
    except VoipPushError as error:
        print(json.dumps({"outcome": "failed", "error": str(error)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["outcome"] in ("answered", "downgraded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
