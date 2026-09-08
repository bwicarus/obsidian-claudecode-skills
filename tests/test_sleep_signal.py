"""健康数据信号优先于设备活动推断(2026-09-08 手表功能第一步)。"""
from pathlib import Path
import json
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "extensions" / "bw-reader-webext" / "windows" / "computer-voice-desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import replication_notifications as rn  # noqa: E402


def day_start_ms() -> int:
    return int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)) * 1000)


class SleepSignalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, **fields):
        (self.root / "sleep-signal.json").write_text(
            json.dumps(fields), encoding="utf-8")

    def test_todays_signal_wins_over_activity_inference(self):
        # ⚠ 不能写死"今天 7 点"：凌晨跑这个测试时 7 点还在未来，
        # 而未来时间戳会被 sleep_signal_woke_today_ms 正当地拒掉 ——
        # 于是这条从 00:00 到 07:00 每天必红（2026-09-09 02:27 撞到）。
        # 取"刚才"，并夹到今天之内。
        woke = max(day_start_ms(), int(time.time() * 1000) - 60_000)
        self.write(wokeAtMs=woke, source="healthkit")
        # 目录里没有命令账本,只有信号 → 拿到的就是信号本身
        self.assertEqual(rn.wake_time_today_ms(self.root), woke)

    def test_yesterdays_signal_is_ignored(self):
        """昨天的起床点用在今天,会让窗口起点永远停在过去。"""
        self.write(wokeAtMs=day_start_ms() - 5 * 3_600_000)
        self.assertIsNone(rn.sleep_signal_woke_today_ms(self.root))

    def test_future_timestamp_is_ignored(self):
        """设备时钟错乱时写进未来,会让提醒再也不出声。"""
        self.write(wokeAtMs=int(time.time() * 1000) + 6 * 3_600_000)
        self.assertIsNone(rn.sleep_signal_woke_today_ms(self.root))

    def test_missing_or_broken_file_falls_back_quietly(self):
        self.assertIsNone(rn.sleep_signal_woke_today_ms(self.root))
        (self.root / "sleep-signal.json").write_text("{ 坏的", encoding="utf-8")
        self.assertIsNone(rn.sleep_signal_woke_today_ms(self.root))
        self.write(wokeAtMs=0)
        self.assertIsNone(rn.sleep_signal_woke_today_ms(self.root))

    def test_endpoint_is_registered_in_all_three_places(self):
        """路由、serve 白名单、发布预检白名单 —— 少一处就 404 或打包被拒。"""
        cs = (ROOT / "extensions/bw-reader-webext/windows/ComputerVoiceAudio"
              / "ReaderSleepSignal.cs").read_text(encoding="utf-8")
        self.assertIn('RoutePath = "/reader-sleep/v1"', cs)
        server = (ROOT / "extensions/bw-reader-webext/windows/ComputerVoiceAudio"
                  / "DirectBridgeServer.cs").read_text(encoding="utf-8")
        self.assertIn("ReaderSleepSignal.RoutePath", server)
        core = (ROOT / "extensions/bw-reader-webext/windows/computer-voice-desktop"
                / "bridge_core.py").read_text(encoding="utf-8")
        self.assertIn('"/reader-sleep/v1"', core)
        preflight = (ROOT / "extensions/bw-reader-webext"
                     / "release_preflight.py").read_text(encoding="utf-8")
        self.assertIn("ComputerVoiceAudio/ReaderSleepSignal.cs", preflight)


if __name__ == "__main__":
    unittest.main()
