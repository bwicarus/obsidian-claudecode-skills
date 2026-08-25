#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""活动记录查询 —— AI 在 Windows 上贴数据回答「我什么时候在哪学了什么」。

方向（2026-08-25 用户拍板）：记录的家在 Windows 服务器端（ReaderPC），
AI 就在这台机上跑，查询零传输。本脚本是**派生/查询层**，只读三处原始层：

1. 复制命令账本（SQLite）—— 用户在 App 上的每次高亮/便签/用户页/墨迹
   改删（actor=user 的数据域命令）。
2. 各书数据目录下的 activity-dwell.jsonl —— 读页停留批（含端/地点）。
3. 各书数据域副本（document-notes.json 等）—— 条目的**当前内容**，
   让 AI 看到的不只是编号。

## AI 读取范围（2026-08-25 用户拍板的分层设计）

时间上三层，由近及远：

- **内容窗（48 小时内）**：条目直接带内容摘要 —— AI 不用再查一跳。
- **编号窗（查询窗口内其余）**：折叠为编号 ×N；AI 需要内容时用
  `--id <编号>` 取回单条全量（当前内容 + 操作历史）。
- **窗外（默认 1 天，报告 7 天）**：不出现。要更早必须显式 `--since N`
  —— 「超过非常长的时间就退出记录」由窗口本身表达。

查询参数（AI 的筛选指令）：

- `--id <itemId>`      单条 recall：当前内容全量 + 该条目全部操作历史
- `--kind a,b`         类型筛选：highlight/note/userpage/ink/insert-page/dwell
- `--since N`/`--today` 时间范围（天，可小数）
- `--verbosity ids|summary|full`  详细程度：仅编号 / 摘要(默认) / 全部内容
- `--detail`           不折叠不截断（配窄时间窗）
- `--json`             机器可读

⚠ 这里只读不写。原始层 append-only（采集不可重来），汇总规则改了随时重放。
原始层永远不整体喂给 AI —— 纪律内建于默认参数，不靠 AI 自觉。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPLICATION_DATA_DIRECTORY_NAME = "replication-data"
ACTIVITY_FILE_NAME = "activity-dwell.jsonl"
LEDGER_FILE_NAME = "replication-command-ledger.sqlite3"
LINKS_FILE_NAME = "replication-book-links.json"

CONTENT_WINDOW_HOURS = 48        # 内容窗:近 48h 的条目直接带内容摘要
CONTENT_BRIEF_CHARS = 120        # 摘要截断
CONTENT_FULL_CHARS = 1200        # --verbosity full / --id 的内容上限

_DOMAIN_KINDS = {
    "/pdf/api/highlights": "highlight",
    "/pdf/api/epub-highlights": "highlight",
    "/pdf/api/notes": "note",
    "/pdf/api/userpages": "userpage",
    "/pdf/api/pdf-insert-page": "insert-page",
    "/pdf/api/ink": "ink",
    "/pdf/api/epub-ink": "ink",
}
_KIND_LABELS = {
    "highlight": "高亮",
    "note": "便签/卡片",
    "userpage": "用户页",
    "insert-page": "真实插入页",
    "ink": "墨迹",
    "dwell": "阅读",
}
_METHOD_LABELS = {"POST": "新建", "PATCH": "修改", "DELETE": "删除"}
_KIND_COPY_FILES = {
    "highlight": ("pdf-highlights.json", "epub-highlights.json"),
    "note": ("document-notes.json",),
    "userpage": ("user-pages.json",),
    "ink": ("pdf-ink.json", "epub-ink.json"),
}


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def _book_names(root: Path) -> dict[str, str]:
    try:
        value = json.loads(
            (root / LINKS_FILE_NAME).read_text(encoding="utf-8-sig")
        )
        return {
            str(link.get("replicationBookId")): str(
                link.get("displayName") or link.get("replicationBookId")
            )
            for link in value.get("links", [])
            if isinstance(link, dict)
        }
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _content_of_payload(kind: str, payload: dict) -> str:
    """从数据域副本条目里提取人话内容。空串 = 该类型没有文本内容。"""
    if kind == "highlight":
        parts = [str(payload.get("text") or ""), str(payload.get("note") or "")]
        return " · ".join(p for p in parts if p)
    if kind == "note":
        card = payload.get("card")
        if isinstance(card, dict):
            faces = []
            for one in (card.get("cards") or [])[:3]:
                if not isinstance(one, dict):
                    continue
                front = _strip_html(
                    str(one.get("front") or one.get("question") or ""))
                back = _strip_html(
                    str(one.get("back") or one.get("answer") or ""))
                faces.append(front + (" ⇄ " + back if back else ""))
            joined = " | ".join(f for f in faces if f)
            if joined:
                return joined
        html = payload.get("html")
        if isinstance(html, dict) and html.get("content"):
            return _strip_html(str(html["content"]))
        return str(payload.get("text") or "")
    if kind == "userpage":
        title = str(payload.get("title") or "")
        md = str(payload.get("md") or "")
        return (title + "：" if title else "") + md
    return ""


