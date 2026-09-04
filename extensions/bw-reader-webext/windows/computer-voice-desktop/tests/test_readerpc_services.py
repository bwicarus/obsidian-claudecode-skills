from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

import readerpc_services  # noqa: E402
from readerpc_services import (  # noqa: E402
    EXIT_REQUEST_CONTRACT,
    EXIT_REQUEST_MAX_SECONDS,
    PC_OCR_STATUS_CONTRACT,
    ManagedProcessController,
    ManagedServiceSpec,
    PcOcrPaths,
    PcOcrServiceController,
    ReaderPCPaths,
    ReaderPCServiceError,
    clear_readerpc_exit_request,
    default_server_services,
    read_readerpc_exit_request,
    write_readerpc_exit_request,
    read_codex_voice_activity,
    read_reader_context_status,
    write_readerpc_status,
)


class FakeProbe:
    def __init__(self, values: dict[int, int | None]):
        self.values = values

    def start_file_time_utc(self, pid: int) -> int | None:
        return self.values.get(pid)


class ReaderPCExitRequestTests(unittest.TestCase):
    """带外退出请求（2026-09-05）。

    换代接管原本只有 `taskkill /PID`（WM_CLOSE）一条路，而 ReaderPC 收进托盘后
    **没有顶层窗口**（实测 MainWindowHandle = 0），WM_CLOSE 无处可去、taskkill 却
    返回 0 —— 等 60s 超时、拒绝接管，新旧两代同时在跑。这个类钉的是这条补充通道
    **不能反过来把新实例弄死**。
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = ReaderPCPaths(
            local_root=root,
            status_file=root / "status.json",
            preferences_file=root / "config.json",
        )

    def test_live_request_is_honoured_then_cleared(self):
        # 退出请求是**别的进程**写给我的;自己写的那条永远不认(见下面那条契约)。
        write_readerpc_exit_request(
            self.paths, "新一代 ReaderPC 正在接管", pid=os.getpid() + 1)
        self.assertEqual(
            read_readerpc_exit_request(
                self.paths, pid_alive=lambda pid: True),
            "新一代 ReaderPC 正在接管",
        )
        payload = json.loads(self.paths.exit_request_file.read_text("utf-8"))
        self.assertEqual(payload["contract"], EXIT_REQUEST_CONTRACT)
        clear_readerpc_exit_request(self.paths)
        self.assertIsNone(read_readerpc_exit_request(self.paths))
        clear_readerpc_exit_request(self.paths)   # 再删一次不许抛

    def test_a_request_written_by_this_process_is_never_consumed(self):
        # 2026-09-05 实锤：接管方在自己进程里写这条请求去请旧实例退场；
        # 旧实例先走（WM_CLOSE 比刷新循环快）时请求留在原地，
        # 新实例下一次刷新读到的是**自己写的**那条 → 刚接管完就自杀，
        # 整台机器的 ReaderPC/桥/Flask 一起没了。按 pid 认自己，直接不认。
        write_readerpc_exit_request(self.paths, "接管")
        self.assertIsNone(read_readerpc_exit_request(self.paths))
        # 别人写的仍然照认
        write_readerpc_exit_request(self.paths, "接管", pid=os.getpid() + 1)
        self.assertEqual(
            read_readerpc_exit_request(self.paths, pid_alive=lambda pid: True),
            "接管")

    def test_expired_request_is_ignored(self):
        # 赖在原地的请求会让**新**实例一启动就自杀，表现是"双击没反应"。
        write_readerpc_exit_request(
            self.paths, "接管", ttl_seconds=10.0, clock=lambda: 500.0,
            pid=os.getpid() + 1,
        )
        self.assertEqual(
            read_readerpc_exit_request(
                self.paths, clock=lambda: 509.0, pid_alive=lambda pid: True),
            "接管",
        )
        self.assertIsNone(
            read_readerpc_exit_request(
                self.paths, clock=lambda: 511.0, pid_alive=lambda pid: True)
        )

    def test_request_from_a_dead_process_is_ignored(self):
        write_readerpc_exit_request(self.paths, "接管", pid=4242)
        self.assertIsNone(
            read_readerpc_exit_request(self.paths, pid_alive=lambda pid: False)
        )
        self.assertEqual(
            read_readerpc_exit_request(self.paths, pid_alive=lambda pid: True),
            "接管",
        )

    def test_foreign_or_broken_payload_never_asks_for_exit(self):
        for payload in (
            "{",
            json.dumps({"contract": "other/1", "reason": "x", "pid": 1,
                        "expiresAtEpoch": 9e18}),
            json.dumps({"contract": EXIT_REQUEST_CONTRACT, "reason": "",
                        "pid": 1, "expiresAtEpoch": 9e18}),
            json.dumps({"contract": EXIT_REQUEST_CONTRACT, "reason": "x",
                        "pid": 0, "expiresAtEpoch": 9e18}),
            json.dumps({"contract": EXIT_REQUEST_CONTRACT, "reason": "x",
                        "pid": 1}),
        ):
            self.paths.exit_request_file.write_text(payload, encoding="utf-8")
            self.assertIsNone(read_readerpc_exit_request(self.paths), payload)

    def test_missing_file_and_bounded_ttl(self):
        self.assertIsNone(read_readerpc_exit_request(self.paths))
        with self.assertRaises(ReaderPCServiceError):
            write_readerpc_exit_request(
                self.paths, "太久", ttl_seconds=EXIT_REQUEST_MAX_SECONDS + 1
            )
        with self.assertRaises(ReaderPCServiceError):
            write_readerpc_exit_request(self.paths, "")


class ReaderPCServicesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        python = self.root / "venv" / "python.exe"
        worker = self.root / "project" / "scripts" / "reader_pc_preprocess_worker.py"
        python.parent.mkdir(parents=True)
        worker.parent.mkdir(parents=True)
        python.write_bytes(b"python")
        worker.write_text("pass\n", "utf-8")
        self.paths = PcOcrPaths(
            local_root=self.root,
            cache_root=self.root / "cache",
            status_file=self.root / "cache" / "worker-status.json",
            stdout_log=self.root / "logs" / "out.log",
            stderr_log=self.root / "logs" / "err.log",
            python_exe=python,
            project_root=self.root / "project",
            worker_script=worker,
            doclayout_model=self.root / "models" / "layout.pt",
            unimernet_model_dir=self.root / "models" / "unimernet",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_worker_is_online_only_for_exact_pid_generation(self) -> None:
        self.paths.status_file.parent.mkdir(parents=True)
        self.paths.status_file.write_text(
            json.dumps(
                {
                    "contract": PC_OCR_STATUS_CONTRACT,
                    "state": "idle",
                    "phase": "preparing",
                    "processId": 4242,
                    "processStartFileTimeUtc": 9001,
                    "gpu": {"deviceName": "RTX"},
                }
            ),
            "utf-8",
        )
        online = PcOcrServiceController(
            self.paths,
            process_probe=FakeProbe({4242: 9001}),
        ).status()
        stale = PcOcrServiceController(
            self.paths,
            process_probe=FakeProbe({4242: 9002}),
        ).status()
        self.assertTrue(online.running)
        self.assertEqual(online.state_label, "在线 · 空闲")
        self.assertFalse(stale.running)
        self.assertEqual(stale.state, "stale")

    def test_context_freshness_uses_received_timestamp(self) -> None:
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(
            json.dumps(
                {
                    "schema": "reader-context-snapshot/1",
                    "activeReading": {
                        "kind": "pdf",
                        "title": "book",
                        "receivedAtEpochMs": 100_000,
                    },
                }
            ),
            "utf-8",
        )
        fresh = read_reader_context_status(snapshot, now_epoch_ms=120_000)
        stale = read_reader_context_status(snapshot, now_epoch_ms=150_000)
        self.assertTrue(fresh.fresh)
        self.assertEqual(fresh.title, "book")
        self.assertFalse(stale.fresh)

    def test_codex_voice_activity_uses_exact_microphone_ledger_semantics(self) -> None:
        active = read_codex_voice_activity(lambda: (101, 100))
        stopped = read_codex_voice_activity(lambda: (101, 102))
        never_started = read_codex_voice_activity(lambda: (0, 0))
        self.assertEqual((active.status, active.active), ("available", True))
        self.assertEqual(active.generation, 101)
        self.assertEqual((stopped.status, stopped.active), ("available", False))
        self.assertIsNone(stopped.generation)
        self.assertEqual(
            (never_started.status, never_started.active),
            ("available", False),
        )
        self.assertIsNone(never_started.generation)

    def test_codex_voice_activity_distinguishes_unavailable_and_invalid(self) -> None:
        unavailable = read_codex_voice_activity(lambda: None)
        invalid = read_codex_voice_activity(lambda: (True, 0))
        denied = read_codex_voice_activity(
            lambda: (_ for _ in ()).throw(PermissionError("denied"))
        )
        self.assertEqual((unavailable.status, unavailable.active), ("unavailable", None))
        self.assertEqual((invalid.status, invalid.active), ("error", None))
        self.assertEqual((denied.status, denied.active), ("error", None))

    def test_unified_status_contains_no_process_paths_or_tokens(self) -> None:
        self.paths.status_file.parent.mkdir(parents=True)
        self.paths.status_file.write_text(
            json.dumps(
                {
                    "contract": PC_OCR_STATUS_CONTRACT,
                    "state": "idle",
                    "phase": "preparing",
                    "processId": 7,
                    "processStartFileTimeUtc": 11,
                    "workerId": "pc_test",
                }
            ),
            "utf-8",
        )
        pc = PcOcrServiceController(
            self.paths,
            process_probe=FakeProbe({7: 11}),
        ).status()
        context_path = self.root / "missing.json"
        context = read_reader_context_status(context_path)
        output = self.root / "readerpc.json"
        write_readerpc_status(
            output,
            voice={"online": True, "reason": "reader-connected"},
            context=context,
            pc_ocr=pc,
        )
        text = output.read_text("utf-8")
        self.assertIn('"contract": "readerpc-server-status/1"', text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("token", text.casefold())

    def test_supervised_worker_command_recycles_after_each_job(self) -> None:
        captured: list[str] = []
        probe = FakeProbe({})

        class Process:
            def poll(self):
                return None

        def popen(command, **_kwargs):
            captured.extend(command)
            probe.values[4242] = 9001
            self.paths.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.paths.status_file.write_text(
                json.dumps(
                    {
                        "contract": PC_OCR_STATUS_CONTRACT,
                        "state": "idle",
                        "phase": "preparing",
                        "processId": 4242,
                        "processStartFileTimeUtc": 9001,
                    }
                ),
                "utf-8",
            )
            return Process()

        status = PcOcrServiceController(
            self.paths,
            process_probe=probe,
            popen=popen,
        ).start()

        self.assertTrue(status.running)
        self.assertIn("--recycle-after-job", captured)


class ManagedProcessControllerTests(unittest.TestCase):
    """三守护合一第一步:通用受管进程控制器(影子模式下只观测,托管时拉起/保活)。"""

    def _spec(self, root: Path) -> "ManagedServiceSpec":
        return ManagedServiceSpec(
            name="webapp", label="Flask", command=("python", "app.py"),
            cwd=root, port=5000, log_file=root / "logs" / "flask.log", env={"A": "1"},
        )

    def test_status_reports_external_process_as_reachable_but_not_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = ManagedProcessController(
                self._spec(Path(tmp)), popen=lambda *a, **k: self.fail("不该拉起"),
                reachable=lambda port: True,
            )
            status = controller.status()
            self.assertTrue(status.reachable)
            self.assertFalse(status.owned)
            # 端口被别人(旧守护)占着:start 不抢,只报告
            self.assertFalse(controller.start().owned)

    def test_start_waits_for_port_and_stop_kills_owned_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reachable = {"value": False}
            captured: dict[str, object] = {}

            class Process:
                pid = 4321
                returncode = None
                def poll(self):
                    return self.returncode
                def wait(self, timeout=None):
                    self.returncode = 0
                def terminate(self):
                    self.returncode = 0
                def kill(self):
                    self.returncode = -9

            def popen(command, **kwargs):
                captured["command"] = list(command)
                captured["cwd"] = kwargs.get("cwd")
                captured["env"] = kwargs.get("env")
                reachable["value"] = True
                return Process()

            clock = {"t": 0.0}
            controller = ManagedProcessController(
                self._spec(Path(tmp)), popen=popen,
                reachable=lambda port: reachable["value"],
                clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            )
            status = controller.start()
            self.assertTrue(status.owned)
            self.assertEqual(status.pid, 4321)
            self.assertEqual(captured["command"], ["python", "app.py"])
            self.assertEqual(captured["env"], {"A": "1"})
            self.assertTrue((Path(tmp) / "logs" / "flask.log").exists())
            # 进程死了 → ensure 在退避后重拉,restarts 递增
            controller._process.returncode = 1
            reachable["value"] = False
            clock["t"] += 30
            self.assertTrue(controller.ensure().owned)
            self.assertEqual(controller.status().restarts, 2)

    def test_start_failure_when_process_exits_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class Dead:
                pid = 1
                returncode = 3
                def poll(self):
                    return 3
            controller = ManagedProcessController(
                self._spec(Path(tmp)), popen=lambda *a, **k: Dead(), reachable=lambda port: False,
            )
            with self.assertRaises(ReaderPCServiceError):
                controller.start()
            self.assertIn("立即退出", controller.status().error or "")

    def test_default_specs_require_a_server_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(default_server_services(root), [])
            (root / "_server_deploy").mkdir()
            (root / "_server_deploy" / "app.py").write_text("", "utf-8")
            (root / ".env.local").write_text("APP_PYTHON=C:/py/python.exe\n", "utf-8")
            specs = default_server_services(root)
            self.assertEqual([s.name for s in specs], ["webapp", "voice-rt", "watch-voice", "rbi", "mcp"])
            self.assertEqual(specs[0].command[0], "C:/py/python.exe")
            self.assertEqual(specs[0].port, 5000)
            # 状态文件带 services 子树,且不含进程路径/凭据
            status = ManagedProcessController(specs[0], reachable=lambda port: False).status()
            self.assertEqual(status.to_public()["reachable"], False)
            self.assertNotIn("command", status.to_public())


if __name__ == "__main__":
    unittest.main()


class ManagedProcessControllerSideEffectTests(unittest.TestCase):
    """步骤 2:旧 Flask 守护的两项副作用 + 熔断,搬进受管控制器后要有人盯着。"""

    def _controller(self, tmp, *, watch=("*.py",), reachable_flags=None):
        from readerpc_services import ManagedProcessController, ManagedServiceSpec

        deploy = Path(tmp) / "_server_deploy"
        deploy.mkdir()
        (deploy / "app.py").write_text("print('x')\n", encoding="utf-8")
        spec = ManagedServiceSpec(
            "webapp", "Flask", ("python", "app.py"), deploy, 5000,
            Path(tmp) / "logs" / "flask.log", None, watch_globs=watch,
        )
        started: list[object] = []

        class Process:
            def __init__(self):
                self.pid = 4242
                self._alive = True
                self.returncode = None

            def poll(self):
                return None if self._alive else self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self._alive = False
                self.returncode = -9

        def popen(*args, **kwargs):
            proc = Process()
            started.append(proc)
            return proc

        state = {"reachable": False}
        clock = {"now": 100.0}

        def killer(cmd, **kwargs):
            # taskkill /F /T /PID:模拟进程被杀
            for proc in started:
                proc._alive = False
                proc.returncode = 1
            return SimpleNamespace(returncode=0)

        controller = ManagedProcessController(
            spec,
            popen=popen,
            reachable=lambda port: state["reachable"] or bool(started and started[-1]._alive),
            clock=lambda: clock["now"],
            sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        )
        return controller, started, state, clock, killer, deploy

    def test_code_change_restarts_only_owned_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller, started, state, clock, killer, deploy = self._controller(tmp)
            # 影子模式:端口被别人占着 → 就算代码变了也不动
            state["reachable"] = True
            (deploy / "app.py").write_text("print('y')\n", encoding="utf-8")
            self.assertIsNone(controller.restart_if_code_changed())
            self.assertEqual(started, [])
            state["reachable"] = False
            controller.start()
            self.assertEqual(len(started), 1)
            self.assertIsNone(controller.code_changed(), "刚启动时快照应与磁盘一致")
            (deploy / "app.py").write_text("print('z')\n", encoding="utf-8")
            import os as _os
            _os.utime(deploy / "app.py", (2_000_000_000, 2_000_000_000))
            with patch("readerpc_services.subprocess.run", killer):
                self.assertEqual(controller.restart_if_code_changed(), "app.py")
            self.assertEqual(len(started), 2, "变更后应停旧起新")
            self.assertIsNone(controller.code_changed(), "重启后快照刷新")

    def test_fast_fail_halts_after_limit_and_reset_clears(self):
        from readerpc_services import MANAGED_FAST_FAIL_LIMIT

        with tempfile.TemporaryDirectory() as tmp:
            controller, started, state, clock, killer, deploy = self._controller(tmp, watch=())
            controller.start()
            for _ in range(MANAGED_FAST_FAIL_LIMIT):
                started[-1]._alive = False
                started[-1].returncode = 3
                clock["now"] += 1.0        # 起来 1 秒就死 = 快失败
                controller.ensure(backoff_seconds=0.0)
            status = controller.status()
            self.assertTrue(status.halted, status)
            self.assertIn("熔断", status.error or "")
            self.assertTrue(status.to_public()["halted"])
            before = len(started)
            clock["now"] += 100.0
            controller.ensure(backoff_seconds=0.0)
            self.assertEqual(len(started), before, "熔断后不再自动拉起")
            controller.reset()
            controller.ensure(backoff_seconds=0.0)
            self.assertEqual(len(started), before + 1, "reset 后恢复拉起")

    def test_scheduled_task_watch_enables_and_starts(self):
        calls: list[list[str]] = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            if "Get-ScheduledTask" in cmd[-1]:
                return SimpleNamespace(stdout="Disabled\n", returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        outcome = readerpc_services.ensure_scheduled_task_running("Obsidian Headless Sync", runner=runner)
        if sys.platform != "win32":
            self.assertEqual(outcome, "absent")
            return
        self.assertEqual(outcome, "enabled+started")
        self.assertIn("Enable-ScheduledTask -TaskName 'Obsidian Headless Sync'", calls[1][-1])
        self.assertIn("Start-ScheduledTask -TaskName 'Obsidian Headless Sync'", calls[1][-1])

        def running(cmd, **kwargs):
            return SimpleNamespace(stdout="Running\n", returncode=0)

        self.assertEqual(readerpc_services.ensure_scheduled_task_running("X", runner=running), "running")

        def absent(cmd, **kwargs):
            return SimpleNamespace(stdout="", returncode=0)

        self.assertEqual(readerpc_services.ensure_scheduled_task_running("X", runner=absent), "absent")
        self.assertEqual(readerpc_services.OBSIDIAN_SYNC_TASK_NAME, "Obsidian Headless Sync")
