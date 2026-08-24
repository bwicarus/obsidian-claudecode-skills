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

    def test_fails_loudly_when_no_process_survives(self) -> None:
        with self.assertRaises(module.PackageError):
            module.verify_running_generation(
                self.release,
                probe=lambda: [],
                sleeper=lambda seconds: None,
                timeout_seconds=0.05,
            )

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


if __name__ == "__main__":
    unittest.main()
