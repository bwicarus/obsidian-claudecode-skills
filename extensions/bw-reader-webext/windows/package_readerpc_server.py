#!/usr/bin/env python3
"""Build, verify, self-test and atomically install ReaderPC 服务器."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Sequence
import uuid
import zipfile


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
DESKTOP_SOURCE = HERE / "computer-voice-desktop"
LAUNCHER_SOURCE = DESKTOP_SOURCE / "readerpc_launcher.py"
PACKAGER_SOURCE = Path(__file__).resolve()
CANDIDATES = HERE / "readerpc-candidates"
PYINSTALLER = Path(
    r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\Scripts\pyinstaller.exe"
)

PACKAGE_CONTRACT = "readerpc-server-package/1"
MANIFEST_SCHEMA = 1
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){1,3}\Z")
ARCHIVE_STAMP = (1980, 1, 1, 0, 0, 0)
EXE_REL = "ReaderPC-Server.exe"
MANIFEST_REL = "manifest.json"
RUNTIME_SOURCES = {
    # AI 查询 CLI(自包含,仅标准库)。安装时另复制到稳定路径
    # %LOCALAPPDATA%/BWReader/replication_activity.py,供 AGENTS 指令引用 ——
    # 路径跨版本不变,内容随每次安装刷新。
    "readerpc-runtime/replication_activity.py": (
        PROJECT_ROOT / "extensions" / "bw-reader-webext" / "windows"
        / "computer-voice-desktop" / "replication_activity.py"
    ),
    "readerpc-runtime/replication_notifications.py": (
        PROJECT_ROOT / "extensions" / "bw-reader-webext" / "windows"
        / "computer-voice-desktop" / "replication_notifications.py"
    ),
    "readerpc-runtime/replication_places.py": (
        PROJECT_ROOT / "extensions" / "bw-reader-webext" / "windows"
        / "computer-voice-desktop" / "replication_places.py"
    ),
    "readerpc-runtime/camera_capture.py": (
        PROJECT_ROOT / "extensions" / "bw-reader-webext" / "windows"
        / "computer-voice-desktop" / "camera_capture.py"
    ),
    # ⚠ 漏了它的话，对账循环里 `import voip_push` 会 ImportError，
    # 而那处是 `except ImportError: return 0` —— 于是 deliver=call 的待办
    # **静默地永远不响**，没有任何一处会报错（2026-08-29 差点漏掉）。
    "readerpc-runtime/voip_push.py": (
        PROJECT_ROOT / "extensions" / "bw-reader-webext" / "windows"
        / "computer-voice-desktop" / "voip_push.py"
    ),
    "readerpc-runtime/transit_search.py": (
        PROJECT_ROOT / "extensions" / "bw-reader-webext" / "windows"
        / "computer-voice-desktop" / "transit_search.py"
    ),
    "readerpc-runtime/scripts/reader_pc_preprocess_worker.py": (
        PROJECT_ROOT / "scripts" / "reader_pc_preprocess_worker.py"
    ),
    "readerpc-runtime/scripts/reader_unimernet_adapter.py": (
        PROJECT_ROOT / "scripts" / "reader_unimernet_adapter.py"
    ),
    "readerpc-runtime/scripts/reader_unimernet_base.yaml": (
        PROJECT_ROOT / "scripts" / "reader_unimernet_base.yaml"
    ),
    "readerpc-runtime/scripts/camera_snap.py": (
        PROJECT_ROOT / "scripts" / "camera_snap.py"
    ),
    "readerpc-runtime/scripts/google_vision_ocr.py": (
        PROJECT_ROOT / "scripts" / "google_vision_ocr.py"
    ),
    "readerpc-runtime/scripts/google_api_quota.py": (
        PROJECT_ROOT / "scripts" / "google_api_quota.py"
    ),
    "readerpc-runtime/_server_deploy/reader_book_ocr_worker.py": (
        PROJECT_ROOT / "_server_deploy" / "reader_book_ocr_worker.py"
    ),
}
PAYLOAD_PATHS = (EXE_REL, *RUNTIME_SOURCES.keys())


class PackageError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise PackageError(message)


def _version(value: str) -> str:
    if VERSION_RE.fullmatch(value or "") is None:
        _fail("版本号必须是至少两段的非负整数。")
    return value


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_regular(path: Path) -> bytes:
    try:
        status_value = path.lstat()
    except OSError as exc:
        _fail(f"缺少打包输入: {path}: {exc}")
    if not stat.S_ISREG(status_value.st_mode) or path.is_symlink():
        _fail(f"打包输入不是普通文件: {path}")
    return path.read_bytes()


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _source_inputs() -> list[dict[str, str]]:
    paths = [PACKAGER_SOURCE, *RUNTIME_SOURCES.values()]
    for path in sorted(DESKTOP_SOURCE.glob("*.py")):
        if path.name != "__init__.py":
            paths.append(path)
    # 同一文件可能既在 RUNTIME_SOURCES(要打进 zip)又被 desktop glob 收进
    # 取证清单(replication_activity.py) —— 取证按内容一份就够,去重。
    seen: set[str] = set()
    paths = [p for p in paths
             if str(p.resolve()) not in seen
             and not seen.add(str(p.resolve()))]
    result = []
    for path in paths:
        try:
            relative = path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            relative = path.name
        result.append({"path": relative, "sha256": _sha256(_read_regular(path))})
    if len({item["path"].casefold() for item in result}) != len(result):
        _fail("打包输入路径重复。")
    return sorted(result, key=lambda item: item["path"])


def _zip_write(path: Path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=ARCHIVE_STAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entries[name])


def _safe_archive_name(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(
        value
        and "\\" not in value
        and not value.startswith("/")
        and path.as_posix() == value
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _build_manifest(
    version: str,
    payload: dict[str, bytes],
    source_inputs: list[dict[str, str]],
    pyinstaller_version: str,
) -> dict[str, Any]:
    if tuple(payload) != PAYLOAD_PATHS:
        _fail("候选 payload 不等于精确白名单。")
    return {
        "contract": PACKAGE_CONTRACT,
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "buildInputs": {
            "pyinstaller": {
                "version": pyinstaller_version,
                "oneFile": True,
                "noConsole": True,
            },
            "sourceFiles": source_inputs,
        },
        "files": [
            {
                "path": name,
                "sha256": _sha256(payload[name]),
                "size": len(payload[name]),
            }
            for name in PAYLOAD_PATHS
        ],
    }


def _require_app_version_matches(version: str) -> None:
    """源码里的 APP_VERSION 必须等于这次打的版本号。

    2026-08-18 发现两者早就分家了:候选已经打到 0.1.45，而 readerpc_launcher.APP_VERSION
    还停在 0.1.34 —— 也就是说 --self-test 报出来的 version、状态文件里的 version、
    以及日后任何按版本判断的逻辑，指的都不是实际在跑的这一版。
    这种漂移不会报错，只会在排查时把人引到错误的版本上，所以在这里拦住。
    """

    text = LAUNCHER_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]+)"', text, re.M)
    if match is None:
        _fail("readerpc_launcher.py 里找不到 APP_VERSION")
    if match.group(1) != version:
        _fail(
            f"APP_VERSION({match.group(1)}) 与打包版本({version})不一致；"
            "先改源码里的 APP_VERSION 再打包。"
        )


def build_candidate(version: str) -> Path:
    version = _version(version)
    if not PYINSTALLER.is_file():
        _fail(f"未找到固定 PyInstaller: {PYINSTALLER}")
    _require_app_version_matches(version)
    _read_regular(LAUNCHER_SOURCE)
    for source in RUNTIME_SOURCES.values():
        _read_regular(source)
    destination = CANDIDATES / version
    archive_path = destination / f"readerpc-server-{version}-windows-x64.zip"
    if destination.exists():
        _fail(f"候选版本已存在，拒绝覆盖: {destination}")
    source_inputs = _source_inputs()
    destination.mkdir(parents=True)
    work = destination / "_work"
    dist = work / "dist"
    environment = os.environ.copy()
    environment.update({"PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "315532800"})
    try:
        version_result = subprocess.run(
            [str(PYINSTALLER), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        if version_result.returncode != 0:
            _fail("PyInstaller 版本探测失败。")
        pyinstaller_version = version_result.stdout.strip().splitlines()[0]
        command = [
            str(PYINSTALLER),
            "--noconfirm",
            "--clean",
            "--onefile",
            "--noconsole",
            "--name",
            "ReaderPC-Server",
            "--distpath",
            str(dist),
            "--workpath",
            str(work / "build"),
            "--specpath",
            str(work / "spec"),
            "--paths",
            str(DESKTOP_SOURCE),
            "--hidden-import",
            "pystray._win32",
            "--collect-submodules",
            "pystray",
            str(LAUNCHER_SOURCE),
        ]
        result = subprocess.run(
            command,
            cwd=str(HERE),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=environment,
        )
        if result.returncode != 0:
            _fail(f"PyInstaller 失败: {(result.stderr or result.stdout)[-1200:]}")
        if _source_inputs() != source_inputs:
            _fail("打包期间源码发生变化。")
        payload: dict[str, bytes] = {EXE_REL: _read_regular(dist / EXE_REL)}
        for relative, source in RUNTIME_SOURCES.items():
            payload[relative] = _read_regular(source)
        manifest = _build_manifest(
            version,
            payload,
            source_inputs,
            pyinstaller_version,
        )
        prefix = f"ReaderPC-Server-{version}"
        entries = {
            f"{prefix}/{MANIFEST_REL}": _manifest_bytes(manifest),
            **{f"{prefix}/{name}": content for name, content in payload.items()},
        }
        _zip_write(archive_path, entries)
        return archive_path
    except Exception:
        if archive_path.exists():
            archive_path.unlink()
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _read_archive(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or not all(_safe_archive_name(name) for name in names):
                _fail("候选 ZIP 含重复或不安全路径。")
            entries = {name: archive.read(name) for name in names}
    except (OSError, zipfile.BadZipFile) as exc:
        _fail(f"无法读取候选 ZIP: {exc}")
    manifests = [name for name in entries if name.endswith("/manifest.json")]
    if len(manifests) != 1:
        _fail("候选 ZIP manifest 数量不是 1。")
    try:
        manifest = json.loads(entries[manifests[0]].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _fail(f"manifest 不是 UTF-8 JSON: {exc}")
    if not isinstance(manifest, dict):
        _fail("manifest 根必须是 object。")
    return manifest, entries


def verify_archive(path: Path) -> dict[str, Any]:
    manifest, entries = _read_archive(path)
    if (
        manifest.get("contract") != PACKAGE_CONTRACT
        or manifest.get("schema") != MANIFEST_SCHEMA
    ):
        _fail("候选合同不匹配。")
    version = _version(str(manifest.get("version") or ""))
    prefix = f"ReaderPC-Server-{version}"
    expected = {f"{prefix}/{MANIFEST_REL}", *(f"{prefix}/{name}" for name in PAYLOAD_PATHS)}
    if set(entries) != expected:
        _fail("候选 ZIP 不等于精确 payload 白名单。")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(PAYLOAD_PATHS):
        _fail("manifest files 不完整。")
    by_path = {item.get("path"): item for item in files if isinstance(item, dict)}
    if tuple(by_path) != PAYLOAD_PATHS:
        _fail("manifest files 顺序或路径不匹配。")
    for relative in PAYLOAD_PATHS:
        content = entries[f"{prefix}/{relative}"]
        item = by_path[relative]
        if item.get("sha256") != _sha256(content) or item.get("size") != len(content):
            _fail(f"payload 摘要不匹配: {relative}")
    return manifest


def run_packaged_self_test(path: Path) -> None:
    manifest = verify_archive(path)
    _, entries = _read_archive(path)
    version = str(manifest["version"])
    prefix = f"ReaderPC-Server-{version}"
    with tempfile.TemporaryDirectory(prefix="readerpc-self-test-") as raw:
        root = Path(raw)
        for relative in (MANIFEST_REL, *PAYLOAD_PATHS):
            target = root / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entries[f"{prefix}/{relative}"])
        result = subprocess.run(
            [str(root / EXE_REL), "--self-test"],
            cwd=str(root),
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            _fail(f"包内 ReaderPC 自检失败: exit={result.returncode}")
        for relative in RUNTIME_SOURCES:
            if relative.endswith(".py"):
                compile((root / relative).read_text("utf-8"), relative, "exec")


def _default_install_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BWReader" / "ReaderPC-Server"


def _write_shortcut(shortcut: Path, executable: Path) -> Path:
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    temporary = shortcut.with_name(shortcut.name + f".tmp-{uuid.uuid4().hex}.lnk")
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut({quote(str(temporary))});"
        f"$s.TargetPath={quote(str(executable))};"
        f"$s.WorkingDirectory={quote(str(executable.parent))};"
        "$s.Description='ReaderPC 服务器';$s.Save();"
        f"Move-Item -LiteralPath {quote(str(temporary))} -Destination {quote(str(shortcut))} -Force"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=False,
        capture_output=True,
        text=True,
        # 中文 Windows 上 PowerShell 的报错是按控制台代码页(GBK)输出的，而这里按
        # UTF-8 解 —— 一旦它真的报错，subprocess.run 会先抛 UnicodeDecodeError，
        # 于是**看不到那条错误本身**，只看到一个跟快捷方式毫无关系的解码异常。
        # 诊断通道不能反过来把诊断毁掉：解不出来的字节就替换掉，把原文留给人看。
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0 or not shortcut.is_file():
        _fail(f"创建快捷方式失败: {(result.stderr or result.stdout)[-500:]}")
    return shortcut


def _write_shortcuts(executable: Path) -> dict[str, Path]:
    appdata = Path(os.environ.get("APPDATA") or Path.home())
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    return {
        "startMenu": _write_shortcut(
            appdata
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "ReaderPC 服务器.lnk",
            executable,
        ),
        "desktop": _write_shortcut(
            profile / "Desktop" / "ReaderPC 服务器.lnk",
            executable,
        ),
    }


def install_archive(path: Path, *, launch: bool = False, install_root: Path | None = None) -> Path:
    manifest = verify_archive(path)
    _, entries = _read_archive(path)
    version = str(manifest["version"])
    prefix = f"ReaderPC-Server-{version}"
    root = (install_root or _default_install_root()).resolve()
    releases = root / "releases"
    release = releases / version
    if release.exists():
        _fail(f"安装版本已存在，拒绝覆盖: {release}")
    releases.mkdir(parents=True, exist_ok=True)
    staging = releases / f".staging-{version}-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        for relative in (MANIFEST_REL, *PAYLOAD_PATHS):
            target = staging / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entries[f"{prefix}/{relative}"])
        for item in manifest["files"]:
            installed = staging / PurePosixPath(item["path"])
            if _sha256(installed.read_bytes()) != item["sha256"]:
                _fail(f"安装 staging 摘要不匹配: {item['path']}")
        staging.replace(release)
        # ⚠ **这是第二份清单。** 上面 RUNTIME_SOURCES 决定"打进包里"，
        # 这里决定"铺到稳定路径"（%LOCALAPPDATA%/BWReader/*.py，AI 直接跑的
        # 就是这些）。2026-08-29 只加了第一份，结果 voip_push.py 在包里、
        # 却不在运行位置 —— 而调用方是 `except ImportError: return 0`，
        # 于是 deliver=call **静默地永远不响**。
        # 加新 CLI 时两份都要动。
        for stable_name in (
            "replication_activity.py", "replication_notifications.py",
            "replication_places.py", "transit_search.py",
            "camera_capture.py", "voip_push.py",
        ):
            (root.parent / stable_name).write_bytes(
                (release / "readerpc-runtime" / stable_name).read_bytes()
            )
        # 配额闸 CLI 也要稳定路径(AGENTS 引用) —— 它在 scripts/ 子目录打包,
        # 复制口径与上面不同,单列。
        (root.parent / "google_api_quota.py").write_bytes(
            (release / "readerpc-runtime" / "scripts"
             / "google_api_quota.py").read_bytes()
        )
        # 取图脚本同理:它与 Pi 上跑的是同一份源码,本机摄像头也用它。
        (root.parent / "camera_snap.py").write_bytes(
            (release / "readerpc-runtime" / "scripts"
             / "camera_snap.py").read_bytes()
        )
        shortcuts = _write_shortcuts(release / EXE_REL)
        _atomic_json(
            root / "current.json",
            {
                "contract": "readerpc-server-install/1",
                "version": version,
                "release": str(release),
                "shortcut": str(shortcuts["startMenu"]),
                "desktopShortcut": str(shortcuts["desktop"]),
                "manifestSha256": _sha256(_manifest_bytes(manifest)),
            },
        )
        if launch:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            subprocess.Popen(
                [str(release / EXE_REL)],
                cwd=str(release),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
            # ⚠ Popen 成功 ≠ 新代际在跑。2026-08-24 实锤：新实例发现旧
            # ReaderPC 未完成正常退出会**按设计拒绝接管并自行退出**（痕迹在
            # readerpc-server.log），旧版本继续跑 —— 而"心跳新鲜"是旧进程
            # 刷的，验证心跳等于什么都没验。这里必须验证到"所有 ReaderPC
            # 进程都来自新 release"，否则安装静默失败。
            verify_running_generation(release)
        return release
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _running_readerpc_exe_paths() -> list[str]:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='ReaderPC-Server.exe'\""
            " | ForEach-Object { $_.ExecutablePath }",
        ],
        capture_output=True,
        text=True,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        _fail("无法枚举 ReaderPC 进程；不能确认新代际是否在跑。")
    return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]


def verify_running_generation(
    release: Path,
    *,
    probe: Any = None,
    sleeper: Any = None,
    timeout_seconds: float = 90.0,
) -> None:
    """--launch 后的接管验证：所有 ReaderPC 进程都必须来自新 release。

    三种失败形态，全部出声：
    - 旧代际仍在（新实例拒绝接管后自退）→ 指向 readerpc-server.log；
    - 没有任何进程（新实例起了又崩）；
    - 超时仍混着新旧。
    """
    import time as _time

    probe = probe or _running_readerpc_exe_paths
    sleeper = sleeper or _time.sleep
    release_text = str(release.resolve()).lower()
    deadline = _time.monotonic() + timeout_seconds
    last: list[str] = []
    while _time.monotonic() < deadline:
        last = probe()
        if last and all(release_text in path.lower() for path in last):
            return
        sleeper(3.0)
    log_hint = str(_default_install_root().parent / "readerpc-server.log")
    if not last:
        _fail(
            "安装后没有任何 ReaderPC 进程在跑（新实例可能起了又退）；"
            f"看 {log_hint}"
        )
    _fail(
        "安装后仍有旧代际 ReaderPC 在跑（新实例可能拒绝接管后自退）："
        f"{last}；看 {log_hint} 里的'启动接管'记录，"
        "手动退出旧实例后重新 --launch 或直接启动新 release 的 exe。"
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    temporary.write_bytes(_manifest_bytes(value))
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", metavar="VERSION")
    parser.add_argument("--verify", type=Path, metavar="ZIP")
    parser.add_argument("--self-test", type=Path, metavar="ZIP")
    parser.add_argument("--install", type=Path, metavar="ZIP")
    parser.add_argument("--launch", action="store_true", help="--install 后启动托盘")
    args = parser.parse_args(argv)
    actions = [args.build, args.verify, args.self_test, args.install]
    if sum(value is not None for value in actions) != 1 or (args.launch and args.install is None):
        parser.error("必须且只能选择一个操作；--launch 只能配合 --install。")
    try:
        if args.build is not None:
            print(build_candidate(args.build))
        elif args.verify is not None:
            print(json.dumps(verify_archive(args.verify), ensure_ascii=False, sort_keys=True))
        elif args.self_test is not None:
            run_packaged_self_test(args.self_test)
            print("OK: ReaderPC packaged self-test passed")
        else:
            print(install_archive(args.install, launch=args.launch))
    except PackageError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
