from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from bridge_core import (  # noqa: E402
    BridgeError,
    BridgePaths,
    CaptureEndpoint,
    CONTEXT_DELIVERY_SNAPSHOT,
    DIRECT_CONFIG_CONTRACT,
    DEFAULT_ALLOWED_ORIGINS,
    FIXED_ALLOWED_TAILSCALE_USER_LOGIN,
    DIRECT_STATUS_CONTRACT,
    DIRECT_SHUTDOWN_CONTRACT,
    DIRECT_SHUTDOWN_RECEIPT_CONTRACT,
    FIXED_APP_KIND,
    FIXED_LISTEN_HOST,
    FIXED_LISTEN_PORT,
    FIXED_OUTPUT_SCOPE,
    LOCAL_PACKAGED_APP_IDS,
    NATIVE_APP_ORIGIN,
    LocalOptOutDuringStart,
    RenderEndpoint,
    SERVICE_RECORD_CONTRACT,
    SHORTCUT_BROKER_CONTRACT,
    ShortcutBrokerRequestProcessor,
    WindowsProcessRunner,
    build_direct_config,
    build_local_app_launch_command,
    build_self_test_report,
    build_start_command,
    build_tailscale_command_plan,
    clear_direct_maintenance_hold,
    DIRECT_MAINTENANCE_CONTRACT,
    DIRECT_MAINTENANCE_MAX_SECONDS,
    disable_and_stop_direct_service,
    disable_config,
    enumerate_active_capture_endpoints,
    enumerate_active_render_endpoints,
    legacy_microphone_config_requires_migration,
    load_direct_config,
    migrate_native_app_origin,
    read_direct_maintenance_hold,
    read_direct_status,
    restore_direct_config,
    run_idle_bootstrap,
    run_tailscale_read_only_preflight,
    save_enabled_config,
    set_direct_config_enabled,
    start_direct_service,
    stop_direct_service,
    validate_direct_config,
    write_direct_maintenance_hold,
)


NOW = datetime(2026, 7, 29, 4, 30, tzinfo=timezone.utc)
VIRTUAL_MICROPHONE = RenderEndpoint(
    "{0.0.0.00000000}.{11111111-1111-1111-1111-111111111111}",
    "Virtual microphone A",
)
VIRTUAL_SPEAKER = RenderEndpoint(
    "{0.0.0.00000000}.{33333333-3333-3333-3333-333333333333}",
    "Virtual speaker B",
)
VIRTUAL_MICROPHONE_CAPTURE = (
    "{0.0.1.00000000}.{22222222-2222-2222-2222-222222222222}"
)
VIRTUAL_MICROPHONE_CAPTURE_DEVICE = CaptureEndpoint(
    VIRTUAL_MICROPHONE_CAPTURE,
    "Virtual microphone A recording side",
)
VIRTUAL_SPEAKER_CAPTURE = (
    "{0.0.1.00000000}.{44444444-4444-4444-4444-444444444444}"
)


class FakeProcessRunner:
    def __init__(self) -> None:
        self.starts: list[tuple[tuple[str, ...], Path]] = []
        self.executables: dict[int, Path] = {}
        self.terminations: list[tuple[int, Path]] = []
        self.next_pid = 4242

    def start(self, command, *, cwd):
        self.starts.append((tuple(command), cwd))
        self.executables[self.next_pid] = Path(command[0])
        return self.next_pid

    def executable_for_pid(self, pid):
        return self.executables.get(pid)

    def terminate_exact(self, pid, executable):
        self.terminations.append((pid, executable))
        return self.executables.get(pid) == executable


class FakeReadOnlyRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run_read_only(self, command, *, timeout_seconds):
        self.calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "{}", "")


