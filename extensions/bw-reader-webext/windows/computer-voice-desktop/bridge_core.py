from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urlsplit
import uuid


DIRECT_CONFIG_CONTRACT = "reader-computer-voice-direct-config/1"
DIRECT_STATUS_CONTRACT = (
    "reader-computer-voice-direct-runtime-status/1"
)
SERVICE_RECORD_CONTRACT = "reader-computer-voice-desktop-service/1"
SELF_TEST_CONTRACT = "reader-computer-voice-desktop-self-test/1"

FIXED_LISTEN_HOST = "127.0.0.1"
FIXED_LISTEN_PORT = 43128
DIRECT_SERVE_PATH = "/reader-computer-voice/v1"
DIRECT_WSS_URL = (
    "wss://bwicarus-2.taile44d0c.ts.net"
    f"{DIRECT_SERVE_PATH}"
)
FIXED_ALLOWED_TAILSCALE_USER_LOGIN = "bwicarus@gmail.com"
FIXED_OUTPUT_SCOPE = "process-only"
FIXED_APP_KIND = "codex-desktop"
FIXED_SHORTCUT = "Ctrl+Shift+C"
DEFAULT_ALLOWED_ORIGINS = ("https://bwicarus.taile44d0c.ts.net",)
PAIR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIR_CODE_LENGTH = 10
PAIR_CODE_TTL = timedelta(minutes=5)
PAIR_CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{10}$")
BASE64URL_SHA256_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ONLINE_STATES = frozenset(
    {
        "starting",
        "idle",
        "reader-connected",
        "starting-app",
        "waiting-app-ready",
        "starting-capture",
        "active",
    }
)
TASK_NAME = "BW Computer Voice Direct Bootstrap"
CREATE_NO_WINDOW = 0x08000000
SUPERVISOR_POLL_SECONDS = 5.0
SUPERVISOR_STABLE_POLLS = 3
SUPERVISOR_UNRESPONSIVE_POLLS = 3
SUPERVISOR_RESTART_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
DISABLE_STOP_RECHECK_SECONDS = (0.05, 0.1, 0.2, 0.4)

# These identifiers are local constants.  Reader input is never accepted as an
# application path, command, or AUMID.
LOCAL_PACKAGED_APP_IDS = {
    "codex-desktop": "OpenAI.Codex_2p2nqsd0c76g0!App",
    "chatgpt-classic": (
        "OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT"
    ),
}

DIRECT_CONFIG_KEYS = frozenset(
    {
        "contract",
        "localOptIn",
        "microphoneEndpointId",
        "listenHost",
        "listenPort",
        "allowedOrigins",
        "allowedTailscaleUserLogin",
        "pairingCodeHash",
        "pairingExpiresAtUtc",
        "pairedClientPublicKeySpki",
        "pairedClientFingerprintSha256",
        "outputScope",
        "appKind",
        "runtimeStatusPath",
    }
)
DIRECT_STATUS_KEYS = frozenset(
    {
        "contract",
        "serviceInstanceId",
        "pid",
        "state",
        "readerConnected",
        "captureActive",
        "updatedAtUtc",
    }
)
SERVICE_RECORD_KEYS = frozenset(
    {
        "contract",
        "pid",
        "executable",
        "configPath",
        "startedAtUtc",
    }
)


class BridgeError(RuntimeError):
    pass


class LocalOptOutDuringStart(BridgeError):
    """The just-started owned child was stopped after a concurrent opt-out."""


@dataclass(frozen=True)
class Microphone:
    endpoint_id: str
    friendly_name: str

    @property
    def display_name(self) -> str:
        return self.friendly_name or self.endpoint_id


@dataclass(frozen=True)
class BridgePaths:
    root: Path
    native_host: Path
    desktop_launcher: Path
    direct_config: Path
    runtime_status: Path
    service_record: Path

    @classmethod
    def for_root(cls, root: Path) -> "BridgePaths":
        root = root.resolve()
        return cls(
            root=root,
            native_host=(
                root / "native-host" / "bw-computer-voice-audio.exe"
            ),
            desktop_launcher=(
                root
                / "desktop-launcher"
                / "BW-Computer-Voice-Bridge.exe"
            ),
            direct_config=(
                root
                / "native-host"
                / "computer-voice-direct.config.json"
            ),
            runtime_status=(
                root
                / "runtime"
                / "computer-voice-direct.status.json"
            ),
            service_record=(
                root
                / "runtime"
                / "computer-voice-direct.service.json"
            ),
        )

    @classmethod
    def discover(cls) -> "BridgePaths":
        candidates: list[Path] = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent.parent)
        candidates.append(Path.home() / "bw-computer-voice-bridge")
        root = next(
            (
                candidate
                for candidate in candidates
                if (
                    candidate
                    / "native-host"
                    / "bw-computer-voice-audio.exe"
                ).is_file()
            ),
            candidates[0],
        )
        return cls.for_root(root)


