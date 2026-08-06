#!/usr/bin/env python3
"""Local-only voice-typist helper for the native computer-voice host.

The server cannot choose an action or path.  This helper can idempotently
ensure the existing, fixed-path voice-typist is running, or release only the
exact PID lease returned when this bridge started it.  Both operations reuse
the supervisor's strict fixed-path/session/process verification.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Callable

from bw_computer_voice_supervisor import (
    CONTRACT,
    DEFAULT_TYPIST_TARGET_APP,
    SupervisorError,
    VoiceTypistLauncher,
    require_typist_target_app,
)


def _default_stop_runner(
    launcher_path: Path,
    expected_pid: int,
    expected_start_file_time_utc: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher_path),
            "-Action",
            "Stop",
            "-ExpectedPid",
            str(expected_pid),
            "-ExpectedStartFileTimeUtc",
            str(expected_start_file_time_utc),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=20.0,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def stop_if_owned(
    expected_pid: int,
    expected_start_file_time_utc: int,
    *,
    launcher: VoiceTypistLauncher | None = None,
    stop_runner: Callable[
        [Path, int, int], subprocess.CompletedProcess[str]
    ] = _default_stop_runner,
) -> dict[str, object]:
    if isinstance(expected_pid, bool) or expected_pid <= 0:
        raise SupervisorError(
            "BW_COMPUTER_VOICE_TYPIST_LEASE_INVALID",
            "voice-typist lease PID 无效",
        )
    if (
        isinstance(expected_start_file_time_utc, bool)
        or expected_start_file_time_utc <= 0
    ):
        raise SupervisorError(
            "BW_COMPUTER_VOICE_TYPIST_LEASE_INVALID",
            "voice-typist lease 启动时间无效",
        )
    controller = launcher or VoiceTypistLauncher()
    before = controller.verified_status()
    if not before.running:
        return {
            "running": False,
            "stopped": False,
            "result": "already-stopped",
            "expectedPid": expected_pid,
            "expectedStartFileTimeUtc": expected_start_file_time_utc,
        }
    if (
        before.pid != expected_pid
        or before.process_start_file_time_utc
            != expected_start_file_time_utc
    ):
        raise SupervisorError(
            "BW_COMPUTER_VOICE_TYPIST_LEASE_MISMATCH",
            "voice-typist 当前进程代次与桥接器 lease 不一致，拒绝停止",
        )

    completed = stop_runner(
        controller.launcher_path,
        expected_pid,
        expected_start_file_time_utc,
    )
    if completed.returncode != 0:
        raise SupervisorError(
            "BW_COMPUTER_VOICE_TYPIST_STOP_FAILED",
            "voice-typist launcher Stop 失败",
        )
    after = controller.verified_status()
    if after.running:
        raise SupervisorError(
            "BW_COMPUTER_VOICE_TYPIST_STOP_FAILED",
            "voice-typist launcher Stop 后置条件未成立",
        )
    return {
        "running": False,
        "stopped": True,
        "result": "stopped",
        "expectedPid": expected_pid,
        "expectedStartFileTimeUtc": expected_start_file_time_utc,
    }


def _parse_positive_integer(value: str, label: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise SupervisorError(
            "BW_COMPUTER_VOICE_TYPIST_LEASE_INVALID",
            f"voice-typist lease {label} 无效",
        )
    pid = int(value, 10)
    if pid <= 0:
        raise SupervisorError(
            "BW_COMPUTER_VOICE_TYPIST_LEASE_INVALID",
            f"voice-typist lease {label} 无效",
        )
    return pid


def _write(payload: dict[str, object]) -> None:
    print(json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ))


def main(argv: list[str]) -> int:
    try:
        if len(argv) in {3, 4} and argv[0] == "--ensure-running":
            target_app_kind = (
                require_typist_target_app(argv[3])
                if len(argv) == 4
                else DEFAULT_TYPIST_TARGET_APP
            )
            result = VoiceTypistLauncher().ensure_running(
                _parse_positive_integer(argv[1], "owner PID"),
                _parse_positive_integer(argv[2], "owner 启动时间"),
                target_app_kind,
            )
        elif len(argv) == 3 and argv[0] == "--stop-if-owned":
            result = stop_if_owned(
                _parse_positive_integer(argv[1], "PID"),
                _parse_positive_integer(argv[2], "启动时间"),
            )
        else:
            _write({
                "contract": CONTRACT,
                "ok": False,
                "code": "BW_COMPUTER_VOICE_ACTION_DENIED",
            })
            return 64
    except SupervisorError as error:
        _write({
            "contract": CONTRACT,
            "ok": False,
            "code": error.code,
            "error": str(error),
        })
        return 1
    _write({
        "contract": CONTRACT,
        "ok": True,
        **result,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
