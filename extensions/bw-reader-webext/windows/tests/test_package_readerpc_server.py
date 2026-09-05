from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

import package_readerpc_server as module  # noqa: E402


class VerifyRunningGenerationTests(unittest.TestCase):
    """2026-08-24 实锤的静默失败：--install --launch 后新实例拒绝接管自退，
    旧版本继续跑，而 Popen 返回成功、心跳由旧进程刷新 —— 验证必须落在
    "所有进程都来自新 release" 上。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.release = Path(self.temporary.name) / "releases" / "0.9.9"
        self.release.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_passes_when_all_processes_are_from_the_new_release(self) -> None:
        exe = str(self.release / "ReaderPC-Server.exe")
        module.verify_running_generation(
            self.release,
            probe=lambda: [exe, exe],
            sleeper=lambda seconds: None,
            timeout_seconds=1.0,
        )

    def test_fails_loudly_when_an_old_generation_survives(self) -> None:
        old = str(self.release.parent / "0.1.62" / "ReaderPC-Server.exe")
        calls = {"n": 0}

        def sleeper(seconds: float) -> None:
            calls["n"] += 1

        with self.assertRaises(module.PackageError):
            module.verify_running_generation(
                self.release,
                probe=lambda: [old],
                sleeper=sleeper,
                timeout_seconds=0.05,
            )

    def test_fails_loudly_when_no_process_survives_even_after_relaunch(self) -> None:
        relaunches = {"n": 0}

        def relauncher() -> None:
            relaunches["n"] += 1

        with self.assertRaises(module.PackageError) as caught:
            module.verify_running_generation(
                self.release,
                probe=lambda: [],
                sleeper=lambda seconds: None,
                timeout_seconds=0.05,
                relauncher=relauncher,
            )
        self.assertEqual(relaunches["n"], 1, "兜底只拉一次，不无限重试")
        self.assertIn("兜底拉起也没起来", str(caught.exception))

    def test_relaunches_once_when_the_new_instance_exited_after_refusing_takeover(self) -> None:
        """2026-09-06 一天撞两次：新实例拒绝接管自退、旧实例已按退出请求退出 →
        一个 ReaderPC 都不在，整栈下线。兜底 = 用稳定启动器再拉一次，拉起来了也要出声。"""
        exe = str(self.release / "ReaderPC-Server.exe")
        state = {"relaunched": False}

        def probe() -> list[str]:
            return [exe, exe] if state["relaunched"] else []

        def relauncher() -> None:
            state["relaunched"] = True

        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            module.verify_running_generation(
                self.release,
                probe=probe,
                sleeper=lambda seconds: None,
                timeout_seconds=0.05,
                relauncher=relauncher,
            )
        self.assertTrue(state["relaunched"])
        self.assertIn("WARN", out.getvalue(), "兜底成功也必须出声，接管失败的根因不能被吞掉")

    def test_does_not_relaunch_when_the_new_generation_is_already_running(self) -> None:
        exe = str(self.release / "ReaderPC-Server.exe")
        relaunches = {"n": 0}

        def relauncher() -> None:
            relaunches["n"] += 1

        module.verify_running_generation(
            self.release,
            probe=lambda: [exe],
            sleeper=lambda seconds: None,
            timeout_seconds=1.0,
            relauncher=relauncher,
        )
        self.assertEqual(relaunches["n"], 0)

    def test_old_generation_surviving_is_not_relaunched_over(self) -> None:
        """旧代际还在跑时不兜底：再拉一个只会又撞一次接管；照旧报错让人看日志。"""
        old = str(self.release.parent / "0.1.62" / "ReaderPC-Server.exe")
        relaunches = {"n": 0}

        def relauncher() -> None:
            relaunches["n"] += 1

        with self.assertRaises(module.PackageError):
            module.verify_running_generation(
                self.release,
                probe=lambda: [old],
                sleeper=lambda seconds: None,
                timeout_seconds=0.05,
                relauncher=relauncher,
            )
        self.assertEqual(relaunches["n"], 0)

    def test_waits_for_a_slow_handover_before_passing(self) -> None:
        exe = str(self.release / "ReaderPC-Server.exe")
        old = str(self.release.parent / "0.1.62" / "ReaderPC-Server.exe")
        states = [[old, exe], [old, exe], [exe]]

        def probe() -> list[str]:
            return states.pop(0) if states else [exe]

        module.verify_running_generation(
            self.release,
            probe=probe,
            sleeper=lambda seconds: None,
            timeout_seconds=5.0,
        )


class RuntimeSourceListsAgreeTest(unittest.TestCase):
    """两份清单必须对齐 —— 打进包里 ≠ 铺到运行位置。

    2026-08-29 voip_push.py 只加了第一份：包里有、运行位置没有，而调用方是
    `except ImportError: return 0`，于是 deliver=call **静默地永远不响**。
    2026-09-05 board_card_render.py 又踩一次（表现是"渲染脚本缺失"，
    这次因为有出声的诊断，一条日志就看见了）。
    所以这条契约直接比对两份清单本身。
    """

    def test_every_flat_runtime_source_is_also_installed_to_the_stable_path(self):
        source = Path(module.__file__).read_text("utf-8")
        block = source[source.index("for stable_name in ("):]
        stable = block[:block.index(")")]
        missing = []
        for key in module.RUNTIME_SOURCES:
            prefix, _, name = key.partition("/")
            if prefix != "readerpc-runtime" or "/" in name:
                continue   # scripts/ 子目录那几个复制口径不同，单列在下面
            if '"' + name + '"' not in stable:
                missing.append(name)
        self.assertEqual(
            missing, [],
            "这些运行时源码打进了包却没铺到 %LOCALAPPDATA%/BWReader：" + repr(missing))


if __name__ == "__main__":
    unittest.main()
