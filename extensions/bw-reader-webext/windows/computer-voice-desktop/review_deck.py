#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review_deck — 取「现在该复习的这一组卡」，供语音复习逐张念。

    python review_deck.py                 # 紧凑清单（默认 10 张）
    python review_deck.py --limit 5
    python review_deck.py --json          # 机器可读，含正反面全文
    python review_deck.py --card <cid>    # 只看一张

## 为什么存在（用户 2026-09-09）

> 我希望能提供一种可选的语音复习功能，也就是说 ai 提醒我复习后我可以选择让 ai
> 语音描述卡片的正面内容我来回答背面内容，根据我回答的结果 ai 来判断掌握程度后
> 录入……只是为 ai 提供一个读取最新需要复习的一组 anki 卡片内容并代替我选择
> 掌握度的一个接口

这是那个接口的**读**的一半：一条命令拿到该复习的一组卡，含正面（念给他听）、
背面（对照他的回答）、以及评分要用的身份。AI 不必自己去翻数据目录 ——
临场发明的取数路径没有任何人测过，这正是整套情境原语的出发点。

**写**的一半（代他录入掌握度）还没有：见下面「为什么现在还评不了」。

## 为什么现在还评不了

Reader **自己不排期**。评分要走 `/pdf/api/review-answer` → AnkiConnect 的
`answerCards`，必须有真实 Anki 卡号；而卡的权威状态（`_next`）在 App 本地，
Windows 这边只有复制过来的副本。所以「Windows 直接评分」会让 Anki 和 App
各存一份进度，对不上。

要补齐这一半，正确的路是走两节点复制的命令通道（App 会应用 Windows 发过来的
命令），而不是绕过它直接改副本。这里先把读的一半做扎实。

## 纪律

- **不猜哪张最该做**：到期的按逾期时长排，新卡按最早创建排，就这两条。
  "他最可能忘的那张"是判断不是数据，交给 AI。
- **评不了的卡照样列出来并标明**：藏起来会让 AI 以为总数不对；
  2026-09-09 就有 10 张卡因为没进 Anki 而评不了分，藏起来只会让人更晚发现。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

