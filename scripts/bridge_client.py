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
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

PI_HOST = os.environ.get("BW_PI_HOST", "bwicarus@bwicarus.taile44d0c.ts.net")
REMOTE = os.environ.get("BW_BRIDGE_REMOTE", "/home/bwicarus/claude/scripts/reader_bridge.py")
IDLE_S = int(os.environ.get("BW_BRIDGE_IDLE_S", "60"))    # 空闲多久自动断(ControlPersist)
CONNECT_TIMEOUT_S = 10
CALL_TIMEOUT_S = 45

_CTL_DIR = Path(os.environ.get("TEMP", "/tmp")) / "bw-bridge-ssh"


class BridgeError(RuntimeError):
    """连接/执行失败。消息里必须能看出是哪一层出的问题。"""


class ResultEnvelopeError(ValueError):
    """reader-result/1 不合规；在触网前 fail closed。"""


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
        # reader_bridge.py 省略 --json 时才从 stdin 读；它从来没有 --stdin 参数。
        _ssh_base() + ["python3", REMOTE],
        input=payload, encoding="utf-8", errors="strict",
        capture_output=True, timeout=CALL_TIMEOUT_S,
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


_RESULT_CONTRACT = "reader-result/1"
_RESULT_VALIDATION_CONTRACT = "reader-result-validation/1"
_RESULT_TOOL_KINDS = {"weather", "news", "images", "videos", "fact", "general"}
_RESULT_KINDS = _RESULT_TOOL_KINDS | {"cards"}
_CORRELATION_RE = re.compile(r"[A-Za-z0-9._:-]{1,40}\Z")
_MAX_RESULT_ITEMS = 20
_MAX_RESULT_TEXT = 2000


