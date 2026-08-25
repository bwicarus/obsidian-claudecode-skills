#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""活动记录查询 —— AI 在 Windows 上贴数据回答「我什么时候在哪学了什么」。

方向（2026-08-25 用户拍板）：记录的家在 Windows 服务器端（ReaderPC），
AI 就在这台机上跑，查询零传输。本脚本是**派生/查询层**，只读两处原始层：

1. 复制命令账本（SQLite）—— 用户在 App 上的每次高亮/便签/用户页/墨迹
   改删（actor=user 的数据域命令），这就是「修改/删除的版本记录」
   （activity-ledger-design §3.2 的用户侧半边）。
2. 各书数据目录下的 activity-dwell.jsonl —— 读页停留批（含端/地点），
   由 /replication/activity 命令落盘（§3.3/§3.4 的 Windows 侧）。

用法（AI 直接跑）::

    python replication_activity.py --today            # 今天的活动摘要
    python replication_activity.py --since 7          # 最近 7 天
    python replication_activity.py --today --json     # 机器可读

⚠ 这里只读不写。原始层 append-only（采集不可重来），汇总规则改了随时重放。
"""
from __future__ import annotations

import argparse
import json
import os
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

_DOMAIN_LABELS = {
    "/pdf/api/highlights": "高亮",
    "/pdf/api/epub-highlights": "高亮",
    "/pdf/api/notes": "便签/卡片",
    "/pdf/api/userpages": "用户页",
    "/pdf/api/pdf-insert-page": "真实插入页",
    "/pdf/api/ink": "墨迹",
    "/pdf/api/epub-ink": "墨迹",
}
_METHOD_LABELS = {"POST": "新建", "PATCH": "修改", "DELETE": "删除"}


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


def _book_names(root: Path) -> dict[str, str]:
    """repbook id → 展示名（链接表的 displayName；缺表按 id 显示）。"""
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


def load_mutations(
    root: Path, since_utc_ms: int
) -> list[dict[str, Any]]:
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
        label = _DOMAIN_LABELS.get(str(url))
        if label is None or str(method) not in _METHOD_LABELS:
            continue
        try:
            body = json.loads(body_raw)
        except (TypeError, json.JSONDecodeError):
            body = {}
        out.append({
            "atUtcMs": int(received),
            "book": str(book),
            "actor": str(actor),
            "kind": label,
            "op": _METHOD_LABELS[str(method)],
            "itemId": str(body.get("id") or body.get("page") or ""),
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


def summarize(root: Path, since_days: float) -> dict[str, Any]:
    since_ms = int((time.time() - since_days * 86400) * 1000)
    names = _book_names(root)
    dwell = load_dwell(root, since_ms)
    mutations = load_mutations(root, since_ms)

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
        books.append({
            "book": names.get(book, book),
            "replicationBookId": book,
            "minutes": round(bucket["seconds"] / 60, 1),
            "places": dict(sorted(
                bucket["places"].items(), key=lambda kv: -kv[1]
            )),
            "mutations": bucket["mutations"],
        })
    return {
        "sinceDays": since_days,
        "generatedAtUtcMs": int(time.time() * 1000),
        "books": books,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None,
                        help="BWReader 数据根（默认 %%LOCALAPPDATA%%\\BWReader）")
    parser.add_argument("--today", action="store_true", help="只看今天")
    parser.add_argument("--since", type=float, default=None,
                        help="最近 N 天（可小数）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()
    days = 1.0 if args.today else (args.since if args.since else 1.0)
    report = summarize(args.root or default_root(), days)

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    print("学习活动  最近 %.1f 天" % report["sinceDays"])
    if not report["books"]:
        print("  （这段时间没有记录）")
        return 0
    for book in report["books"]:
        print("  《%s》 阅读 %.1f 分钟" % (book["book"], book["minutes"]))
        for place, secs in book["places"].items():
            print("    📍 %s  %.1f 分钟" % (place, secs / 60))
        for item in book["mutations"]:
            stamp = time.strftime(
                "%m-%d %H:%M", time.localtime(item["atUtcMs"] / 1000)
            )
            actor = "" if item["actor"] == "user" else "（%s）" % item["actor"]
            print("    %s %s%s%s %s" % (
                stamp, item["op"], item["kind"], actor,
                item["itemId"],
            ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
