from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from replication_notifications import (  # noqa: E402
    NotificationError,
    NotificationStore,
    _now_ms,
)


class NotificationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_state_machine_and_archive(self) -> None:
        item = self.store.create(kind="review-due", title="该复习了")
        self.assertEqual(item["state"], "pending")
        acked = self.store.acknowledge(item["id"])
        self.assertEqual(acked["state"], "acknowledged")
        resolved = self.store.resolve(
            item["id"], by="ai", note="用户完成了目标")
        self.assertEqual(resolved["resolvedBy"], "ai")
        self.assertEqual(self.store.open_items(), [], "resolve 即离开 open 表")
        lines = (self.root / "notifications-archive.jsonl") \
            .read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1, "入库归档一条")
        self.assertEqual(json.loads(lines[0])["resolutionNote"],
                         "用户完成了目标")

    def test_dedupe_key_is_idempotent(self) -> None:
        first = self.store.create(
            kind="review-due", title="该复习了", dedupe_key="review-due")
        second = self.store.create(
            kind="review-due", title="该复习了(重复)", dedupe_key="review-due")
        self.assertEqual(first["id"], second["id"],
                         "同 dedupe key 的 open 通知只有一条")

    def test_auto_resolve_item_mutated(self) -> None:
        item = self.store.create(
            kind="fix-card", title="修这张卡",
            auto_resolve={"type": "item-mutated", "itemId": "n123",
                          "op": "修改"})
        untouched = self.store.create(kind="other", title="别的事")
        count = self.store.auto_resolve_against([
            {"kind": "note", "itemId": "n999", "op": "修改"},
            {"kind": "note", "itemId": "n123", "op": "修改"},
        ])
        self.assertEqual(count, 1)
        remaining = [i["id"] for i in self.store.open_items()]
        self.assertEqual(remaining, [untouched["id"]])
        archived = json.loads(
            (self.root / "notifications-archive.jsonl")
            .read_text("utf-8").splitlines()[0])
        self.assertEqual(archived["resolvedBy"], "auto")
        self.assertEqual(archived["id"], item["id"])

    def test_auto_resolve_card_reviewed(self) -> None:
        self.store.create(
            kind="review-card", title="复习这张卡",
            auto_resolve={"type": "card-reviewed", "cardId": "anki_card_9"})
        miss = self.store.auto_resolve_against([
            {"kind": "note", "itemId": "anki_card_9", "op": "修改"},
        ])
        self.assertEqual(miss, 0, "普通改动不算复习")
        hit = self.store.auto_resolve_against([
            {"kind": "review", "itemId": "anki_card_9", "op": "复习"},
        ])
        self.assertEqual(hit, 1)

    def test_expire_and_export(self) -> None:
        self.store.create(kind="a", title="快过期",
                          expires_at_ms=int(time.time() * 1000) - 1)
        keep = self.store.create(kind="b", title="留着")
        self.assertEqual(self.store.expire_due(), 1)
        export = self.root / "runtime" / "notifications-open.json"
        self.store.export_open(export)
        value = json.loads(export.read_text("utf-8"))
        self.assertEqual([i["id"] for i in value["items"]], [keep["id"]])
        self.assertNotIn("dedupeKey", value["items"][0],
                         "投影只带展示字段")

    def test_bad_auto_type_rejected(self) -> None:
        with self.assertRaises(NotificationError):
            self.store.create(kind="x", title="y",
                              auto_resolve={"type": "nonsense"})


if __name__ == "__main__":
    unittest.main()


class ReviewProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)
        self.book = self.root / "replication-data" / ("repbook-" + "a" * 32)
        self.book.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_notes(self, cards: list[dict]) -> None:
        (self.book / "document-notes.json").write_text(json.dumps({
            "contract": "replication-book-data/1",
            "items": {"n1": {"id": "n1", "card": {"cards": cards}}},
            "tombstones": {}, "order": ["n1"],
        }, ensure_ascii=False), "utf-8")

    def test_due_creates_once_per_day_and_clears_on_zero(self) -> None:
        from replication_notifications import ensure_review_due
        now_ms = int(time.time() * 1000)
        self._write_notes([
            {"front": "Q", "_next": now_ms - 1000, "_st": "review"},
            {"front": "Q2", "_next": None, "_st": "learn"},
        ])
        first = ensure_review_due(self.store, self.root)
        self.assertEqual(first, {"due": 1, "new": 1})
        again = ensure_review_due(self.store, self.root)
        self.assertEqual(len(self.store.open_items()), 1,
                         "dedupe 按日,一天最多一条")
        self.assertEqual(again["due"], 1)
        # 复习完(副本 _next 推到未来) → due 清零 → 自动入库
        self._write_notes([
            {"front": "Q", "_next": now_ms + 86400_000, "_st": "review"},
        ])
        cleared = ensure_review_due(self.store, self.root)
        self.assertEqual(cleared["due"], 0)
        self.assertEqual(self.store.open_items(), [])
        archived = [json.loads(x) for x in
                    (self.root / "notifications-archive.jsonl")
                    .read_text("utf-8").splitlines()]
        self.assertEqual(archived[-1]["resolvedBy"], "auto")
        self.assertEqual(archived[-1]["resolutionNote"], "到期清零")

    def test_seconds_epoch_next_is_also_understood(self) -> None:
        from replication_notifications import count_due_cards
        self._write_notes([
            {"front": "Q", "_next": int(time.time()) - 10, "_st": "review"},
        ])
        due, _new = count_due_cards(self.root)
        self.assertEqual(due, 1, "秒级 _next 也判到期")


class StandingTodoTests(unittest.TestCase):
    """持续待办(2026-08-25 用户洞察):定时生效后保持到 resolve。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_dormant_until_activation_then_persists(self) -> None:
        future = int(time.time() * 1000) + 3600_000
        item = self.store.create(
            kind="user-todo", title="倒垃圾",
            source="ai-on-user-request", activate_at_ms=future)
        self.assertEqual(self.store.visible_items(), [],
                         "生效前不可见(不进快照/list)")
        self.assertEqual(len(self.store.open_items()), 1,
                         "但在 open 表里活着(维护循环能看到)")
        export = self.root / "notifications-open.json"
        self.store.export_open(export)
        self.assertEqual(
            json.loads(export.read_text("utf-8"))["items"], [],
            "导出同样过滤蛰伏项")
        # 把生效时间拨到过去 → 可见,且没有任何自动消失:保持到 resolve
        items = self.store.open_items()
        items[0]["activateAtUtcMs"] = int(time.time() * 1000) - 1
        self.store._save(items)
        self.assertEqual(len(self.store.visible_items()), 1)
        self.assertEqual(self.store.expire_due(), 0,
                         "无 expires 的持续待办永不自动过期")
        self.store.resolve(item["id"], by="ai", note="用户说倒完了")
        self.assertEqual(self.store.visible_items(), [])


class NotificationCommandTests(unittest.TestCase):
    """侧边栏通知 tab 的操作经复制命令流回 Windows。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        from replication_apply import (
            ReplicationCommandApplier, ReplicationDataStore,
        )
        from replication_command_ledger import ReplicationCommandLedger
        self.ledger = ReplicationCommandLedger(
            self.root / "replication-command-ledger.sqlite3")
        self.applier = ReplicationCommandApplier(
            self.ledger,
            ReplicationDataStore(self.root / "replication-data"),
            self.root / "state.json", self.root / "dead-letter.jsonl",
        )
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def _cmd(self, suffix: str, body: dict) -> dict:
        return {
            "contract": "replication-command/1",
            "deviceId": "native-app-v1-" + "a" * 32,
            "replicationBookId": "repbook-" + "c" * 32,
            "actor": "user",
            "op": {"mutationId": "mut-v2-" + suffix * 32,
                   "url": "/replication/notification", "method": "POST",
                   "body": body},
        }

    def test_user_resolve_via_command_stream(self) -> None:
        item = self.store.create(kind="user-todo", title="倒垃圾")
        self.ledger.append(self._cmd("1", {
            "action": "resolve", "id": item["id"], "note": "倒完了"}))
        report = self.applier.apply_pending()
        self.assertEqual(report["deadLetters"], [])
        self.assertEqual(self.store.open_items(), [])
        archived = json.loads(
            (self.root / "notifications-archive.jsonl")
            .read_text("utf-8").splitlines()[-1])
        self.assertEqual(archived["resolvedBy"], "user",
                         "App 侧操作 by=user —— 三来源齐全")
        self.assertEqual(archived["resolutionNote"], "倒完了")

    def test_replayed_action_on_gone_notification_is_idempotent(self) -> None:
        self.ledger.append(self._cmd("2", {
            "action": "ack", "id": "ntf-000000000000"}))
        report = self.applier.apply_pending()
        self.assertEqual(report["deadLetters"], [],
                         "已消失的通知按幂等成功,不死信")


