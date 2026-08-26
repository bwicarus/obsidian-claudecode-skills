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
USER_EXPORT_FILE_NAME = "notifications-user.json"
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


class _FileLock:
    """跨进程互斥：ReaderPC 每轮维护与 CLI(AI/系统)同时读-改-写通知表,
    没有锁就是丢失更新(2026-08-26 实锤:CLI create 成功返回、条目却被
    并发的 run_once save 覆盖蒸发)。O_EXCL 锁文件 + 有限重试;持锁者
    崩溃留下的陈锁按 mtime 超时(30s)强夺。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + 15.0
        while True:
            try:
                handle = os.open(
                    self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(handle)
                return self
            except FileExistsError:
                try:
                    if time.time() - self._path.stat().st_mtime > 30:
                        self._path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise NotificationError(
                        f"通知表锁等待超时（{self._path}）")
                time.sleep(0.05)

    def __exit__(self, *_exc: object) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


class NotificationStore:
    """open 表原地更新（状态机），resolved/expired append 进归档。
    全部写路径持文件锁 —— 见 _FileLock。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._open_path = root / OPEN_FILE_NAME
        self._archive_path = root / ARCHIVE_FILE_NAME
        self._lock_path = root / (OPEN_FILE_NAME + ".lock")

    def _locked(self) -> "_FileLock":
        self._open_path.parent.mkdir(parents=True, exist_ok=True)
        return _FileLock(self._lock_path)

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
        activate_at_ms: int | None = None,
        due_at_ms: int | None = None,
        audience: str = "ai",
    ) -> dict[str, Any]:
        with self._locked():
            if not kind or len(kind) > 40 or not title:
                raise NotificationError("通知 kind/title 非法")
            if audience not in ("ai", "user"):
                raise NotificationError("audience 必须是 ai 或 user")
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
                # 受众(2026-08-26 用户拍板):快照只给 ai 方向(待 AI 处理的);
                # 侧边栏 tab 只给 user 方向(AI 整理后投递给用户的)。两个
                # 收件箱,一张表,按 audience 分流。
                "audience": audience,
                "createdAtUtcMs": _now_ms(),
            }
            if auto_resolve is not None:
                item["autoResolve"] = auto_resolve
            if expires_at_ms is not None:
                item["expiresAtUtcMs"] = int(expires_at_ms)
            if dedupe_key:
                item["dedupeKey"] = dedupe_key[:120]
            if activate_at_ms is not None:
                # 定时生效(2026-08-25 用户:「某一天通知我倒垃圾」是持续待办,
                # 不是时间点提醒):到时之前不出现在快照/list,到时后自动可见
                # 并**保持到 resolve** —— 这正是 Codex 原生定时任务做不到的。
                item["activateAtUtcMs"] = int(activate_at_ms)
            if due_at_ms is not None:
                # 到期时刻(2026-08-27 行程场景:「14:32 的电车」):纯展示字段,
                # 不影响可见性/状态机 —— 它投影成苹果提醒的 dueDate+闹钟,
                # **到点响铃由苹果系统负责**,比 AI 轮询可靠。与 activate_at
                # 的分工:activate=何时开始出现,due=何时到点。行程通常
                # 立即可见+带 due;垃圾日待办则用 activate 当天出现。
                item["dueAtUtcMs"] = int(due_at_ms)
            items.append(item)
            self._save(items)
            return item

        # ── 消费（AI 侧的两个动作） ──

    def acknowledge(self, notification_id: str) -> dict[str, Any]:
        with self._locked():
            items = self._load()
            for item in items:
                if item["id"] == notification_id:
                    if item["state"] == "pending":
                        item["state"] = "acknowledged"
                        item["acknowledgedAtUtcMs"] = _now_ms()
                        self._save(items)
                    return item
            raise NotificationError(f"通知不存在或已入库：{notification_id}")

    def _resolve_unlocked(
        self, notification_id: str, *, by: str, note: str
    ) -> dict[str, Any]:
        """假定调用方已持锁（auto_resolve_against 循环内复用）。"""
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

    def resolve(
        self, notification_id: str, *, by: str = "ai", note: str = ""
    ) -> dict[str, Any]:
        with self._locked():
            return self._resolve_unlocked(
                notification_id, by=by, note=note)

    def update(
        self,
        notification_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        activate_at_ms: int | None = None,
        expires_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """修改一条 open 通知（标题/正文/生效/过期）。状态机不受影响。"""
        with self._locked():
            items = self._load()
            for item in items:
                if item["id"] == notification_id:
                    if title is not None:
                        item["title"] = title[:MAX_TEXT]
                    if body is not None:
                        item["body"] = body[:MAX_TEXT]
                    if activate_at_ms is not None:
                        item["activateAtUtcMs"] = int(activate_at_ms)
                    if expires_at_ms is not None:
                        item["expiresAtUtcMs"] = int(expires_at_ms)
                    item["updatedAtUtcMs"] = _now_ms()
                    self._save(items)
                    return item
            raise NotificationError(f"通知不存在或已入库：{notification_id}")

    def cancel(
        self, notification_id: str, *, by: str = "ai", note: str = ""
    ) -> dict[str, Any]:
        """删除 = 撤销入库（cancelled）。与 resolve 的区别是语义：
        resolve=目标达成；cancel=不再需要/建错了。都留档，绝不静默蒸发。"""
        with self._locked():
            items = self._load()
            for index, item in enumerate(items):
                if item["id"] == notification_id:
                    item["state"] = "cancelled"
                    item["cancelledAtUtcMs"] = _now_ms()
                    item["cancelledBy"] = by[:20]
                    if note:
                        item["cancelNote"] = note[:MAX_TEXT]
                    del items[index]
                    self._save(items)
                    self._archive(item)
                    return item
            raise NotificationError(f"通知不存在或已入库：{notification_id}")

        # ── 系统维护 ──

    def expire_due(self) -> int:
        with self._locked():
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
        with self._locked():
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
                self._resolve_unlocked(notification_id, by="auto", note="")
            return len(resolved_ids)

    def open_items(self) -> list[dict[str, Any]]:
        return self._load()

    def visible_items(self) -> list[dict[str, Any]]:
        """快照/list 看到的:已生效的 open 通知(未到 activateAt 的还蛰伏着)。"""
        now = _now_ms()
        return [
            item for item in self._load()
            if item.get("activateAtUtcMs") is None
            or item["activateAtUtcMs"] <= now
        ]

    def _export_projection(
        self, export_path: Path, audience: str
    ) -> None:
        items = [
            item for item in self.visible_items()
            if item.get("audience", "ai") == audience
        ]
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
                    "dueAtUtcMs": item.get("dueAtUtcMs"),
                }
                for item in items
            ],
        })

    def export_open(self, export_path: Path) -> None:
        """快照 join 用：**AI 方向**的已生效通知（AI 的收件箱）。"""
        self._export_projection(export_path, "ai")

    def export_user_open(self, export_path: Path) -> None:
        """侧边栏 tab 用：**用户方向**的已生效通知（用户的收件箱）。

        顺带附 review 摘要（到期/新卡数）：App 拉这份数据喂 iOS 小组件
        （2026-08-27 用户拍板做分功能小组件），复习数就搭这趟车下发 ——
        不另开通道。数据源是 Windows 副本的 count_due_cards，新鲜度 =
        run_once 周期，对小组件的分钟级刷新绰绰有余。
        """
        self._export_projection(export_path, "user")
        try:
            due, new = count_due_cards(self._root)
            value = json.loads(export_path.read_text(encoding="utf-8"))
            value["review"] = {"due": due, "new": new, "atMs": _now_ms()}
            _atomic_write_json(export_path, value)
        except Exception:
            # review 摘要是增强：算不出来时通知本体照常导出，
            # 小组件那侧对缺失字段显示"暂无数据"而不是空白。
            pass