@dataclass(frozen=True)
class PairingMaterial:
    display_code: str
    code_hash: str
    expires_at_utc: str


@dataclass(frozen=True)
class DirectStatus:
    configuration_enabled: bool
    service_online: bool
    reader_connected: bool
    reason: str
    pid: int | None = None
    service_instance_id: str = ""
    capture_active: bool = False
    paired_client_fingerprint: str = ""


@dataclass(frozen=True)
class TailscaleCommandPlan:
    status: tuple[str, ...]
    serve_status: tuple[str, ...]
    apply_serve: tuple[str, ...]
    rollback_serve: tuple[str, ...]


class ProcessRunner(Protocol):
    def start(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
    ) -> int:
        ...

    def executable_for_pid(self, pid: int) -> Path | None:
        ...

    def terminate_exact(self, pid: int, executable: Path) -> bool:
        ...


class ReadOnlyCommandRunner(Protocol):
    def run_read_only(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise BridgeError("UTC 时间必须带时区。")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_valid_origin(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and value == f"https://{parsed.netloc}"
    )


def _validate_microphone_id(value: str) -> str:
    if (
        not value
        or value.isspace()
        or len(value) > 2048
        or any(ord(character) < 32 for character in value)
    ):
        raise BridgeError("必须明确选择一个有效麦克风。")
    return value


def _validate_runtime_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BridgeError("直连状态文件路径无效。")
    path = Path(value)
    if not path.is_absolute():
        raise BridgeError("直连状态文件必须使用绝对路径。")
    return str(path.resolve())


def _validated_paired_client(
    public_key: object,
    fingerprint: object,
) -> tuple[str, str]:
    if not isinstance(public_key, str) or not isinstance(fingerprint, str):
        raise BridgeError("Reader 公钥记录结构无效。")
    if not public_key and not fingerprint:
        return "", ""
    if (
        not public_key
        or len(public_key) > 4096
        or not BASE64URL_RE.fullmatch(public_key)
        or not BASE64URL_SHA256_RE.fullmatch(fingerprint)
    ):
        raise BridgeError("Reader 公钥或指纹无效。")
    return public_key, fingerprint


def validate_direct_config(
    value: dict[str, Any],
    *,
    expected_runtime_status: Path | None = None,
) -> dict[str, Any]:
    if set(value) != DIRECT_CONFIG_KEYS:
        raise BridgeError("直连配置字段不完整或包含未知字段。")
    if value.get("contract") != DIRECT_CONFIG_CONTRACT:
        raise BridgeError("直连配置合同版本不匹配。")
    if not isinstance(value.get("localOptIn"), bool):
        raise BridgeError("直连配置授权状态无效。")
    microphone = _validate_microphone_id(
        str(value.get("microphoneEndpointId", ""))
    )
    if value.get("listenHost") != FIXED_LISTEN_HOST:
        raise BridgeError("直连服务只允许绑定 127.0.0.1。")
    if value.get("listenPort") != FIXED_LISTEN_PORT:
        raise BridgeError("直连服务端口不符合固定合同。")
    origins = value.get("allowedOrigins")
    if (
        not isinstance(origins, list)
        or not origins
        or len(origins) > 8
        or len(set(origins)) != len(origins)
        or not all(_is_valid_origin(origin) for origin in origins)
    ):
        raise BridgeError("Reader HTTPS 来源白名单无效。")
    if (
        value.get("allowedTailscaleUserLogin")
        != FIXED_ALLOWED_TAILSCALE_USER_LOGIN
    ):
        raise BridgeError("Tailscale 登录身份不符合固定合同。")
    pairing_hash = value.get("pairingCodeHash")
    pairing_expiry = value.get("pairingExpiresAtUtc")
    if pairing_hash == "":
        if pairing_expiry is not None:
            raise BridgeError("无配对码时不得保留过期时间。")
    elif (
        not isinstance(pairing_hash, str)
        or not BASE64URL_SHA256_RE.fullmatch(pairing_hash)
        or parse_utc(pairing_expiry) is None
    ):
        raise BridgeError("一次性配对摘要或过期时间无效。")
    public_key, fingerprint = _validated_paired_client(
        value.get("pairedClientPublicKeySpki"),
        value.get("pairedClientFingerprintSha256"),
    )
    if value.get("outputScope") != FIXED_OUTPUT_SCOPE:
        raise BridgeError("禁止使用全系统输出回退。")
    if value.get("appKind") != FIXED_APP_KIND:
        raise BridgeError("目标应用类型不在本机允许列表中。")
    runtime_path = _validate_runtime_path(value.get("runtimeStatusPath"))
    if (
        expected_runtime_status is not None
        and os.path.normcase(runtime_path)
        != os.path.normcase(str(expected_runtime_status.resolve()))
    ):
        raise BridgeError("直连状态文件偏离固定安装目录。")
    return {
        **value,
        "microphoneEndpointId": microphone,
        "allowedOrigins": list(origins),
        "pairedClientPublicKeySpki": public_key,
        "pairedClientFingerprintSha256": fingerprint,
        "runtimeStatusPath": runtime_path,
    }


def load_direct_config(paths: BridgePaths) -> dict[str, Any] | None:
    value = read_json(paths.direct_config)
    if value is None:
        return None
    try:
        return validate_direct_config(
            value,
            expected_runtime_status=paths.runtime_status,
        )
    except BridgeError:
        return None


def _base64url_sha256(value: bytes) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(value).digest())
        .decode("ascii")
        .rstrip("=")
    )


