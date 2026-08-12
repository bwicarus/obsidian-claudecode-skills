from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "computer_voice_package", HERE / "package_computer_voice_direct.py"
)
assert SPEC and SPEC.loader
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.cwds: list[Path] = []

    def run(self, args, *, cwd):
        command = tuple(args)
        self.calls.append(command)
        self.cwds.append(Path(cwd))
        if command[-1:] == ("--version",):
            return package.CommandResult(0, "8.0.423\n" if "dotnet" in command[0] else "6.16.0\n")
        if "publish" in command:
            publish_dir = Path(command[command.index("--output") + 1])
            publish_dir.mkdir(parents=True, exist_ok=True)
            (publish_dir / "bw-computer-voice-audio.exe").write_bytes(b"native-bundle")
            return package.CommandResult(0)
        if "--onefile" in command:
            dist_dir = Path(command[command.index("--distpath") + 1])
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "BW-Computer-Voice-Bridge.exe").write_bytes(b"desktop-bundle")
            return package.CommandResult(0)
        if command[-1:] == ("--self-test",):
            return package.CommandResult(0, '{"ok":true}')
        return package.CommandResult(1, stderr="unexpected fake command")


class FakeStdioRunner:
    def __init__(self, *, result: package.CommandResult | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, str]] = []
        self.result = result

    def run(self, args, *, cwd, stdin):
        command = tuple(args)
        self.calls.append((command, Path(cwd), stdin))
        state_path = Path(command[-1])
        snapshot = json.loads(state_path.read_text(encoding="utf-8"))
        if self.result is not None:
            return self.result
        responses = (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "serverInfo": {
                        "name": "bw-reader-context-snapshot",
                        "version": "1.2.0",
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {"name": "reader_context_snapshot"},
                        {"name": "reader_capability_guide"},
                        {"name": "reader_visual_image"},
                        {"name": "reader_browser_control"},
                        {"name": "reader_card"},
                        {"name": "reader_command"},
                    ],
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    **snapshot,
                                    "mcp": {"instanceId": "fake"},
                                },
                                separators=(",", ":"),
                            ),
                        },
                    ],
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": "# Reader capability index\n"
                            "Reader progressive disclosure guide is ready.",
                        },
                    ],
                },
            },
        )
        return package.CommandResult(
            0,
            "\n".join(json.dumps(item) for item in responses) + "\n",
        )


class FakeInstallService:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.events: list[str] = []

    def is_running(self, install_root: Path) -> bool:
        self.events.append("is-running")
        return self.running

    def stop(self, install_root: Path) -> None:
        self.events.append("stop")
        if not self.running:
            raise AssertionError("service was not running")
        self.running = False

    def start(self, install_root: Path) -> None:
        self.events.append("start")
        self.running = True


class FakeMcpController:
    def __init__(self, *, stopped: int = 0) -> None:
        self.stopped = stopped
        self.events: list[str] = []

    def quiesce(self, install_root: Path) -> int:
        self.events.append("quiesce")
        return self.stopped


class FakeInstalledProcessBackend:
    def __init__(self, processes) -> None:
        self.processes = list(processes)
        self.terminated: list[int] = []

    def list_exact_executable(self, executable: Path):
        return tuple(self.processes)

    def terminate_exact(self, process) -> bool:
        if process not in self.processes:
            return False
        self.terminated.append(process.pid)
        self.processes.remove(process)
        return True


