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
_AUTO_TYPES = ("item-mutated", "card-reviewed", "place-arrived")

#: 每个 auto 条件必须绑到哪个字段上（place-arrived 另有查名字的校验）。
#: 条件 → (记录里的字段名, 命令行参数名)。见创建时的校验。
_AUTO_BINDINGS = {
    "item-mutated": ("itemId", "--auto-item"),
    "card-reviewed": ("cardId", "--auto-card"),
}

# 怎么送到用户面前（2026-08-29 用户要的"选择权"）。
#
# 在此之前只有"发"没有"怎么发"：一条待办建出来就同时走七条通道
# （横幅 / 苹果提醒 / 到点通知 / 系统闹钟 / 小组件 / 手表 / 侧栏 tab），
# 建它的人一个字都插不上话。于是"在公司别出声"这种要求根本无处表达。
#
# ⚠ 这里**只放已经存在的能力**。放一个不存在的档进来，等于承诺一个
# 不会生效的东西 —— 而失败是静默的：调用方以为选了就有效，用户什么也
# 收不到，链路上没有一处会报错。
#
# call 档 2026-08-29 开放：用户在开发者后台建了 Production 的 APNs 密钥，
# App 侧的 PushKit + CallKit 与 Windows 侧的推送器都已就位。
_DELIVER_MODES = (
    "auto",     # 按位置状态决定：在家可出声，在工作只静音（默认）
    "silent",   # 一定不出声：只进通知/提醒事项/侧栏
    "voice",    # 一定出声：明确要求打断时才用
    "call",     # 打一通电话：穿透静音/专注，接通后 AI 直接说
)


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader"


