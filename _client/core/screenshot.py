"""截图工具。第一档只做全屏截图；区域选择留后续版本。"""
from __future__ import annotations

import io

from PIL import ImageGrab


def capture_full_screen() -> bytes:
    """整屏截图，返回 PNG bytes。"""
    img = ImageGrab.grab(all_screens=False)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
