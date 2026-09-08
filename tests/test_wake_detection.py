"""起床时刻 = 一段长静默之后的第一次操作(2026-09-08)。

第一版取"今天第一条",实测当场戳穿:用户跨零点用到 00:10,于是"起床"被算成午夜。
睡眠留下的是**缺口**,不是日期边界。
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "extensions" / "bw-reader-webext" / "windows" / "computer-voice-desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import replication_notifications as rn  # noqa: E402

HOUR = 3_600_000


def day_start_ms() -> int:
    return int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)) * 1000)


class WakeDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "replication-command-ledger.sqlite3"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE commands (received_at_utc_ms INTEGER, actor TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, *stamps, actor="user"):
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO commands VALUES (?, ?)",
                         [(int(s), actor) for s in stamps])
        conn.commit()
        conn.close()

    def test_first_activity_after_a_long_gap_is_waking(self):
        base = day_start_ms()
        self.add(base - 2 * HOUR)          # 昨晚 22 点还在用
        self.add(base + 9 * HOUR)          # 今早 9 点(隔了 11 小时)
        self.add(base + 9 * HOUR + 60_000)
        self.assertEqual(rn.wake_time_today_ms(self.root), base + 9 * HOUR)

    def test_past_midnight_usage_is_not_waking(self):
        """跨零点连着用:00:10 那条不是起床,真正的起床在下午那段空白之后。"""
        base = day_start_ms()
        self.add(base - HOUR, base + 10 * 60_000)   # 23 点 → 00:10,连着的
        self.add(base + 14 * HOUR)                  # 下午 14 点,隔了近 14 小时
        self.assertEqual(rn.wake_time_today_ms(self.root), base + 14 * HOUR)

    def test_all_nighter_has_no_waking_moment(self):
        """通宵没睡就没有"起床"这回事 → None,调用方回落配置钟点。"""
        base = day_start_ms()
        stamps = [base - HOUR + i * 30 * 60_000 for i in range(12)]   # 每半小时一次,不断
        self.add(*stamps)
        self.assertIsNone(rn.wake_time_today_ms(self.root))

    def test_background_commands_do_not_count_as_waking(self):
        """后台对账自己也写命令;拿它当"人醒了"会让起床恒为 0 点。"""
        base = day_start_ms()
        self.add(base - 2 * HOUR)
        self.add(base + 30 * 60_000, actor="system")   # 凌晨的后台命令
        self.add(base + 10 * HOUR)
        self.assertEqual(rn.wake_time_today_ms(self.root), base + 10 * HOUR)

    def test_missing_ledger_returns_none_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(rn.wake_time_today_ms(Path(empty)))


if __name__ == "__main__":
    unittest.main()
