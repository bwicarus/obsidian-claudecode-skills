"""全局热键注册（用 keyboard 库的 hook 模式，user-mode 即可，不需要管理员）。

GUI 在主线程调 register(callback)；按下绑定键时 callback 在 keyboard 库的子线程被调。
注意：callback 里如果要操作 tkinter UI，记得 root.after(0, ...) 切回主线程。
"""
from __future__ import annotations

from typing import Callable

try:
    import keyboard  # type: ignore
    _AVAILABLE = True
except Exception:
    keyboard = None  # type: ignore
    _AVAILABLE = False


_registered: dict[str, object] = {}   # combo -> handle


def is_available() -> bool:
    return _AVAILABLE


def register(combo: str, callback: Callable[[], None]) -> tuple[bool, str]:
    """注册全局热键。combo 形如 'ctrl+shift+q'；同一 combo 重复注册会先取消旧的。"""
    if not _AVAILABLE:
        return False, "keyboard 库不可用，全局热键功能禁用"
    combo = (combo or "").strip().lower()
    if not combo:
        return False, "未指定快捷键"
    # 取消旧绑定
    old = _registered.pop(combo, None)
    if old is not None:
        try:
            keyboard.remove_hotkey(old)
        except Exception:
            pass
    try:
        h = keyboard.add_hotkey(combo, callback, suppress=False)
        _registered[combo] = h
        return True, f"全局热键已绑定：{combo}"
    except Exception as e:
        return False, f"绑定失败：{e}"


def unregister(combo: str) -> None:
    if not _AVAILABLE:
        return
    h = _registered.pop((combo or "").strip().lower(), None)
    if h is not None:
        try:
            keyboard.remove_hotkey(h)
        except Exception:
            pass


def unregister_all() -> None:
    if not _AVAILABLE:
        return
    for combo, h in list(_registered.items()):
        try:
            keyboard.remove_hotkey(h)
        except Exception:
            pass
    _registered.clear()


def record(timeout: float = 10.0) -> tuple[bool, str]:
    """阻塞捕获一次组合键，返回 (ok, combo_or_error)。
    超时返回 False。建议在子线程跑，调用前先把已注册热键 unregister 避免冲突。
    """
    if not _AVAILABLE:
        return False, "keyboard 库不可用"
    try:
        # keyboard.read_hotkey 阻塞直到一个完整 hotkey 被按下并松开
        combo = keyboard.read_hotkey(suppress=False)
        if not combo:
            return False, "未捕获到组合键"
        return True, combo
    except Exception as e:
        return False, f"录制失败：{e}"
