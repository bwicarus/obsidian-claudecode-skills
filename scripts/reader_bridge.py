#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阅读器跨机命令桥:Windows→Pi 的**唯一**命令入口(方向 B)。

设计中心约束(用户拍板)：
  跨机联系只有一条 SSH bridge 通道。
    方向 A：Pi 推快照 → Windows 固定 context.md + assets/（push_reader_context_to_pc.py）
    方向 B：外部 Codex 发白名单 envelope → 本脚本校验并路由到阅读器真实接口（本文件）
  外部调用方**永远不需要**知道 state 落盘位置、web API 路径、PDF/EPUB/HTML 宿主差异。
  MCP 仍是"需要实时真值/页面控制"时的能力层，但跨机状态与命令协议唯一、固定、可审计。

Envelope（stdin JSON 或 --json）：
  {version, request_id, kind, payload, file?, page?, context_ref?}
  kind 白名单见 _KINDS。禁止任意路径、任意 JS/HTML/shell。

`assistant_turn` 的写回语义（每轮一次、批量、事务性）：
  payload = {user_utterance?, assistant_text, result?, cards?[], artifacts?[]}
  - cards **可选 0..N**：没有卡片时走纯文本路径，绝不生成空卡/占位卡，协议也不会因此失败。
  - 文本与卡片在**同一条**助手消息里落库（一次 HTTP、一个 parts 数组）→ 不存在
    "文本写了卡片没写"的半状态。
  - request_id 幂等：同 id 重放直接返回上次结果，不重复写入。

审计：每条 envelope 追加到 state/reader-bridge/audit.jsonl（含结果与耗时）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
BASE = os.environ.get("BW_BRIDGE_BASE", "http://127.0.0.1:5000")
STATE = ROOT / "state" / "reader-bridge"
AUDIT = STATE / "audit.jsonl"
SEEN = STATE / "seen.json"          # request_id → 上次结果（幂等）

VERSION = 1
# envelope 的 kind(动作类型)仍是有限集合——这是"外部只能做哪几件事"的权限边界。
# 但**卡片内容**不再由本文件判定:合法卡型/字段的唯一来源是 reader_card_contract
# (它又从前端统一渲染器解析)。以前桥接器自带一份卡片白名单,渲染器一升级它就落后。
_KINDS = {"assistant_turn", "open_page", "highlight", "create_note", "ping"}
_ACK_WAIT_S = 1.5        # 等前端渲染回执的上限;等不到不代表失败,只是"没人开着侧栏"
_ACK_PATH = ROOT / "state" / "reader-bridge" / "acks.json"
_MAX_TEXT = 8000
_MAX_CARDS = 20


def _token() -> str:
    t = (os.environ.get("MCP_WEBAPP_TOKEN") or "").strip()
    if t:
        return t
    try:
        return Path("~/.config/mcp-webapp-token").expanduser().read_text().strip()
    except Exception:
        return ""


def _api(path: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_token()}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", "replace")[:300]}
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


def _seen_load() -> dict:
    try:
        return json.loads(SEEN.read_text("utf-8"))
    except Exception:
        return {}


def _seen_put(rid: str, result: dict) -> None:
    d = _seen_load()
    d[rid] = {"ts": int(time.time()), "result": result}
    if len(d) > 500:                      # 只留最近 500 条幂等键
        for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:len(d) - 500]:
            d.pop(k, None)
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = SEEN.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
    tmp.replace(SEEN)


def _audit(env: dict, result: dict, took: float) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    rec = {"ts": int(time.time()), "request_id": env.get("request_id"), "kind": env.get("kind"),
           "file": env.get("file"), "page": env.get("page"),
           "ok": bool(result.get("ok")), "took_s": round(took, 3),
           "summary": str(result.get("error") or result.get("n") or "")[:200]}
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── kind 处理器 ────────────────────────────────────────────────────────────────
def _active() -> dict:
    """当前活动文档(书/页)。调用方不必自己拼路径、不必知道 sidecar 落在哪。"""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import reader_context_snapshot as SNAP
        rec = SNAP.jload(SNAP.sc("reader-active.json"), {}) or {}
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