def count_due_cards(root: Path) -> tuple[int, int]:
    """从数据域副本数到期/新卡（Windows 自主计算，不依赖 App 在线）。

    due = 卡片 `_next` 非空且已到时；new = `_next` 为空且未移除的学习卡。
    副本由两节点复制保持新鲜，App 评分后 PATCH 会把 `_next` 推过来。
    """
    import json as _json
    data_dir = root / "replication-data"
    due = 0
    new = 0
    if not data_dir.is_dir():
        return 0, 0
    now_ms = _now_ms()
    for book_dir in data_dir.iterdir():
        path = book_dir / "document-notes.json"
        try:
            value = _json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for item in (value.get("items") or {}).values():
            card = item.get("card") if isinstance(item, dict) else None
            if not isinstance(card, dict):
                continue
            for one in card.get("cards") or []:
                if not isinstance(one, dict) or one.get("_removed"):
                    continue
                next_at = one.get("_next")
                if isinstance(next_at, (int, float)) and next_at > 0:
                    # _next 语义按秒或毫秒都可能;>1e12 视为毫秒。
                    next_ms = next_at if next_at > 1e12 else next_at * 1000
                    if next_ms <= now_ms:
                        due += 1
                elif one.get("_st") in ("learn", None):
                    new += 1
    return due, new


