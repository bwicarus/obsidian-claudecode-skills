#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review_deck — 取「现在该复习的这一组卡」，供语音复习逐张念。

    python review_deck.py                 # 紧凑清单（默认 10 张）
    python review_deck.py --limit 5
    python review_deck.py --json          # 机器可读，含正反面全文
    python review_deck.py --card <cid>    # 只看一张
    python review_deck.py --book 料理师part2      # 只复习这本
    python review_deck.py --page 10-30            # 只复习这几页上的卡
    python review_deck.py --kind due              # 只做到期的，不碰新卡

## 为什么可以不开着书（用户 2026-09-21）

> 我在外连接语音想复习，这时候没有在看任何书页，我希望可以进行指定范围的复习

出门戴耳机复习时**没有任何阅读器页面在前台**。在此之前 AI 只好临场去问阅读器，
于是拿到 `BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE`（"来源不在线"），并把它
当成"复习这件事做不了"。

它从来就不需要阅读器：这一组卡读的是 Windows 上的**复制副本**
（``replication-data/<repbookId>/document-notes.json``），是文件，不是页面。
**能不能复习与在不在看书无关** —— 这句话要写进工具说明里，否则 AI 下次还会
自己发明一条要页面在线的路（面向 AI 的说明写反比没写更糟，见 CLAUDE.md）。

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
- **范围永远连同"有哪些范围"一起返回**（``scopes``）：AI 只有一次开口机会，
  用户说"复习那本书"时它得当场报得出书名和张数，而不是再问一轮。
- **范围筛空了要说清是筛空的**，不能和"没有到期卡"长成一个样 ——
  后者让人以为复习完了，前者其实是书名写错了。
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