class AudienceSplitTests(unittest.TestCase):
    """两个收件箱(2026-08-26):快照=AI 方向;侧边栏 tab=用户方向。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_projections_split_by_audience(self) -> None:
        ai = self.store.create(kind="review-due", title="该提醒复习了")
        user = self.store.create(kind="task-report", title="X 已更新",
                                 audience="user")
        snap = self.root / "notifications-open.json"
        tab = self.root / "notifications-user.json"
        self.store.export_open(snap)
        self.store.export_user_open(tab)
        snap_ids = [i["id"] for i in
                    json.loads(snap.read_text("utf-8"))["items"]]
        tab_ids = [i["id"] for i in
                   json.loads(tab.read_text("utf-8"))["items"]]
        self.assertEqual(snap_ids, [ai["id"]], "快照只装 AI 方向")
        self.assertEqual(tab_ids, [user["id"]], "tab 只装用户方向")

    def test_bad_audience_rejected(self) -> None:
        with self.assertRaises(NotificationError):
            self.store.create(kind="x", title="y", audience="everyone")


class DueAtTests(unittest.TestCase):
    """到点时刻(2026-08-26):它要穿过四个进程(Python→C#桥→JS→Swift)才能
    变成设备上的闹钟,任何一跳把它丢了都表现为「提醒到点不响」且沿途
    无人报错 —— 所以在源头锁住它的存在与形状。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_due_at_persisted_and_exported(self) -> None:
        due = 1_800_000_000_000
        item = self.store.create(
            kind="trip", title="14:20 出门", audience="user",
            due_at_ms=due)
        self.assertEqual(item["dueAtUtcMs"], due, "落库要带到点时刻")
        tab = self.root / "notifications-user.json"
        self.store.export_user_open(tab)
        exported = json.loads(tab.read_text("utf-8"))["items"][0]
        self.assertEqual(
            exported["dueAtUtcMs"], due,
            "导出必须显式搬这个字段 —— 只在落库存下、导出漏搬,"
            "表现是「校验全过就是不响」")

    def test_missing_due_at_exports_as_null(self) -> None:
        self.store.create(kind="user-todo", title="倒垃圾", audience="user")
        tab = self.root / "notifications-user.json"
        self.store.export_user_open(tab)
        exported = json.loads(tab.read_text("utf-8"))["items"][0]
        self.assertIsNone(
            exported["dueAtUtcMs"],
            "没有到点时刻的条目要显式给 null,不是缺键 —— "
            "缺键会让下游的严格字段校验整条拒收")

    def test_activate_and_due_are_independent(self) -> None:
        """两个时间字段的分工:activate=何时开始出现,due=何时到点。
        混用会让「垃圾日当天才出现的待办」变成「到点响一次就完」。"""
        item = self.store.create(
            kind="user-todo", title="倒垃圾", audience="user",
            activate_at_ms=1_700_000_000_000,
            due_at_ms=1_800_000_000_000)
        self.assertEqual(item["activateAtUtcMs"], 1_700_000_000_000)
        self.assertEqual(item["dueAtUtcMs"], 1_800_000_000_000)


class DueAtGuardTests(unittest.TestCase):
    """create 的三条校验(2026-08-26 对抗式复核):这三种组合都能建出
    「创建成功、到点永远不响」的条目,而链路上没有任何一处会报错。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_past_due_rejected(self) -> None:
        with self.assertRaises(NotificationError) as caught:
            self.store.create(kind="trip", title="早就过了", audience="user",
                              due_at_ms=1_000_000_000_000)
        self.assertIn("已经过去", str(caught.exception))

    def test_due_after_expiry_rejected(self) -> None:
        """条目会在响之前就入库消失 —— 比不设更糟,因为看着是设了的。"""
        soon = _now_ms() + 60_000
        later = _now_ms() + 600_000
        with self.assertRaises(NotificationError):
            self.store.create(kind="trip", title="过期早于到点",
                              audience="user",
                              due_at_ms=later, expires_at_ms=soon)

    def test_ai_audience_with_due_rejected(self) -> None:
        """设备侧只投影用户方向的条目;ai 方向的到点时刻没有消费端。"""
        with self.assertRaises(NotificationError) as caught:
            self.store.create(kind="trip", title="方向错了", audience="ai",
                              due_at_ms=_now_ms() + 600_000)
        self.assertIn("audience user", str(caught.exception))

    def test_dedupe_hit_applies_new_due_and_reports(self) -> None:
        """同 key 命中时不能静默丢掉新参数:AI 以为自己刚设好了到点时刻,
        实际什么都没变、也没有任何报错。"""
        first = self.store.create(
            kind="trip", title="回家", audience="user",
            dedupe_key="trip:home", due_at_ms=_now_ms() + 600_000)
        target = _now_ms() + 1_200_000
        again = self.store.create(
            kind="trip", title="回家", audience="user",
            dedupe_key="trip:home", due_at_ms=target)
        self.assertEqual(again["id"], first["id"], "仍然幂等,不新建")
        self.assertEqual(again["dueAtUtcMs"], target, "新的到点时刻要生效")
        self.assertIn("dueAtUtcMs", again.get("_dedupeUpdated") or [],
                      "并且要报告改了什么")


class PlaceBindingTests(unittest.TestCase):
    """地点触发(2026-08-26):真值表只存名字+触发方式,坐标在导出那刻解析。
    这样归档不会变成坐标副本,用户日后重命名地点时导出会自动跟上。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _name_place(self, name: str, lat: float, lon: float) -> None:
        import replication_places
        replication_places.save_alias(self.root, name, lat, lon)

    def test_place_stored_by_name_not_coordinates(self) -> None:
        item = self.store.create(
            kind="user-todo", title="倒垃圾", audience="user",
            place={"name": "家", "proximity": "enter"})
        self.assertEqual(item["place"], {"name": "家", "proximity": "enter"},
                         "真值表里不该有坐标")

    def test_export_resolves_coordinates(self) -> None:
        self._name_place("家", 35.6586, 139.7454)
        self.store.create(kind="user-todo", title="倒垃圾", audience="user",
                          place={"name": "家", "proximity": "enter"})
        out = self.root / "notifications-user.json"
        self.store.export_user_open(out)
        place = json.loads(out.read_text("utf-8"))["items"][0]["place"]
        self.assertEqual(place["name"], "家")
        self.assertAlmostEqual(place["lat"], 35.6586, places=4)
        self.assertAlmostEqual(place["lon"], 139.7454, places=4)
        self.assertEqual(place["proximity"], "enter")
        self.assertEqual(place["radiusMeters"], 200,
                         "默认半径与别名命中半径同口径,否则自相矛盾")

    def test_unknown_place_exports_null(self) -> None:
        """名字查不到时导出 null（并在 stderr 出声）—— 而不是导出一个
        没有坐标的半截对象让下游去猜。"""
        self.store.create(kind="user-todo", title="买菜", audience="user",
                          place={"name": "还没命名过的地方",
                                 "proximity": "enter"})
        out = self.root / "notifications-user.json"
        self.store.export_user_open(out)
        self.assertIsNone(
            json.loads(out.read_text("utf-8"))["items"][0]["place"])

    def test_place_requires_user_audience(self) -> None:
        with self.assertRaises(NotificationError):
            self.store.create(kind="user-todo", title="方向错了",
                              audience="ai",
                              place={"name": "家", "proximity": "enter"})

    def test_bad_proximity_rejected(self) -> None:
        with self.assertRaises(NotificationError):
            self.store.create(kind="user-todo", title="触发方式错了",
                              audience="user",
                              place={"name": "家", "proximity": "nearby"})

    def test_rename_place_follows_on_next_export(self) -> None:
        """坐标不进真值表的好处:地点被移动/重命名后,已有的提醒自动跟上。"""
        self._name_place("家", 35.0, 139.0)
        self.store.create(kind="user-todo", title="倒垃圾", audience="user",
                          place={"name": "家", "proximity": "enter"})
        self._name_place("家", 36.0, 140.0)   # 用户重新标定了「家」
        out = self.root / "notifications-user.json"
        self.store.export_user_open(out)
        place = json.loads(out.read_text("utf-8"))["items"][0]["place"]
        self.assertAlmostEqual(place["lat"], 36.0, places=4,
                               msg="导出要用当前坐标,不是创建时的快照")
