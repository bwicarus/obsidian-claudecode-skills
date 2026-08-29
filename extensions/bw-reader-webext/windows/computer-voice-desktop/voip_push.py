#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 iPad 打一通电话 —— 通知阶梯最响的那一级（用户 2026-08-29 拍板）。

普通通知会被专注模式、静音、锁屏挡住。当有一条**必须现在让他知道**的事、
而他又不在语音会话里也没在用 App 时，唯一能穿透的就是系统来电 ——
像 LINE / 微信那样。接通后复用已有的语音通道，AI 直接说。

## ⚠ 这条通道只能真的响铃

iOS 13 起，每一个 VoIP 推送都**必须**让 App 立刻向 CallKit 报一通来电，
否则 App 被杀、后续推送被永久拒发。所以：

- 不要拿它做"更响一点的通知"。它在 deliver 里是独立的一档。
- 不要用它试探设备在不在线。推一次就是响一次。

## ⚠ Production 环境

密钥在开发者后台配成 Production（用户 2026-08-29 选的，不可更改），
TestFlight 装的 App 走的正是这个环境。所以主机名固定
api.push.apple.com，**不是** api.sandbox.push.apple.com。
配错的表现是 APNs 返回 BadDeviceToken —— 看起来像 token 坏了，
其实是环境不匹配。这条写在这里，免得下次照着症状去查错方向。
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

# JWT 有效期 Apple 规定最长 1 小时；低于 20 分钟重签会被限流
# （同一把 key 频繁签发会收到 TooManyProviderTokenUpdates）。
# 取 45 分钟：既不碰上限，也不至于频繁重签。
_TOKEN_TTL_SECONDS = 45 * 60

_cached_jwt: tuple[str, float] | None = None


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


class VoipPushError(RuntimeError):
    pass


def _load_config(root: Path) -> dict[str, Any]:
    """读 APNs 配置。**缺什么就说缺什么** —— 一句"配置无效"会让人
    把三个字段全查一遍。"""
    path = root / "voip-push-config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise VoipPushError(
            "没有 %s。需要一个 JSON，含 keyId / teamId / keyPath "
            "（keyPath 指向 .p8 私钥文件）" % path) from error
    except json.JSONDecodeError as error:
        raise VoipPushError("%s 不是合法 JSON：%s" % (path, error)) from error
    missing = [k for k in ("keyId", "teamId", "keyPath", "bundleId")
               if not value.get(k)]
    if missing:
        raise VoipPushError(
            "%s 缺字段：%s" % (path, "、".join(missing)))
    if not Path(value["keyPath"]).is_file():
        raise VoipPushError(
            "keyPath 指向的私钥不存在：%s（.p8 只能下载一次，"
            "丢了要在开发者后台重建）" % value["keyPath"])
    return value


def _provider_jwt(config: dict[str, Any]) -> str:
    """签一个 provider token。缓存 45 分钟，见 _TOKEN_TTL_SECONDS。"""
    global _cached_jwt
    now = time.time()
    if _cached_jwt and now < _cached_jwt[1]:
        return _cached_jwt[0]
    try:
        import jwt  # PyJWT
    except ImportError as error:
        raise VoipPushError(
            "缺 PyJWT：pip install pyjwt cryptography") from error
    private_key = Path(config["keyPath"]).read_text(encoding="utf-8")
    token = jwt.encode(
        {"iss": config["teamId"], "iat": int(now)},
        private_key,
        algorithm="ES256",
        headers={"kid": config["keyId"]},
    )
    _cached_jwt = (token, now + _TOKEN_TTL_SECONDS)
    return token


def _token_paths(root: Path) -> list[Path]:
    """token 可能落在哪。

    ⚠ **写它的和读它的不是同一个目录** —— 桥写在自己的 runtime 目录
    （bw-computer-voice-bridge/runtime），而这里的 root 默认是
    %LOCALAPPDATA%\\BWReader。2026-08-29 实测踩到：路由回 ok、
    文件也确实写了，而 --check 说"没有 token" —— 两边都没错，
    只是各说各的路径。

    与其挑一个"正确的"再去改另一边（那会让已经在跑的东西对不上），
    不如**两个都找**：这个文件很小、只增不减，多看一处的代价近乎零，
    而看漏一处的代价是电话永远打不进来且没人报错。
    """
    bridge_runtime = (
        Path(os.environ.get("USERPROFILE") or Path.home())
        / "bw-computer-voice-bridge" / "runtime")
    return [root / TOKEN_FILE_NAME, bridge_runtime / TOKEN_FILE_NAME]


