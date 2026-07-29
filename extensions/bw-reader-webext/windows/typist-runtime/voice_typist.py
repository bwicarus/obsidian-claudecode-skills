#!/usr/bin/env python3
"""Codex Voice typist: the last hop the App Server API cannot cover.

The reader bridge already authenticates, pulls, de-duplicates and formats
context events.  What it cannot do is put one of those events into the Voice
conversation that is *actually running* in the desktop app -- `thread/inject_items`
returns success against a standalone app-server process that the desktop Voice
runtime never reads.

This component takes one already-formatted event and submits it as a single text
message to one explicitly designated Codex conversation, using the clipboard and
synthesized input.  Every step is verified against what the app really shows:

  * the target window is resolved uniquely, brought to the foreground, and the
    click point is checked for occlusion;
  * the conversation identity is read back with the app's own
    "Copy session id" command -- not guessed from a window title;
  * the composer is proven empty before pasting, and proven to contain exactly
    the payload after pasting, by copying its content back out;
  * submission uses the app's dedicated "Send the current composer message"
    command, never a bare Enter that might land somewhere else;
  * after submitting, the chat is copied back as Markdown and searched for the
    event id, so "delivered" means visible in the conversation.

Anything that cannot be verified fails closed and is logged with a distinct
reason.  A local audit record is not evidence that the model understood the
message; only the transcript check is.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

INSTALL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = INSTALL_DIR / "voice-typist.config.json"
DEFAULT_LOG = INSTALL_DIR / "logs" / "voice-typist.jsonl"
DEFAULT_STATE_DIR = INSTALL_DIR / "state"
INJECTOR_PATH = INSTALL_DIR / "reader-context-injector.py"

MARKER = "[[READER_SYNC]]"
MARKER_END = "[[/READER_SYNC]]"
SENTINEL_PREFIX = "\u2400voice-typist-probe\u2400"

if os.name != "nt":  # pragma: no cover - the component is Windows-only by design
    raise SystemExit("voice_typist requires Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)


# --------------------------------------------------------------------------
# Win32 plumbing
# --------------------------------------------------------------------------

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SW_RESTORE, GA_ROOT = 9, 2
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
DWMWA_CLOAKED = 14
# Marks input this component synthesized, so a future reader of an input log can
# tell our keystrokes from the user's.
INPUT_SIGNATURE = 0x52435458  # 'RCTX'


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


# Every HWND/HANDLE crosses this boundary as a 64-bit value.  Without explicit
# prototypes ctypes narrows them to C int and a valid handle turns into an
# OverflowError or, worse, a silently truncated one.
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
user32.MapVirtualKeyW.restype = wt.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND

user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.EnumClipboardFormats.argtypes = [wt.UINT]
user32.EnumClipboardFormats.restype = wt.UINT
user32.GetClipboardSequenceNumber.restype = wt.DWORD

kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.restype = wt.HGLOBAL
kernel32.GetCurrentThreadId.restype = wt.DWORD
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]


def set_dpi_awareness() -> str:
    """Physical pixels everywhere, so window rects and click points agree."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per_monitor_v2"
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return "per_monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


VK = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "escape": 0x1B, "esc": 0x1B, "space": 0x20,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i
for _c in "0123456789":
    VK[_c] = ord(_c)
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())

EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
                0x5B, 0x5C, 0x5D, 0x90, 0x2C}
MODIFIER_VKS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
                "win": 0x5B, "lwin": 0x5B}


class HotkeyError(ValueError):
    pass


def parse_hotkey(spec: str) -> Tuple[List[int], int]:
    """'ctrl+alt+c' -> ([VK_CONTROL, VK_MENU], VK_C).  Unknown names fail loudly."""
    if not isinstance(spec, str) or not spec.strip():
        raise HotkeyError("empty hotkey")
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"unparsable hotkey: {spec!r}")
    mods: List[int] = []
    for part in parts[:-1]:
        if part not in MODIFIER_VKS:
            raise HotkeyError(f"unknown modifier {part!r} in {spec!r}")
        mods.append(MODIFIER_VKS[part])
    key = parts[-1]
    if key not in VK:
        raise HotkeyError(f"unknown key {key!r} in {spec!r}")
    return mods, VK[key]


def _key_input(vk: int, up: bool) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    return INPUT(type=INPUT_KEYBOARD,
                 ki=KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0,
                               dwExtraInfo=INPUT_SIGNATURE))


def send_inputs(items: List[INPUT]) -> None:
    if not items:
        return
    arr = (INPUT * len(items))(*items)
    sent = user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))
    if sent != len(items):
        raise OSError(f"SendInput delivered {sent}/{len(items)} "
                      f"(err {ctypes.get_last_error()})")


def press(spec: str, settle: float = 0.03) -> None:
    mods, key = parse_hotkey(spec)
    seq = [_key_input(m, False) for m in mods]
    seq.append(_key_input(key, False))
    seq.append(_key_input(key, True))
    seq.extend(_key_input(m, True) for m in reversed(mods))
    send_inputs(seq)
    time.sleep(settle)


def click(x: int, y: int, settle: float = 0.12) -> None:
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    ax = int(round((x - vx) * 65535 / vw))
    ay = int(round((y - vy) * 65535 / vh))
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    move = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0,
                                                 dwFlags=flags, time=0,
                                                 dwExtraInfo=INPUT_SIGNATURE))
    down = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0,
                                                 dwFlags=flags | MOUSEEVENTF_LEFTDOWN,
                                                 time=0, dwExtraInfo=INPUT_SIGNATURE))
    up = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0,
                                               dwFlags=flags | MOUSEEVENTF_LEFTUP,
                                               time=0, dwExtraInfo=INPUT_SIGNATURE))
    send_inputs([move])
    time.sleep(0.03)
    send_inputs([down, up])
    time.sleep(settle)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND),
                ("rcCaret", wt.RECT)]


user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]
imm32 = ctypes.WinDLL("imm32", use_last_error=True)
imm32.ImmGetDefaultIMEWnd.argtypes = [wt.HWND]
imm32.ImmGetDefaultIMEWnd.restype = wt.HWND

WM_IME_CONTROL = 0x0283
IMC_GETOPENSTATUS, IMC_SETOPENSTATUS = 0x0005, 0x0006


def focused_hwnd(hwnd: int) -> int:
    """The control with keyboard focus inside the target window's thread."""
    tid = user32.GetWindowThreadProcessId(wt.HWND(hwnd), None)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
        return int(info.hwndFocus)
    return hwnd


class ImeGuard:
    """Turn the IME off around a sequence, then put it back.

    On a Chinese-locale machine the IME swallows Enter and every character key
    into a composition, so an injected message would be silently rewritten or
    never submitted.  The IME window is asked over WM_IME_CONTROL, which works
    across process boundaries where the Imm* context calls do not.
    """

    def __init__(self, hwnd: int) -> None:
        self.ime_hwnd = int(imm32.ImmGetDefaultIMEWnd(wt.HWND(focused_hwnd(hwnd))) or 0)
        self.previous: Optional[int] = None

    @property
    def available(self) -> bool:
        return bool(self.ime_hwnd)

    def open_status(self) -> Optional[int]:
        if not self.ime_hwnd:
            return None
        return int(user32.SendMessageW(wt.HWND(self.ime_hwnd), WM_IME_CONTROL,
                                       IMC_GETOPENSTATUS, 0))

    def __enter__(self) -> "ImeGuard":
        if self.ime_hwnd:
            self.previous = self.open_status()
            if self.previous:
                user32.SendMessageW(wt.HWND(self.ime_hwnd), WM_IME_CONTROL,
                                    IMC_SETOPENSTATUS, 0)
                time.sleep(0.12)
        return self

    def __exit__(self, *exc) -> None:
        if self.ime_hwnd and self.previous:
            user32.SendMessageW(wt.HWND(self.ime_hwnd), WM_IME_CONTROL,
                                IMC_SETOPENSTATUS, 1)


def cursor_pos() -> Tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def release_modifiers() -> None:
    """Never leave a modifier stuck down if a sequence aborts mid-way."""
    seq = []
    for vk in (0x11, 0x12, 0x10, 0x5B, 0x5C):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            seq.append(_key_input(vk, True))
    if seq:
        try:
            send_inputs(seq)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Clipboard
# --------------------------------------------------------------------------

class ClipboardError(RuntimeError):
    pass


