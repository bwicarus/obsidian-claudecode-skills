#!/usr/bin/env python3
"""Security regressions for the Windows test-channel release pipeline."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import publish_test_channel as publish
import release_preflight as release
import package_safari as safari

_HANDOFF_SPEC = importlib.util.spec_from_file_location(
    "bw_reader_handoff_check",
    HERE / "handoff_check.py",
)
assert _HANDOFF_SPEC is not None and _HANDOFF_SPEC.loader is not None
handoff = importlib.util.module_from_spec(_HANDOFF_SPEC)
_HANDOFF_SPEC.loader.exec_module(handoff)


class ReleasePipelineTests(unittest.TestCase):
    def test_production_reader_comparison_uses_generated_reader_parts(self) -> None:
        class ReaderStampEntry:
            policy = "reader_git_stamp"

            @staticmethod
            def deployed_content_matches(source: bytes, target: bytes) -> bool:
                return target == source + b"\n;window.__READER_GIT='abc1234';\n"

        with tempfile.TemporaryDirectory() as raw:
            pdf = Path(raw) / "pdf"
            parts = pdf / "reader.src"
            parts.mkdir(parents=True)
            (pdf / "reader.js").write_bytes(b"stale repository bundle")
            (parts / "01-a.js").write_bytes(b"first\n")
            (parts / "02-b.js").write_bytes(b"second\n")
            deployed = Path(raw) / "deployed-reader.js"
            deployed.write_bytes(
                b"first\nsecond\n\n;window.__READER_GIT='abc1234';\n"
            )

            self.assertTrue(
                handoff.production_copy_matches(
                    pdf / "reader.js",
                    deployed,
                    entry=ReaderStampEntry(),
                )
            )

    def test_windows_process_lock_retries_only_contention_at_byte_zero(
        self,
    ) -> None:
        calls: list[tuple[int, int, int]] = []
        sleeps: list[float] = []

        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2
            attempts = 0
            stream = None

            @classmethod
            def locking(
                cls,
                _descriptor: int,
                mode: int,
                size: int,
            ) -> None:
                calls.append((mode, size, cls.stream.tell()))
                if mode == cls.LK_NBLCK and cls.attempts == 0:
                    cls.attempts += 1
                    raise OSError(publish.errno.EACCES, "busy")

        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "publisher.lock"
            with lock_path.open("a+b") as handle:
                FakeMsvcrt.stream = handle
                with (
                    mock.patch.object(publish, "msvcrt", FakeMsvcrt),
                    publish.windows_process_lock(
                        handle,
                        sleeper=sleeps.append,
                    ),
                ):
                    self.assertEqual(lock_path.stat().st_size, 0)
            self.assertEqual(
                calls,
                [
                    (FakeMsvcrt.LK_NBLCK, 1, 0),
                    (FakeMsvcrt.LK_NBLCK, 1, 0),
                    (FakeMsvcrt.LK_UNLCK, 1, 0),
                ],
            )
            self.assertEqual(
                sleeps,
                [publish.WINDOWS_LOCK_RETRY_SECONDS],
            )

    def test_windows_process_lock_does_not_retry_foreign_error(self) -> None:
        calls: list[int] = []
        sleeps: list[float] = []

        class FailingMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            @staticmethod
            def locking(
                _descriptor: int,
                mode: int,
                _size: int,
            ) -> None:
                calls.append(mode)
                raise OSError(publish.errno.EIO, "foreign failure")

        with tempfile.TemporaryDirectory() as raw:
            lock_path = Path(raw) / "publisher.lock"
            with (
                lock_path.open("a+b") as handle,
                mock.patch.object(publish, "msvcrt", FailingMsvcrt),
                self.assertRaises(OSError),
                publish.windows_process_lock(
                    handle,
                    sleeper=sleeps.append,
                ),
            ):
                self.fail("non-contention error unexpectedly acquired lock")
        self.assertEqual(calls, [FailingMsvcrt.LK_NBLCK])
        self.assertEqual(sleeps, [])

    @unittest.skipUnless(
        os.name == "nt",
        "Windows kernel byte-range lock contract",
    )
    def test_windows_process_lock_contends_and_releases_on_process_exit(
        self,
    ) -> None:
        holder_code = "\n".join([
            "import pathlib, sys, time",
            f"sys.path.insert(0, {str(HERE)!r})",
            "import publish_test_channel as publish",
            "lock = pathlib.Path(sys.argv[1])",
            "ready = pathlib.Path(sys.argv[2])",
            "with publish.process_lock(lock):",
            "    ready.write_text('ready', encoding='utf-8')",
            "    time.sleep(60)",
        ])
        waiter_code = "\n".join([
            "import pathlib, sys",
            f"sys.path.insert(0, {str(HERE)!r})",
            "import publish_test_channel as publish",
            "lock = pathlib.Path(sys.argv[1])",
            "attempted = pathlib.Path(sys.argv[2])",
            "entered = pathlib.Path(sys.argv[3])",
            "attempted.write_text('attempted', encoding='utf-8')",
            "with publish.process_lock(lock):",
            "    entered.write_text('entered', encoding='utf-8')",
        ])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lock_path = root / "publisher.lock"
            ready = root / "holder-ready"
            attempted = root / "waiter-attempted"
            entered = root / "waiter-entered"
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(lock_path), str(ready)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            waiter = None
            try:
                deadline = time.monotonic() + 10
                while not ready.is_file() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.is_file(), "holder did not acquire lock")
                waiter = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        waiter_code,
                        str(lock_path),
                        str(attempted),
                        str(entered),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.monotonic() + 10
                while (
                    not attempted.is_file()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertTrue(
                    attempted.is_file(),
                    "waiter did not reach lock attempt",
                )
                time.sleep(0.2)
                self.assertFalse(
                    entered.exists(),
                    "waiter entered while holder still owned byte zero",
                )
                holder.kill()
                holder.communicate(timeout=10)
                waiter_stdout, waiter_stderr = waiter.communicate(timeout=10)
                self.assertEqual(
                    waiter.returncode,
                    0,
                    msg=waiter_stdout + waiter_stderr,
                )
                self.assertTrue(entered.is_file())
                self.assertEqual(lock_path.stat().st_size, 0)
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.communicate(timeout=10)
                if waiter is not None and waiter.poll() is None:
                    waiter.kill()
                    waiter.communicate(timeout=10)

    def make_bundle(self, root: Path) -> dict[str, Path | dict]:
        root.mkdir(parents=True, exist_ok=True)
        manifest = release.read_json(HERE / "manifest.json")
        version = str(manifest["version"])
        launcher_version = release.source_launcher_version(HERE)
        package = root / release.package_name(version)
        launcher_archive = root / release.launcher_archive_name(launcher_version)
        launcher_script = root / release.launcher_script_name(launcher_version)
        channel_path = root / release.CHANNEL_FILENAME

        publish.write_deterministic_zip(
            package,
            release.package_source_snapshot(HERE),
        )
        launcher_payload = release.launcher_source_snapshot(HERE)
        publish.write_deterministic_zip(launcher_archive, launcher_payload)
        launcher_script.write_bytes(launcher_payload[release.LAUNCHER_PS1])
        channel = release.make_channel(
            version=version,
            package_sha256=release.sha256_file(package),
            launcher_version=launcher_version,
            launcher_sha256=release.sha256_file(launcher_script),
        )
        channel_path.write_text(
            json.dumps(channel, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "version": version,
            "package": package,
            "launcher_archive": launcher_archive,
            "launcher_script": launcher_script,
            "channel_path": channel_path,
            "channel": channel,
        }

    def audit_bundle(self, bundle: dict[str, Path | dict]) -> dict:
        return release.audit_artifact(
            package_path=bundle["package"],
            channel_path=bundle["channel_path"],
            launcher_script_path=bundle["launcher_script"],
            launcher_archive_path=bundle["launcher_archive"],
            version=bundle["version"],
            source_root=HERE,
        )

    def test_valid_bundle_and_deployed_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.make_bundle(Path(raw))
            self.audit_bundle(bundle)
            deployed = release.audit_deployed_baseline(bundle["channel_path"])
            self.assertEqual(deployed["version"], bundle["version"])

    def test_channel_rejects_untrusted_launcher_and_start_urls(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.make_bundle(Path(raw))
            for field, value in (
                ("launcherUrl", "https://attacker.invalid/payload.ps1"),
                ("url", "https://attacker.invalid/extension.zip"),
                ("startUrl", "https://attacker.invalid/start"),
            ):
                with self.subTest(field=field):
                    altered = copy.deepcopy(bundle["channel"])
                    altered[field] = value
                    bundle["channel_path"].write_text(
                        json.dumps(altered),
                        encoding="utf-8",
                    )
                    with self.assertRaises(SystemExit):
                        self.audit_bundle(bundle)

    def test_source_layout_rejects_symlink_and_extra_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw) / "bw-reader-webext"
            shutil.copytree(
                HERE,
                fixture,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            secret = Path(raw) / "secret.txt"
            secret.write_text("release-secret", encoding="utf-8")
            try:
                os.symlink(secret, fixture / "src" / "review-secret.js")
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest(
                        "Windows 未启用创建符号链接权限；生产门禁逻辑仍由其它平台覆盖"
                    )
                raise
            with self.assertRaises(SystemExit):
                release.validate_source_layout(fixture)

    def test_source_layout_ignores_only_exact_windows_build_caches(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw) / "bw-reader-webext"
            shutil.copytree(
                HERE,
                fixture,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            cache = fixture / "windows" / "__pycache__"
            cache.mkdir()
            (cache / "native_host.cpython-313.pyc").write_bytes(b"cache")
            for generated in ("bin", "obj"):
                generated_root = (
                    fixture
                    / "windows"
                    / "ComputerVoiceAudio"
                    / generated
                    / "Release"
                )
                generated_root.mkdir(parents=True, exist_ok=True)
                (generated_root / "generated.bin").write_bytes(b"generated")
            candidate_root = fixture / "windows" / "candidates" / "0.4.1"
            candidate_root.mkdir(parents=True, exist_ok=True)
            (candidate_root / "candidate.zip").write_bytes(b"generated")
            readerpc_candidate = (
                fixture / "windows" / "readerpc-candidates" / "0.1.1"
            )
            readerpc_candidate.mkdir(parents=True, exist_ok=True)
            (readerpc_candidate / "candidate.zip").write_bytes(b"generated")
            release.validate_source_layout(fixture)

            (fixture / "windows" / "unexpected").mkdir()
            with self.assertRaises(SystemExit):
                release.validate_source_layout(fixture)

            (fixture / "windows" / "unexpected").rmdir()
            foreign_cache = fixture / "windows" / "OtherProject" / "bin"
            foreign_cache.mkdir(parents=True)
            with self.assertRaises(SystemExit):
                release.validate_source_layout(fixture)

        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw) / "bw-reader-webext"
            shutil.copytree(
                HERE,
                fixture,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (fixture / "vendor" / "review-extra.js").write_text(
                "",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                release.validate_source_layout(fixture)

    def test_launcher_zip_rejects_any_extra_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.make_bundle(Path(raw))
            with zipfile.ZipFile(
                bundle["launcher_archive"],
                "a",
                zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr("SURFACE-PEN-CHECKLIST.md", "not a launcher")
            with self.assertRaises(SystemExit):
                self.audit_bundle(bundle)

    def test_launcher_hashing_works_without_get_file_hash_cmdlet(self) -> None:
        source = (
            HERE / "windows" / release.LAUNCHER_PS1
        ).read_text(encoding="utf-8-sig")
        self.assertIn("System.Security.Cryptography.SHA256", source)
        self.assertGreaterEqual(source.count("Get-Sha256Hex"), 3)
        self.assertNotIn("Get-FileHash", source)

    def test_launcher_stops_only_dedicated_profile_before_extension_swap(
        self,
    ) -> None:
        source = (
            HERE / "windows" / release.LAUNCHER_PS1
        ).read_text(encoding="utf-8-sig")
        self.assertIn(
            "function Stop-TestProfileBrowserForUpdate",
            source,
        )
        self.assertIn(
            "[System.IO.Path]::GetFullPath($profileDir)",
            source,
        )
        self.assertIn("$_.CommandLine.ToLowerInvariant().Contains($profileNeedle)", source)
        self.assertIn("($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe')", source)
        self.assertIn(
            "Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue",
            source,
        )
        stop = source.index("Stop-TestProfileBrowserForUpdate")
        swap = source.index(
            "Move-Item -LiteralPath $extensionDir -Destination $backupDir"
        )
        self.assertLess(stop, swap)
        self.assertIn(
            "The extension was not replaced.",
            source,
        )
        self.assertIn("function Reload-BwExtensionWorker", source)
        self.assertIn("--remote-debugging-address=127.0.0.1", source)
        self.assertIn("--remote-debugging-port=9222", source)
        self.assertIn(
            "--remote-allow-origins=http://127.0.0.1:9222",
            source,
        )
        self.assertIn(
            "'http://127.0.0.1:9222'",
            source,
        )
        self.assertIn(
            "globalThis.__BW_READER_BACKGROUND_BUILD_VERSION",
            source,
        )
        self.assertIn(
            "'chrome-extension://' + $extensionId + '/popup.html'",
            source,
        )
        self.assertIn(
            "'chrome-extension://' + $extensionId + '/background.js'",
            source,
        )
        self.assertIn("function New-DevToolsTarget", source)
        self.assertIn("http://127.0.0.1:9222/json/new?", source)
        self.assertIn("[Uri]::EscapeDataString($Url)", source)
        self.assertIn(
            "foreach ($target in $response) { Write-Output $target }",
            source,
        )
        self.assertIn("chrome.runtime.reload(); true", source)
        self.assertIn("worker-runtime-version.txt", source)
        self.assertIn(
            "its worker reload is pending",
            source,
        )
        self.assertIn(
            "if ($swapped -and (Test-Path -LiteralPath $extensionDir))",
            source,
        )
        self.assertNotIn(
            "Default\\Service Worker\\CacheStorage",
            source,
        )
        self.assertNotIn(
            "Default\\IndexedDB",
            source,
        )
        self.assertNotIn(
            "Default\\Service Worker\\ScriptCache",
            source,
        )

    def test_background_build_marker_matches_manifest_version(self) -> None:
        manifest = release.read_json(HERE / "manifest.json")
        source = (HERE / "background.js").read_text(encoding="utf-8")
        marker = (
            'globalThis.__BW_READER_BACKGROUND_BUILD_VERSION = "'
            + str(manifest["version"])
            + '";'
        )
        self.assertIn(marker, source)

    def test_manifest_rejects_inserted_runtime_script(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw) / "extensions" / "bw-reader-webext"
            fixture.parent.mkdir(parents=True)
            # 只复制 audit_manifest 需要的源码;windows/candidates 与 readerpc-candidates 里是几 GB 的
            # 发布 zip(2026-09-04 实锤:整目录 copytree 让这一个用例跑十几分钟,handoff 看起来像挂了)。
            shutil.copytree(
                HERE,
                fixture,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "candidates", "readerpc-candidates", "*.zip",
                    "node_modules", "dist", "build",
                ),
            )
            extra = fixture / "src" / "review-extra.js"
            extra.write_text("", encoding="utf-8")
            manifest_path = fixture / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["content_scripts"][1]["js"].insert(
                2,
                "src/review-extra.js",
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            spec = importlib.util.spec_from_file_location(
                "handoff_check_fixture",
                fixture / "handoff_check.py",
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            audit = module.Audit()
            module.audit_manifest(audit)
            self.assertTrue(
                any("逐项同序一致" in error for error in audit.errors),
                audit.errors,
            )

    def test_launcher_content_change_requires_version_bump(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.make_bundle(Path(raw))
            deployed = copy.deepcopy(bundle["channel"])
            candidate = copy.deepcopy(bundle["channel"])
            candidate["launcherSha256"] = "f" * 64
            with self.assertRaises(SystemExit):
                release.validate_launcher_upgrade(
                    candidate=candidate,
                    deployed=deployed,
                )

    def test_launcher_cmd_change_requires_version_bump(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bundle = self.make_bundle(Path(raw))
            deployed = copy.deepcopy(bundle["channel"])
            candidate = copy.deepcopy(bundle["channel"])
            payload = release.launcher_source_snapshot(HERE)
            deployed["_launcherPayloadSha256"] = release.payload_sha256(payload)
            changed = dict(payload)
            changed[release.LAUNCHER_FILES[0]] += b"\r\nrem changed\r\n"
            candidate["_launcherPayloadSha256"] = release.payload_sha256(changed)
            with self.assertRaises(SystemExit):
                release.validate_launcher_upgrade(
                    candidate=candidate,
                    deployed=deployed,
                )

    def test_versioned_publish_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "candidate.zip"
            target = root / "deployed" / "candidate.zip"
            source.write_bytes(b"candidate")
            target.parent.mkdir()
            target.write_bytes(b"orphaned-other-build")
            with self.assertRaises(SystemExit):
                publish.immutable_copy(source, target)
            self.assertEqual(target.read_bytes(), b"orphaned-other-build")

    def test_publish_backs_up_existing_channel_before_any_production_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = self.make_bundle(root / "candidate")
            deploy = root / "deploy"
            deploy.mkdir()
            channel_target = deploy / release.CHANNEL_FILENAME
            original = b'{"previous":"exact bytes"}\n'
            channel_target.write_bytes(original)
            backup_root = root / "backups"
            observed_backups: list[Path] = []

            def inspect_then_fail(source: Path, target: Path) -> None:
                directories = list(backup_root.iterdir())
                self.assertEqual(len(directories), 1)
                backup_dir = directories[0]
                record = json.loads(
                    (backup_dir / "channel-deploy.json").read_text(
                        encoding="utf-8",
                    )
                )
                self.assertEqual(record["status"], "prepared")
                self.assertEqual(record["target"], str(channel_target))
                self.assertEqual(record["original"]["state"], "present")
                self.assertEqual(
                    (backup_dir / "channel.before").read_bytes(),
                    original,
                )
                observed_backups.append(backup_dir)
                raise OSError("injected immutable publication failure")

            with mock.patch.object(
                publish,
                "immutable_copy",
                side_effect=inspect_then_fail,
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    publish.publish_candidate(
                        {
                            "version": bundle["version"],
                            "package": bundle["package"],
                            "launcher_archive": bundle["launcher_archive"],
                            "launcher_script": bundle["launcher_script"],
                            "channel": bundle["channel_path"],
                        },
                        deploy_root=deploy,
                        backup_root=backup_root,
                    )
            self.assertTrue(observed_backups)
            self.assertEqual(channel_target.read_bytes(), original)
            record = json.loads(
                (observed_backups[0] / "channel-deploy.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(record["status"], "rolled-back")
            self.assertTrue(record["rollback"]["verified"])
            self.assertEqual(record["rollback"]["result"], "restored")

    def test_publish_restores_existing_channel_after_post_replace_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = self.make_bundle(root / "candidate")
            deploy = root / "deploy"
            deploy.mkdir()
            target = deploy / release.CHANNEL_FILENAME
            original = b'{"old-channel":"byte-for-byte"}\n'
            target.write_bytes(original)
            original_atomic_copy = publish.atomic_copy
            injected = False

            def replace_then_fail(source: Path, destination: Path) -> None:
                nonlocal injected
                original_atomic_copy(source, destination)
                if destination == target and not injected:
                    injected = True
                    raise OSError("injected post-replace failure")

            with mock.patch.object(
                publish,
                "atomic_copy",
                side_effect=replace_then_fail,
            ):
                with self.assertRaisesRegex(OSError, "post-replace"):
                    publish.publish_candidate(
                        {
                            "version": bundle["version"],
                            "package": bundle["package"],
                            "launcher_archive": bundle["launcher_archive"],
                            "launcher_script": bundle["launcher_script"],
                            "channel": bundle["channel_path"],
                        },
                        deploy_root=deploy,
                        backup_root=root / "backups",
                    )

            self.assertEqual(target.read_bytes(), original)
            backup_dir = next((root / "backups").iterdir())
            record = json.loads(
                (backup_dir / "channel-deploy.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(record["status"], "rolled-back")
            self.assertEqual(
                record["original"]["sha256"],
                release.sha256_bytes(original),
            )
            self.assertTrue(record["rollback"]["verified"])
            for key in ("package", "launcher_archive", "launcher_script"):
                source = bundle[key]
                self.assertEqual(
                    (deploy / source.name).read_bytes(),
                    source.read_bytes(),
                )

    def test_publish_removes_channel_when_original_was_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = self.make_bundle(root / "candidate")
            deploy = root / "deploy"
            deploy.mkdir()
            target = deploy / release.CHANNEL_FILENAME
            original_atomic_copy = publish.atomic_copy
            injected = False

            def replace_then_fail(source: Path, destination: Path) -> None:
                nonlocal injected
                original_atomic_copy(source, destination)
                if destination == target and not injected:
                    injected = True
                    raise OSError("injected missing-baseline failure")

            with mock.patch.object(
                publish,
                "atomic_copy",
                side_effect=replace_then_fail,
            ):
                with self.assertRaisesRegex(OSError, "missing-baseline"):
                    publish.publish_candidate(
                        {
                            "version": bundle["version"],
                            "package": bundle["package"],
                            "launcher_archive": bundle["launcher_archive"],
                            "launcher_script": bundle["launcher_script"],
                            "channel": bundle["channel_path"],
                        },
                        deploy_root=deploy,
                        backup_root=root / "backups",
                    )

            self.assertFalse(target.exists())
            backup_dir = next((root / "backups").iterdir())
            record = json.loads(
                (backup_dir / "channel-deploy.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertEqual(record["original"]["state"], "missing")
            self.assertEqual(record["rollback"]["result"], "removed")
            self.assertTrue(record["rollback"]["verified"])

    def test_successful_publish_keeps_immutable_assets_and_audit_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle = self.make_bundle(root / "candidate")
            deploy = root / "deploy"
            deploy.mkdir()
            target = deploy / release.CHANNEL_FILENAME
            original = b'{"version":"old"}\n'
            target.write_bytes(original)
            backup = publish.publish_candidate(
                {
                    "version": bundle["version"],
                    "package": bundle["package"],
                    "launcher_archive": bundle["launcher_archive"],
                    "launcher_script": bundle["launcher_script"],
                    "channel": bundle["channel_path"],
                },
                deploy_root=deploy,
                backup_root=root / "backups",
            )

            self.assertEqual(
                target.read_bytes(),
                bundle["channel_path"].read_bytes(),
            )
            for key in ("package", "launcher_archive", "launcher_script"):
                source = bundle[key]
                self.assertEqual(
                    (deploy / source.name).read_bytes(),
                    source.read_bytes(),
                )
            self.assertEqual(
                backup.payload_path.read_bytes(),
                original,
            )
            record = json.loads(
                backup.record_path.read_text(encoding="utf-8")
            )
            self.assertEqual(record["status"], "committed")
            self.assertEqual(
                record["activated"]["sha256"],
                release.sha256_file(bundle["channel_path"]),
            )
            self.assertFalse(record["rollback"]["attempted"])

    def test_release_preflight_requires_artifact_argument(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HERE / "release_preflight.py")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            # 无超时的子进程会让整条默认档悬挂(2026-09-02 一天两次卡在本文件,
            # 人肉等了近一小时)。这里预期立即以 2 退出,120s 是宽裕上限。
            timeout=120,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--artifact", result.stderr)
        self.assertNotIn("READY:", result.stdout)

    def test_safari_compat_loads_document_notes_after_storage_dependencies(
        self,
    ) -> None:
        manifest = safari.safari_manifest(compat=True)
        safari.validate(manifest, compat=True)
        scripts = manifest["background"]["scripts"]
        self.assertEqual(scripts, list(safari.BACKGROUND_SCRIPTS))
        repository = (
            "vendor/reader-runtime-document-note-repository.js"
        )
        self.assertLess(
            scripts.index("vendor/reader-runtime-data-store.js"),
            scripts.index(repository),
        )
        self.assertLess(
            scripts.index("vendor/reader-runtime-indexeddb-store.js"),
            scripts.index(repository),
        )
        self.assertLess(
            scripts.index("vendor/reader-runtime-data-registry.js"),
            scripts.index(repository),
        )
        self.assertLess(
            scripts.index("vendor/reader-runtime-data-registry.js"),
            scripts.index("vendor/reader-runtime-sync-owner-lease.js"),
        )
        self.assertLess(
            scripts.index("vendor/reader-runtime-sync-owner-lease.js"),
            scripts.index("vendor/reader-runtime-sync-gateway.js"),
        )
        self.assertLess(scripts.index(repository), scripts.index("background.js"))

    def test_safari_standard_and_compat_packages_include_note_repository(
        self,
    ) -> None:
        repository = (
            "vendor/reader-runtime-document-note-repository.js"
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for compat in (False, True):
                with self.subTest(compat=compat):
                    manifest = safari.safari_manifest(compat=compat)
                    package = root / (
                        "safari-compat.zip" if compat else "safari-standard.zip"
                    )
                    safari.write_package(
                        package,
                        manifest,
                        compat=compat,
                    )
                    with zipfile.ZipFile(package) as archive:
                        names = set(archive.namelist())
                        packed_manifest = json.loads(
                            archive.read("manifest.json")
                        )
                    self.assertIn(repository, names)
                    self.assertEqual(packed_manifest, manifest)
                    if compat:
                        self.assertEqual(
                            packed_manifest["background"]["scripts"],
                            list(safari.BACKGROUND_SCRIPTS),
                        )
                    else:
                        self.assertEqual(
                            packed_manifest["background"],
                            {"service_worker": "background.js"},
                        )

    def test_safari_package_rejects_missing_implicit_worker_dependency(
        self,
    ) -> None:
        repository = (
            "vendor/reader-runtime-document-note-repository.js"
        )
        manifest = safari.safari_manifest()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid = root / "valid.zip"
            broken = root / "broken.zip"
            safari.write_package(valid, manifest)
            with (
                zipfile.ZipFile(valid) as source,
                zipfile.ZipFile(
                    broken,
                    "w",
                    zipfile.ZIP_DEFLATED,
                ) as target,
            ):
                for info in source.infolist():
                    if info.filename != repository:
                        target.writestr(info, source.read(info.filename))
            with self.assertRaisesRegex(
                SystemExit,
                "missing runtime resources",
            ):
                safari.validate_package(broken, manifest)

    def test_safari_manifest_rejects_missing_direct_content_host(self) -> None:
        manifest = safari.safari_manifest()
        scripts = manifest["content_scripts"][1]["js"]
        scripts.remove("src/direct-sync-content-host.js")
        with self.assertRaisesRegex(
            SystemExit,
            "exactly match",
        ):
            safari.validate(manifest)


if __name__ == "__main__":
    unittest.main()
