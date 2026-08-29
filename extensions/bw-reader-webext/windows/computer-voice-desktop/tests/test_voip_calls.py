from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import replication_apply  # noqa: E402
from replication_notifications import NotificationStore  # noqa: E402


class VoipFollowupTests(unittest.TestCase):
    """电话打完之后怎么办。

    ## ⚠ 拨号本身不在这里

    用户 2026-08-29 纠正：电话必须由**语音 AI** 发起 —— 它打过来是为了
    接通后把事情说清楚，并在那一刻把电脑的语音链路切到这通电话上。
    对账循环自己拨的话，用户接起来只有沉默，比不打更糟。
    （我最初正是那么做的，这套测试也跟着重写了。）

    ## 三种结局三种处置

        answered    不动 —— AI 自己判断用户听懂没有，然后 ack
        declined    立刻降级：他知道有事，只是现在不想接
        unanswered  隔几分钟标成待重拨；第二次还没人接才降级
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        # ⚠ **把 USERPROFILE 指到临时目录。**
        #
        # _call_state_path 优先用桥的 runtime 目录（那是唯一的写者），
        # 而那个目录在开发机上是**真实存在**的 —— 不隔离的话，测试会直接
        # 读写生产文件。2026-08-29 实测：跑完一轮之后，真实的
        # voip-calls.json 里躺着测试造的假 ntf id。
        # 测试污染生产数据不会报错，只会在某天让一通电话打错人。
        self._saved_profile = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = str(self.root)
        self.store = NotificationStore(self.root)
        self.item = self.store.create(
            kind="user-todo", title="要打电话的事", audience="user",
            end="never", deliver="call")

    def tearDown(self) -> None:
        if self._saved_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = self._saved_profile
        shutil.rmtree(self.root, ignore_errors=True)

    def _record(self, outcome: str, attempts: int = 1,
                age_seconds: float = 0.0) -> None:
        replication_apply._save_call_state(self.root, {
            self.item["id"]: {
                "outcome": outcome,
                "attempts": attempts,
                "lastAtUtcMs": int((time.time() - age_seconds) * 1000),
            }
        })

    def _current(self) -> str:
        return replication_apply._load_call_state(
            self.root)[self.item["id"]]["outcome"]

    def test_declined_downgrades_immediately(self) -> None:
        # 主动拒接 = 明确的"现在别烦我"。再打一次是骚扰。
        self._record("declined")
        replication_apply._voip_followup(self.store, self.root)
        self.assertEqual(self._current(), "downgraded")

    def test_unanswered_waits_before_retrying(self) -> None:
        # 刚打完还没到重试时刻 —— 不能立刻再打，那跟连打两通没区别。
        self._record("unanswered", age_seconds=10)
        replication_apply._voip_followup(self.store, self.root)
        self.assertEqual(self._current(), "unanswered")

    def test_unanswered_becomes_retry_due_after_the_wait(self) -> None:
        self._record("unanswered", age_seconds=10 * 60)
        replication_apply._voip_followup(self.store, self.root)
        # ⚠ 是 retry-due（待 AI 重拨），**不是**直接拨出去 ——
        # 拨号的人必须是要说话的那个。
        self.assertEqual(self._current(), "retry-due")

    def test_second_unanswered_downgrades(self) -> None:
        # 试过一次就够了。第二次还没人接，说明他现在接不了电话。
        self._record("unanswered", attempts=2, age_seconds=10 * 60)
        replication_apply._voip_followup(self.store, self.root)
        self.assertEqual(self._current(), "downgraded")

    def test_answered_is_left_alone(self) -> None:
        # 接通之后归 AI 判断（用户可能在通话里说"我知道了"）。
        self._record("answered")
        replication_apply._voip_followup(self.store, self.root)
        self.assertEqual(self._current(), "answered")

    def test_resolved_todo_drops_its_call_record(self) -> None:
        # 待办已经完成 —— 再跟进就是为一件做完的事继续折腾人。
        self._record("unanswered", age_seconds=10 * 60)
        self.store.resolve(self.item["id"], by="user", note="做完了")
        replication_apply._voip_followup(self.store, self.root)
        self.assertNotIn(
            self.item["id"], replication_apply._load_call_state(self.root))
