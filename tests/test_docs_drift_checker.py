"""`check_docs_drift.py` 自己的回归测试 —— 主要防的是"把误报调没了，顺手把召回也调没了"。

这条检查调判据时踩过一次：第一版报 44 处、几乎全是误报（markdown 表格行天然
重复路径）；第二版索性整类排除表格行并要求含中文标点，误报归零 —— 但拿真实
坏行一测**只剩 4/8**，而且漏掉的恰好包括一整行坏掉的表格。干净的代价是它不再干活。

所以夹具里两组都必须在：**坏行组**（2026-08-19 那批"只替换前缀、旧尾巴留在
原地"的真实原文，逐字保留）和**对照组**（正常文档里天然的重复：环境对照表、
自指链接 `[`x`](x)`、路由归属表）。调判据可以，两组的数字不许退。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_docs_drift as drift   # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "docs_drift_mangled.md"


class DuplicatedSpanTest(unittest.TestCase):
    """行内重复：坏行必须全中，正常重复必须一个都不报。"""

    def setUp(self) -> None:
        self._saved = drift._SCAN_ALL
        # 夹具不在 git 改动集里，测试要绕过"只看本次改动行"的默认范围
        drift._SCAN_ALL = True
        lines = FIXTURE.read_text("utf-8").splitlines()
        self.lines = lines
        self.control_start = next(
            i for i, line in enumerate(lines, 1) if line.startswith("## 对照组")
        )

    def tearDown(self) -> None:
        drift._SCAN_ALL = self._saved

    def test_catches_every_real_mangled_line(self) -> None:
        hit = {f.line_no for f in drift.check_duplicated_span([FIXTURE])}
        caught = sorted(n for n in hit if n < self.control_start)
        expected = [
            n for n, line in enumerate(self.lines[: self.control_start - 1], 1)
            if line.startswith(("- ", "不依赖", "改完", "实现:", "后端", "| **", "> - "))
            and len(line) > 200
        ]
        missing = [n for n in expected if n not in caught]
        self.assertFalse(
            missing,
            "这些真实坏行没被抓到 —— 判据收得过头了：\n"
            + "\n".join(f"  第 {n} 行：{self.lines[n - 1][:70]}" for n in missing),
        )

    def test_never_flags_ordinary_repetition(self) -> None:
        hit = {f.line_no for f in drift.check_duplicated_span([FIXTURE])}
        wrong = sorted(n for n in hit if n >= self.control_start)
        self.assertFalse(
            wrong,
            "对照组被误报 —— 正常文档里的重复不是症状：\n"
            + "\n".join(f"  第 {n} 行：{self.lines[n - 1][:70]}" for n in wrong),
        )


class FactsComeFromCodeTest(unittest.TestCase):
    """判据必须从代码取事实，不能硬编码一份"正确答案"——否则脚本自己会过期。"""

    def test_profile_version_is_read_from_worker(self) -> None:
        worker = ROOT / "_server_deploy" / "reader_book_ocr_worker.py"
        if not worker.exists():
            self.skipTest("worker 不在这个 checkout 里")
        source = (ROOT / "scripts" / "check_docs_drift.py").read_text("utf-8")
        self.assertTrue("PROCESSING_PROFILES" in source,
                        "profile 检查应当去 worker 源码读当前值")
        # 用 assertTrue 而不是 assertNotIn：后者失败时会把整份源码打进报告，
        # 几百行淹掉真正的那句话，等于失败信息不可读。
        #
        # 只认"写死赋值"这一种形态。第一版写的是"源码里不许出现
        # quality-first-v"，结果把合法的 `re.findall(r"quality-first-v\d+")`
        # 也判红了 —— 匹配版本号是这条检查的正事，禁的是**把答案钉死**。
        import re as _re
        hardcoded = _re.search(r'current\s*=\s*["\']quality-first-v', source)
        self.assertIsNone(
            hardcoded,
            "别把版本号写死在检查器里 —— 代码升版本时它自己就成了新的漂移源",
        )

    def test_local_route_detection_knows_both_dispatch_shapes(self) -> None:
        """两种分发形状缺一都会误判：只认第一种会把 highlights 判成走 Pi。"""
        source = (ROOT / "scripts" / "check_docs_drift.py").read_text("utf-8")
        self.assertIn("url.pathname === ", source)
        self.assertIn("path === ", source)

    def test_does_not_mistake_outbox_whitelist_for_local(self) -> None:
        """路径出现在 NATIVE_SYNC_BATCH_ENDPOINTS 里恰恰说明它**发去 Pi**。

        第一版判据用的是纯字面量包含，正好把这类判反 —— 注释必须留着，
        因为下一个来"简化"这段的人会重犯。
        """
        source = (ROOT / "scripts" / "check_docs_drift.py").read_text("utf-8")
        self.assertIn("NATIVE_SYNC_BATCH_ENDPOINTS", source)


class RepoIsCleanTest(unittest.TestCase):
    """仓库当前应当是零漂移的。有人写进新的旧说法，这里就红。"""

    def test_no_drift_in_committed_docs(self) -> None:
        targets = drift.DOCS + drift.SKILLS
        found = []
        for name, fn in drift.CHECKS:
            if name == "行内重复":
                continue          # 它按改动行判，跟仓库整体状态无关
            found.extend(fn(targets))
        self.assertFalse(
            found,
            "文档漂回了旧架构：\n" + "\n".join(f"  {f}" for f in found[:20]),
        )


if __name__ == "__main__":
    unittest.main()