def normalize_pair_code(value: str) -> str:
    return re.sub(r"[\s-]+", "", value).upper()


def pairing_material_from_code(
    code: str,
    *,
    now: datetime | None = None,
    ttl: timedelta = PAIR_CODE_TTL,
) -> PairingMaterial:
    code = normalize_pair_code(code)
    if not PAIR_CODE_RE.fullmatch(code):
        raise BridgeError("一次性配对码格式无效。")
    now = now or utc_now()
    if now.tzinfo is None or ttl <= timedelta(0) or ttl > timedelta(minutes=10):
        raise BridgeError("一次性配对码有效期无效。")
    return PairingMaterial(
        display_code=code,
        code_hash=_base64url_sha256(code.encode("ascii")),
        expires_at_utc=format_utc(now + ttl),
    )


def generate_pairing_material(
    *,
    now: datetime | None = None,
    choice: Callable[[str], str] = secrets.choice,
) -> PairingMaterial:
    code = "".join(choice(PAIR_CODE_ALPHABET) for _ in range(PAIR_CODE_LENGTH))
    return pairing_material_from_code(code, now=now)


def build_direct_config(
    microphone_endpoint_id: str,
    runtime_status_path: Path,
    *,
    allowed_origins: Sequence[str] = DEFAULT_ALLOWED_ORIGINS,
    local_opt_in: bool = True,
    pairing: PairingMaterial | None = None,
    previous: dict[str, Any] | None = None,
    replace_paired_client: bool = False,
) -> dict[str, Any]:
    microphone = _validate_microphone_id(microphone_endpoint_id)
    origins = list(allowed_origins)
    if not origins or not all(_is_valid_origin(origin) for origin in origins):
        raise BridgeError("Reader HTTPS 来源白名单无效。")

    public_key = ""
    fingerprint = ""
    previous_hash = ""
    previous_expiry: str | None = None
    if previous is not None:
        previous = validate_direct_config(previous)
        public_key, fingerprint = _validated_paired_client(
            previous["pairedClientPublicKeySpki"],
            previous["pairedClientFingerprintSha256"],
        )
        previous_hash = str(previous["pairingCodeHash"])
        previous_expiry = previous["pairingExpiresAtUtc"]
    if replace_paired_client:
        public_key = ""
        fingerprint = ""
    pairing_hash = pairing.code_hash if pairing else previous_hash
    pairing_expiry = (
        pairing.expires_at_utc if pairing else previous_expiry
    )
    value = {
        "contract": DIRECT_CONFIG_CONTRACT,
        "localOptIn": bool(local_opt_in),
        "microphoneEndpointId": microphone,
        "listenHost": FIXED_LISTEN_HOST,
        "listenPort": FIXED_LISTEN_PORT,
        "allowedOrigins": origins,
        "allowedTailscaleUserLogin":
            FIXED_ALLOWED_TAILSCALE_USER_LOGIN,
        "pairingCodeHash": pairing_hash,
        "pairingExpiresAtUtc": pairing_expiry,
        "pairedClientPublicKeySpki": public_key,
        "pairedClientFingerprintSha256": fingerprint,
        "outputScope": FIXED_OUTPUT_SCOPE,
        "appKind": FIXED_APP_KIND,
        "runtimeStatusPath": str(runtime_status_path.resolve()),
    }
    return validate_direct_config(value)


