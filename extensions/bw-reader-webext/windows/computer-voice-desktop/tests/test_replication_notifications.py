from __future__ import annotations

import json
from pathlib import Path
import shutil
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

    def test_due_speaks_only_at_threshold(self) -> None:
        """2026-08-30 砍收件箱后的契约：

        - 阈值以下**一条通知都不建**（数字由桥直接渲到慢板，陈述句）；
        - 积到 32 建一条 **audience=user** 的真通知（按日 dedupe）；
        - 回落到阈值之下自动入库（due 下降的唯一途径是他在复习）。
        """
        from replication_notifications import (
            REVIEW_DUE_SPEAK_THRESHOLD, ensure_review_due)
        now_ms = int(time.time() * 1000)

        def due_notes(count):
            return [{"front": "Q%d" % i, "_next": now_ms - 1000,
                     "_st": "review"} for i in range(count)]

        # ① 阈值以下：只报数，不建通知 —— 建了就等于 AI 每天被喊一次
        #    去说一件不值得说的事。
        self._write_notes(due_notes(REVIEW_DUE_SPEAK_THRESHOLD - 1))
        below = ensure_review_due(self.store, self.root)
        self.assertEqual(below["due"], REVIEW_DUE_SPEAK_THRESHOLD - 1)
        self.assertEqual(self.store.open_items(), [],
                         "阈值以下不该产生任何通知")

        # ② 到阈值：建一条 audience=user（要走慢板/ack 状态机，
        #    audience=ai 的话没人会醒 —— 那正是砍收件箱的原因）。
        self._write_notes(due_notes(REVIEW_DUE_SPEAK_THRESHOLD))
        ensure_review_due(self.store, self.root)
        items = self.store.open_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["audience"], "user")
        ensure_review_due(self.store, self.root)
        self.assertEqual(len(self.store.open_items()), 1,
                         "dedupe 按日,一天最多一条")

        # ③ 回落（他在复习）→ 自动入库，别让 AI 拿过时数字去说。
        self._write_notes(due_notes(REVIEW_DUE_SPEAK_THRESHOLD - 5))
        fallen = ensure_review_due(self.store, self.root)
        self.assertEqual(fallen["due"], REVIEW_DUE_SPEAK_THRESHOLD - 5)
        self.assertEqual(self.store.open_items(), [])
        archived = [json.loads(x) for x in
                    (self.root / "notifications-archive.jsonl")
                    .read_text("utf-8").splitlines()]
        self.assertEqual(archived[-1]["resolvedBy"], "auto")
        self.assertIn("回落", archived[-1]["resolutionNote"])

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
                                 audience="user", end="never")
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
            due_at_ms=due, end="never")
        self.assertEqual(item["dueAtUtcMs"], due, "落库要带到点时刻")
        tab = self.root / "notifications-user.json"
        self.store.export_user_open(tab)
        exported = json.loads(tab.read_text("utf-8"))["items"][0]
        self.assertEqual(
            exported["dueAtUtcMs"], due,
            "导出必须显式搬这个字段 —— 只在落库存下、导出漏搬,"
            "表现是「校验全过就是不响」")

    def test_missing_due_at_exports_as_null(self) -> None:
        self.store.create(kind="user-todo", title="倒垃圾", audience="user", end="never")
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
            due_at_ms=1_800_000_000_000, end="never")
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
                              due_at_ms=_now_ms() + 600_000, end="never")
        self.assertIn("audience user", str(caught.exception))

    def test_dedupe_hit_applies_new_due_and_reports(self) -> None:
        """同 key 命中时不能静默丢掉新参数:AI 以为自己刚设好了到点时刻,
        实际什么都没变、也没有任何报错。"""
        first = self.store.create(
            kind="trip", title="回家", audience="user",
            dedupe_key="trip:home", due_at_ms=_now_ms() + 600_000, end="never")
        target = _now_ms() + 1_200_000
        again = self.store.create(
            kind="trip", title="回家", audience="user",
            dedupe_key="trip:home", due_at_ms=target, end="never")
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
            place={"name": "家", "proximity": "enter"}, end="never")
        self.assertEqual(item["place"], {"name": "家", "proximity": "enter"},
                         "真值表里不该有坐标")

    def test_export_resolves_coordinates(self) -> None:
        self._name_place("家", 35.6586, 139.7454)
        self.store.create(kind="user-todo", title="倒垃圾", audience="user",
                          place={"name": "家", "proximity": "enter"}, end="never")
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
                                 "proximity": "enter"}, end="never")
        out = self.root / "notifications-user.json"
        self.store.export_user_open(out)
        self.assertIsNone(
            json.loads(out.read_text("utf-8"))["items"][0]["place"])

    def test_place_requires_user_audience(self) -> None:
        with self.assertRaises(NotificationError):
            self.store.create(kind="user-todo", title="方向错了",
                              audience="ai",
                              place={"name": "家", "proximity": "enter"}, end="never")

    def test_bad_proximity_rejected(self) -> None:
        with self.assertRaises(NotificationError):
            self.store.create(kind="user-todo", title="触发方式错了",
                              audience="user",
                              place={"name": "家", "proximity": "nearby"})

    def test_rename_place_follows_on_next_export(self) -> None:
        """坐标不进真值表的好处:地点被移动/重命名后,已有的提醒自动跟上。"""
        self._name_place("家", 35.0, 139.0)
        self.store.create(kind="user-todo", title="倒垃圾", audience="user",
                          place={"name": "家", "proximity": "enter"}, end="never")
        self._name_place("家", 36.0, 140.0)   # 用户重新标定了「家」
        out = self.root / "notifications-user.json"
        self.store.export_user_open(out)
        place = json.loads(out.read_text("utf-8"))["items"][0]["place"]
        self.assertAlmostEqual(place["lat"], 36.0, places=4,
                               msg="导出要用当前坐标,不是创建时的快照")


