from __future__ import annotations

from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from readerpc_launcher import (  # noqa: E402
    ReaderPCWindow,
    disable_readerpc_voice,
    load_preferences,
    main,
    save_preferences,
    set_codex_voice_keep_active,
    start_readerpc_voice,
    stop_readerpc_services,
)


class ReaderPCLauncherTests(unittest.TestCase):
    @staticmethod
    def window_without_tk() -> ReaderPCWindow:
        window = ReaderPCWindow.__new__(ReaderPCWindow)
        window.closed = False
        window.closing = False
        window.busy = False
        window.root = Mock()
        window.footer = Mock()
        window.voice_button = Mock()
        window.pc_button = Mock()
        window.tray = Mock()
        window.events = queue.Queue()
        window.service_lock = threading.Lock()
        window.bridge_paths = Mock()
        window.process_runner = Mock()
        window.pc_ocr = Mock()
        window.last_voice_start_attempt = 0.0
        window.voice_recovery_in_progress = False
        return window

    def test_preferences_default_to_keep_pc_online(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "missing.json"
            self.assertEqual(
                load_preferences(path),
                {"keepPcPreprocessingOnline": True},
            )

    def test_preferences_round_trip_explicit_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "readerpc.json"
            save_preferences(path, keep_pc_online=False)
            self.assertEqual(
                load_preferences(path),
                {"keepPcPreprocessingOnline": False},
            )

    def test_invalid_preferences_fail_to_safe_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "readerpc.json"
            path.write_text('{"keepPcPreprocessingOnline":"yes"}', "utf-8")
            self.assertTrue(load_preferences(path)["keepPcPreprocessingOnline"])

    def test_codex_voice_keepalive_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime_status = Path(raw) / "runtime" / "status.json"
            bridge_paths = SimpleNamespace(runtime_status=runtime_status)
            set_codex_voice_keep_active(bridge_paths, True)
            self.assertEqual(
                (runtime_status.parent / "codex-voice-keepalive.json").read_text(
                    "utf-8"
                ),
                '{\n  "contract": "reader-codex-voice-keepalive/1",\n'
                '  "enabled": true\n}\n',
            )

    def test_disable_voice_clears_keepalive_and_stops_service(self) -> None:
        bridge_paths = Mock()
        process_runner = Mock()
        calls: list[object] = []
        with (
            patch(
                "readerpc_launcher.set_codex_voice_keep_active",
                side_effect=lambda _paths, enabled: calls.append(enabled),
            ),
            patch(
                "readerpc_launcher.disable_and_stop_direct_service",
                side_effect=lambda *_args: calls.append("stop"),
            ),
        ):
            disable_readerpc_voice(bridge_paths, process_runner)
        self.assertEqual(calls, [False, "stop"])

    def test_shutdown_stops_pc_and_direct_services(self) -> None:
        pc_ocr = Mock()
        status = SimpleNamespace(service_online=True, configuration_enabled=True)
        with (
            patch("readerpc_launcher.read_direct_status", return_value=status),
            patch("readerpc_launcher.disable_readerpc_voice") as stop_voice,
        ):
            stop_readerpc_services(Mock(), Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        stop_voice.assert_called_once()

    def test_shutdown_attempts_both_services_and_reports_failure(self) -> None:
        pc_ocr = Mock()
        pc_ocr.stop.side_effect = RuntimeError("pc-stop-failed")
        status = SimpleNamespace(service_online=True, configuration_enabled=True)
        with (
            patch("readerpc_launcher.read_direct_status", return_value=status),
            patch(
                "readerpc_launcher.disable_readerpc_voice",
                side_effect=RuntimeError("voice-stop-failed"),
            ) as stop_voice,
            self.assertRaisesRegex(
                RuntimeError,
                "pc-stop-failed.*voice-stop-failed",
            ),
        ):
            stop_readerpc_services(Mock(), Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        stop_voice.assert_called_once()

    def test_shutdown_skips_direct_stop_when_already_disabled(self) -> None:
        pc_ocr = Mock()
        status = SimpleNamespace(service_online=False, configuration_enabled=False)
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = False
        with (
            patch("readerpc_launcher.read_direct_status", return_value=status),
            patch("readerpc_launcher.set_codex_voice_keep_active") as keep_voice,
            patch("readerpc_launcher.disable_and_stop_direct_service") as stop_voice,
            patch("readerpc_launcher.stop_direct_service") as stop_orphan,
        ):
            stop_readerpc_services(bridge_paths, Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        keep_voice.assert_called_once_with(bridge_paths, False)
        stop_voice.assert_not_called()
        stop_orphan.assert_not_called()

    def test_shutdown_stops_owned_direct_record_after_opt_out(self) -> None:
        pc_ocr = Mock()
        status = SimpleNamespace(service_online=False, configuration_enabled=False)
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = True
        with (
            patch("readerpc_launcher.read_direct_status", return_value=status),
            patch("readerpc_launcher.set_codex_voice_keep_active") as keep_voice,
            patch("readerpc_launcher.disable_and_stop_direct_service") as disable_voice,
            patch("readerpc_launcher.stop_direct_service") as stop_orphan,
        ):
            stop_readerpc_services(bridge_paths, Mock(), pc_ocr)
        disable_voice.assert_not_called()
        keep_voice.assert_called_once_with(bridge_paths, False)
        stop_orphan.assert_called_once()

    def test_start_voice_sets_keepalive_before_starting_service(self) -> None:
        window = self.window_without_tk()
        calls: list[object] = []
        window._run_task = Mock(side_effect=lambda _pending, action, _success: action())
        with (
            patch(
                "readerpc_launcher.set_codex_voice_keep_active",
                side_effect=lambda _paths, enabled: calls.append(enabled),
            ),
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args: calls.append("configure") or True,
            ),
            patch(
                "readerpc_launcher.start_readerpc_voice",
                side_effect=lambda *_args: calls.append("start") or 4321,
            ),
        ):
            window._start_voice_task(recovery=False)
        self.assertEqual(calls, [True, "configure", "start"])

    def test_voice_start_requires_matching_fresh_runtime(self) -> None:
        runner = Mock()
        runner.executable_for_pid.return_value = Path("C:/voice.exe")
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = False
        offline = SimpleNamespace(
            service_online=False,
            pid=4321,
            reason="runtime-status-offline-or-stale",
        )
        online = SimpleNamespace(
            service_online=True,
            pid=4321,
            reason="reader-not-connected",
        )
        ticks = iter((0.0, 0.1, 0.2, 0.3))
        with (
            patch("readerpc_launcher.start_direct_service", return_value=4321),
            patch(
                "readerpc_launcher.read_direct_status",
                side_effect=(offline, online),
            ),
            patch("readerpc_launcher.stop_direct_service") as stop,
        ):
            self.assertEqual(
                start_readerpc_voice(
                    bridge_paths,
                    runner,
                    clock=lambda: next(ticks),
                    sleeper=lambda _seconds: None,
                ),
                4321,
            )
        stop.assert_not_called()

    def test_voice_start_reports_immediate_child_exit_and_clears_record(self) -> None:
        runner = Mock()
        runner.executable_for_pid.return_value = None
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = False
        offline = SimpleNamespace(
            service_online=False,
            pid=4321,
            reason="service-process-offline",
        )
        with (
            patch("readerpc_launcher.start_direct_service", return_value=4321),
            patch("readerpc_launcher.read_direct_status", return_value=offline),
            patch("readerpc_launcher.stop_direct_service") as stop,
            self.assertRaisesRegex(RuntimeError, "启动后立即退出.*配置可能不兼容"),
        ):
            start_readerpc_voice(bridge_paths, runner)
        stop.assert_called_once()

    def test_voice_retry_stops_faulted_owned_generation_before_restart(self) -> None:
        runner = Mock()
        runner.executable_for_pid.return_value = Path("C:/voice.exe")
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = True
        faulted = SimpleNamespace(
            service_online=False,
            pid=1111,
            reason="runtime-status-offline-or-stale",
        )
        online = SimpleNamespace(
            service_online=True,
            pid=2222,
            reason="reader-not-connected",
        )
        with (
            patch(
                "readerpc_launcher.read_direct_status",
                side_effect=(faulted, online),
            ),
            patch("readerpc_launcher.stop_direct_service") as stop,
            patch("readerpc_launcher.start_direct_service", return_value=2222),
        ):
            self.assertEqual(start_readerpc_voice(bridge_paths, runner), 2222)
        stop.assert_called_once_with(bridge_paths, runner)

    def test_request_exit_stops_services_before_destroying_window(self) -> None:
        window = self.window_without_tk()

        def immediate_thread(*, target, **_kwargs):
            return SimpleNamespace(start=target)

        with (
            patch("readerpc_launcher.threading.Thread", side_effect=immediate_thread),
            patch("readerpc_launcher.stop_readerpc_services") as stop_services,
        ):
            window.request_exit()
            self.assertFalse(window.root.destroy.called)
            window._drain_events()
        stop_services.assert_called_once_with(
            window.bridge_paths,
            window.process_runner,
            window.pc_ocr,
        )
        window.tray.stop.assert_called_once_with()
        window.root.destroy.assert_called_once_with()
        self.assertTrue(window.closed)

    def test_request_exit_failure_keeps_window_open(self) -> None:
        window = self.window_without_tk()

        def immediate_thread(*, target, **_kwargs):
            return SimpleNamespace(start=target)

        with (
            patch("readerpc_launcher.threading.Thread", side_effect=immediate_thread),
            patch(
                "readerpc_launcher.stop_readerpc_services",
                side_effect=RuntimeError("stop-failed"),
            ),
            patch("readerpc_launcher.messagebox.showerror") as show_error,
        ):
            window.request_exit()
            window._drain_events()
        window.root.destroy.assert_not_called()
        show_error.assert_called_once()
        self.assertFalse(window.closed)
        self.assertFalse(window.closing)

    def test_gui_owns_shortcut_broker_for_entire_window_lifetime(self) -> None:
        with (
            patch("readerpc_launcher.SingleInstance") as instance,
            patch("readerpc_launcher.WindowsShortcutBroker") as broker,
            patch("readerpc_launcher.tk.Tk") as make_root,
            patch("readerpc_launcher.ReaderPCWindow") as make_window,
        ):
            instance.return_value.acquire.return_value = True
            self.assertEqual(main([]), 0)

        broker.assert_called_once_with()
        broker.return_value.__enter__.assert_called_once_with()
        broker.return_value.__exit__.assert_called_once()
        make_window.assert_called_once_with(make_root.return_value)
        make_root.return_value.mainloop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