def save_enabled_config(
    paths: BridgePaths,
    microphone: Microphone,
    *,
    allowed_origins: Sequence[str] = DEFAULT_ALLOWED_ORIGINS,
    active_microphones: Sequence[Microphone] | None = None,
) -> dict[str, Any]:
    if not paths.native_host.is_file():
        raise BridgeError(f"直连代理不存在：{paths.native_host}")
    if active_microphones is not None and microphone.endpoint_id not in {
        item.endpoint_id for item in active_microphones
    }:
        raise BridgeError("所选麦克风已不再是 Active 设备；拒绝保存。")
    previous = load_direct_config(paths)
    if paths.direct_config.exists() and previous is None:
        raise BridgeError(
            "现有直连配置无效；拒绝覆盖可能包含的配对公钥。"
        )
    value = build_direct_config(
        microphone.endpoint_id,
        paths.runtime_status,
        allowed_origins=allowed_origins,
        local_opt_in=True,
        previous=previous,
    )
    _atomic_write_json(paths.direct_config, value)
    return value


def disable_config(paths: BridgePaths) -> bool:
    previous = load_direct_config(paths)
    if previous is None:
        if paths.direct_config.exists():
            raise BridgeError("现有直连配置无效；拒绝静默改写。")
        return False
    value = {
        **previous,
        "localOptIn": False,
        "pairingCodeHash": "",
        "pairingExpiresAtUtc": None,
    }
    _atomic_write_json(
        paths.direct_config,
        validate_direct_config(
            value,
            expected_runtime_status=paths.runtime_status,
        ),
    )
    return True


def prepare_pairing(
    paths: BridgePaths,
    *,
    replace_existing: bool = False,
    now: datetime | None = None,
    choice: Callable[[str], str] = secrets.choice,
) -> PairingMaterial:
    previous = load_direct_config(paths)
    if previous is None or previous.get("localOptIn") is not True:
        raise BridgeError("请先保存并启用直连配置。")
    paired = bool(previous["pairedClientPublicKeySpki"])
    if paired and not replace_existing:
        raise BridgeError(
            "此电脑已有 Reader 公钥；重新配对必须显式撤销旧设备。"
        )
    pairing = generate_pairing_material(now=now, choice=choice)
    value = build_direct_config(
        previous["microphoneEndpointId"],
        paths.runtime_status,
        allowed_origins=previous["allowedOrigins"],
        local_opt_in=True,
        pairing=pairing,
        previous=previous,
        replace_paired_client=replace_existing,
    )
    _atomic_write_json(paths.direct_config, value)
    return pairing


def _require_exact_native_paths(paths: BridgePaths) -> tuple[Path, Path]:
    expected_host = (
        paths.root
        / "native-host"
        / "bw-computer-voice-audio.exe"
    ).resolve()
    expected_config = (
        paths.root
        / "native-host"
        / "computer-voice-direct.config.json"
    ).resolve()
    if (
        paths.native_host.resolve() != expected_host
        or paths.direct_config.resolve() != expected_config
    ):
        raise BridgeError("直连进程路径偏离固定安装目录。")
    if not expected_host.is_file():
        raise BridgeError(f"直连代理不存在：{expected_host}")
    if not expected_config.is_file():
        raise BridgeError(f"直连配置不存在：{expected_config}")
    return expected_host, expected_config


def build_start_command(paths: BridgePaths) -> tuple[str, ...]:
    host, config = _require_exact_native_paths(paths)
    return (
        str(host),
        "--direct-serve",
        "--config",
        str(config),
    )


def _require_named_executable(path: Path, expected_name: str) -> Path:
    path = path.resolve()
    if not path.is_absolute() or path.name.casefold() != expected_name.casefold():
        raise BridgeError(f"{expected_name} 路径无效。")
    return path