def _load_copy_items(root: Path, book: str) -> dict[str, tuple[str, dict]]:
    """book 的全部存活条目：itemId → (kind, payload)。"""
    out: dict[str, tuple[str, dict]] = {}
    base = root / REPLICATION_DATA_DIRECTORY_NAME / book
    for kind, files in _KIND_COPY_FILES.items():
        for name in files:
            try:
                value = json.loads(
                    (base / name).read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError):
                continue
            for item_id, payload in (value.get("items") or {}).items():
                if isinstance(payload, dict):
                    out[str(item_id)] = (kind, payload)
    return out


def load_mutations(root: Path, since_utc_ms: int) -> list[dict[str, Any]]:
    path = root / LEDGER_FILE_NAME
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    connection = sqlite3.connect(
        "file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True
    )
    try:
        rows = connection.execute(
            "SELECT received_at_utc_ms, replication_book_id, actor,"
            " url, method, body_json FROM commands"
            " WHERE received_at_utc_ms >= ? ORDER BY cursor",
            (since_utc_ms,),
        ).fetchall()
    finally:
        connection.close()
    for received, book, actor, url, method, body_raw in rows:
        kind = _DOMAIN_KINDS.get(str(url))
        if kind is None or str(method) not in _METHOD_LABELS:
            continue
        try:
            body = json.loads(body_raw)
        except (TypeError, json.JSONDecodeError):
            body = {}
        out.append({
            "atUtcMs": int(received),
            "book": str(book),
            "actor": str(actor),
            "kind": kind,
            "op": _METHOD_LABELS[str(method)],
            "itemId": str(body.get("id") or body.get("page") or ""),
            "_body": body,
        })
    return out


def load_dwell(root: Path, since_utc_ms: int) -> list[dict[str, Any]]:
    data_dir = root / REPLICATION_DATA_DIRECTORY_NAME
    out: list[dict[str, Any]] = []
    if not data_dir.is_dir():
        return out
    for book_dir in sorted(data_dir.iterdir()):
        path = book_dir / ACTIVITY_FILE_NAME
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(record.get("receivedAtUtcMs") or 0) < since_utc_ms:
                continue
            body = record.get("body") or {}
            if body.get("kind") != "dwell":
                continue
            secs = sum(
                int(item.get("secs") or 0)
                for item in body.get("entries") or []
                if isinstance(item, dict)
            )
            row: dict[str, Any] = {
                "atUtcMs": int(record.get("receivedAtUtcMs") or 0),
                "book": book_dir.name,
                "secs": secs,
                "client": str(body.get("client") or ""),
            }
            loc = body.get("loc")
            if isinstance(loc, dict) and loc.get("name"):
                row["place"] = str(loc["name"])
            out.append(row)
    return out


