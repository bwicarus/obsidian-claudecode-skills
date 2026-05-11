"""开机自启 — 写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 注册表。

写到 HKCU 不需要管理员权限，每次用户登录时自动启动。
卸载客户端时若 .exe 路径变了，旧注册项指向无效路径，无害但建议手动清掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

_KEY_PATH  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "BwicarusClient"


def _exe_path() -> str | None:
    """返回当前 .exe 的绝对路径；源码模式返回 None（启动项需要 .exe 才有意义）。"""
    if not getattr(sys, "frozen", False):
        return None
    return sys.executable


def is_enabled() -> bool:
    if not _exe_path():
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, _VALUE_NAME)
            return bool(value)
    except (FileNotFoundError, OSError):
        return False


def enable() -> tuple[bool, str]:
    exe = _exe_path()
    if not exe:
        return False, "源码模式不支持开机自启（要 .exe）"
    try:
        import winreg
    except ImportError:
        return False, "Windows 注册表不可用"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, f'"{exe}"')
        return True, f"已开启开机自启：{Path(exe).name}"
    except Exception as e:
        return False, f"写入注册表失败：{e}"


def disable() -> tuple[bool, str]:
    try:
        import winreg
    except ImportError:
        return True, "Windows 注册表不可用"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
        return True, "已关闭开机自启"
    except FileNotFoundError:
        return True, "本来就没设置"
    except Exception as e:
        return False, f"删除注册表项失败：{e}"