def _wait_ack(turn_id: str, timeout: float = _ACK_WAIT_S) -> bool:
    """等前端渲染回执。轮询本地文件(同机,无需再开一条 HTTP)。"""
    if not turn_id:
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            m = json.loads(_ACK_PATH.read_text("utf-8"))
            if isinstance(m, dict) and turn_id in m:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def _do_assistant_turn(env: dict) -> dict:
    """一轮助手回复:**调用方只交高层内容**,其余全自动。

    payload = {text, cards?[], flashcards?[], result?, user_utterance?}
      - text        本轮说给用户的话(纯文本/Markdown)
      - cards       0..N 张**工具卡**(天气/新闻/配图/视频/事实/综合),字段由
                    reader_card_contract 校验 —— 不合规明确报错,不静默丢弃
      - flashcards  0..N 张 Anki 草稿卡(跟工具卡是两回事,分开传免得互相冒充)
      - result      可选的操作结果对象,附在正文后
    书名/页码/请求号都由本函数自动取(当前活动文档 + 时间戳),调用方**不需要**
    拼路径、不需要自己编 JSON 结构、也不需要知道 parts 协议长什么样。
    """
    p = env.get("payload") or {}
    text = str(p.get("text") or p.get("assistant_text") or "").strip()[:_MAX_TEXT]
    cards = p.get("cards") if isinstance(p.get("cards"), list) else []
    flash = p.get("flashcards") if isinstance(p.get("flashcards"), list) else []
    if not text and not cards and not flash:
        return {"ok": False, "error": "text / cards / flashcards 至少给一样"}

    # 卡片校验:唯一来源是渲染器契约。不合规**明确拒绝**并指出是哪张卡的哪个字段。
    parts = []
    if text:
        parts.append({"kind": "text", "text": text})
    for i, c in enumerate(cards[:_MAX_CARDS]):
        try:
            sys.path.insert(0, str(ROOT / "_server_deploy"))
            import reader_card_contract as CC
            parts.append({"kind": "card", "card": CC.validate_card(c)})
        except Exception as e:
            return {"ok": False, "error": f"cards[{i}] 不合契约:{e}",
                    "hint": "合法卡型以前端统一渲染器为准;字段规格见 reader_card_contract.CARD_FIELD_SPECS"}
    if flash:
        parts.append({"kind": "cards", "cards": flash[:_MAX_CARDS], "draft": True})
    if isinstance(p.get("result"), (dict, list)) and p["result"]:
        parts.append({"kind": "text",
                      "text": "```json\n" + json.dumps(p["result"], ensure_ascii=False)[:2000] + "\n```"})

    act = _active()
    turn_id = str(env.get("request_id"))[:40]
    body = {"assistant": text or "[卡片]", "parts": parts, "via": "bridge", "turn_id": turn_id}
    if p.get("user_utterance"):
        body["user"] = str(p["user_utterance"])[:_MAX_TEXT]
    # 书/页:envelope 显式给了就用,否则自动取当前活动文档(合并书用真实卷,页码用卷内页)
    f = env.get("file") or act.get("member") or act.get("file")
    if f:
        body["file"] = str(f)
    pg = env.get("page")
    if pg is None:
        pg = act.get("member_pos") if act.get("member_pos") is not None else act.get("pos")
    try:
        if pg is not None:
            body["page"] = int(pg)
    except Exception:
        pass

    r = _api("/api/assistant/log", body)
    if not r.get("ok"):
        return {"ok": False, "written": False,
                "error": r.get("error") or r.get("detail") or "写入失败",
                "where": "/api/assistant/log"}
    delivered = int(r.get("delivered") or 0)
    acked = _wait_ack(turn_id) if delivered else False
    return {
        "ok": True,
        "written": True,                     # ← 已落库(历史里一定有,刷新可见)
        "turn_id": turn_id,
        "parts": len(parts), "cards": len(cards), "flashcards": len(flash),
        "auto": {"file": body.get("file"), "page": body.get("page")},
        "delivery": {                        # ← 与"已写入"分开:前端到底收到/画出来没有
            "published": delivered > 0,
            "subscribers": delivered,
            "rendered": acked,
            "note": ("侧栏已实时渲染" if acked else
                     ("已推送但未收到渲染回执(侧栏可能没开或正在加载),刷新后可见"
                      if delivered else "当前没有在线侧栏订阅,内容已入库,打开侧栏即可见")),
        },
    }


def _do_open_page(env: dict) -> dict:
    p = env.get("payload") or {}
    return _api("/pdf/api/reading-pos", {"file": env.get("file"), "kind": p.get("kind") or "pdf",
                                         "pos": int(p.get("page") or env.get("page") or 1)})


def _do_highlight(env: dict) -> dict:
    p = env.get("payload") or {}
    return _api("/pdf/api/highlight-text",
                {"file": env.get("file"), "page": env.get("page"),
                 "texts": [str(t)[:2000] for t in (p.get("texts") or [])][:15],
                 "color": p.get("color") or ""})


def _do_create_note(env: dict) -> dict:
    p = env.get("payload") or {}
    return _api("/pdf/api/notes",
                {"file": env.get("file"),
                 "anchor": {"kind": p.get("host") or "pdf", "page": env.get("page") or 1,
                            "x": float(p.get("x") or 0.5), "y": float(p.get("y") or 0.5)},
                 "text": str(p.get("text") or "")[:4000]})


_HANDLERS = {
    "assistant_turn": _do_assistant_turn,
    "open_page": _do_open_page,
    "highlight": _do_highlight,
    "create_note": _do_create_note,
    "ping": lambda env: {"ok": True, "pong": int(time.time())},
}


def handle(env: dict) -> dict:
    if not isinstance(env, dict):
        return {"ok": False, "error": "envelope 必须是 JSON 对象"}
    if int(env.get("version") or 0) != VERSION:
        return {"ok": False, "error": f"version 必须是 {VERSION}"}
    rid = str(env.get("request_id") or "").strip()
    if not rid or len(rid) > 64:
        return {"ok": False, "error": "request_id 必填（≤64 字符）"}
    kind = str(env.get("kind") or "").strip()
    if kind not in _KINDS:
        return {"ok": False, "error": f"kind 不在白名单：{sorted(_KINDS)}"}
    f = env.get("file")
    if f is not None:
        f = str(f)
        if ".." in f or f.startswith(("/", "\\")) or ":" in f.split("/")[0][2:]:
            return {"ok": False, "error": "file 必须是 vault 相对路径，禁止绝对路径与 .."}

    prev = _seen_load().get(rid)
    if prev:                                    # 幂等：同 request_id 重放不重复写
        out = dict(prev["result"])
        out["idempotent_replay"] = True
        return out

    t0 = time.time()
    try:
        result = _HANDLERS[kind](env)
    except Exception as ex:
        result = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    took = time.time() - t0
    if result.get("ok"):
        _seen_put(rid, result)
    _audit(env, result, took)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="阅读器跨机命令桥（唯一入口）")
    ap.add_argument("--json", help="envelope JSON；省略则从 stdin 读")
    a = ap.parse_args()
    raw = a.json if a.json else sys.stdin.read()
    try:
        env = json.loads(raw)
    except Exception as ex:
        print(json.dumps({"ok": False, "error": f"JSON 解析失败: {ex}"}, ensure_ascii=False))
        return 2
    out = handle(env)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
