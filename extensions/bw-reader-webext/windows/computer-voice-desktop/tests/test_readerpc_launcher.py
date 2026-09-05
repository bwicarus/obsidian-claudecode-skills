from __future__ import annotations

from datetime import datetime, timezone
import json
import os
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

import readerpc_launcher
import readerpc_services  # noqa: E402
from readerpc_launcher import (  # noqa: E402
    ReaderPCWindow,
    ShortcutBrokerError,
    enable_readerpc_voice,
    load_preferences,
    merge_preferences_with_service_intent,
    main,
    read_codex_voice_keep_active,
    rearm_codex_voice_keep_active,
    describe_voice_failure,
    autostart_script_checks,
    readerpc_history_sync_enabled,
    prepare_readerpc_shortcut_broker,
    save_preferences,
    set_codex_voice_keep_active,
    start_readerpc_voice,
    stop_readerpc_voice,
    stop_readerpc_services,
    terminate_stale_instances,
    write_disabled_reader_context_snapshot,
    write_recovering_reader_context_snapshot,
)
from readerpc_services import (  # noqa: E402
    CodexVoiceActivityStatus,
    PcOcrStatus,
    ReaderContextStatus,
    read_reader_context_status,
    write_disabled_reader_context_snapshot as write_offline_snapshot,
)


_BOOT_LOG_PATCH = None


def setUpModule() -> None:
    # 单测绝不能往真实 %LOCALAPPDATA%/BWReader/readerpc-server.log 写:
    # 2026-09-03 实锤,测试夹具里的 Mock 异常("unsupported operand ... 'Mock' and 'str'")
    # 与假 PID 的"启动接管"行混进了线上日志,排障时会把人带偏。
    global _BOOT_LOG_PATCH
    _BOOT_LOG_PATCH = patch.object(readerpc_launcher, "_boot_log", lambda message: None)
    _BOOT_LOG_PATCH.start()


def tearDownModule() -> None:
    if _BOOT_LOG_PATCH is not None:
        _BOOT_LOG_PATCH.stop()

