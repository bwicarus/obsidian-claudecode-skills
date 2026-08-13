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
    enable_readerpc_voice,
    load_preferences,
    main,
    read_codex_voice_keep_active,
    readerpc_history_sync_enabled,
    prepare_readerpc_shortcut_broker,
    save_preferences,
    set_codex_voice_keep_active,
    start_readerpc_voice,
    stop_readerpc_voice,
    stop_readerpc_services,
    write_disabled_reader_context_snapshot,
)
from readerpc_services import (  # noqa: E402
    CodexVoiceActivityStatus,
    PcOcrStatus,
    ReaderContextStatus,
    read_reader_context_status,
    write_disabled_reader_context_snapshot as write_offline_snapshot,
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
        window.voice_start_in_progress = False
        window.voice_stop_in_progress = False
        window.voice_snapshot_offline_marked = False
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

    def test_codex_voice_master_switch_is_exact_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime" / "direct.status.json"
            paths = SimpleNamespace(runtime_status=runtime)
            self.assertIsNone(read_codex_voice_keep_active(paths))
            runtime.parent.mkdir(parents=True)
            keepalive = runtime.parent / "codex-voice-keepalive.json"
            keepalive.write_text(
                json.dumps(
                    {
                        "contract": "reader-codex-voice-keepalive/1",
                        "enabled": True,
                    }
                ),
                "utf-8",
            )
            self.assertTrue(read_codex_voice_keep_active(paths))
            keepalive.write_text(
                '{"contract":"reader-codex-voice-keepalive/1",'
                '"enabled":true,"extra":1}',
                "utf-8",
            )
            self.assertIsNone(read_codex_voice_keep_active(paths))

    def test_explicit_readerpc_choice_writes_shared_master_switch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime_status = Path(raw) / "runtime" / "status.json"
            bridge_paths = SimpleNamespace(runtime_status=runtime_status)
            set_codex_voice_keep_active(bridge_paths, True)
            self.assertTrue(read_codex_voice_keep_active(bridge_paths))
            set_codex_voice_keep_active(bridge_paths, False)
            self.assertFalse(read_codex_voice_keep_active(bridge_paths))

    def test_history_sync_requires_explicit_snapshot_mode(self) -> None:
        paths = object()
        with (
            patch(
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
            ),
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
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

    def test_stop_voice_fences_snapshot_without_writing_app_intent(self) -> None:
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = True
        process_runner = Mock()
        calls: list[str] = []
        with (
            patch(
                "readerpc_launcher.read_direct_status",
                return_value=SimpleNamespace(service_online=True),
            ),
            patch(
                "readerpc_launcher.stop_direct_service",
                side_effect=lambda *_args: calls.append("stop"),
            ) as stop,
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot",
                side_effect=lambda _paths: calls.append("tombstone"),
            ) as tombstone,
        ):
            stop_readerpc_voice(bridge_paths, process_runner)
        self.assertEqual(calls, ["tombstone", "stop", "tombstone"])
        stop.assert_called_once_with(bridge_paths, process_runner)
        self.assertEqual(tombstone.call_count, 2)

    def test_disabled_intent_derives_direct_config_off_before_stop(self) -> None:
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = False
        process_runner = Mock()
        calls: list[str] = []
        with (
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args, **_kwargs: calls.append("configure-off"),
            ) as configure,
            patch(
                "readerpc_launcher.read_direct_status",
                return_value=SimpleNamespace(service_online=False),
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot",
                side_effect=lambda _paths: calls.append("tombstone"),
            ),
        ):
            stop_readerpc_voice(
                bridge_paths,
                process_runner,
                disable_configuration=True,
            )
        configure.assert_called_once_with(bridge_paths, False)
        self.assertEqual(calls, ["configure-off", "tombstone", "tombstone"])

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

    def test_disabled_snapshot_is_not_reported_as_available_or_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_offline_snapshot(
                root,
                now=datetime(2026, 8, 14, 1, 2, 3, tzinfo=timezone.utc),
                producer_instance_id="b" * 32,
            )
            status = read_reader_context_status(
                root / "runtime" / "reader-context-snapshot.json",
                now_epoch_ms=int(
                    datetime(
                        2026, 8, 14, 1, 2, 4, tzinfo=timezone.utc
                    ).timestamp() * 1000
                ),
            )
        self.assertFalse(status.available)
        self.assertFalse(status.fresh)

    def test_recovery_switches_to_snapshot_without_writing_app_intent(self) -> None:
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
                "readerpc_launcher.read_codex_voice_keep_active",
                side_effect=(True, True, True),
            ),
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args, **kwargs: calls.append(
                    ("configure", kwargs)
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
        self.assertEqual(calls[1:], ["start"])

    def test_enable_without_valid_config_revokes_stale_snapshot(self) -> None:
        bridge_paths = Mock()
        bridge_paths.direct_config.exists.return_value = True
        with (
            patch("readerpc_launcher.load_direct_config", return_value=None),
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=False,
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
            self.assertRaisesRegex(RuntimeError, "现有电脑语音配置无效"),
        ):
            enable_readerpc_voice(bridge_paths, Mock())
        tombstone.assert_called_once_with(bridge_paths)

    def test_start_failure_keeps_app_intent_and_revokes_snapshot(self) -> None:
        bridge_paths = Mock()
        bridge_paths.direct_config.exists.return_value = True
        events: list[str] = []

        def fail_start(*_args) -> int:
            events.append("start")
            raise RuntimeError("runtime-heartbeat-timeout")

        with (
            patch(
                "readerpc_launcher.load_direct_config",
                return_value={
                    "localOptIn": True,
                    "contextDeliveryMode": "snapshot-mcp",
                },
            ),
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                side_effect=(True, True),
            ),
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args, **_kwargs: events.append("configure"),
            ),
            patch("readerpc_launcher.start_readerpc_voice", side_effect=fail_start),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot",
                side_effect=lambda _paths: events.append("tombstone"),
            ) as tombstone,
            self.assertRaisesRegex(
                RuntimeError,
                "runtime-heartbeat-timeout",
            ),
        ):
            enable_readerpc_voice(bridge_paths, Mock())
        self.assertEqual(events, ["configure", "start", "tombstone"])
        tombstone.assert_called_once_with(bridge_paths)

    def test_recovery_refuses_false_app_intent_and_stops_only(self) -> None:
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
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=False,
            ),
            patch("readerpc_launcher.set_direct_config_enabled") as configure,
            patch("readerpc_launcher.start_readerpc_voice") as start,
            patch("readerpc_launcher.stop_readerpc_voice") as stop,
            patch("readerpc_launcher.write_disabled_reader_context_snapshot"),
            self.assertRaisesRegex(
                RuntimeError,
                "总开关已关闭.*取消后台启动",
            ),
        ):
            enable_readerpc_voice(bridge_paths, Mock())
        stop.assert_called_once()
        configure.assert_not_called()
        start.assert_not_called()

    def test_app_turning_off_mid_start_rolls_back_listener(self) -> None:
        bridge_paths = Mock()
        bridge_paths.direct_config.exists.return_value = True
        with (
            patch(
                "readerpc_launcher.load_direct_config",
                return_value={
                    "localOptIn": True,
                    "contextDeliveryMode": "snapshot-mcp",
                },
            ),
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                side_effect=(True, True, False),
            ),
            patch("readerpc_launcher.set_direct_config_enabled") as configure,
            patch(
                "readerpc_launcher.start_readerpc_voice",
                return_value=1234,
            ) as start,
            patch("readerpc_launcher.stop_readerpc_voice") as stop,
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
            self.assertRaisesRegex(RuntimeError, "总开关已关闭.*取消后台启动"),
        ):
            enable_readerpc_voice(bridge_paths, Mock())
        configure.assert_called_once()
        start.assert_called_once()
        stop.assert_called_once()
        tombstone.assert_called_once_with(bridge_paths)

    def test_shutdown_stops_pc_and_direct_services(self) -> None:
        pc_ocr = Mock()
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
            ),
            patch("readerpc_launcher.stop_readerpc_voice") as stop_voice,
        ):
            stop_readerpc_services(Mock(), Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        stop_voice.assert_called_once()

    def test_shutdown_attempts_both_services_and_reports_failure(self) -> None:
        pc_ocr = Mock()
        pc_ocr.stop.side_effect = RuntimeError("pc-stop-failed")
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
            ),
            patch(
                "readerpc_launcher.stop_readerpc_voice",
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

    def test_shutdown_preserves_true_app_intent(self) -> None:
        pc_ocr = Mock()
        bridge_paths = Mock()
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
            ),
            patch("readerpc_launcher.stop_readerpc_voice") as stop_voice,
            patch("readerpc_launcher.set_codex_voice_keep_active") as write_intent,
        ):
            stop_readerpc_services(bridge_paths, Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        stop_voice.assert_called_once()
        write_intent.assert_not_called()

    def test_explicit_readerpc_enable_writes_shared_intent_then_starts(self) -> None:
        window = self.window_without_tk()
        window.voice_snapshot_offline_marked = True
        window._run_task = Mock(side_effect=lambda _pending, action, _success: action())
        calls: list[object] = []
        with (
            patch(
                "readerpc_launcher.set_codex_voice_keep_active",
                side_effect=lambda _paths, enabled: calls.append(enabled),
            ),
            patch(
                "readerpc_launcher.enable_readerpc_voice",
                side_effect=lambda *_args: calls.append("start") or 4321,
            ) as enable,
        ):
            window._start_voice_task(explicit_enable=True)
        self.assertEqual(calls, [True, "start"])
        enable.assert_called_once_with(window.bridge_paths, window.process_runner)
        self.assertFalse(window.voice_snapshot_offline_marked)

    def test_offline_retry_does_not_rewrite_master_intent(self) -> None:
        window = self.window_without_tk()
        window._run_task = Mock(side_effect=lambda _pending, action, _success: action())
        with (
            patch("readerpc_launcher.set_codex_voice_keep_active") as write_intent,
            patch(
                "readerpc_launcher.enable_readerpc_voice",
                return_value=4321,
            ) as enable,
        ):
            window._start_voice_task(explicit_enable=False)
        write_intent.assert_not_called()
        enable.assert_called_once_with(window.bridge_paths, window.process_runner)

    def test_toggle_false_intent_is_an_explicit_shared_enable(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(
            return_value=SimpleNamespace(service_online=True)
        )
        window._start_voice_task = Mock()
        with patch(
            "readerpc_launcher.read_codex_voice_keep_active",
            return_value=False,
        ):
            window.toggle_voice()
        window._start_voice_task.assert_called_once_with(explicit_enable=True)

    def test_explicit_readerpc_disable_writes_false_then_stops(self) -> None:
        window = self.window_without_tk()
        window._run_task = Mock(side_effect=lambda _pending, action, _success: action())
        calls: list[object] = []
        with (
            patch(
                "readerpc_launcher.set_codex_voice_keep_active",
                side_effect=lambda _paths, enabled: calls.append(enabled),
            ),
            patch(
                "readerpc_launcher.stop_readerpc_voice",
                side_effect=lambda *_args, **_kwargs: calls.append("stop"),
            ),
        ):
            window._stop_voice_task(explicit_disable=True)
        self.assertEqual(calls, [False, "stop"])

    def test_offline_monitor_tombstones_before_retry_and_only_once(self) -> None:
        window = self.window_without_tk()
        window.last_voice_start_attempt = 0.0
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=False,
            configuration_enabled=True,
        ))
        calls: list[str] = []

        def retry(**_kwargs) -> None:
            calls.append("retry")
            window.last_voice_start_attempt = 31.0

        window._start_voice_task = Mock(side_effect=retry)
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot",
                side_effect=lambda _paths: calls.append("tombstone"),
            ) as tombstone,
            patch("readerpc_launcher.time.monotonic", return_value=31.0),
        ):
            window._ensure_voice_online()
            window._ensure_voice_online()
        self.assertEqual(calls, ["tombstone", "retry"])
        tombstone.assert_called_once_with(window.bridge_paths)
        self.assertEqual(window.root.after.call_count, 2)

    def test_master_switch_recovers_listener_even_if_direct_config_is_off(self) -> None:
        window = self.window_without_tk()
        window.voice_snapshot_offline_marked = True
        window._voice_status = Mock(side_effect=(
            SimpleNamespace(service_online=True, configuration_enabled=True),
            SimpleNamespace(service_online=False, configuration_enabled=False),
        ))
        window._start_voice_task = Mock()
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
            patch("readerpc_launcher.time.monotonic", return_value=31.0),
        ):
            window._ensure_voice_online()
            window._ensure_voice_online()
        tombstone.assert_called_once_with(window.bridge_paths)
        window._start_voice_task.assert_called_once_with()
        self.assertTrue(window.voice_snapshot_offline_marked)

    def test_busy_unrelated_work_still_revokes_without_starting_retry(self) -> None:
        window = self.window_without_tk()
        window.busy = True
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=False,
            configuration_enabled=True,
        ))
        window._start_voice_task = Mock()
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
        ):
            window._ensure_voice_online()
        tombstone.assert_called_once_with(window.bridge_paths)
        window._start_voice_task.assert_not_called()

    def test_monitor_does_not_clobber_snapshot_during_known_start(self) -> None:
        window = self.window_without_tk()
        window.busy = True
        window.voice_start_in_progress = True
        window._voice_status = Mock()
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
        ):
            window._ensure_voice_online()
        window._voice_status.assert_not_called()
        tombstone.assert_not_called()

    def test_monitor_tombstone_failure_is_visible_and_polling_continues(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=False,
            configuration_enabled=False,
        ))
        with patch(
            "readerpc_launcher.read_codex_voice_keep_active",
            return_value=True,
        ), patch(
            "readerpc_launcher.write_disabled_reader_context_snapshot",
            side_effect=OSError("snapshot-write-denied"),
        ):
            window._ensure_voice_online()
        window.footer.configure.assert_called_once()
        self.assertIn(
            "snapshot-write-denied",
            window.footer.configure.call_args.kwargs["text"],
        )
        window.root.after.assert_called_once_with(
            5_000,
            window._ensure_voice_online,
        )

    def test_online_listener_is_stopped_when_master_switch_turns_false(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=True,
            configuration_enabled=True,
        ))
        window._start_voice_task = Mock()
        window._stop_voice_task = Mock()
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=False,
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
        ):
            window._ensure_voice_online()
        window._stop_voice_task.assert_called_once_with()
        tombstone.assert_called_once_with(window.bridge_paths)
        window._start_voice_task.assert_not_called()

    def test_unreadable_master_switch_preserves_last_running_state(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=True,
            configuration_enabled=True,
        ))
        window._start_voice_task = Mock()
        window._stop_voice_task = Mock()
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=None,
            ),
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as tombstone,
        ):
            window._ensure_voice_online()
        window._stop_voice_task.assert_not_called()
        window._start_voice_task.assert_not_called()
        tombstone.assert_not_called()
        self.assertIn(
            "保留上次有效运行状态",
            window.footer.configure.call_args.kwargs["text"],
        )

    def test_refresh_does_not_report_listener_online_as_voice_ready(self) -> None:
        window = self.window_without_tk()
        window.voice_status = Mock()
        window.voice_detail = Mock()
        window.context_status = Mock()
        window.context_detail = Mock()
        window.pc_status = Mock()
        window.pc_detail = Mock()
        window.readerpc_paths = SimpleNamespace(
            status_file=Path("C:/readerpc.status.json")
        )
        window.last_status_publish = 0.0
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=True,
            configuration_enabled=True,
            reader_connected=False,
            capture_active=False,
            reason="reader-not-connected",
            pid=4321,
        ))
        window.pc_ocr.status.return_value = PcOcrStatus(
            running=False,
            state="stopped",
            phase="",
            pid=None,
            start_file_time_utc=None,
            worker_id=None,
            gpu_name=None,
            current_page=None,
            progress={},
            updated_at_epoch_ms=None,
            error=None,
            controllable=True,
            source_ready=True,
        )
        window.bridge_paths.root = Path("C:/fixed")
        context = ReaderContextStatus(False, False, "", "", None)
        with (
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=True,
            ),
            patch(
                "readerpc_launcher.read_codex_voice_activity",
                return_value=CodexVoiceActivityStatus("available", False),
            ),
            patch(
                "readerpc_launcher.read_reader_context_status",
                return_value=context,
            ),
            patch("readerpc_launcher.write_readerpc_status") as publish,
            patch("readerpc_launcher.time.monotonic", return_value=20.0),
        ):
            window.refresh()
        self.assertEqual(
            window.voice_status.configure.call_args.kwargs["text"],
            "直连在线 · 等待 Codex 语音",
        )
        voice = publish.call_args.kwargs["voice"]
        self.assertTrue(voice["online"])
        self.assertTrue(voice["intentEnabled"])
        self.assertFalse(voice["codexVoiceActive"])

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
