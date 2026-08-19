"""复习补投脚本的分类与安全边界（C 组 #17 的 G5）。

补投的对象是**用户真实的复习进度** —— `answerCards` 会真改 Anki 的调度。
所以这里钉住的不只是"它能补"，更是"它在什么情况下**不**补"。
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "replay_review_events.py"


class ReplayReviewEventsTest(unittest.TestCase):
    def _run(self, rows, seen=None, extra_args=()):
        """把脚本的 state 目录指到临时目录再跑（不碰真数据）。"""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "reader-review-events.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                "utf-8",
            )
            (state / "review-answers-seen.json").write_text(
                json.dumps(seen or []), "utf-8"
            )
            patched = SCRIPT.read_text("utf-8").replace(
                '_STATE = config.PROJECT_DIR / "state"',
                f"_STATE = Path(r{str(state)!r})",
            )
            # 放在 scripts/ 下跑，`import config` 才解析得到
            probe = ROOT / "scripts" / "_replay_test_probe.py"
            probe.write_text(patched, "utf-8")
            try:
                result = subprocess.run(
                    [sys.executable, str(probe), *extra_args],
                    capture_output=True, text=True, encoding="utf-8",
                )
            finally:
                probe.unlink(missing_ok=True)
            return result.stdout + result.stderr

    @staticmethod
    def _event(**over):
        now = int(time.time() * 1000)
        row = {
            "id": "e", "aid": "a_1", "ankiCardId": "1001",
            "ease": 3, "reviewedAt": now - 3600000, "queue": "local",
        }
        row.update(over)
        return row

    def test_default_is_dry_run(self) -> None:
        """默认不能改任何东西 —— answerCards 是不可逆的。"""
        out = self._run([self._event()])
        self.assertIn("这是预演", out)
        self.assertIn("待补投 1", out)

    def test_settled_aid_is_skipped(self) -> None:
        """台账里已完成的不再投 —— 重复投会让同一次复习被算两遍。"""
        out = self._run([self._event(aid="a_done")], seen=["a_done"])
        self.assertIn("已落库 1", out)
        self.assertIn("待补投 0", out)

    def test_stale_events_are_reported_not_replayed(self) -> None:
        """太旧的只报告。

        FSRS 按"距上次复习多久"算间隔，而 answerCards **没有时间戳参数** ——
        补投一条 30 天前的复习，Anki 会当成"现在"，间隔算出来是错的。
        """
        old = self._event(id="old", reviewedAt=int(time.time() * 1000) - 30 * 86400000)
        out = self._run([old])
        self.assertIn("太旧 1", out)
        self.assertIn("待补投 0", out)
        self.assertIn("补投会把 FSRS 间隔算错", out)

    def test_event_without_anki_card_id_cannot_be_targeted(self) -> None:
        out = self._run([self._event(id="nocid", ankiCardId="")])
        self.assertIn("无法定位", out)
        self.assertIn("投不到具体哪张卡", out)

    def test_event_without_aid_is_never_replayed(self) -> None:
        """没有 aid 就跟台账对不上号 —— 宁可不投也不重复投。"""
        row = self._event(id="noaid")
        row.pop("aid")
        out = self._run([row])
        self.assertIn("无法与台账对账", out)
        self.assertIn("待补投 0", out)

    def test_empty_log_says_so(self) -> None:
        out = self._run([])
        self.assertIn("事件日志是空的", out)


class ReplaySafetyContractTest(unittest.TestCase):
    """源码级的安全边界。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text("utf-8")

    def test_apply_is_opt_in(self) -> None:
        self.assertIn('"--apply", action="store_true"', self.source)
        self.assertIn("if not args.apply:", self.source)

    def test_replayed_ledger_failure_is_loud(self) -> None:
        # 记不下"补过了"比补投失败更危险：下次会重复投。
        self.assertIn("下次可能重复投", self.source)
        self.assertIn("return 1", self.source)

    def test_answer_cards_result_is_verified(self) -> None:
        # answerCards 返回 [true] 才算接受；不检查就会把失败当成功记进台账。
        self.assertIn("answerCards 未接受", self.source)
        self.assertIn("isinstance(result, list) and result and result[0]", self.source)


if __name__ == "__main__":
    unittest.main()
