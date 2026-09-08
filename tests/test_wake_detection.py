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


class ActivitySourcesTests(unittest.TestCase):
    """「他还醒着吗」要看几个来源，取最晚的那个（2026-09-09 实测撞出来的）。

    原来只看复制账本的 actor='user'，也就是只看"他动过阅读器"。实况：
    账本里最后一条是九小时前，而同一刻 Windows 说上次键鼠输入在 1.6 分钟前 ——
    人明明就在电脑前，却被判成睡了九小时。跟 AI 打字、语音说话、开别的软件，
    一条都不会写进那个账本。

    方向是**不对称**的：任何一个来源有动静都足以证明醒着，"都没动静"才是弱证据。
    所以取最晚而不是取某一个，也不能取平均。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._real_idle = rn.pc_input_idle_ms

    def tearDown(self):
        rn.pc_input_idle_ms = self._real_idle
        self._tmp.cleanup()

    def write_ledger(self, at_ms):
        path = self.root / "replication-command-ledger.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE commands (received_at_utc_ms INTEGER, actor TEXT)")
        conn.execute(
            "INSERT INTO commands VALUES (?, 'user')", (int(at_ms),))
        conn.commit()
        conn.close()

    def write_presence(self, at_ms):
        import json
        (self.root / "presence-signal.json").write_text(
            json.dumps({"atMs": int(at_ms), "audioRoute": "speaker",
                        "foreground": True}), encoding="utf-8")

    def test_recent_keyboard_beats_a_stale_ledger(self):
        # 这就是实测那一刻：账本九小时前，键鼠两分钟前。
        now = int(time.time() * 1000)
        self.write_ledger(now - 9 * HOUR)
        rn.pc_input_idle_ms = lambda: 2 * 60_000
        last = rn.last_user_activity_ms(self.root)
        self.assertAlmostEqual(last, now - 2 * 60_000, delta=5_000)
        awake, why = rn.looks_awake(self.root)
        self.assertTrue(awake, why)

    def test_app_foreground_counts_when_he_is_away_from_the_pc(self):
        # 人在用手机、没碰电脑：键鼠会一直涨，但 App 报过在场。
        now = int(time.time() * 1000)
        self.write_ledger(now - 9 * HOUR)
        self.write_presence(now - 3 * 60_000)
        rn.pc_input_idle_ms = lambda: 8 * HOUR
        last = rn.last_user_activity_ms(self.root)
        self.assertAlmostEqual(last, now - 3 * 60_000, delta=5_000)
        self.assertTrue(rn.looks_awake(self.root)[0])

    def test_takes_the_latest_not_the_ledger(self):
        now = int(time.time() * 1000)
        self.write_ledger(now - 60_000)          # 账本最新
        self.write_presence(now - 5 * HOUR)
        rn.pc_input_idle_ms = lambda: 3 * HOUR
        self.assertAlmostEqual(
            rn.last_user_activity_ms(self.root), now - 60_000, delta=5_000)

    def test_all_sources_quiet_still_reads_asleep(self):
        # 负对照：只证明"能判醒"的话，一个永远返回醒着的实现也能过。
        now = int(time.time() * 1000)
        self.write_ledger(now - 9 * HOUR)
        rn.pc_input_idle_ms = lambda: 9 * HOUR
        awake, why = rn.looks_awake(self.root)
        self.assertFalse(awake)
        self.assertIn("小时", why)

    def test_nothing_readable_is_unknown_and_treated_as_awake(self):
        rn.pc_input_idle_ms = lambda: None
        self.assertIsNone(rn.last_user_activity_ms(self.root))
        awake, why = rn.looks_awake(self.root)
        # 判不出来当醒着：守卫是为了别在睡觉时出声，不是制造"提醒神秘消失"。
        self.assertTrue(awake)
        self.assertIn("读不到", why)

    def test_input_idle_probe_is_platform_guarded(self):
        # 非 Windows 上没有 GetLastInputInfo；返回 None 而不是抛。
        import inspect
        source = inspect.getsource(rn.pc_input_idle_ms)
        self.assertIn('sys.platform != "win32"', source)
        self.assertIn("return None", source)
        # 能证明醒、不能证明睡 —— 这条纪律要写在文档串里
        self.assertIn("不能证明", rn.pc_input_idle_ms.__doc__ or "")

    def test_wake_time_gap_detection_still_uses_the_ledger_only(self):
        # 键鼠只给"上一次输入"一个时刻，给不出历史，所以它进不了缺口检测。
        # 把它掺进 wake_time_today_ms 会让起床点变成"刚才"。
        import inspect
        source = inspect.getsource(rn.wake_time_today_ms)
        self.assertNotIn("pc_input_idle_ms", source)
        self.assertIn("actor='user'", source)
