"""entitlement 必须配上用途说明 —— 否则只在**上传那一步**才失败。

2026-09-08 这是同类失败的第三次。共同形态：编译过、归档过、altool 的本地
校验也过，直到 App Store 的上传校验才报错，而那时一整轮 CI（十几分钟）已经花掉。
前两次是手表 app 放进 PlugIns/、WKBackgroundModes 填 audio；这次是
`com.apple.developer.healthkit` 缺 NSHealthUpdateUsageDescription（run 34229274644，
90683），结果睡眠上报功能从未上过设备。

⚠ 判据是 **entitlement**，不是代码。我们的 ReaderSleepReporter 只读
（`toShare: []`），但 healthkit 这个 entitlement 本身允许写，苹果就据此
要求写权限的用途说明。所以「我们不写所以不用写这条」是错的。

CI 里有一份同样的检查（构建产物上跑）。这份跑在源码上，为的是在推之前就知道。
"""
from pathlib import Path
import plistlib
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ios" / "BWReader" / "App"

#: entitlement → 它要求的用途说明键。加新 entitlement 时同步这张表。
REQUIRED_PURPOSE_STRINGS = {
    "com.apple.developer.healthkit": (
        "NSHealthShareUsageDescription",
        "NSHealthUpdateUsageDescription",
    ),
    # 后台投递本身不额外要用途说明，但它同样属于"碰健康数据"。
    # 单独列一条是为了防一种改动：有人把 healthkit 那条去掉却留着这条，
    # 于是上面的检查不再触发，而苹果照样会要那两句话。
    "com.apple.developer.healthkit.background-delivery": (
        "NSHealthShareUsageDescription",
        "NSHealthUpdateUsageDescription",
    ),
}


class PurposeStringTests(unittest.TestCase):
    def setUp(self):
        with open(APP / "Info.plist", "rb") as handle:
            self.info = plistlib.load(handle)
        with open(APP / "BWReader.entitlements", "rb") as handle:
            self.entitlements = plistlib.load(handle)

    def test_every_entitlement_has_its_purpose_strings(self):
        for entitlement, keys in REQUIRED_PURPOSE_STRINGS.items():
            if not self.entitlements.get(entitlement):
                continue
            for key in keys:
                with self.subTest(entitlement=entitlement, key=key):
                    self.assertIn(
                        key, self.info,
                        "entitlement %s 要求 Info.plist 有 %s；"
                        "漏掉它编译和归档都会过，上传时才报 90683"
                        % (entitlement, key))

    def test_purpose_strings_are_real_sentences(self):
        # 空字符串或占位符会被人工审核打回，而那比 CI 失败慢得多。
        for key, value in self.info.items():
            if not key.endswith("UsageDescription"):
                continue
            with self.subTest(key=key):
                self.assertIsInstance(value, str)
                self.assertGreaterEqual(
                    len(value.strip()), 10,
                    "%s 的说明太短，审核会打回" % key)
                self.assertNotIn("TODO", value)

    def test_ci_guard_exists_for_the_same_pairing(self):
        # 这份测试要人来跑；CI 那份是最后一道闸。两份都要在。
        workflow = (ROOT / ".github" / "workflows"
                    / "safari-extension-ios.yml").read_text(encoding="utf-8")
        self.assertIn("NSHealthUpdateUsageDescription", workflow)
        self.assertIn("90683", workflow)


if __name__ == "__main__":
    unittest.main()