class ArrivalAutoResolveTests(unittest.TestCase):
    """到达即完成(2026-08-26 用户:「我都到家很久了但还是显示那个坐车
    回家的待办」)。原则:待办在创建时就该定义它怎么结束。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = NotificationStore(self.root)
        import replication_places
        self.places = replication_places
        self.places.save_alias(self.root, "家", 35.0, 139.0)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _dwell(self, at_ms: int, lat: float, lon: float) -> None:
        """写一条带定位的停留记录。

        ⚠ 形状必须与真实账本一致（replication-data/<书>/activity-dwell.jsonl，
        坐标在 body.loc、时刻在 receivedAtUtcMs）。第一版测试自己编了个
        形状，于是"到达没关掉待办" —— 那是测试错了不是代码错了，但它也
        正说明这条链只认真实形状。
        """
        import replication_activity
        book = self.root / "replication-data" / "book-test"
        book.mkdir(parents=True, exist_ok=True)
        path = book / replication_activity.ACTIVITY_FILE_NAME
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "receivedAtUtcMs": at_ms,
                "body": {
                    "kind": "dwell",
                    "loc": {"lat": lat, "lon": lon, "name": "测试点"},
                    "entries": [{"secs": 600}],
                },
            }, ensure_ascii=False) + "\n")

    def test_arrival_after_creation_closes_it(self) -> None:
        import replication_notifications as rn
        item = self.store.create(
            kind="trip", title="坐车回家", audience="user",
            auto_resolve={"type": "place-arrived", "place": "家"})
        self._dwell(int(item["createdAtUtcMs"]) + 60_000, 35.0, 139.0)
        closed = rn.auto_resolve_on_arrival(self.store, self.root)
        self.assertEqual(closed, 1)
        self.assertEqual(self.store.open_items(), [],
                         "到家之后这条待办就该自己消失")

    def test_being_there_at_creation_does_not_self_close(self) -> None:
        """判的是「到达」这个事件,不是「此刻在不在」。否则「到公司记得
        交表」在公司说出口的瞬间就自我了断了。"""
        import replication_notifications as rn
        item = self.store.create(
            kind="user-todo", title="到家倒垃圾", audience="user",
            auto_resolve={"type": "place-arrived", "place": "家"})
        self._dwell(int(item["createdAtUtcMs"]) - 60_000, 35.0, 139.0)
        self.assertEqual(rn.auto_resolve_on_arrival(self.store, self.root), 0)
        self.assertEqual(len(self.store.open_items()), 1)

    def test_arrival_elsewhere_does_not_close(self) -> None:
        import replication_notifications as rn
        item = self.store.create(
            kind="trip", title="坐车回家", audience="user",
            auto_resolve={"type": "place-arrived", "place": "家"})
        self._dwell(int(item["createdAtUtcMs"]) + 60_000, 36.0, 140.0)
        self.assertEqual(rn.auto_resolve_on_arrival(self.store, self.root), 0)

    def test_unknown_place_rejected_at_creation(self) -> None:
        """地点名拼错 = 这条待办永远关不掉,且没有任何地方会说出来。"""
        with self.assertRaises(NotificationError):
            self.store.create(
                kind="trip", title="回没命名过的地方", audience="user",
                auto_resolve={"type": "place-arrived", "place": "月球"})


class TerminationGuardTests(unittest.TestCase):
    """「创建成功、永远不会结束」—— 跟到点那三条同族的第四种。

    2026-08-29 实锤：08-27 / 08-28 的垃圾提醒到 08-29 还挂在提醒事项里，
    因为 autoResolve / dueAt / expiresAt 全是 null。带 place 只决定
    **什么时候提醒**，不决定**什么时候结束** —— 这两件事看起来很像。
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_user_todo_without_any_termination_is_refused(self) -> None:
        with self.assertRaises(NotificationError) as e:
            self.store.create(
                kind="user-todo", title="倒垃圾", audience="user")
        # 报错必须**能照做**：把模式列出来，而不是只说"无效"。
        message = str(e.exception)
        for option in ("end=expires:", "end=auto:", "end=never"):
            self.assertIn(option, message)
        # ⚠ 还要说清「判断不了」怎么办 —— 用户 2026-08-29：
        # 「如果无法回答某个内容可能他需要回来询问我或者自己分析」。
        # 不写这句的话，被拦住的 AI 最可能的反应是**随便挑一个**，
        # 那比不拦更糟：它会造出一个看着合理、实际错误的终止条件，
        # 而错误的终止条件是查不出来的（待办悄悄提前消失或永远不消失）。
        self.assertIn("回去问用户", message)
        # due 不是终止条件这件事也必须写在报错里 —— 我自己第一版就搞错了。
        self.assertIn("due", message)

    def test_each_termination_option_is_accepted(self) -> None:
        future = _now_ms() + 3600 * 1000
        self.store.create(kind="trip", title="A", audience="user",
                          due_at_ms=future, end="never")
        self.store.create(kind="user-todo", title="B", audience="user",
                          expires_at_ms=future)
        self.store.create(kind="user-todo", title="C", audience="user",
                          auto_resolve={"type": "item-mutated",
                                        "itemId": "it-1"})
        self.store.create(kind="user-todo", title="D", audience="user",
                          end="never")
        self.assertEqual(len(list(self.store.open_items())), 4)

    def test_auto_condition_requires_something_to_bind_to(self) -> None:
        # 一个 auto 条件没绑到具体对象上就**永远不会命中** —— 建出来的是
        # 一条永不结束的待办，而链路上没有一处会说出来。2026-08-30 之前
        # 只拦了 place-arrived，另外两个照样「已创建」。
        for auto_type, flag in (("item-mutated", "--auto-item"),
                                ("card-reviewed", "--auto-card")):
            with self.assertRaises(NotificationError) as caught:
                self.store.create(kind="user-todo", title="X",
                                  audience="user",
                                  auto_resolve={"type": auto_type})
            # 报错要说清该补哪个参数，否则等于只说"不行"。
            self.assertIn(flag, str(caught.exception))

    def test_auto_condition_with_binding_is_accepted(self) -> None:
        # ⚠ 负对照。这条同时钉住另一件事：`end=auto:` 分支**不许重建**
        # autoResolve —— 它曾无条件 `= {"type": condition}`，把命令行传进
        # 来的绑定字段整个盖掉，表现是「参数给了、校验也过了，就是不生效」。
        for auto_type, field, value in (
                ("item-mutated", "itemId", "it-9"),
                ("card-reviewed", "cardId", "card-9")):
            item = self.store.create(
                kind="user-todo", title="Y", audience="user",
                end="auto:" + auto_type,
                auto_resolve={"type": auto_type, field: value})
            # 光是"建出来了"不够，绑定字段必须**真的还在**。
            self.assertEqual(item["autoResolve"].get(field), value)

    def test_ai_audience_is_not_affected(self) -> None:
        # ⚠ 负对照：系统/AI 方向的条目**本来就**不需要终止条件
        # （靠同 dedupe_key 覆盖、靠下一轮对账退场）。第一版我对全部条目
        # 都拦，当场挂掉 12 个既有用例 —— 那正是这条边界的证据。
        self.store.create(kind="hint", title="随便", audience="ai")
        self.assertEqual(len(list(self.store.open_items())), 1)