def _strict_json_loads(raw: str):
    """Parse CLI JSON without JSON's duplicate-key/NaN ambiguities."""
    # Windows PowerShell 管道可能在 stdin 开头附 UTF-8 BOM；它不是 payload
    # 字段，也不应迫使调用方改用临时文件。
    raw = raw.removeprefix("\ufeff")

    def reject_constant(value: str):
        raise ResultEnvelopeError(f"JSON 不允许 {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ResultEnvelopeError(f"JSON 含重复字段:{key}")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def _result_object(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise ResultEnvelopeError(f"{where} 必须是对象")
    return value


def _result_exact_fields(value: dict, required: set[str], optional: set[str],
                         where: str) -> None:
    missing = required - set(value)
    if missing:
        raise ResultEnvelopeError(f"{where} 缺字段:{sorted(missing)}")
    extra = set(value) - required - optional
    if extra:
        raise ResultEnvelopeError(f"{where} 含未知/多余字段:{sorted(extra)}")


def _result_text(value, where: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ResultEnvelopeError(f"{where} 必须是字符串")
    if required and not value.strip():
        raise ResultEnvelopeError(f"{where} 不能为空")
    if len(value) > _MAX_RESULT_TEXT:
        raise ResultEnvelopeError(f"{where} 超过 {_MAX_RESULT_TEXT} 字符")
    return value


def _result_scalar(value, where: str):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ResultEnvelopeError(f"{where} 必须是字符串或数字")
    if isinstance(value, float) and not math.isfinite(value):
        raise ResultEnvelopeError(f"{where} 不能是 NaN/Infinity")
    if isinstance(value, str):
        return _result_text(value, where, required=True)
    return value


def _result_url(value, where: str, *,
                allow_reader_page_image: bool = False) -> str:
    text = _result_text(value, where, required=True)
    # str 没有 iscontrol()。URL 是安全边界，Unicode 的 Control/Format/
    # Surrogate/Private-use/Unassigned 一律 fail closed，连同首尾空白一起拒绝。
    if text != text.strip() or any(
        unicodedata.category(char).startswith("C") for char in text
    ):
        raise ResultEnvelopeError(f"{where} 必须是安全 URL")
    if "\\" in text:
        raise ResultEnvelopeError(f"{where} 必须是安全 URL")
    try:
        parsed = urlsplit(text)
        # Accessing port also rejects malformed bracketed hosts and ports.
        _ = parsed.port
    except ValueError as exc:
        raise ResultEnvelopeError(f"{where} 必须是安全 URL") from exc
    if parsed.scheme.lower() == "https":
        if (
            not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ResultEnvelopeError(f"{where} 只允许无凭据的 HTTPS URL")
        return text
    if (
        allow_reader_page_image
        and not parsed.scheme
        and not parsed.netloc
        and parsed.path == "/pdf/api/page-image"
        and not parsed.fragment
    ):
        try:
            pairs = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError as exc:
            raise ResultEnvelopeError(
                f"{where} Reader 页图参数无效") from exc
        params = {}
        for key, item in pairs:
            if key in params:
                raise ResultEnvelopeError(
                    f"{where} Reader 页图参数重复:{key}")
            params[key] = item
        if set(params) - {"file", "page", "w", "v", "sharp"}:
            raise ResultEnvelopeError(
                f"{where} Reader 页图含未知参数")
        file = params.get("file", "")
        page = params.get("page", "")
        if (
            not file
            or file.startswith(("/", "\\"))
            or "\x00" in file
            or ".." in re.split(r"[\\/]", file)
            or not page.isdecimal()
            or int(page) < 1
        ):
            raise ResultEnvelopeError(
                f"{where} Reader 页图必须包含安全 file 与正整数 page")
        if any(
            value and not value.isdecimal()
            for key, value in params.items()
            if key in {"w", "v", "sharp"}
        ):
            raise ResultEnvelopeError(
                f"{where} Reader 页图数值参数无效")
        return text
    raise ResultEnvelopeError(
        f"{where} 只允许 HTTPS"
        + (" 或 Reader 相对页图 URL" if allow_reader_page_image else "")
    )


def _result_item_list(payload: dict, kind: str, *, required: set[str],
                      optional: set[str]) -> dict:
    _result_exact_fields(payload, {"items"}, set(), f"payload({kind})")
    items = payload["items"]
    if not isinstance(items, list) or not items:
        raise ResultEnvelopeError(f"payload({kind}).items 必须是非空数组")
    if len(items) > _MAX_RESULT_ITEMS:
        raise ResultEnvelopeError(
            f"payload({kind}).items 超过 {_MAX_RESULT_ITEMS} 条")
    out = []
    for index, raw in enumerate(items):
        item = _result_object(raw, f"payload({kind}).items[{index}]")
        _result_exact_fields(
            item, required, optional, f"payload({kind}).items[{index}]")
        one = {}
        for field in required | optional:
            if field not in item:
                continue
            where = f"payload({kind}).items[{index}].{field}"
            if kind == "images" and field == "url":
                one[field] = _result_url(
                    item[field],
                    where,
                    allow_reader_page_image=True,
                )
            elif kind == "videos" and field == "thumb":
                one[field] = _result_url(
                    item[field],
                    where,
                    allow_reader_page_image=True,
                )
            elif kind == "videos" and field == "url":
                one[field] = _result_url(item[field], where)
            else:
                one[field] = _result_text(
                    item[field],
                    where,
                    required=field in required,
                )
        out.append(one)
    return {"items": out}


def _normalize_tool_payload(kind: str, raw) -> dict:
    payload = _result_object(raw, f"payload({kind})")
    if kind == "weather":
        required = {"lo", "hi", "cond"}
        optional = {"loc", "date", "precip", "tip"}
        _result_exact_fields(payload, required, optional, "payload(weather)")
        out = {}
        for field in required | optional:
            if field in payload:
                out[field] = _result_scalar(
                    payload[field], f"payload(weather).{field}")
        return out
    if kind == "news":
        return _result_item_list(
            payload, kind, required={"t"}, optional={"s", "src"})
    if kind == "images":
        return _result_item_list(
            payload, kind, required={"url"}, optional={"title", "aid", "src"})
    if kind == "videos":
        return _result_item_list(
            payload, kind, required={"title"},
            optional={"thumb", "url", "channel", "src"})
    if kind == "fact":
        _result_exact_fields(payload, {"answer"}, {"detail"}, "payload(fact)")
        out = {"answer": _result_scalar(payload["answer"], "payload(fact).answer")}
        if "detail" in payload:
            out["detail"] = _result_text(
                payload["detail"], "payload(fact).detail")
        return out
    if kind == "general":
        _result_exact_fields(payload, set(), {"text"}, "payload(general)")
        return ({
            "text": _result_text(payload["text"], "payload(general).text")
        } if "text" in payload else {})
    raise ResultEnvelopeError(f"kind={kind!r} 不是结果卡类型")


def _normalize_flashcards(raw) -> list[dict]:
    payload = _result_object(raw, "payload(cards)")
    _result_exact_fields(payload, {"cards"}, {"draft"}, "payload(cards)")
    if "draft" in payload and payload["draft"] is not True:
        # reader_bridge.py 的既有落点只表达草稿卡；不能静默把 false 改成 true。
        raise ResultEnvelopeError("payload(cards).draft 当前只能省略或为 true")
    cards = payload["cards"]
    if not isinstance(cards, list) or not cards:
        raise ResultEnvelopeError("payload(cards).cards 必须是非空数组")
    if len(cards) > _MAX_RESULT_ITEMS:
        raise ResultEnvelopeError(
            f"payload(cards).cards 超过 {_MAX_RESULT_ITEMS} 张")
    out = []
    allowed = {"type", "front", "back", "cloze", "text"}
    for index, raw_card in enumerate(cards):
        card = _result_object(raw_card, f"payload(cards).cards[{index}]")
        _result_exact_fields(
            card, set(), allowed, f"payload(cards).cards[{index}]")
        one = {}
        for field in allowed - {"type"}:
            if field in card:
                one[field] = _result_text(
                    card[field], f"payload(cards).cards[{index}].{field}")
        card_type = card.get("type")
        if card_type is not None and card_type not in {"basic", "cloze"}:
            raise ResultEnvelopeError(
                f"payload(cards).cards[{index}].type 必须是 basic 或 cloze")
        if not any(str(one.get(k) or "").strip()
                   for k in ("front", "cloze", "text")):
            raise ResultEnvelopeError(
                f"payload(cards).cards[{index}] 缺 front/cloze/text")
        inferred = card_type or (
            "basic" if str(one.get("front") or "").strip() else "cloze")
        if inferred == "basic" and not str(one.get("front") or "").strip():
            raise ResultEnvelopeError(
                f"payload(cards).cards[{index}] basic 卡缺 front")
        if inferred == "cloze" and not any(
                str(one.get(k) or "").strip() for k in ("cloze", "text")):
            raise ResultEnvelopeError(
                f"payload(cards).cards[{index}] cloze 卡缺 cloze/text")
        out.append({"type": inferred, **one})
    return out


def _normalize_sources(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ResultEnvelopeError("sources 必须是非空数组")
    if len(raw) > 5:
        raise ResultEnvelopeError("sources 最多 5 条")
    out = []
    for index, item_raw in enumerate(raw):
        item = _result_object(item_raw, f"sources[{index}]")
        _result_exact_fields(item, {"url", "title"}, set(), f"sources[{index}]")
        out.append({
            "url": _result_url(
                item["url"], f"sources[{index}].url"),
            "title": _result_text(
                item["title"], f"sources[{index}].title", required=True),
        })
    return out


def _normalize_anchor(raw) -> tuple[str, int]:
    anchor = _result_object(raw, "anchor")
    # selection 在 reader-result/1 里是可选元信息，但 result.present 的既有
    # 展示落点没有对应字段。这里明确拒绝，避免让调用方误以为选区已经被保存。
    if "selection" in anchor:
        raise ResultEnvelopeError(
            "anchor.selection 当前没有确定性落点，拒绝静默丢弃")
    _result_exact_fields(anchor, {"file", "page"}, set(), "anchor")
    file = _result_text(anchor["file"], "anchor.file", required=True)
    if (file.startswith(("/", "\\")) or "\x00" in file or ":" in file
            or ".." in re.split(r"[\\/]", file)):
        raise ResultEnvelopeError("anchor.file 必须是无 .. 的 vault 相对路径")
    page = anchor["page"]
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ResultEnvelopeError("anchor.page 必须是正整数")
    return file, page


def validate_result(envelope: dict) -> dict:
    """本地验证并规范化 reader-result/1；绝不连接 SSH/Pi。

    返回值是即将交给既有 ``result.present`` 的确定性直接命令预览。
    它不是 MCP mutation，也不会调用 AI、voice-typist 或任何网络入口。
    """
    env = _result_object(envelope, "reader-result")
    required = {"envelope", "correlation", "kind", "payload", "anchor"}
    optional = {"title", "brief", "sources"}
    _result_exact_fields(env, required, optional, "reader-result")
    if env["envelope"] != _RESULT_CONTRACT:
        raise ResultEnvelopeError(f"envelope 必须是 {_RESULT_CONTRACT}")
    correlation = env["correlation"]
    if not isinstance(correlation, str) or not _CORRELATION_RE.fullmatch(correlation):
        raise ResultEnvelopeError(
            "correlation 必须匹配 [A-Za-z0-9._:-]{1,40}")
    kind = env["kind"]
    if not isinstance(kind, str) or kind not in _RESULT_KINDS:
        raise ResultEnvelopeError(
            f"kind 不支持；可用:{sorted(_RESULT_KINDS)}")
    file, page = _normalize_anchor(env["anchor"])

    if kind == "cards":
        forbidden = optional & set(env)
        if forbidden:
            raise ResultEnvelopeError(
                f"kind=cards 不支持这些顶层字段:{sorted(forbidden)}")
        parts = [{
            "kind": "cards",
            "cards": _normalize_flashcards(env["payload"]),
            "draft": True,
        }]
    else:
        card = {"kind": kind, "data": _normalize_tool_payload(kind, env["payload"])}
        for field in ("title", "brief"):
            if field in env:
                card[field] = _result_text(env[field], field)
        if "sources" in env:
            card["sources"] = _normalize_sources(env["sources"])
        parts = [{"kind": "card", "card": card}]
    command = {
        "contract": "reader-direct-command/1",
        "correlation": correlation,
        "mode": "independent",
        "idempotency": correlation,
        "action": "result.present",
        "anchor": {"file": file, "page": page},
        "params": {"turnId": correlation, "parts": parts},
    }
    return {
        "ok": True,
        "contract": _RESULT_VALIDATION_CONTRACT,
        "networkAttempted": False,
        "action": "direct_command",
        "requestId": correlation,
        "payload": command,
    }


def publish_result(envelope: dict) -> dict:
    """把严格的 reader-result/1 映射到既有 result.present 直接回写。

    该路径只做确定性结构变换；不把结果塞进正文、不调用 AI，也不扩展 Pi 的动作面。
    ``correlation`` 原样用作 request_id，所以 ``call`` 首次失败后的唯一一次重连仍使用
    同一幂等键。
    """
    normalized = validate_result(envelope)
    return call(
        normalized["action"],
        normalized["payload"],
        request_id=normalized["requestId"],
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Windows→Pi 阅读器桥接客户端")
    ap.add_argument("--kind", default="assistant_turn")
    ap.add_argument("--text", help="assistant_turn 的回复正文")
    ap.add_argument("--json", help="完整 payload(JSON);给了它就忽略 --text")
    ap.add_argument(
        "--publish-result",
        action="store_true",
        help="从 stdin 读取严格 reader-result/1 并走确定性卡片回写",
    )
    ap.add_argument(
        "--validate-result",
        action="store_true",
        help="只在本机验证 stdin 的 reader-result/1；不连接 SSH/Pi",
    )
    ap.add_argument("--ping", action="store_true", help="只测连通性")
    a = ap.parse_args()
    try:
        if a.ping:
            r = call("ping", {})
        elif a.validate_result:
            r = validate_result(_strict_json_loads(sys.stdin.read()))
        elif a.publish_result:
            r = publish_result(_strict_json_loads(sys.stdin.read()))
        elif a.json:
            r = call(a.kind, _strict_json_loads(a.json))
        elif a.text:
            r = say(a.text)
        else:
            ap.error(
                "要么 --text,要么 --json,要么 --ping,"
                "要么 --validate-result/--publish-result")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    except (BridgeError, ResultEnvelopeError, json.JSONDecodeError) as e:
        print(f"[bridge] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
