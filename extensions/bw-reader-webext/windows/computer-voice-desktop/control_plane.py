from __future__ import annotations

import csv
from dataclasses import dataclass
import json
import locale
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable, ContextManager, Protocol, Sequence
import xml.etree.ElementTree as ET

from bridge_core import (
    BridgeError,
    BridgePaths,
    CREATE_NO_WINDOW,
    DIRECT_SERVE_PATH,
    DIRECT_SERVE_SIBLING_PATHS,
    FIXED_LISTEN_HOST,
    FIXED_LISTEN_PORT,
    TASK_NAME,
    build_tailscale_command_plan,
)


TASK_XML_NAMESPACE = (
    "http://schemas.microsoft.com/windows/2004/02/mit/task"
)
TASK_DESCRIPTION_MARKER = (
    "BW_READER_COMPUTER_VOICE_DIRECT_BOOTSTRAP_V1"
)
TASK_TEMP_PREFIX = "bw-computer-voice-task-"
TASK_XML_NAME = "computer-voice-bootstrap-task.xml"
SERVE_PATH = DIRECT_SERVE_PATH
ALLOWED_SERVE_PATHS = (SERVE_PATH,) + DIRECT_SERVE_SIBLING_PATHS
SERVE_TARGET = (
    f"http://{FIXED_LISTEN_HOST}:{FIXED_LISTEN_PORT}{SERVE_PATH}"
)
SERVE_WEB_HOST = "bwicarus-2.taile44d0c.ts.net:443"
SERVE_TCP_PORT = "443"
SID_RE = re.compile(r"^S-1-(?:\d+-){1,14}\d+$")
TASK_NOT_FOUND_MARKERS = (
    "the system cannot find the file specified",
    "系统找不到指定的文件",
    "系統找不到指定的檔案",
    "指定されたファイルが見つかりません",
)


@dataclass(frozen=True)
class ControlPaths:
    schtasks_exe: Path
    whoami_exe: Path
    tailscale_exe: Path

    @classmethod
    def discover(cls) -> "ControlPaths":
        windows_root = Path(
            os.environ.get("SystemRoot", r"C:\Windows")
        )
        program_files = Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")
        )
        return cls(
            schtasks_exe=windows_root / "System32" / "schtasks.exe",
            whoami_exe=windows_root / "System32" / "whoami.exe",
            tailscale_exe=(
                program_files / "Tailscale" / "tailscale.exe"
            ),
        )


@dataclass(frozen=True)
class TaskCommandPlan:
    identity: tuple[str, ...]
    query: tuple[str, ...]
    create: tuple[str, ...]
    run: tuple[str, ...]
    end: tuple[str, ...]
    delete: tuple[str, ...]


@dataclass(frozen=True)
class TaskInspection:
    exists: bool
    owned: bool
    user_sid: str


@dataclass(frozen=True)
class ServeInspection:
    state: str
    handlers: tuple[tuple[str, str], ...]


