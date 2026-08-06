from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from bridge_core import CaptureEndpoint  # noqa: E402
from bridge_core import BridgeError, DirectStatus  # noqa: E402
from bridge_core import RenderEndpoint  # noqa: E402
from control_plane import TaskInspection  # noqa: E402
from desktop_launcher import BridgeWindow, main  # noqa: E402


VIRTUAL_MICROPHONE = RenderEndpoint("id-a", "virtual mic A")
VIRTUAL_SPEAKER = RenderEndpoint("id-b", "virtual speaker B")
VIRTUAL_MICROPHONE_CAPTURE = CaptureEndpoint(
    "{0.0.1.00000000}.{22222222-2222-2222-2222-222222222222}",
    "virtual mic A recording side",
)


class FakeWidget:
    def __init__(self) -> None:
        self.values: list[dict[str, object]] = []

    def configure(self, **values) -> None:
        self.values.append(values)


class FakeCombo(FakeWidget):
    def __init__(self, current_index: int = -1) -> None:
        super().__init__()
        self.current_index = current_index
        self.text = "stale"

    def current(self, index: int | None = None) -> int:
        if index is not None:
            self.current_index = index
        return self.current_index

    def set(self, value: str) -> None:
        self.text = value
        self.current_index = -1


class DesktopLauncherTests(unittest.TestCase):
    def test_busy_state_survives_missing_optional_controls(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.busy = False
        window.enable_button = FakeWidget()
        window.start_button = FakeWidget()
        window.refresh_button = FakeWidget()
        window.footer = FakeWidget()
        window.set_busy(True, "working")
        self.assertTrue(window.busy)
        self.assertEqual(
            window.start_button.values[-1],
            {"state": "disabled"},
        )
        self.assertEqual(
            window.footer.values[-1],
            {"text": "working"},
        )

    def test_offline_render_uses_offline_markers_and_colors(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.config_status = FakeWidget()
        window.service_status = FakeWidget()
        window.reader_status = FakeWidget()
        window.error_status = FakeWidget()
        window.render_status(
            DirectStatus(
                configuration_enabled=True,
                service_online=False,
                reader_connected=False,
                reason="runtime-status-offline-or-stale",
                pid=101,
            )
        )
        service = window.service_status.values[-1]
        reader = window.reader_status.values[-1]
        self.assertTrue(str(service["text"]).startswith("○"))
        self.assertEqual(service["foreground"], "#6b7280")
        self.assertTrue(str(reader["text"]).startswith("○"))
        self.assertEqual(reader["foreground"], "#6b7280")

    def test_runtime_failure_is_rendered_without_endpoint_details(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.config_status = FakeWidget()
        window.service_status = FakeWidget()
        window.reader_status = FakeWidget()
        window.error_status = FakeWidget()
        window.render_status(
            DirectStatus(
                True,
                True,
                False,
                "reader-not-connected",
                last_error={
                    "failureId": "failure-AAAAAAAAAAAAAAAA",
                    "code": "BW_COMPUTER_VOICE_AUDIO_FAILURE",
                    "stage": "virtual-speaker.validate",
                    "hresult": "0x80070490",
                    "atUtc": "2026-07-29T04:29:00Z",
                },
            )
        )
        rendered = str(window.error_status.values[-1]["text"])
        self.assertIn("BW_COMPUTER_VOICE_AUDIO_FAILURE", rendered)
        self.assertIn("0x80070490", rendered)
        self.assertNotIn("endpoint", rendered.casefold())

    def test_render_endpoint_refresh_has_no_default_fallback(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.virtual_microphone_combo = FakeCombo()
        window.virtual_speaker_combo = FakeCombo()
        window.render_endpoint_provider = lambda: [
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        ]
        window._refresh_render_endpoints()
        self.assertEqual(window.virtual_microphone_combo.current(), -1)
        self.assertEqual(window.virtual_speaker_combo.current(), -1)
        with self.assertRaises(BridgeError):
            window.selected_virtual_endpoints()

    def test_same_endpoint_is_rejected_by_ui_selection(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.render_endpoints = [VIRTUAL_MICROPHONE]
        window.virtual_microphone_combo = FakeCombo(0)
        window.virtual_speaker_combo = FakeCombo(0)
        with self.assertRaisesRegex(BridgeError, "不能相同"):
            window.selected_virtual_endpoints()

    def test_capture_refresh_explicitly_offers_v4_and_restores_v5_id(
        self,
    ) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.virtual_microphone_capture_combo = FakeCombo()
        window.capture_endpoint_provider = lambda: [
            VIRTUAL_MICROPHONE_CAPTURE
        ]
        window._refresh_capture_endpoints()
        self.assertEqual(
            window.virtual_microphone_capture_combo.current(),
            0,
        )
        self.assertIsNone(
            window.selected_virtual_microphone_capture_endpoint()
        )

        window._refresh_capture_endpoints(
            VIRTUAL_MICROPHONE_CAPTURE.endpoint_id
        )
        self.assertEqual(
            window.virtual_microphone_capture_combo.current(),
            1,
        )
        self.assertEqual(
            window.selected_virtual_microphone_capture_endpoint(),
            VIRTUAL_MICROPHONE_CAPTURE,
        )
        window._refresh_capture_endpoints(
            "{0.0.1.00000000}.{99999999-9999-9999-9999-999999999999}"
        )
        self.assertEqual(
            window.virtual_microphone_capture_combo.current(),
            -1,
        )
        with self.assertRaisesRegex(BridgeError, "选择已失效"):
            window.selected_virtual_microphone_capture_endpoint()

    def test_route_status_distinguishes_v5_from_legacy_v4(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.audio_route_status = FakeWidget()
        window.capture_endpoints = [VIRTUAL_MICROPHONE_CAPTURE]
        window._render_audio_route_config_status(
            {
                "virtualMicrophoneCaptureEndpointId":
                    VIRTUAL_MICROPHONE_CAPTURE.endpoint_id,
            }
        )
        self.assertIn(
            "已启用（/5",
            str(window.audio_route_status.values[-1]["text"]),
        )
        window._render_audio_route_config_status({})
        self.assertIn(
            "未启用（/4",
            str(window.audio_route_status.values[-1]["text"]),
        )

    def test_audio_settings_button_opens_documented_settings_page(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.root = object()
        window.footer = FakeWidget()
        with patch("desktop_launcher.os.startfile") as startfile:
            window.on_open_audio_settings()
        startfile.assert_called_once_with("ms-settings:apps-volume")
        self.assertIn(
            "未修改全局默认设备",
            str(window.footer.values[-1]["text"]),
        )

    def test_refresh_callback_does_not_start_any_action(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.footer = FakeWidget()
        calls: list[str] = []

        def refresh():
            calls.append("refresh")
            return DirectStatus(False, False, False, "offline")

        window.refresh_static = refresh
        window.on_refresh()
        self.assertEqual(calls, ["refresh"])
        self.assertIn(
            "没有启动服务",
            str(window.footer.values[-1]["text"]),
        )

    def test_runtime_source_has_no_chrome_cdp_or_websocket_import(self) -> None:
        forbidden_imports = {
            "websocket",
            "selenium",
            "playwright",
        }
        for name in (
            "bridge_core.py",
            "control_plane.py",
            "desktop_launcher.py",
        ):
            source = (SOURCE_ROOT / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imports: set[str] = set()
            function_names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".", 1)[0])
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function_names.add(node.name)
            self.assertTrue(imports.isdisjoint(forbidden_imports))
            self.assertNotIn("AudioPolicyConfigFactory", source)
            self.assertTrue(
                function_names.isdisjoint(
                    {
                        "find_chrome",
                        "cdp_targets",
                        "ensure_test_browser",
                        "extension_status",
                        "extension_pair",
                        "extension_stop",
                    }
                )
            )

    def test_all_new_control_mutations_require_confirmation(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window._confirm_mutation = lambda *_: False
        calls: list[str] = []
        window.run_task = lambda *_: calls.append("mutation")
        for method in (
            window.on_install_bootstrap,
            window.on_remove_bootstrap,
            window.on_apply_tailscale,
            window.on_remove_tailscale,
        ):
            method()
        self.assertEqual(calls, [])

    def test_existing_mutations_also_require_confirmation(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.root = object()
        window.paths = object()
        window.selected_virtual_endpoints = lambda: (
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        )
        window.selected_virtual_microphone_capture_endpoint = (
            lambda: None
        )
        window._confirm_mutation = lambda *_: False
        calls: list[str] = []
        window.run_task = lambda *_: calls.append("mutation")
        with patch(
            "desktop_launcher.legacy_microphone_config_requires_migration",
            return_value=False,
        ):
            window.on_enable_config()
            window.on_start()
        with patch(
            "desktop_launcher.messagebox.askyesno",
            return_value=False,
        ):
            window.on_disable_config()
        self.assertEqual(calls, [])

    def test_disable_and_stop_uses_atomic_bounded_helper(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.root = object()
        window.paths = object()
        window.process_runner = object()
        window.footer = FakeWidget()
        window.refresh_static = lambda: None
        calls: list[str] = []

        def immediate(_label, action, success):
            success(action())

        window.run_task = immediate
        with (
            patch(
                "desktop_launcher.messagebox.askyesno",
                return_value=True,
            ),
            patch(
                "desktop_launcher.disable_and_stop_direct_service",
                side_effect=lambda *_:
                    calls.append("disable-stop") or (True, True),
            ) as disable_stop,
        ):
            window.on_disable_config()
        self.assertEqual(calls, ["disable-stop"])
        disable_stop.assert_called_once_with(
            window.paths,
            window.process_runner,
        )

    def test_desktop_ui_has_no_pairing_action_or_pairing_copy(self) -> None:
        source = (SOURCE_ROOT / "desktop_launcher.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("on_generate_pairing", source)
        self.assertNotIn("generate_pair_button", source)
        self.assertNotIn("一次性配对码", source)
        self.assertIn("experimentalSingleUserMode", (
            SOURCE_ROOT / "bridge_core.py"
        ).read_text(encoding="utf-8"))

    def test_desktop_ui_warns_that_listed_endpoints_are_not_created_by_bridge(
        self,
    ) -> None:
        source = (SOURCE_ROOT / "desktop_launcher.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("桥接程序本身不会创建", source)
        self.assertIn("不要把 Realtek、Steam、Oculus", source)
        self.assertIn("两根彼此独立、已签名的虚拟音频线缆", source)
        self.assertIn("不会从 A 的播放端 ID 推导", source)
        self.assertIn("挂断后恢复原选择", source)

    def test_enable_with_capture_confirms_fixed_audio_bus(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.root = object()
        window.paths = object()
        window.selected_virtual_endpoints = lambda: (
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        )
        window.selected_virtual_microphone_capture_endpoint = (
            lambda: VIRTUAL_MICROPHONE_CAPTURE
        )
        window.render_endpoint_provider = lambda: [
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        ]
        window.capture_endpoint_provider = lambda: [
            VIRTUAL_MICROPHONE_CAPTURE
        ]
        confirmations: list[tuple[str, str]] = []
        window._confirm_mutation = lambda title, detail: (
            confirmations.append((title, detail)) or True
        )
        window.footer = FakeWidget()
        window.refresh_static = lambda: None

        def immediate(_label, action, success):
            success(action())

        window.run_task = immediate
        with (
            patch(
                "desktop_launcher.legacy_microphone_config_requires_migration",
                return_value=False,
            ),
            patch(
                "desktop_launcher.save_enabled_config",
                return_value={
                    "virtualMicrophoneCaptureEndpointId":
                        VIRTUAL_MICROPHONE_CAPTURE.endpoint_id,
                },
            ) as save,
        ):
            window.on_enable_config()
        self.assertIn("保存 /6", confirmations[0][1])
        self.assertIn("下行直接读取 B", confirmations[0][1])
        self.assertEqual(
            save.call_args.kwargs[
                "virtual_microphone_capture_endpoint_id"
            ],
            VIRTUAL_MICROPHONE_CAPTURE.endpoint_id,
        )
        self.assertEqual(
            save.call_args.kwargs["active_capture_endpoints"],
            [VIRTUAL_MICROPHONE_CAPTURE],
        )

    def test_enable_without_capture_requires_explicit_v4_confirmation(
        self,
    ) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.root = object()
        window.paths = object()
        window.selected_virtual_endpoints = lambda: (
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        )
        window.selected_virtual_microphone_capture_endpoint = (
            lambda: None
        )
        window.render_endpoint_provider = lambda: [
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        ]
        confirmations: list[tuple[str, str]] = []
        window._confirm_mutation = lambda title, detail: (
            confirmations.append((title, detail)) or True
        )
        window.footer = FakeWidget()
        window.refresh_static = lambda: None

        def immediate(_label, action, success):
            success(action())

        window.run_task = immediate
        with (
            patch(
                "desktop_launcher.legacy_microphone_config_requires_migration",
                return_value=False,
            ),
            patch(
                "desktop_launcher.save_enabled_config",
                return_value={},
            ) as save,
        ):
            window.on_enable_config()
        self.assertIn("/4 兼容配置", confirmations[0][0])
        self.assertIn("自动路由不会启用", confirmations[0][1])
        self.assertIsNone(
            save.call_args.kwargs[
                "virtual_microphone_capture_endpoint_id"
            ]
        )
        self.assertIsNone(
            save.call_args.kwargs["active_capture_endpoints"]
        )

    def test_start_prefers_owned_task_after_atomic_opt_in(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.root = object()
        window.paths = object()
        window.control_paths = object()
        window.control_runner = object()
        window.process_runner = object()
        window.selected_virtual_endpoints = lambda: (
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        )
        window.selected_virtual_microphone_capture_endpoint = (
            lambda: None
        )
        window.render_endpoint_provider = lambda: [
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        ]
        window._confirm_mutation = lambda *_: True
        window.footer = FakeWidget()
        window.refresh_static = lambda: None
        order: list[str] = []

        def immediate(_label, action, success):
            success(action())

        window.run_task = immediate
        with (
            patch(
                "desktop_launcher.legacy_microphone_config_requires_migration",
                return_value=False,
            ),
            patch(
                "desktop_launcher.inspect_bootstrap_task",
                return_value=TaskInspection(True, True, "S-1-5-21-1"),
            ),
            patch(
                "desktop_launcher.save_enabled_config",
                side_effect=lambda *_args, **_kwargs:
                    order.append("opt-in") or {},
            ),
            patch(
                "desktop_launcher.run_bootstrap_task_if_owned",
                side_effect=lambda *_: order.append("run-task") or True,
            ),
            patch(
                "desktop_launcher.start_direct_service",
            ) as direct,
        ):
            window.on_start()
        self.assertEqual(order, ["opt-in", "run-task"])
        direct.assert_not_called()
        self.assertIn(
            "supervisor",
            str(window.footer.values[-1]["text"]),
        )

    def test_start_without_task_is_explicitly_unsupervised(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.root = object()
        window.paths = object()
        window.control_paths = object()
        window.control_runner = object()
        window.process_runner = object()
        window.selected_virtual_endpoints = lambda: (
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        )
        window.selected_virtual_microphone_capture_endpoint = (
            lambda: None
        )
        window.render_endpoint_provider = lambda: [
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        ]
        window._confirm_mutation = lambda *_: True
        window.footer = FakeWidget()
        window.refresh_static = lambda: None
        order: list[str] = []

        def immediate(_label, action, success):
            success(action())

        window.run_task = immediate
        with (
            patch(
                "desktop_launcher.legacy_microphone_config_requires_migration",
                return_value=False,
            ),
            patch(
                "desktop_launcher.inspect_bootstrap_task",
                return_value=TaskInspection(False, False, "S-1-5-21-1"),
            ),
            patch(
                "desktop_launcher.save_enabled_config",
                side_effect=lambda *_args, **_kwargs:
                    order.append("opt-in") or {},
            ),
            patch(
                "desktop_launcher.run_bootstrap_task_if_owned",
                side_effect=lambda *_: order.append("task-missing") or False,
            ),
            patch(
                "desktop_launcher.start_direct_service",
                side_effect=lambda *_: order.append("direct") or 4242,
            ),
        ):
            window.on_start()
        self.assertEqual(
            order,
            ["opt-in", "task-missing", "direct"],
        )
        self.assertIn(
            "未受后台 supervisor",
            str(window.footer.values[-1]["text"]),
        )

    def test_start_refuses_unknown_task_before_opt_in_or_run(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window.root = object()
        window.paths = object()
        window.control_paths = object()
        window.control_runner = object()
        window.process_runner = object()
        window.selected_virtual_endpoints = lambda: (
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        )
        window.selected_virtual_microphone_capture_endpoint = (
            lambda: None
        )
        window.render_endpoint_provider = lambda: [
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
        ]
        window._confirm_mutation = lambda *_: True
        errors: list[Exception] = []

        def immediate(_label, action, _success):
            try:
                action()
            except Exception as error:
                errors.append(error)

        window.run_task = immediate
        with (
            patch(
                "desktop_launcher.legacy_microphone_config_requires_migration",
                return_value=False,
            ),
            patch(
                "desktop_launcher.inspect_bootstrap_task",
                return_value=TaskInspection(True, False, "S-1-5-21-1"),
            ),
            patch(
                "desktop_launcher.save_enabled_config",
            ) as save,
            patch(
                "desktop_launcher.run_bootstrap_task_if_owned",
            ) as run,
            patch(
                "desktop_launcher.start_direct_service",
            ) as direct,
        ):
            window.on_start()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], BridgeError)
        save.assert_not_called()
        run.assert_not_called()
        direct.assert_not_called()

    def test_confirmed_control_button_is_only_mutation_entry(self) -> None:
        window = BridgeWindow.__new__(BridgeWindow)
        window._confirm_mutation = lambda *_: True
        window.control_paths = object()
        window.control_runner = object()
        window.tailscale_install_status = FakeWidget()
        window.footer = FakeWidget()

        def immediate(_label, action, success):
            success(action())

        window.run_task = immediate
        with patch(
            "desktop_launcher.apply_tailscale_serve",
            return_value=True,
        ) as apply:
            window.on_apply_tailscale()
        apply.assert_called_once_with(
            window.control_paths,
            window.control_runner,
        )

    def test_bootstrap_cli_is_exact_and_bypasses_gui(self) -> None:
        sentinel_paths = object()
        with (
            patch.object(
                sys,
                "argv",
                ["desktop_launcher.py", "--bootstrap"],
            ),
            patch(
                "desktop_launcher.BridgePaths.discover",
                return_value=sentinel_paths,
            ),
            patch(
                "desktop_launcher.WindowsProcessRunner",
                return_value=object(),
            ),
            patch(
                "desktop_launcher.run_idle_bootstrap",
                return_value=7,
            ) as bootstrap,
            patch(
                "desktop_launcher.WindowsShortcutBroker",
            ) as broker,
            patch(
                "desktop_launcher.WindowsKeepAwakeLease",
            ) as keep_awake,
        ):
            self.assertEqual(main(), 7)
        keep_awake.assert_called_once_with()
        keep_awake.return_value.__enter__.assert_called_once_with()
        keep_awake.return_value.__exit__.assert_called_once()
        broker.assert_called_once_with()
        broker.return_value.__enter__.assert_called_once_with()
        broker.return_value.__exit__.assert_called_once()
        bootstrap.assert_called_once()

    def test_self_test_is_headless_safe_for_noconsole_package(self) -> None:
        with (
            patch.object(
                sys,
                "argv",
                ["desktop_launcher.py", "--self-test"],
            ),
            patch.object(sys, "stdout", None),
            patch(
                "desktop_launcher.BridgePaths.discover",
                return_value=object(),
            ),
            patch(
                "desktop_launcher.build_self_test_report",
                return_value={"ok": True},
            ),
        ):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