CONTRACT = "review-deck/1"
DEFAULT_LIMIT = 10
#: 正反面截断长度。语音复习念的是全文，所以 --json 不截；
#: 只有给人看的紧凑清单才截。
BRIEF_CHARS = 40


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _next_ms(value: Any) -> int | None:
    """`_next` 的语义按秒或毫秒都可能；>1e12 视为毫秒（与 count_due_cards 同口径）。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0:
        return None
    return int(value if value > 1e12 else value * 1000)


_RT_RE = None
_TAG_RE = None
_BREAK_RE = None


def speakable(html: str) -> str:
    """把卡面 HTML 变成**能念出来**的纯文本。

    ⚠ 振假名要**整段丢掉**，不能只剥标签：`<ruby>教<rt>きょう</rt></ruby>`
    剥完标签是「教きょう」，念出来是把汉字和它的读音连着读一遍。
    保留底字（教）让 TTS 自己按上下文读，是三种做法里唯一不出错的。
    """
    import re
    global _RT_RE, _TAG_RE, _BREAK_RE
    if _RT_RE is None:
        # rp 是给不支持 ruby 的浏览器看的括号，一并丢掉
        _RT_RE = re.compile(r"<(rt|rp)\b[^>]*>.*?</\1>", re.I | re.S)
        # <br> / </p> / </li> 是**停顿**，剥成空会把分条的背面连成一句
        _BREAK_RE = re.compile(r"<(br|/p|/li|/div)\b[^>]*>", re.I)
        _TAG_RE = re.compile(r"<[^>]+>")
    text = _RT_RE.sub("", str(html or ""))
    text = _BREAK_RE.sub(chr(10), text)
    text = _TAG_RE.sub("", text)
    for entity, plain in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                          ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, plain)
    # 换行留着：背面常是分条的，念的时候那是停顿点
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def collect(root: Path | None = None) -> list[dict[str, Any]]:
    """全部待复习的卡（到期的 + 没学过的），带内容和身份。"""
    root = root or default_root()
    data_dir = root / "replication-data"
    now = _now_ms()
    deck: list[dict[str, Any]] = []
    if not data_dir.is_dir():
        return deck
    for book_dir in sorted(data_dir.iterdir()):
        path = book_dir / "document-notes.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for item_id, item in (value.get("items") or {}).items():
            card = item.get("card") if isinstance(item, dict) else None
            if not isinstance(card, dict):
                continue
            for index, one in enumerate(card.get("cards") or []):
                if not isinstance(one, dict) or one.get("_removed"):
                    continue
                due_at = _next_ms(one.get("_next"))
                if due_at is not None and due_at > now:
                    continue                      # 还没到期
                if due_at is None and one.get("_st") not in ("learn", None):
                    continue                      # 既不到期也不是新卡
                blocked = bool(one.get("_ratingUnavailable")) and (
                    one.get("_ratingUnavailableReason")
                    in ("not-exported", "export-unknown"))
                deck.append({
                    # 身份：gid+index 是 Reader 的稳定坐标；noteId 是评分要用的
                    # 外部投影（没有它就评不了 —— 见模块头）。
                    "gid": card.get("gid") or "",
                    "index": index,
                    "cid": card.get("cid") or "",
                    "noteId": one.get("_nid"),
                    "book": book_dir.name,
                    "itemId": item_id,
                    "kind": "due" if due_at is not None else "new",
                    "dueAtMs": due_at,
                    "overdueMinutes": (
                        None if due_at is None
                        else round((now - due_at) / 60000.0)),
                    "createdAtMs": item.get("created"),
                    "type": one.get("type") or "basic",
                    "front": one.get("front") or "",
                    "back": one.get("back") or "",
                    # 念给用户听用这两个；原文留着给要显示的场合。
                    "frontText": speakable(one.get("front")),
                    "backText": speakable(one.get("back")),
                    "cloze": one.get("cloze") or "",
                    "gradable": not blocked,
                    "blockedReason": (
                        one.get("_ratingUnavailableReason") if blocked else None),
                })
    # 到期的排前面（逾期越久越靠前），然后新卡按最早创建。
    # ⚠ 不做"他最可能忘的那张"这类排序：那是判断不是数据。
    deck.sort(key=lambda one: (
        0 if one["kind"] == "due" else 1,
        -(one["overdueMinutes"] or 0) if one["kind"] == "due" else 0,
        one["createdAtMs"] or 0,
    ))
    return deck


def take(root: Path | None = None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    deck = collect(root)
    limit = max(1, min(int(limit), 50))
    return {
        "contract": CONTRACT,
        "atUtcMs": _now_ms(),
        "total": len(deck),
        "due": sum(1 for one in deck if one["kind"] == "due"),
        "new": sum(1 for one in deck if one["kind"] == "new"),
        "blocked": sum(1 for one in deck if not one["gradable"]),
        "cards": deck[:limit],
    }


def _brief(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat[:BRIEF_CHARS] + ("…" if len(flat) > BRIEF_CHARS else "")


def render(payload: dict[str, Any]) -> str:
    """给人/AI 读的紧凑清单。念给用户听要用 --json 拿全文。"""
    lines = ["待复习 %d 张（到期 %d，新卡 %d%s），下面列 %d 张：" % (
        payload["total"], payload["due"], payload["new"],
        "，其中 %d 张评不了分" % payload["blocked"] if payload["blocked"] else "",
        len(payload["cards"]))]
    if not payload["cards"]:
        lines.append("  （没有该复习的卡）")
    for order, card in enumerate(payload["cards"], 1):
        mark = "" if card["gradable"] else "  ⚠评不了分(%s)" % card["blockedReason"]
        when = ("逾期 %d 分钟" % card["overdueMinutes"]
                if card["kind"] == "due" else "新卡")
        lines.append("  %d. [%s] %s%s" % (
            order, when, _brief(card["frontText"]), mark))
        lines.append("      背面：%s" % _brief(card["backText"]))
        lines.append("      身份：gid=%s index=%d noteId=%s" % (
            card["gid"][:16], card["index"], card["noteId"]))
    if payload["blocked"]:
        lines.append("")
        lines.append("⚠ 标着「评不了分」的卡还没进 Anki —— Reader 自己不排期，"
                     "评分必须有真实 Anki 卡号。先确认 Anki 开着再重开那些卡片。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="取该复习的一组卡")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true",
                        help="机器可读，正反面**不截断**（语音复习要念全文）")
    parser.add_argument("--card", default=None, help="只看这个 cid")
    args = parser.parse_args()
    payload = take(args.root, args.limit)
    if args.card:
        payload["cards"] = [one for one in collect(args.root)
                            if one["cid"] == args.card]
    print(json.dumps(payload, ensure_ascii=False, indent=2)
          if args.json else render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
