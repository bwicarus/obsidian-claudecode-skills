"""在外面没戴耳机自动静音的整条链（2026-09-08 用户拍板，保留位置条件）。

用户原话两句，第二句否掉了我提的简化：
  「如果我在外面没有带耳机的情况下如果 app 的语音是开启的其实我希望 app 可以自动静音」
  「在家里我说的算，而且也只有我一个人」
所以判据是**两条都要成立**，不是"没耳机就静音"。

链路：App 本机判（ReaderPresenceGuard，即时、不出网）
      → 判据「语音区」由 Python 导出、桥随 /reader-presence/v1 响应下发
      → 在场状态回传 Windows 变成 situation_signals 的三个信号（给 AI 和规则用）

这份测试守的是**不能静错**的那几个方向。静错的代价不对称：错误静音会让
语音看起来是坏的且没有提示说得出为什么，少静一次只是少静一次。
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "extensions" / "bw-reader-webext" / "windows" / "computer-voice-desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import replication_places as rp  # noqa: E402
import situation_signals  # noqa: E402

SWIFT = (ROOT / "ios" / "BWReader" / "App" / "ReaderPresenceGuard.swift"
         ).read_text(encoding="utf-8")
ENGINE = (ROOT / "ios" / "BWReader" / "App" / "NativeAudioEngine.swift"
          ).read_text(encoding="utf-8")


class VoiceZonesExportTests(unittest.TestCase):
    """判据由 Python 导：别名→状态的映射只能存在一处。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_aliases(self, *aliases):
        (self.root / rp.ALIASES_FILE_NAME).write_text(json.dumps({
            "contract": rp.ALIASES_CONTRACT, "aliases": list(aliases),
        }), encoding="utf-8")

    def test_alias_names_are_folded_into_states(self):
        # App 只认 home/work/elsewhere，绝不该让 Swift 再抄一份中文别名表。
        self.write_aliases(
            {"name": "家", "lat": 35.65, "lon": 139.31},
            {"name": "工作地点", "lat": 35.63, "lon": 139.30},
            {"name": "超市", "lat": 35.64, "lon": 139.32})
        value = rp.export_voice_zones(self.root)
        states = {one["name"]: one["state"] for one in value["zones"]}
        self.assertEqual(states["家"], "home")
        self.assertEqual(states["工作地点"], "work")
        self.assertEqual(states["超市"], "elsewhere")

    def test_radius_is_shared_with_alias_resolution(self):
        # 两处各写一个半径，就会出现"Windows 认为到家了、App 认为还没到"。
        self.write_aliases({"name": "家", "lat": 35.65, "lon": 139.31})
        self.assertEqual(
            rp.export_voice_zones(self.root)["hitRadiusM"],
            rp.ALIAS_HIT_RADIUS_M)

    def test_broken_alias_rows_are_skipped_not_fatal(self):
        self.write_aliases(
            {"name": "家", "lat": 35.65, "lon": 139.31},
            {"name": "没坐标"},
            {"lat": 1.0, "lon": 2.0})
        value = rp.export_voice_zones(self.root)
        self.assertEqual([one["name"] for one in value["zones"]], ["家"])

    def test_export_lands_next_to_the_alias_table(self):
        self.write_aliases({"name": "家", "lat": 35.65, "lon": 139.31})
        rp.export_voice_zones(self.root)
        # 桥的 C# 从 BWReader 根读它 —— 换目录等于 App 永远拿不到判据。
        self.assertTrue((self.root / rp.VOICE_ZONES_FILE_NAME).is_file())