class NotificationError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _format_ms(value: int) -> str:
    """毫秒时刻 → 人能直接核对的本地时间。报错里印一串毫秒等于没报。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value / 1000))
    except (OverflowError, OSError, ValueError):
        return str(value)


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
        place: dict[str, Any] | None = None,
        audience: str = "ai",
        never_ends: bool = False,
        end: str | None = None,
        deliver: str = "auto",
    ) -> dict[str, Any]:
        # 「模式 + 参数」入口（用户 2026-08-29 定的形状）。
        #
        # 为什么不是三个各自独立的 kwargs：三个可以**同时为空**，而"同时
        # 为空"恰恰是最容易发生、后果最久的那种错 —— 它不报错、不显形，
        # 几天后待办堆起来才被发现。收成一个字段之后，"没选"就是一个
        # **明确的状态**，一眼看得出来。
        #
        # ⚠ 取值是**闭集**：AI 只从里面挑一个，不自由发挥。
        if end is not None:
            text = str(end).strip()
            if text == "never":
                never_ends = True
            elif text.startswith("expires:"):
                expires_at_ms = int(text.split(":", 1)[1])
            elif text.startswith("auto:"):
                condition = text.split(":", 1)[1]
                if condition not in _AUTO_TYPES:
                    raise NotificationError(
                        "end=auto: 的条件必须是 %s 之一，收到 %r"
                        % (", ".join(_AUTO_TYPES), condition))
                # ⚠ **不要在这里重建对象。** 调用方可能已经用
                # `--auto-item/--auto-card/--auto-place` 把绑定对象组装好
                # 传进来了；无条件 `= {"type": condition}` 会把它整个盖掉,
                # 表现是「参数给了、校验也过了,就是不生效」—— 这正是
                # CLAUDE.md 记的那个形态:有些站点是**重建**而不是透传,
                # 放行了还得显式搬字段。2026-08-30 实测撞上。
                if (isinstance(auto_resolve, dict)
                        and auto_resolve.get("type") == condition):
                    pass  # 已经有同类型的绑定,原样留着
                else:
                    auto_resolve = {"type": condition}
                if condition == "place-arrived" and not auto_resolve.get(
                        "place") and place:
                    # `--at-place` 兼作绑定来源:只在没有显式 `--auto-place`
                    # 时才用它,否则会把更具体的那个覆盖掉。
                    auto_resolve["place"] = place.get("name")
            else:
                raise NotificationError(
                    "end 只接受 expires:<毫秒时刻> / auto:<条件> / never，"
                    "收到 %r" % text)

        with self._locked():
            if not kind or len(kind) > 40 or not title:
                raise NotificationError("通知 kind/title 非法")
            if audience not in ("ai", "user"):
                raise NotificationError("audience 必须是 ai 或 user")
            if deliver not in _DELIVER_MODES:
                raise NotificationError(
                    "deliver 必须是 %s 之一，收到 %r。\n"
                    "  auto   按位置决定：在家可出声，在工作只静音（默认）\n"
                    "  silent 一定不出声\n"
                    "  voice  一定出声（明确要打断时才用）\n"
                    "  call   打一通电话（穿透静音/专注，接通后 AI 直接说）\n"
                    "⚠ call 只用在**必须现在让他知道**的事上。两条代价，"
                    "都不是可以调的选项：\n"
                    "  1. iOS 规定每一个 VoIP 推送都必须真的响铃 —— "
                    "它不能当'更响一点的通知'用；\n"
                    "  2. 他一按接听，**iPad 会强制切到 BWReader 前台**，"
                    "不管他当时在干什么。这是 CallKit 自 iOS 10 起的固定"
                    "行为，没有任何 API 能阻止（2026-08-30 实测确认）。\n"
                    "  也就是说这一档不只是吵，它会**打断他手上的事**。"
                    % (", ".join(_DELIVER_MODES), deliver))
            # 到点时刻的三条校验（2026-08-26 对抗式复核）：这三种组合都
            # 能建出「创建成功、到点永远不响」的条目，而链路上没有任何
            # 一处会报错 —— AI 和用户都以为设好了。
            if due_at_ms is not None:
                due_ms = int(due_at_ms)
                if due_ms <= _now_ms():
                    raise NotificationError(
                        "到点时刻已经过去了（%s）；要么给未来的时刻，"
                        "要么别带 --due-at" % _format_ms(due_ms))
                if expires_at_ms is not None and due_ms > int(expires_at_ms):
                    raise NotificationError(
                        "到点时刻晚于过期时刻：条目会在响之前就入库消失")
                if audience != "user":
                    raise NotificationError(
                        "带 --due-at 的条目必须 --audience user："
                        "设备侧的通知/闹钟只投影用户方向的条目，"
                        "ai 方向的到点时刻没有任何消费端")
            # 「创建成功、永远不会结束」—— 跟上面那三条同族的第四种
            # （2026-08-29 实锤）：08-27 和 08-28 的垃圾提醒到 08-29 还挂在
            # 提醒事项里，因为它们 autoResolve / dueAt / expiresAt **全是
            # null**。带 place 只决定"什么时候提醒"，不决定"什么时候结束"，
            # 而这两件事看起来很像，最容易被当成一件。
            #
            # 建条目的人（AI）不会注意到自己漏了终止条件，链路上也没有
            # 任何一处会报错 —— 症状要过几天、待办堆起来了才显形。
            # 所以在**创建的那一刻**就拦住，并且把可选项列出来让他挑。
            #
            # ⚠ **只管 audience=user。** 我第一版对全部条目都拦，当场挂掉
            # 12 个既有用例 —— 那说明"没有终止条件"对系统/AI 方向的条目
            # 是**正常**的：它们靠同 dedupe_key 覆盖、靠下一轮对账退场，
            # 本来就不需要谁去结束。真正会堆起来的是**要人去做**的那些，
            # 因为只有人做完了才算完，而人不会记得回来销账。
            #
            # ⚠⚠ **due_at_ms 不算终止条件。**
            # 我第一版把它算进去了 —— 错的。`due` 是"什么时候提醒"，
            # 到点之后条目照样挂着。垃圾提醒就算设了 due 也一样会堆。
            # 「什么时候提醒」和「什么时候结束」是两件事，这正是本次
            # 事故的根源（用户以为 place 就是完成条件）。
            if (
                audience == "user"
                and expires_at_ms is None
                and auto_resolve is None
                and not never_ends
            ):
                raise NotificationError(
                    "这个条目没有终止条件，会永远挂着。**选一个模式 + 参数**：\n"
                    "  end=expires:<毫秒时刻>  到这个时刻还没做完就作废\n"
                    "                          （垃圾回收日这种，过了就没意义）\n"
                    "  end=auto:<条件>         满足条件自动完成，条件取值：%s\n"
                    "  end=never               确实要一直留着（明知而选）\n"
                    "⚠ due（到点提醒）不是终止条件：到点之后条目照样挂着。\n"
                    "判断不了就**回去问用户**或先去查清楚 —— "
                    "「不知道」是合法的结果，随便填一个不是。"
                    % (", ".join(_AUTO_TYPES),))
            if auto_resolve is not None:
                if (
                    not isinstance(auto_resolve, dict)
                    or auto_resolve.get("type") not in _AUTO_TYPES
                ):
                    raise NotificationError(
                        "autoResolve.type 必须是 %s 之一" % (_AUTO_TYPES,))
                # ⚠ 每个 auto 条件都得**绑到一个具体对象**上,否则它永远
                # 不会命中。三个条件同一个道理,但 2026-08-30 之前只拦了
                # place —— 另外两个缺了绑定对象照样「已创建」,建出来的是
                # 一条**永远不会结束**的待办,而链路上没有一处会说出来。
                # 这正是这个模块开头列的那类事故,只是漏在了自己身上。
                auto_type = auto_resolve.get("type")
                if auto_type == "place-arrived":
                    # 地点名拼错 = 这条待办永远不会自动关闭,而且没有任何
                    # 地方会说出来 —— 当场拒绝比事后困惑好。
                    import replication_places
                    if not replication_places.resolve_name(
                            self._root, str(auto_resolve.get("place") or "")):
                        raise NotificationError(
                            "自动完成绑定的地点「%s」还没有命名过"
                            % auto_resolve.get("place"))
                elif auto_type in _AUTO_BINDINGS:
                    field, flag = _AUTO_BINDINGS[auto_type]
                    if not str(auto_resolve.get(field) or "").strip():
                        raise NotificationError(
                            "end=auto:%s 必须同时给 %s —— 没有它,这个条件"
                            "永远不会命中,建出来的是一条永不结束的待办。\n"
                            "不知道该填哪个就别用 auto:,改用 expires: 或"
                            "回去问用户。" % (auto_type, flag))
            items = self._load()
            if dedupe_key:
                for existing in items:
                    if existing.get("dedupeKey") != dedupe_key:
                        continue
                    # 幂等:同 key 的 open 通知只有一条。但**不能静默**把
                    # 新参数丢掉 —— 原来这里直接 return,AI 以为自己刚设
                    # 好了到点时刻,实际什么都没变、也没有任何报错。
                    changed = []
                    if due_at_ms is not None and \
                            existing.get("dueAtUtcMs") != int(due_at_ms):
                        existing["dueAtUtcMs"] = int(due_at_ms)
                        changed.append("dueAtUtcMs")
                    if activate_at_ms is not None and \
                            existing.get("activateAtUtcMs") != int(activate_at_ms):
                        existing["activateAtUtcMs"] = int(activate_at_ms)
                        changed.append("activateAtUtcMs")
                    if changed:
                        self._save(items)
                    existing["_dedupeHit"] = True
                    existing["_dedupeUpdated"] = changed
                    return existing
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
                # 怎么送。默认 auto = 交给位置状态决定。
                "deliver": deliver,
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
            if place is not None:
                # 地点触发（「到家时提醒我倒垃圾」）。真值表里**只存名字与
                # 触发方式**，坐标留到导出那刻再解析 —— 归档 jsonl 不该变成
                # 坐标副本，而且用户日后重命名/移动地点时导出会自动跟上。
                name = str(place.get("name") or "").strip()
                if not name:
                    raise NotificationError("地点绑定缺少名字")
                proximity = str(place.get("proximity") or "enter")
                if proximity not in ("enter", "leave"):
                    raise NotificationError(
                        "地点触发方式只能是 enter（到达）或 leave（离开）")
                if audience != "user":
                    raise NotificationError(
                        "带地点绑定的条目必须 --audience user")
                item["place"] = {"name": name[:80], "proximity": proximity}
                radius = place.get("radiusMeters")
                if radius is not None:
                    item["place"]["radiusMeters"] = float(radius)
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

    def _resolve_place(self, place: Any) -> dict[str, Any] | None:
        """地点名 → 带坐标的投影。名字查不到就返回 None 并**出声** ——
        静默返回 None 会让"到家提醒我"变成一条永远不会触发的普通提醒。"""
        if not isinstance(place, dict) or not place.get("name"):
            return None
        try:
            import replication_places
            found = replication_places.resolve_name(
                self._root, str(place["name"]))
        except Exception as error:
            print("警告：地点解析失败（%s）：%s" % (place.get("name"), error),
                  file=sys.stderr)
            return None
        if not found:
            print("警告：通知绑定的地点「%s」还没有命名过，地点提醒不会触发"
                  % place.get("name"), file=sys.stderr)
            return None
        out = {
            "name": found["name"],
            "lat": round(found["lat"], 6),
            "lon": round(found["lon"], 6),
            "proximity": place.get("proximity") or "enter",
        }
        # 200m 是系统里"算不算在这儿"的既有口径（ALIAS_HIT_RADIUS_M）；
        # 触发半径低于它会与 resolve_alias 自相矛盾。
        out["radiusMeters"] = float(place.get("radiusMeters") or 200)
        return out

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
                    # 怎么送。设备侧据此决定要不要出声（auto 时看位置状态）。
                    # ⚠ 老条目没有这个字段 —— 缺省 auto，跟建它时的行为一致。
                    "deliver": item.get("deliver") or "auto",
                    "createdAtUtcMs": item["createdAtUtcMs"],
                    "dueAtUtcMs": item.get("dueAtUtcMs"),
                    "place": self._resolve_place(item.get("place")),
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


def auto_resolve_on_arrival(store: "NotificationStore", root: Path) -> int:
    """把「到达某地即完成」的待办结掉（每轮对账顺路跑）。

    这是待办三种自动关闭条件里的第三种。前两种看账本（条目被改动、
    卡片被复习），这一种看位置记录。
    """
    import replication_places
    closed = 0
    for item in list(store.open_items()):
        auto = item.get("autoResolve") or {}
        if auto.get("type") != "place-arrived":
            continue
        name = str(auto.get("place") or "")
        if not name:
            continue
        arrived = replication_places.arrived_at(
            root, name, int(item.get("createdAtUtcMs") or 0))
        if arrived is None:
            continue
        try:
            store.resolve(item["id"], by="auto",
                          note="已到达「%s」" % name)
            closed += 1
        except NotificationError:
            # 并发下条目可能已被别处关掉 —— 幂等跳过,不算失败。
            pass
    return closed


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


#: 路由层的输出（写在 BWReader 根，桥的板子读它渲祈使句）。
ROUTING_FILE_NAME = "notification-routing.json"


def route_open_user_items(
    store: "NotificationStore", root: Path, runtime_dir: Path,
) -> dict[str, Any]:
    """路由层：对每条 pending 的 audience=user 通知判「现在能不能说、怎么说」。

    通知链三问的第 ② 问（用户 2026-08-30 定稿）：值不值得说在创建时定
    （deliver 档），**是不是时机由这里判**，AI 只当嘴 + 裁歧义。

    输出四种 action：
      speak  现在用语音说（板上出祈使句）
      call   打电话（deliver=call **穿透一切场合** —— 选那档时就已决定
             "必须现在知道"，这里再拦就是两层规则打架）
      hold   压着不上板。⚠ 不上板 ≠ 丢了：静默渠道（苹果提醒/横幅）在
             创建时刻已送达；现状一变（他到家/拿起设备）下一轮自动翻案
      judge  程序判不了 → 板上写明**为什么判不了**，AI 跑 judgment_basis
             后自己定（证据规则：判不出就如实记歧义，别假装能判）

    没有「下次重试时间」：电平触发，每轮对账重判，条件本身就是闹钟。
    """
    now_ms = _now_ms()

    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text("utf-8-sig"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    place = _read(runtime_dir / "current-place.json")
    status = _read(root / "readerpc-server.status.json")
    # 心跳陈旧的状态不算数 —— 「语音已连」是旧话时按不知道处理，
    # 让判定落回地点分支，而不是拿旧话当现状。
    heartbeat_fresh = bool(status) and (
        now_ms - int(status.get("updatedAtEpochMs") or 0) < 5 * 60_000)
    voice = (status or {}).get("voice") or {}
    voice_connected = heartbeat_fresh and bool(
        voice.get("readerConnected")) and bool(voice.get("captureActive"))
    context = (status or {}).get("readerContext") or {}
    reading_active = heartbeat_fresh and bool(
        str(context.get("title") or "").strip()) and (
        now_ms - int(context.get("updatedAtEpochMs") or 0) < 10 * 60_000)
    place_state = place.get("state") if place else None
    place_alias = (place or {}).get("alias")

    routes: dict[str, dict[str, str]] = {}
    for item in store.open_items():
        if item.get("audience") != "user":
            continue
        if item.get("state") != "pending":
            continue  # acked 的本来就不出祈使句
        deliver = str(item.get("deliver") or "auto")
        condition = str(((item.get("place") or {}).get("name")) or "")
        if deliver == "call":
            action, reason = "call", ""
        elif deliver == "silent":
            action, reason = "hold", "silent 档：静默渠道已送达"
        elif deliver == "voice":
            # voice = 明确要打断时才用的档 —— 建的时候就决定了要出声。
            action, reason = "speak", ""
        elif condition:
            # 「在 X 时说」：条件在，位置说了算。
            if place is None:
                action = "judge"
                reason = "这条要在「%s」时说，但从来没有过定位记录" % condition
            elif place_alias == condition:
                action, reason = "speak", ""
            else:
                action, reason = "hold", "等他到「%s」" % condition
        elif voice_connected or reading_active:
            # 正在用设备/连着语音 —— 说话够得着，且不算打扰。
            action, reason = "speak", ""
        elif place is None:
            action = "judge"
            reason = "地点没有任何记录，设备也没动静 —— 判不了在不在场"
        elif place_state == "home":
            action, reason = "speak", ""
        elif place_state == "work":
            action, reason = "hold", "在工作且没在用设备"
        else:
            action, reason = "hold", "在别处且设备没动静（语音够不着）"
        routes[str(item.get("id"))] = {"action": action, "reason": reason}

    payload = {
        "contract": "notification-routing/1",
        "atUtcMs": now_ms,
        "routes": routes,
    }
    target = root / ROUTING_FILE_NAME
    temporary = target.with_suffix(".json.tmp-%d" % os.getpid())
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return payload


def ensure_codex_voice_health(
    store: "NotificationStore", root: Path, runtime_dir: Path,
) -> None:
    """Codex 语音连续启动失败 → 一条真通知；恢复 → 自动入库。

    2026-08-30 一晚的教训落地：App 卡在崩溃页/热键僵死时，keepalive 的
    按键**按到天亮也没用**，而没有任何一处会告诉用户 —— 表现就是
    「语音怎么一直起不来」。这类失败要人看一眼（点掉错误框 / File→Quit
    彻底重启），程序自己修不了（keepalive 有意不杀用户开着的 App）。

    判据全部电平化，不带时钟进通知内容：
      连续失败 ≥4 次（前三次是正常冷启动的预算，见 C# BackoffFor）
      且此刻语音未激活 → 建通知（dedupe 按失败首见时间戳，同一轮不重报）
      语音激活 → 自动入库。
    """
    try:
        status = json.loads(
            (root / "readerpc-server.status.json").read_text("utf-8-sig"))
        voice_active = bool(
            (status.get("voice") or {}).get("codexVoiceActive"))
    except (OSError, ValueError):
        voice_active = False
    try:
        lines = (
            runtime_dir / "computer-voice-direct.failures.jsonl"
        ).read_text("utf-8-sig").splitlines()
    except OSError:
        lines = []
    now_ms = _now_ms()
    recent = []
    for line in lines[-40:]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("code") != (
            "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED"
        ):
            continue
        at = record.get("atUtc")
        if not at:
            continue
        import datetime as _dt
        try:
            t_ms = int(_dt.datetime.fromisoformat(at).timestamp() * 1000)
        except ValueError:
            continue
        # 只看最近 15 分钟 —— 历史失败不该在恢复后还阴魂不散。
        if now_ms - t_ms < 15 * 60_000:
            recent.append(t_ms)
    # ── 解除条件（2026-09-01 修）：voice_active 是「正在通话中」，语音
    # 健康但空闲时恒 False —— 拿它当解除判据等于永不解除。哨兵监控的
    # 电平就是失败流本身：**最近 15 分钟没有失败** = 问题不存在 → 全解。
    # 1-3 条失败是滞回带（正常冷启动预算内），既不建也不解。
    # ── 用户 2026-09-01 拍板：「我希望这种东西不要进入通知」──
    # 系统健康类告警不再打扰用户。这个函数从此**只清不建**：把历史遗留
    # 的 codex-voice-stuck 全部入库（一次性清扫 + 防止旧版建的残留）。
    # 卡死场景的兜底改由 ReaderPC 看门狗自愈（autoStartOnBoot 已开）；
    # 失败流仍完整记录在 failures.jsonl 供诊断，只是不再变成通知。
    del recent, now_ms, voice_active
    for item in list(store.open_items()):
        if item.get("kind") == "codex-voice-stuck":
            store.resolve(
                item["id"], by="auto",
                note="健康告警不再进通知（用户 2026-09-01 拍板）")


#: 到期卡积到多少张才值得让 AI 开口（用户 2026-08-30 定的 32）。
#: 低于它时只在慢板上摆一行**陈述句**的计数（那是桥直接读对账状态渲的，
#: 不经过这里），AI 看到不用动。
REVIEW_DUE_SPEAK_THRESHOLD = 32


def ensure_review_due(store: "NotificationStore", root: Path) -> dict:
    """复习到期生产者（每轮对账调用）。

    ## 2026-08-30 改：收件箱砍掉之后的形态

    原来 due>0 就每天建一条 audience=ai 的"原料"，等 AI 整理 —— 那条
    判断通道只有这一个生产者、而它根本不需要判断（措辞已是成品），盯板
    架构落地后更是没人会去读它。用户拍板砍掉。

    现在分两层，判断权各归其位：
      - due 的**数字**由桥直接读对账状态渲到慢板（陈述句，4 张一档），
        AI 看到不用动 —— 这里不产任何东西；
      - 积到 REVIEW_DUE_SPEAK_THRESHOLD 才建一条 **audience=user** 的
        真通知 → 慢板上变祈使句，走 ack 状态机 ——「只触发一次」不用
        另写逻辑：AI 说过一次、ack，就转"已确认"不再催。

    回落即自动入库：due 掉到阈值之下只可能是**他在复习**（这是 due 下降的
    唯一途径），目标已达成，别再让 AI 拿着过时的数字去说。回落 + 按日
    dedupe 一起兜住阈值附近的抖动：同一天重新越线不会再建。
    """
    due, new = count_due_cards(root)
    day = time.strftime("%Y%m%d")
    if due >= REVIEW_DUE_SPEAK_THRESHOLD:
        store.create(
            kind="review-due",
            title="到期待复习的卡片已积到 %d 张" % due,
            body=("另有 %d 张新卡待学习。" % new) if new else "",
            source="review-scheduler",
            audience="user",
            dedupe_key="review-due:" + day,
            end="expires:%d" % (_now_ms() + 24 * 3600 * 1000),
        )
    else:
        for item in list(store.open_items()):
            if item.get("kind") == "review-due":
                store.resolve(
                    item["id"], by="auto",
                    note="已回落到 %d 张（他在复习）" % due)
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
    create.add_argument("--at-place", default=None,
                        help="绑定到一个**已命名**的地点（如 家 / 工作地点），"
                             "到达时由系统提醒 App 触发。名字要与 "
                             "replication_places.py aliases 里的一致")
    create.add_argument("--on-leave", action="store_true",
                        help="改成离开该地点时触发（默认是到达时）")
    create.add_argument("--audience", default="ai", choices=("ai", "user"),
                        help="一律用 user（进慢板/侧边栏/苹果提醒）。"
                             "ai 这档 2026-08-30 起**休眠**：收件箱流程已"
                             "废除，写进去没有任何东西会醒来读它。默认值"
                             "仍是 ai 只为兼容旧脚本 —— 所以 user 方向"
                             "必须显式写 --audience user")
    create.add_argument("--auto-item", default=None,
                        help="itemId：该条目在账本出现操作即自动入库")
    create.add_argument("--auto-card", default=None,
                        help="cardId：该卡被复习即自动入库")
    create.add_argument("--auto-place", default=None,
                        help="地点名：**到达**该地即自动完成（判的是到达"
                             "这个事件，不是此刻在不在，所以在目的地创建"
                             "也不会当场自我了断）")
    # 「模式 + 参数」—— 用户 2026-08-29 定的形状：AI 只从闭集里挑一个，
    # 不自由发挥。给 audience=user 的条目必填（否则会永远挂着）。
    create.add_argument(
        "--end", default=None,
        help="终止方式，三选一：expires:<毫秒时刻>（到点作废）/ "
             "auto:<item-mutated|card-reviewed|place-arrived>（条件达成"
             "自动完成）/ never（一直留着，明知而选）。"
             "⚠ 用 auto: 就**必须**同时给绑定对象："
             "item-mutated→--auto-item / card-reviewed→--auto-card / "
             "place-arrived→--auto-place。没绑对象的条件永远不会命中，"
             "建出来的是一条永不结束的待办。填不出那个 id 就别用 auto:，"
             "改用 expires: 或回去问用户 —— 猜一个 id 比不写更糟，"
             "它让条目看起来是有终点的。"
             "⚠ --due-at 不是终止条件：到点之后条目照样挂着。"
             "判断不了就回来问用户 —— 「不知道」是合法结果，随便填不是")
    create.add_argument(
        "--deliver", default="auto", choices=_DELIVER_MODES,
        help="怎么送到用户面前：auto=按位置决定（在家可出声，在工作只静音，"
             "默认）/ silent=一定不出声 / voice=一定出声 / "
             "call=打一通真电话（穿透静音与专注模式，接通后你直接开口说）。"
             "⚠ call 的代价有两条，都不是可以调的选项：① iOS 规定每个 VoIP "
             "推送都必须真的响铃，它不能当'更响一点的通知'用；② 他一按接听，"
             "iPad 会强制切到 BWReader 前台，不管他当时在干什么（CallKit 自 "
             "iOS 10 起的固定行为，没有 API 能阻止，2026-08-30 实测确认）。"
             "所以这一档不只是吵，它会打断他手上的事 —— "
             "只用在必须现在让他知道的事上")
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
            elif args.auto_place:
                auto = {"type": "place-arrived", "place": args.auto_place}
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
            place = None
            if args.at_place:
                place = {
                    "name": args.at_place,
                    "proximity": "leave" if args.on_leave else "enter",
                }
            item = store.create(
                kind=args.kind, title=args.title, body=args.body,
                source=args.source, auto_resolve=auto,
                expires_at_ms=expires, dedupe_key=args.dedupe_key,
                activate_at_ms=activate, due_at_ms=due, place=place,
                audience=args.audience, end=args.end, deliver=args.deliver,
            )
            if item.get("_dedupeHit"):
                updated = item.get("_dedupeUpdated") or []
                print("已存在同 key 的条目：%s（%s）" % (
                    item["id"],
                    ("已更新 " + "、".join(updated)) if updated else "未改动"))
            else:
                print("已创建：%s" % item["id"])
            if item.get("place"):
                # 回显并当场验证地点名能不能解析出坐标 —— 拼错名字时
                # 立刻看得见,而不是等到该响的那天发现没响。
                bound = store._resolve_place(item["place"])
                print("  地点：%s（%s）%s" % (
                    item["place"]["name"],
                    "到达时" if item["place"]["proximity"] == "enter"
                    else "离开时",
                    "" if bound else " ⚠ 这个名字还没命名过，不会触发"))
            if item.get("dueAtUtcMs"):
                # 回显给 AI 与用户核对：--due-at 按**本机本地时间**解析,
                # 印出来才看得出是不是差了时区或写错了日期。
                print("  到点：%s" % _format_ms(int(item["dueAtUtcMs"])))
    except NotificationError as error:
        print("错误：%s" % error)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
