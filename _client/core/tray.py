"""系统托盘图标 + 菜单 — pystray 在子线程跑，主线程留给 customtkinter mainloop。

Windows-only 验证过；macOS 上 pystray 要求主线程，本项目暂不考虑。
"""
from __future__ import annotations

import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw, ImageFont


def _make_icon_image() -> Image.Image:
    """生成一个简单的图标：紫色圆角方块 + 白色 'B'"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 紫色圆角矩形
    d.rounded_rectangle((4, 4, size - 4, size - 4), radius=12, fill=(99, 102, 241, 255))
    # 中心写 B
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), "B", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - 2), "B",
           fill=(255, 255, 255, 255), font=font)
    return img


class TrayIcon:
    def __init__(
        self,
        on_show: Callable[[], None],
        on_sync_now: Callable[[], None],
        on_quit: Callable[[], None],
        on_toggle_floating: Callable[[], None] | None = None,
        title: str = "bwicarus-client",
    ):
        self._on_show = on_show
        self._on_sync = on_sync_now
        self._on_quit = on_quit

        items = [
            pystray.MenuItem("显示主窗口", self._wrap(on_show), default=True),
        ]
        if on_toggle_floating is not None:
            items.append(pystray.MenuItem("切换任务监视窗", self._wrap(on_toggle_floating)))
        items += [
            pystray.MenuItem("立即同步", self._wrap(on_sync_now)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._wrap(on_quit)),
        ]
        menu = pystray.Menu(*items)
        self.icon = pystray.Icon(
            "bwicarus-client",
            _make_icon_image(),
            title,
            menu,
        )
        self._thread: threading.Thread | None = None

    @staticmethod
    def _wrap(fn: Callable[[], None]):
        def handler(icon=None, item=None):
            fn()
        return handler

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, daemon=True, name="tray")
        self._thread.start()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass

    def update_title(self, title: str) -> None:
        self.icon.title = title

    def notify(self, message: str, title: str = "bwicarus-client") -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            pass
