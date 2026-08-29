from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import replication_apply  # noqa: E402
from replication_notifications import NotificationStore  # noqa: E402


class _FakeVoip:
    """假的推送器。真推一次就是真响一次 —— 测试里绝不能碰真的。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def send_call(self, root, title, reason=""):
        self.calls.append(title)
        if not self.ok:
            raise RuntimeError("配置没就绪")
        return {"ok": True, "status": 200}


class PlaceVoipCallsTests(unittest.TestCase):
    """deliver=call 的待办要真的打电话，而且**每条只打一次**。

    电话是最强的打断。打第二次不会让人更快去做那件事，只会让他直接
    静音这个 App —— 那时连普通通知也一起失去了。
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.store = NotificationStore(self.root)
        self.fake = _FakeVoip()
        self._real = replication_apply.__dict__.get("voip_push")
        sys.modules["voip_push"] = self.fake  # type: ignore[assignment]

    def tearDown(self) -> None:
        sys.modules.pop("voip_push", None)
        shutil.rmtree(self.root, ignore_errors=True)

    def _make(self, title: str, deliver: str) -> None:
        self.store.create(
            kind="user-todo", title=title, audience="user",
            end="never", deliver=deliver)

    def test_only_call_deliver_rings(self) -> None:
        self._make("要打电话的", "call")
        self._make("安静的", "silent")
        self._make("默认的", "auto")
        placed = replication_apply._place_voip_calls(self.store, self.root)
        self.assertEqual(placed, 1)
        self.assertEqual(self.fake.calls, ["要打电话的"])

    def test_does_not_ring_twice(self) -> None:
        self._make("只该响一次", "call")
        replication_apply._place_voip_calls(self.store, self.root)
        again = replication_apply._place_voip_calls(self.store, self.root)
        self.assertEqual(again, 0, "同一条待办打了第二通电话")
        self.assertEqual(len(self.fake.calls), 1)

    def test_survives_restart(self) -> None:
        # ⚠ 记在内存里的话，ReaderPC 一重启就会把所有 call 待办重打一遍。
        # 所以"打过没打过"必须落盘。
        self._make("重启后不该再响", "call")
        replication_apply._place_voip_calls(self.store, self.root)
        record = json.loads(
            (self.root / replication_apply._CALLED_FILE_NAME)
            .read_text(encoding="utf-8"))
        self.assertTrue(record["ids"], "打过的记录没落盘")
        fresh = NotificationStore(self.root)
        self.assertEqual(
            replication_apply._place_voip_calls(fresh, self.root), 0)

    def test_failure_is_not_recorded_as_called(self) -> None:
        # ⚠ 打失败（没 token / 没密钥）**不能**标成打过 ——
        # 标了的话配置修好之后它永远不会再响，而没人会发现。
        self.fake.ok = False
        self._make("失败后还该重试", "call")
        placed = replication_apply._place_voip_calls(self.store, self.root)
        self.assertEqual(placed, 0)
        self.assertFalse(
            (self.root / replication_apply._CALLED_FILE_NAME).exists(),
            "失败却记成了打过")
        # 配置修好之后，同一条应该还能响。
        self.fake.ok = True
        self.assertEqual(
            replication_apply._place_voip_calls(self.store, self.root), 1)

    def test_resolved_items_are_not_called(self) -> None:
        # 为一件已经完成的事把人吵醒，是最伤信任的一种打扰。
        self._make("已经做完了", "call")
        item = list(self.store.open_items())[0]
        self.store.resolve(item["id"], by="user", note="做完了")
        self.assertEqual(
            replication_apply._place_voip_calls(self.store, self.root), 0)
        self.assertEqual(self.fake.calls, [])