class Clipboard:
    """Minimal Win32 clipboard access with retries and hijack detection."""

    @staticmethod
    def sequence() -> int:
        return int(user32.GetClipboardSequenceNumber())

    @staticmethod
    def _open(timeout: float = 1.5) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if user32.OpenClipboard(None):
                return
            time.sleep(0.03)
        raise ClipboardError("clipboard is locked by another process")

    @classmethod
    def formats(cls) -> List[int]:
        cls._open()
        try:
            out, fmt = [], 0
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                out.append(int(fmt))
            return out
        finally:
            user32.CloseClipboard()

    @classmethod
    def get_text(cls) -> Optional[str]:
        cls._open()
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(wt.HGLOBAL(handle))
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(wt.HGLOBAL(handle))
        finally:
            user32.CloseClipboard()

    @classmethod
    def set_text(cls, text: str) -> None:
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        cls._open()
        try:
            if not user32.EmptyClipboard():
                raise ClipboardError("EmptyClipboard failed")
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                raise ClipboardError("GlobalAlloc failed")
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                raise ClipboardError("GlobalLock failed")
            ctypes.memmove(ptr, ctypes.byref(data), size)
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                raise ClipboardError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()

    @classmethod
    def clear(cls) -> None:
        cls._open()
        try:
            user32.EmptyClipboard()
        finally:
            user32.CloseClipboard()


@dataclass
class ClipboardBackup:
    """Best-effort save/restore.  Non-text formats cannot be round-tripped, so
    the caller is told rather than silently losing them."""
    text: Optional[str]
    had_other_formats: bool

    @classmethod
    def capture(cls) -> "ClipboardBackup":
        try:
            fmts = Clipboard.formats()
            text = Clipboard.get_text()
        except ClipboardError:
            return cls(text=None, had_other_formats=False)
        other = any(f not in (CF_UNICODETEXT, 1, 7, 16) for f in fmts)
        return cls(text=text, had_other_formats=other)

    def restore(self) -> bool:
        try:
            if self.text is None:
                Clipboard.clear()
            else:
                Clipboard.set_text(self.text)
            return True
        except ClipboardError:
            return False


# --------------------------------------------------------------------------
# Window targeting
# --------------------------------------------------------------------------

EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    cls: str
    pid: int
    rect: Tuple[int, int, int, int]  # left, top, right, bottom


class WindowError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _process_name(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).stem
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _is_cloaked(hwnd: int) -> bool:
    value = ctypes.c_int(0)
    if dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), DWMWA_CLOAKED,
                                    ctypes.byref(value), ctypes.sizeof(value)) == 0:
        return bool(value.value)
    return False


def enumerate_windows() -> List[WindowInfo]:
    found: List[WindowInfo] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append(WindowInfo(int(hwnd), title.value, cls.value, int(pid.value),
                                (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    user32.EnumWindows(EnumProc(_cb), 0)
    return found


def resolve_target(cfg: Dict[str, Any]) -> WindowInfo:
    """Exactly one match, or fail.  Ambiguity is never resolved by guessing."""
    want_proc = str(cfg.get("process_name") or "").lower()
    want_cls = cfg.get("window_class") or None
    pattern = cfg.get("window_title_regex")
    regex = re.compile(pattern) if pattern else None
    pinned = cfg.get("hwnd")

    matches: List[WindowInfo] = []
    for win in enumerate_windows():
        if want_cls and win.cls != want_cls:
            continue
        if regex and not regex.search(win.title):
            continue
        if want_proc and _process_name(win.pid).lower() != want_proc:
            continue
        if _is_cloaked(win.hwnd):
            continue
        if win.rect[2] - win.rect[0] < 200 or win.rect[3] - win.rect[1] < 200:
            continue  # popouts and mini control windows are not the chat window
        matches.append(win)

    if pinned:
        matches = [m for m in matches if m.hwnd == int(pinned)] or []
        if not matches:
            raise WindowError("window_not_found",
                              f"pinned hwnd {pinned} is not a visible target window")
    if not matches:
        raise WindowError("window_not_found",
                          "no visible window matched the configured target")
    if len(matches) > 1:
        detail = ", ".join(f"{m.hwnd}:{m.title!r}" for m in matches)
        raise WindowError("window_ambiguous",
                          f"{len(matches)} windows matched the target: {detail}")
    return matches[0]


def force_foreground(hwnd: int, timeout: float = 2.0, attempts: int = 3) -> None:
    """Raise the window, then confirm it really is foreground.

    Windows refuses SetForegroundWindow from a process that does not own the
    current foreground; attaching to that thread's input queue lifts the lock,
    and a stray ALT tap clears it in the cases attaching does not.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    deadline = time.time() + timeout
    for attempt in range(attempts):
        if user32.GetForegroundWindow() == hwnd:
            return
        fg = user32.GetForegroundWindow()
        me = kernel32.GetCurrentThreadId()
        other = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(other) and bool(user32.AttachThreadInput(me, other, True))
        try:
            if attempt and not attached:
                send_inputs([_key_input(0x12, False), _key_input(0x12, True)])
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, other, False)
        while time.time() < deadline:
            if user32.GetForegroundWindow() == hwnd:
                return
            time.sleep(0.05)
            break
        time.sleep(0.1)
    while time.time() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return
        time.sleep(0.05)
    raise WindowError("window_not_foreground",
                      "target window did not become foreground")


def point_is_ours(hwnd: int, x: int, y: int) -> bool:
    """Whatever sits under the click point must belong to the target window."""
    pt = wt.POINT(x, y)
    hit = user32.WindowFromPoint(pt)
    if not hit:
        return False
    root = user32.GetAncestor(hit, GA_ROOT)
    return int(root) == int(hwnd)


# --------------------------------------------------------------------------
# Configuration and runtime state
# --------------------------------------------------------------------------

DEFAULT_CONFIG_BODY: Dict[str, Any] = {
    "target": {
        "process_name": "ChatGPT",
        "window_class": "Chrome_WidgetWin_1",
        "window_title_regex": "^(ChatGPT|Codex)",
        "hwnd": None,
        # "follow_active": deliver to whatever conversation is open, so a fresh
        # voice chat needs no reconfiguration.  "pinned": refuse anything but
        # target.session_id.  Either way the id must be readable, and every
        # submission records which conversation received it.
        "session_mode": "follow_active",
        "session_id": None,
        "composer_anchor": None,
    },
    "hotkeys": {
        "copy_session_id": "ctrl+alt+c",
        # Enter, not a click on the send button: clicking inside the app ends the
        # running voice chat.  Enter is only ever pressed after the composer has
        # been proven to hold exactly the payload.
        "send_message": "enter",
        "copy_chat_markdown": None,
        "refocus_composer": None,
    },
    "input": {
        # Mouse input anywhere in the app stops the active voice chat, so the
        # composer is never clicked.  Focus is proven by reading the field back,
        # not assumed from a click.
        "focus_click": False,
    },
    # after_paste and after_submit are *deadlines*, not sleeps: the sequence
    # polls for the condition and moves on as soon as it holds.  Raise them if a
    # slow machine starts reporting paste_verification_failed / submit_failed.
    "timing": {
        "after_focus": 0.12,
        "after_click": 0.25,
        "after_paste": 1.50,
        "after_submit": 2.50,
        "clipboard_wait": 1.5,
    },
    "limits": {
        "max_payload_chars": 6000,
        "min_submit_interval_seconds": 2.0,
        "max_retries": 2,
        "queue_size": 32,
        "rejection_cooldown_seconds": 15.0,
        # How long one selection/page/drawing must stop changing before its
        # latest state is submitted.
        "coalesce_settle_seconds": 1.5,
    },
    "verification": {
        # "rollout" reads the app's own conversation record and needs no hotkey;
        # "markdown_hotkey" copies the chat back through the clipboard;
        # "none" reports delivery as unverified.
        "method": "rollout",
        "rollout_root": str(Path.home() / ".codex" / "sessions"),
        "timeout_seconds": 10.0,
    },
    "safety": {
        "allow_enter_fallback": False,
        "require_transcript_verification": False,
        "restore_clipboard": True,
    },
}


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"config not found: {path}\nRun with --init-config first.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    merged = json.loads(json.dumps(DEFAULT_CONFIG_BODY))
    for section, values in raw.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def save_config(path: Path, cfg: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class RunState:
    """Enable/pause/emergency-stop, expressed as files so any process can flip
    them and the launcher can report them without talking to the typist."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.pause_file = root / "PAUSED"
        self.stop_file = root / "EMERGENCY_STOP"
        self.status_file = root / "status.json"

    @property
    def paused(self) -> bool:
        return self.pause_file.exists()

    @property
    def stopped(self) -> bool:
        return self.stop_file.exists()

    def pause(self) -> None:
        self.pause_file.write_text(now_ts(), encoding="utf-8")

    def resume(self) -> None:
        self.pause_file.unlink(missing_ok=True)

    def emergency_stop(self, reason: str = "manual") -> None:
        self.stop_file.write_text(json.dumps({"at": now_ts(), "reason": reason}),
                                  encoding="utf-8")

    def clear_stop(self) -> None:
        self.stop_file.unlink(missing_ok=True)

    def write_status(self, payload: Dict[str, Any]) -> None:
        body = dict(payload)
        body["at"] = now_ts()
        body["paused"] = self.paused
        body["emergency_stop"] = self.stopped
        tmp = self.status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.status_file)


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: Dict[str, Any]) -> None:
        body = dict(record)
        body.setdefault("at", now_ts())
        line = json.dumps(body, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


# --------------------------------------------------------------------------
# Payload safety
# --------------------------------------------------------------------------

# Never route credentials or bulk private data through the clipboard, whatever
# the upstream journal happened to include.
SECRET_PATTERNS = [
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.I),
    re.compile(r"\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|credential)\b\s*[\"':=]\s*\S{6,}", re.I),
    re.compile(r"\bAuthorization\b\s*[\"':=]", re.I),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),  # JWT
]
# A drawing event must carry a page/revision/reference, never the raw strokes.
BULK_PATTERNS = [
    re.compile(r"\"(strokes|points|path_data|stroke_points)\"\s*:\s*\[", re.I),
]