def ensure_review_due(store: "NotificationStore", root: Path) -> dict:
    """复习到期生产者（每轮对账调用）。

    - due>0：确保当天有一条 review-due 通知（dedupe 按日，防打扰）。
    - due==0：把 open 的 review-due 通知自动入库（目标已达成）。
    """
    due, new = count_due_cards(root)
    day = time.strftime("%Y%m%d")
    if due > 0:
        store.create(
            kind="review-due",
            title="有 %d 张卡片到期待复习" % due,
            body=("另有 %d 张新卡待学习。" % new) if new else "",
            source="review-scheduler",
            dedupe_key="review-due:" + day,
            expires_at_ms=_now_ms() + 24 * 3600 * 1000,
        )
    else:
        for item in list(store.open_items()):
            if item.get("kind") == "review-due":
                store.resolve(item["id"], by="auto", note="到期清零")
    return {"due": due, "new": new}


def render_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return "当前没有待办通知。"
    lines = ["待办通知（%d 条）：" % len(items)]
    for item in items:
        stamp = time.strftime(
            "%m-%d %H:%M", time.localtime(item["createdAtUtcMs"] / 1000)
        )
        marker = "[新]" if item["state"] == "pending" else "[已获取]"
        marker += "[给用户]" if item.get("audience") == "user" else ""
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
    update = sub.add_parser("update", help="修改 open 通知（标题/正文/时间）")
    update.add_argument("id")
    update.add_argument("--title", default=None)
    update.add_argument("--body", default=None)
    update.add_argument("--activate-date", default=None)
    update.add_argument("--expires-hours", type=float, default=None)
    cancel = sub.add_parser(
        "cancel", help="撤销入库（不再需要/建错了；与 resolve 语义区分）")
    cancel.add_argument("id")
    cancel.add_argument("--note", default="")
    create = sub.add_parser("create", help="生产通知（系统或经用户授权的 AI）")
    create.add_argument("--kind", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    create.add_argument("--source", default="cli")
    create.add_argument("--dedupe-key", default=None)
    create.add_argument("--expires-hours", type=float, default=None)
    create.add_argument("--activate-date", default=None,
                        help="生效日期 YYYY-MM-DD（当天 00:00 起可见并保持到"
                             " resolve；持续待办用这个，不用定时任务）")
    create.add_argument("--activate-in-hours", type=float, default=None)
    create.add_argument("--due-at", default=None,
                        help="到期时刻 'YYYY-MM-DD HH:MM'（本地时间）。投影成"
                             "苹果提醒的到点闹钟；行程/赶车类必带")
    create.add_argument("--audience", default="ai", choices=("ai", "user"),
                        help="ai=快照(你的收件箱,默认) / user=侧边栏 tab"
                             "(整理后投递给用户)")
    create.add_argument("--auto-item", default=None,
                        help="itemId：该条目在账本出现操作即自动入库")
    create.add_argument("--auto-card", default=None,
                        help="cardId：该卡被复习即自动入库")
    args = parser.parse_args()
    store = NotificationStore(args.root or default_root())

    try:
        if args.command == "list":
            print(render_list(store.visible_items()))
        elif args.command == "ack":
            item = store.acknowledge(args.id)
            print("已获取：%s（%s）" % (item["id"], item["title"]))
        elif args.command == "resolve":
            item = store.resolve(args.id, by="ai", note=args.note)
            print("已完成入库：%s（%s）" % (item["id"], item["title"]))
        elif args.command == "update":
            activate = None
            if args.activate_date:
                activate = int(time.mktime(time.strptime(
                    args.activate_date, "%Y-%m-%d"))) * 1000
            expires = (
                _now_ms() + int(args.expires_hours * 3600 * 1000)
                if args.expires_hours else None
            )
            item = store.update(
                args.id, title=args.title, body=args.body,
                activate_at_ms=activate, expires_at_ms=expires,
            )
            print("已修改：%s（%s）" % (item["id"], item["title"]))
        elif args.command == "cancel":
            item = store.cancel(args.id, by="ai", note=args.note)
            print("已撤销入库：%s（%s）" % (item["id"], item["title"]))
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
            activate = None
            if args.activate_date:
                activate = int(time.mktime(time.strptime(
                    args.activate_date, "%Y-%m-%d"))) * 1000
            elif args.activate_in_hours:
                activate = _now_ms() + int(
                    args.activate_in_hours * 3600 * 1000)
            due = None
            if args.due_at:
                due = int(time.mktime(time.strptime(
                    args.due_at, "%Y-%m-%d %H:%M"))) * 1000
            item = store.create(
                kind=args.kind, title=args.title, body=args.body,
                source=args.source, auto_resolve=auto,
                expires_at_ms=expires, dedupe_key=args.dedupe_key,
                activate_at_ms=activate, due_at_ms=due,
                audience=args.audience,
            )
            print("已创建：%s" % item["id"])
    except NotificationError as error:
        print("错误：%s" % error)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