class DeliverModeTests(unittest.TestCase):
    """怎么送到用户面前。此前只有"发"没有"怎么发" —— 一条待办建出来就
    同时走七条通道，建它的人一个字都插不上话，"在公司别出声"无处表达。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_default_is_auto(self) -> None:
        item = self.store.create(
            kind="user-todo", title="倒垃圾", audience="user", end="never")
        self.assertEqual(item["deliver"], "auto")

    def test_each_mode_accepted_and_persisted(self) -> None:
        for mode in ("auto", "silent", "voice"):
            item = self.store.create(
                kind="user-todo", title="t-" + mode, audience="user",
                end="never", deliver=mode)
            self.assertEqual(item["deliver"], mode)

    def test_unknown_mode_is_refused_with_options(self) -> None:
        with self.assertRaises(NotificationError) as e:
            self.store.create(
                kind="user-todo", title="x", audience="user",
                end="never", deliver="telepathy")
        message = str(e.exception)
        for mode in ("auto", "silent", "voice"):
            self.assertIn(mode, message)

    def test_call_mode_is_accepted_now_that_the_channel_exists(self) -> None:
        # 2026-08-29 开放。这条用例此前断言的是**拒绝**，理由写着
        # "不接受一个不存在的能力" —— 那条理由随通道建成而不成立了
        # （Production APNs 密钥 + App 侧 PushKit/CallKit + Windows 推送器）。
        # 规则变了就改断言，而不是留一条测过时行为的绿灯。
        item = self.store.create(
            kind="user-todo", title="必须现在知道的事", audience="user",
            end="never", deliver="call")
        self.assertEqual(item["deliver"], "call")

    def test_export_carries_deliver(self) -> None:
        # 设备侧要靠它决定出不出声，所以必须导出去 —— 只落库不导出的话，
        # 这个字段在真正用到它的地方是不存在的。
        self.store.create(
            kind="user-todo", title="安静的事", audience="user",
            end="never", deliver="silent")
        out = self.root / "notifications-user.json"
        self.store.export_user_open(out)
        item = json.loads(out.read_text("utf-8"))["items"][0]
        self.assertEqual(item["deliver"], "silent")


class RouterTests(unittest.TestCase):
    """路由层（2026-08-30）。守的是那张路由表本身 —— 每行一条断言，
    表改了测试就该跟着改，反过来测试红了说明表被悄悄动了。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.runtime = Path(self.temporary.name) / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _place(self, state, alias):
        (self.runtime / "current-place.json").write_text(json.dumps({
            "state": state, "alias": alias,
            "observedAtUtcMs": int(time.time() * 1000)}), encoding="utf-8")

    def _devices(self, voice=False, reading=False):
        now_ms = int(time.time() * 1000)
        (self.root / "readerpc-server.status.json").write_text(json.dumps({
            "updatedAtEpochMs": now_ms,
            "voice": {"readerConnected": voice, "captureActive": voice},
            "readerContext": {
                "title": "书" if reading else "",
                "updatedAtEpochMs": now_ms},
        }), encoding="utf-8")

    def _route_of(self, **kwargs):
        from replication_notifications import route_open_user_items
        item = self.store.create(
            kind="user-todo", title="事", audience="user",
            end="never", **kwargs)
        payload = route_open_user_items(self.store, self.root, self.runtime)
        return payload["routes"][item["id"]]

    def test_call_punches_through_everything(self) -> None:
        # 在工作、没用设备 —— call 照样打：选那档时就已决定"必须现在知道"。
        self._place("work", "工作地点")
        self._devices()
        self.assertEqual(self._route_of(deliver="call")["action"], "call")

    def test_silent_never_reaches_the_board(self) -> None:
        self._place("home", "家")
        self.assertEqual(self._route_of(deliver="silent")["action"], "hold")

    def test_auto_at_work_idle_holds(self) -> None:
        # 用户的原话场景：在上班场所且没在用 app 也没连语音 = 在工作。
        self._place("work", "工作地点")
        self._devices(voice=False, reading=False)
        route = self._route_of()
        self.assertEqual(route["action"], "hold")
        self.assertIn("在工作", route["reason"])

    def test_auto_engaged_speaks_even_at_work(self) -> None:
        # 连着语音 = 说话够得着且不算打扰，位置不再是障碍。
        self._place("work", "工作地点")
        self._devices(voice=True)
        self.assertEqual(self._route_of()["action"], "speak")

    def test_auto_at_home_speaks(self) -> None:
        self._place("home", "家")
        self._devices()
        self.assertEqual(self._route_of()["action"], "speak")

    def test_auto_unknown_place_goes_to_judge_with_reason(self) -> None:
        # 「不知道在哪」≠「不在家」：程序判不了就如实交给 AI，
        # 且必须写明为什么判不了 —— 不写原因等于只说"不行"。
        self._devices()
        route = self._route_of()
        self.assertEqual(route["action"], "judge")
        self.assertTrue(route["reason"])

    def test_place_bound_todo_waits_for_that_place(self) -> None:
        self._place("work", "工作地点")
        route = self._route_of(place={"name": "家"})
        self.assertEqual(route["action"], "hold")
        self.assertIn("家", route["reason"])
        self._place("home", "家")
        from replication_notifications import route_open_user_items
        payload = route_open_user_items(self.store, self.root, self.runtime)
        self.assertEqual(
            list(payload["routes"].values())[0]["action"], "speak",
            "到了指定地点必须翻案 —— 条件本身就是闹钟")

    def test_stale_heartbeat_is_not_engagement(self) -> None:
        # 心跳停了的「语音已连」是旧话 —— 拿旧话当现状会在他早已离开后
        # 还判"够得着"。陈旧状态必须落回地点分支。
        now_ms = int(time.time() * 1000)
        (self.root / "readerpc-server.status.json").write_text(json.dumps({
            "updatedAtEpochMs": now_ms - 30 * 60_000,
            "voice": {"readerConnected": True, "captureActive": True},
            "readerContext": {"title": "书", "updatedAtEpochMs": now_ms},
        }), encoding="utf-8")
        self._place("work", "工作地点")
        self.assertEqual(self._route_of()["action"], "hold")


