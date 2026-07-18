#!/usr/bin/env python3
"""daily smoke gate:跑 scripts/test_learning_loop.py 全套学习闭环回归(R3-G5 接入 unittest discover)。

daily 的 run_smoke_tests 会 `unittest discover tests`,于是这套回归**自动随 daily 跑**;
任一核心不变量回退(availability 假 open / 假绿 / proposal 可重放 / 回访误拉回 …)→ 本用例失败
→ run_smoke_tests 返回非零 → main() 在动 Anki 前 early-return(gate)。子脚本自身用临时 store 隔离。"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LearningLoopRegression(unittest.TestCase):
    def test_suite_passes(self):
        env = dict(os.environ)
        env.setdefault("CLAUDE_PROJECT", str(ROOT))
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "test_learning_loop.py")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        if r.returncode != 0:
            self.fail("学习闭环回归有失败项:\n" + (r.stdout or "")[-2500:] + "\n" + (r.stderr or "")[-500:])


if __name__ == "__main__":
    unittest.main()