def load_reviews(root: Path, since_utc_ms: int) -> list[dict[str, Any]]:
    """activity jsonl 里的复习事件（通知 card-reviewed 自动消除的信号源）。

    形状对齐 auto_resolve_against 的期望：kind='review'，itemId 依次取
    gid / ankiCardId / key —— 匹配任一即命中，通知创建方引用哪个都行。
    """
    data_dir = root / REPLICATION_DATA_DIRECTORY_NAME
    out: list[dict[str, Any]] = []
    if not data_dir.is_dir():
        return out
    for book_dir in sorted(data_dir.iterdir()):
        path = book_dir / ACTIVITY_FILE_NAME
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(record.get("receivedAtUtcMs") or 0) < since_utc_ms:
                continue
            body = record.get("body") or {}
            if body.get("kind") != "review":
                continue
            for identity in ("gid", "ankiCardId", "key"):
                value = body.get(identity)
                if isinstance(value, str) and value:
                    out.append({
                        "atUtcMs": int(record.get("receivedAtUtcMs") or 0),
                        "book": book_dir.name,
                        "kind": "review",
                        "op": "复习",
                        "itemId": value,
                        "ease": body.get("ease"),
                    })
    return out


def _fold_mutations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一条目的连续操作折成一行（收纳/拖动噪音 ×5 → 一行）。"""
    folded: list[dict[str, Any]] = []
    for item in items:
        last = folded[-1] if folded else None
        if (
            last is not None
            and last["kind"] == item["kind"]
            and last["itemId"] == item["itemId"]
        ):
            last["count"] += 1
            last["lastAtUtcMs"] = item["atUtcMs"]
            if item["op"] not in last["ops"]:
                last["ops"].append(item["op"])
            continue
        folded.append({
            "kind": item["kind"], "itemId": item["itemId"],
            "ops": [item["op"]], "count": 1,
            "atUtcMs": item["atUtcMs"], "lastAtUtcMs": item["atUtcMs"],
            "actor": item["actor"],
        })
    return folded


def summarize(
    root: Path, since_days: float,
    *, detail: bool = False, limit: int = 20,
    kinds: set[str] | None = None,
    verbosity: str = "summary",
) -> dict[str, Any]:
    since_ms = int((time.time() - since_days * 86400) * 1000)
    content_since_ms = int(
        (time.time() - CONTENT_WINDOW_HOURS * 3600) * 1000
    )
    names = _book_names(root)
    dwell = (
        load_dwell(root, since_ms)
        if kinds is None or "dwell" in kinds else []
    )
    mutations = [
        item for item in load_mutations(root, since_ms)
        if kinds is None or item["kind"] in kinds
    ]

    per_book: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"seconds": 0, "places": defaultdict(int), "mutations": []}
    )
    for row in dwell:
        bucket = per_book[row["book"]]
        bucket["seconds"] += row["secs"]
        if row.get("place"):
            bucket["places"][row["place"]] += row["secs"]
    for item in mutations:
        per_book[item["book"]]["mutations"].append(item)

    books = []
    for book, bucket in sorted(
        per_book.items(), key=lambda kv: -kv[1]["seconds"]
    ):
        copies = _load_copy_items(root, book)
        raw_items = bucket["mutations"]
        if detail:
            shown = [dict(item) for item in raw_items]
            omitted = 0
        else:
            folded = _fold_mutations(raw_items)
            shown = folded[-max(0, limit):] if limit else folded
            omitted = len(folded) - len(shown)
        cap = (
            CONTENT_FULL_CHARS if verbosity == "full"
            else CONTENT_BRIEF_CHARS
        )
        for item in shown:
            item.pop("_body", None)
            if verbosity == "ids":
                continue
            # 内容窗:近 48h 的条目(或 full 模式下全部)带当前内容。
            recent = item.get("lastAtUtcMs", item["atUtcMs"]) \
                >= content_since_ms
            if verbosity == "full" or recent:
                entry = copies.get(item["itemId"])
                if entry is not None:
                    text = _content_of_payload(entry[0], entry[1])
                    if text:
                        item["content"] = text[:cap]
                elif "删除" in item.get("ops", [item.get("op")]):
                    item["content"] = "（已删除）"
        books.append({
            "book": names.get(book, book),
            "replicationBookId": book,
            "minutes": round(bucket["seconds"] / 60, 1),
            "places": dict(sorted(
                bucket["places"].items(), key=lambda kv: -kv[1]
            )),
            "mutations": shown,
            "mutationsOmitted": omitted,
        })
    return {
        "sinceDays": since_days,
        "generatedAtUtcMs": int(time.time() * 1000),
        "detail": detail,
        "verbosity": verbosity,
        "kinds": sorted(kinds) if kinds else None,
        "books": books,
    }


def recall(root: Path, item_id: str) -> dict[str, Any]:
    """单条 recall：当前内容全量 + 该条目在账本里的全部操作历史。"""
    names = _book_names(root)
    history = [
        item for item in load_mutations(root, 0)
        if item["itemId"] == item_id
    ]
    found_book = history[-1]["book"] if history else None
    current: tuple[str, dict] | None = None
    kind = history[-1]["kind"] if history else None
    data_dir = root / REPLICATION_DATA_DIRECTORY_NAME
    scan_books = [found_book] if found_book else (
        [p.name for p in data_dir.iterdir() if p.is_dir()]
        if data_dir.is_dir() else []
    )
    for book in scan_books:
        entry = _load_copy_items(root, book).get(item_id)
        if entry is not None:
            kind = entry[0]
            current = entry
            found_book = book
            break
    result: dict[str, Any] = {
        "itemId": item_id,
        "kind": kind,
        "book": names.get(found_book, found_book) if found_book else None,
        "alive": current is not None,
        "content": (
            _content_of_payload(current[0], current[1])[:CONTENT_FULL_CHARS]
            if current is not None else None
        ),
        "history": [
            {k: v for k, v in item.items() if k != "_body"}
            for item in history
        ],
    }
    if current is None and history:
        # 已删条目:从账本最后一条带内容的命令里抢救最后已知内容 ——
        # 账本 append-only,删除不清历史,这正是"版本记录"的价值。
        for item in reversed(history):
            text = _content_of_payload(
                kind or "note", item.get("_body") or {}
            )
            if text:
                result["lastKnownContent"] = text[:CONTENT_FULL_CHARS]
                break
    return result


def render_text(report: dict) -> str:
    """summarize 输出 → CLI/AI 看到的文本。终端/看板 AI 读取版/AI 上下文
    三处逐字同源。"""
    lines: list[str] = []
    lines.append("学习活动  最近 %.1f 天" % report["sinceDays"])
    if not report["books"]:
        lines.append("  （这段时间没有记录）")
        return "\n".join(lines)
    for book in report["books"]:
        lines.append("  《%s》 阅读 %.1f 分钟" % (book["book"], book["minutes"]))
        for place, secs in book["places"].items():
            lines.append("    📍 %s  %.1f 分钟" % (place, secs / 60))
        if book.get("mutationsOmitted"):
            lines.append("    …更早还有 %d 条（--detail 或调大 --limit 查看）"
                         % book["mutationsOmitted"])
        for item in book["mutations"]:
            stamp = time.strftime(
                "%m-%d %H:%M", time.localtime(item["atUtcMs"] / 1000)
            )
            actor = "" if item["actor"] == "user" else "（%s）" % item["actor"]
            kind_label = _KIND_LABELS.get(item["kind"], item["kind"])
            if "count" in item:
                op = "/".join(item["ops"])
                times = ("×%d" % item["count"]) if item["count"] > 1 else ""
                lines.append("    %s %s%s%s%s %s" % (
                    stamp, op, kind_label, times, actor, item["itemId"],
                ))
            else:
                lines.append("    %s %s%s%s %s" % (
                    stamp, item["op"], kind_label, actor, item["itemId"],
                ))
            content = item.get("content")
            if content:
                lines.append("        「%s」" % content)
    return "\n".join(lines)


def render_recall_text(value: dict) -> str:
    lines = ["条目 %s" % value["itemId"]]
    lines.append("  类型：%s  书：%s  状态：%s" % (
        _KIND_LABELS.get(value.get("kind") or "", value.get("kind") or "未知"),
        value.get("book") or "未知",
        "存活" if value.get("alive") else "已删除/未找到",
    ))
    if value.get("content"):
        lines.append("  当前内容：「%s」" % value["content"])
    if value.get("lastKnownContent"):
        lines.append("  最后已知内容：「%s」" % value["lastKnownContent"])
    history = value.get("history") or []
    lines.append("  操作历史（%d 条）：" % len(history))
    for item in history[-40:]:
        stamp = time.strftime(
            "%m-%d %H:%M", time.localtime(item["atUtcMs"] / 1000)
        )
        lines.append("    %s %s（%s）" % (stamp, item["op"], item["actor"]))
    if len(history) > 40:
        lines.append("    …更早 %d 条略" % (len(history) - 40))
    return "\n".join(lines)


def raw_tail(root: Path, since_days: float, limit: int = 40) -> dict:
    """元数据面板的原始记录尾巴。

    看板"元数据"层给用户看**采集层原样**（账本命令 + dwell 批），但
    有上限 —— 原始层永不整体外泄的纪律对页面同样成立；看更早的去
    终端（sqlite3 / jsonl 本体）。
    """
    since_ms = int((time.time() - since_days * 86400) * 1000)
    commands: list[dict] = []
    path = root / LEDGER_FILE_NAME
    if path.is_file():
        connection = sqlite3.connect(
            "file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True
        )
        try:
            rows = connection.execute(
                "SELECT received_at_utc_ms, replication_book_id, actor,"
                " url, method, mutation_id FROM commands"
                " WHERE received_at_utc_ms >= ? ORDER BY cursor DESC LIMIT ?",
                (since_ms, limit),
            ).fetchall()
        finally:
            connection.close()
        for received, book, actor, url, method, mutation in rows:
            commands.append({
                "atUtcMs": int(received), "book": str(book),
                "actor": str(actor), "url": str(url),
                "method": str(method), "mutationId": str(mutation),
            })
    dwell_rows = load_dwell(root, since_ms)
    dwell_rows.sort(key=lambda item: -item["atUtcMs"])
    return {
        "limit": limit,
        "commands": commands,
        "dwell": dwell_rows[:limit],
    }


def export_report(root: Path, since_days: float = 7.0) -> dict:
    """看板三层一次导出：summary(处理后) / aiText(AI 读取版) / raw(元数据)。"""
    report = summarize(root, since_days)
    report["aiText"] = render_text(report)
    report["raw"] = raw_tail(root, since_days)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None,
                        help="BWReader 数据根（默认 %%LOCALAPPDATA%%\\BWReader）")
    parser.add_argument("--today", action="store_true", help="只看今天")
    parser.add_argument("--since", type=float, default=None,
                        help="最近 N 天（可小数）")
    parser.add_argument("--id", dest="item_id", default=None,
                        help="单条 recall：当前内容全量 + 全部操作历史")
    parser.add_argument("--kind", default=None,
                        help="类型筛选，逗号分隔："
                             + "/".join(sorted(_KIND_LABELS)))
    parser.add_argument("--verbosity", default="summary",
                        choices=("ids", "summary", "full"),
                        help="详细程度：仅编号 / 摘要(默认,近48h带内容) / 全部内容")
    parser.add_argument("--detail", action="store_true",
                        help="不折叠不截断（配窄时间窗）")
    parser.add_argument("--limit", type=int, default=20,
                        help="每书最多显示的改删行数（默认 20，0=不限）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()
    root = args.root or default_root()

    if args.item_id:
        value = recall(root, args.item_id)
        print(json.dumps(value, ensure_ascii=False) if args.json
              else render_recall_text(value))
        return 0

    kinds: set[str] | None = None
    if args.kind:
        kinds = {part.strip() for part in args.kind.split(",") if part.strip()}
        unknown = kinds - set(_KIND_LABELS)
        if unknown:
            print("未知类型：%s（可用：%s）" % (
                ",".join(sorted(unknown)), "/".join(sorted(_KIND_LABELS))))
            return 2
    days = 1.0 if args.today else (args.since if args.since else 1.0)
    report = summarize(root, days, detail=args.detail, limit=args.limit,
                       kinds=kinds, verbosity=args.verbosity)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