def book_titles(root: Path | None = None) -> dict[str, str]:
    """repbookId → 人看得懂的书名。

    ⚠ **同一个书名会对应多个 repbookId**（实测：「料理师part1 · PDF 阅读器」
    有三条）—— 同一本书从不同设备/不同副本配对过。所以按书名选范围是**并集**，
    不是歧义；报出来的匹配数可能大于 1，那是对的。
    """
    root = root or default_root()
    try:
        value = json.loads(
            (root / "replication-book-links.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for link in value.get("links") or []:
        if not isinstance(link, dict):
            continue
        rid = str(link.get("replicationBookId") or "")
        name = str(link.get("displayName") or "").strip()
        if rid and name:
            out.setdefault(rid, name)
    return out


def _card_page(item: Any, card: Any) -> int | None:
    """这张卡钉在第几页。

    两处都可能有：``item.anchor.page``（便签自身的落点）与 ``card.bind.page``
    （词锚）。取先有的那个；都没有（EPUB / 未锚定）就是 None —— 按页筛范围时
    这类卡不参与，不能猜成第 1 页。
    """
    for holder, key in ((item, "anchor"), (card, "bind")):
        node = holder.get(key) if isinstance(holder, dict) else None
        page = node.get("page") if isinstance(node, dict) else None
        if isinstance(page, int) and not isinstance(page, bool) and page > 0:
            return page
    return None


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
    titles = book_titles(root)
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
                    # 书名给人听（"复习料理师part2"），repbookId 给机器对齐。
                    # 没配对过的书没有书名 —— 如实留空，不拿 id 冒充书名。
                    "bookTitle": titles.get(book_dir.name, ""),
                    # 页码给"复习第几页到第几页"用，也是卡片钉在哪的说明。
                    "page": _card_page(item, card),
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


def parse_pages(value: Any) -> tuple[int, int] | None:
    """``"10-30"`` / ``"12"`` → (lo, hi)。写不成样子就是 None（调用方出声）。"""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split("-", 1)
    try:
        lo = int(parts[0])
        hi = int(parts[1]) if len(parts) == 2 and parts[1].strip() else lo
    except ValueError:
        return None
    if lo <= 0 or hi <= 0:
        return None
    return (lo, hi) if lo <= hi else (hi, lo)


def scopes_of(deck: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """有哪些书可以选，各有多少张。

    ⚠ 这份摘要**永远按未筛选的整副牌算**：用户说"换一本"时 AI 得知道还有哪些，
    而不是只看得见自己刚筛出来的那一本。
    """
    by_book: dict[str, dict[str, Any]] = {}
    for one in deck:
        row = by_book.setdefault(one["book"], {
            "book": one["book"], "title": one.get("bookTitle") or "",
            "total": 0, "due": 0, "new": 0, "blocked": 0,
            "pageLo": None, "pageHi": None,
        })
        row["total"] += 1
        row["due" if one["kind"] == "due" else "new"] += 1
        if not one["gradable"]:
            row["blocked"] += 1
        page = one.get("page")
        if isinstance(page, int):
            row["pageLo"] = page if row["pageLo"] is None else min(row["pageLo"], page)
            row["pageHi"] = page if row["pageHi"] is None else max(row["pageHi"], page)
    return sorted(by_book.values(), key=lambda row: (-row["due"], -row["total"]))


def select(
    deck: list[dict[str, Any]],
    *,
    book: str = "",
    pages: tuple[int, int] | None = None,
    kind: str = "all",
) -> dict[str, Any]:
    """按范围挑出要复习的那一组。返回选中的卡 + **这次范围实际命中了什么**。

    ``book`` 匹配 repbookId 全等，或书名（不分大小写）含这段文字；同名多副本
    取并集（见 :func:`book_titles`）。
    """
    book = str(book or "").strip()
    kind = str(kind or "all").strip().lower() or "all"
    matched_books: list[str] = []
    picked: list[dict[str, Any]] = []
    for one in deck:
        if book:
            needle = book.casefold()
            if one["book"] != book and needle not in (one.get("bookTitle") or "").casefold():
                continue
        if pages is not None:
            page = one.get("page")
            if not isinstance(page, int) or not (pages[0] <= page <= pages[1]):
                continue
        if kind in ("due", "new") and one["kind"] != kind:
            continue
        picked.append(one)
        if one["book"] not in matched_books:
            matched_books.append(one["book"])
    return {
        "cards": picked,
        "applied": {
            "book": book, "kind": kind,
            "pages": list(pages) if pages else None,
        },
        "matchedBooks": matched_books,
        # 筛空了和"根本没有到期卡"是两回事：前者多半是书名写错了。
        "narrowedToNothing": bool(picked == [] and deck and (book or pages or kind != "all")),
    }


def take(
    root: Path | None = None,
    limit: int = DEFAULT_LIMIT,
    *,
    book: str = "",
    pages: tuple[int, int] | None = None,
    kind: str = "all",
) -> dict[str, Any]:
    everything = collect(root)
    chosen = select(everything, book=book, pages=pages, kind=kind)
    deck = chosen["cards"]
    limit = max(1, min(int(limit), 50))
    return {
        "contract": CONTRACT,
        "atUtcMs": _now_ms(),
        # ⚠ 复习不依赖任何阅读器页面 —— 这一条要跟着数据走到 AI 面前，
        #   否则它看不到书就以为复习做不了（2026-09-21 实录）。
        "needsReaderOpen": False,
        "scope": chosen["applied"],
        "matchedBooks": chosen["matchedBooks"],
        "narrowedToNothing": chosen["narrowedToNothing"],
        "scopes": scopes_of(everything),
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
    scope = payload.get("scope") or {}
    bits = []
    if scope.get("book"):
        bits.append("书=%s（命中 %d 本副本）" % (
            scope["book"], len(payload.get("matchedBooks") or [])))
    if scope.get("pages"):
        bits.append("第 %d–%d 页" % tuple(scope["pages"]))
    if scope.get("kind") in ("due", "new"):
        bits.append("只要%s" % ("到期的" if scope["kind"] == "due" else "新卡"))
    lines = ["待复习 %d 张（到期 %d，新卡 %d%s）%s，下面列 %d 张：" % (
        payload["total"], payload["due"], payload["new"],
        "，其中 %d 张评不了分" % payload["blocked"] if payload["blocked"] else "",
        "｜范围：" + "、".join(bits) if bits else "",
        len(payload["cards"]))]
    if not payload["cards"]:
        # 筛空了和"复习完了"必须长得不一样 —— 后者让人放心，前者是写错了范围。
        lines.append("  （这个范围里没有卡；别的范围还有）"
                     if payload.get("narrowedToNothing")
                     else "  （没有该复习的卡）")
    for order, card in enumerate(payload["cards"], 1):
        mark = "" if card["gradable"] else "  ⚠评不了分(%s)" % card["blockedReason"]
        when = ("逾期 %d 分钟" % card["overdueMinutes"]
                if card["kind"] == "due" else "新卡")
        lines.append("  %d. [%s] %s%s" % (
            order, when, _brief(card["frontText"]), mark))
        lines.append("      背面：%s" % _brief(card["backText"]))
        lines.append("      身份：gid=%s index=%d noteId=%s%s" % (
            card["gid"][:16], card["index"], card["noteId"],
            "  第 %d 页" % card["page"] if card.get("page") else ""))
    rows = payload.get("scopes") or []
    if rows:
        lines.append("")
        lines.append("可选范围（他说「换一本」时直接报这些）：")
        for row in rows[:12]:
            span = ("，第 %d–%d 页" % (row["pageLo"], row["pageHi"])
                    if row["pageLo"] else "")
            lines.append("  · %s —— 到期 %d、新卡 %d%s" % (
                row["title"] or row["book"], row["due"], row["new"], span))
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
    parser.add_argument("--book", default="",
                        help="只复习这本：repbookId 全等，或书名含这段文字"
                             "（同名多副本取并集）")
    parser.add_argument("--page", default="",
                        help="只复习这几页上的卡，如 10-30 或 12"
                             "（没有页码的卡不参与）")
    parser.add_argument("--kind", default="all", choices=["all", "due", "new"],
                        help="只做到期的 / 只做新卡")
    args = parser.parse_args()
    pages = parse_pages(args.page)
    if args.page and pages is None:
        parser.error("--page 要写成 10-30 或 12")
    payload = take(args.root, args.limit,
                   book=args.book, pages=pages, kind=args.kind)
    if args.card:
        payload["cards"] = [one for one in collect(args.root)
                            if one["cid"] == args.card]
    print(json.dumps(payload, ensure_ascii=False, indent=2)
          if args.json else render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