def build_tailscale_command_plan(
    tailscale_exe: Path,
) -> TailscaleCommandPlan:
    executable = _require_named_executable(tailscale_exe, "tailscale.exe")
    target = (
        f"http://{FIXED_LISTEN_HOST}:{FIXED_LISTEN_PORT}"
        f"{DIRECT_SERVE_PATH}"
    )
    return TailscaleCommandPlan(
        status=(str(executable), "status", "--json"),
        serve_status=(str(executable), "serve", "status", "--json"),
        apply_serve=(
            str(executable),
            "serve",
            "--yes",
            "--bg",
            "--https=443",
            f"--set-path={DIRECT_SERVE_PATH}",
            target,
        ),
        rollback_serve=(
            str(executable),
            "serve",
            "--yes",
            "--bg",
            "--https=443",
            f"--set-path={DIRECT_SERVE_PATH}",
            "off",
        ),
    )


def run_tailscale_read_only_preflight(
    tailscale_exe: Path,
    runner: ReadOnlyCommandRunner,
) -> tuple[subprocess.CompletedProcess[str], ...]:
    plan = build_tailscale_command_plan(tailscale_exe)
    # Only the two read-only commands are callable here.  Mutating Serve
    # commands deliberately have no execution helper in this module.
    return (
        runner.run_read_only(plan.status, timeout_seconds=5.0),
        runner.run_read_only(plan.serve_status, timeout_seconds=5.0),
    )


def build_local_app_launch_command(
    explorer_exe: Path,
    app_kind: str,
) -> tuple[str, ...]:
    explorer = _require_named_executable(explorer_exe, "explorer.exe")
    try:
        app_id = LOCAL_PACKAGED_APP_IDS[app_kind]
    except KeyError as error:
        raise BridgeError("目标应用不在本机允许列表中。") from error
    return (str(explorer), f"shell:AppsFolder\\{app_id}")


def _same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _load_service_record(paths: BridgePaths) -> dict[str, Any] | None:
    value = read_json(paths.service_record)
    if (
        value is None
        or set(value) != SERVICE_RECORD_KEYS
        or value.get("contract") != SERVICE_RECORD_CONTRACT
        or parse_utc(value.get("startedAtUtc")) is None
    ):
        return None
    try:
        pid = int(value.get("pid", 0))
    except (TypeError, ValueError):
        return None
    if (
        pid <= 0
        or not _same_path(value.get("executable", ""), paths.native_host)
        or not _same_path(value.get("configPath", ""), paths.direct_config)
    ):
        return None
    return {**value, "pid": pid}


def _runtime_status_is_fresh(
    value: dict[str, Any],
    *,
    now: datetime,
    maximum_age: timedelta,
) -> bool:
    updated = parse_utc(value.get("updatedAtUtc"))
    if updated is None:
        return False
    age = now.astimezone(timezone.utc) - updated
    return timedelta(seconds=-2) <= age <= maximum_age


def read_direct_status(
    paths: BridgePaths,
    runner: ProcessRunner,
    *,
    now: datetime | None = None,
    maximum_age: timedelta = timedelta(seconds=15),
) -> DirectStatus:
    config = load_direct_config(paths)
    configured = bool(config and config.get("localOptIn") is True)
    fingerprint = (
        str(config.get("pairedClientFingerprintSha256", ""))
        if config
        else ""
    )
    record = _load_service_record(paths)
    if record is None:
        return DirectStatus(
            configured,
            False,
            False,
            "service-record-missing",
            paired_client_fingerprint=fingerprint,
        )
    pid = record["pid"]
    executable = runner.executable_for_pid(pid)
    if executable is None or not _same_path(executable, paths.native_host):
        return DirectStatus(
            configured,
            False,
            False,
            "service-process-offline",
            pid=pid,
            paired_client_fingerprint=fingerprint,
        )
    runtime = read_json(paths.runtime_status)
    if (
        runtime is None
        or set(runtime) != DIRECT_STATUS_KEYS
        or runtime.get("contract") != DIRECT_STATUS_CONTRACT
        or runtime.get("pid") != pid
        or not isinstance(runtime.get("serviceInstanceId"), str)
        or not re.fullmatch(
            r"[0-9a-f]{32}",
            runtime.get("serviceInstanceId", ""),
        )
        or runtime.get("state") not in ONLINE_STATES
        or not _runtime_status_is_fresh(
            runtime,
            now=now or utc_now(),
            maximum_age=maximum_age,
        )
    ):
        return DirectStatus(
            configured,
            False,
            False,
            "runtime-status-offline-or-stale",
            pid=pid,
            paired_client_fingerprint=fingerprint,
        )
    reader_connected = runtime.get("readerConnected") is True
    return DirectStatus(
        configured,
        True,
        reader_connected,
        "reader-connected" if reader_connected else "reader-not-connected",
        pid=pid,
        service_instance_id=runtime["serviceInstanceId"],
        capture_active=runtime.get("captureActive") is True,
        paired_client_fingerprint=fingerprint,
    )


