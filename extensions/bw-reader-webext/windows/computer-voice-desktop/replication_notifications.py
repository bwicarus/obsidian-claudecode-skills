#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知系统 —— 账本的姊妹：账本答「发生了什么」，这里答「该做什么」。

设计（2026-08-25 用户拍板）：

- **状态机**：pending（生成）→ acknowledged（AI 已获取，显式 ack）→
  resolved（完成入库，by=auto/ai/user）。过期走 expired。
- **AI 只消费**：通知由系统生产（复习到期/定时任务/异常）；AI 的写
  操作只有 ack 和 resolve —— 读到快照里的 [新] 通知先 ack，判断目标
  完成后 resolve。生产入口 create 只给系统侧调。
- **自动消除**：通知可带 autoResolve 条件；检测器挂在 ReaderPC 对账
  循环（它每轮都在扫账本，顺路对照）。第一期支持：
    - item-mutated: {itemId, op?}  账本里出现该条目的操作即命中
    - card-reviewed: {cardId}      复习事件进账本后生效（通道随第 2 步）
- **入库**：resolved/expired 的通知 append 进归档 jsonl —— 通知生命
  周期本身就是活动记录的一部分（"我上周完成了哪些提醒"可查）。
- 快照注入由桥完成：本模块每轮把 open 通知导出 runtime 目录的
  notifications-open.json，桥合并进快照 JSON 的 `notifications` 节。

与 Codex 原生定时任务的分工（写进 AGENTS 的准则）：
**事实驱动、需要跨会话存活和审计的走这里；对话驱动、短时效的走
Codex 原生。**
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

OPEN_FILE_NAME = "notifications.json"
ARCHIVE_FILE_NAME = "notifications-archive.jsonl"
EXPORT_FILE_NAME = "notifications-open.json"
OPEN_CONTRACT = "reader-notifications/1"
MAX_OPEN = 50
MAX_TEXT = 400
_STATES = ("pending", "acknowledged")
_AUTO_TYPES = ("item-mutated", "card-reviewed")


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


