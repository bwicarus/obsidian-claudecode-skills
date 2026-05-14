"""跨平台子进程辅助。

主项目最初为 Windows 写，迁服务器（Linux）时若干 scripts 仍在用
`subprocess.CREATE_NO_WINDOW` / `STARTUPINFO` 这类 Windows-only 属性 ——
未守卫的会 AttributeError，守卫的散落 `getattr(subprocess, ..., 0)` 风格不一。

这里统一封装一次，调用方：

    from platform_utils import WINDOWS, hidden_run, hidden_popen, NO_WINDOW_KW

    hidden_run(["foo", "bar"], capture_output=True, text=True)
    hidden_popen([...], stdout=logf, stderr=subprocess.STDOUT)
    subprocess.run([...], **NO_WINDOW_KW, **other_kwargs)
"""
from __future__ import annotations

import subprocess
import sys

WINDOWS = sys.platform == "win32"


def _no_window_kwargs() -> dict:
    """Windows 上返回 {creationflags, startupinfo} 用于隐藏控制台窗口；Linux 上返回空 dict。"""
    if not WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": si,
    }


# 模块加载时计算一次。注意 STARTUPINFO 是 mutable 的，但我们只读不写——
# 调用方拿到的是 reference，他们不应该改 si 字段。如果有疑虑用 hidden_run/hidden_popen。
NO_WINDOW_KW: dict = _no_window_kwargs()


def hidden_run(cmd, **kwargs):
    """subprocess.run 但在 Windows 上隐藏控制台窗口（Linux 是裸调用）。

    支持调用方通过 kwargs 覆盖 creationflags / startupinfo。
    """
    merged = {**_no_window_kwargs(), **kwargs}
    return subprocess.run(cmd, **merged)


def hidden_popen(cmd, **kwargs):
    """subprocess.Popen 但在 Windows 上隐藏控制台窗口（Linux 是裸调用）。"""
    merged = {**_no_window_kwargs(), **kwargs}
    return subprocess.Popen(cmd, **merged)


def is_systemd_service_active(service: str) -> bool:
    """Linux 检查 systemd unit 是否 active；Windows 永远返回 False。"""
    if WINDOWS:
        return False
    try:
        r = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False