def start_direct_service(
    paths: BridgePaths,
    runner: ProcessRunner,
    *,
    now: datetime | None = None,
) -> int:
    config = load_direct_config(paths)
    if config is None or config.get("localOptIn") is not True:
        raise BridgeError("请先保存并启用直连配置。")
    current = read_direct_status(paths, runner, now=now)
    if current.service_online and current.pid is not None:
        return current.pid
    existing = _load_service_record(paths)
    if paths.service_record.exists() and existing is None:
        raise BridgeError(
            "直连服务记录存在但合同无效；拒绝覆盖或启动第二实例。"
        )
    if existing is not None:
        observed = runner.executable_for_pid(existing["pid"])
        if observed is not None:
            if not _same_path(observed, paths.native_host):
                raise BridgeError(
                    "服务记录 PID 属于陌生进程；拒绝覆盖、停止或冒充。"
                )
            raise BridgeError(
                "固定直连代理进程仍存在但状态无效；"
                "请先显式停止，拒绝启动第二实例。"
            )
    command = build_start_command(paths)
    pid = runner.start(command, cwd=paths.native_host.parent.resolve())
    if not isinstance(pid, int) or pid <= 0:
        raise BridgeError("直连代理没有返回有效 PID。")
    record = {
        "contract": SERVICE_RECORD_CONTRACT,
        "pid": pid,
        "executable": str(paths.native_host.resolve()),
        "configPath": str(paths.direct_config.resolve()),
        "startedAtUtc": format_utc(now or utc_now()),
    }
    try:
        _atomic_write_json(paths.service_record, record)
    except Exception:
        # Do not leave an untracked listener behind if the PID record cannot
        # be committed.  The runner still revalidates PID + exact EXE.
        runner.terminate_exact(pid, paths.native_host.resolve())
        raise
    # The GUI and scheduled bootstrap are different processes, so an in-memory
    # lock cannot serialize "disable" against this launch.  Publishing the
    # owned PID record before re-reading strict config closes both orderings:
    # disable can either see and stop this PID, or this post-commit check sees
    # opt-out and stops the child itself.
    latest = load_direct_config(paths)
    if latest is None or latest.get("localOptIn") is not True:
        terminated = runner.terminate_exact(
            pid,
            paths.native_host.resolve(),
        )
        if not terminated:
            raise BridgeError(
                "直连代理启动期间已停用，但无法用同一进程句柄确认"
                "刚启动的进程停止。"
            )
        _remove_service_record_for_pid(paths, pid)
        raise LocalOptOutDuringStart(
            "直连代理启动期间已停用；刚启动的进程已安全停止。"
        )
    return pid


def _remove_service_record_for_pid(paths: BridgePaths, pid: int) -> bool:
    record = _load_service_record(paths)
    if record is None or record["pid"] != pid:
        return False
    try:
        paths.service_record.unlink()
    except FileNotFoundError:
        pass
    return True


def stop_direct_service(
    paths: BridgePaths,
    runner: ProcessRunner,
) -> bool:
    record = _load_service_record(paths)
    if record is None:
        if paths.service_record.exists():
            raise BridgeError(
                "直连服务记录存在但合同无效；拒绝按未知 PID 停止。"
            )
        return False
    pid = record["pid"]
    executable = runner.executable_for_pid(pid)
    if executable is None:
        return False
    if not _same_path(executable, paths.native_host):
        raise BridgeError("PID 对应进程不是固定直连代理；拒绝停止。")
    if not runner.terminate_exact(pid, paths.native_host.resolve()):
        raise BridgeError("直连代理未确认停止。")
    try:
        paths.service_record.unlink()
    except FileNotFoundError:
        pass
    return True