class NotificationError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class NotificationStore:
    """open 表原地更新（状态机），resolved/expired append 进归档。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._open_path = root / OPEN_FILE_NAME
        self._archive_path = root / ARCHIVE_FILE_NAME

    def _load(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self._open_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as error:
            raise NotificationError(
                f"通知表 JSON 损坏（{self._open_path}），拒绝静默重置：{error}"
            ) from error
        if (
            not isinstance(value, dict)
            or value.get("contract") != OPEN_CONTRACT
            or not isinstance(value.get("items"), list)
        ):
            raise NotificationError("通知表 contract 不符")
        return value["items"]

    def _save(self, items: list[dict[str, Any]]) -> None:
        _atomic_write_json(self._open_path, {
            "contract": OPEN_CONTRACT,
            "items": items,
        })

    def _archive(self, item: dict[str, Any]) -> None:
        self._archive_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._archive_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    # ── 生产（系统侧） ──

    def create(
        self,
        *,
        kind: str,
        title: str,
        body: str = "",
        source: str = "system",
        auto_resolve: dict[str, Any] | None = None,
        expires_at_ms: int | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        if not kind or len(kind) > 40 or not title:
            raise NotificationError("通知 kind/title 非法")
        if auto_resolve is not None:
            if (
                not isinstance(auto_resolve, dict)
                or auto_resolve.get("type") not in _AUTO_TYPES
            ):
                raise NotificationError(
                    "autoResolve.type 必须是 %s 之一" % (_AUTO_TYPES,))
        items = self._load()
        if dedupe_key:
            for existing in items:
                if existing.get("dedupeKey") == dedupe_key:
                    return existing   # 幂等:同 key 的 open 通知只有一条
        if len(items) >= MAX_OPEN:
            raise NotificationError(f"open 通知已达上限 {MAX_OPEN} 条")
        item = {
            "id": "ntf-" + secrets.token_hex(6),
            "kind": kind[:40],
            "title": title[:MAX_TEXT],
            "body": body[:MAX_TEXT],
            "source": source[:40],
            "state": "pending",
            "createdAtUtcMs": _now_ms(),
        }
        if auto_resolve is not None:
            item["autoResolve"] = auto_resolve
        if expires_at_ms is not None:
            item["expiresAtUtcMs"] = int(expires_at_ms)
        if dedupe_key:
            item["dedupeKey"] = dedupe_key[:120]
        items.append(item)
        self._save(items)
        return item

    # ── 消费（AI 侧的两个动作） ──

    def acknowledge(self, notification_id: str) -> dict[str, Any]:
        items = self._load()
        for item in items:
            if item["id"] == notification_id:
                if item["state"] == "pending":
                    item["state"] = "acknowledged"
                    item["acknowledgedAtUtcMs"] = _now_ms()
                    self._save(items)
                return item
        raise NotificationError(f"通知不存在或已入库：{notification_id}")

    def resolve(
        self, notification_id: str, *, by: str = "ai", note: str = ""
    ) -> dict[str, Any]:
        items = self._load()
        for index, item in enumerate(items):
            if item["id"] == notification_id:
                item["state"] = "resolved"
                item["resolvedAtUtcMs"] = _now_ms()
                item["resolvedBy"] = by[:20]
                if note:
                    item["resolutionNote"] = note[:MAX_TEXT]
                del items[index]
                self._save(items)
                self._archive(item)
                return item
        raise NotificationError(f"通知不存在或已入库：{notification_id}")

    # ── 系统维护 ──

    def expire_due(self) -> int:
        now = _now_ms()
        items = self._load()
        keep: list[dict[str, Any]] = []
        expired = 0
        for item in items:
            if item.get("expiresAtUtcMs") and item["expiresAtUtcMs"] < now:
                item["state"] = "expired"
                item["expiredAtUtcMs"] = now
                self._archive(item)
                expired += 1
            else:
                keep.append(item)
        if expired:
            self._save(keep)
        return expired

    def auto_resolve_against(
        self, mutations: list[dict[str, Any]]
    ) -> int:
        """对照一批账本记录（load_mutations 形状），命中条件的通知自动入库。"""
        items = self._load()
        resolved_ids: list[str] = []
        for item in items:
            condition = item.get("autoResolve")
            if not isinstance(condition, dict):
                continue
            ctype = condition.get("type")
            hit = False
            if ctype == "item-mutated":
                want_id = str(condition.get("itemId") or "")
                want_op = condition.get("op")
                hit = any(
                    m.get("itemId") == want_id
                    and (want_op is None or m.get("op") == want_op)
                    for m in mutations
                )
            elif ctype == "card-reviewed":
                want_card = str(condition.get("cardId") or "")
                hit = any(
                    m.get("kind") == "review"
                    and m.get("itemId") == want_card
                    for m in mutations
                )
            if hit:
                resolved_ids.append(item["id"])
        for notification_id in resolved_ids:
            self.resolve(notification_id, by="auto")
        return len(resolved_ids)

    def open_items(self) -> list[dict[str, Any]]:
        return self._load()

    def export_open(self, export_path: Path) -> None:
        """给桥的快照 join 用：open 通知的只读投影（上限内全量）。"""
        items = self._load()
        _atomic_write_json(export_path, {
            "contract": OPEN_CONTRACT,
            "exportedAtUtcMs": _now_ms(),
            "items": [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "title": item["title"],
                    "body": item.get("body") or "",
                    "state": item["state"],
                    "createdAtUtcMs": item["createdAtUtcMs"],
                }
                for item in items
            ],
        })


def render_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return "当前没有待办通知。"
    lines = ["待办通知（%d 条）：" % len(items)]
    for item in items:
        stamp = time.strftime(
            "%m-%d %H:%M", time.localtime(item["createdAtUtcMs"] / 1000)
        )
        marker = "[新]" if item["state"] == "pending" else "[已获取]"
        lines.append("  %s %s %s  %s（%s）" % (
            marker, item["id"], stamp, item["title"], item["kind"],
        ))
        if item.get("body"):
            lines.append("      %s" % item["body"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="看 open 通知（AI 用）")
    ack = sub.add_parser("ack", help="标记已获取（AI 读到 [新] 通知先调这个）")
    ack.add_argument("id")
    resolve = sub.add_parser(
        "resolve", help="完成入库（AI 判断目标达成后调）")
    resolve.add_argument("id")
    resolve.add_argument("--note", default="", help="完成说明（入库留档）")
    create = sub.add_parser("create", help="生产通知（系统侧；AI 不用这个）")
    create.add_argument("--kind", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    create.add_argument("--source", default="cli")
    create.add_argument("--dedupe-key", default=None)
    create.add_argument("--expires-hours", type=float, default=None)
    create.add_argument("--auto-item", default=None,
                        help="itemId：该条目在账本出现操作即自动入库")
    create.add_argument("--auto-card", default=None,
                        help="cardId：该卡被复习即自动入库")
    args = parser.parse_args()
    store = NotificationStore(args.root or default_root())

    try:
        if args.command == "list":
            print(render_list(store.open_items()))
        elif args.command == "ack":
            item = store.acknowledge(args.id)
            print("已获取：%s（%s）" % (item["id"], item["title"]))
        elif args.command == "resolve":
            item = store.resolve(args.id, by="ai", note=args.note)
            print("已完成入库：%s（%s）" % (item["id"], item["title"]))
        elif args.command == "create":
            auto = None
            if args.auto_item:
                auto = {"type": "item-mutated", "itemId": args.auto_item}
            elif args.auto_card:
                auto = {"type": "card-reviewed", "cardId": args.auto_card}
            expires = (
                _now_ms() + int(args.expires_hours * 3600 * 1000)
                if args.expires_hours else None
            )
            item = store.create(
                kind=args.kind, title=args.title, body=args.body,
                source=args.source, auto_resolve=auto,
                expires_at_ms=expires, dedupe_key=args.dedupe_key,
            )
            print("已创建：%s" % item["id"])
    except NotificationError as error:
        print("错误：%s" % error)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