def load_device_token(root: Path) -> str | None:
    """设备上报的 VoIP token。没有 = 打不进去，**这不是可以忽略的状态**。"""
    newest: tuple[float, str] | None = None
    for path in _token_paths(root):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            token = value.get("token")
            if not isinstance(token, str) or not token:
                continue
            stamp = path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        # 两处都有时取**最新的那份**：token 会变（重装/恢复备份/系统更新），
        # 拿旧的推等于推给一个已经不存在的设备。
        if newest is None or stamp > newest[0]:
            newest = (stamp, token)
    return newest[1] if newest else None


def send_call(root: Path, title: str, reason: str = "",
              notification_id: str = "") -> dict[str, Any]:
    """推一通来电。

    ⚠ 返回值一定要看：APNs 会用 200 之外的状态码带上原因
    （BadDeviceToken / TopicDisallowed / …）。吞掉它的话，
    表现就是"推了但没响"，而没有任何一处会报错。
    """
    config = _load_config(root)
    device_token = load_device_token(root)
    if not device_token:
        raise VoipPushError(
            "还没有设备 token（%s 不存在或为空）。App 启动时会上报一次；"
            "没有它就永远打不进来，而且失败是静默的" % (root / TOKEN_FILE_NAME))
    try:
        import httpx
    except ImportError as error:
        raise VoipPushError(
            "缺 httpx（APNs 要 HTTP/2）：pip install 'httpx[http2]'"
        ) from error

    payload = {"title": title[:80], "reason": reason[:200]}
    if notification_id:
        # ⚠ 带上是哪条待办。挂断时 App 会连着结局一起回报 ——
        # 没有它，Windows 侧不知道该给谁记「拒接 / 没接」，那条会永远
        # 卡在"已拨出"：既不重拨也不降级，用户什么也收不到。
        payload["notificationId"] = notification_id[:64]
    headers = {
        "authorization": "bearer " + _provider_jwt(config),
        # ⚠ VoIP 推送的 topic **必须**带 .voip 后缀，用普通 bundleId
        # 会被 APNs 以 TopicDisallowed 拒掉。
        "apns-topic": config["bundleId"] + ".voip",
        "apns-push-type": "voip",
        "apns-priority": "10",
        "apns-expiration": "0",
    }
    with httpx.Client(http2=True, timeout=15.0) as client:
        response = client.post(
            "https://%s/3/device/%s" % (APNS_HOST, device_token),
            headers=headers,
            json=payload,
        )
    ok = response.status_code == 200
    detail = ""
    if not ok:
        try:
            detail = response.json().get("reason", "")
        except Exception:
            detail = response.text[:200]
    return {
        "ok": ok,
        "status": response.status_code,
        "reason": detail,
        "apnsId": response.headers.get("apns-id", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None)
    parser.add_argument("--title", default="BWReader")
    parser.add_argument("--reason", default="")
    parser.add_argument(
        "--ntf", default="",
        help="这通电话是为哪条待办打的（ntf-…）。带上它，挂断后的"
             "「拒接/没接」才会记到那条待办上")
    parser.add_argument(
        "--check", action="store_true",
        help="只检查配置与 token 是否就绪，**不推送**（推一次就是响一次）")
    args = parser.parse_args()
    root = Path(args.root) if args.root else default_root()
    try:
        if args.check:
            config = _load_config(root)
            token = load_device_token(root)
            print(json.dumps({
                "ok": bool(token),
                "keyId": config["keyId"],
                "teamId": config["teamId"],
                "topic": config["bundleId"] + ".voip",
                "hasDeviceToken": bool(token),
                "note": "" if token else
                        "没有设备 token —— 装了新版 App 并启动一次才会有",
            }, ensure_ascii=False, indent=1))
            return 0 if token else 3
        result = send_call(root, args.title, args.reason, args.ntf)
    except VoipPushError as error:
        print("错误：%s" % error)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
