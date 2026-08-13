from __future__ import annotations

from datetime import datetime, timezone
import json
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
    enable_readerpc_voice,
    load_preferences,
    main,
    readerpc_history_sync_enabled,
    prepare_readerpc_shortcut_broker,
    save_preferences,
    set_codex_voice_keep_active,
    start_readerpc_voice,
    stop_readerpc_services,
    write_disabled_reader_context_snapshot,
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

    def test_history_sync_requires_explicit_snapshot_mode(self) -> None:
        paths = object()
        with patch(
            "readerpc_launcher.load_direct_config",
            side_effect=(
                None,
                {
                    "localOptIn": True,
                    "contextDeliveryMode": "legacy-inject",
                },
                {
                    "localOptIn": False,
                    "contextDeliveryMode": "snapshot-mcp",
                },
                {
                    "localOptIn": True,
                    "contextDeliveryMode": "snapshot-mcp",
                },
            ),
        ):
            self.assertFalse(readerpc_history_sync_enabled(paths))
            self.assertFalse(readerpc_history_sync_enabled(paths))
            self.assertFalse(readerpc_history_sync_enabled(paths))
            self.assertTrue(readerpc_history_sync_enabled(paths))

    def test_history_sync_runs_under_single_owner_lease(self) -> None:
        window = self.window_without_tk()
        window.bridge_paths.root = Path("C:/fixed")
        window.history_stop_event = Mock()
        window.history_synchronizer = Mock()
        lease = Mock()
        lease.__enter__ = Mock(return_value=True)
        lease.__exit__ = Mock(return_value=False)
        with (
            patch("readerpc_launcher.history_worker_lease", return_value=lease),
            patch("readerpc_launcher.monitor_capture_history") as monitor,
        ):
            window._run_history_sync()
        monitor.assert_called_once()
        self.assertIs(
            monitor.call_args.kwargs["stop_event"],
            window.history_stop_event,
        )
        self.assertIs(
            monitor.call_args.kwargs["synchronizer"],
            window.history_synchronizer,
        )

    def test_history_sync_skips_when_another_owner_holds_lease(self) -> None:
        window = self.window_without_tk()
        window.bridge_paths.root = Path("C:/fixed")
        window.history_stop_event = Mock()
        window.history_synchronizer = Mock()
        lease = Mock()
        lease.__enter__ = Mock(return_value=False)
        lease.__exit__ = Mock(return_value=False)
        with (
            patch("readerpc_launcher.history_worker_lease", return_value=lease),
            patch("readerpc_launcher.monitor_capture_history") as monitor,
        ):
            window._run_history_sync()
        monitor.assert_not_called()

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
                side_effect=lambda *_args, **kwargs: (
                    calls.append("opt-out"),
                    kwargs["after_disable"](),
                    calls.append("stop"),
                    kwargs["after_stop"](),
                ),
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot",
                side_effect=lambda _paths: calls.append("tombstone"),
            ),
        ):
            disable_readerpc_voice(bridge_paths, process_runner)
        self.assertEqual(
            calls,
            [False, "opt-out", "tombstone", "stop", "tombstone"],
        )

    def test_disable_voice_passes_both_tombstone_fences_to_atomic_stop(self) -> None:
        with (
            patch("readerpc_launcher.set_codex_voice_keep_active"),
            patch(
                "readerpc_launcher.disable_and_stop_direct_service"
            ) as disable,
        ):
            disable_readerpc_voice(Mock(), Mock())
        self.assertTrue(callable(disable.call_args.kwargs["after_disable"]))
        self.assertTrue(callable(disable.call_args.kwargs["after_stop"]))

    def test_disabled_snapshot_revokes_every_reader_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge_paths = SimpleNamespace(root=Path(raw))
            write_disabled_reader_context_snapshot(
                bridge_paths,
                now=datetime(2026, 8, 14, 1, 2, 3, tzinfo=timezone.utc),
                producer_instance_id="a" * 32,
            )
            snapshot = json.loads(
                (
                    Path(raw)
                    / "runtime"
                    / "reader-context-snapshot.json"
                ).read_text("utf-8")
            )
        self.assertEqual(snapshot["contextStatus"], "disabled")
        self.assertIsNone(snapshot["activeReading"])
        self.assertIsNone(snapshot["currentPage"])
        self.assertEqual(
            snapshot["selection"]["reason"],
            "readerpc-service-disabled",
        )
        self.assertEqual(snapshot["producerInstanceId"], "a" * 32)
        self.assertEqual(snapshot["revision"], 0)

    def test_enable_voice_switches_to_snapshot_before_keepalive_and_start(self) -> None:
        bridge_paths = Mock()
        bridge_paths.direct_config.exists.return_value = True
        process_runner = Mock()
        previous = {
            "localOptIn": False,
            "contextDeliveryMode": "legacy-inject",
        }
        calls: list[object] = []
        with (
            patch("readerpc_launcher.load_direct_config", return_value=previous),
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args, **kwargs: calls.append(
                    ("configure", kwargs)
                ),
            ),
            patch(
                "readerpc_launcher.set_codex_voice_keep_active",
                side_effect=lambda _paths, enabled: calls.append(
                    ("keepalive", enabled)
                ),
            ),
            patch(
                "readerpc_launcher.start_readerpc_voice",
                side_effect=lambda *_args: calls.append("start") or 4321,
            ),
        ):
            self.assertEqual(
                enable_readerpc_voice(bridge_paths, process_runner),
                4321,
            )
        self.assertEqual(calls[0][0], "configure")
        self.assertEqual(
            calls[0][1]["context_delivery_mode"],
            "snapshot-mcp",
        )
        self.assertEqual(calls[1:], [("keepalive", True), "start"])

    def test_enable_failure_restores_entire_previous_config(self) -> None:
        bridge_paths = Mock()
        bridge_paths.direct_config.exists.return_value = True
        previous = {
            "localOptIn": True,
            "contextDeliveryMode": "legacy-inject",
            "sentinel": "keep-me",
        }
        with (
            patch("readerpc_launcher.load_direct_config", return_value=previous),
            patch("readerpc_launcher.set_direct_config_enabled"),
            patch("readerpc_launcher.set_codex_voice_keep_active") as keepalive,
            patch(
                "readerpc_launcher.start_readerpc_voice",
                side_effect=RuntimeError("runtime-heartbeat-timeout"),
            ),
            patch("readerpc_launcher.restore_direct_config") as restore,
            self.assertRaisesRegex(RuntimeError, "runtime-heartbeat-timeout"),
        ):
            enable_readerpc_voice(bridge_paths, Mock())
        self.assertEqual(
            [call.args[1] for call in keepalive.call_args_list],
            [True, False],
        )
        restore.assert_called_once_with(bridge_paths, previous)

    def test_enable_failure_reports_incomplete_rollback(self) -> None:
        bridge_paths = Mock()
        bridge_paths.direct_config.exists.return_value = True
        with (
            patch(
                "readerpc_launcher.load_direct_config",
                return_value={
                    "localOptIn": False,
                    "contextDeliveryMode": "legacy-inject",
                },
            ),
            patch("readerpc_launcher.set_direct_config_enabled"),
            patch("readerpc_launcher.set_codex_voice_keep_active"),
            patch(
                "readerpc_launcher.start_readerpc_voice",
                side_effect=RuntimeError("start-failed"),
            ),
            patch(
                "readerpc_launcher.restore_direct_config",
                side_effect=OSError("restore-denied"),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "启用失败.*回滚未完整完成.*restore-denied",
            ),
        ):
            enable_readerpc_voice(bridge_paths, Mock())

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
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
        ):
            stop_readerpc_services(bridge_paths, Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        keep_voice.assert_called_once_with(bridge_paths, False)
        stop_voice.assert_not_called()
        stop_orphan.assert_not_called()
        self.assertEqual(tombstone.call_count, 2)

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
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
        ):
            stop_readerpc_services(bridge_paths, Mock(), pc_ocr)
        disable_voice.assert_not_called()
        keep_voice.assert_called_once_with(bridge_paths, False)
        stop_orphan.assert_called_once()
        self.assertEqual(tombstone.call_count, 2)

    def test_start_voice_sets_keepalive_before_starting_service(self) -> None:
        window = self.window_without_tk()
        window._run_task = Mock(side_effect=lambda _pending, action, _success: action())
        with patch(
            "readerpc_launcher.enable_readerpc_voice",
            return_value=4321,
        ) as enable:
            window._start_voice_task(recovery=False)
        enable.assert_called_once_with(
            window.bridge_paths,
            window.process_runner,
        )

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
        window.history_stop_event = Mock()
        window.history_thread = Mock()
        window.history_thread.is_alive.return_value = True

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
        window.history_stop_event.set.assert_called_once_with()
        window.history_thread.join.assert_called_once_with(timeout=3)
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
            patch(
                "readerpc_launcher.prepare_readerpc_shortcut_broker"
            ) as prepare_broker,
            patch("readerpc_launcher.tk.Tk") as make_root,
            patch("readerpc_launcher.ReaderPCWindow") as make_window,
        ):
            instance.return_value.acquire.return_value = True
            self.assertEqual(main([]), 0)

        prepare_broker.assert_called_once_with()
        prepare_broker.return_value.close.assert_called_once_with()
        make_window.assert_called_once_with(make_root.return_value)
        make_root.return_value.mainloop.assert_called_once_with()

    def test_readerpc_reuses_strictly_owned_logon_broker(self) -> None:
        inspection = SimpleNamespace(exists=True, owned=True)
        with (
            patch(
                "readerpc_launcher.inspect_bootstrap_task",
                return_value=inspection,
            ),
            patch(
                "readerpc_launcher.run_bootstrap_task_if_owned",
                return_value=True,
            ) as run_task,
            patch(
                "readerpc_launcher.WindowsShortcutBroker",
            ) as broker,
        ):
            self.assertIsNone(prepare_readerpc_shortcut_broker())
        run_task.assert_called_once()
        broker.assert_not_called()

    def test_readerpc_rejects_unowned_logon_broker(self) -> None:
        with (
            patch(
                "readerpc_launcher.inspect_bootstrap_task",
                return_value=SimpleNamespace(exists=True, owned=False),
            ),
            patch("readerpc_launcher.run_bootstrap_task_if_owned") as run_task,
            patch("readerpc_launcher.WindowsShortcutBroker") as broker,
        ):
            with self.assertRaisesRegex(Exception, "不属于 Reader"):
                prepare_readerpc_shortcut_broker()
        run_task.assert_not_called()
        broker.assert_not_called()

    def test_readerpc_owns_broker_when_logon_task_is_absent(self) -> None:
        with (
            patch(
                "readerpc_launcher.inspect_bootstrap_task",
                return_value=SimpleNamespace(exists=False, owned=False),
            ),
            patch("readerpc_launcher.WindowsShortcutBroker") as broker,
        ):
            self.assertIs(
                prepare_readerpc_shortcut_broker(),
                broker.return_value,
            )
        broker.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