class ReaderPCLauncherTests(unittest.TestCase):
    class MutableProcessRunner:
        def __init__(self, executables: dict[int, Path]) -> None:
            self.executables = dict(executables)
            self.terminations: list[tuple[int, Path]] = []

        def executable_for_pid(self, pid: int) -> Path | None:
            return self.executables.get(pid)

        def terminate_exact(self, pid: int, executable: Path) -> bool:
            self.terminations.append((pid, executable))
            return False

    @staticmethod
    def write_takeover_direct_identity(
        paths,
        pid: int,
        instance_id: str = "a" * 32,
    ) -> None:
        paths.native_host.parent.mkdir(parents=True, exist_ok=True)
        paths.native_host.write_bytes(b"direct placeholder")
        paths.service_record.parent.mkdir(parents=True, exist_ok=True)
        paths.service_record.write_text(
            json.dumps({
                "contract": "reader-computer-voice-desktop-service/1",
                "pid": pid,
                "executable": str(paths.native_host.resolve()),
                "configPath": str(paths.direct_config.resolve()),
                "startedAtUtc": "2026-08-22T00:00:00Z",
            }),
            encoding="utf-8",
        )
        paths.runtime_status.write_text(
            json.dumps({
                "contract": "reader-computer-voice-direct-runtime-status/2",
                "serviceInstanceId": instance_id,
                "pid": pid,
                "state": "active",
                "readerConnected": True,
                "captureActive": False,
                "lastError": None,
                "updatedAtUtc": "2026-08-22T00:00:00Z",
            }),
            encoding="utf-8",
        )

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
        window.voice_maintenance_notice = None
        window._shortcut_broker = Mock()
        window.history_stop_event = threading.Event()
        window.history_thread = None
        return window

    def test_preferences_default_to_keep_pc_online(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "missing.json"
            self.assertEqual(
                load_preferences(path),
                {"keepPcPreprocessingOnline": True, "serviceMode": "full", "voiceEnabled": True, "snapshotViewerHidden": False, "hideVoiceOrb": False, "autoStartOnBoot": False, "manageServerServices": False},
            )

    def test_preferences_round_trip_explicit_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "readerpc.json"
            save_preferences(path, keep_pc_online=False)
            self.assertEqual(
                load_preferences(path),
                {"keepPcPreprocessingOnline": False, "serviceMode": "full", "voiceEnabled": True, "snapshotViewerHidden": False, "hideVoiceOrb": False, "autoStartOnBoot": False, "manageServerServices": False},
            )

    def test_invalid_preferences_fail_to_safe_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "readerpc.json"
            path.write_text('{"keepPcPreprocessingOnline":"yes"}', "utf-8")
            self.assertTrue(load_preferences(path)["keepPcPreprocessingOnline"])

    def test_preferences_bridge_mode_round_trip_and_legacy_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "readerpc.json"
            save_preferences(
                path,
                keep_pc_online=True,
                service_mode="bridge-only",
            )
            self.assertEqual(
                load_preferences(path)["serviceMode"], "bridge-only"
            )
            # 旧版偏好文件(没有 serviceMode 键)→ 落 full,contract 不 bump
            path.write_text(
                '{"contract":"readerpc-server-config/1",'
                '"keepPcPreprocessingOnline":true}',
                "utf-8",
            )
            self.assertEqual(load_preferences(path)["serviceMode"], "full")

    def test_preferences_voice_axis_round_trip_and_legacy_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "readerpc.json"
            save_preferences(
                path,
                keep_pc_online=True,
                service_mode="bridge-only",
                voice_enabled=False,
            )
            value = load_preferences(path)
            self.assertEqual(value["serviceMode"], "bridge-only")
            self.assertFalse(value["voiceEnabled"])
            path.write_text(
                '{"contract":"readerpc-server-config/1",'
                '"keepPcPreprocessingOnline":true,"serviceMode":"full"}',
                "utf-8",
            )
            self.assertTrue(load_preferences(path)["voiceEnabled"])

    def test_service_intent_overrides_stale_preferences_before_voice_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bridge_paths = Mock()
            bridge_paths.runtime_status.parent = root
            preferences = {
                "keepPcPreprocessingOnline": True,
                "serviceMode": "full",
                "voiceEnabled": True,
                "snapshotViewerHidden": False,
                "hideVoiceOrb": False,
                "autoStartOnBoot": False,
            }
            (root / "readerpc-service-mode.json").write_text(
                json.dumps({
                    "contract": "readerpc-service-mode/1",
                    "mode": "bridge-only",
                    "voiceEnabled": False,
                    "snapshotViewer": "hidden",
                }),
                encoding="utf-8",
            )
            merged = merge_preferences_with_service_intent(
                preferences,
                bridge_paths,
            )
        self.assertEqual(merged["serviceMode"], "bridge-only")
        self.assertFalse(merged["voiceEnabled"])
        self.assertTrue(merged["snapshotViewerHidden"])

    def test_invalid_service_mode_preference_falls_back_to_full(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "readerpc.json"
            # 非法模式值 → 落 full
            path.write_text(
                '{"contract":"readerpc-server-config/1",'
                '"keepPcPreprocessingOnline":true,"serviceMode":"chaos"}',
                "utf-8",
            )
            self.assertEqual(load_preferences(path)["serviceMode"], "full")

    def test_service_mode_intent_file_written_before_voice_start(self) -> None:
        """桥接模式与独立语音轴都在 start 前落盘。"""
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime" / "direct.status.json"
            runtime.parent.mkdir(parents=True)
            bridge_paths = SimpleNamespace(
                runtime_status=runtime,
                direct_config=SimpleNamespace(exists=lambda: True),
            )
            order: list[str] = []
            with (
                patch(
                    "readerpc_launcher.load_direct_config",
                    return_value={
                        "localOptIn": True,
                        "contextDeliveryMode": "snapshot-mcp",
                    },
                ),
                patch("readerpc_launcher.set_direct_config_enabled"),
                patch(
                    "readerpc_launcher.start_readerpc_voice",
                    side_effect=lambda *_a, **_k: (
                        order.append("start"),
                        4321,
                    )[1],
                ),
            ):
                self.assertEqual(
                    enable_readerpc_voice(
                        bridge_paths,
                        Mock(),
                        bridge_only=True,
                    ),
                    4321,
                )
            mode_file = runtime.parent / "readerpc-service-mode.json"
            self.assertEqual(
                json.loads(mode_file.read_text("utf-8")),
                {"contract": "readerpc-service-mode/1", "mode": "bridge-only",
                 "voiceEnabled": True,
                 "snapshotViewer": "visible"},
            )
            keepalive = json.loads(
                (runtime.parent / "codex-voice-keepalive.json")
                .read_text("utf-8")
            )
            # 2026-08-17 语义更正:桥接模式语音留在电脑,keepalive 照常 True
            self.assertTrue(keepalive["enabled"])
            self.assertEqual(order, ["start"])

    def test_voice_disabled_still_starts_direct_non_voice_foundation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime" / "direct.status.json"
            runtime.parent.mkdir(parents=True)
            bridge_paths = SimpleNamespace(
                runtime_status=runtime,
                direct_config=SimpleNamespace(exists=lambda: True),
            )
            with (
                patch(
                    "readerpc_launcher.load_direct_config",
                    return_value={
                        "localOptIn": True,
                        "contextDeliveryMode": "snapshot-mcp",
                    },
                ),
                patch("readerpc_launcher.set_direct_config_enabled") as configure,
                patch(
                    "readerpc_launcher.start_readerpc_voice",
                    return_value=7654,
                ) as start,
            ):
                self.assertEqual(
                    enable_readerpc_voice(
                        bridge_paths,
                        Mock(),
                        bridge_only=True,
                        voice_enabled=False,
                    ),
                    7654,
                )
            intent = json.loads(
                (runtime.parent / "readerpc-service-mode.json")
                .read_text("utf-8")
            )
            self.assertEqual(intent["mode"], "bridge-only")
            self.assertFalse(intent["voiceEnabled"])
            keepalive = json.loads(
                (runtime.parent / "codex-voice-keepalive.json")
                .read_text("utf-8")
            )
            self.assertFalse(keepalive["enabled"])
            configure.assert_called_once()
            start.assert_called_once()

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
        window._voice_status = Mock(
            return_value=SimpleNamespace(service_online=True)
        )
        lease = Mock()
        lease.__enter__ = Mock(return_value=True)
        lease.__exit__ = Mock(return_value=False)
        with (
            patch("readerpc_launcher.history_worker_lease", return_value=lease),
            patch("readerpc_launcher.monitor_capture_history") as monitor,
            patch(
                "readerpc_launcher.read_codex_voice_activity",
                return_value=CodexVoiceActivityStatus(
                    "available",
                    True,
                    12345,
                ),
            ),
        ):
            window._run_history_sync()
            history_status = monitor.call_args.kwargs["status_provider"]()
        monitor.assert_called_once()
        self.assertIs(
            monitor.call_args.kwargs["stop_event"],
            window.history_stop_event,
        )
        self.assertIs(
            monitor.call_args.kwargs["synchronizer"],
            window.history_synchronizer,
        )
        self.assertTrue(history_status.service_online)
        self.assertTrue(history_status.capture_active)
        self.assertEqual(history_status.capture_generation, 12345)

    def test_history_status_fails_closed_without_active_codex_generation(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(
            return_value=SimpleNamespace(service_online=True)
        )
        for ledger in (
            CodexVoiceActivityStatus("available", False),
            CodexVoiceActivityStatus("unavailable", None),
            CodexVoiceActivityStatus("error", None),
        ):
            with (
                self.subTest(ledger=ledger),
                patch(
                    "readerpc_launcher.read_codex_voice_activity",
                    return_value=ledger,
                ),
            ):
                status = window._history_status()
                self.assertTrue(status.service_online)
                self.assertFalse(status.capture_active)
                self.assertIsNone(status.capture_generation)

    def test_no_voice_history_status_does_not_probe_codex_activity(self) -> None:
        window = self.window_without_tk()
        window.voice_enabled = Mock()
        window.voice_enabled.get.return_value = False
        window._voice_status = Mock(
            return_value=SimpleNamespace(service_online=True)
        )
        with patch(
            "readerpc_launcher.read_codex_voice_activity"
        ) as activity:
            status = window._history_status()
        activity.assert_not_called()
        self.assertTrue(status.service_online)
        self.assertFalse(status.capture_active)
        self.assertIsNone(status.capture_generation)

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

    def test_stop_voice_revokes_server_intent_and_fences_snapshot(self) -> None:
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = True
        process_runner = Mock()
        calls: list[str] = []
        with (
            patch(
                "readerpc_launcher.set_codex_voice_keep_active",
                side_effect=lambda _paths, enabled: calls.append(
                    f"intent-{str(enabled).lower()}"
                ),
            ) as intent,
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args, **_kwargs: calls.append("configure-off"),
            ),
            patch("readerpc_launcher.set_readerpc_service_mode"),
            patch(
                "readerpc_launcher.read_direct_status",
                return_value=SimpleNamespace(service_online=True),
            ),
            patch(
                "readerpc_launcher.stop_direct_service",
                side_effect=lambda *_args, **_kwargs: calls.append("stop"),
            ) as stop,
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot",
                side_effect=lambda _paths: calls.append("tombstone"),
            ) as tombstone,
        ):
            stop_readerpc_voice(bridge_paths, process_runner)
        self.assertEqual(
            calls,
            [
                "intent-false",
                "configure-off",
                "tombstone",
                "stop",
                "tombstone",
            ],
        )
        intent.assert_called_once_with(bridge_paths, False)
        stop.assert_called_once_with(
            bridge_paths,
            process_runner,
            graceful=True,
            force_on_cleanup_failure=True,
        )
        self.assertEqual(tombstone.call_count, 2)

    def test_disabled_intent_derives_direct_config_off_before_stop(self) -> None:
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = False
        process_runner = Mock()
        calls: list[str] = []
        with (
            patch("readerpc_launcher.set_codex_voice_keep_active"),
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args, **_kwargs: calls.append("configure-off"),
            ) as configure,
            patch("readerpc_launcher.set_readerpc_service_mode"),
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

    def test_generation_replacement_marks_recovering_without_disabling(self) -> None:
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = True
        process_runner = Mock()
        with (
            patch("readerpc_launcher.set_codex_voice_keep_active") as intent,
            patch("readerpc_launcher.set_readerpc_service_mode"),
            patch("readerpc_launcher.set_direct_config_enabled") as configure,
            patch(
                "readerpc_launcher.read_direct_status",
                return_value=SimpleNamespace(service_online=True),
            ),
            patch("readerpc_launcher.stop_direct_service"),
            patch(
                "readerpc_launcher.write_recovering_reader_context_snapshot"
            ) as recovering,
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as disabled,
        ):
            stop_readerpc_voice(
                bridge_paths,
                process_runner,
                disable_configuration=False,
            )
        configure.assert_not_called()
        # 2026-09-06：换代/重启不撤销 keepalive —— 撤销 = C# 按 F24 把活着的通话关掉。
        intent.assert_not_called()
        self.assertEqual(recovering.call_count, 2)
        disabled.assert_not_called()

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

    def test_recovering_snapshot_revokes_targets_without_claiming_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bridge_paths = SimpleNamespace(root=Path(raw))
            write_recovering_reader_context_snapshot(
                bridge_paths,
                now=datetime(2026, 8, 14, 1, 2, 3, tzinfo=timezone.utc),
                producer_instance_id="c" * 32,
            )
            snapshot = json.loads(
                (
                    Path(raw)
                    / "runtime"
                    / "reader-context-snapshot.json"
                ).read_text("utf-8")
            )
            status = read_reader_context_status(
                Path(raw) / "runtime" / "reader-context-snapshot.json",
                now_epoch_ms=1_786_650_123_000,
            )
        self.assertEqual(snapshot["contextStatus"], "pending")
        self.assertEqual(snapshot["latestEvent"]["type"], "readerpc.recovering")
        self.assertEqual(
            snapshot["selection"]["reason"],
            "readerpc-service-recovering",
        )
        self.assertIsNone(snapshot["activeReading"])
        self.assertIsNone(snapshot["currentPage"])
        self.assertTrue(status.available)
        self.assertFalse(status.fresh)

    def test_server_start_publishes_intent_and_owns_snapshot_mode(self) -> None:
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
                "readerpc_launcher.set_codex_voice_keep_active",
                side_effect=lambda _paths, enabled: calls.append(
                    ("intent", enabled)
                ),
            ),
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args, **kwargs: calls.append(
                    ("configure", kwargs)
                ),
            ),
            patch("readerpc_launcher.set_readerpc_service_mode"),
            patch(
                "readerpc_launcher.start_readerpc_voice",
                side_effect=lambda *_args, **kwargs: calls.append(
                    ("start", kwargs)
                ) or 4321,
            ),
        ):
            self.assertEqual(
                enable_readerpc_voice(bridge_paths, process_runner),
                4321,
            )
        self.assertEqual(calls[0], ("intent", True))
        self.assertEqual(calls[1][0], "configure")
        self.assertEqual(
            calls[1][1]["context_delivery_mode"],
            "snapshot-mcp",
        )
        self.assertEqual(calls[2][0], "start")
        self.assertEqual(calls[2][1]["owner_pid"], os.getpid())

    def test_enable_without_valid_config_marks_snapshot_recovering(self) -> None:
        bridge_paths = Mock()
        bridge_paths.direct_config.exists.return_value = True
        with (
            patch("readerpc_launcher.load_direct_config", return_value=None),
            patch(
                "readerpc_launcher.read_codex_voice_keep_active",
                return_value=False,
            ),
            patch(
                "readerpc_launcher.write_recovering_reader_context_snapshot"
            ) as recovering,
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as disabled,
            self.assertRaisesRegex(RuntimeError, "现有电脑语音配置无效"),
        ):
            enable_readerpc_voice(bridge_paths, Mock())
        recovering.assert_called_once_with(bridge_paths)
        disabled.assert_not_called()

    def test_start_failure_revokes_runtime_intent_but_keeps_recovering_state(self) -> None:
        bridge_paths = Mock()
        bridge_paths.direct_config.exists.return_value = True
        events: list[str] = []

        def fail_start(*_args, **_kwargs) -> int:
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
                "readerpc_launcher.set_codex_voice_keep_active",
                side_effect=lambda _paths, enabled: events.append(
                    f"intent-{str(enabled).lower()}"
                ),
            ),
            patch(
                "readerpc_launcher.set_direct_config_enabled",
                side_effect=lambda *_args, **_kwargs: events.append("configure"),
            ),
            patch("readerpc_launcher.set_readerpc_service_mode"),
            patch("readerpc_launcher.start_readerpc_voice", side_effect=fail_start),
            patch(
                "readerpc_launcher.write_recovering_reader_context_snapshot",
                side_effect=lambda _paths: events.append("recovering"),
            ) as recovering,
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as disabled,
            self.assertRaisesRegex(
                RuntimeError,
                "runtime-heartbeat-timeout",
            ),
        ):
            enable_readerpc_voice(bridge_paths, Mock())
        self.assertEqual(
            events,
            ["intent-true", "configure", "start", "intent-false", "recovering"],
        )
        recovering.assert_called_once_with(bridge_paths)
        disabled.assert_not_called()

    def test_obsolete_false_app_intent_cannot_block_readerpc_start(self) -> None:
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
                "readerpc_launcher.set_codex_voice_keep_active",
                return_value=False,
            ) as intent,
            patch("readerpc_launcher.set_readerpc_service_mode"),
            patch("readerpc_launcher.set_direct_config_enabled") as configure,
            patch(
                "readerpc_launcher.start_readerpc_voice",
                return_value=1234,
            ) as start,
        ):
            self.assertEqual(enable_readerpc_voice(bridge_paths, Mock()), 1234)
        intent.assert_called_once_with(bridge_paths, True)
        configure.assert_called_once()
        start.assert_called_once()

    def test_readerpc_start_passes_current_process_as_owner(self) -> None:
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
                "readerpc_launcher.set_codex_voice_keep_active"
            ),
            patch("readerpc_launcher.set_readerpc_service_mode"),
            patch("readerpc_launcher.set_direct_config_enabled") as configure,
            patch(
                "readerpc_launcher.start_readerpc_voice",
                return_value=1234,
            ) as start,
        ):
            self.assertEqual(enable_readerpc_voice(bridge_paths, Mock()), 1234)
        configure.assert_called_once()
        self.assertEqual(start.call_args.kwargs["owner_pid"], os.getpid())

    def test_shutdown_stops_pc_and_direct_services(self) -> None:
        pc_ocr = Mock()
        with patch("readerpc_launcher.stop_readerpc_voice") as stop_voice:
            stop_readerpc_services(Mock(), Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        self.assertTrue(stop_voice.call_args.kwargs["disable_configuration"])
        self.assertTrue(stop_voice.call_args.kwargs["terminate_service"])

    def test_shutdown_attempts_both_services_and_reports_failure(self) -> None:
        pc_ocr = Mock()
        pc_ocr.stop.side_effect = RuntimeError("pc-stop-failed")
        with (
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
        self.assertTrue(stop_voice.call_args.kwargs["terminate_service"])

    def test_shutdown_unconditionally_revokes_readerpc_service_intent(self) -> None:
        pc_ocr = Mock()
        bridge_paths = Mock()
        with patch("readerpc_launcher.stop_readerpc_voice") as stop_voice:
            stop_readerpc_services(bridge_paths, Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        stop_voice.assert_called_once()
        self.assertTrue(stop_voice.call_args.kwargs["disable_configuration"])
        self.assertTrue(stop_voice.call_args.kwargs["terminate_service"])

    def test_readerpc_monitor_start_delegates_to_owned_enable(self) -> None:
        window = self.window_without_tk()
        window.voice_snapshot_offline_marked = True
        window._run_task = Mock(side_effect=lambda _pending, action, _success: action())
        with patch(
            "readerpc_launcher.enable_readerpc_voice",
            return_value=4321,
        ) as enable:
            window._start_voice_task()
        enable.assert_called_once_with(
            window.bridge_paths,
            window.process_runner,
            bridge_only=False,
            voice_enabled=True,
            snapshot_viewer_hidden=False,
        )
        self.assertFalse(window.voice_snapshot_offline_marked)

    def test_offline_retry_uses_the_same_owned_start_path(self) -> None:
        window = self.window_without_tk()
        window._run_task = Mock(side_effect=lambda _pending, action, _success: action())
        with patch(
            "readerpc_launcher.enable_readerpc_voice",
            return_value=4321,
        ) as enable:
            window._start_voice_task()
        enable.assert_called_once_with(
            window.bridge_paths,
            window.process_runner,
            bridge_only=False,
            voice_enabled=True,
            snapshot_viewer_hidden=False,
        )

    def test_voice_switch_restarts_only_optional_layer_intent(self) -> None:
        window = self.window_without_tk()
        window.voice_enabled = Mock()
        window.voice_enabled.get.return_value = False
        window._save_current_preferences = Mock()
        window._restart_voice_with_intent = Mock()
        window.on_voice_enabled_changed()
        window._save_current_preferences.assert_called_once_with()
        window._restart_voice_with_intent.assert_called_once()
        pending, done = window._restart_voice_with_intent.call_args.args
        self.assertIn("关闭语音", pending)
        self.assertIn("继续可用", done)

    def test_voice_disabled_restart_closes_f24_and_keeps_direct_online(self) -> None:
        window = self.window_without_tk()
        window.bridge_only = Mock()
        window.bridge_only.get.return_value = False
        window.voice_enabled = Mock()
        window.voice_enabled.get.return_value = False
        window.snapshot_hidden = Mock()
        window.snapshot_hidden.get.return_value = False
        window._run_task = Mock(
            side_effect=lambda _pending, action, _success: action()
        )
        window._converge_shortcut_broker = Mock()
        window._converge_history_monitor = Mock()
        with (
            patch("readerpc_launcher.stop_readerpc_voice") as stop,
            patch(
                "readerpc_launcher.enable_readerpc_voice",
                return_value=4321,
            ) as enable,
        ):
            window._restart_voice_with_intent("pending", "done")
        self.assertFalse(stop.call_args.kwargs["force_on_cleanup_failure"])
        window._converge_shortcut_broker.assert_called_once_with(
            voice_shortcut_enabled=False
        )
        window._converge_history_monitor.assert_called_once_with(False)
        enable.assert_called_once_with(
            window.bridge_paths,
            window.process_runner,
            bridge_only=False,
            voice_enabled=False,
            snapshot_viewer_hidden=False,
        )

    def test_switch_cleanup_failure_preserves_helpers_and_stops_new_generation(self) -> None:
        window = self.window_without_tk()
        window.bridge_only = Mock()
        window.bridge_only.get.return_value = False
        window.voice_enabled = Mock()
        window.voice_enabled.get.return_value = False
        window.snapshot_hidden = Mock()
        window.snapshot_hidden.get.return_value = False
        window._applied_service_mode = "full"
        window._applied_voice_enabled = True
        window._applied_snapshot_hidden = False
        window._run_task = Mock(
            side_effect=lambda _pending, action, _success: action()
        )
        window._converge_shortcut_broker = Mock()
        window._converge_history_monitor = Mock()
        with (
            patch(
                "readerpc_launcher.stop_readerpc_voice",
                side_effect=RuntimeError("cleanup receipt failed"),
            ),
            patch("readerpc_launcher.enable_readerpc_voice") as enable,
            self.assertRaisesRegex(RuntimeError, "cleanup receipt failed"),
        ):
            window._restart_voice_with_intent("pending", "done")
        window._converge_shortcut_broker.assert_not_called()
        window._converge_history_monitor.assert_not_called()
        enable.assert_not_called()
        kind, rollback = window.events.get_nowait()
        self.assertEqual(kind, "intent-rollback")
        self.assertTrue(rollback["voice_enabled"])

    def test_reconcile_compares_app_intent_with_applied_not_desired_ui(self) -> None:
        window = self.window_without_tk()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            window.bridge_paths.runtime_status.parent = root
            (root / "readerpc-service-mode.json").write_text(
                json.dumps({
                    "contract": "readerpc-service-mode/1",
                    "mode": "full",
                    "voiceEnabled": False,
                    "snapshotViewer": "visible",
                }),
                encoding="utf-8",
            )
            window.bridge_only = Mock()
            window.bridge_only.get.return_value = False
            window.voice_enabled = Mock()
            window.voice_enabled.get.return_value = False
            window._applied_service_mode = "full"
            window._applied_voice_enabled = True
            window._save_current_preferences = Mock()
            window._restart_voice_with_intent = Mock()
            window._reconcile_service_mode_intent()
        window._restart_voice_with_intent.assert_called_once()

    def test_manual_retry_only_starts_when_server_service_is_offline(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(
            side_effect=(
                SimpleNamespace(service_online=True),
                SimpleNamespace(service_online=False),
            )
        )
        window._start_voice_task = Mock()
        window.toggle_voice()
        window.toggle_voice()
        window._start_voice_task.assert_called_once_with()

    def test_stop_task_delegates_to_readerpc_stop(self) -> None:
        window = self.window_without_tk()
        window._run_task = Mock(side_effect=lambda _pending, action, _success: action())
        with patch("readerpc_launcher.stop_readerpc_voice") as stop:
            window._stop_voice_task()
        self.assertTrue(stop.call_args.kwargs["disable_configuration"])

    def test_offline_monitor_marks_recovering_before_retry_and_only_once(self) -> None:
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
                "readerpc_launcher.write_recovering_reader_context_snapshot",
                side_effect=lambda _paths: calls.append("recovering"),
            ) as recovering,
            patch(
                "readerpc_launcher.write_disabled_reader_context_snapshot"
            ) as disabled,
            patch("readerpc_launcher.time.monotonic", return_value=31.0),
        ):
            window._ensure_voice_online()
            window._ensure_voice_online()
        self.assertEqual(calls, ["recovering", "retry"])
        recovering.assert_called_once_with(window.bridge_paths)
        disabled.assert_not_called()
        self.assertEqual(window.root.after.call_count, 2)

    def test_maintenance_hold_keeps_the_keepalive_off_the_installer(self) -> None:
        # 装桥要先停 Direct 再原子替换 exe；保活一重拉，替换就撞 WinError 5，
        # 整个安装事务回滚（2026-09-03 / 09-04 各一次）。见标记就等着。
        window = self.window_without_tk()
        window.last_voice_start_attempt = 0.0
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=False,
            configuration_enabled=True,
        ))
        window._start_voice_task = Mock()
        with (
            patch(
                "readerpc_launcher.read_direct_maintenance_hold",
                return_value="安装 Direct 0.1.275",
            ),
            patch(
                "readerpc_launcher.write_recovering_reader_context_snapshot"
            ) as recovering,
            patch("readerpc_launcher._boot_log") as boot_log,
            patch("readerpc_launcher.time.monotonic", return_value=31.0),
        ):
            window._ensure_voice_online()
            window._ensure_voice_online()
        window._start_voice_task.assert_not_called()
        recovering.assert_not_called()
        self.assertEqual(boot_log.call_count, 1, "同一条原因只播报一次")
        self.assertEqual(window.voice_maintenance_notice, "安装 Direct 0.1.275")
        # 标记一撤，保活立刻恢复（否则语音要等到下次事件才回来）
        with (
            patch(
                "readerpc_launcher.read_direct_maintenance_hold",
                return_value=None,
            ),
            patch("readerpc_launcher.write_recovering_reader_context_snapshot"),
            patch("readerpc_launcher._boot_log") as resumed,
            patch("readerpc_launcher.time.monotonic", return_value=62.0),
        ):
            window._ensure_voice_online()
        window._start_voice_task.assert_called_once_with()
        self.assertIsNone(window.voice_maintenance_notice)
        self.assertEqual(resumed.call_count, 1, "恢复也要出声")

    def test_server_recovers_listener_even_if_derived_config_is_off(self) -> None:
        window = self.window_without_tk()
        window.voice_snapshot_offline_marked = True
        window._voice_status = Mock(side_effect=(
            SimpleNamespace(service_online=True, configuration_enabled=True),
            SimpleNamespace(service_online=False, configuration_enabled=False),
        ))
        window._start_voice_task = Mock()
        with (
            patch(
                "readerpc_launcher.write_recovering_reader_context_snapshot"
            ) as recovering,
            patch("readerpc_launcher.time.monotonic", return_value=31.0),
        ):
            window._ensure_voice_online()
            window._ensure_voice_online()
        recovering.assert_called_once_with(window.bridge_paths)
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
                "readerpc_launcher.write_recovering_reader_context_snapshot"
            ) as recovering,
        ):
            window._ensure_voice_online()
        recovering.assert_called_once_with(window.bridge_paths)
        window._start_voice_task.assert_not_called()

    def test_monitor_does_not_clobber_snapshot_during_known_start(self) -> None:
        window = self.window_without_tk()
        window.busy = True
        window.voice_start_in_progress = True
        window._voice_status = Mock()
        with (
            patch(
                "readerpc_launcher.write_recovering_reader_context_snapshot"
            ) as recovering,
        ):
            window._ensure_voice_online()
        window._voice_status.assert_not_called()
        recovering.assert_not_called()

    def test_monitor_recovering_failure_is_visible_and_polling_continues(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=False,
            configuration_enabled=False,
        ))
        with patch(
            "readerpc_launcher.write_recovering_reader_context_snapshot",
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

    def test_obsolete_external_false_cannot_stop_running_server_service(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=True,
            configuration_enabled=True,
        ))
        window._start_voice_task = Mock()
        window._stop_voice_task = Mock()
        with patch(
            "readerpc_launcher.write_recovering_reader_context_snapshot"
        ) as recovering:
            window._ensure_voice_online()
        window._stop_voice_task.assert_not_called()
        recovering.assert_not_called()
        window._start_voice_task.assert_not_called()

    def test_server_online_state_never_depends_on_keepalive_file_read(self) -> None:
        window = self.window_without_tk()
        window._voice_status = Mock(return_value=SimpleNamespace(
            service_online=True,
            configuration_enabled=True,
        ))
        window._start_voice_task = Mock()
        window._stop_voice_task = Mock()
        with patch(
            "readerpc_launcher.write_recovering_reader_context_snapshot"
        ) as recovering:
            window._ensure_voice_online()
        window._stop_voice_task.assert_not_called()
        window._start_voice_task.assert_not_called()
        recovering.assert_not_called()

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

    def test_no_voice_refresh_does_not_probe_codex_activity(self) -> None:
        window = self.window_without_tk()
        window.voice_enabled = Mock()
        window.voice_enabled.get.return_value = False
        window.bridge_only = Mock()
        window.bridge_only.get.return_value = False
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
        with (
            patch("readerpc_launcher.read_codex_voice_activity") as activity,
            patch(
                "readerpc_launcher.read_reader_context_status",
                return_value=ReaderContextStatus(False, False, "", "", None),
            ),
            patch("readerpc_launcher.write_readerpc_status") as publish,
            patch("readerpc_launcher.time.monotonic", return_value=20.0),
        ):
            window.refresh()
        activity.assert_not_called()
        self.assertEqual(
            window.voice_status.configure.call_args.kwargs["text"],
            "Reader 服务在线 · 语音功能已关闭",
        )
        self.assertEqual(
            publish.call_args.kwargs["voice"]["codexVoiceStatus"],
            "disabled",
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

    def test_takeover_closes_only_pyinstaller_gui_leaf_then_stops_direct(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = readerpc_launcher.BridgePaths.for_root(root)
            direct_pid = 3201
            parent_pid = 3101
            gui_pid = 3102
            reader_exe = root / "ReaderPC-Server.exe"
            self.write_takeover_direct_identity(paths, direct_pid)
            runner = self.MutableProcessRunner({
                parent_pid: reader_exe,
                gui_pid: reader_exe,
                direct_pid: paths.native_host.resolve(),
            })
            rows = [
                (parent_pid, 1, "ReaderPC-Server.exe", str(reader_exe)),
                (gui_pid, parent_pid, "ReaderPC-Server.exe", str(reader_exe)),
                (
                    direct_pid,
                    gui_pid,
                    "bw-computer-voice-audio.exe",
                    f'"{paths.native_host}" --direct-serve --config x',
                ),
            ]
            closed: list[int] = []

            def close_gui(pid: int) -> bool:
                closed.append(pid)
                runner.executables.pop(gui_pid, None)
                runner.executables.pop(parent_pid, None)
                return True

            def stop_direct(_paths, _runner, **kwargs) -> bool:
                self.assertFalse(kwargs["force_on_cleanup_failure"])
                runner.executables.pop(direct_pid, None)
                paths.service_record.unlink()
                paths.shutdown_receipt.write_text(json.dumps({
                    "contract": "readerpc-direct-shutdown-result/1",
                    "serviceInstanceId": "a" * 32,
                    "state": "success",
                }), encoding="utf-8")
                return True

            with patch(
                "readerpc_launcher.stop_direct_service",
                side_effect=stop_direct,
            ) as stop:
                stale = terminate_stale_instances(
                    paths,
                    runner,
                    process_rows=rows,
                    close_process=close_gui,
                )
        self.assertEqual(stale, [parent_pid, gui_pid])
        self.assertEqual(closed, [gui_pid])
        stop.assert_called_once()
        self.assertEqual(runner.terminations, [])

    def test_takeover_asks_out_of_band_before_it_asks_the_window(self) -> None:
        # taskkill /PID 发的是 WM_CLOSE，而收进托盘的 ReaderPC 没有顶层窗口
        # （实测 MainWindowHandle = 0）—— 只有窗口这一条路就必然超时（2026-09-05
        # 两次换代失败）。所以文件请求必须**先于** close 尝试写下。
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = readerpc_launcher.BridgePaths.for_root(root)
            data_root = root / "BWReader"
            data_root.mkdir()
            readerpc_paths = readerpc_launcher.ReaderPCPaths(
                local_root=data_root,
                status_file=data_root / "status.json",
                preferences_file=data_root / "config.json",
            )
            gui_pid = 5101
            reader_exe = root / "ReaderPC-Server.exe"
            runner = self.MutableProcessRunner({gui_pid: reader_exe})
            rows = [(gui_pid, 1, "ReaderPC-Server.exe", str(reader_exe))]
            seen: list[str | None] = []

            def close_gui(pid: int) -> bool:
                # ⚠ 直接读文件,不走 read_readerpc_exit_request ——
                #   那个函数**故意不认自己写的**(接管方与本测试是同一个进程),
                #   而这条契约要验的是"文件在 close 之前就已经写下了"。
                try:
                    payload = json.loads(
                        readerpc_paths.exit_request_file.read_text("utf-8"))
                    seen.append(payload.get("reason"))
                except OSError:
                    seen.append(None)
                runner.executables.pop(gui_pid, None)
                return True

            with patch.object(
                readerpc_launcher.ReaderPCPaths,
                "discover",
                staticmethod(lambda: readerpc_paths),
            ):
                stale = terminate_stale_instances(
                    paths,
                    runner,
                    process_rows=rows,
                    close_process=close_gui,
                )
        self.assertEqual(stale, [gui_pid])
        self.assertEqual(seen, ["新一代 ReaderPC 正在接管"])
        # 接管完成后必须清掉，否则本代第一次刷新就自己退出（"双击没反应"）
        self.assertFalse(readerpc_paths.exit_request_file.exists())

    def test_takeover_waits_if_record_disappears_while_direct_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = readerpc_launcher.BridgePaths.for_root(root)
            direct_pid = 4201
            gui_pid = 4101
            reader_exe = root / "ReaderPC-Server.exe"
            self.write_takeover_direct_identity(paths, direct_pid)
            runner = self.MutableProcessRunner({
                gui_pid: reader_exe,
                direct_pid: paths.native_host.resolve(),
            })
            rows = [
                (gui_pid, 1, "ReaderPC-Server.exe", str(reader_exe)),
                (
                    direct_pid,
                    gui_pid,
                    "bw-computer-voice-audio.exe",
                    f'"{paths.native_host}" --direct-serve --config x',
                ),
            ]
            clock = [0.0]
            waits = [0]

            def close_gui(_pid: int) -> bool:
                runner.executables.pop(gui_pid, None)
                paths.service_record.unlink()
                return True

            def finish_cleanup(delay: float) -> None:
                clock[0] += delay
                waits[0] += 1
                if waits[0] == 1:
                    runner.executables.pop(direct_pid, None)
                    paths.shutdown_receipt.write_text(json.dumps({
                        "contract": "readerpc-direct-shutdown-result/1",
                        "serviceInstanceId": "a" * 32,
                        "state": "success",
                    }), encoding="utf-8")

            stale = terminate_stale_instances(
                paths,
                runner,
                process_rows=rows,
                close_process=close_gui,
                sleeper=finish_cleanup,
                monotonic=lambda: clock[0],
                timeout_seconds=1.0,
            )
        self.assertEqual(stale, [gui_pid])
        self.assertEqual(waits[0], 1)
        self.assertEqual(runner.terminations, [])

    def test_takeover_refuses_live_direct_after_record_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = readerpc_launcher.BridgePaths.for_root(root)
            direct_pid = 5201
            gui_pid = 5101
            reader_exe = root / "ReaderPC-Server.exe"
            self.write_takeover_direct_identity(paths, direct_pid)
            runner = self.MutableProcessRunner({
                gui_pid: reader_exe,
                direct_pid: paths.native_host.resolve(),
            })
            rows = [
                (gui_pid, 1, "ReaderPC-Server.exe", str(reader_exe)),
                (
                    direct_pid,
                    gui_pid,
                    "bw-computer-voice-audio.exe",
                    f'"{paths.native_host}" --direct-serve --config x',
                ),
            ]
            clock = [0.0]

            def close_gui(_pid: int) -> bool:
                runner.executables.pop(gui_pid, None)
                paths.service_record.unlink()
                return True

            def advance(delay: float) -> None:
                clock[0] += delay

            with self.assertRaisesRegex(
                readerpc_launcher.ReaderPCServiceError,
                "未完成退出清理",
            ):
                terminate_stale_instances(
                    paths,
                    runner,
                    process_rows=rows,
                    close_process=close_gui,
                    sleeper=advance,
                    monotonic=lambda: clock[0],
                    timeout_seconds=0.3,
                )
        self.assertEqual(runner.terminations, [])
        self.assertIn(direct_pid, runner.executables)

    def test_takeover_requires_success_receipt_after_direct_exits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = readerpc_launcher.BridgePaths.for_root(root)
            direct_pid = 6201
            gui_pid = 6101
            reader_exe = root / "ReaderPC-Server.exe"
            self.write_takeover_direct_identity(paths, direct_pid)
            runner = self.MutableProcessRunner({
                gui_pid: reader_exe,
                direct_pid: paths.native_host.resolve(),
            })
            rows = [
                (gui_pid, 1, "ReaderPC-Server.exe", str(reader_exe)),
                (
                    direct_pid,
                    gui_pid,
                    "bw-computer-voice-audio.exe",
                    f'"{paths.native_host}" --direct-serve --config x',
                ),
            ]

            def close_without_receipt(_pid: int) -> bool:
                runner.executables.pop(gui_pid, None)
                runner.executables.pop(direct_pid, None)
                paths.service_record.unlink()
                return True

            with self.assertRaisesRegex(
                readerpc_launcher.ReaderPCServiceError,
                "没有可验证的清理成功回执",
            ):
                terminate_stale_instances(
                    paths,
                    runner,
                    process_rows=rows,
                    close_process=close_without_receipt,
                )
        self.assertEqual(runner.terminations, [])

    def test_takeover_blocks_orphan_direct_without_force_kill(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = readerpc_launcher.BridgePaths.for_root(root)
            paths.native_host.parent.mkdir(parents=True, exist_ok=True)
            paths.native_host.write_bytes(b"direct placeholder")
            direct_pid = 7201
            runner = self.MutableProcessRunner({
                direct_pid: paths.native_host.resolve(),
            })
            rows = [(
                direct_pid,
                1,
                "bw-computer-voice-audio.exe",
                f'"{paths.native_host}" --direct-serve --config x',
            )]
            with self.assertRaisesRegex(
                readerpc_launcher.ReaderPCServiceError,
                "未被严格服务记录认证",
            ):
                terminate_stale_instances(
                    paths,
                    runner,
                    process_rows=rows,
                    close_process=lambda _pid: True,
                )
        self.assertEqual(runner.terminations, [])

    def test_gui_owns_shortcut_broker_for_entire_window_lifetime(self) -> None:
        paths = SimpleNamespace(preferences_file=Path("C:/readerpc.json"))
        bridge_paths = Mock()
        process_runner = Mock()
        preferences = {
            "keepPcPreprocessingOnline": True,
            "serviceMode": "full",
            "voiceEnabled": True,
            "snapshotViewerHidden": False,
            "hideVoiceOrb": False,
            "autoStartOnBoot": False,
        }
        with (
            patch(
                "readerpc_launcher.BridgePaths.discover",
                return_value=bridge_paths,
            ),
            patch(
                "readerpc_launcher.WindowsProcessRunner",
                return_value=process_runner,
            ),
            patch(
                "readerpc_launcher.terminate_stale_instances",
                return_value=[],
            ) as takeover,
            patch("readerpc_launcher.SingleInstance") as instance,
            patch(
                "readerpc_launcher.ReaderPCPaths.discover",
                return_value=paths,
            ),
            patch(
                "readerpc_launcher.load_preferences",
                return_value=preferences,
            ),
            patch(
                "readerpc_launcher.merge_preferences_with_service_intent",
                return_value=preferences,
            ),
            patch(
                "readerpc_launcher.prepare_readerpc_shortcut_broker"
            ) as prepare_broker,
            patch("readerpc_launcher.tk.Tk") as make_root,
            patch("readerpc_launcher.ReaderPCWindow") as make_window,
        ):
            instance.return_value.acquire.return_value = True
            self.assertEqual(main([]), 0)

        prepare_broker.assert_called_once_with(voice_shortcut_enabled=True)
        takeover.assert_called_once_with(bridge_paths, process_runner)
        prepare_broker.return_value.close.assert_not_called()
        make_window.return_value.close_shortcut_broker.assert_called_once_with()
        make_window.assert_called_once_with(
            make_root.return_value,
            bridge_paths=bridge_paths,
            process_runner=process_runner,
            readerpc_paths=paths,
            shortcut_broker=prepare_broker.return_value,
        )
        make_root.return_value.mainloop.assert_called_once_with()

    def test_readerpc_retires_owned_logon_bootstrap_and_owns_broker(self) -> None:
        inspection = SimpleNamespace(exists=True, owned=True)
        with (
            patch(
                "readerpc_launcher.inspect_bootstrap_task",
                return_value=inspection,
            ),
            patch(
                "readerpc_launcher.remove_bootstrap_task",
                return_value=True,
            ) as remove_task,
            patch("readerpc_launcher.stop_readerpc_voice") as stop_voice,
            patch(
                "readerpc_launcher.WindowsShortcutBroker",
            ) as broker,
        ):
            self.assertIs(
                prepare_readerpc_shortcut_broker(),
                broker.return_value,
            )
        remove_task.assert_called_once()
        stop_voice.assert_called_once()
        self.assertFalse(stop_voice.call_args.kwargs["disable_configuration"])
        broker.return_value.start.assert_called_once_with()

    def test_readerpc_rejects_unowned_logon_broker(self) -> None:
        with (
            patch(
                "readerpc_launcher.inspect_bootstrap_task",
                return_value=SimpleNamespace(exists=True, owned=False),
            ),
            patch("readerpc_launcher.remove_bootstrap_task") as remove_task,
            patch("readerpc_launcher.stop_readerpc_voice") as stop_voice,
            patch("readerpc_launcher.WindowsShortcutBroker") as broker,
        ):
            with self.assertRaisesRegex(Exception, "不属于 Reader"):
                prepare_readerpc_shortcut_broker()
        remove_task.assert_not_called()
        stop_voice.assert_not_called()
        broker.assert_not_called()

    def test_readerpc_owns_broker_when_logon_task_is_absent(self) -> None:
        with (
            patch(
                "readerpc_launcher.inspect_bootstrap_task",
                return_value=SimpleNamespace(exists=False, owned=False),
            ),
            patch("readerpc_launcher.remove_bootstrap_task") as remove_task,
            patch("readerpc_launcher.stop_readerpc_voice") as stop_voice,
            patch("readerpc_launcher.WindowsShortcutBroker") as broker,
        ):
            self.assertIs(
                prepare_readerpc_shortcut_broker(),
                broker.return_value,
            )
        remove_task.assert_not_called()
        stop_voice.assert_called_once()
        broker.return_value.start.assert_called_once_with()

    def test_voice_disabled_retires_old_owner_without_creating_f24_broker(self) -> None:
        with (
            patch(
                "readerpc_launcher.inspect_bootstrap_task",
                return_value=SimpleNamespace(exists=False, owned=False),
            ),
            patch("readerpc_launcher.remove_bootstrap_task") as remove_task,
            patch("readerpc_launcher.stop_readerpc_voice") as stop_voice,
            patch("readerpc_launcher.WindowsShortcutBroker") as broker,
        ):
            self.assertIsNone(
                prepare_readerpc_shortcut_broker(
                    voice_shortcut_enabled=False
                )
            )
        remove_task.assert_not_called()
        stop_voice.assert_called_once()
        broker.assert_not_called()

    def test_retired_broker_pipe_is_replaced_after_bounded_unwind(self) -> None:
        first = Mock()
        replacement = Mock()
        first.start.side_effect = ShortcutBrokerError("pipe still owned")
        with (
            patch(
                "readerpc_launcher.inspect_bootstrap_task",
                return_value=SimpleNamespace(exists=True, owned=True),
            ),
            patch("readerpc_launcher.remove_bootstrap_task"),
            patch("readerpc_launcher.stop_readerpc_voice"),
            patch(
                "readerpc_launcher.WindowsShortcutBroker",
                side_effect=(first, replacement),
            ) as broker_type,
            patch.object(
                broker_type,
                "probe_available",
                side_effect=(True, False),
            ),
            patch(
                "readerpc_launcher.time.monotonic",
                side_effect=(0.0, 0.0, 0.1),
            ),
            patch("readerpc_launcher.time.sleep"),
        ):
            self.assertIs(prepare_readerpc_shortcut_broker(), replacement)
        first.close.assert_called_once_with()
        replacement.start.assert_called_once_with()


class RearmCodexVoiceTests(unittest.TestCase):
    """keepalive 必须真的**翻转**过去，而且**不能把用户正在进行的通话关掉**。

    翻转的必要性：C# 的 ApplyKeepActiveChange 里 `if (previous == enabled) return false;`
    —— 值没变就不动，而自动恢复预算耗尽后设下的 _automaticRecoveryBlocked
    只有真跃迁才会清。ReaderPC 过去每次"恢复"都只是再写一遍 true。

    不能伤人的必要性（2026-08-18 调查）：C# 的 ReconcileKeepActiveAsync 读到 false 时，
    若麦克风台账显示语音正开着，会发 F24 **主动把通话关掉**。而 C# 是 5 秒纯轮询、
    没有文件监视 —— 第一版只留 0.35 秒窗口，等于九成空操作、一成可能伤人。
    """

    def _paths(self, root: Path):
        return SimpleNamespace(runtime_status=root / "runtime" / "runtime-status.json")

    def _activity(self, active):
        return lambda: SimpleNamespace(active=active, status="available", generation=None)

    def _record(self, seen):
        real = set_codex_voice_keep_active

        def record(bridge_paths, enabled):
            seen.append(enabled)
            real(bridge_paths, enabled)

        return record

    def test_refuses_to_flip_while_a_call_is_live(self):
        # 伤害的触发条件正是"台账 active"。这是最重要的一条：宁可不解封，
        # 也不能去关用户正在进行的通话。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runtime").mkdir(parents=True)
            paths = self._paths(root)
            set_codex_voice_keep_active(paths, True)
            seen: list[bool] = []
            with (
                patch("readerpc_launcher.set_codex_voice_keep_active", self._record(seen)),
                patch("readerpc_launcher.time.sleep"),
            ):
                flipped = rearm_codex_voice_keep_active(
                    paths, activity_reader=self._activity(True)
                )
            self.assertFalse(flipped)
            self.assertEqual(seen, [], "通话进行中一个字都不该写")
            self.assertIs(read_codex_voice_keep_active(paths), True)

    def test_unreadable_activity_is_treated_as_live(self):
        # 读不到台账就当"可能开着"：失败方向必须偏向不伤人。
        def boom():
            raise OSError("registry unavailable")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runtime").mkdir(parents=True)
            paths = self._paths(root)
            set_codex_voice_keep_active(paths, True)
            seen: list[bool] = []
            with (
                patch("readerpc_launcher.set_codex_voice_keep_active", self._record(seen)),
                patch("readerpc_launcher.time.sleep"),
            ):
                self.assertFalse(
                    rearm_codex_voice_keep_active(paths, activity_reader=boom)
                )
            self.assertEqual(seen, [])

    def test_flips_false_then_true_when_voice_is_down(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runtime").mkdir(parents=True)
            paths = self._paths(root)
            set_codex_voice_keep_active(paths, True)
            seen: list[bool] = []
            with (
                patch("readerpc_launcher.set_codex_voice_keep_active", self._record(seen)),
                patch("readerpc_launcher.time.sleep"),
                patch("readerpc_launcher.time.monotonic", side_effect=[0.0, 1.0, 99.0]),
            ):
                flipped = rearm_codex_voice_keep_active(
                    paths, activity_reader=self._activity(False)
                )
            self.assertTrue(flipped)
            self.assertEqual(seen, [False, True], "必须先落到 false 才谈得上跃迁")
            self.assertIs(read_codex_voice_keep_active(paths), True)

    def test_window_outlives_one_csharp_poll_interval(self):
        # C# 是 5 秒纯轮询；窗口窄于一个周期，翻了也看不见（第一版 0.35 秒 ≈ 7%）。
        import inspect

        source = inspect.getsource(rearm_codex_voice_keep_active)
        self.assertIn("_KEEPALIVE_POLL_SECONDS", source)
        self.assertGreater(
            readerpc_launcher._KEEPALIVE_POLL_SECONDS, 4.0,
            "这个常数必须跟 C# 的 PeriodicTimer 对齐",
        )

    def test_rearm_does_not_flip_when_already_disabled(self):
        # 本来就是关的 => 没有封锁可解，直接置开即可，不必先制造一次停机。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runtime").mkdir(parents=True)
            paths = self._paths(root)
            set_codex_voice_keep_active(paths, False)
            seen: list[bool] = []
            with patch("readerpc_launcher.set_codex_voice_keep_active", self._record(seen)):
                flipped = rearm_codex_voice_keep_active(
                    paths, activity_reader=self._activity(False)
                )
            self.assertFalse(flipped)
            self.assertEqual(seen, [True])



class VoiceFailureDescriptionTests(unittest.TestCase):
    """失败原因一直写在 runtime-status 里，只是从来没人读出来给人看。"""

    def test_known_code_becomes_readable_text_with_stage(self):
        # 用真实存在的 code。之前这里写的 ..._PORT_BUSY 是我凭空造的——C# 侧
        # 167 个码里根本没有它，于是这条测试在验一件不会发生的事。
        text = describe_voice_failure({
            "failureId": "failure-abcdefghijklmnop",
            "code": "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED",
            "stage": "start",
            "hresult": None,
            "atUtc": "2026-08-18T00:00:00Z",
        })
        self.assertEqual(text, "音频链路建立失败；未能确认通话就绪（start）")

    def test_keepalive_not_confirmed_tells_the_user_what_to_do(self):
        # 实测（低层键盘钩子）确认：F24 确实进了系统输入流，是 Codex 不响应。
        # 这条路只有一个可执行的自救动作 —— 重启 Codex 让它重新注册全局热键。
        text = describe_voice_failure({
            "failureId": "failure-abcdefghijklmnop",
            "code": "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED",
            "stage": "codex-voice-keepalive",
            "hresult": None,
            "atUtc": "2026-08-18T00:00:00Z",
            "exceptionType": None,
        })
        self.assertIn("重启 Codex", text)
        self.assertNotIn("BW_", text)

    def test_exception_type_is_appended_not_the_message(self):
        # 桥带回来的是**异常类型名**，不是 message —— message 里可能有设备/端点标识，
        # 桥那边刻意不外传（自测里那条异常就叫 secret-endpoint-id-must-never-be-serialized）。
        text = describe_voice_failure({
            "failureId": "failure-abcdefghijklmnop",
            "code": "BW_COMPUTER_VOICE_DIRECT_VOICE_START_NOT_CONFIRMED",
            "stage": "start",
            "hresult": None,
            "atUtc": "2026-08-18T00:00:00Z",
            "exceptionType": "TimeoutException",
        })
        self.assertIn("音频链路建立失败", text)
        self.assertIn("TimeoutException", text)


    def test_wildcard_code_without_message_is_still_labelled(self):
        # INTERNAL_FAILURE 这种通配码没有 message 就等于零信息 —— 至少得说清是哪一段。
        text = describe_voice_failure({
            "failureId": "failure-abcdefghijklmnop",
            "code": "BW_COMPUTER_VOICE_DIRECT_INTERNAL_FAILURE",
            "stage": "codex-voice-keepalive",
            "hresult": None,
            "atUtc": "2026-08-18T00:00:00Z",
        })
        self.assertIn("codex-voice-keepalive", text)


    def test_table_miss_degrades_to_family_plus_raw_code(self):
        # 167 个码不可能手工翻全。表外的必须仍然"说得出是哪一类"并保留原码，
        # 而不是把一串大写英文糊到用户脸上。
        text = describe_voice_failure({
            "failureId": "failure-abcdefghijklmnop",
            "code": "BW_COMPUTER_VOICE_DIRECT_AUDIO_ROUTE_LEASE_ENDED",
            "stage": "start",
            "hresult": None,
            "atUtc": "2026-08-18T00:00:00Z",
        })
        self.assertIn("音频线路", text)
        self.assertIn("AUDIO_ROUTE_LEASE_ENDED", text)
        self.assertNotIn("BW_COMPUTER_VOICE_DIRECT_", text)


    def test_missing_error_is_silent(self):
        self.assertEqual(describe_voice_failure(None), "")


class AutostartScriptCheckTests(unittest.TestCase):
    """自启链的两个 bug 都是编码问题，而"文件已生成"完全看不出来。"""

    def test_generated_scripts_satisfy_interpreter_encoding_rules(self):
        checks = autostart_script_checks()
        self.assertTrue(checks["autostart-ps1-has-bom"], "PowerShell 5.1 靠 BOM 认中文")
        self.assertTrue(checks["autostart-vbs-no-bom"], "VBScript 会把 BOM 当第一个字符")
        self.assertTrue(checks["autostart-vbs-ascii"])
        self.assertTrue(checks["autostart-ps1-parses"], "让 PowerShell 自己说它认不认")

    def test_checks_do_not_touch_the_installed_scripts(self):
        installed = Path(os.environ.get("LOCALAPPDATA", "")) / "BWReader" / "ReaderPC-Server"
        before = None
        target = installed / "start-readerpc.vbs"
        if target.exists():
            before = target.read_bytes()
        autostart_script_checks()
        if before is not None:
            self.assertEqual(target.read_bytes(), before, "自测绝不能改已安装的脚本")


class VoiceOrbSignatureTest(unittest.TestCase):
    """语音球窗口签名（2026-09-06 用户：换正式版 Codex 后「隐藏语音球失灵」）。

    EnumWindows 实测：正式版的语音球是两个 TOPMOST + TOOLWINDOW 的 Chrome_WidgetWin_1，
    标题是 "ChatGPT" 而不是 Beta 时期的 "Codex"。只认一个标题就一个都对不上。
    """

    def test_signature_accepts_both_client_titles(self) -> None:
        import readerpc_launcher

        self.assertIn("Codex", readerpc_launcher.ORB_WINDOW_TITLES)
        self.assertIn("ChatGPT", readerpc_launcher.ORB_WINDOW_TITLES)
        for exe in ("chatgpt.exe", "chatgpt (beta).exe", "codex.exe"):
            self.assertIn(exe, readerpc_launcher.ORB_OWNER_EXES)

    def test_finder_uses_the_shared_signature_tables(self) -> None:
        import inspect

        import readerpc_launcher

        source = inspect.getsource(readerpc_launcher.find_voice_orb_windows)
        self.assertIn("ORB_WINDOW_TITLES", source)
        self.assertIn("ORB_OWNER_EXES", source)
        self.assertNotIn('!= "Codex"', source)


if __name__ == "__main__":
    unittest.main()
