#!/usr/bin/env python3
"""make_app_icons.py — 各功能页的主屏图标(用户需求 2026-07-19:PDF 阅读器/健身/网页
门户加到 iOS 桌面时要能分辨)。

Pi 无彩色 emoji 字体 → 用「深色底 + Noto Serif CJK 粗体白字」绘制(与全站暗色调统一,
iOS 自动切圆角)。180×180(apple-touch-icon 标准)+ 32×32(favicon)。
产物:_server_deploy/static/icons/<name>.png(入 git)→ cp 到 /var/www/html/static/icons/。
加新图标:往 ICONS 里添一行,重跑本脚本 + 对应模板 head 挂 <link>。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"
OUT = Path(__file__).resolve().parent.parent / "_server_deploy" / "static" / "icons"

# name → (字, 底色顶部, 底色底部)——每个功能一眼可辨的单字 + 专属色
ICONS = {
    "reader":  ("書", (22, 35, 63),  (10, 17, 34)),    # 书架/PDF/EPUB 阅读器:深蓝
    "fitness": ("筋", (64, 38, 15),  (32, 18, 6)),     # 健身:深橙棕
    "web":     ("网", (19, 58, 51),  (8, 28, 24)),     # 网页门户:深青绿
    "control": ("制", (46, 22, 58),  (22, 10, 30)),    # 控制面板:深紫
    "insights": ("览", (58, 46, 16), (28, 22, 7)),     # 学习看板:深金
}


def make(name, ch, top, bot, size=180):
    img = Image.new("RGB", (size, size))
    dr = ImageDraw.Draw(img)
    for y in range(size):                      # 纵向微渐变(顶亮底暗,有质感不花哨)
        t = y / size
        dr.line([(0, y), (size, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))
    font = ImageFont.truetype(FONT, int(size * 0.62))
    bb = dr.textbbox((0, 0), ch, font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    dr.text(((size - w) / 2 - bb[0], (size - h) / 2 - bb[1]), ch,
            font=font, fill=(240, 244, 252))
    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / f"{name}.png")
    img.resize((32, 32), Image.LANCZOS).save(OUT / f"{name}-32.png")


if __name__ == "__main__":
    for name, (ch, top, bot) in ICONS.items():
        make(name, ch, top, bot)
        print(f"  ✓ {name}.png(「{ch}」)")
    print(f"→ {OUT}(部署:sudo cp {OUT}/*.png /var/www/html/static/icons/)")