def disable_and_stop_direct_service(
    paths: BridgePaths,
    runner: ProcessRunner,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    recheck_delays: Sequence[float] = DISABLE_STOP_RECHECK_SECONDS,
) -> tuple[bool, bool]:
    if any(delay < 0 or delay > 1 for delay in recheck_delays):
        raise BridgeError("停用后的服务重查间隔无效。")
    disabled = disable_config(paths)
    if not disabled:
        raise BridgeError(
            "没有可原子停用的有效配置；拒绝先行停止服务。"
        )
    stopped = stop_direct_service(paths, runner)
    if stopped:
        return True, True

    # A bootstrap may have passed its first config read but not published its
    # PID record yet.  Its own post-commit opt-out check is the final invariant;
    # these bounded rechecks let the foreground GUI also observe and stop the
    # PID as soon as that record becomes visible.
    for delay in recheck_delays:
        sleeper(float(delay))
        if stop_direct_service(paths, runner):
            return True, True
    if paths.service_record.exists():
        raise BridgeError(
            "停用后仍存在 owned 服务记录，无法确认 listener 已停止。"
        )
    return True, False


def enumerate_microphones() -> list[Microphone]:
    # winreg is imported lazily so pure contract tests remain portable.  This
    # function only reads Active capture endpoints and never writes registry.
    try:
        import winreg
    except ImportError:
        return []
    capture_path = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
    )
    microphones: list[Microphone] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, capture_path) as root:
            index = 0
            while True:
                try:
                    endpoint_id = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, endpoint_id) as endpoint:
                        state = int(
                            winreg.QueryValueEx(endpoint, "DeviceState")[0]
                        )
                    if state != 1:
                        continue
                    labels: dict[str, str] = {}
                    with winreg.OpenKey(
                        root,
                        endpoint_id + r"\Properties",
                    ) as properties:
                        value_index = 0
                        while True:
                            try:
                                name, item, _ = winreg.EnumValue(
                                    properties,
                                    value_index,
                                )
                            except OSError:
                                break
                            value_index += 1
                            if isinstance(item, str) and item:
                                labels[name.rsplit(",", 1)[-1]] = item
                    microphones.append(
                        Microphone(
                            endpoint_id=endpoint_id,
                            friendly_name=(
                                labels.get("26")
                                or labels.get("6")
                                or endpoint_id
                            ),
                        )
                    )
                except OSError:
                    continue
    except OSError:
        return []
    microphones.sort(key=lambda item: item.display_name.casefold())
    return microphones


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class WindowsProcessRunner:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[Any]] = {}

    def start(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
    ) -> int:
        if not command:
            raise BridgeError("直连启动命令为空。")
        executable = _require_named_executable(
            Path(command[0]),
            "bw-computer-voice-audio.exe",
        )
        expected_config = (
            executable.parent / "computer-voice-direct.config.json"
        ).resolve()
        if (
            len(command) != 4
            or command[1] != "--direct-serve"
            or command[2] != "--config"
            or not _same_path(command[3], expected_config)
            or not _same_path(executable.parent, cwd)
        ):
            raise BridgeError("直连启动命令或目录偏离固定合同。")
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._children[process.pid] = process
        return process.pid

    def executable_for_pid(self, pid: int) -> Path | None:
        child = self._children.get(pid)
        if child is not None and child.poll() is not None:
            self._children.pop(pid, None)
            return None
        if os.name != "nt" or pid <= 0:
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            ):
                return None
            return Path(buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    def terminate_exact(self, pid: int, executable: Path) -> bool:
        if os.name != "nt" or pid <= 0:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION | self.PROCESS_TERMINATE,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            # Query and terminate through the same kernel handle.  Reopening by
            # PID after a path check permits PID reuse to redirect termination
            # to an unrelated process.
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            ):
                return False
            if not _same_path(Path(buffer.value), executable):
                return False
            terminated = bool(kernel32.TerminateProcess(handle, 0))
            if terminated:
                self._children.pop(pid, None)
            return terminated
        finally:
            kernel32.CloseHandle(handle)

    def wait_for_exit(self, pid: int) -> int:
        try:
            process = self._children[pid]
        except KeyError as error:
            raise BridgeError(
                "bootstrap 只能等待自己创建的直连代理。"
            ) from error
        try:
            return int(process.wait())
        finally:
            self._children.pop(pid, None)


class SubprocessReadOnlyRunner:
    def run_read_only(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )


def run_idle_bootstrap(
    paths: BridgePaths,
    runner: ProcessRunner,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    now_provider: Callable[[], datetime] = utc_now,
    max_cycles: int | None = None,
) -> int:
    if max_cycles is not None and max_cycles <= 0:
        raise BridgeError("bootstrap 测试循环上限无效。")
    restart_count = 0
    stable_polls = 0
    unresponsive_polls = 0
    cycles = 0

    while True:
        config = load_direct_config(paths)
        if config is None or config.get("localOptIn") is not True:
            return 0

        current = read_direct_status(
            paths,
            runner,
            now=now_provider(),
        )
        record = _load_service_record(paths)
        if paths.service_record.exists() and record is None:
            raise BridgeError(
                "bootstrap 发现无效服务记录；拒绝覆盖未知状态。"
            )

        if current.service_online:
            stable_polls += 1
            unresponsive_polls = 0
            if stable_polls >= SUPERVISOR_STABLE_POLLS:
                restart_count = 0
        else:
            stable_polls = 0
            observed: Path | None = None
            if record is not None:
                observed = runner.executable_for_pid(record["pid"])
                if (
                    observed is not None
                    and not _same_path(observed, paths.native_host)
                ):
                    raise BridgeError(
                        "bootstrap 发现服务记录 PID 属于陌生进程；"
                        "拒绝停止、覆盖或冒充。"
                    )
                if observed is not None:
                    unresponsive_polls += 1
                    if (
                        unresponsive_polls
                        >= SUPERVISOR_UNRESPONSIVE_POLLS
                    ):
                        pid = record["pid"]
                        terminated = runner.terminate_exact(
                            pid,
                            paths.native_host.resolve(),
                        )
                        if not terminated:
                            after = runner.executable_for_pid(pid)
                            if after is not None:
                                raise BridgeError(
                                    "精确服务连续无心跳且无法安全停止；"
                                    "拒绝启动第二实例。"
                                )
                        latest = _load_service_record(paths)
                        if latest is not None and latest["pid"] == pid:
                            try:
                                paths.service_record.unlink()
                            except FileNotFoundError:
                                pass
                        observed = None
                        unresponsive_polls = 0
                        restart_count = max(restart_count, 1)
            else:
                unresponsive_polls = 0

            if observed is None:
                unresponsive_polls = 0
                if restart_count:
                    delay = SUPERVISOR_RESTART_BACKOFF_SECONDS[
                        min(
                            restart_count - 1,
                            len(SUPERVISOR_RESTART_BACKOFF_SECONDS) - 1,
                        )
                    ]
                    sleeper(delay)
                    config = load_direct_config(paths)
                    if (
                        config is None
                        or config.get("localOptIn") is not True
                    ):
                        return 0
                try:
                    start_direct_service(
                        paths,
                        runner,
                        now=now_provider(),
                    )
                except LocalOptOutDuringStart:
                    return 0
                restart_count += 1

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return 0
        sleeper(SUPERVISOR_POLL_SECONDS)


def build_self_test_report(paths: BridgePaths) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    errors: list[str] = []

    def check(name: str, action: Callable[[], Any]) -> None:
        try:
            result = action()
            if result is False:
                raise BridgeError("check returned false")
        except Exception as error:
            checks[name] = False
            errors.append(f"{name}: {error}")
        else:
            checks[name] = True

    check("native-host-present", paths.native_host.is_file)
    check("desktop-launcher-present", paths.desktop_launcher.is_file)
    check(
        "fixed-native-host-path",
        lambda: paths.native_host.resolve()
        == (
            paths.root
            / "native-host"
            / "bw-computer-voice-audio.exe"
        ).resolve(),
    )
    check(
        "direct-config-contract",
        lambda: build_direct_config(
            "{self-test-endpoint}",
            paths.runtime_status,
        ),
    )
    check(
        "pairing-hash-contract",
        lambda: pairing_material_from_code(
            "ABCDEFGHJK",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    report = {
        "contract": SELF_TEST_CONTRACT,
        "ok": all(checks.values()),
        "checks": checks,
        "errors": errors,
        "writesToBridgeConfiguration": False,
        "serviceStarted": False,
        "audioOpened": False,
        "applicationStarted": False,
        "typistStarted": False,
        "shortcutSent": False,
        "taskRegistered": False,
        "registryWritten": False,
        "tailscaleServeChanged": False,
        "browserOpened": False,
    }
    return report
