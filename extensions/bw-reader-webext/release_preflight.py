#!/usr/bin/env python3
"""Fail-closed gate for a Windows test-channel deployment.

The gate validates one immutable candidate bundle: extension ZIP, channel JSON,
versioned PowerShell launcher, and launcher ZIP.  It also re-reads the deployed
channel while the publisher holds its process lock, so the version comparison is
made immediately before publication.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit
import zipfile


HERE = Path(__file__).resolve().parent
EXTENSIONS = HERE.parent
DEPLOY_ROOT = Path("/var/www/html/static/pdf")
CHANNEL_FILENAME = "bw-reader-webext-test-channel.json"
DEFAULT_DEPLOYED_CHANNEL = DEPLOY_ROOT / CHANNEL_FILENAME

OFFICIAL_ORIGIN = "https://bwicarus.taile44d0c.ts.net"
OFFICIAL_STATIC_PATH = "/static/pdf"
OFFICIAL_STATIC_URL = OFFICIAL_ORIGIN + OFFICIAL_STATIC_PATH
CHANNEL_SCHEMA = 1
WEB_TEST_URL = "https://en.wikipedia.org/wiki/Reading"
LAUNCHER_VERSION = 12

ROOT_FILES = (
    "manifest.json",
    "background.js",
    "content.js",
    "offscreen.html",
    "offscreen.js",
    "popup.html",
    "popup.js",
)
SRC_FILES = (
    "bw-probe.js",
    "browser-control.js",
    "computer-voice-native-protocol.js",
    "direct-sync-content-host.js",
    "facade.js",
    "pwa-adapter.js",
    "pwa-marker.js",
    "settings-sync.js",
    "shell.js",
    "web-adapter.js",
    "web-decorations.js",
    "web-highlights.js",
    "web-ink.js",
    "web-notes.js",
    "web-pins.js",
)
ICON_FILES = (
    "icon-48-opaque.png",
    "icon-64-opaque.png",
    "icon-96-opaque.png",
    "icon-128-opaque.png",
    "icon-256-opaque.png",
    "icon-512.png",
    "icon-512-opaque.png",
    "icon-1024-safari.png",
)
LAUNCHER_FILES = ("BW扩展测试.cmd", "BW扩展测试.ps1")
WINDOWS_SOURCE_FILES = (
    *LAUNCHER_FILES,
    "SURFACE-PEN-CHECKLIST.md",
    "bw-computer-voice-preflight.ps1",
    "bw_computer_voice_supervisor.py",
    "bw_computer_voice_typist_helper.py",
    "computer-voice.config.example.json",
    "install-computer-voice-native-host.ps1",
    "test_bw_computer_voice_typist_helper.py",
    "test_computer_voice_native_host.py",
    "ComputerVoiceAudio/.gitignore",
    "ComputerVoiceAudio/AudioBridgeContract.cs",
    "ComputerVoiceAudio/ChatGptClassicVoiceAutomation.cs",
    "ComputerVoiceAudio/CodexVoiceActivity.cs",
    "ComputerVoiceAudio/CodexVoiceActivitySelfTest.cs",
    "ComputerVoiceAudio/CodexVoiceHistory.cs",
    "ComputerVoiceAudio/CodexVoiceHistorySelfTest.cs",
    "ComputerVoiceAudio/CodexVoiceShortcutBroker.cs",
    "ComputerVoiceAudio/ComputerVoiceAudio.csproj",
    "ComputerVoiceAudio/ContractSelfTest.cs",
    "ComputerVoiceAudio/DirectAudioDiagnostics.cs",
    "ComputerVoiceAudio/DirectAppTargetProfile.cs",
    "ComputerVoiceAudio/DirectBridgeAdapters.cs",
    "ComputerVoiceAudio/DirectBridgeConfig.cs",
    "ComputerVoiceAudio/DirectBridgeContract.cs",
    "ComputerVoiceAudio/DirectBridgeProtocol.cs",
    "ComputerVoiceAudio/DirectBridgeSelfTest.cs",
    "ComputerVoiceAudio/DirectBridgeServer.cs",
    "ComputerVoiceAudio/DirectConnectionPhaseDeadline.cs",
    "ComputerVoiceAudio/DirectContextBridge.cs",
    "ComputerVoiceAudio/DirectContextSnapshot.cs",
    "ComputerVoiceAudio/DirectMicrophoneDiscovery.cs",
    "ComputerVoiceAudio/DirectOutputCaptureSession.cs",
    "ComputerVoiceAudio/DirectOutputRouteObserver.cs",
    "ComputerVoiceAudio/DirectPcmFrame.cs",
    "ComputerVoiceAudio/DirectRuntimeStatus.cs",
    "ComputerVoiceAudio/DirectServiceLease.cs",
    "ComputerVoiceAudio/DirectSnapshotPresentation.cs",
    "ComputerVoiceAudio/ExplicitMicrophoneCaptureSession.cs",
    "ComputerVoiceAudio/Interop/ExplicitMicrophoneInterop.cs",
    "ComputerVoiceAudio/Interop/ProcessLoopbackInterop.cs",
    "ComputerVoiceAudio/NativeHostConfig.cs",
    "ComputerVoiceAudio/NativeMessagingHost.cs",
    "ComputerVoiceAudio/Pcm48kMonoFramer.cs",
    "ComputerVoiceAudio/PcmCaptureContract.cs",
    "ComputerVoiceAudio/PerAppAudioRoute.cs",
    "ComputerVoiceAudio/PerAppAudioRouteProbe.cs",
    "ComputerVoiceAudio/PerAppAudioRouteSelfTest.cs",
    "ComputerVoiceAudio/ProcessLoopbackActivation.cs",
    "ComputerVoiceAudio/ProcessLoopbackCaptureSession.cs",
    "ComputerVoiceAudio/Program.cs",
    "ComputerVoiceAudio/ReaderCapabilities/cards.md",
    "ComputerVoiceAudio/ReaderCapabilities/command-format.md",
    "ComputerVoiceAudio/ReaderCapabilities/conversation.md",
    "ComputerVoiceAudio/ReaderCapabilities/get.md",
    "ComputerVoiceAudio/ReaderCapabilities/highlight.md",
    "ComputerVoiceAudio/ReaderCapabilities/index.md",
    "ComputerVoiceAudio/ReaderCapabilities/navigation.md",
    "ComputerVoiceAudio/ReaderCapabilities/tool-status.md",
    "ComputerVoiceAudio/ReaderCapabilityCatalog.cs",
    "ComputerVoiceAudio/ReaderContextMcpServer.cs",
    "ComputerVoiceAudio/ReaderBrowserControl.cs",
    "ComputerVoiceAudio/ReaderBrowserControlRpc.cs",
    "ComputerVoiceAudio/ReaderContextReadLedger.cs",
    "ComputerVoiceAudio/ReaderDocumentCorpus.cs",
    "ComputerVoiceAudio/ReaderRealtimeOutput.cs",
    "ComputerVoiceAudio/ReaderRealtimeOutputRpc.cs",
    "ComputerVoiceAudio/ReaderVisualDelivery.cs",
    "ComputerVoiceAudio/ReaderVisualRpc.cs",
    "ComputerVoiceAudio/README.md",
    "ComputerVoiceAudio/SharedEventDrivenPcmRuntime.cs",
    "ComputerVoiceAudio/VirtualMicrophoneRenderSession.cs",
    "ComputerVoiceAudio/WindowsCodexAppProbe.cs",
    "ComputerVoiceAudio/WindowsDirectAdapters.cs",
    "ComputerVoiceAudio/computer-voice-direct.config.example.json",
    "ComputerVoiceAudio/computer-voice-native.config.example.json",
    "computer-voice-desktop/README.md",
    "computer-voice-desktop/bridge_core.py",
    "computer-voice-desktop/computer-voice-direct.config.example.json",
    "computer-voice-desktop/control_plane.py",
    "computer-voice-desktop/desktop_launcher.py",
    "computer-voice-desktop/readerpc_launcher.py",
    "computer-voice-desktop/readerpc_services.py",
    "computer-voice-desktop/sidebar_bridge_client.py",
    "computer-voice-desktop/tests/test_bridge_core.py",
    "computer-voice-desktop/tests/test_control_plane.py",
    "computer-voice-desktop/tests/test_desktop_launcher.py",
    "computer-voice-desktop/tests/test_readerpc_launcher.py",
    "computer-voice-desktop/tests/test_readerpc_services.py",
    "computer-voice-desktop/voice_history_sidebar_sync.py",
    "package_computer_voice_direct.py",
    "package_readerpc_server.py",
    "test_computer_voice_direct_package.py",
    "test_readerpc_server_package.py",
    "typist-runtime/typist_ipc.py",
    "typist-runtime/voice_typist.py",
    "typist-runtime/voice-typist-launcher.ps1",
    "typist-runtime/tests/test_typist_ipc.py",
    "typist-runtime/tests/test_voice_typist_direct_runtime.py",
)
LAUNCHER_PS1 = "BW扩展测试.ps1"
LAUNCHER_CHANNEL_BASENAME = "bw-reader-extension-test"
LEGACY_LAUNCHER_SCRIPT_NAME = "bw-reader-extension-test.ps1"

CHANNEL_FIELDS = {
    "schema",
    "version",
    "url",
    "sha256",
    "startUrl",
    "launcherVersion",
    "launcherUrl",
    "launcherSha256",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
LAUNCHER_VERSION_RE = re.compile(
    r"(?m)^\s*\$launcherVersion\s*=\s*(\d+)\s*(?:#.*)?$"
)


def fail(message: str) -> None:
    raise SystemExit(f"BLOCKED: {message}")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_regular_file(path).decode("utf-8"))
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"无法读取 {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"{path} 顶层必须是 JSON object")
    return value


def version_tuple(value: object) -> tuple[int, ...]:
    text = str(value)
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,3}", text):
        fail(f"扩展版本 {text!r} 不是 Chrome MV3 的 1–4 段数字版本")
    parts = tuple(int(part) for part in text.split("."))
    if not any(parts) or any(part > 65535 for part in parts):
        fail(f"扩展版本 {text!r} 超出 Chrome MV3 数字范围")
    return parts


def compare_version(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    a = left + (0,) * (width - len(left))
    b = right + (0,) * (width - len(right))
    return (a > b) - (a < b)


def read_regular_file(path: Path) -> bytes:
    """Read a regular file without following a final-component symlink."""

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        fail(f"无法安全打开普通文件 {path}: {exc}")
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            fail(f"不是普通文件: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(read_regular_file(path))


def load_build_module(source_root: Path = HERE):
    path = source_root / "build.py"
    spec = importlib.util.spec_from_file_location(
        f"bw_webext_build_contract_{hash(path)}",
        path,
    )
    if not spec or not spec.loader:
        fail(f"无法加载构建清单: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        fail(f"无法加载构建清单 {path}: {exc}")
    return module


def expected_vendor_files(source_root: Path = HERE) -> tuple[str, ...]:
    build = load_build_module(source_root)
    try:
        names = (
            list(build.LIBS.values())
            + list(build.GUARDED_LIBS.values())
            + list(build.FILES)
        )
    except Exception as exc:
        fail(f"build.py 的 vendor 清单无法读取: {exc}")
    if (
        not names
        or len(names) != len(set(names))
        or any(
            not isinstance(name, str)
            or PurePosixPath(name).name != name
            or not name.endswith(".js")
            for name in names
        )
    ):
        fail("build.py 的 vendor 输出必须是唯一的根级 .js 文件名")
    return tuple(sorted(names))


def expected_runtime_js(source_root: Path = HERE) -> tuple[str, ...]:
    """Return the exact content-script order derived from build.py."""

    build = load_build_module(source_root)
    wrapped = list(build.FILES)
    if wrapped.count("rc-stickynote.js") != 1:
        fail("build.py FILES 必须且只能包含一个 rc-stickynote.js")
    wrapped.remove("rc-stickynote.js")
    guarded = list(build.GUARDED_LIBS.values())
    if guarded != ["rc-ink.js", "web-immersive.js"]:
        fail("build.py GUARDED_LIBS 顺序必须是 rc-ink.js → web-immersive.js")
    libraries = set(build.LIBS.values())
    required_libraries = {
        "html2canvas.min.js",
        "mathjax-full.js",
        "marked.js",
        "reader-runtime-context-selection-registry.js",
        "reader-runtime-computer-voice-webrtc.js",
        "reader-runtime-card-improvement-actions.js",
        "reader-runtime-data-registry.js",
        "reader-runtime-sync-owner-lease.js",
        "reader-runtime-direct-sync-host.js",
        "reader-runtime-direct-sync-protocol.js",
        "reader-runtime-direct-sync-signal-transport.js",
        "reader-runtime-interaction-policy.js",
        "reader-runtime-sync-gateway.js",
        "reader-runtime-vocabulary-state.js",
    }
    if not required_libraries.issubset(libraries):
        fail(
            "build.py LIBS 缺少内容脚本依赖: "
            + "、".join(sorted(required_libraries - libraries))
        )
    if not wrapped or wrapped[0] != "rc-core.js":
        fail("build.py FILES 首项必须是 rc-core.js")
    wrapped_after_core = wrapped[1:]
    return (
        "src/bw-probe.js",
        "src/facade.js",
        "src/settings-sync.js",
        "src/browser-control.js",
        "vendor/html2canvas.min.js",
        "vendor/mathjax-full.js",
        "vendor/marked.js",
        # The selection registry is a dependency of rc-voicecall but does not
        # depend on RC.  Card-improvement actions use RC endpoints, so place
        # them immediately after the shared core and before their consumers.
        "vendor/reader-runtime-context-selection-registry.js",
        "vendor/reader-runtime-interaction-policy.js",
        "vendor/reader-runtime-vocabulary-state.js",
        "vendor/reader-runtime-data-registry.js",
        "vendor/reader-runtime-sync-gateway.js",
        "vendor/reader-runtime-direct-sync-protocol.js",
        "vendor/reader-runtime-direct-sync-signal-transport.js",
        "vendor/reader-runtime-direct-sync-host.js",
        "vendor/reader-runtime-computer-voice-webrtc.js",
        "vendor/rc-core.js",
        "vendor/reader-runtime-card-improvement-actions.js",
        *(f"vendor/{name}" for name in wrapped_after_core),
        "vendor/rc-ink.js",
        "vendor/rc-stickynote.js",
        "vendor/web-immersive.js",
        "src/pwa-adapter.js",
        "src/web-adapter.js",
        "src/web-decorations.js",
        "src/web-highlights.js",
        "src/web-pins.js",
        "src/web-notes.js",
        "src/web-ink.js",
        "src/shell.js",
        "src/direct-sync-content-host.js",
        "content.js",
    )


def _audit_exact_directory(
    root: Path,
    expected_names: tuple[str, ...],
    *,
    label: str,
    ignored_directory_names: frozenset[str] = frozenset(),
    ignored_relative_directories: frozenset[str] = frozenset(),
) -> None:
    if root.is_symlink() or not root.is_dir():
        fail(f"{label} 目录不存在或是符号链接: {root}")
    expected = set(expected_names)
    if len(expected) != len(expected_names):
        fail(f"{label} 白名单包含重复项")
    discovered: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            fail(f"{label} 不允许符号链接: {relative}")
        if any(
            relative == ignored
            or relative.startswith(ignored + "/")
            for ignored in ignored_relative_directories
        ):
            continue
        if any(part in ignored_directory_names for part in path.relative_to(root).parts):
            continue
        if path.is_dir():
            if not any(name.startswith(relative + "/") for name in expected):
                fail(f"{label} 存在多余目录: {relative}")
            continue
        if not path.is_file():
            fail(f"{label} 存在非普通文件: {relative}")
        discovered.add(relative)
    missing = sorted(expected - discovered)
    extra = sorted(discovered - expected)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("缺少 " + ", ".join(missing[:8]))
        if extra:
            detail.append("多出 " + ", ".join(extra[:8]))
        fail(f"{label} 不等于精确白名单：" + "；".join(detail))


def validate_source_layout(source_root: Path = HERE) -> None:
    for name in ROOT_FILES:
        path = source_root / name
        if path.is_symlink():
            fail(f"根文件不允许符号链接: {name}")
        read_regular_file(path)
    _audit_exact_directory(source_root / "src", SRC_FILES, label="src")
    _audit_exact_directory(
        source_root / "vendor",
        expected_vendor_files(source_root),
        label="vendor",
    )
    _audit_exact_directory(source_root / "icons", ICON_FILES, label="icons")
    _audit_exact_directory(
        source_root / "windows",
        WINDOWS_SOURCE_FILES,
        label="windows",
        # Running the checked-in Python smoke test creates this interpreter
        # cache before the release audit.  It is never packaged and must not
        # turn a successful validation run into a false source-layout failure.
        ignored_directory_names=frozenset({"__pycache__"}),
        # The direct C# self-test is required before release auditing and
        # creates only these two project-local build trees.  The standalone
        # direct-bridge packager likewise writes only beneath the fixed
        # candidates roots.  Keep every exemption path-exact so an unrelated
        # build or candidate directory still fails closed and no generated
        # file can enter the source snapshot.
        ignored_relative_directories=frozenset({
            "ComputerVoiceAudio/bin",
            "ComputerVoiceAudio/obj",
            "candidates",
            "readerpc-candidates",
        }),
    )
    if any(not name.endswith(".js") for name in (*SRC_FILES, *expected_vendor_files(source_root))):
        fail("src/vendor 白名单只能包含 .js")
    if any(not name.endswith(".png") for name in ICON_FILES):
        fail("icons 白名单只能包含 .png")


def package_source_snapshot(source_root: Path = HERE) -> dict[str, bytes]:
    validate_source_layout(source_root)
    relative_names = [
        *ROOT_FILES,
        *(f"src/{name}" for name in SRC_FILES),
        *(f"vendor/{name}" for name in expected_vendor_files(source_root)),
        *(f"icons/{name}" for name in ICON_FILES),
    ]
    return {
        name: read_regular_file(source_root / PurePosixPath(name))
        for name in sorted(relative_names)
    }


def launcher_source_snapshot(source_root: Path = HERE) -> dict[str, bytes]:
    validate_source_layout(source_root)
    return {
        name: read_regular_file(source_root / "windows" / name)
        for name in LAUNCHER_FILES
    }


def parse_launcher_version(value: bytes, *, label: str) -> int:
    try:
        text = value.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        fail(f"{label} 不是 UTF-8 PowerShell 脚本: {exc}")
    matches = LAUNCHER_VERSION_RE.findall(text)
    if len(matches) != 1:
        fail(f"{label} 必须且只能声明一个 $launcherVersion")
    version = int(matches[0])
    if version <= 0 or version > 65535:
        fail(f"{label} 的 launcher version 超出范围")
    return version


def source_launcher_version(source_root: Path = HERE) -> int:
    source = read_regular_file(source_root / "windows" / LAUNCHER_PS1)
    parsed = parse_launcher_version(source, label="源码 launcher")
    if parsed != LAUNCHER_VERSION:
        fail(
            "Python/PowerShell launcher version 不一致: "
            f"{LAUNCHER_VERSION} != {parsed}"
        )
    return parsed


def package_name(version: str) -> str:
    version_tuple(version)
    return f"bw-reader-webext-{version}-windows-test.zip"


def launcher_script_name(version: int) -> str:
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        fail(f"launcherVersion 无效: {version!r}")
    return f"{LAUNCHER_CHANNEL_BASENAME}-v{version}.ps1"


def launcher_archive_name(version: int) -> str:
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        fail(f"launcherVersion 无效: {version!r}")
    return f"bw-reader-webext-test-channel-windows-v{version}.zip"


def official_url(filename: str) -> str:
    if PurePosixPath(filename).name != filename or not filename:
        fail(f"公开文件名不安全: {filename!r}")
    return f"{OFFICIAL_STATIC_URL}/{filename}"


def _audit_url(value: object, expected: str, *, field: str) -> None:
    if not isinstance(value, str) or value != expected:
        fail(f"channel {field} 必须精确等于 {expected}")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != urlsplit(OFFICIAL_ORIGIN).netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        fail(f"channel {field} 必须位于官方 Tailscale HTTPS origin")


def validate_channel_metadata(
    channel: dict[str, Any],
    *,
    version: str,
    launcher_version: int,
    allow_legacy_launcher_url: bool = False,
) -> None:
    if set(channel) != CHANNEL_FIELDS:
        missing = sorted(CHANNEL_FIELDS - set(channel))
        extra = sorted(set(channel) - CHANNEL_FIELDS)
        fail(f"channel 字段不精确；缺少={missing} 多出={extra}")
    if type(channel.get("schema")) is not int or channel["schema"] != CHANNEL_SCHEMA:
        fail(f"channel schema 必须是整数 {CHANNEL_SCHEMA}")
    if not isinstance(channel.get("version"), str) or channel["version"] != version:
        fail("channel 版本与 manifest 不一致")
    version_tuple(channel["version"])
    if channel.get("startUrl") != WEB_TEST_URL:
        fail(f"channel startUrl 必须精确等于 {WEB_TEST_URL}")
    if (
        type(channel.get("launcherVersion")) is not int
        or channel["launcherVersion"] != launcher_version
    ):
        fail("channel launcherVersion 与 PowerShell launcher 不一致")
    for field in ("sha256", "launcherSha256"):
        value = channel.get(field)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            fail(f"channel {field} 必须是小写 SHA-256")
    _audit_url(
        channel.get("url"),
        official_url(package_name(version)),
        field="url",
    )
    expected_launcher_url = official_url(launcher_script_name(launcher_version))
    if (
        allow_legacy_launcher_url
        and channel.get("launcherUrl") == official_url(LEGACY_LAUNCHER_SCRIPT_NAME)
    ):
        _audit_url(
            channel.get("launcherUrl"),
            official_url(LEGACY_LAUNCHER_SCRIPT_NAME),
            field="launcherUrl",
        )
    else:
        _audit_url(
            channel.get("launcherUrl"),
            expected_launcher_url,
            field="launcherUrl",
        )


def make_channel(
    *,
    version: str,
    package_sha256: str,
    launcher_version: int,
    launcher_sha256: str,
) -> dict[str, Any]:
    channel = {
        "schema": CHANNEL_SCHEMA,
        "version": version,
        "url": official_url(package_name(version)),
        "sha256": package_sha256,
        "startUrl": WEB_TEST_URL,
        "launcherVersion": launcher_version,
        "launcherUrl": official_url(launcher_script_name(launcher_version)),
        "launcherSha256": launcher_sha256,
    }
    validate_channel_metadata(
        channel,
        version=version,
        launcher_version=launcher_version,
    )
    return channel


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(
        name
        and not name.startswith("/")
        and "\\" not in name
        and "\x00" not in name
        and path.as_posix() == name
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _zip_payload(path: Path, *, label: str) -> dict[str, bytes]:
    if path.is_symlink():
        fail(f"{label} 不允许是符号链接: {path}")
    read_regular_file(path)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        fail(f"{label} 无法读取: {exc}")
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            fail(f"{label} 内存在重复路径")
        if len({name.casefold() for name in names}) != len(names):
            fail(f"{label} 内存在 Windows 大小写冲突路径")
        payload: dict[str, bytes] = {}
        for info in infos:
            name = info.filename
            if info.is_dir() or name.endswith("/"):
                fail(f"{label} 不允许目录项: {name}")
            if not _safe_archive_name(name):
                fail(f"{label} 内存在不安全路径: {name}")
            unix_mode = info.external_attr >> 16
            if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                fail(f"{label} 内存在符号链接: {name}")
            try:
                payload[name] = archive.read(info)
            except Exception as exc:
                fail(f"{label} 无法读取 {name}: {exc}")
        return payload


def audit_zip_exact(
    path: Path,
    expected: dict[str, bytes],
    *,
    label: str,
) -> None:
    actual = _zip_payload(path, label=label)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("缺少 " + ", ".join(missing[:8]))
        if extra:
            detail.append("多出 " + ", ".join(extra[:8]))
        fail(f"{label} 内容不等于精确白名单：" + "；".join(detail))
    drift = [name for name in sorted(expected) if actual[name] != expected[name]]
    if drift:
        fail(
            f"{label} 文件内容不是当前源码: "
            + ", ".join(drift[:8])
            + (" …" if len(drift) > 8 else "")
        )


def payload_sha256(payload: dict[str, bytes]) -> str:
    """Hash archive semantics, independent of ZIP metadata/compression."""

    digest = hashlib.sha256()
    for name, content in sorted(payload.items()):
        name_bytes = name.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(8, "big"))
        digest.update(name_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def audit_launcher_archive(
    archive_path: Path,
    *,
    source_root: Path = HERE,
) -> None:
    audit_zip_exact(
        archive_path,
        launcher_source_snapshot(source_root),
        label="launcher ZIP",
    )


def audit_artifact(
    *,
    package_path: Path,
    channel_path: Path,
    launcher_script_path: Path,
    launcher_archive_path: Path,
    version: str,
    source_root: Path = HERE,
) -> dict[str, Any]:
    expected_package_name = package_name(version)
    if package_path.name != expected_package_name:
        fail(f"测试包文件名没有携带 manifest 版本 {version}")
    package_snapshot = package_source_snapshot(source_root)
    audit_zip_exact(package_path, package_snapshot, label="Windows 测试包")
    try:
        packaged_manifest = json.loads(package_snapshot["manifest.json"])
    except Exception as exc:
        fail(f"源码 manifest.json 无法读取: {exc}")
    if str(packaged_manifest.get("version")) != version:
        fail("ZIP 内 manifest 版本与当前源码不一致")

    launcher_snapshot = launcher_source_snapshot(source_root)
    launcher_source = launcher_snapshot[LAUNCHER_PS1]
    launcher_version = parse_launcher_version(
        launcher_source,
        label="源码 launcher",
    )
    if launcher_version != LAUNCHER_VERSION:
        fail(
            "Python/PowerShell launcher version 不一致: "
            f"{LAUNCHER_VERSION} != {launcher_version}"
        )
    if launcher_script_path.name != launcher_script_name(launcher_version):
        fail("launcher 脚本文件名没有携带 launcherVersion")
    if read_regular_file(launcher_script_path) != launcher_source:
        fail("发布 launcher 脚本不是当前 PowerShell 源码")
    if launcher_archive_path.name != launcher_archive_name(launcher_version):
        fail("launcher ZIP 文件名没有携带 launcherVersion")
    audit_zip_exact(
        launcher_archive_path,
        launcher_snapshot,
        label="launcher ZIP",
    )

    channel = read_json(channel_path)
    validate_channel_metadata(
        channel,
        version=version,
        launcher_version=launcher_version,
    )
    digest = sha256_file(package_path)
    launcher_digest = sha256_file(launcher_script_path)
    if channel["sha256"] != digest:
        fail("channel SHA-256 与 Windows 测试包不一致")
    if channel["launcherSha256"] != launcher_digest:
        fail("channel launcherSha256 与 launcher 脚本不一致")
    audited = dict(channel)
    audited["_launcherPayloadSha256"] = payload_sha256(launcher_snapshot)
    return audited


def _public_filename_from_url(url: str, *, field: str) -> str:
    parsed = urlsplit(url)
    prefix = OFFICIAL_STATIC_PATH + "/"
    if (
        parsed.scheme != "https"
        or parsed.netloc != urlsplit(OFFICIAL_ORIGIN).netloc
        or not parsed.path.startswith(prefix)
        or parsed.query
        or parsed.fragment
    ):
        fail(f"已部署 channel {field} 不在官方静态目录")
    name = parsed.path[len(prefix):]
    if PurePosixPath(name).name != name or not name:
        fail(f"已部署 channel {field} 文件名不安全")
    return name


def audit_deployed_baseline(
    channel_path: Path = DEFAULT_DEPLOYED_CHANNEL,
) -> dict[str, Any]:
    """Validate deployed hashes/URLs without comparing an old ZIP to new source."""

    channel = read_json(channel_path)
    version = str(channel.get("version", ""))
    version_tuple(version)
    launcher_version = channel.get("launcherVersion")
    if type(launcher_version) is not int:
        fail("已部署 channel launcherVersion 必须是整数")
    validate_channel_metadata(
        channel,
        version=version,
        launcher_version=launcher_version,
        allow_legacy_launcher_url=True,
    )
    deploy_root = channel_path.resolve().parent
    package_path = deploy_root / _public_filename_from_url(
        channel["url"],
        field="url",
    )
    script_path = deploy_root / _public_filename_from_url(
        channel["launcherUrl"],
        field="launcherUrl",
    )
    archive_path = deploy_root / launcher_archive_name(launcher_version)
    if sha256_file(package_path) != channel["sha256"]:
        fail("已部署主包与 deployed channel SHA-256 不一致")
    script = read_regular_file(script_path)
    if sha256_bytes(script) != channel["launcherSha256"]:
        fail("已部署 launcher 脚本与 deployed channel SHA-256 不一致")
    if parse_launcher_version(script, label="已部署 launcher") != launcher_version:
        fail("已部署 launcher 内部版本与 channel 不一致")
    payload = _zip_payload(archive_path, label="已部署 launcher ZIP")
    if set(payload) != set(LAUNCHER_FILES):
        fail("已部署 launcher ZIP 不是两文件精确清单")
    if payload[LAUNCHER_PS1] != script:
        fail("已部署 launcher ZIP 内 PowerShell 与公开脚本不一致")
    audited = dict(channel)
    audited["_launcherPayloadSha256"] = payload_sha256(payload)
    return audited


def validate_launcher_upgrade(
    *,
    candidate: dict[str, Any],
    deployed: dict[str, Any],
) -> None:
    candidate_version = candidate["launcherVersion"]
    deployed_version = deployed["launcherVersion"]
    if candidate_version < deployed_version:
        fail(
            f"launcherVersion 不得下降: {deployed_version} → {candidate_version}"
        )
    script_changed = (
        candidate["launcherSha256"] != deployed["launcherSha256"]
    )
    candidate_payload = candidate.get("_launcherPayloadSha256")
    deployed_payload = deployed.get("_launcherPayloadSha256")
    archive_changed = (
        isinstance(candidate_payload, str)
        and isinstance(deployed_payload, str)
        and candidate_payload != deployed_payload
    )
    if (script_changed or archive_changed) and candidate_version <= deployed_version:
        fail(
            "launcher 脚本或双文件 ZIP 内容已变化，必须提升 launcherVersion "
            f"（当前 {deployed_version}）"
        )


def audit_source(*, skip_browser: bool) -> None:
    command = [sys.executable, str(HERE / "handoff_check.py")]
    if not skip_browser:
        command.append("--full")
    result = subprocess.run(command, cwd=HERE.parent.parent, check=False)
    if result.returncode:
        fail("handoff_check 未通过，生产 channel 未发生变化")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact",
        type=Path,
        required=True,
        help="generated Windows extension ZIP (required)",
    )
    parser.add_argument(
        "--channel",
        type=Path,
        default=EXTENSIONS / CHANNEL_FILENAME,
    )
    parser.add_argument(
        "--launcher-script",
        type=Path,
        help="versioned published PowerShell launcher",
    )
    parser.add_argument(
        "--launcher-archive",
        type=Path,
        help="versioned two-file launcher ZIP",
    )
    parser.add_argument(
        "--deployed-channel",
        type=Path,
        default=DEFAULT_DEPLOYED_CHANNEL,
    )
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="development-only: omit headed Chromium contracts",
    )
    args = parser.parse_args()

    manifest = read_json(HERE / "manifest.json")
    version = str(manifest.get("version", ""))
    candidate_version = version_tuple(version)
    launcher_version = source_launcher_version(HERE)
    launcher_script_path = (
        args.launcher_script
        or EXTENSIONS / launcher_script_name(launcher_version)
    ).resolve()
    launcher_archive_path = (
        args.launcher_archive
        or EXTENSIONS / launcher_archive_name(launcher_version)
    ).resolve()

    deployed = audit_deployed_baseline(args.deployed_channel.resolve())
    deployed_version = str(deployed["version"])
    if compare_version(candidate_version, version_tuple(deployed_version)) <= 0:
        fail(
            f"候选版本 {version} 必须严格高于已部署测试通道 "
            f"{deployed_version}；请提升 manifest 版本"
        )
    print(f"✓ 版本单调递增: {deployed_version} → {version}")

    candidate = audit_artifact(
        package_path=args.artifact.resolve(),
        channel_path=args.channel.resolve(),
        launcher_script_path=launcher_script_path,
        launcher_archive_path=launcher_archive_path,
        version=version,
        source_root=HERE,
    )
    validate_launcher_upgrade(candidate=candidate, deployed=deployed)
    print(
        "✓ 主包/channel/版本化 launcher 脚本与双文件 launcher ZIP "
        "均为精确白名单和匹配 SHA-256"
    )

    audit_source(skip_browser=args.skip_browser)
    print("READY: Windows 测试通道部署前门禁通过；尚未执行部署。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