class VoiceHealthTests(unittest.TestCase):
    """语音链健康通知（2026-08-30）：连败喊人，恢复自动收。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.runtime = Path(self.temporary.name) / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()
        self.store = NotificationStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _failures(self, count, minutes_ago=1.0):
        import datetime
        at = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=minutes_ago)).isoformat()
        lines = "\n".join(
            json.dumps({
                "atUtc": at,
                "code": "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED",
            }) for _ in range(count))
        (self.runtime / "computer-voice-direct.failures.jsonl").write_text(
            lines, encoding="utf-8")

    def _voice(self, active):
        (self.root / "readerpc-server.status.json").write_text(json.dumps({
            "voice": {"codexVoiceActive": active}}), encoding="utf-8")

    def test_three_failures_stay_quiet(self) -> None:
        # 前三次是正常冷启动的预算（热键 45-60s 才就绪，第一按注定落空），
        # 报出去就是每次冷启动都喊一次狼来了。
        from replication_notifications import ensure_codex_voice_health
        self._voice(False)
        self._failures(3)
        ensure_codex_voice_health(self.store, self.root, self.runtime)
        self.assertEqual(self.store.open_items(), [])

    def test_four_failures_speak_up(self) -> None:
        from replication_notifications import ensure_codex_voice_health
        self._voice(False)
        self._failures(4)
        ensure_codex_voice_health(self.store, self.root, self.runtime)
        items = self.store.open_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["audience"], "user")
        self.assertIn("卡在错误界面", items[0]["title"])
        # 同一轮不重报
        ensure_codex_voice_health(self.store, self.root, self.runtime)
        self.assertEqual(len(self.store.open_items()), 1)

    def test_recovery_resolves(self) -> None:
        from replication_notifications import ensure_codex_voice_health
        self._voice(False)
        self._failures(5)
        ensure_codex_voice_health(self.store, self.root, self.runtime)
        self.assertEqual(len(self.store.open_items()), 1)
        self._voice(True)
        ensure_codex_voice_health(self.store, self.root, self.runtime)
        self.assertEqual(self.store.open_items(), [],
                         "语音恢复必须自动入库 —— 别让旧警报继续吓人")

    def test_stale_failures_do_not_alarm(self) -> None:
        # 负对照：一小时前的连败已成历史（多半早就恢复过又断过电），
        # 不该在此刻拉响。
        from replication_notifications import ensure_codex_voice_health
        self._voice(False)
        self._failures(6, minutes_ago=60)
        ensure_codex_voice_health(self.store, self.root, self.runtime)
        self.assertEqual(self.store.open_items(), [])