class ExactCommandRunner(Protocol):
    def run_exact(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        ...


def _decode_command_output(
    value: bytes | str,
    *,
    fallback_encoding: str | None = None,
) -> str:
    if isinstance(value, str):
        return value
    encodings = ("utf-8", fallback_encoding or locale.getencoding())
    tried: set[str] = set()
    for encoding in encodings:
        normalized = encoding.casefold()
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise BridgeError("控制命令输出编码无效。")


class SubprocessExactCommandRunner:
    def run_exact(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        if not command:
            raise BridgeError("控制命令为空。")
        completed = subprocess.run(
            list(command),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            timeout=timeout_seconds,
            shell=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if isinstance(completed.stdout, str) and isinstance(
            completed.stderr,
            str,
        ):
            return completed
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            _decode_command_output(completed.stdout),
            _decode_command_output(completed.stderr),
        )


def _named_executable(path: Path, expected_name: str) -> Path:
    path = path.resolve()
    if (
        not path.is_absolute()
        or path.name.casefold() != expected_name.casefold()
    ):
        raise BridgeError(f"{expected_name} 路径无效。")
    return path


def _validate_control_paths(paths: ControlPaths) -> ControlPaths:
    return ControlPaths(
        schtasks_exe=_named_executable(
            paths.schtasks_exe,
            "schtasks.exe",
        ),
        whoami_exe=_named_executable(
            paths.whoami_exe,
            "whoami.exe",
        ),
        tailscale_exe=_named_executable(
            paths.tailscale_exe,
            "tailscale.exe",
        ),
    )


def _validate_user_sid(value: str) -> str:
    value = value.strip()
    if len(value) > 184 or not SID_RE.fullmatch(value):
        raise BridgeError("当前 Windows 用户 SID 无效。")
    return value


def _validate_user_name(value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise BridgeError("当前 Windows 用户名无效。")
    return value


def _task_launcher(paths: BridgePaths) -> Path:
    expected = (
        paths.root
        / "desktop-launcher"
        / "BW-Computer-Voice-Bridge.exe"
    ).resolve()
    if (
        paths.desktop_launcher.resolve() != expected
        or not expected.is_file()
    ):
        raise BridgeError("后台引导器 EXE 偏离固定安装目录。")
    return expected


def build_task_xml(paths: BridgePaths, user_sid: str) -> bytes:
    launcher = _task_launcher(paths)
    sid = _validate_user_sid(user_sid)
    ET.register_namespace("", TASK_XML_NAMESPACE)

    def tag(name: str) -> str:
        return f"{{{TASK_XML_NAMESPACE}}}{name}"

    task = ET.Element(tag("Task"), {"version": "1.4"})
    registration = ET.SubElement(task, tag("RegistrationInfo"))
    ET.SubElement(registration, tag("Description")).text = (
        TASK_DESCRIPTION_MARKER
    )

    triggers = ET.SubElement(task, tag("Triggers"))
    logon = ET.SubElement(triggers, tag("LogonTrigger"))
    ET.SubElement(logon, tag("Enabled")).text = "true"
    ET.SubElement(logon, tag("UserId")).text = sid

    principals = ET.SubElement(task, tag("Principals"))
    principal = ET.SubElement(
        principals,
        tag("Principal"),
        {"id": "Author"},
    )
    ET.SubElement(principal, tag("UserId")).text = sid
    ET.SubElement(principal, tag("LogonType")).text = (
        "InteractiveToken"
    )
    ET.SubElement(principal, tag("RunLevel")).text = "LeastPrivilege"

    settings = ET.SubElement(task, tag("Settings"))
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "true"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "true"),
        ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Priority", "7"),
    ):
        ET.SubElement(settings, tag(name)).text = value
    idle = ET.SubElement(settings, tag("IdleSettings"))
    ET.SubElement(idle, tag("StopOnIdleEnd")).text = "false"
    ET.SubElement(idle, tag("RestartOnIdle")).text = "false"
    restart = ET.SubElement(settings, tag("RestartOnFailure"))
    ET.SubElement(restart, tag("Interval")).text = "PT1M"
    ET.SubElement(restart, tag("Count")).text = "3"

    actions = ET.SubElement(task, tag("Actions"), {"Context": "Author"})
    execute = ET.SubElement(actions, tag("Exec"))
    ET.SubElement(execute, tag("Command")).text = str(launcher)
    ET.SubElement(execute, tag("Arguments")).text = "--bootstrap"
    ET.SubElement(execute, tag("WorkingDirectory")).text = str(
        launcher.parent
    )
    return ET.tostring(
        task,
        encoding="utf-16",
        xml_declaration=True,
    )


def _single_text(
    root: ET.Element,
    path: str,
) -> str | None:
    values = root.findall(
        path,
        {"t": TASK_XML_NAMESPACE},
    )
    if len(values) != 1:
        return None
    return values[0].text or ""


def task_xml_is_owned(
    xml: str | bytes,
    paths: BridgePaths,
    user_sid: str,
    *,
    user_name: str | None = None,
) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    sid = _validate_user_sid(user_sid)
    launcher = _task_launcher(paths)
    ns = {"t": TASK_XML_NAMESPACE}
    if root.tag != f"{{{TASK_XML_NAMESPACE}}}Task":
        return False
    triggers = root.find("./t:Triggers", ns)
    principals = root.find("./t:Principals", ns)
    actions = root.find("./t:Actions", ns)
    if (
        triggers is None
        or [child.tag for child in triggers]
        != [f"{{{TASK_XML_NAMESPACE}}}LogonTrigger"]
    ):
        return False
    if (
        principals is None
        or [child.tag for child in principals]
        != [f"{{{TASK_XML_NAMESPACE}}}Principal"]
    ):
        return False
    if (
        actions is None
        or [child.tag for child in actions]
        != [f"{{{TASK_XML_NAMESPACE}}}Exec"]
        or actions.attrib != {"Context": "Author"}
    ):
        return False
    principal = principals[0]
    if principal.attrib != {"id": "Author"}:
        return False
    trigger_user = _single_text(
        root,
        "./t:Triggers/t:LogonTrigger/t:UserId",
    )
    allowed_trigger_users = {sid.casefold()}
    if user_name is not None:
        allowed_trigger_users.add(
            _validate_user_name(user_name).casefold()
        )
    if (
        trigger_user is None
        or trigger_user.casefold() not in allowed_trigger_users
    ):
        return False
    trigger_enabled = root.findall(
        "./t:Triggers/t:LogonTrigger/t:Enabled",
        ns,
    )
    if len(trigger_enabled) > 1 or (
        trigger_enabled
        and (trigger_enabled[0].text or "") != "true"
    ):
        return False
    run_level = root.findall(
        "./t:Principals/t:Principal/t:RunLevel",
        ns,
    )
    if len(run_level) > 1 or (
        run_level
        and (run_level[0].text or "") != "LeastPrivilege"
    ):
        return False
    expected = {
        "./t:RegistrationInfo/t:Description":
            TASK_DESCRIPTION_MARKER,
        "./t:Principals/t:Principal/t:UserId": sid,
        "./t:Principals/t:Principal/t:LogonType":
            "InteractiveToken",
        "./t:Settings/t:MultipleInstancesPolicy": "IgnoreNew",
        "./t:Settings/t:StartWhenAvailable": "true",
        "./t:Settings/t:RunOnlyIfNetworkAvailable": "true",
        "./t:Settings/t:Hidden": "true",
        "./t:Settings/t:ExecutionTimeLimit": "PT0S",
        "./t:Settings/t:RestartOnFailure/t:Interval": "PT1M",
        "./t:Settings/t:RestartOnFailure/t:Count": "3",
        "./t:Actions/t:Exec/t:Command": str(launcher),
        "./t:Actions/t:Exec/t:Arguments": "--bootstrap",
        "./t:Actions/t:Exec/t:WorkingDirectory": str(
            launcher.parent
        ),
    }
    return all(
        _single_text(root, path) == value
        for path, value in expected.items()
    )


def build_task_command_plan(
    control_paths: ControlPaths,
    xml_path: Path,
) -> TaskCommandPlan:
    control = _validate_control_paths(control_paths)
    xml_path = xml_path.resolve()
    if not xml_path.is_absolute() or xml_path.name != TASK_XML_NAME:
        raise BridgeError("计划任务 XML 临时路径无效。")
    return TaskCommandPlan(
        identity=(
            str(control.whoami_exe),
            "/USER",
            "/FO",
            "CSV",
            "/NH",
        ),
        query=(
            str(control.schtasks_exe),
            "/Query",
            "/TN",
            TASK_NAME,
            "/XML",
        ),
        create=(
            str(control.schtasks_exe),
            "/Create",
            "/TN",
            TASK_NAME,
            "/XML",
            str(xml_path),
        ),
        run=(
            str(control.schtasks_exe),
            "/Run",
            "/TN",
            TASK_NAME,
        ),
        end=(
            str(control.schtasks_exe),
            "/End",
            "/TN",
            TASK_NAME,
        ),
        delete=(
            str(control.schtasks_exe),
            "/Delete",
            "/TN",
            TASK_NAME,
            "/F",
        ),
    )


def _run(
    runner: ExactCommandRunner,
    command: Sequence[str],
    *,
    timeout_seconds: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    return runner.run_exact(
        tuple(command),
        timeout_seconds=timeout_seconds,
    )


def current_user_identity(
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
) -> tuple[str, str]:
    placeholder = (
        Path(tempfile.gettempdir())
        / TASK_TEMP_PREFIX
        / TASK_XML_NAME
    )
    plan = build_task_command_plan(control_paths, placeholder)
    result = _run(runner, plan.identity)
    if result.returncode != 0:
        raise BridgeError("无法只读确认当前 Windows 用户 SID。")
    try:
        row = next(csv.reader([result.stdout.strip()]))
    except (StopIteration, csv.Error) as error:
        raise BridgeError("当前 Windows 用户 SID 输出无效。") from error
    if len(row) != 2:
        raise BridgeError("当前 Windows 用户 SID 输出无效。")
    return (
        _validate_user_name(row[0]),
        _validate_user_sid(row[1]),
    )


def current_user_sid(
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
) -> str:
    return current_user_identity(control_paths, runner)[1]


def inspect_bootstrap_task(
    paths: BridgePaths,
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
    *,
    user_sid: str | None = None,
    user_name: str | None = None,
) -> TaskInspection:
    if user_sid is None:
        account_name, sid = current_user_identity(
            control_paths,
            runner,
        )
    else:
        sid = _validate_user_sid(user_sid)
        account_name = (
            _validate_user_name(user_name)
            if user_name is not None
            else None
        )
    placeholder = (
        Path(tempfile.gettempdir())
        / TASK_TEMP_PREFIX
        / TASK_XML_NAME
    )
    plan = build_task_command_plan(control_paths, placeholder)
    result = _run(runner, plan.query)
    query_error = f"{result.stdout}\n{result.stderr}".casefold()
    if result.returncode == 1 and any(
        marker.casefold() in query_error
        for marker in TASK_NOT_FOUND_MARKERS
    ):
        return TaskInspection(False, False, sid)
    if result.returncode != 0:
        raise BridgeError("计划任务只读查询失败。")
    return TaskInspection(
        True,
        task_xml_is_owned(
            result.stdout,
            paths,
            sid,
            user_name=account_name,
        ),
        sid,
    )


def _temporary_directory() -> ContextManager[str]:
    return tempfile.TemporaryDirectory(prefix=TASK_TEMP_PREFIX)


def install_bootstrap_task(
    paths: BridgePaths,
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
    *,
    temporary_directory_factory: Callable[
        [], ContextManager[str]
    ] = _temporary_directory,
) -> bool:
    account_name, sid = current_user_identity(
        control_paths,
        runner,
    )
    existing = inspect_bootstrap_task(
        paths,
        control_paths,
        runner,
        user_sid=sid,
        user_name=account_name,
    )
    if existing.exists:
        raise BridgeError(
            "同名计划任务已存在；无论是否相似都拒绝覆盖。"
        )

    with temporary_directory_factory() as temporary:
        temporary_root = Path(temporary).resolve()
        if (
            not temporary_root.is_dir()
            or not temporary_root.name.startswith(TASK_TEMP_PREFIX)
        ):
            raise BridgeError("计划任务 XML 不在受控临时目录。")
        xml_path = (temporary_root / TASK_XML_NAME).resolve()
        if xml_path.parent != temporary_root:
            raise BridgeError("计划任务 XML 临时路径逃逸。")
        xml_path.write_bytes(build_task_xml(paths, sid))
        plan = build_task_command_plan(control_paths, xml_path)
        created = _run(runner, plan.create)
        if created.returncode != 0:
            raise BridgeError("计划任务创建失败。")

    verified = inspect_bootstrap_task(
        paths,
        control_paths,
        runner,
        user_sid=sid,
        user_name=account_name,
    )
    if verified.exists and verified.owned:
        return True
    if verified.exists:
        raise BridgeError(
            "计划任务创建后 ownership 无法证明；为避免删除未知任务，"
            "已拒绝自动回滚。"
        )
    raise BridgeError("计划任务创建后未找到；没有可安全回滚的对象。")


def remove_bootstrap_task(
    paths: BridgePaths,
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
) -> bool:
    inspection = inspect_bootstrap_task(paths, control_paths, runner)
    if not inspection.exists:
        return False
    if not inspection.owned:
        raise BridgeError("同名任务不是本启动器创建；拒绝删除。")
    plan = build_task_command_plan(
        control_paths,
        Path(tempfile.gettempdir())
        / TASK_TEMP_PREFIX
        / TASK_XML_NAME,
    )
    ended = _run(runner, plan.end)
    if ended.returncode not in (0, 1):
        raise BridgeError("无法安全结束后台引导器任务。")
    deleted = _run(runner, plan.delete)
    if deleted.returncode != 0:
        raise BridgeError("后台引导器任务删除失败。")
    after = inspect_bootstrap_task(
        paths,
        control_paths,
        runner,
        user_sid=inspection.user_sid,
    )
    if after.exists:
        raise BridgeError("后台引导器任务删除后仍存在。")
    return True


def run_bootstrap_task_if_owned(
    paths: BridgePaths,
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
) -> bool:
    inspection = inspect_bootstrap_task(
        paths,
        control_paths,
        runner,
    )
    if not inspection.exists:
        return False
    if not inspection.owned:
        raise BridgeError(
            "同名任务 ownership 未通过；拒绝运行或直接启动旁路。"
        )
    plan = build_task_command_plan(
        control_paths,
        Path(tempfile.gettempdir())
        / TASK_TEMP_PREFIX
        / TASK_XML_NAME,
    )
    started = _run(runner, plan.run)
    if started.returncode != 0:
        raise BridgeError("后台 supervisor 任务启动失败。")
    return True


def _collect_handlers(value: object) -> list[tuple[str, str]]:
    handlers: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "Handlers" and isinstance(child, dict):
                for path, handler in child.items():
                    if isinstance(handler, dict):
                        proxy = handler.get("Proxy")
                        handlers.append(
                            (
                                str(path),
                                str(proxy) if isinstance(proxy, str) else "",
                            )
                        )
                    else:
                        handlers.append((str(path), ""))
            handlers.extend(_collect_handlers(child))
    elif isinstance(value, list):
        for child in value:
            handlers.extend(_collect_handlers(child))
    return handlers


def _expected_serve_proxy(path: str) -> str:
    return f"http://{FIXED_LISTEN_HOST}:{FIXED_LISTEN_PORT}{path}"


def _prune_empty(value: dict) -> dict:
    return {k: v for k, v in value.items() if v not in ({}, [], None)}


def _mounted_paths(inspection: "ServeInspection") -> set[str]:
    return {path for path, _ in inspection.handlers}


def classify_serve_status(value: object) -> ServeInspection:
    if not isinstance(value, dict):
        return ServeInspection("foreign", ())
    handlers = tuple(_collect_handlers(value))
    foreign = ServeInspection("foreign", handlers)
    top = _prune_empty(value)
    # Only TCP/Web may be present with content.  Services, Foreground and a
    # non-empty AllowFunnel stay foreign -- Funnel must never be authorised.
    if set(top) - {"TCP", "Web"}:
        return foreign
    tcp = top.get("TCP")
    if tcp is not None and tcp != {SERVE_TCP_PORT: {"HTTPS": True}}:
        return foreign
    web = top.get("Web", {})
    if not isinstance(web, dict) or set(web) - {SERVE_WEB_HOST}:
        return foreign
    host = web.get(SERVE_WEB_HOST, {})
    if not isinstance(host, dict) or set(host) - {"Handlers"}:
        return foreign
    mounts = host.get("Handlers", {})
    if not isinstance(mounts, dict):
        return foreign
    for path, handler in mounts.items():
        if path not in ALLOWED_SERVE_PATHS:
            return foreign
        if handler != {"Proxy": _expected_serve_proxy(path)}:
            return foreign
    if not mounts:
        return ServeInspection("empty", ())
    return ServeInspection("ours", handlers)


def inspect_tailscale_serve(
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
) -> ServeInspection:
    control = _validate_control_paths(control_paths)
    command = build_tailscale_command_plan(
        control.tailscale_exe
    ).serve_status
    result = _run(runner, command)
    if result.returncode != 0:
        raise BridgeError("Tailscale Serve 状态查询失败。")
    text = result.stdout.strip()
    if not text:
        value: object = {}
    else:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise BridgeError("Tailscale Serve 状态不是有效 JSON。") from error
    return classify_serve_status(value)


def apply_tailscale_serve(
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
) -> bool:
    before = inspect_tailscale_serve(control_paths, runner)
    if before.state not in ("empty", "ours"):
        raise BridgeError(
            "检测到其它 Tailscale Serve 配置；拒绝覆盖。"
        )
    if SERVE_PATH in _mounted_paths(before):
        return False
    control = _validate_control_paths(control_paths)
    plan = build_tailscale_command_plan(control.tailscale_exe)
    applied = _run(runner, plan.apply_serve)
    if applied.returncode != 0:
        raise BridgeError("Tailscale Serve 配置失败。")
    after = inspect_tailscale_serve(control_paths, runner)
    if after.state != "ours" or (
        _mounted_paths(after) != _mounted_paths(before) | {SERVE_PATH}
    ):
        raise BridgeError(
            "Tailscale Serve 配置后完整 ownership 无法证明；"
            "拒绝对混合、Funnel 或未知配置执行 off。"
        )
    return True


def remove_tailscale_serve(
    control_paths: ControlPaths,
    runner: ExactCommandRunner,
) -> bool:
    before = inspect_tailscale_serve(control_paths, runner)
    if before.state not in ("empty", "ours"):
        raise BridgeError(
            "当前 Serve 路径并非本桥接器独占；拒绝关闭。"
        )
    if SERVE_PATH not in _mounted_paths(before):
        return False
    control = _validate_control_paths(control_paths)
    plan = build_tailscale_command_plan(control.tailscale_exe)
    removed = _run(runner, plan.rollback_serve)
    if removed.returncode != 0:
        raise BridgeError("Tailscale Serve 精确关闭失败。")
    after = inspect_tailscale_serve(control_paths, runner)
    if after.state not in ("empty", "ours") or (
        _mounted_paths(after) != _mounted_paths(before) - {SERVE_PATH}
    ):
        raise BridgeError(
            "Tailscale Serve 关闭影响了本桥接器以外的挂载；"
            "拒绝继续改动未知状态。"
        )
    return True