class PresenceSignalTests(unittest.TestCase):
    """在场状态变成 situation_signals 的三个信号。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def read(self, *names):
        return situation_signals.read_all(
            self.root, self.runtime, list(names))["signals"]

    def test_speaker_means_no_headphones(self):
        (self.root / situation_signals.PRESENCE_FILE_NAME).write_text(
            json.dumps({"audioRoute": "speaker", "foreground": True,
                        "atMs": int(__import__("time").time() * 1000)}),
            encoding="utf-8")
        signals = self.read("headphones", "audio_route", "app_foreground")
        self.assertIs(signals["headphones"]["value"], False)
        self.assertEqual(signals["audio_route"]["value"], "speaker")
        self.assertIs(signals["app_foreground"]["value"], True)

    def test_no_report_is_unknown_not_no_headphones(self):
        # 「没报过」不等于「没戴耳机」。混起来会让 AI 以为可以随便出声。
        signals = self.read("headphones")
        self.assertFalse(signals["headphones"]["known"])


class SwiftDecisionTests(unittest.TestCase):
    """Swift 侧的判断规则。真机行为要靠 TestFlight 验，这里守的是规则没被改反。"""

    def test_location_condition_is_kept(self):
        # 用户明确否掉了"没耳机就静音"的简化，位置条件必须在。
        self.assertIn('shouldMuteVoiceOutput = state != "home"', SWIFT)

    def test_headphones_never_mute(self):
        self.assertIn("if Self.routeIsPrivate(route) {", SWIFT)
        self.assertIn(
            'route != "speaker" && route != "receiver"', SWIFT)

    def test_unknown_location_does_not_mute(self):
        # 判不出在哪 ≠ 在外面。这条是最容易被"顺手简化"掉的。
        body = SWIFT.split("func reevaluate(")[1].split("\n    }")[0]
        tail = body.split("} else {")[1]
        self.assertIn("shouldMuteVoiceOutput = false", tail)
        self.assertIn("判不出在哪", tail)

    def test_empty_zone_list_is_treated_as_no_criteria(self):
        # 空清单当"没判据"而不是"一个地点都没命名" —— 后者会让 App
        # 认为自己永远在外面，一出声就静音。
        self.assertIn(
            "return zones.isEmpty ? nil : Zones(", SWIFT)

    def test_zones_are_cached_for_offline_use(self):
        # 出门在外往往连不上家里的电脑，而那恰恰是最需要判断的时刻。
        self.assertIn("UserDefaults.standard.set(raw, forKey: Self.zonesKey)",
                      SWIFT)
        self.assertIn("UserDefaults.standard.dictionary(forKey: Self.zonesKey)",
                      SWIFT)

    def test_mute_is_volume_not_stop(self):
        # 用户要的是"静音"不是"挂断"：会话照常、字幕照出、AI 还听得见。
        self.assertIn("player.volume = muted ? 0 : 1", ENGINE)

    def test_engine_applies_on_route_change_and_on_playback_start(self):
        # 只在路由变化时判会漏掉"出门之后才开始说话"（那时不会再有变化事件）。
        self.assertEqual(ENGINE.count("applyPresenceMute()"), 3)
        route_block = ENGINE.split("routeChangeNotification")[1].split("}")[0]
        self.assertIn("applyPresenceMute", route_block)


class EndpointRegistrationTests(unittest.TestCase):
    """路由、分发、serve 白名单、预检 —— 少一处就 404 或打包被拒。"""

    def test_registered_everywhere(self):
        cs = (ROOT / "extensions/bw-reader-webext/windows/ComputerVoiceAudio"
              / "ReaderPresenceSignal.cs").read_text(encoding="utf-8")
        self.assertIn('RoutePath = "/reader-presence/v1"', cs)
        server = (ROOT / "extensions/bw-reader-webext/windows/ComputerVoiceAudio"
                  / "DirectBridgeServer.cs").read_text(encoding="utf-8")
        self.assertIn("ReaderPresenceSignal.RoutePath", server)
        self.assertIn("HandlePresenceSignalAsync", server)
        core = (ROOT / "extensions/bw-reader-webext/windows/computer-voice-desktop"
                / "bridge_core.py").read_text(encoding="utf-8")
        self.assertIn('"/reader-presence/v1"', core)
        preflight = (ROOT / "extensions/bw-reader-webext"
                     / "release_preflight.py").read_text(encoding="utf-8")
        self.assertIn("ComputerVoiceAudio/ReaderPresenceSignal.cs", preflight)

    def test_missing_zones_omits_the_field(self):
        # 空数组会被 App 理解成"一个地点都没命名"→ 永远静音。
        # 缺字段才是"这次没拿到，用你缓存的那份"。
        cs = (ROOT / "extensions/bw-reader-webext/windows/ComputerVoiceAudio"
              / "ReaderPresenceSignal.cs").read_text(encoding="utf-8")
        self.assertIn('if (zones is not null) reply["voiceZones"] = zones;', cs)

    def test_server_clock_stamps_freshness(self):
        # 新鲜度问的是"App 多久前说的"；用设备时钟一歪就全错。
        cs = (ROOT / "extensions/bw-reader-webext/windows/ComputerVoiceAudio"
              / "ReaderPresenceSignal.cs").read_text(encoding="utf-8")
        self.assertIn('["atMs"] = now,', cs)
        self.assertIn('["reportedAtMs"]', cs)


if __name__ == "__main__":
    unittest.main()