class DirectMaintenanceHoldTests(unittest.TestCase):
    """装桥期间让 ReaderPC 的保活别重拉 Direct（2026-09-04 根治那场赛跑）。

    赛跑的形态：安装器先停 Direct 再原子替换 exe，而保活每 5 秒发现"属下服务不在"
    就立刻重拉 —— 替换时 exe 已被新进程占住，`WinError 5`，整个安装事务回滚。
    标记消掉了窗口；这个类钉的是**它不能反过来把语音永久按住**。
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = BridgePaths.for_root(Path(self.temporary.name))
        self.paths.runtime_status.parent.mkdir(parents=True, exist_ok=True)

    def test_live_hold_is_honoured_and_says_why(self):
        write_direct_maintenance_hold(self.paths, "安装 Direct 9.9.9")
        self.assertEqual(
            read_direct_maintenance_hold(self.paths),
            "安装 Direct 9.9.9",
        )
        payload = json.loads(self.paths.maintenance_hold.read_text("utf-8"))
        self.assertEqual(payload["contract"], DIRECT_MAINTENANCE_CONTRACT)
        self.assertEqual(payload["pid"], os.getpid())
        clear_direct_maintenance_hold(self.paths)
        self.assertIsNone(read_direct_maintenance_hold(self.paths))

    def test_clearing_a_missing_hold_is_not_an_error(self):
        # 安装器在 finally 里撤标记；这里抛异常会盖掉真正的安装错误。
        clear_direct_maintenance_hold(self.paths)
        clear_direct_maintenance_hold(self.paths)

    def test_expired_hold_stops_holding(self):
        # 安装器被杀而标记留在原地 = 语音永久起不来，而现场看起来是
        # "服务都开着、就是没声音"。所以过期必须自动作废。
        write_direct_maintenance_hold(
            self.paths, "安装中", ttl_seconds=30.0, clock=lambda: 1000.0
        )
        self.assertEqual(
            read_direct_maintenance_hold(self.paths, clock=lambda: 1029.0),
            "安装中",
        )
        self.assertIsNone(
            read_direct_maintenance_hold(self.paths, clock=lambda: 1031.0)
        )

    def test_hold_from_a_dead_process_stops_holding(self):
        write_direct_maintenance_hold(self.paths, "安装中", pid=4242)

        class DeadRunner:
            def executable_for_pid(self, pid):
                return None

        with patch.object(os, "name", "nt"):
            self.assertIsNone(
                read_direct_maintenance_hold(self.paths, runner=DeadRunner())
            )

        class LiveRunner:
            def executable_for_pid(self, pid):
                return Path("C:/Windows/py.exe")

        with patch.object(os, "name", "nt"):
            self.assertEqual(
                read_direct_maintenance_hold(self.paths, runner=LiveRunner()),
                "安装中",
            )

    def test_unreadable_or_foreign_payload_never_holds(self):
        for payload in (
            "not json",
            json.dumps({"contract": "other/1", "reason": "x", "pid": 1,
                        "expiresAtEpoch": 9e18}),
            json.dumps({"contract": DIRECT_MAINTENANCE_CONTRACT, "reason": "",
                        "pid": 1, "expiresAtEpoch": 9e18}),
            json.dumps({"contract": DIRECT_MAINTENANCE_CONTRACT, "reason": "x",
                        "pid": True, "expiresAtEpoch": 9e18}),
            json.dumps({"contract": DIRECT_MAINTENANCE_CONTRACT, "reason": "x",
                        "pid": 1}),
        ):
            self.paths.maintenance_hold.write_text(payload, encoding="utf-8")
            self.assertIsNone(read_direct_maintenance_hold(self.paths), payload)

    def test_ttl_is_bounded(self):
        with self.assertRaises(BridgeError):
            write_direct_maintenance_hold(
                self.paths, "太久", ttl_seconds=DIRECT_MAINTENANCE_MAX_SECONDS + 1
            )
        with self.assertRaises(BridgeError):
            write_direct_maintenance_hold(self.paths, "", ttl_seconds=10.0)


class DirectDesktopCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = BridgePaths.for_root(self.root)
        self.paths.native_host.parent.mkdir(parents=True)
        self.paths.native_host.write_bytes(b"test executable placeholder")
        self.paths.desktop_launcher.parent.mkdir(parents=True)
        self.paths.desktop_launcher.write_bytes(
            b"test noconsole launcher placeholder"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enable_config(self) -> dict:
        return save_enabled_config(
            self.paths,
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
            active_render_endpoints=[
                VIRTUAL_MICROPHONE,
                VIRTUAL_SPEAKER,
            ],
        )

    def test_existing_valid_config_can_be_toggled_without_changing_endpoints(self) -> None:
        original = self.enable_config()
        self.assertTrue(set_direct_config_enabled(self.paths, False))
        disabled = load_direct_config(self.paths)
        self.assertIsNotNone(disabled)
        self.assertFalse(disabled["localOptIn"])
        self.assertEqual(
            disabled["virtualMicrophoneRenderEndpointId"],
            original["virtualMicrophoneRenderEndpointId"],
        )
        self.assertTrue(set_direct_config_enabled(self.paths, True))
        self.assertTrue(load_direct_config(self.paths)["localOptIn"])
        self.assertFalse(set_direct_config_enabled(self.paths, True))

    def test_readerpc_enable_commits_opt_in_and_snapshot_mode_together(self) -> None:
        original = self.enable_config()
        self.assertEqual(original["contextDeliveryMode"], "legacy-inject")
        self.assertTrue(
            set_direct_config_enabled(
                self.paths,
                True,
                context_delivery_mode=CONTEXT_DELIVERY_SNAPSHOT,
            )
        )
        enabled = load_direct_config(self.paths)
        self.assertTrue(enabled["localOptIn"])
        self.assertEqual(
            enabled["contextDeliveryMode"],
            CONTEXT_DELIVERY_SNAPSHOT,
        )
        self.assertEqual(
            enabled["virtualMicrophoneRenderEndpointId"],
            original["virtualMicrophoneRenderEndpointId"],
        )

    def test_readerpc_enable_rejects_invalid_mode_without_rewriting_config(self) -> None:
        original = self.enable_config()
        raw_before = self.paths.direct_config.read_bytes()
        with self.assertRaisesRegex(BridgeError, "上下文交付模式无效"):
            set_direct_config_enabled(
                self.paths,
                True,
                context_delivery_mode="nearby-mode",
            )
        self.assertEqual(self.paths.direct_config.read_bytes(), raw_before)
        self.assertEqual(load_direct_config(self.paths), original)

    def test_failed_readerpc_start_can_restore_exact_previous_config(self) -> None:
        original = self.enable_config()
        set_direct_config_enabled(
            self.paths,
            True,
            context_delivery_mode=CONTEXT_DELIVERY_SNAPSHOT,
        )
        self.assertTrue(restore_direct_config(self.paths, original))
        self.assertEqual(load_direct_config(self.paths), original)
        self.assertFalse(restore_direct_config(self.paths, original))

    def test_shortcut_broker_is_strict_and_idempotent(self) -> None:
        sends: list[str] = []
        processor = ShortcutBrokerRequestProcessor(
            lambda: sends.append("F24")
        )
        request = {
            "contract": SHORTCUT_BROKER_CONTRACT,
            "type": "toggle",
            "requestId": "shortcut-AAAAAAAAAAAAAAAAAAAAAA",
            "rootProcessId": 4242,
            "rootProcessStartTimeUtc": "2026-08-01T08:00:00.0000000Z",
            # Keep this golden request aligned with the C# serializer.  The
            # interactive broker validates the exact window generation before
            # it is allowed to emit F24.
            "windowHandle": 0x1234,
        }
        payload = (
            json.dumps(request, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        first = json.loads(processor.process(payload))
        duplicate = json.loads(processor.process(payload))
        self.assertEqual(first, duplicate)
        self.assertEqual(
            first,
            {
                "contract": SHORTCUT_BROKER_CONTRACT,
                "type": "receipt",
                "requestId": request["requestId"],
                "ok": True,
            },
        )
        self.assertEqual(sends, ["F24"])

        invalid = {**request, "shortcut": "F24"}
        rejected = json.loads(
            processor.process(
                json.dumps(invalid).encode("utf-8") + b"\n"
            )
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(sends, ["F24"])

        wrong_window = {**request, "windowHandle": 0}
        rejected = json.loads(
            processor.process(
                json.dumps(wrong_window).encode("utf-8") + b"\n"
            )
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(sends, ["F24"])

    def test_shortcut_broker_caches_failure_without_retry(self) -> None:
        attempts = 0

        def fail() -> None:
            nonlocal attempts
            attempts += 1
            raise OSError("injected")

        processor = ShortcutBrokerRequestProcessor(fail)
        request = {
            "contract": SHORTCUT_BROKER_CONTRACT,
            "type": "toggle",
            "requestId": "shortcut-AQEBAQEBAQEBAQEBAQEBAQ",
            "rootProcessId": 7,
            "rootProcessStartTimeUtc": "2026-08-01T08:00:00Z",
            "windowHandle": 0x2345,
        }
        payload = json.dumps(request).encode("utf-8") + b"\n"
        first = processor.process(payload)
        second = processor.process(payload)
        self.assertEqual(first, second)
        self.assertEqual(attempts, 1)

    def write_runtime(
        self,
        *,
        pid: int,
        state: str = "idle",
        reader_connected: bool = False,
        updated: datetime = NOW,
        last_error: dict | None = None,
    ) -> None:
        self.paths.runtime_status.parent.mkdir(parents=True, exist_ok=True)
        self.paths.runtime_status.write_text(
            json.dumps(
                {
                    "contract": DIRECT_STATUS_CONTRACT,
                    "serviceInstanceId": "a" * 32,
                    "pid": pid,
                    "state": state,
                    "readerConnected": reader_connected,
                    "captureActive": state == "active",
                    "lastError": last_error,
                    "updatedAtUtc": (
                        updated.isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
                    ),
                }
            ),
            encoding="utf-8",
        )

    def test_direct_config_is_exact_and_has_no_long_term_token(self) -> None:
        value = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                VIRTUAL_MICROPHONE_CAPTURE
            ),
        )
        self.assertEqual(value["contract"], DIRECT_CONFIG_CONTRACT)
        self.assertEqual(
            value["contract"],
            "reader-computer-voice-direct-config/5",
        )
        self.assertEqual(
            value["virtualMicrophoneCaptureEndpointId"],
            VIRTUAL_MICROPHONE_CAPTURE,
        )
        self.assertEqual(value["contextDeliveryMode"], "legacy-inject")
        self.assertEqual(value["listenHost"], FIXED_LISTEN_HOST)
        self.assertEqual(value["listenPort"], FIXED_LISTEN_PORT)
        self.assertEqual(
            value["allowedOrigins"],
            list(DEFAULT_ALLOWED_ORIGINS),
        )
        self.assertIs(value["experimentalSingleUserMode"], True)
        self.assertEqual(
            value["allowedTailscaleUserLogin"],
            FIXED_ALLOWED_TAILSCALE_USER_LOGIN,
        )
        self.assertEqual(value["outputScope"], FIXED_OUTPUT_SCOPE)
        self.assertEqual(value["appKind"], FIXED_APP_KIND)
        flattened_keys = " ".join(value).casefold()
        self.assertNotIn("token", flattened_keys)
        self.assertNotIn("pair", flattened_keys)
        with self.assertRaises(BridgeError):
            validate_direct_config({**value, "deviceToken": "secret"})
        with self.assertRaises(BridgeError):
            validate_direct_config(
                {
                    **value,
                    "allowedTailscaleUserLogin": "other@example.test",
                }
            )
        with self.assertRaises(BridgeError):
            build_direct_config(
                VIRTUAL_MICROPHONE.endpoint_id,
                VIRTUAL_SPEAKER.endpoint_id,
                self.paths.runtime_status,
                allowed_origins=["https://user@example.test"],
            )
        for origin in (
            "http://127.0.0.1:43130",
            "http://localhost:43129",
            "http://127.0.0.1:43129/",
            "HTTP://127.0.0.1:43129",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(BridgeError):
                    build_direct_config(
                        VIRTUAL_MICROPHONE.endpoint_id,
                        VIRTUAL_SPEAKER.endpoint_id,
                        self.paths.runtime_status,
                        allowed_origins=[origin],
                    )

    def test_existing_config_atomically_gains_native_app_origin(self) -> None:
        old = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
            allowed_origins=["https://bwicarus.taile44d0c.ts.net"],
        )
        self.assertNotIn(NATIVE_APP_ORIGIN, old["allowedOrigins"])
        self.paths.direct_config.write_text(
            json.dumps(old),
            encoding="utf-8",
        )
        with patch("bridge_core.os.replace", wraps=os.replace) as replace:
            migrated = migrate_native_app_origin(self.paths)
        self.assertIs(migrated, True)
        self.assertEqual(
            load_direct_config(self.paths)["allowedOrigins"],
            list(DEFAULT_ALLOWED_ORIGINS),
        )
        replace.assert_called_once()
        self.assertEqual(
            list(self.paths.direct_config.parent.glob("*.tmp-*")),
            [],
        )
        with patch("bridge_core.os.replace", wraps=os.replace) as replace:
            migrated_again = migrate_native_app_origin(self.paths)
        self.assertIs(migrated_again, False)
        replace.assert_not_called()

    def test_fixed_audio_bus_config_has_explicit_b_capture(self) -> None:
        value = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                VIRTUAL_MICROPHONE_CAPTURE
            ),
            virtual_speaker_capture_endpoint_id=(
                VIRTUAL_SPEAKER_CAPTURE
            ),
        )
        self.assertEqual(
            value["contract"],
            "reader-computer-voice-direct-config/6",
        )
        self.assertEqual(
            value["virtualSpeakerCaptureEndpointId"],
            VIRTUAL_SPEAKER_CAPTURE,
        )

    def test_v5_config_requires_explicit_single_user_true(self) -> None:
        invalid = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                VIRTUAL_MICROPHONE_CAPTURE
            ),
        )
        invalid.pop("experimentalSingleUserMode")
        with self.assertRaises(BridgeError):
            validate_direct_config(invalid)
        with self.assertRaises(BridgeError):
            build_direct_config(
                VIRTUAL_MICROPHONE.endpoint_id,
                VIRTUAL_SPEAKER.endpoint_id,
                self.paths.runtime_status,
                experimental_single_user_mode=False,
                virtual_microphone_capture_endpoint_id=(
                    VIRTUAL_MICROPHONE_CAPTURE
                ),
            )

    def test_v4_loads_without_enabling_or_inventing_capture(self) -> None:
        legacy = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
        )
        self.assertEqual(
            legacy["contract"],
            "reader-computer-voice-direct-config/4",
        )
        self.assertNotIn(
            "virtualMicrophoneCaptureEndpointId",
            legacy,
        )
        self.paths.direct_config.write_text(
            json.dumps(legacy),
            encoding="utf-8",
        )
        loaded = load_direct_config(self.paths)
        self.assertIsNotNone(loaded)
        self.assertEqual(
            loaded["contract"],
            "reader-computer-voice-direct-config/4",
        )
        self.assertNotIn(
            "virtualMicrophoneCaptureEndpointId",
            loaded,
        )

    def test_v5_rejects_cross_flow_or_missing_capture(self) -> None:
        value = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                VIRTUAL_MICROPHONE_CAPTURE
            ),
        )
        with self.assertRaisesRegex(BridgeError, "eCapture"):
            validate_direct_config(
                {
                    **value,
                    "virtualMicrophoneCaptureEndpointId":
                        VIRTUAL_MICROPHONE.endpoint_id,
                }
            )
        with self.assertRaises(BridgeError):
            validate_direct_config(
                {
                    key: item
                    for key, item in value.items()
                    if key != "virtualMicrophoneCaptureEndpointId"
                }
            )
        with self.assertRaisesRegex(BridgeError, "eRender"):
            validate_direct_config(
                {
                    **value,
                    "virtualSpeakerRenderEndpointId":
                        VIRTUAL_MICROPHONE_CAPTURE,
                }
            )

    def test_saving_existing_v5_preserves_explicit_capture(self) -> None:
        value = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                VIRTUAL_MICROPHONE_CAPTURE
            ),
        )
        self.paths.direct_config.write_text(
            json.dumps(value),
            encoding="utf-8",
        )
        saved = save_enabled_config(
            self.paths,
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
            active_render_endpoints=[
                VIRTUAL_MICROPHONE,
                VIRTUAL_SPEAKER,
            ],
        )
        self.assertEqual(saved["contract"], DIRECT_CONFIG_CONTRACT)
        self.assertEqual(
            saved["virtualMicrophoneCaptureEndpointId"],
            VIRTUAL_MICROPHONE_CAPTURE,
        )

    def test_changing_render_a_does_not_reuse_old_capture(self) -> None:
        value = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                VIRTUAL_MICROPHONE_CAPTURE
            ),
        )
        self.paths.direct_config.write_text(
            json.dumps(value),
            encoding="utf-8",
        )
        replacement = RenderEndpoint(
            "{0.0.0.00000000}."
            "{66666666-6666-6666-6666-666666666666}",
            "Replacement virtual microphone A",
        )
        saved = save_enabled_config(
            self.paths,
            replacement,
            VIRTUAL_SPEAKER,
            active_render_endpoints=[
                replacement,
                VIRTUAL_SPEAKER,
            ],
        )
        self.assertEqual(
            saved["contract"],
            "reader-computer-voice-direct-config/4",
        )
        self.assertNotIn(
            "virtualMicrophoneCaptureEndpointId",
            saved,
        )

    def test_explicit_legacy_choice_downgrades_existing_v5(self) -> None:
        value = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
            virtual_microphone_capture_endpoint_id=(
                VIRTUAL_MICROPHONE_CAPTURE
            ),
        )
        self.paths.direct_config.write_text(
            json.dumps(value),
            encoding="utf-8",
        )
        saved = save_enabled_config(
            self.paths,
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
            active_render_endpoints=[
                VIRTUAL_MICROPHONE,
                VIRTUAL_SPEAKER,
            ],
            virtual_microphone_capture_endpoint_id=None,
        )
        self.assertEqual(
            saved["contract"],
            "reader-computer-voice-direct-config/4",
        )
        self.assertNotIn(
            "virtualMicrophoneCaptureEndpointId",
            saved,
        )

    def test_explicit_capture_must_still_be_active_when_checked(self) -> None:
        with self.assertRaisesRegex(BridgeError, "不再是 Active"):
            save_enabled_config(
                self.paths,
                VIRTUAL_MICROPHONE,
                VIRTUAL_SPEAKER,
                active_render_endpoints=[
                    VIRTUAL_MICROPHONE,
                    VIRTUAL_SPEAKER,
                ],
                active_capture_endpoints=[],
                virtual_microphone_capture_endpoint_id=(
                    VIRTUAL_MICROPHONE_CAPTURE
                ),
            )

    def test_microphone_endpoint_id_is_preserved_exactly(self) -> None:
        endpoint = " {0.0.1.00000000}.{exact-endpoint} "
        value = build_direct_config(
            endpoint,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
        )
        self.assertEqual(
            value["virtualMicrophoneRenderEndpointId"],
            endpoint,
        )

    def test_virtual_endpoints_must_be_distinct(self) -> None:
        with self.assertRaisesRegex(BridgeError, "必须选择不同端点"):
            build_direct_config(
                VIRTUAL_MICROPHONE.endpoint_id,
                VIRTUAL_MICROPHONE.endpoint_id,
                self.paths.runtime_status,
            )

    def test_v1_microphone_config_requires_explicit_migration(self) -> None:
        legacy = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
        )
        legacy["microphoneEndpointId"] = legacy.pop(
            "virtualMicrophoneRenderEndpointId"
        )
        legacy.pop("virtualSpeakerRenderEndpointId")
        legacy.pop("contextDeliveryMode")
        legacy["contract"] = "reader-computer-voice-direct-config/1"
        legacy.update(
            {
                "pairingCodeHash": "A" * 43,
                "pairingExpiresAtUtc": "2026-07-29T04:35:00Z",
                "pairedClientPublicKeySpki": "B" * 120,
                "pairedClientFingerprintSha256": "C" * 43,
            }
        )
        self.paths.direct_config.write_text(
            json.dumps(legacy),
            encoding="utf-8",
        )
        self.assertIsNone(load_direct_config(self.paths))
        self.assertTrue(
            legacy_microphone_config_requires_migration(self.paths)
        )
        with self.assertRaisesRegex(
            BridgeError,
            "必须在桌面窗口中明确确认迁移",
        ):
            save_enabled_config(
                self.paths,
                VIRTUAL_MICROPHONE,
                VIRTUAL_SPEAKER,
                active_render_endpoints=[
                    VIRTUAL_MICROPHONE,
                    VIRTUAL_SPEAKER,
                ],
            )
        migrated = save_enabled_config(
            self.paths,
            VIRTUAL_MICROPHONE,
            VIRTUAL_SPEAKER,
            active_render_endpoints=[
                VIRTUAL_MICROPHONE,
                VIRTUAL_SPEAKER,
            ],
            allow_legacy_migration=True,
        )
        self.assertNotIn("microphoneEndpointId", migrated)
        self.assertNotIn("pairingCodeHash", migrated)
        self.assertEqual(
            migrated["contract"],
            "reader-computer-voice-direct-config/4",
        )
        self.assertEqual(
            migrated["contextDeliveryMode"],
            "legacy-inject",
        )
        self.assertEqual(
            migrated["virtualSpeakerRenderEndpointId"],
            VIRTUAL_SPEAKER.endpoint_id,
        )

    def test_both_endpoints_must_still_be_active_when_saved(self) -> None:
        with self.assertRaisesRegex(BridgeError, "虚拟扬声器 B"):
            save_enabled_config(
                self.paths,
                VIRTUAL_MICROPHONE,
                VIRTUAL_SPEAKER,
                active_render_endpoints=[VIRTUAL_MICROPHONE],
            )

    def test_render_discovery_uses_native_core_audio_ids(self) -> None:
        payload = json.dumps(
            {
                "contract":
                    "reader-computer-voice-render-endpoints/1",
                "ok": True,
                "captureStarted": False,
                "devices": [
                    {
                        "endpointId": (
                            "{3.0.1.00000001}."
                            "{A3ED9185-1E02-411C-B11B-05D92F25CEF4}"
                        ),
                        "friendlyName": "远程音频",
                    }
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "bridge_core.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=payload,
                stderr="",
            ),
        ) as run:
            devices = enumerate_active_render_endpoints(
                self.paths.native_host
            )
        self.assertEqual(
            devices,
            [
                RenderEndpoint(
                    (
                        "{3.0.1.00000001}."
                        "{A3ED9185-1E02-411C-B11B-05D92F25CEF4}"
                    ),
                    "远程音频",
                )
            ],
        )
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            (
                str(self.paths.native_host.resolve()),
                "--list-direct-render-endpoints",
            ),
        )
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_render_discovery_rejects_non_read_only_contract(self) -> None:
        payload = json.dumps(
            {
                "contract": "unexpected",
                "ok": True,
                "captureStarted": False,
                "devices": [],
            }
        )
        with patch(
            "bridge_core.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=payload,
                stderr="",
            ),
        ):
            self.assertEqual(
                enumerate_active_render_endpoints(
                    self.paths.native_host
                ),
                [],
            )

    def test_capture_discovery_uses_native_core_audio_ids(self) -> None:
        payload = json.dumps(
            {
                "contract":
                    "reader-computer-voice-microphones/1",
                "ok": True,
                "captureStarted": False,
                "devices": [
                    {
                        "endpointId": VIRTUAL_MICROPHONE_CAPTURE,
                        "friendlyName": "Virtual Cable A",
                    }
                ],
            },
            ensure_ascii=False,
        )
        with patch(
            "bridge_core.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=payload,
                stderr="",
            ),
        ) as run:
            devices = enumerate_active_capture_endpoints(
                self.paths.native_host
            )
        self.assertEqual(
            devices,
            [
                CaptureEndpoint(
                    VIRTUAL_MICROPHONE_CAPTURE,
                    "Virtual Cable A",
                )
            ],
        )
        self.assertEqual(
            run.call_args.args[0],
            (
                str(self.paths.native_host.resolve()),
                "--list-direct-microphones",
            ),
        )
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_capture_discovery_rejects_render_flow_id(self) -> None:
        payload = json.dumps(
            {
                "contract":
                    "reader-computer-voice-microphones/1",
                "ok": True,
                "captureStarted": False,
                "devices": [
                    {
                        "endpointId": VIRTUAL_MICROPHONE.endpoint_id,
                        "friendlyName": "Wrong flow",
                    }
                ],
            }
        )
        with patch(
            "bridge_core.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=payload,
                stderr="",
            ),
        ):
            self.assertEqual(
                enumerate_active_capture_endpoints(
                    self.paths.native_host
                ),
                [],
            )

    def test_invalid_existing_config_is_not_silently_overwritten(self) -> None:
        self.paths.direct_config.write_text(
            '{"contract":"unknown","pairedClientPublicKeySpki":"opaque"}',
            encoding="utf-8",
        )
        with self.assertRaises(BridgeError):
            save_enabled_config(
                self.paths,
                VIRTUAL_MICROPHONE,
                VIRTUAL_SPEAKER,
                active_render_endpoints=[
                    VIRTUAL_MICROPHONE,
                    VIRTUAL_SPEAKER,
                ],
            )
        self.assertIn(
            '"contract":"unknown"',
            self.paths.direct_config.read_text(encoding="utf-8"),
        )

    def test_v3_config_rejects_all_pairing_fields(self) -> None:
        value = self.enable_config()
        for key in (
            "pairingCodeHash",
            "pairingExpiresAtUtc",
            "pairedClientPublicKeySpki",
            "pairedClientFingerprintSha256",
        ):
            with self.subTest(key=key):
                with self.assertRaises(BridgeError):
                    validate_direct_config({**value, key: ""})

    def test_offline_or_stale_never_pretends_to_be_online(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)

        missing = read_direct_status(self.paths, runner, now=NOW)
        self.assertTrue(missing.configuration_enabled)
        self.assertFalse(missing.service_online)
        self.assertFalse(missing.reader_connected)

        self.write_runtime(
            pid=pid,
            reader_connected=True,
            updated=NOW - timedelta(minutes=1),
        )
        stale = read_direct_status(self.paths, runner, now=NOW)
        self.assertFalse(stale.service_online)
        self.assertFalse(stale.reader_connected)

        runner.executables[pid] = self.root / "wrong.exe"
        wrong_process = read_direct_status(self.paths, runner, now=NOW)
        self.assertFalse(wrong_process.service_online)

        runner.executables[pid] = self.paths.native_host
        self.write_runtime(
            pid=pid,
            state="reader-connected",
            reader_connected=True,
        )
        connected = read_direct_status(self.paths, runner, now=NOW)
        self.assertTrue(connected.service_online)
        self.assertTrue(connected.reader_connected)

    def test_runtime_status_v2_preserves_only_valid_last_error(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        failure = {
            "failureId": "failure-AAAAAAAAAAAAAAAA",
            "code": "BW_COMPUTER_VOICE_AUDIO_FAILURE",
            "stage": "virtual-microphone.render",
            "hresult": "0x80070490",
            "atUtc": "2026-07-29T04:29:00Z",
        }
        self.write_runtime(pid=pid, last_error=failure)
        status = read_direct_status(self.paths, runner, now=NOW)
        self.assertTrue(status.service_online)
        self.assertEqual(status.last_error, failure)
        runner.executables.pop(pid)
        offline = read_direct_status(self.paths, runner, now=NOW)
        self.assertFalse(offline.service_online)
        self.assertEqual(offline.last_error, failure)
        runner.executables[pid] = self.paths.native_host

        self.write_runtime(
            pid=pid,
            last_error={**failure, "hresult": "0xnot-safe"},
        )
        invalid = read_direct_status(self.paths, runner, now=NOW)
        self.assertFalse(invalid.service_online)
        self.assertIsNone(invalid.last_error)

    def test_waiting_voice_ready_remains_online_without_capture(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(
            pid=pid,
            state="waiting-voice-ready",
            reader_connected=True,
        )

        status = read_direct_status(self.paths, runner, now=NOW)

        self.assertTrue(status.service_online)
        self.assertTrue(status.reader_connected)
        self.assertFalse(status.capture_active)

    def test_three_states_are_independent(self) -> None:
        value = self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(pid=pid, state="idle", updated=NOW)
        value["localOptIn"] = False
        self.paths.direct_config.write_text(
            json.dumps(value),
            encoding="utf-8",
        )
        status = read_direct_status(self.paths, runner, now=NOW)
        self.assertFalse(status.configuration_enabled)
        self.assertTrue(status.service_online)
        self.assertFalse(status.reader_connected)

    def test_start_and_stop_use_injected_runner_and_exact_paths(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.assertEqual(pid, runner.next_pid)
        self.assertEqual(
            runner.starts,
            [
                (
                    build_start_command(self.paths),
                    self.paths.native_host.parent.resolve(),
                )
            ],
        )
        self.assertTrue(stop_direct_service(self.paths, runner))
        self.assertEqual(
            runner.terminations,
            [(pid, self.paths.native_host.resolve())],
        )

    def test_readerpc_graceful_stop_requires_success_receipt_and_pid_exit(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(pid=pid, state="active", updated=NOW)
        requests: list[dict] = []
        clock = [0.0]
        step = [0]

        def graceful_exit(delay: float) -> None:
            clock[0] += delay
            step[0] += 1
            if step[0] == 1:
                requests.append(json.loads(
                    self.paths.shutdown_request.read_text("utf-8")
                ))
                self.paths.shutdown_receipt.write_text(json.dumps({
                    "contract": DIRECT_SHUTDOWN_RECEIPT_CONTRACT,
                    "serviceInstanceId": "a" * 32,
                    "state": "accepted",
                    "maximumWaitMs": 40000,
                }), encoding="utf-8")
            elif step[0] == 2:
                self.paths.shutdown_receipt.write_text(json.dumps({
                    "contract": DIRECT_SHUTDOWN_RECEIPT_CONTRACT,
                    "serviceInstanceId": "a" * 32,
                    "state": "success",
                }), encoding="utf-8")
            else:
                runner.executables.pop(pid, None)

        with patch("bridge_core.utc_now", return_value=NOW):
            self.assertTrue(stop_direct_service(
                self.paths,
                runner,
                graceful=True,
                graceful_poll_seconds=0.1,
                sleeper=graceful_exit,
                monotonic=lambda: clock[0],
            ))
        self.assertEqual(requests, [{
            "contract": DIRECT_SHUTDOWN_CONTRACT,
            "serviceInstanceId": "a" * 32,
        }])
        self.assertEqual(runner.terminations, [])
        self.assertFalse(self.paths.service_record.exists())
        self.assertFalse(self.paths.shutdown_request.exists())
        self.assertEqual(
            json.loads(self.paths.shutdown_receipt.read_text("utf-8")),
            {
                "contract": DIRECT_SHUTDOWN_RECEIPT_CONTRACT,
                "serviceInstanceId": "a" * 32,
                "state": "success",
            },
        )

    def test_readerpc_graceful_stop_failed_receipt_hard_kills_and_raises(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(pid=pid, state="active", updated=NOW)
        clock = [0.0]

        def fail_cleanup(delay: float) -> None:
            clock[0] += delay
            self.paths.shutdown_receipt.write_text(json.dumps({
                "contract": DIRECT_SHUTDOWN_RECEIPT_CONTRACT,
                "serviceInstanceId": "a" * 32,
                "state": "failed",
                "code": "BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING",
            }), encoding="utf-8")

        with patch("bridge_core.utc_now", return_value=NOW):
            with self.assertRaisesRegex(
                BridgeError,
                "MEDIA_CLEANUP_PENDING",
            ):
                stop_direct_service(
                    self.paths,
                    runner,
                    graceful=True,
                    graceful_poll_seconds=0.1,
                    sleeper=fail_cleanup,
                    monotonic=lambda: clock[0],
                )
        self.assertEqual(
            runner.terminations,
            [(pid, self.paths.native_host.resolve())],
        )
        self.assertFalse(self.paths.shutdown_request.exists())

    def test_graceful_failed_receipt_without_force_preserves_identity(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(pid=pid, state="active", updated=NOW)
        clock = [0.0]

        def fail_cleanup(delay: float) -> None:
            clock[0] += delay
            self.paths.shutdown_receipt.write_text(json.dumps({
                "contract": DIRECT_SHUTDOWN_RECEIPT_CONTRACT,
                "serviceInstanceId": "a" * 32,
                "state": "failed",
                "code": "BW_COMPUTER_VOICE_DIRECT_CLEANUP_FAILED",
            }), encoding="utf-8")

        with self.assertRaisesRegex(BridgeError, "CLEANUP_FAILED"):
            stop_direct_service(
                self.paths,
                runner,
                graceful=True,
                force_on_cleanup_failure=False,
                graceful_poll_seconds=0.1,
                sleeper=fail_cleanup,
                monotonic=lambda: clock[0],
            )
        self.assertEqual(runner.terminations, [])
        self.assertIn(pid, runner.executables)
        self.assertTrue(self.paths.service_record.exists())
        self.assertTrue(self.paths.shutdown_request.exists())
        self.assertEqual(
            json.loads(self.paths.shutdown_receipt.read_text("utf-8"))["state"],
            "failed",
        )

    def test_graceful_accept_timeout_without_force_preserves_identity(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(pid=pid, state="active", updated=NOW)
        clock = [0.0]

        def advance(delay: float) -> None:
            clock[0] += delay

        with self.assertRaisesRegex(BridgeError, "没有接受"):
            stop_direct_service(
                self.paths,
                runner,
                graceful=True,
                force_on_cleanup_failure=False,
                graceful_accept_timeout_seconds=0.2,
                graceful_poll_seconds=0.1,
                sleeper=advance,
                monotonic=lambda: clock[0],
            )
        self.assertEqual(runner.terminations, [])
        self.assertIn(pid, runner.executables)
        self.assertTrue(self.paths.service_record.exists())
        self.assertTrue(self.paths.shutdown_request.exists())

    def test_graceful_cleanup_timeout_without_force_preserves_identity(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(pid=pid, state="active", updated=NOW)
        clock = [0.0]

        def accept_but_never_finish(delay: float) -> None:
            clock[0] += delay
            self.paths.shutdown_receipt.write_text(json.dumps({
                "contract": DIRECT_SHUTDOWN_RECEIPT_CONTRACT,
                "serviceInstanceId": "a" * 32,
                "state": "accepted",
                "maximumWaitMs": 1000,
            }), encoding="utf-8")

        with self.assertRaisesRegex(BridgeError, "清理期限"):
            stop_direct_service(
                self.paths,
                runner,
                graceful=True,
                force_on_cleanup_failure=False,
                graceful_accept_timeout_seconds=1.0,
                graceful_poll_seconds=0.6,
                sleeper=accept_but_never_finish,
                monotonic=lambda: clock[0],
            )
        self.assertEqual(runner.terminations, [])
        self.assertIn(pid, runner.executables)
        self.assertTrue(self.paths.service_record.exists())
        self.assertTrue(self.paths.shutdown_request.exists())
        self.assertTrue(self.paths.shutdown_receipt.exists())

    def test_readerpc_graceful_stop_accepts_stale_but_exact_runtime_identity(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(
            pid=pid,
            state="active",
            updated=NOW - timedelta(minutes=5),
        )
        clock = [0.0]
        step = [0]

        def finish(delay: float) -> None:
            clock[0] += delay
            step[0] += 1
            state = "accepted" if step[0] == 1 else "success"
            value = {
                "contract": DIRECT_SHUTDOWN_RECEIPT_CONTRACT,
                "serviceInstanceId": "a" * 32,
                "state": state,
            }
            if state == "accepted":
                value["maximumWaitMs"] = 40000
            self.paths.shutdown_receipt.write_text(
                json.dumps(value),
                encoding="utf-8",
            )
            if step[0] >= 3:
                runner.executables.pop(pid, None)

        self.assertTrue(stop_direct_service(
            self.paths,
            runner,
            graceful=True,
            graceful_poll_seconds=0.1,
            sleeper=finish,
            monotonic=lambda: clock[0],
        ))
        self.assertEqual(runner.terminations, [])

    def test_readerpc_graceful_stop_refuses_unidentified_live_generation(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        with self.assertRaisesRegex(BridgeError, "无法认证当前服务代际"):
            stop_direct_service(self.paths, runner, graceful=True)
        self.assertEqual(runner.terminations, [])
        self.assertIn(pid, runner.executables)

    def test_readerpc_owned_start_passes_exact_positive_owner_pid(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(
            self.paths,
            runner,
            now=NOW,
            readerpc_owner_pid=4242,
        )
        self.assertEqual(pid, runner.next_pid)
        self.assertEqual(
            runner.starts[0][0],
            build_start_command(self.paths, readerpc_owner_pid=4242),
        )
        self.assertEqual(
            runner.starts[0][0][-2:],
            ("--readerpc-owner-pid", "4242"),
        )
        with self.assertRaisesRegex(BridgeError, "owner PID"):
            build_start_command(self.paths, readerpc_owner_pid=0)

    def test_default_runner_validates_full_command_before_popen(self) -> None:
        self.enable_config()
        runner = WindowsProcessRunner()
        command = build_start_command(self.paths)
        with patch("bridge_core.subprocess.Popen") as popen:
            popen.return_value.pid = 5151
            self.assertEqual(
                runner.start(
                    command,
                    cwd=self.paths.native_host.parent.resolve(),
                ),
                5151,
            )
            self.assertFalse(popen.call_args.kwargs["shell"])
        owner_command = build_start_command(
            self.paths,
            readerpc_owner_pid=4242,
        )
        with patch("bridge_core.subprocess.Popen") as popen:
            popen.return_value.pid = 5152
            self.assertEqual(
                runner.start(
                    owner_command,
                    cwd=self.paths.native_host.parent.resolve(),
                ),
                5152,
            )
        with patch("bridge_core.subprocess.Popen") as popen:
            with self.assertRaises(BridgeError):
                runner.start(
                    (command[0], "--self-test"),
                    cwd=self.paths.native_host.parent.resolve(),
                )
            popen.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows handle contract")
    def test_terminate_revalidates_path_and_terminates_on_same_handle(self) -> None:
        class Function:
            def __init__(self, callback) -> None:
                self.callback = callback
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.callback(*args)

        handle = 991
        queried: list[int] = []
        terminated: list[int] = []
        closed: list[int] = []

        def query(actual_handle, _flags, buffer, _size) -> bool:
            queried.append(actual_handle)
            buffer.value = str(self.paths.native_host.resolve())
            return True

        kernel32 = type("FakeKernel32", (), {})()
        kernel32.OpenProcess = Function(
            lambda _access, _inherit, _pid: handle
        )
        kernel32.QueryFullProcessImageNameW = Function(query)
        kernel32.TerminateProcess = Function(
            lambda actual_handle, _code:
                terminated.append(actual_handle) or True
        )
        kernel32.CloseHandle = Function(
            lambda actual_handle: closed.append(actual_handle) or True
        )

        with patch("bridge_core.ctypes.WinDLL", return_value=kernel32):
            self.assertTrue(
                WindowsProcessRunner().terminate_exact(
                    4242,
                    self.paths.native_host.resolve(),
                )
            )
        self.assertEqual(queried, [handle])
        self.assertEqual(terminated, [handle])
        self.assertEqual(closed, [handle])

    @unittest.skipUnless(os.name == "nt", "Windows handle contract")
    def test_terminate_same_handle_rejects_foreign_image(self) -> None:
        class Function:
            def __init__(self, callback) -> None:
                self.callback = callback
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.callback(*args)

        terminated: list[int] = []

        def query(_handle, _flags, buffer, _size) -> bool:
            buffer.value = str((self.root / "foreign.exe").resolve())
            return True

        kernel32 = type("FakeKernel32", (), {})()
        kernel32.OpenProcess = Function(lambda *_: 992)
        kernel32.QueryFullProcessImageNameW = Function(query)
        kernel32.TerminateProcess = Function(
            lambda actual_handle, _code:
                terminated.append(actual_handle) or True
        )
        kernel32.CloseHandle = Function(lambda *_: True)

        with patch("bridge_core.ctypes.WinDLL", return_value=kernel32):
            self.assertFalse(
                WindowsProcessRunner().terminate_exact(
                    4242,
                    self.paths.native_host.resolve(),
                )
            )
        self.assertEqual(terminated, [])

    def test_record_write_failure_stops_new_exact_process(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        with patch(
            "bridge_core._atomic_write_json",
            side_effect=OSError("record write denied"),
        ):
            with self.assertRaises(OSError):
                start_direct_service(self.paths, runner, now=NOW)
        self.assertEqual(
            runner.terminations,
            [(runner.next_pid, self.paths.native_host.resolve())],
        )

    def test_start_is_idempotent_only_after_fresh_online_proof(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        with self.assertRaises(BridgeError):
            start_direct_service(self.paths, runner, now=NOW)
        self.assertEqual(len(runner.starts), 1)

        self.write_runtime(pid=pid, state="idle", updated=NOW)
        self.assertEqual(
            start_direct_service(self.paths, runner, now=NOW),
            pid,
        )
        self.assertEqual(len(runner.starts), 1)

    def test_start_refuses_service_record_pid_owned_by_foreign_exe(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        runner.executables[pid] = self.root / "foreign.exe"

        with self.assertRaises(BridgeError):
            start_direct_service(self.paths, runner, now=NOW)
        self.assertEqual(len(runner.starts), 1)
        self.assertEqual(runner.terminations, [])

    def test_stop_refuses_pid_that_points_to_another_executable(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        runner.executables[pid] = self.root / "not-the-host.exe"
        with self.assertRaises(BridgeError):
            stop_direct_service(self.paths, runner)
        self.assertEqual(runner.terminations, [])

    def test_tailscale_preflight_can_only_run_read_only_commands(self) -> None:
        executable = self.root / "Tailscale" / "tailscale.exe"
        plan = build_tailscale_command_plan(executable)
        runner = FakeReadOnlyRunner()
        run_tailscale_read_only_preflight(executable, runner)
        self.assertEqual(runner.calls, [plan.status, plan.serve_status])
        self.assertNotIn(plan.apply_serve, runner.calls)
        self.assertNotIn(plan.rollback_serve, runner.calls)

    def test_reader_cannot_supply_an_app_command_or_identifier(self) -> None:
        explorer = self.root / "Windows" / "explorer.exe"
        command = build_local_app_launch_command(
            explorer,
            "codex-desktop",
        )
        self.assertEqual(
            command[-1],
            "shell:AppsFolder\\"
            + LOCAL_PACKAGED_APP_IDS["codex-desktop"],
        )
        with self.assertRaises(BridgeError):
            build_local_app_launch_command(
                explorer,
                r"C:\attacker\program.exe",
            )

    def test_refresh_status_never_calls_start(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        status = read_direct_status(self.paths, runner, now=NOW)
        self.assertFalse(status.service_online)
        self.assertEqual(runner.starts, [])

    def test_self_test_is_no_side_effect_and_uses_no_runner(self) -> None:
        report = build_self_test_report(self.paths)
        self.assertTrue(report["ok"])
        for key in (
            "writesToBridgeConfiguration",
            "serviceStarted",
            "audioOpened",
            "applicationStarted",
            "typistStarted",
            "shortcutSent",
            "taskRegistered",
            "registryWritten",
            "tailscaleServeChanged",
            "browserOpened",
        ):
            self.assertFalse(report[key], key)
        self.assertFalse(self.paths.direct_config.exists())

    def test_bootstrap_missing_invalid_or_disabled_config_never_starts(self) -> None:
        for label, content in (
            ("missing", None),
            ("invalid", '{"contract":"unknown"}'),
        ):
            with self.subTest(label=label):
                if content is None:
                    self.paths.direct_config.unlink(missing_ok=True)
                else:
                    self.paths.direct_config.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    self.paths.direct_config.write_text(
                        content,
                        encoding="utf-8",
                    )
                runner = FakeProcessRunner()
                self.assertEqual(
                    run_idle_bootstrap(
                        self.paths,
                        runner,
                        sleeper=lambda _: None,
                        now_provider=lambda: NOW,
                        max_cycles=1,
                    ),
                    0,
                )
                self.assertEqual(runner.starts, [])

        self.paths.direct_config.unlink(missing_ok=True)
        self.enable_config()
        disable_config(self.paths)
        runner = FakeProcessRunner()
        self.assertEqual(
            run_idle_bootstrap(
                self.paths,
                runner,
                sleeper=lambda _: None,
                now_provider=lambda: NOW,
                max_cycles=1,
            ),
            0,
        )
        self.assertEqual(runner.starts, [])

    def test_start_postcommit_opt_out_stops_child_and_clears_record(self) -> None:
        self.enable_config()

        class OptOutRunner(FakeProcessRunner):
            def start(inner_self, command, *, cwd):
                pid = super(OptOutRunner, inner_self).start(
                    command,
                    cwd=cwd,
                )
                disable_config(self.paths)
                return pid

            def terminate_exact(inner_self, pid, executable):
                stopped = super(
                    OptOutRunner,
                    inner_self,
                ).terminate_exact(pid, executable)
                if stopped:
                    inner_self.executables.pop(pid, None)
                return stopped

        runner = OptOutRunner()
        with self.assertRaises(LocalOptOutDuringStart):
            start_direct_service(self.paths, runner, now=NOW)
        self.assertEqual(len(runner.starts), 1)
        self.assertEqual(len(runner.terminations), 1)
        self.assertFalse(self.paths.service_record.exists())
        self.assertFalse(load_direct_config(self.paths)["localOptIn"])

    def test_bootstrap_postcommit_opt_out_exits_without_listener(self) -> None:
        self.enable_config()

        class OptOutRunner(FakeProcessRunner):
            def start(inner_self, command, *, cwd):
                pid = super(OptOutRunner, inner_self).start(
                    command,
                    cwd=cwd,
                )
                disable_config(self.paths)
                return pid

            def terminate_exact(inner_self, pid, executable):
                stopped = super(
                    OptOutRunner,
                    inner_self,
                ).terminate_exact(pid, executable)
                if stopped:
                    inner_self.executables.pop(pid, None)
                return stopped

        runner = OptOutRunner()
        self.assertEqual(
            run_idle_bootstrap(
                self.paths,
                runner,
                sleeper=lambda _: None,
                now_provider=lambda: NOW,
                max_cycles=1,
            ),
            0,
        )
        self.assertEqual(len(runner.starts), 1)
        self.assertEqual(len(runner.terminations), 1)
        self.assertFalse(self.paths.service_record.exists())

    def test_postcommit_opt_out_never_claims_failed_stop_is_safe(self) -> None:
        self.enable_config()

        class RefusingRunner(FakeProcessRunner):
            def start(inner_self, command, *, cwd):
                pid = super(RefusingRunner, inner_self).start(
                    command,
                    cwd=cwd,
                )
                disable_config(self.paths)
                return pid

            def terminate_exact(inner_self, pid, executable):
                inner_self.terminations.append((pid, executable))
                return False

        runner = RefusingRunner()
        with self.assertRaises(BridgeError) as raised:
            start_direct_service(self.paths, runner, now=NOW)
        self.assertNotIsInstance(
            raised.exception,
            LocalOptOutDuringStart,
        )
        self.assertTrue(self.paths.service_record.exists())

    def test_disable_bounded_recheck_stops_late_published_owned_pid(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = runner.next_pid
        runner.executables[pid] = self.paths.native_host.resolve()
        delays: list[float] = []

        def publish_after_first_check(delay: float) -> None:
            delays.append(delay)
            if len(delays) != 1:
                return
            self.paths.service_record.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            self.paths.service_record.write_text(
                json.dumps(
                    {
                        "contract": SERVICE_RECORD_CONTRACT,
                        "pid": pid,
                        "executable": str(
                            self.paths.native_host.resolve()
                        ),
                        "configPath": str(
                            self.paths.direct_config.resolve()
                        ),
                        "startedAtUtc": "2026-07-29T04:30:00Z",
                    }
                ),
                encoding="utf-8",
            )

        disabled, stopped = disable_and_stop_direct_service(
            self.paths,
            runner,
            sleeper=publish_after_first_check,
            recheck_delays=(0.01, 0.02),
        )
        self.assertTrue(disabled)
        self.assertTrue(stopped)
        self.assertEqual(delays, [0.01])
        self.assertEqual(
            runner.terminations,
            [(pid, self.paths.native_host.resolve())],
        )
        self.assertFalse(load_direct_config(self.paths)["localOptIn"])
        self.assertFalse(self.paths.service_record.exists())

    def test_disable_callback_runs_after_opt_out_and_before_exact_stop(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        observed: list[tuple[bool, bool]] = []

        def publish_disabled_snapshot() -> None:
            observed.append(
                (
                    load_direct_config(self.paths)["localOptIn"],
                    runner.executable_for_pid(pid) is not None,
                )
            )

        disabled, stopped = disable_and_stop_direct_service(
            self.paths,
            runner,
            after_disable=publish_disabled_snapshot,
        )
        self.assertEqual((disabled, stopped), (True, True))
        self.assertEqual(observed, [(False, True)])

    def test_disable_already_opted_out_still_stops_exact_owned_service(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        set_direct_config_enabled(self.paths, False)

        disabled, stopped = disable_and_stop_direct_service(
            self.paths,
            runner,
        )

        self.assertEqual((disabled, stopped), (False, True))
        self.assertEqual(
            runner.terminations,
            [(pid, self.paths.native_host.resolve())],
        )
        self.assertFalse(self.paths.service_record.exists())

    def test_disable_retries_snapshot_after_stop_if_pre_stop_write_failed(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        start_direct_service(self.paths, runner, now=NOW)
        calls: list[str] = []

        def first_write() -> None:
            calls.append("before")
            raise OSError("temporary-denied")

        disabled, stopped = disable_and_stop_direct_service(
            self.paths,
            runner,
            after_disable=first_write,
            after_stop=lambda: calls.append("after"),
        )

        self.assertEqual((disabled, stopped), (True, True))
        self.assertEqual(calls, ["before", "after"])

    def test_disable_reports_final_snapshot_failure_after_exact_stop(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        with self.assertRaisesRegex(
            BridgeError,
            "最终停用快照写入失败.*after-denied",
        ):
            disable_and_stop_direct_service(
                self.paths,
                runner,
                after_disable=lambda: None,
                after_stop=lambda: (_ for _ in ()).throw(
                    OSError("after-denied")
                ),
            )
        self.assertEqual(
            runner.terminations,
            [(pid, self.paths.native_host.resolve())],
        )
        self.assertFalse(self.paths.service_record.exists())

    def test_disable_snapshot_failure_is_visible_but_exact_service_still_stops(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        with self.assertRaisesRegex(
            BridgeError,
            "停用快照写入失败.*snapshot-write-denied",
        ):
            disable_and_stop_direct_service(
                self.paths,
                runner,
                after_disable=lambda: (_ for _ in ()).throw(
                    OSError("snapshot-write-denied")
                ),
            )
        self.assertEqual(
            runner.terminations,
            [(pid, self.paths.native_host.resolve())],
        )
        self.assertFalse(load_direct_config(self.paths)["localOptIn"])

    def test_disable_recheck_fails_closed_if_owned_record_remains(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        start_direct_service(self.paths, runner, now=NOW)
        runner.executables.clear()
        with self.assertRaises(BridgeError):
            disable_and_stop_direct_service(
                self.paths,
                runner,
                sleeper=lambda _: None,
                recheck_delays=(0.01,),
            )
        self.assertFalse(load_direct_config(self.paths)["localOptIn"])
        self.assertTrue(self.paths.service_record.exists())

    def test_bootstrap_restarts_after_normal_and_abnormal_exit(self) -> None:
        class RestartRunner(FakeProcessRunner):
            def __init__(self) -> None:
                super().__init__()
                self.next_pid = 5000

            def start(self, command, *, cwd):
                pid = self.next_pid
                self.next_pid += 1
                self.starts.append((tuple(command), cwd))
                self.executables[pid] = Path(command[0])
                return pid

        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                self.enable_config()
                runner = RestartRunner()
                sleeps: list[float] = []

                def sleeper(delay: float) -> None:
                    sleeps.append(delay)
                    if delay == 5.0 and runner.executables:
                        pid = max(runner.executables)
                        runner.executables.pop(pid)
                        runner.last_exit_code = exit_code

                self.assertEqual(
                    run_idle_bootstrap(
                        self.paths,
                        runner,
                        sleeper=sleeper,
                        now_provider=lambda: NOW,
                        max_cycles=2,
                    ),
                    0,
                )
                self.assertEqual(len(runner.starts), 2)
                self.assertIn(1.0, sleeps)
                self.paths.service_record.unlink(missing_ok=True)

    def test_bootstrap_restart_backoff_is_capped(self) -> None:
        class RestartRunner(FakeProcessRunner):
            def start(self, command, *, cwd):
                pid = self.next_pid
                self.next_pid += 1
                self.starts.append((tuple(command), cwd))
                self.executables[pid] = Path(command[0])
                return pid

        self.enable_config()
        runner = RestartRunner()
        sleeps: list[float] = []

        def sleeper(delay: float) -> None:
            sleeps.append(delay)
            if runner.executables:
                runner.executables.pop(max(runner.executables))

        run_idle_bootstrap(
            self.paths,
            runner,
            sleeper=sleeper,
            now_provider=lambda: NOW,
            max_cycles=7,
        )
        self.assertEqual(len(runner.starts), 7)
        self.assertEqual(
            sleeps,
            [5.0, 1.0, 5.0, 2.0, 5.0, 5.0, 5.0,
             10.0, 5.0, 30.0, 5.0, 30.0],
        )

    def test_bootstrap_supervises_online_process_without_double_start(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        self.write_runtime(pid=pid, state="idle", updated=NOW)
        sleeps: list[float] = []

        run_idle_bootstrap(
            self.paths,
            runner,
            sleeper=sleeps.append,
            now_provider=lambda: NOW,
            max_cycles=3,
        )
        self.assertEqual(len(runner.starts), 1)
        self.assertEqual(sleeps, [5.0, 5.0])

    def test_bootstrap_stops_loop_when_config_flips_false(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()

        def disable_after_first_poll(_: float) -> None:
            disable_config(self.paths)

        run_idle_bootstrap(
            self.paths,
            runner,
            sleeper=disable_after_first_poll,
            now_provider=lambda: NOW,
        )
        self.assertEqual(len(runner.starts), 1)
        self.assertFalse(load_direct_config(self.paths)["localOptIn"])

    def test_bootstrap_never_kills_or_impersonates_foreign_pid(self) -> None:
        self.enable_config()
        runner = FakeProcessRunner()
        pid = start_direct_service(self.paths, runner, now=NOW)
        runner.executables[pid] = self.root / "foreign.exe"

        with self.assertRaises(BridgeError):
            run_idle_bootstrap(
                self.paths,
                runner,
                sleeper=lambda _: None,
                now_provider=lambda: NOW,
                max_cycles=1,
            )
        self.assertEqual(len(runner.starts), 1)
        self.assertEqual(runner.terminations, [])

    def test_bootstrap_restarts_exact_process_after_three_stale_polls(self) -> None:
        class TerminatingRunner(FakeProcessRunner):
            def start(self, command, *, cwd):
                pid = self.next_pid
                self.next_pid += 1
                self.starts.append((tuple(command), cwd))
                self.executables[pid] = Path(command[0])
                return pid

            def terminate_exact(self, pid, executable):
                self.terminations.append((pid, executable))
                if self.executables.get(pid) != executable:
                    return False
                self.executables.pop(pid)
                return True

        self.enable_config()
        runner = TerminatingRunner()
        start_direct_service(self.paths, runner, now=NOW)
        run_idle_bootstrap(
            self.paths,
            runner,
            sleeper=lambda _: None,
            now_provider=lambda: NOW,
            max_cycles=3,
        )
        self.assertEqual(len(runner.terminations), 1)
        self.assertEqual(len(runner.starts), 2)


if __name__ == "__main__":
    unittest.main()