class PayloadRejected(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def check_payload(text: str, max_chars: int) -> None:
    if not text.strip():
        raise PayloadRejected("payload_empty", "refusing to submit empty text")
    if len(text) > max_chars:
        raise PayloadRejected(
            "payload_too_large",
            f"payload is {len(text)} chars, limit is {max_chars}")
    if MARKER not in text:
        raise PayloadRejected(
            "payload_unmarked",
            f"payload does not carry the {MARKER} marker")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise PayloadRejected("payload_contains_secret",
                                  "payload matched a credential pattern")
    for pattern in BULK_PATTERNS:
        if pattern.search(text):
            raise PayloadRejected("payload_contains_bulk_data",
                                  "payload matched a bulk-geometry pattern")


# --------------------------------------------------------------------------
# The typist
# --------------------------------------------------------------------------

class RolloutVerifier:
    """Confirm delivery against the app's own conversation record.

    Codex persists each conversation as
    ``sessions/<date>/rollout-<stamp>-<session_id>.jsonl``.  Searching that file
    for the event id proves the message became part of the conversation, and
    unlike a "copy chat as Markdown" hotkey it depends on no key binding at all.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: Dict[str, Path] = {}

    def find(self, session_id: str) -> Optional[Path]:
        cached = self._cache.get(session_id)
        if cached is not None and cached.exists():
            return cached
        if not self.root.exists():
            return None
        matches = sorted(self.root.glob(f"**/rollout-*-{session_id}.jsonl"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return None
        self._cache[session_id] = matches[0]
        return matches[0]

    @staticmethod
    def _read_tail(path: Path, max_bytes: int = 2_000_000) -> str:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read().decode("utf-8", errors="replace")

    def contains(self, session_id: str, needle: str,
                 timeout: float) -> Tuple[Optional[bool], Optional[Path]]:
        """(True | False | None, path).  None means the record was never found."""
        deadline = time.time() + timeout
        path: Optional[Path] = None
        while True:
            path = path or self.find(session_id)
            if path is not None:
                try:
                    if needle in self._read_tail(path):
                        return True, path
                except OSError:
                    pass
            if time.time() >= deadline:
                break
            time.sleep(0.15)
        return (False if path is not None else None), path


@dataclass
class SubmitResult:
    ok: bool
    outcome: str
    detail: str = ""
    session_id: Optional[str] = None
    hwnd: Optional[int] = None
    verified_in_conversation: Optional[bool] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "outcome": self.outcome, "detail": self.detail,
                "session": _short_session(self.session_id), "hwnd": self.hwnd,
                "verified_in_conversation": self.verified_in_conversation}


def _short_session(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    value = value.strip()
    return value if len(value) <= 20 else f"{value[:8]}…{value[-6:]}"


@dataclass
class PendingItem:
    text: str
    event_id: str
    event_type: str
    source: str
    attempts: int = 0
    # Events describing the same thing (one selection, one page, one drawing)
    # share a key so only the settled last one is ever submitted.
    coalesce_key: Optional[str] = None
    queued_at: float = 0.0


class VoiceTypist:
    def __init__(self, cfg: Dict[str, Any], log: AuditLog, state: RunState,
                 dry_run: bool = False) -> None:
        self.cfg = cfg
        self.log = log
        self.state = state
        self.dry_run = dry_run
        self._last_submit = 0.0
        # Set when the app refuses a post because no turn is live.  Retrying
        # immediately would only stack error toasts; the condition clears itself
        # when the user speaks again.
        self._rejected_until = 0.0
        self._phase_ms: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _mark(self, phase: str, since: float) -> float:
        self._phase_ms[phase] = int((time.time() - since) * 1000)
        return time.time()

    # -- small helpers ----------------------------------------------------
    def _t(self, key: str) -> float:
        return float(self.cfg["timing"][key])

    def _hotkey(self, name: str) -> Optional[str]:
        value = self.cfg["hotkeys"].get(name)
        return value if isinstance(value, str) and value.strip() else None

    def _abort_if_halted(self) -> None:
        if self.state.stopped:
            raise WindowError("emergency_stop", "emergency stop is engaged")
        if self.state.paused:
            raise WindowError("paused", "component is paused")

    def _copy_via(self, hotkey: str, label: str) -> Optional[str]:
        """Press a copy command and return the text it produced, or None.

        A sentinel is planted first so an unchanged clipboard is unambiguous:
        the app copied nothing, rather than copying something identical.
        """
        sentinel = f"{SENTINEL_PREFIX}{label}:{time.time_ns()}"
        Clipboard.set_text(sentinel)
        before = Clipboard.sequence()
        press(hotkey)
        deadline = time.time() + self._t("clipboard_wait")
        while time.time() < deadline:
            if Clipboard.sequence() != before:
                time.sleep(0.02)
                value = Clipboard.get_text()
                if value is not None and value != sentinel:
                    return value
                return None
            time.sleep(0.015)
        return None

    def _read_composer(self) -> Optional[str]:
        """Select all in the focused field and copy it back out."""
        press("ctrl+a", settle=0.05)
        return self._copy_via("ctrl+c", "composer")

    def _read_composer_until(self, predicate, timeout: float,
                             first_delay: float = 0.08):
        """Poll the composer until it looks right, instead of sleeping the worst
        case every time.  Returns (matched, last_value)."""
        time.sleep(first_delay)
        deadline = time.time() + timeout
        value = None
        while True:
            value = self._read_composer()
            if predicate(value):
                return True, value
            if time.time() >= deadline:
                return False, value
            time.sleep(0.05)

    # -- the sequence -----------------------------------------------------
    def submit(self, text: str, event_id: str, event_type: str = "",
               source: str = "") -> SubmitResult:
        with self._lock:
            return self._submit_locked(text, event_id, event_type, source)

    def _submit_locked(self, text: str, event_id: str, event_type: str,
                       source: str) -> SubmitResult:
        started = time.time()
        base = {"event": "submit", "event_id": event_id, "event_type": event_type,
                "source": source, "payload_sha256": sha256_text(text),
                "payload_chars": len(text)}

        self._phase_ms = {}

        def done(result: SubmitResult) -> SubmitResult:
            record = dict(base)
            record.update(result.as_dict())
            record["elapsed_ms"] = int((time.time() - started) * 1000)
            # Which stage actually costs the time: our keystrokes, or the app
            # taking its time to accept and persist the message.
            if self._phase_ms:
                record["phase_ms"] = dict(self._phase_ms)
            self.log.write(record)
            return result

        try:
            check_payload(text, int(self.cfg["limits"]["max_payload_chars"]))
        except PayloadRejected as exc:
            return done(SubmitResult(False, exc.reason, str(exc)))

        send_hotkey = self._hotkey("send_message")
        if send_hotkey is None:
            if not self.cfg["safety"].get("allow_enter_fallback"):
                return done(SubmitResult(
                    False, "submit_hotkey_missing",
                    "hotkeys.send_message is unset; bind Codex's "
                    "\"Send the current composer message\" command and put it "
                    "in the config, or set safety.allow_enter_fallback"))
            send_hotkey = "enter"
        for name in ("copy_session_id", "send_message", "copy_chat_markdown"):
            spec = self._hotkey(name)
            if spec is not None:
                try:
                    parse_hotkey(spec)
                except HotkeyError as exc:
                    return done(SubmitResult(False, "hotkey_invalid", str(exc)))

        try:
            self._abort_if_halted()
        except WindowError as exc:
            return done(SubmitResult(False, exc.reason, str(exc)))

        cooldown = self._rejected_until - time.time()
        if cooldown > 0:
            return done(SubmitResult(
                False, "waiting_for_live_turn",
                f"the app refused a post {int(cooldown)}s ago because no turn was "
                f"live; holding off before trying again"))

        interval = float(self.cfg["limits"]["min_submit_interval_seconds"])
        wait = self._last_submit + interval - time.time()
        if wait > 0:
            time.sleep(wait)

        try:
            window = resolve_target(self.cfg["target"])
        except WindowError as exc:
            return done(SubmitResult(False, exc.reason, str(exc)))

        point: Optional[Tuple[int, int]] = None
        if self.cfg.get("input", {}).get("focus_click"):
            anchor = self.cfg["target"].get("composer_anchor")
            if not isinstance(anchor, dict) or "x_frac" not in anchor:
                return done(SubmitResult(
                    False, "composer_not_calibrated",
                    "input.focus_click is on but target.composer_anchor is unset; "
                    "run --calibrate", hwnd=window.hwnd))
            left, top, right, bottom = window.rect
            px = int(left + float(anchor["x_frac"]) * (right - left))
            # The composer is docked to the bottom edge at a fixed height, so an
            # offset from the bottom survives a resize where a fraction would not.
            if anchor.get("y_from_bottom") is not None:
                py = int(bottom - float(anchor["y_from_bottom"]))
            else:
                py = int(top + float(anchor["y_frac"]) * (bottom - top))
            if not (top < py < bottom and left < px < right):
                return done(SubmitResult(
                    False, "anchor_out_of_window",
                    f"anchor resolves to ({px},{py}), outside the window rect "
                    f"{window.rect}; re-run --calibrate", hwnd=window.hwnd))
            point = (px, py)

        if self.dry_run:
            plan = f"would click {point} then submit" if point else \
                   "would submit without touching the mouse"
            return done(SubmitResult(True, "dry_run", plan, hwnd=window.hwnd))

        backup = ClipboardBackup.capture()
        restore = bool(self.cfg["safety"].get("restore_clipboard", True))
        try:
            return done(self._run_sequence(window, point, text, event_id,
                                           send_hotkey))
        except WindowError as exc:
            return done(SubmitResult(False, exc.reason, str(exc), hwnd=window.hwnd))
        except ClipboardError as exc:
            return done(SubmitResult(False, "clipboard_failed", str(exc),
                                     hwnd=window.hwnd))
        except OSError as exc:
            return done(SubmitResult(False, "input_failed", str(exc),
                                     hwnd=window.hwnd))
        finally:
            release_modifiers()
            if restore:
                restored = backup.restore()
                if not restored or backup.had_other_formats:
                    self.log.write({"event": "clipboard_restore_partial",
                                    "restored_text": restored,
                                    "non_text_formats_lost": backup.had_other_formats})

    def _run_sequence(self, window: WindowInfo, point: Optional[Tuple[int, int]],
                      text: str, event_id: str, send_hotkey: str) -> SubmitResult:
        self._abort_if_halted()
        mark = time.time()
        force_foreground(window.hwnd, timeout=2.0)
        time.sleep(self._t("after_focus"))
        self._mark("focus", mark)

        # Occlusion only matters for a click; keystrokes reach the focused window
        # whatever is drawn on top of it.
        if point is not None and not point_is_ours(window.hwnd, *point):
            raise WindowError("window_occluded",
                              f"point {point} is covered by another window")

        guard = ImeGuard(window.hwnd)
        with guard:
            if guard.available and guard.previous:
                self.log.write({"event": "ime_suppressed", "hwnd": window.hwnd})
            elif not guard.available:
                self.log.write({"event": "ime_window_not_found", "hwnd": window.hwnd})
            return self._keyboard_sequence(window, point, text, event_id,
                                           send_hotkey)

    def _keyboard_sequence(self, window: WindowInfo, point: Optional[Tuple[int, int]],
                           text: str, event_id: str,
                           send_hotkey: str) -> SubmitResult:
        # 1. Conversation identity, straight from the app.
        mark = time.time()
        session_hotkey = self._hotkey("copy_session_id")
        expected = self.cfg["target"].get("session_id")
        mode = str(self.cfg["target"].get("session_mode", "pinned")).lower()
        session_id: Optional[str] = None
        if session_hotkey:
            session_id = self._copy_via(session_hotkey, "session")
            # Reading an id is still mandatory in either mode: it proves a
            # conversation is open at all (the settings page yields nothing) and
            # it names, in the audit log, exactly where the context went.
            if session_id is None:
                raise WindowError("session_id_unavailable",
                                  "the app did not produce a session id")
            session_id = session_id.strip()
            if mode == "pinned":
                if expected and session_id != str(expected).strip():
                    raise WindowError(
                        "session_mismatch",
                        f"active session {_short_session(session_id)} is not the "
                        f"configured {_short_session(str(expected))}")
            elif expected and session_id != str(expected).strip():
                self.log.write({"event": "session_followed",
                                "from": _short_session(str(expected)),
                                "to": _short_session(session_id)})
        elif mode == "pinned" and expected:
            raise WindowError("session_check_unavailable",
                              "a session id is configured but no "
                              "hotkeys.copy_session_id is bound")
        else:
            raise WindowError("session_check_unavailable",
                              "hotkeys.copy_session_id is required to know which "
                              "conversation the context would go to")

        mark = self._mark("session_id", mark)
        self._abort_if_halted()
        if user32.GetForegroundWindow() != window.hwnd:
            raise WindowError("focus_lost", "focus left the target window")

        # 2. Prove the keyboard focus is an empty editable field.  The composer is
        #    never clicked -- mouse input inside the app ends the voice chat -- so
        #    focus is established by reading the field back, not by pointing at it.
        if point is not None:
            click(*point)
            time.sleep(self._t("after_click"))
            if user32.GetForegroundWindow() != window.hwnd:
                raise WindowError("focus_lost",
                                  "focus left the target window after the click")
        refocus = self._hotkey("refocus_composer")
        if refocus:
            press(refocus, settle=self._t("after_click"))

        pre = self._read_composer()
        if pre is not None and pre.strip() and MARKER in pre and len(pre) <= 2000:
            # Our own payload, left behind by an earlier rejected submit.  A user
            # never types this marker, so clearing it is safe -- and without it a
            # single rejection would wedge every later event.
            press("ctrl+a", settle=0.08)
            press("backspace", settle=0.15)
            self.log.write({"event": "stale_payload_cleared", "chars": len(pre)})
            pre = self._read_composer()
        if pre is not None and pre.strip():
            press("end", settle=0.05)  # collapse the selection, touch nothing
            # A big selection is the transcript, not a draft: the two failures
            # need different fixes, so they are reported apart.
            if len(pre) > 2000:
                raise WindowError(
                    "composer_not_focused",
                    f"select-all returned {len(pre)} chars, so the keyboard focus "
                    f"is the conversation, not the composer")
            raise WindowError(
                "composer_not_empty",
                f"the focused field already holds {len(pre)} chars; refusing to "
                f"overwrite what looks like a draft")

        # 3. Paste and read it straight back.
        mark = self._mark("precheck", mark)
        self._abort_if_halted()
        Clipboard.set_text(text)
        if Clipboard.get_text() != text:
            raise ClipboardError("clipboard content changed before pasting")
        seq_before = Clipboard.sequence()
        press("ctrl+v", settle=0.04)
        # Check for a hijack before the readback -- reading plants a sentinel of
        # our own, which would move the sequence number either way.
        if Clipboard.sequence() != seq_before:
            raise ClipboardError("another process took the clipboard mid-paste")
        # Then wait for the paste to land, not for a fixed worst case.
        matched, post = self._read_composer_until(lambda v: v == text,
                                                  self._t("after_paste"))
        if not matched:
            got = "nothing" if post is None else f"{len(post)} chars"
            if post and MARKER in post:
                press("ctrl+a", settle=0.06)
                press("delete", settle=0.10)
                detail = f"paste mismatch ({got}); composer cleared"
            else:
                press("end", settle=0.05)
                detail = (f"paste mismatch ({got}); left untouched, "
                          f"manual cleanup may be needed")
            raise WindowError("paste_verification_failed", detail)

        # 4. Submit through the app's own command, with the selection collapsed.
        mark = self._mark("paste", mark)
        self._abort_if_halted()
        press("end", settle=0.05)
        if user32.GetForegroundWindow() != window.hwnd:
            raise WindowError("focus_lost", "focus left the target window before submit")
        press(send_hotkey, settle=0.06)
        self._last_submit = time.time()

        # 5. Did it actually leave the composer?  Poll until it empties rather
        #    than always paying the slowest observed round trip.
        emptied, leftover = self._read_composer_until(
            lambda v: v is None or not v.strip(), self._t("after_submit"))
        if not emptied:
            # The keystroke reached the app; the app refused to post it.  The
            # observed cause is a conversation whose turn is not live -- Codex
            # answers "Cannot steer conversation ... its active turn already
            # ended" and leaves the composer untouched.  Take our text back out:
            # it was verified byte-for-byte a moment ago, so nothing else can be
            # lost, and leaving it there would block every later event.
            press("ctrl+a", settle=0.08)
            press("backspace", settle=0.15)
            self._rejected_until = time.time() + float(
                self.cfg["limits"].get("rejection_cooldown_seconds", 15.0))
            return SubmitResult(
                False, "submit_rejected_by_app",
                "the send fired but the app refused to post it; the conversation "
                "has no live turn to steer. Composer restored; retrying after a "
                "cooldown",
                session_id=session_id, hwnd=window.hwnd,
                verified_in_conversation=False)

        # 6. Is it really in the conversation?  This is the only evidence that
        #    counts; a cleared composer only says the app accepted the keystroke.
        mark = self._mark("send_and_clear", mark)
        try:
            return self._verify_delivery(session_id, event_id, window)
        finally:
            self._mark("verify", mark)

    def _verify_delivery(self, session_id: Optional[str], event_id: str,
                         window: WindowInfo) -> SubmitResult:
        method = str(self.cfg.get("verification", {}).get("method", "none")).lower()
        strict = bool(self.cfg["safety"].get("require_transcript_verification"))

        def unverified(detail: str) -> SubmitResult:
            if strict:
                return SubmitResult(False, "transcript_check_unavailable", detail,
                                    session_id=session_id, hwnd=window.hwnd)
            return SubmitResult(True, "submitted_unverified", detail,
                                session_id=session_id, hwnd=window.hwnd,
                                verified_in_conversation=None)

        if not event_id:
            return unverified("composer cleared; no event id to search for")

        if method == "rollout":
            if not session_id:
                return unverified("composer cleared; no session id to locate the "
                                  "conversation record")
            cfgv = self.cfg["verification"]
            verifier = RolloutVerifier(Path(cfgv["rollout_root"]))
            found, path = verifier.contains(session_id, event_id,
                                            float(cfgv["timeout_seconds"]))
            if found is None:
                return unverified("composer cleared; no conversation record found "
                                  f"under {cfgv['rollout_root']}")
            if found:
                return SubmitResult(True, "submitted_verified",
                                    f"event id present in {path.name}",
                                    session_id=session_id, hwnd=window.hwnd,
                                    verified_in_conversation=True)
            return SubmitResult(False, "submitted_not_visible",
                                f"{path.name} does not carry this event id",
                                session_id=session_id, hwnd=window.hwnd,
                                verified_in_conversation=False)

        if method == "markdown_hotkey":
            hotkey = self._hotkey("copy_chat_markdown")
            if hotkey is None:
                return unverified("composer cleared; hotkeys.copy_chat_markdown "
                                  "is unset")
            transcript = self._copy_via(hotkey, "transcript")
            if transcript is None:
                return unverified("composer cleared; the transcript copy command "
                                  "produced nothing")
            if event_id in transcript:
                return SubmitResult(True, "submitted_verified",
                                    f"event id found in a {len(transcript)}-char "
                                    f"transcript", session_id=session_id,
                                    hwnd=window.hwnd, verified_in_conversation=True)
            return SubmitResult(False, "submitted_not_visible",
                                f"the {len(transcript)}-char transcript does not "
                                f"carry the injected message",
                                session_id=session_id, hwnd=window.hwnd,
                                verified_in_conversation=False)

        return unverified("composer cleared; verification.method is 'none'")


# --------------------------------------------------------------------------
# Queue pump: journal polling stays decoupled from slow UI work
# --------------------------------------------------------------------------

class SubmitQueue:
    def __init__(self, typist: VoiceTypist, log: AuditLog, cfg: Dict[str, Any]) -> None:
        self.typist = typist
        self.log = log
        self.max_retries = int(cfg["limits"]["max_retries"])
        self.settle = float(cfg["limits"].get("coalesce_settle_seconds", 1.5))
        self.items: deque[PendingItem] = deque(maxlen=int(cfg["limits"]["queue_size"]))
        self.last_result: Optional[SubmitResult] = None

    # Failures that will not improve by trying the same thing again.
    TERMINAL = {"payload_empty", "payload_too_large", "payload_unmarked",
                "payload_contains_secret", "payload_contains_bulk_data",
                "session_mismatch", "submit_hotkey_missing", "hotkey_invalid",
                "composer_not_calibrated", "window_ambiguous",
                "submitted_not_visible", "emergency_stop"}

    # Transient: the queue holds the item and tries again later instead of
    # burning one of its retries on a condition only the user can clear.
    HOLD = {"paused", "waiting_for_live_turn", "submit_rejected_by_app",
            "window_not_found", "window_not_foreground", "composer_not_focused"}

    def enqueue(self, item: PendingItem) -> bool:
        item.queued_at = time.time()
        # Dragging a selection emits an event per intermediate state.  Each one
        # has its own event id, so id-based de-duplication cannot collapse them;
        # only the last, settled state describes what the user actually selected.
        if item.coalesce_key:
            for index, queued in enumerate(self.items):
                if queued.coalesce_key == item.coalesce_key:
                    self.log.write({"event": "coalesced",
                                    "superseded_event_id": queued.event_id,
                                    "by_event_id": item.event_id,
                                    "key": item.coalesce_key})
                    self.items[index] = item
                    return True
        if len(self.items) == self.items.maxlen:
            dropped = self.items[0]
            self.log.write({"event": "queue_overflow", "dropped_event_id": dropped.event_id,
                            "dropped_type": dropped.event_type})
        self.items.append(item)
        return True

    def pump(self) -> Optional[SubmitResult]:
        if not self.items:
            return None
        item = self.items[0]
        # Hold a coalescing item back until nothing newer has superseded it.
        if item.coalesce_key:
            waited = time.time() - item.queued_at
            if waited < self.settle:
                return None
        result = self.typist.submit(item.text, item.event_id, item.event_type,
                                    item.source)
        self.last_result = result
        if result.ok:
            self.items.popleft()
            return result
        if result.outcome in self.HOLD:
            return result  # keep it queued; the blocker is not ours to fix
        item.attempts += 1
        if result.outcome in self.TERMINAL or item.attempts > self.max_retries:
            self.items.popleft()
            self.log.write({"event": "dropped", "event_id": item.event_id,
                            "event_type": item.event_type,
                            "attempts": item.attempts, "outcome": result.outcome})
        return result


# --------------------------------------------------------------------------
# Bridge integration: reuse the existing injector's fetch/dedupe/format
# --------------------------------------------------------------------------

def load_injector(path: Path):
    spec = importlib.util.spec_from_file_location("reader_context_injector", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the reader bridge from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["reader_context_injector"] = module
    spec.loader.exec_module(module)
    return module


def build_bridge(injector, queue: SubmitQueue, log: AuditLog, args) -> Any:
    """A SessionBridge whose transport is the typist instead of the App Server.

    Subclassing keeps the journal contract, the de-duplication and the exact
    injection text identical to the proven path -- only the last hop changes.
    """

    # A selection drag, a page turn and a pen stroke each emit a burst of
    # events describing one thing.  Everything except a command failure is
    # therefore collapsed to its latest state before submission; a failure is
    # always its own message because each one needs its own follow-up.
    def coalesce_key(event) -> Optional[str]:
        if event.event_type == "COMMAND_RESULT":
            return None
        payload = event.payload
        book = payload.get("book_id") or payload.get("file") or ""
        page = payload.get("page")
        return f"{event.event_type}:{book}:{page}"

    class TypistBridge(injector.SessionBridge):
        def _inject(self, event, state) -> bool:  # type: ignore[override]
            key = event.payload.get("event_id")
            if isinstance(key, str) and state.seen_event(key):
                log.write({"event": "deduped", "event_id": key,
                           "event_type": event.event_type})
                return False
            text = self._format_injection_text(event)
            queue.enqueue(PendingItem(text=text, event_id=str(key or ""),
                                      event_type=event.event_type,
                                      source=str(event.payload.get("source") or ""),
                                      coalesce_key=coalesce_key(event)))
            log.write({"event": "queued", "event_id": key,
                       "event_type": event.event_type,
                       "queue_depth": len(queue.items)})
            if isinstance(key, str):
                state.remember(key)
            return True

    return TypistBridge(
        thread_id=args.thread_id,
        watch_root=args.watch_root,
        session_pattern=args.session_pattern,
        rules_dir=args.rules_local,
        appserver=None,
        poll_seconds=args.poll,
        debounce_seconds=args.page_debounce,
        log_path=args.bridge_log,
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init_config(args) -> int:
    if args.config.exists() and not args.force:
        print(f"config already exists: {args.config} (use --force to overwrite)")
        return 1
    save_config(args.config, DEFAULT_CONFIG_BODY)
    print(f"wrote {args.config}")
    print("Next: bind Codex's \"Send the current composer message\" (and ideally "
          "\"Copy the current chat as Markdown\"), record them in hotkeys, then "
          "run --calibrate.")
    return 0


def cmd_windows(args) -> int:
    for win in enumerate_windows():
        proc = _process_name(win.pid)
        w = win.rect[2] - win.rect[0]
        h = win.rect[3] - win.rect[1]
        print(f"hwnd={win.hwnd:<10} pid={win.pid:<7} proc={proc:<16} "
              f"cls={win.cls:<22} {w}x{h} title={win.title!r}")
    return 0


def cmd_calibrate(args) -> int:
    cfg = load_config(args.config)
    try:
        window = resolve_target(cfg["target"])
    except WindowError as exc:
        print(f"[{exc.reason}] {exc}")
        return 2
    left, top, right, bottom = window.rect
    print(f"target: hwnd={window.hwnd} {window.title!r} "
          f"rect={left},{top} {right-left}x{bottom-top}")
    print()
    if args.point:
        try:
            sx, sy = args.point.split(",")
            x, y = int(sx), int(sy)
        except ValueError:
            print(f"--point expects 'X,Y' in screen pixels, got {args.point!r}")
            return 2
        print(f"using the supplied point ({x},{y})")
    elif args.countdown:
        print("Move the mouse pointer over the message input box -- do not click.")
        for remaining in range(int(args.countdown), 0, -1):
            print(f"  sampling the cursor in {remaining}s ...", end="\r", flush=True)
            time.sleep(1.0)
        x, y = cursor_pos()
        print(f"\nsampled cursor at ({x},{y})            ")
    else:
        print("1. Open the Codex conversation you want context delivered to.")
        print("2. Move the mouse pointer over its message input box -- do not click.")
        print("3. Come back here and press Enter.")
        input("   ready> ")
        x, y = cursor_pos()
    if not (left <= x <= right and top <= y <= bottom):
        print(f"cursor ({x},{y}) is outside the target window; nothing saved")
        return 2
    if not point_is_ours(window.hwnd, x, y):
        print(f"point ({x},{y}) belongs to another window; nothing saved")
        return 2
    anchor = {"x_frac": round((x - left) / max(1, right - left), 4),
              "y_frac": round((y - top) / max(1, bottom - top), 4),
              "y_from_bottom": bottom - y,
              "calibrated_at": now_ts(),
              "window_size": [right - left, bottom - top]}
    cfg["target"]["composer_anchor"] = anchor
    if args.pin_hwnd:
        cfg["target"]["hwnd"] = window.hwnd

    session_hotkey = cfg["hotkeys"].get("copy_session_id")
    if session_hotkey:
        print("\nReading the conversation id with "
              f"{session_hotkey!r} ...")
        state = RunState(args.state_dir)
        typist = VoiceTypist(cfg, AuditLog(args.log), state)
        try:
            force_foreground(window.hwnd)
            time.sleep(0.3)
            session_id = typist._copy_via(session_hotkey, "session")
        except Exception as exc:  # noqa: BLE001 - calibration is interactive
            session_id = None
            print(f"  failed: {exc}")
        if session_id:
            cfg["target"]["session_id"] = session_id.strip()
            print(f"  session id: {session_id.strip()}")
        else:
            print("  no session id came back; leaving target.session_id unchanged")

    save_config(args.config, cfg)
    print(f"\nsaved anchor {anchor['x_frac']},{anchor['y_frac']} to {args.config}")
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    state = RunState(args.state_dir)
    checks: List[Tuple[str, bool, str]] = []

    checks.append(("dpi awareness", True, set_dpi_awareness()))

    try:
        window = resolve_target(cfg["target"])
        checks.append(("target window", True,
                       f"hwnd={window.hwnd} {window.title!r} pid={window.pid}"))
    except WindowError as exc:
        checks.append(("target window", False, f"[{exc.reason}] {exc}"))
        window = None

    focus_click = bool(cfg.get("input", {}).get("focus_click"))
    checks.append(("mouse", True,
                   "click-to-focus ENABLED (this ends the voice chat)" if focus_click
                   else "never used -- keyboard only"))
    anchor = cfg["target"].get("composer_anchor")
    checks.append(("composer anchor", isinstance(anchor, dict) or not focus_click,
                   json.dumps(anchor, ensure_ascii=False) if anchor
                   else ("not calibrated -- run --calibrate" if focus_click
                         else "not needed while focus_click is off")))

    for name in ("copy_session_id", "send_message", "copy_chat_markdown",
                 "refocus_composer"):
        spec = cfg["hotkeys"].get(name)
        if not spec:
            required = name in ("copy_session_id", "send_message")
            checks.append((f"hotkey {name}", not required,
                           "unset" + (" (required)" if required else " (optional)")))
            continue
        try:
            parse_hotkey(spec)
            checks.append((f"hotkey {name}", True, spec))
        except HotkeyError as exc:
            checks.append((f"hotkey {name}", False, str(exc)))

    mode = str(cfg["target"].get("session_mode", "pinned")).lower()
    checks.append(("session mode", mode in ("pinned", "follow_active"),
                   "follows whichever conversation is open" if mode == "follow_active"
                   else f"pinned to {_short_session(cfg['target'].get('session_id'))}"))

    method = str(cfg.get("verification", {}).get("method", "none")).lower()
    if method == "rollout":
        root = Path(cfg["verification"]["rollout_root"])
        if mode == "follow_active":
            checks.append(("delivery evidence", root.exists(),
                           f"rollout records under {root}" if root.exists()
                           else f"missing {root}"))
        else:
            session = cfg["target"].get("session_id")
            record = RolloutVerifier(root).find(str(session)) if session else None
            checks.append(("delivery evidence", record is not None,
                           f"conversation record {record.name}" if record
                           else f"no rollout-*-{session}.jsonl under {root}"))
    else:
        checks.append(("delivery evidence", method != "none",
                       f"method={method}"))

    checks.append(("emergency stop", not state.stopped,
                   "engaged" if state.stopped else "clear"))
    checks.append(("paused", True, "yes" if state.paused else "no"))

    if window is not None and cfg["hotkeys"].get("copy_session_id") and args.live:
        typist = VoiceTypist(cfg, AuditLog(args.log), state)
        backup = ClipboardBackup.capture()
        try:
            force_foreground(window.hwnd)
            time.sleep(0.3)
            got = typist._copy_via(cfg["hotkeys"]["copy_session_id"], "session")
            want = cfg["target"].get("session_id")
            if got is None:
                checks.append(("live session id", False, "app produced nothing"))
            elif want and got.strip() != str(want).strip():
                checks.append(("live session id", False,
                               f"active {got.strip()} != configured {want}"))
            else:
                checks.append(("live session id", True, got.strip()))
        except Exception as exc:  # noqa: BLE001
            checks.append(("live session id", False, str(exc)))
        finally:
            release_modifiers()
            backup.restore()

    width = max(len(name) for name, _, _ in checks)
    failures = 0
    for name, ok, detail in checks:
        mark = "ok  " if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{mark}] {name.ljust(width)}  {detail}")
    print()
    print("verdict:", "ready" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 2


def cmd_probe(args) -> int:
    """Report what the keyboard focus currently is, without changing anything.

    This answers the one question the accessibility tree cannot: is the composer
    focused right now?  Nothing is typed, pasted or submitted.
    """
    cfg = load_config(args.config)
    set_dpi_awareness()
    typist = VoiceTypist(cfg, AuditLog(args.log), RunState(args.state_dir))
    try:
        window = resolve_target(cfg["target"])
    except WindowError as exc:
        print(json.dumps({"ok": False, "outcome": exc.reason, "detail": str(exc)},
                         ensure_ascii=False))
        return 2

    backup = ClipboardBackup.capture()
    try:
        force_foreground(window.hwnd)
        time.sleep(float(cfg["timing"]["after_focus"]))
        guard = ImeGuard(window.hwnd)
        with guard:
            ime_state = {"ime_window": bool(guard.available),
                         "ime_was_open": bool(guard.previous)}
            session = None
            if cfg["hotkeys"].get("copy_session_id"):
                session = typist._copy_via(cfg["hotkeys"]["copy_session_id"],
                                           "session")
            content = typist._read_composer()
            press("end", settle=0.05)
    except (WindowError, ClipboardError, OSError) as exc:
        print(json.dumps({"ok": False,
                          "outcome": getattr(exc, "reason", type(exc).__name__),
                          "detail": str(exc)}, ensure_ascii=False))
        return 3
    finally:
        release_modifiers()
        backup.restore()

    if content is None:
        focus = "empty_editable_or_no_selection"
    elif len(content) > 2000:
        focus = "conversation_transcript"
    elif MARKER in content:
        focus = "composer_holds_leftover_payload"
    else:
        focus = "small_text_field"
    print(json.dumps({
        "ok": True,
        "hwnd": window.hwnd,
        "window": window.title,
        "session_id": (session or "").strip() or None,
        "session_matches_config": (session or "").strip() ==
                                  str(cfg["target"].get("session_id") or "").strip(),
        "focus_guess": focus,
        "selection_chars": None if content is None else len(content),
        "selection_head": None if content is None else content[:160],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_lock(args) -> int:
    """Pin whichever conversation is open right now as the delivery target.

    Re-pointing the component is deliberate and auditable: nothing else ever
    rewrites target.session_id, so a conversation switch stops delivery instead
    of silently following the user around.
    """
    cfg = load_config(args.config)
    set_dpi_awareness()
    log = AuditLog(args.log)
    typist = VoiceTypist(cfg, log, RunState(args.state_dir))
    hotkey = cfg["hotkeys"].get("copy_session_id")
    if not hotkey:
        print(json.dumps({"ok": False, "detail": "hotkeys.copy_session_id is unset"},
                         ensure_ascii=False))
        return 2
    try:
        window = resolve_target(cfg["target"])
    except WindowError as exc:
        print(json.dumps({"ok": False, "outcome": exc.reason, "detail": str(exc)},
                         ensure_ascii=False))
        return 2

    backup = ClipboardBackup.capture()
    try:
        force_foreground(window.hwnd)
        time.sleep(float(cfg["timing"]["after_focus"]))
        with ImeGuard(window.hwnd):
            session = typist._copy_via(hotkey, "session")
    except (WindowError, ClipboardError, OSError) as exc:
        print(json.dumps({"ok": False,
                          "outcome": getattr(exc, "reason", type(exc).__name__),
                          "detail": str(exc)}, ensure_ascii=False))
        return 3
    finally:
        release_modifiers()
        backup.restore()

    if not session or not session.strip():
        print(json.dumps({"ok": False, "detail": "the app produced no session id"},
                         ensure_ascii=False))
        return 3
    previous = cfg["target"].get("session_id")
    cfg["target"]["session_id"] = session.strip()
    # Locking is only meaningful in pinned mode, so asking for it selects it.
    cfg["target"]["session_mode"] = "follow_active" if args.follow else "pinned"
    save_config(args.config, cfg)
    log.write({"event": "target_locked", "session": _short_session(session.strip()),
               "previous": _short_session(previous),
               "mode": cfg["target"]["session_mode"]})
    print(json.dumps({"ok": True, "locked_session": session.strip(),
                      "previous_session": previous,
                      "session_mode": cfg["target"]["session_mode"]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_clear(args) -> int:
    """Remove a leftover injected payload from the composer.

    Only clears text this component recognises as its own, so it can never wipe
    something the user was writing.
    """
    cfg = load_config(args.config)
    set_dpi_awareness()
    typist = VoiceTypist(cfg, AuditLog(args.log), RunState(args.state_dir))
    try:
        window = resolve_target(cfg["target"])
    except WindowError as exc:
        print(json.dumps({"ok": False, "detail": str(exc)}, ensure_ascii=False))
        return 2

    backup = ClipboardBackup.capture()
    try:
        force_foreground(window.hwnd)
        time.sleep(float(cfg["timing"]["after_focus"]))
        with ImeGuard(window.hwnd):
            content = typist._read_composer()
            if content is None:
                press("end", settle=0.05)
                verdict = {"ok": True, "action": "none", "detail": "composer is empty"}
            elif MARKER in content or args.force:
                press("ctrl+a", settle=0.08)
                press("backspace", settle=0.15)
                after = typist._read_composer()
                press("end", settle=0.05)
                verdict = {"ok": after is None, "action": "cleared",
                           "removed_chars": len(content),
                           "still_holds": None if after is None else len(after)}
            else:
                press("end", settle=0.05)
                verdict = {"ok": False, "action": "refused",
                           "detail": f"the composer holds {len(content)} chars that "
                                     f"are not ours; use --force to clear anyway",
                           "head": content[:120]}
    except (WindowError, ClipboardError, OSError) as exc:
        verdict = {"ok": False, "action": "aborted",
                   "outcome": getattr(exc, "reason", type(exc).__name__),
                   "detail": str(exc)}
    finally:
        release_modifiers()
        backup.restore()
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["ok"] else 3


def cmd_press(args) -> int:
    """Focus the target window and press one key combination.

    A diagnostic for newly bound app shortcuts: it reports whether the composer
    changed, which is how you tell a working send binding from a dead one.
    """
    cfg = load_config(args.config)
    set_dpi_awareness()
    typist = VoiceTypist(cfg, AuditLog(args.log), RunState(args.state_dir))
    try:
        parse_hotkey(args.hotkey)
        window = resolve_target(cfg["target"])
    except (HotkeyError, WindowError) as exc:
        print(json.dumps({"ok": False, "detail": str(exc)}, ensure_ascii=False))
        return 2

    backup = ClipboardBackup.capture()
    try:
        force_foreground(window.hwnd)
        time.sleep(float(cfg["timing"]["after_focus"]))
        with ImeGuard(window.hwnd):
            if args.expect_copy:
                # A copy command is judged by what it puts on the clipboard, not
                # by what happens to the composer.
                copied = typist._copy_via(args.hotkey, "press")
                print(json.dumps({
                    "ok": copied is not None, "hotkey": args.hotkey,
                    "copied_chars": None if copied is None else len(copied),
                    "copied_head": None if copied is None else copied[:200],
                }, ensure_ascii=False, indent=2))
                return 0 if copied is not None else 3
            before = typist._read_composer()
            press("end", settle=0.05)
            press(args.hotkey, settle=0.1)
            time.sleep(float(args.wait))
            after = typist._read_composer()
            press("end", settle=0.05)
    except (WindowError, ClipboardError, OSError) as exc:
        print(json.dumps({"ok": False,
                          "outcome": getattr(exc, "reason", type(exc).__name__),
                          "detail": str(exc)}, ensure_ascii=False))
        return 3
    finally:
        release_modifiers()
        backup.restore()

    print(json.dumps({
        "ok": True, "hotkey": args.hotkey,
        "composer_before_chars": None if before is None else len(before),
        "composer_after_chars": None if after is None else len(after),
        "composer_cleared": before is not None and after is None,
        "changed": before != after,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_send(args) -> int:
    cfg = load_config(args.config)
    set_dpi_awareness()
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        text = sys.stdin.read()
    event_id = args.event_id or sha256_text(text)[:32]
    if MARKER not in text and args.wrap:
        payload = {"event_type": args.event_type or "MANUAL", "event_id": event_id,
                   "at": now_ts(), "note": text}
        text = (f"{MARKER}\n"
                + json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
                + f"\n{MARKER_END}")
    typist = VoiceTypist(cfg, AuditLog(args.log), RunState(args.state_dir),
                         dry_run=args.dry_run)
    result = typist.submit(text, event_id=event_id,
                           event_type=args.event_type or "MANUAL", source="cli")
    print(json.dumps(result.as_dict(), ensure_ascii=False))
    return 0 if result.ok else 3


def cmd_state(args) -> int:
    state = RunState(args.state_dir)
    if args.action == "pause":
        state.pause()
    elif args.action == "resume":
        state.resume()
    elif args.action == "stop":
        state.emergency_stop("cli")
    elif args.action == "clear-stop":
        state.clear_stop()
    print(json.dumps({"paused": state.paused, "emergency_stop": state.stopped},
                     ensure_ascii=False))
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    set_dpi_awareness()
    log = AuditLog(args.log)
    state = RunState(args.state_dir)
    if args.clear_stop:
        state.clear_stop()

    injector = load_injector(args.injector)
    typist = VoiceTypist(cfg, log, state, dry_run=args.dry_run)
    queue = SubmitQueue(typist, log, cfg)
    bridge = build_bridge(injector, queue, log, args)

    journal = None
    if args.journal_url:
        journal = injector.ProductionJournal(
            args.journal_url, args.journal_credential_target,
            args.journal_cursor, args.journal_limit)

    log.write({"event": "started", "dry_run": args.dry_run,
               "journal": bool(journal), "watch_root": str(args.watch_root),
               "config": str(args.config)})
    print(f"voice-typist running (dry_run={args.dry_run}); "
          f"state dir {args.state_dir}")

    last_journal = 0.0
    last_status = 0.0
    last_activity = time.time()
    idle_exit = max(0.0, float(getattr(args, "idle_exit_seconds", 0.0) or 0.0))
    base_interval = max(0.2, args.journal_poll)
    journal_interval = base_interval
    journal_failures = 0
    try:
        while True:
            now = time.time()
            if state.stopped:
                if now - last_status > 2.0:
                    state.write_status({"running": True, "reason": "emergency_stop",
                                        "queue_depth": len(queue.items)})
                    last_status = now
                time.sleep(0.5)
                continue

            if journal and now - last_journal >= journal_interval:
                try:
                    batch = journal.fetch()
                    rows = injector.journal_rows(batch) if batch.get("gap") is not True else []
                    bridge.consume_journal_batch(batch, journal)
                    if journal_failures:
                        log.write({"event": "journal_recovered",
                                   "after_failures": journal_failures})
                    journal_failures = 0
                    journal_interval = base_interval
                    if rows:
                        last_activity = now
                    else:
                        log.write({"event": "journal_empty"})
                except Exception as exc:  # noqa: BLE001 - transport is untrusted
                    journal_failures += 1
                    # Back off instead of hammering a source that is already
                    # failing, and log the first few plus milestones rather than
                    # one line every poll.
                    journal_interval = min(30.0, base_interval * (2 ** min(journal_failures, 6)))
                    if journal_failures <= 3 or journal_failures % 20 == 0:
                        log.write({"event": "journal_fetch_failed",
                                   "err": str(exc)[:400],
                                   "consecutive": journal_failures,
                                   "next_retry_seconds": round(journal_interval, 1)})
                last_journal = now

            try:
                bridge.scan_once()
            except Exception as exc:  # noqa: BLE001
                log.write({"event": "scan_failed", "err": str(exc)[:400]})

            if queue.items or state.paused:
                # Pending work, or a deliberate pause the user can still resume
                # from: neither is idle, so the backstop below must not fire.
                last_activity = now

            if not state.paused:
                result = queue.pump()
                if result is not None:
                    last_activity = now
                    print(json.dumps(result.as_dict(), ensure_ascii=False))

            if now - last_status > 2.0:
                last = queue.last_result
                state.write_status({
                    "running": True,
                    "queue_depth": len(queue.items),
                    "dry_run": args.dry_run,
                    "last_outcome": last.outcome if last else None,
                    "last_verified": last.verified_in_conversation if last else None,
                    "journal_failures": journal_failures,
                    "journal_retry_seconds": round(journal_interval, 1),
                })
                last_status = now

            if idle_exit and now - last_activity >= idle_exit:
                # The upper layer owns start/stop; this only catches the case
                # where nobody is left to stop us.
                idle_for = round(now - last_activity, 1)
                log.write({"event": "stopped", "reason": "idle_exit",
                           "idle_seconds": idle_for,
                           "idle_exit_seconds": idle_exit})
                state.write_status({"running": False, "queue_depth": 0,
                                    "reason": "idle_exit",
                                    "idle_seconds": idle_for})
                print(f"voice-typist exiting: idle for {idle_for}s "
                      f"(limit {idle_exit}s)")
                return 0

            time.sleep(max(0.2, args.poll))
    except KeyboardInterrupt:
        log.write({"event": "stopped", "reason": "keyboard_interrupt"})
        state.write_status({"running": False, "queue_depth": len(queue.items)})
        return 130


def cmd_panel(args) -> int:
    """A small always-visible enable/pause/stop indicator."""
    import tkinter as tk

    state = RunState(args.state_dir)
    root = tk.Tk()
    root.title("Reader → Codex Voice")
    root.attributes("-topmost", True)
    root.geometry("300x132")
    root.resizable(False, False)

    banner = tk.Label(root, text="", font=("Segoe UI", 16, "bold"),
                      height=2, fg="white")
    banner.pack(fill="x")
    detail = tk.Label(root, text="", font=("Segoe UI", 9), justify="left",
                      anchor="w")
    detail.pack(fill="x", padx=8)

    row = tk.Frame(root)
    row.pack(fill="x", pady=6)
    tk.Button(row, text="暂停", width=8,
              command=state.pause).pack(side="left", padx=6)
    tk.Button(row, text="恢复", width=8,
              command=lambda: (state.resume(), state.clear_stop())).pack(side="left")
    tk.Button(row, text="紧急停止", width=10, fg="white", bg="#b00020",
              command=lambda: state.emergency_stop("panel")).pack(side="right", padx=6)

    def refresh() -> None:
        if state.stopped:
            banner.config(text="紧急停止", bg="#b00020")
        elif state.paused:
            banner.config(text="已暂停", bg="#b26a00")
        else:
            banner.config(text="启用中", bg="#1e7b34")
        try:
            status = json.loads(state.status_file.read_text(encoding="utf-8"))
            detail.config(text=f"队列 {status.get('queue_depth', '?')}   "
                               f"上次 {status.get('last_outcome') or '-'}\n"
                               f"更新 {status.get('at', '-')}")
        except Exception:  # noqa: BLE001
            detail.config(text="尚无状态文件（组件未运行？）")
        root.after(500, refresh)

    refresh()
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice_typist",
        description="Submit one formatted reader-context event into the current "
                    "Codex Voice conversation and verify it landed there.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-config", help="write a default config file")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init_config)

    p = sub.add_parser("windows", help="list candidate windows")
    p.set_defaults(func=cmd_windows)

    p = sub.add_parser("calibrate", help="record the composer anchor and session id")
    p.add_argument("--pin-hwnd", action="store_true",
                   help="also pin the exact window handle (breaks on app restart)")
    p.add_argument("--point", help="skip the prompt: 'X,Y' in screen pixels")
    p.add_argument("--countdown", type=int,
                   help="skip the prompt: sample the cursor after N seconds")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("doctor", help="check every precondition")
    p.add_argument("--live", action="store_true",
                   help="also focus the app and read back the session id")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("probe", help="report the current focus without changing it")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("lock", help="pin the currently open conversation as target")
    p.add_argument("--follow", action="store_true",
                   help="record the id but keep following whatever chat is open")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("clear", help="remove a leftover injected payload")
    p.add_argument("--force", action="store_true",
                   help="clear even text this component did not write")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("press", help="press one hotkey in the target window")
    p.add_argument("hotkey")
    p.add_argument("--wait", type=float, default=1.0)
    p.add_argument("--expect-copy", action="store_true",
                   help="judge the hotkey by what it puts on the clipboard")
    p.set_defaults(func=cmd_press)

    p = sub.add_parser("send", help="submit one message")
    p.add_argument("--text")
    p.add_argument("--file")
    p.add_argument("--event-id")
    p.add_argument("--event-type")
    p.add_argument("--wrap", action="store_true",
                   help="wrap plain text in the bridge envelope")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("state", help="pause / resume / emergency stop")
    p.add_argument("action", choices=["show", "pause", "resume", "stop", "clear-stop"])
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("panel", help="always-on-top status panel")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("run", help="consume the reader bridge and submit events")
    p.add_argument("--injector", type=Path, default=INJECTOR_PATH)
    p.add_argument("--thread-id", default="voice-typist")
    p.add_argument("--watch-root", type=Path,
                   default=Path(r"C:\Users\bwica\bw-reader-context\events"))
    p.add_argument("--session-pattern", default="realtime-voice-chat-*")
    p.add_argument("--rules-local", type=Path,
                   default=Path(r"C:\Users\bwica\bw-reader-context\rules"))
    p.add_argument("--bridge-log", type=Path,
                   default=INSTALL_DIR / "logs" / "voice-typist-bridge.jsonl")
    p.add_argument("--journal-url")
    p.add_argument("--journal-credential-target", default="BWReaderJournal")
    p.add_argument("--journal-cursor", type=Path,
                   default=INSTALL_DIR / "voice-typist-journal-cursor.json")
    p.add_argument("--journal-poll", type=float, default=0.5)
    p.add_argument("--journal-limit", type=int, default=200)
    p.add_argument("--poll", type=float, default=0.5)
    p.add_argument("--page-debounce", type=float, default=2.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--clear-stop", action="store_true",
                   help="clear a leftover emergency stop at startup")
    # Lifecycle: the typist is meant to live and die with the voice session that
    # needs it.  The upper layer is supposed to call `launcher Stop`, but a
    # crashed or detached parent leaves this process orphaned and spinning
    # forever (observed: 11.6h alive, parent gone, 77753 empty polls).  This is
    # the backstop that does not depend on anyone else behaving correctly.
    # 0 disables it.
    p.add_argument("--idle-exit-seconds", type=float, default=600.0,
                   help="exit after this many seconds with no journal rows, "
                        "no queued work and no delivery attempt (0 = never)")
    p.set_defaults(func=cmd_run)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    # Always, so every reported rect is in the same physical pixels the input
    # layer uses.  A DPI-unaware listing would print coordinates that click
    # somewhere else entirely.
    set_dpi_awareness()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