class DirectPackageTests(unittest.TestCase):
    def _sources(self, root: Path) -> tuple[Path, Path]:
        audio = root / "ComputerVoiceAudio"
        desktop = root / "computer-voice-desktop"
        audio.mkdir()
        desktop.mkdir()
        (audio / "ComputerVoiceAudio.csproj").write_text("<Project />", encoding="utf-8")
        (audio / "Program.cs").write_text("class Program {}", encoding="utf-8")
        (audio / "ReaderCapabilities").mkdir()
        (audio / "ReaderCapabilities" / "index.md").write_text(
            "# Reader capability index",
            encoding="utf-8",
        )
        (audio / "obj").mkdir()
        (audio / "obj" / "Generated.cs").write_text("generated", encoding="utf-8")
        (desktop / "desktop_launcher.py").write_text("print('desktop')", encoding="utf-8")
        (desktop / "bridge_core.py").write_text("VALUE = 1", encoding="utf-8")
        (desktop / "tests").mkdir()
        (desktop / "tests" / "test_desktop.py").write_text("generated", encoding="utf-8")
        return audio, desktop

    def _runtime_sources(self, root: Path) -> tuple[Path, Path, Path]:
        runtime = root / "typist-runtime"
        runtime.mkdir()
        script = runtime / "voice_typist.py"
        ipc = runtime / "typist_ipc.py"
        launcher = runtime / "voice-typist-launcher.ps1"
        script.write_text(
            "import typist_ipc as typist_ipc_runtime\n"
            "typist_ipc_runtime.connect_pipe\n"
            "typist_ipc_runtime.serve\n"
            "def process_generation_alive(): pass\n"
            "COMMAND = 'queue-resolve'\n",
            encoding="utf-8",
        )
        ipc.write_text(
            "import ctypes\n"
            "ctypes.WinDLL\n"
            "BW_TYPIST_IPC_HANDOFF_FAILED = 'x'\n",
            encoding="utf-8",
        )
        launcher.write_text(
            "param([ValidateSet('Status','Start','Stop','ResolveUncertain')]$Action,\n"
            "      [int]$ExpectedPid = 0,\n"
            "      [long]$ExpectedStartFileTimeUtc = 0,\n"
            "      [int]$OwnerPid = 0,\n"
            "      [long]$OwnerStartFileTimeUtc = 0)\n"
            "$install = $PSScriptRoot\n"
            "$arguments = @('run', '--idle-exit-seconds', '600', "
            "'--owner-process-id')\n"
            "$resolve = @('queue-resolve', '--launcher-confirmed-stopped')\n"
            "Get-TypistProcess -Strict\n",
            encoding="utf-8",
        )
        return script, ipc, launcher

    def _candidate(self, root: Path, version: str = "0.4.1") -> Path:
        audio, desktop = self._sources(root)
        typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
        dotnet = root / "dotnet.exe"
        pyinstaller = root / "pyinstaller.exe"
        dotnet.write_bytes(b"tool")
        pyinstaller.write_bytes(b"tool")
        return package.build_candidate(
            version,
            runner=FakeRunner(),
            candidates=root / "candidates",
            dotnet=dotnet,
            pyinstaller=pyinstaller,
            audio_source=audio,
            desktop_source=desktop,
            typist_script_source=typist_script,
            typist_ipc_source=typist_ipc,
            typist_launcher_source=typist_launcher,
        )

    def _installed_old_payload(
        self,
        archive: Path,
        install_root: Path,
    ) -> tuple[dict, dict[str, bytes]]:
        manifest, archive_payload = package._verified_archive_contents(archive)
        prefix = package.bundle_name(manifest["version"])
        payload = {
            relative: archive_payload[f"{prefix}/{relative}"]
            for relative in package.PAYLOAD_RELATIVE_PATHS
        }
        old_payload = dict(payload)
        old_payload[package.NATIVE_REL] = b"old-native"
        old_payload[package.DESKTOP_REL] = b"old-desktop"
        old_manifest = copy.deepcopy(manifest)
        old_manifest["version"] = "0.4.0"
        for entry in old_manifest["files"]:
            content = old_payload[entry["path"]]
            entry["sha256"] = package._sha256(content)
            entry["size"] = len(content)
        install_root.mkdir()
        package._write_payload_tree(install_root, old_manifest, old_payload)
        (install_root / "native-host" / "computer-voice-direct.config.json").write_text(
            "config-must-survive", encoding="utf-8"
        )
        (install_root / "runtime").mkdir()
        (install_root / "runtime" / "state.json").write_text(
            "runtime-must-survive", encoding="utf-8"
        )
        (install_root / "dotnet8").mkdir()
        (install_root / "dotnet8" / "dotnet.exe").write_bytes(b"dotnet-must-survive")
        return old_manifest, old_payload

    def test_atomic_install_backs_up_payload_preserves_state_and_restores_service(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self._candidate(root)
            install_root = root / "install"
            backup_root = root / "backups"
            old_manifest, old_payload = self._installed_old_payload(
                archive, install_root
            )
            service = FakeInstallService()
            mcp = FakeMcpController(stopped=2)

            receipt = package.install_archive(
                archive,
                install_root=install_root,
                backup_root=backup_root,
                service_controller=service,
                mcp_controller=mcp,
                runner=FakeRunner(),
            )

            self.assertEqual(receipt["installedVersion"], "0.4.1")
            self.assertEqual(receipt["previousVersion"], "0.4.0")
            self.assertEqual(receipt["mcpProcessesStopped"], 2)
            self.assertEqual(mcp.events, ["quiesce"])
            self.assertEqual(
                service.events,
                ["is-running", "stop", "start"],
            )
            self.assertTrue(service.running)
            installed, _ = package._verified_install_directory(
                install_root, label="test installed"
            )
            self.assertEqual(installed["version"], "0.4.1")
            self.assertEqual(
                (install_root / "native-host" / "computer-voice-direct.config.json").read_text(),
                "config-must-survive",
            )
            self.assertEqual(
                (install_root / "runtime" / "state.json").read_text(),
                "runtime-must-survive",
            )
            self.assertEqual(
                (install_root / "dotnet8" / "dotnet.exe").read_bytes(),
                b"dotnet-must-survive",
            )
            backup = Path(receipt["backup"])
            backed_manifest, backed_payload = package._verified_install_directory(
                backup, label="test backup"
            )
            self.assertEqual(backed_manifest, old_manifest)
            self.assertEqual(backed_payload, old_payload)

            rollback_receipt = package.rollback_install(
                backup,
                install_root=install_root,
                backup_root=backup_root,
                service_controller=service,
                mcp_controller=mcp,
                runner=FakeRunner(),
            )
            self.assertEqual(rollback_receipt["installedVersion"], "0.4.0")
            self.assertEqual(mcp.events, ["quiesce", "quiesce"])
            rolled_back, rolled_back_payload = package._verified_install_directory(
                install_root, label="test explicit rollback"
            )
            self.assertEqual(rolled_back, old_manifest)
            self.assertEqual(rolled_back_payload, old_payload)
            self.assertEqual(
                service.events,
                [
                    "is-running", "stop", "start",
                    "is-running", "stop", "start",
                ],
            )

    def test_partial_install_failure_restores_old_payload_and_service(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self._candidate(root)
            install_root = root / "install"
            backup_root = root / "backups"
            old_manifest, old_payload = self._installed_old_payload(
                archive, install_root
            )
            service = FakeInstallService()
            mcp = FakeMcpController(stopped=1)
            calls = 0

            def fail_once(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated replace failure")
                package.os.replace(source, target)

            with self.assertRaisesRegex(
                package.PackageError,
                "安装失败，已自动回滚",
            ):
                package.install_archive(
                    archive,
                    install_root=install_root,
                    backup_root=backup_root,
                    service_controller=service,
                    mcp_controller=mcp,
                    runner=FakeRunner(),
                    replace_file=fail_once,
                )

            restored_manifest, restored_payload = package._verified_install_directory(
                install_root, label="test restored"
            )
            self.assertEqual(restored_manifest, old_manifest)
            self.assertEqual(restored_payload, old_payload)
            self.assertTrue(service.running)
            self.assertEqual(mcp.events, ["quiesce", "quiesce"])
            self.assertEqual(
                service.events,
                ["is-running", "stop", "is-running", "start"],
            )
            self.assertEqual(
                (install_root / "runtime" / "state.json").read_text(),
                "runtime-must-survive",
            )

    def test_mcp_quiesce_stops_only_exact_state_bound_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            install_root = Path(raw) / "install root"
            executable = install_root / package.NATIVE_REL
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"installed-native")
            executable = executable.resolve(strict=True)
            state = (
                install_root / "runtime" / "reader-context-snapshot.json"
            ).resolve(strict=False)
            process = package.InstalledProcess(
                pid=101,
                executable=executable,
                command_line=(
                    f'"{executable}" --reader-context-mcp --state "{state}"'
                ),
                creation_date="20260812123456.000000+540",
            )
            backend = FakeInstalledProcessBackend([process])

            stopped = package.ExactReaderContextMcpController(backend).quiesce(
                install_root
            )

            self.assertEqual(stopped, 1)
            self.assertEqual(backend.terminated, [101])

    def test_install_refuses_unfamiliar_same_executable_without_terminating(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = self._candidate(root)
            install_root = root / "install"
            backup_root = root / "backups"
            old_manifest, old_payload = self._installed_old_payload(
                archive, install_root
            )
            executable = (install_root / package.NATIVE_REL).resolve(strict=True)
            process = package.InstalledProcess(
                pid=202,
                executable=executable,
                command_line=f'"{executable}" --describe',
                creation_date="20260812123556.000000+540",
            )
            backend = FakeInstalledProcessBackend([process])
            service = FakeInstallService()

            with self.assertRaisesRegex(
                package.PackageError,
                "非 MCP 模式占用",
            ):
                package.install_archive(
                    archive,
                    install_root=install_root,
                    backup_root=backup_root,
                    service_controller=service,
                    mcp_controller=package.ExactReaderContextMcpController(backend),
                    runner=FakeRunner(),
                )

            self.assertEqual(backend.terminated, [])
            self.assertEqual(
                service.events,
                ["is-running", "stop", "is-running", "start"],
            )
            restored_manifest, restored_payload = package._verified_install_directory(
                install_root, label="test unfamiliar owner unchanged"
            )
            self.assertEqual(restored_manifest, old_manifest)
            self.assertEqual(restored_payload, old_payload)

    def test_build_is_versioned_deterministic_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            candidates = root / "candidates"
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            runner = FakeRunner()
            archive = package.build_candidate(
                "0.4.1",
                runner=runner,
                candidates=candidates,
                dotnet=dotnet,
                pyinstaller=pyinstaller,
                audio_source=audio,
                desktop_source=desktop,
                typist_script_source=typist_script,
                typist_ipc_source=typist_ipc,
                typist_launcher_source=typist_launcher,
            )
            self.assertEqual(archive.name, "bw-computer-voice-direct-0.4.1-windows-x64.zip")
            manifest = package.verify_archive(archive, expected_version="0.4.1")
            self.assertEqual(manifest["contract"], package.PACKAGE_CONTRACT)
            self.assertEqual([item["path"] for item in manifest["files"]], list(package.PAYLOAD_RELATIVE_PATHS))
            self.assertEqual(
                manifest["buildInputs"]["environment"],
                package.DETERMINISTIC_BUILD_ENV,
            )
            self.assertNotIn(str(root), json.dumps(manifest))
            source_paths = {
                item["path"] for item in manifest["buildInputs"]["sourceFiles"]
            }
            source_hashes = {
                item["path"]: item["sha256"]
                for item in manifest["buildInputs"]["sourceFiles"]
            }
            self.assertNotIn("input/ComputerVoiceAudio/obj/Generated.cs", source_paths)
            self.assertNotIn(
                "input/computer-voice-desktop/tests/test_desktop.py",
                source_paths,
            )
            self.assertIn(
                "input/ComputerVoiceAudio/ComputerVoiceAudio.csproj",
                source_paths,
            )
            self.assertIn("input/ComputerVoiceAudio/Program.cs", source_paths)
            self.assertIn(
                "input/ComputerVoiceAudio/ReaderCapabilities/index.md",
                source_paths,
            )
            self.assertIn(
                "input/computer-voice-desktop/desktop_launcher.py",
                source_paths,
            )
            self.assertIn(
                "input/computer-voice-desktop/bridge_core.py",
                source_paths,
            )
            self.assertIn("package_computer_voice_direct.py", source_paths)
            self.assertIn("bw_computer_voice_typist_helper.py", source_paths)
            self.assertIn("bw_computer_voice_supervisor.py", source_paths)
            self.assertIn("input/voice_typist.py", source_paths)
            self.assertIn("input/typist_ipc.py", source_paths)
            self.assertIn("input/voice-typist-launcher.ps1", source_paths)
            self.assertEqual(
                source_hashes["bw_computer_voice_typist_helper.py"],
                package._sha256(
                    package.TYPIST_HELPER_SOURCE.read_bytes()
                ),
            )
            self.assertEqual(
                source_hashes["bw_computer_voice_supervisor.py"],
                package._sha256(
                    package.SUPERVISOR_SOURCE.read_bytes()
                ),
            )
            self.assertEqual(
                source_hashes["input/voice_typist.py"],
                package._sha256(typist_script.read_bytes()),
            )
            self.assertEqual(
                source_hashes["input/typist_ipc.py"],
                package._sha256(typist_ipc.read_bytes()),
            )
            self.assertEqual(
                source_hashes["input/voice-typist-launcher.ps1"],
                package._sha256(typist_launcher.read_bytes()),
            )
            with zipfile.ZipFile(archive) as zip_file:
                self.assertEqual(zip_file.namelist(), sorted(zip_file.namelist()))
                for info in zip_file.infolist():
                    self.assertEqual(info.date_time, package.ARCHIVE_STAMP)
                prefix = package.bundle_name("0.4.1")
                self.assertEqual(
                    zip_file.read(
                        f"{prefix}/{package.TYPIST_HELPER_REL}"
                    ),
                    package.TYPIST_HELPER_SOURCE.read_bytes(),
                )
                self.assertEqual(
                    zip_file.read(
                        f"{prefix}/{package.SUPERVISOR_REL}"
                    ),
                    package.SUPERVISOR_SOURCE.read_bytes(),
                )
                self.assertEqual(
                    zip_file.read(f"{prefix}/{package.TYPIST_SCRIPT_REL}"),
                    typist_script.read_bytes(),
                )
                self.assertEqual(
                    zip_file.read(f"{prefix}/{package.TYPIST_IPC_REL}"),
                    typist_ipc.read_bytes(),
                )
                self.assertEqual(
                    zip_file.read(f"{prefix}/{package.TYPIST_LAUNCHER_REL}"),
                    typist_launcher.read_bytes(),
                )
            self.assertTrue(any("publish" in call for call in runner.calls))
            self.assertTrue(any("--onefile" in call for call in runner.calls))
            publish_call = next(call for call in runner.calls if "publish" in call)
            pyinstaller_call = next(
                call for call in runner.calls if "--onefile" in call
            )
            self.assertEqual(
                Path(publish_call[publish_call.index("publish") + 1]),
                audio / "ComputerVoiceAudio.csproj",
            )
            self.assertEqual(
                Path(pyinstaller_call[-1]),
                desktop / "desktop_launcher.py",
            )
            self.assertEqual(
                Path(pyinstaller_call[pyinstaller_call.index("--paths") + 1]),
                desktop,
            )
            self.assertIn("--artifacts-path", publish_call)
            self.assertFalse(any("--direct-serve" in call for call in runner.calls))

    def test_verify_rejects_extra_entry_and_does_not_run_executables(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "bw-computer-voice-direct-0.4.1-windows-x64.zip"
            prefix = package.bundle_name("0.4.1")
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr(f"{prefix}/unexpected.txt", b"no")
            with self.assertRaises(package.PackageError):
                package.verify_archive(archive)

    def test_packaged_self_tests_exercise_self_tests_and_real_mcp_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            candidates = root / "candidates"
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            runner = FakeRunner()
            archive = package.build_candidate(
                "0.4.1", runner=runner, candidates=candidates, dotnet=dotnet,
                pyinstaller=pyinstaller, audio_source=audio, desktop_source=desktop,
                typist_script_source=typist_script,
                typist_ipc_source=typist_ipc,
                typist_launcher_source=typist_launcher,
            )
            runner.calls.clear()
            runner.cwds.clear()
            stdio_runner = FakeStdioRunner()
            with patch.object(
                package,
                "_read_archive",
                wraps=package._read_archive,
            ) as read_archive:
                package.run_packaged_self_tests(
                    archive,
                    runner=runner,
                    stdio_runner=stdio_runner,
                )
            self.assertEqual(read_archive.call_count, 1)
            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(all(call[-1:] == ("--self-test",) for call in runner.calls))
            self.assertTrue(
                all(Path(call[0]).suffix.casefold() == ".exe" for call in runner.calls)
            )
            self.assertEqual(len(set(runner.cwds)), 1)
            temporary_root = runner.cwds[0]
            prefix = package.bundle_name("0.4.1")
            for command, cwd in zip(runner.calls, runner.cwds, strict=True):
                executable = Path(command[0])
                self.assertEqual(cwd, temporary_root)
                self.assertTrue(executable.is_relative_to(cwd))
                self.assertEqual(
                    executable.relative_to(cwd).parts[0],
                    prefix,
                )
            self.assertEqual(len(stdio_runner.calls), 1)
            mcp_command, mcp_cwd, mcp_input = stdio_runner.calls[0]
            self.assertEqual(mcp_cwd, temporary_root)
            self.assertEqual(
                mcp_command[1:3],
                ("--reader-context-mcp", "--state"),
            )
            self.assertTrue(Path(mcp_command[0]).is_relative_to(mcp_cwd))
            self.assertTrue(Path(mcp_command[3]).is_relative_to(mcp_cwd))
            requests = [json.loads(line) for line in mcp_input.splitlines()]
            self.assertEqual(
                [item.get("method") for item in requests],
                [
                    "initialize",
                    "notifications/initialized",
                    "tools/list",
                    "tools/call",
                    "tools/call",
                ],
            )
            self.assertEqual(
                requests[3]["params"]["name"],
                "reader_context_snapshot",
            )
            self.assertEqual(
                requests[4]["params"],
                {
                    "name": "reader_capability_guide",
                    "arguments": {"topic": "index"},
                },
            )

    def test_packaged_self_test_uses_one_verified_archive_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            candidates = root / "candidates"
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            archive = package.build_candidate(
                "0.4.1",
                runner=FakeRunner(),
                candidates=candidates,
                dotnet=dotnet,
                pyinstaller=pyinstaller,
                audio_source=audio,
                desktop_source=desktop,
                typist_script_source=typist_script,
                typist_ipc_source=typist_ipc,
                typist_launcher_source=typist_launcher,
            )
            original_read = package._read_regular
            archive_reads = 0

            def replace_after_read(path):
                nonlocal archive_reads
                content = original_read(Path(path))
                if Path(path) == archive:
                    archive_reads += 1
                    archive.write_bytes(b"replaced-after-verified-read")
                return content

            runner = FakeRunner()
            stdio_runner = FakeStdioRunner()
            with patch.object(
                package,
                "_read_regular",
                side_effect=replace_after_read,
            ):
                package.run_packaged_self_tests(
                    archive,
                    runner=runner,
                    stdio_runner=stdio_runner,
                )
            self.assertEqual(archive_reads, 1)
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(len(stdio_runner.calls), 1)

    def test_packaged_self_test_compiles_python_runtime_without_running_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            candidates = root / "candidates"
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            helper = root / "bw_computer_voice_typist_helper.py"
            supervisor = root / "bw_computer_voice_supervisor.py"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            helper.write_text("def invalid(:\\n", encoding="utf-8")
            supervisor.write_text("VALUE = 1\\n", encoding="utf-8")
            archive = package.build_candidate(
                "0.4.1",
                runner=FakeRunner(),
                candidates=candidates,
                dotnet=dotnet,
                pyinstaller=pyinstaller,
                audio_source=audio,
                desktop_source=desktop,
                typist_helper_source=helper,
                supervisor_source=supervisor,
                typist_script_source=typist_script,
                typist_ipc_source=typist_ipc,
                typist_launcher_source=typist_launcher,
            )
            runner = FakeRunner()
            stdio_runner = FakeStdioRunner()
            with self.assertRaisesRegex(
                package.PackageError,
                "Python runtime 语法无效",
            ):
                package.run_packaged_self_tests(
                    archive,
                    runner=runner,
                    stdio_runner=stdio_runner,
                )
            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(
                all(call[-1:] == ("--self-test",) for call in runner.calls)
            )
            self.assertEqual(len(stdio_runner.calls), 1)

    def test_stdio_mcp_smoke_rejects_single_file_process_crash(self) -> None:
        with self.assertRaisesRegex(
            package.PackageError,
            "stdio MCP 前向测试退出失败.*TypeInfoResolver",
        ):
            package._validate_mcp_smoke_output(
                package.CommandResult(
                    1,
                    stderr=(
                        "InvalidOperationException: JsonSerializerOptions must "
                        "specify a TypeInfoResolver"
                    ),
                )
            )

    def test_existing_candidate_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            candidates = root / "candidates"
            package.candidate_directory("0.4.1", candidates=candidates).mkdir(parents=True)
            with self.assertRaisesRegex(package.PackageError, "拒绝覆盖"):
                package.build_candidate(
                    "0.4.1",
                    candidates=candidates,
                    typist_script_source=typist_script,
                    typist_ipc_source=typist_ipc,
                    typist_launcher_source=typist_launcher,
                )

    def test_real_runner_converts_timeout_to_bounded_failure(self) -> None:
        runner = package.SubprocessRunner(
            timeout_seconds=1,
            environment_overrides=package.DETERMINISTIC_BUILD_ENV,
        )
        with patch.object(
            package.subprocess,
            "run",
            side_effect=package.subprocess.TimeoutExpired(
                cmd=["self-test.exe"],
                timeout=1,
            ),
        ) as run:
            result = runner.run(("self-test.exe", "--self-test"), cwd=HERE)
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out after 1s", result.stderr)
        self.assertEqual(run.call_args.kwargs["timeout"], 1)
        for key, value in package.DETERMINISTIC_BUILD_ENV.items():
            self.assertEqual(run.call_args.kwargs["env"][key], value)

    def test_reparse_candidates_root_is_rejected_before_tools_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            candidates = root / "candidates"
            candidates.mkdir()
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            runner = FakeRunner()
            original = package._is_reparse_path

            def mark_candidates(path, status=None):
                if Path(path) == candidates:
                    return True
                return original(Path(path), status)

            with patch.object(
                package,
                "_is_reparse_path",
                side_effect=mark_candidates,
            ):
                with self.assertRaisesRegex(package.PackageError, "reparse"):
                    package.build_candidate(
                        "0.4.1",
                        runner=runner,
                        candidates=candidates,
                        dotnet=dotnet,
                        pyinstaller=pyinstaller,
                        audio_source=audio,
                        desktop_source=desktop,
                        typist_script_source=typist_script,
                        typist_ipc_source=typist_ipc,
                        typist_launcher_source=typist_launcher,
                    )
            self.assertEqual(runner.calls, [])

    def test_reparse_source_root_ancestor_fails_before_tools_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            runner = FakeRunner()
            original = package._is_reparse_path

            def mark_source_ancestor(path, status=None):
                if Path(path) == audio.parent:
                    return True
                return original(Path(path), status)

            with patch.object(
                package,
                "_is_reparse_path",
                side_effect=mark_source_ancestor,
            ):
                with self.assertRaisesRegex(
                    package.PackageError,
                    "祖先必须是非 reparse 普通目录",
                ):
                    package.build_candidate(
                        "0.4.1",
                        runner=runner,
                        candidates=root / "candidates",
                        dotnet=dotnet,
                        pyinstaller=pyinstaller,
                        audio_source=audio,
                        desktop_source=desktop,
                        typist_script_source=typist_script,
                        typist_ipc_source=typist_ipc,
                        typist_launcher_source=typist_launcher,
                    )
            self.assertEqual(runner.calls, [])

    def test_nested_reparse_source_is_rejected_instead_of_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            linked_source = desktop / "linked-production-source"
            linked_source.mkdir()
            (linked_source / "hidden.py").write_text(
                "VALUE = 'must-not-be-skipped'\n",
                encoding="utf-8",
            )
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            runner = FakeRunner()
            original = package._is_reparse_path

            def mark_nested_source(path, status=None):
                if Path(path) == linked_source:
                    return True
                return original(Path(path), status)

            with patch.object(
                package,
                "_is_reparse_path",
                side_effect=mark_nested_source,
            ):
                with self.assertRaisesRegex(
                    package.PackageError,
                    "拒绝静默跳过",
                ):
                    package.build_candidate(
                        "0.4.1",
                        runner=runner,
                        candidates=root / "candidates",
                        dotnet=dotnet,
                        pyinstaller=pyinstaller,
                        audio_source=audio,
                        desktop_source=desktop,
                        typist_script_source=typist_script,
                        typist_ipc_source=typist_ipc,
                        typist_launcher_source=typist_launcher,
                    )
            self.assertEqual(runner.calls, [])

    def test_missing_canonical_runtime_fails_before_tools_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            runner = FakeRunner()

            with self.assertRaisesRegex(
                package.PackageError,
                "canonical voice-typist runtime",
            ):
                package.build_candidate(
                    "0.4.1",
                    runner=runner,
                    candidates=root / "candidates",
                    dotnet=dotnet,
                    pyinstaller=pyinstaller,
                    audio_source=audio,
                    desktop_source=desktop,
                    typist_script_source=root / "missing" / "voice_typist.py",
                    typist_ipc_source=root / "missing" / "typist_ipc.py",
                    typist_launcher_source=(
                        root / "missing" / "voice-typist-launcher.ps1"
                    ),
                )
            self.assertEqual(runner.calls, [])

    def test_reparse_canonical_runtime_source_fails_before_tools_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            runner = FakeRunner()
            original = package._is_reparse_path

            def mark_runtime_script(path, status=None):
                if Path(path) == typist_script:
                    return True
                return original(Path(path), status)

            with patch.object(
                package,
                "_is_reparse_path",
                side_effect=mark_runtime_script,
            ):
                with self.assertRaisesRegex(
                    package.PackageError,
                    "拒绝链接或目录",
                ):
                    package.build_candidate(
                        "0.4.1",
                        runner=runner,
                        candidates=root / "candidates",
                        dotnet=dotnet,
                        pyinstaller=pyinstaller,
                        audio_source=audio,
                        desktop_source=desktop,
                        typist_script_source=typist_script,
                        typist_ipc_source=typist_ipc,
                        typist_launcher_source=typist_launcher,
                    )
            self.assertEqual(runner.calls, [])

    def test_failed_cleanup_refuses_non_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidates = root / "candidates"
            candidates.mkdir()
            outside = root / "outside" / "0.4.1"
            outside.mkdir(parents=True)
            marker = outside / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(package.PackageError, "精确版本子目录"):
                package._remove_failed_candidate(
                    outside,
                    candidates_root=candidates.resolve(),
                    version="0.4.1",
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_canonical_runtime_change_during_build_fails_and_removes_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            typist_script, typist_ipc, typist_launcher = self._runtime_sources(root)
            candidates = root / "candidates"
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")

            class MutatingRunner(FakeRunner):
                def run(self, args, *, cwd):
                    result = super().run(args, cwd=cwd)
                    if "publish" in tuple(args):
                        typist_script.write_text(
                            "VALUE = 'changed-during-build'\n",
                            encoding="utf-8",
                        )
                    return result

            with self.assertRaisesRegex(package.PackageError, "构建期间发生变化"):
                package.build_candidate(
                    "0.4.1",
                    runner=MutatingRunner(),
                    candidates=candidates,
                    dotnet=dotnet,
                    pyinstaller=pyinstaller,
                    audio_source=audio,
                    desktop_source=desktop,
                    typist_script_source=typist_script,
                    typist_ipc_source=typist_ipc,
                    typist_launcher_source=typist_launcher,
                )
            self.assertFalse(
                package.candidate_directory(
                    "0.4.1",
                    candidates=candidates,
                ).exists()
            )

    def test_manifest_rejects_inexact_build_metadata_and_sources(self) -> None:
        payload = {
            package.NATIVE_REL: b"native",
            package.DESKTOP_REL: b"desktop",
            package.TYPIST_HELPER_REL: b"helper",
            package.SUPERVISOR_REL: b"supervisor",
            package.TYPIST_SCRIPT_REL: b"typist",
            package.TYPIST_IPC_REL: b"ipc",
            package.TYPIST_LAUNCHER_REL: b"launcher",
        }
        build_inputs = [
            {
                "path": "ComputerVoiceAudio/Program.cs",
                "sha256": "1" * 64,
            },
            {
                "path": "bw_computer_voice_supervisor.py",
                "sha256": package._sha256(b"supervisor"),
            },
            {
                "path": "bw_computer_voice_typist_helper.py",
                "sha256": package._sha256(b"helper"),
            },
            {
                "path": "computer-voice-desktop/desktop_launcher.py",
                "sha256": "4" * 64,
            },
            {
                "path": "package_computer_voice_direct.py",
                "sha256": "5" * 64,
            },
            {
                "path": "typist-runtime/voice_typist.py",
                "sha256": package._sha256(b"typist"),
            },
            {
                "path": "typist-runtime/typist_ipc.py",
                "sha256": package._sha256(b"ipc"),
            },
            {
                "path": "typist-runtime/voice-typist-launcher.ps1",
                "sha256": package._sha256(b"launcher"),
            },
        ]
        build_inputs.sort(key=lambda item: item["path"])
        manifest = package.build_manifest(
            version="0.4.1",
            payload=payload,
            build_inputs=build_inputs,
            dotnet_version="8.0.423",
            pyinstaller_version="6.20.0",
        )
        package._validate_manifest(
            manifest,
            version="0.4.1",
            payload=payload,
        )

        invalid_manifests = []
        wrong_rid = copy.deepcopy(manifest)
        wrong_rid["buildInputs"]["dotnet"]["rid"] = "win-arm64"
        invalid_manifests.append(wrong_rid)
        non_boolean = copy.deepcopy(manifest)
        non_boolean["buildInputs"]["dotnet"]["singleFile"] = 1
        invalid_manifests.append(non_boolean)
        missing_dotnet_key = copy.deepcopy(manifest)
        del missing_dotnet_key["buildInputs"]["dotnet"]["selfContained"]
        invalid_manifests.append(missing_dotnet_key)
        wrong_console_mode = copy.deepcopy(manifest)
        wrong_console_mode["buildInputs"]["pyinstaller"]["noConsole"] = False
        invalid_manifests.append(wrong_console_mode)
        extra_pyinstaller_key = copy.deepcopy(manifest)
        extra_pyinstaller_key["buildInputs"]["pyinstaller"]["unknown"] = True
        invalid_manifests.append(extra_pyinstaller_key)
        wrong_build_environment = copy.deepcopy(manifest)
        wrong_build_environment["buildInputs"]["environment"][
            "PYTHONHASHSEED"
        ] = "random"
        invalid_manifests.append(wrong_build_environment)
        duplicate_sources = copy.deepcopy(manifest)
        duplicate_sources["buildInputs"]["sourceFiles"].append(
            copy.deepcopy(duplicate_sources["buildInputs"]["sourceFiles"][0])
        )
        invalid_manifests.append(duplicate_sources)
        unsorted_sources = copy.deepcopy(manifest)
        unsorted_sources["buildInputs"]["sourceFiles"].reverse()
        invalid_manifests.append(unsorted_sources)
        missing_packager = copy.deepcopy(manifest)
        missing_packager["buildInputs"]["sourceFiles"] = [
            item
            for item in missing_packager["buildInputs"]["sourceFiles"]
            if item["path"] != "package_computer_voice_direct.py"
        ]
        invalid_manifests.append(missing_packager)
        mismatched_runtime_digest = copy.deepcopy(manifest)
        runtime_source = next(
            item
            for item in mismatched_runtime_digest["buildInputs"]["sourceFiles"]
            if item["path"] == package.TYPIST_SCRIPT_REL
        )
        runtime_source["sha256"] = "f" * 64
        invalid_manifests.append(mismatched_runtime_digest)

        for invalid in invalid_manifests:
            with self.subTest(invalid=invalid):
                with self.assertRaises(package.PackageError):
                    package._validate_manifest(
                        invalid,
                        version="0.4.1",
                        payload=payload,
                    )


if __name__ == "__main__":
    unittest.main()
