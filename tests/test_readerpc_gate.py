"""后台任务总闸(2026-09-08 用户:「pc 服务器软件退出后真的能把所有相关功能都停下」)。

ReaderPC 的退出流程管得住它自己拉起的东西,管不住 Windows 计划任务 ——
关掉之后每 15 分钟的同步、每晚的词典刷新照跑,昨天凌晨的卡顿就是这么来的。
"""
from pathlib import Path
import json
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import readerpc_gate as gate  # noqa: E402


class ReaderPCGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _heartbeat(self, age_seconds: float):
        (self.root / gate.STATUS_NAME).write_text(json.dumps(
            {"updatedAtEpochMs": int((time.time() - age_seconds) * 1000)}), encoding="utf-8")

    def test_fresh_heartbeat_runs(self):
        self._heartbeat(5)
        active, reason = gate.readerpc_active(self.root)
        self.assertTrue(active)
        self.assertIn("在跑", reason)

    def test_stale_heartbeat_skips(self):
        self._heartbeat(gate.DEFAULT_MAX_STALE_SECONDS + 60)
        active, reason = gate.readerpc_active(self.root)
        self.assertFalse(active, "心跳停了就别再占机器")
        self.assertIn("心跳", reason)

    def test_user_quit_skips_even_with_fresh_heartbeat(self):
        self._heartbeat(1)
        (self.root / gate.EXIT_MARKER_NAME).write_text("{}", encoding="utf-8")
        with mock.patch.object(gate, "_boot_time", return_value=time.time() - 3600):
            active, reason = gate.readerpc_active(self.root)
        self.assertFalse(active, "他主动关了就别替他开回来")
        self.assertIn("主动退出", reason)

    def test_exit_marker_older_than_boot_is_ignored(self):
        """关机时系统也会写标记;陈旧标记不该让后台任务永久停摆。"""
        self._heartbeat(1)
        (self.root / gate.EXIT_MARKER_NAME).write_text("{}", encoding="utf-8")
        with mock.patch.object(gate, "_boot_time", return_value=time.time() + 60):
            active, _ = gate.readerpc_active(self.root)
        self.assertTrue(active)

    def test_unknown_state_runs_rather_than_silently_stopping(self):
        """判不出来一律放行:守卫是为省资源,不是制造"任务神秘不执行"。"""
        active, reason = gate.readerpc_active(self.root)   # 空目录,什么都没有
        self.assertTrue(active)
        self.assertIn("按在跑处理", reason)

    def test_cli_exit_code_distinguishes_skip_from_failure(self):
        """跳过用 10:用 1 会和"脚本自己出错"混在一起,计划任务的失败记录就没意义了。"""
        with mock.patch.object(gate, "readerpc_active", return_value=(True, "ok")):
            self.assertEqual(gate._main(), 0)
        with mock.patch.object(gate, "readerpc_active", return_value=(False, "quit")):
            self.assertEqual(gate._main(), 10)

    def test_both_scheduled_tasks_are_gated(self):
        for name in ("kj_anki_sync.cmd", "jp_dict_refresh.cmd"):
            text = (ROOT / "bin" / name).read_text(encoding="utf-8")
            self.assertIn("readerpc_gate.py", text, name + " 少了守卫")
            self.assertIn("if errorlevel 10 exit /b 0", text, name + " 没按退出码跳过")
            # 路径别再被转义吃掉(第一次写成了 libeaderpc_gate.py)
            self.assertIn(r"scripts\lib\readerpc_gate.py", text)


if __name__ == "__main__":
    unittest.main()
