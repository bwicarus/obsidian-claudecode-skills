"""测试用 Chromium 的跨平台查找（2026-09-01）。

15 个浏览器测试各自写死了 Pi 的 Linux 路径
（`~/.cache/ms-playwright/chromium-1223/chrome-linux/chrome`），在 Windows
主力机上一律 executable doesn't exist —— 而这台机的
`%LOCALAPPDATA%\\ms-playwright` 里明明装着配对好的 Windows 版。写死版本号
还有第二重坑：pip 升级 playwright 后版本目录变了，所有测试同时失联。

查找顺序（第一个存在的胜出）：
① env `BW_CHROME_EXECUTABLE` / `BW_PLAYWRIGHT_CHROME`（两个历史名字都认）；
② Windows playwright 缓存里版本号最大的 chromium（chrome-win64 与旧名
   chrome-win 都试）；
③ Linux playwright 缓存里版本号最大的 chromium（Pi 上行为不变，且不再
   钉死 1223）；
④ 都没有 → 返回旧写死路径，让 launch 报出人能读懂的标准位置 + 装法。
"""
from __future__ import annotations

import os
import pathlib
import re


def _newest(base: pathlib.Path, pattern: str) -> pathlib.Path | None:
    best: pathlib.Path | None = None
    best_version = -1
    try:
        candidates = list(base.glob(pattern))
    except OSError:
        return None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        matched = re.search(r"chromium-(\d+)", str(candidate))
        version = int(matched.group(1)) if matched else 0
        if version > best_version:
            best, best_version = candidate, version
    return best


def chrome_executable() -> pathlib.Path:
    for env_name in ("BW_CHROME_EXECUTABLE", "BW_PLAYWRIGHT_CHROME"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return pathlib.Path(value)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        windows_cache = pathlib.Path(local_app_data) / "ms-playwright"
        for pattern in (
            "chromium-*/chrome-win64/chrome.exe",
            "chromium-*/chrome-win/chrome.exe",
        ):
            found = _newest(windows_cache, pattern)
            if found:
                return found
    linux_cache = pathlib.Path.home() / ".cache" / "ms-playwright"
    found = _newest(linux_cache, "chromium-*/chrome-linux/chrome")
    if found:
        return found
    return linux_cache / "chromium-1223" / "chrome-linux" / "chrome"


CHROME = chrome_executable()
