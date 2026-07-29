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


class DirectPackageTests(unittest.TestCase):
    def _sources(self, root: Path) -> tuple[Path, Path]:
        audio = root / "ComputerVoiceAudio"
        desktop = root / "computer-voice-desktop"
        audio.mkdir()
        desktop.mkdir()
        (audio / "ComputerVoiceAudio.csproj").write_text("<Project />", encoding="utf-8")
        (audio / "Program.cs").write_text("class Program {}", encoding="utf-8")
        (audio / "obj").mkdir()
        (audio / "obj" / "Generated.cs").write_text("generated", encoding="utf-8")
        (desktop / "desktop_launcher.py").write_text("print('desktop')", encoding="utf-8")
        (desktop / "bridge_core.py").write_text("VALUE = 1", encoding="utf-8")
        (desktop / "tests").mkdir()
        (desktop / "tests" / "test_desktop.py").write_text("generated", encoding="utf-8")
        return audio, desktop

    def test_build_is_versioned_deterministic_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
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
            self.assertIn("package_computer_voice_direct.py", source_paths)
            self.assertIn("bw_computer_voice_typist_helper.py", source_paths)
            self.assertIn("bw_computer_voice_supervisor.py", source_paths)
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
            self.assertTrue(any("publish" in call for call in runner.calls))
            self.assertTrue(any("--onefile" in call for call in runner.calls))
            publish_call = next(call for call in runner.calls if "publish" in call)
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

    def test_packaged_self_tests_only_use_self_test_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            candidates = root / "candidates"
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")
            runner = FakeRunner()
            archive = package.build_candidate(
                "0.4.1", runner=runner, candidates=candidates, dotnet=dotnet,
                pyinstaller=pyinstaller, audio_source=audio, desktop_source=desktop,
            )
            runner.calls.clear()
            runner.cwds.clear()
            with patch.object(
                package,
                "_read_archive",
                wraps=package._read_archive,
            ) as read_archive:
                package.run_packaged_self_tests(archive, runner=runner)
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

    def test_packaged_self_test_uses_one_verified_archive_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
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
            with patch.object(
                package,
                "_read_regular",
                side_effect=replace_after_read,
            ):
                package.run_packaged_self_tests(archive, runner=runner)
            self.assertEqual(archive_reads, 1)
            self.assertEqual(len(runner.calls), 2)

    def test_packaged_self_test_compiles_python_runtime_without_running_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
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
            )
            runner = FakeRunner()
            with self.assertRaisesRegex(
                package.PackageError,
                "Python runtime 语法无效",
            ):
                package.run_packaged_self_tests(
                    archive,
                    runner=runner,
                )
            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(
                all(call[-1:] == ("--self-test",) for call in runner.calls)
            )

    def test_existing_candidate_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            candidates = Path(raw) / "candidates"
            package.candidate_directory("0.4.1", candidates=candidates).mkdir(parents=True)
            with self.assertRaisesRegex(package.PackageError, "拒绝覆盖"):
                package.build_candidate("0.4.1", candidates=candidates)

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

    def test_source_change_during_build_fails_and_removes_exact_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio, desktop = self._sources(root)
            candidates = root / "candidates"
            dotnet = root / "dotnet.exe"
            pyinstaller = root / "pyinstaller.exe"
            dotnet.write_bytes(b"tool")
            pyinstaller.write_bytes(b"tool")

            class MutatingRunner(FakeRunner):
                def run(self, args, *, cwd):
                    result = super().run(args, cwd=cwd)
                    if "publish" in tuple(args):
                        (audio / "Program.cs").write_text(
                            "class Program { static int Changed = 1; }",
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
        }
        build_inputs = [
            {
                "path": "ComputerVoiceAudio/Program.cs",
                "sha256": "1" * 64,
            },
            {
                "path": "bw_computer_voice_supervisor.py",
                "sha256": "2" * 64,
            },
            {
                "path": "bw_computer_voice_typist_helper.py",
                "sha256": "3" * 64,
            },
            {
                "path": "computer-voice-desktop/desktop_launcher.py",
                "sha256": "4" * 64,
            },
            {
                "path": "package_computer_voice_direct.py",
                "sha256": "5" * 64,
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
