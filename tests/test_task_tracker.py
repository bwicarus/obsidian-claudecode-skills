"""
task_tracker.Handle 在非 TTY 时把 update / log / set_summary 镜像 print 到 stdout。
这是 control 面板能从 webapp_trigger.log 看到子进程运行细节的基础——
回归会让 trigger 日志重新变成只有触发头无运行细节。
"""
from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path

# 让 tests/ 能 import scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class TaskTrackerMirrorTest(unittest.TestCase):
    def setUp(self) -> None:
        # 强制重 import task_tracker（_MIRROR_TO_STDOUT 是模块加载时算的）。
        # 同时把 sys.stdout 换成 StringIO，让 isatty() == False 触发镜像。
        self._stdout = sys.stdout
        self._buf = io.StringIO()
        sys.stdout = self._buf
        # 干掉缓存的 task_tracker，强制重新加载
        for mod in list(sys.modules):
            if mod == "task_tracker":
                del sys.modules[mod]
        import task_tracker  # noqa: F401
        self.tt = sys.modules["task_tracker"]

    def tearDown(self) -> None:
        sys.stdout = self._stdout

    def test_handle_mirrors_to_stdout_when_not_tty(self) -> None:
        # StringIO.isatty() → False，模块加载时应判断为镜像模式
        self.assertTrue(self.tt._MIRROR_TO_STDOUT, "非 TTY 时应启用 stdout 镜像")

        with self.tt.track("smoke-test", detail="unit test") as h:
            h.update("step-1")
            h.log("event-A")
            h.set_summary("done")

        output = self._buf.getvalue()
        self.assertIn("▶ step-1",  output, "update() 应该镜像为 '▶ <msg>'")
        self.assertIn("event-A",   output, "log() 应该原样镜像")
        self.assertIn("✓ done",    output, "set_summary() 应该镜像为 '✓ <msg>'")


class TaskTrackerStateFileTest(unittest.TestCase):
    """track() 上下文管理器应该在 active_tasks.json 里建条目，结束时移到 completed。"""

    def setUp(self) -> None:
        for mod in list(sys.modules):
            if mod == "task_tracker":
                del sys.modules[mod]
        import task_tracker
        self.tt = task_tracker

    def test_track_writes_and_clears_active_entry(self) -> None:
        with self.tt.track("test-active-flow") as h:
            h.update("running")
            # 上下文里：应该在 active 列表里能找到这个任务
            active = self.tt._load()
            names = [t.get("name") for t in active]
            self.assertIn("test-active-flow", names)

        # 退出后：active 列表不应再含此任务
        active_after = self.tt._load()
        names_after = [t.get("name") for t in active_after]
        self.assertNotIn("test-active-flow", names_after,
                         "track() 退出后应清理 active 条目")


if __name__ == "__main__":
    unittest.main()
