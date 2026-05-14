"""
pending_notes.py 在无待登记笔记时应该干净退出（退出码 0 + 含"共需处理 0 篇"）。
这是 register_notes "fast path" 的依赖：如果 pending_notes 异常，daily timer 第一步就挂。
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_DIR / "scripts" / "pending_notes.py"


class PendingNotesSmokeTest(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.exists(), f"找不到 {SCRIPT}")

    def test_runs_clean_with_correct_output_format(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_DIR),
        )
        self.assertEqual(r.returncode, 0, f"非零退出码 {r.returncode}\nstderr:\n{r.stderr}")

        out = r.stdout
        # 输出格式契约（control / daily 依赖这些 marker）
        self.assertIn("新笔记",    out, "输出缺少『新笔记』段")
        self.assertIn("已修改",    out, "输出缺少『已修改』段")
        self.assertIn("共需处理",  out, "输出缺少『共需处理』汇总")
        # 上次扫描时间这一行（如果有 last_scan）— 不强制，因为新机器可能没有

    def test_idempotent_run_does_not_corrupt_state(self) -> None:
        """pending_notes 是只读扫描，多次跑结果应一致。"""
        runs = [
            subprocess.run([sys.executable, str(SCRIPT)],
                          capture_output=True, text=True, timeout=30,
                          cwd=str(PROJECT_DIR))
            for _ in range(2)
        ]
        for r in runs:
            self.assertEqual(r.returncode, 0)
        # 「共需处理 N 篇」N 应该两次一致
        def total(out: str) -> str:
            for line in out.splitlines():
                if line.startswith("共需处理"):
                    return line
            return ""
        self.assertEqual(total(runs[0].stdout), total(runs[1].stdout),
                         "两次扫描结果不一致，pending_notes 可能不是幂等的")


if __name__ == "__main__":
    unittest.main()
