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
    load_preferences,
    save_preferences,
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

    def test_shutdown_stops_pc_and_direct_services(self) -> None:
        pc_ocr = Mock()
        status = SimpleNamespace(service_online=True, configuration_enabled=True)
        with (
            patch("readerpc_launcher.read_direct_status", return_value=status),
            patch("readerpc_launcher.disable_and_stop_direct_service") as stop_voice,
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
                "readerpc_launcher.disable_and_stop_direct_service",
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
            patch("readerpc_launcher.disable_and_stop_direct_service") as stop_voice,
            patch("readerpc_launcher.stop_direct_service") as stop_orphan,
        ):
            stop_readerpc_services(bridge_paths, Mock(), pc_ocr)
        pc_ocr.stop.assert_called_once_with()
        stop_voice.assert_not_called()
        stop_orphan.assert_not_called()

    def test_shutdown_stops_owned_direct_record_after_opt_out(self) -> None:
        pc_ocr = Mock()
        status = SimpleNamespace(service_online=False, configuration_enabled=False)
        bridge_paths = Mock()
        bridge_paths.service_record.exists.return_value = True
        with (
            patch("readerpc_launcher.read_direct_status", return_value=status),
            patch("readerpc_launcher.disable_and_stop_direct_service") as disable_voice,
            patch("readerpc_launcher.stop_direct_service") as stop_orphan,
        ):
            stop_readerpc_services(bridge_paths, Mock(), pc_ocr)
        disable_voice.assert_not_called()
        stop_orphan.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
