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
    DIRECT_CONFIG_CONTRACT,
    FIXED_ALLOWED_TAILSCALE_USER_LOGIN,
    DIRECT_STATUS_CONTRACT,
    FIXED_APP_KIND,
    FIXED_LISTEN_HOST,
    FIXED_LISTEN_PORT,
    FIXED_OUTPUT_SCOPE,
    LOCAL_PACKAGED_APP_IDS,
    LocalOptOutDuringStart,
    RenderEndpoint,
    SERVICE_RECORD_CONTRACT,
    WindowsProcessRunner,
    build_direct_config,
    build_local_app_launch_command,
    build_self_test_report,
    build_start_command,
    build_tailscale_command_plan,
    disable_and_stop_direct_service,
    disable_config,
    enumerate_active_render_endpoints,
    legacy_microphone_config_requires_migration,
    load_direct_config,
    read_direct_status,
    run_idle_bootstrap,
    run_tailscale_read_only_preflight,
    save_enabled_config,
    start_direct_service,
    stop_direct_service,
    validate_direct_config,
)


NOW = datetime(2026, 7, 29, 4, 30, tzinfo=timezone.utc)
VIRTUAL_MICROPHONE = RenderEndpoint(
    "{explicit-virtual-microphone-render-endpoint}",
    "Virtual microphone A",
)
VIRTUAL_SPEAKER = RenderEndpoint(
    "{explicit-virtual-speaker-render-endpoint}",
    "Virtual speaker B",
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
        )
        self.assertEqual(value["contract"], DIRECT_CONFIG_CONTRACT)
        self.assertEqual(
            value["contract"],
            "reader-computer-voice-direct-config/4",
        )
        self.assertEqual(value["contextDeliveryMode"], "legacy-inject")
        self.assertEqual(value["listenHost"], FIXED_LISTEN_HOST)
        self.assertEqual(value["listenPort"], FIXED_LISTEN_PORT)
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

    def test_v3_config_requires_explicit_single_user_true(self) -> None:
        invalid = build_direct_config(
            VIRTUAL_MICROPHONE.endpoint_id,
            VIRTUAL_SPEAKER.endpoint_id,
            self.paths.runtime_status,
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
