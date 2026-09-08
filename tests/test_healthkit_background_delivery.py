"""睡眠信号的后台投递（2026-09-09 用户点名要）。

守的三条全是**坏掉不留痕迹**的：

① **必须在启动时注册。** 系统因为新的睡眠样本把 App 唤到后台时 SwiftUI
   视图层根本不出现，挂在 `.task` 里永远不跑 —— 表现是"权限给了、投递也开了，
   就是一条都没上报"，而没有一处会报错。VoIP 那条链 2026-08-29 就是这么栽的。
② **`completionHandler` 无论如何都要调。** 不调的话 iOS 先节流、然后干脆不再
   唤醒 —— 表现是"一开始还报，过几天就不报了"，最难查的那一类。
③ **观察查询和后台投递两个都要。** 只挂观察查询的话它只在 App 活着时管用；
   只开投递没有查询则没人处理唤醒。少任何一个，症状都是"前台好使、后台没动静"。

为什么这些只能在源码层断言：真机行为要装上 TestFlight 才验得了，而这三条
一旦写错，装上去也要等好几天才看得出来。
"""
from pathlib import Path
import plistlib
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ios" / "BWReader" / "App"
REPORTER = (APP / "ReaderSleepReporter.swift").read_text(encoding="utf-8")
DELEGATE = (APP / "BWReaderNativeApp.swift").read_text(encoding="utf-8")


class BackgroundDeliveryTests(unittest.TestCase):
    def test_entitlement_is_declared(self):
        with open(APP / "BWReader.entitlements", "rb") as handle:
            entitlements = plistlib.load(handle)
        self.assertTrue(entitlements.get("com.apple.developer.healthkit"))
        self.assertTrue(
            entitlements.get("com.apple.developer.healthkit.background-delivery"),
            "没有这条权限，enableBackgroundDelivery 会失败而 App 照常启动")

    def test_no_background_mode_was_added(self):
        # HealthKit 用自己那套唤醒，不是 UIBackgroundModes。加了反而会被审核
        # 问"你要后台干什么"，而且手表那边 2026-08-27 已经栽过一次背景模式。
        with open(APP / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
        modes = info.get("UIBackgroundModes") or []
        self.assertNotIn("processing", modes)
        self.assertNotIn("fetch", modes)

    # ── ① 启动时注册
    def test_registered_from_app_launch(self):
        self.assertIn("ReaderSleepReporter.activateFromLaunch()", DELEGATE)
        launch = DELEGATE[
            DELEGATE.index("didFinishLaunchingWithOptions"):
            DELEGATE.index("didFinishLaunchingWithOptions") + 2500]
        self.assertIn("ReaderSleepReporter.activateFromLaunch()", launch,
                      "必须挂在 didFinishLaunchingWithOptions 里")
        # nonisolated static：AppDelegate 不在 MainActor 上
        self.assertIn(
            "nonisolated static func activateFromLaunch()", REPORTER)

    # ── ② completionHandler 一定要调
    def test_completion_handler_is_always_called(self):
        body = REPORTER[
            REPORTER.index("let query = HKObserverQuery"):
            REPORTER.index("store.execute(query)")]
        # 写在 defer 里才盖得住每一条提前返回
        self.assertIn("defer { completionHandler() }", body)
        # 出错那一支也必须走到 defer，也就是 return 而不是别的
        self.assertIn("return", body)
        self.assertNotIn("fatalError", body)

    def test_error_from_the_wake_up_is_recorded_not_swallowed(self):
        body = REPORTER[
            REPORTER.index("let query = HKObserverQuery"):
            REPORTER.index("store.execute(query)")]
        self.assertIn("后台唤醒带着错误", body)

    # ── ③ 两个都要
    def test_observer_and_background_delivery_are_both_present(self):
        self.assertIn("HKObserverQuery(", REPORTER)
        self.assertIn("store.execute(query)", REPORTER)
        self.assertIn("enableBackgroundDelivery(", REPORTER)
        # 顺序：先挂查询再开投递。反了的话第一次唤醒可能没人接。
        self.assertLess(
            REPORTER.index("store.execute(query)"),
            REPORTER.index("enableBackgroundDelivery("))

    def test_double_registration_is_guarded(self):
        # 挂两次会收到两份唤醒，也就会重复上报。
        self.assertIn("guard !observing else { return }", REPORTER)
        self.assertIn("observing = true", REPORTER)

    def test_failure_to_enable_is_reported(self):
        # "没挂上"必须留下原因：否则表现只是"以后再也不上报"，无从查起。
        self.assertIn("后台投递没挂上", REPORTER)
        self.assertIn("后台投递已挂上", REPORTER)

    def test_foreground_path_still_exists(self):
        # 后台投递是补充，不是替代：打开 App 时那次立刻上报要留着，
        # 否则刚装上还没等到系统唤醒的这段时间里什么都没有。
        self.assertIn("await ReaderSleepReporter.shared.refresh()", DELEGATE)


if __name__ == "__main__":
    unittest.main()
