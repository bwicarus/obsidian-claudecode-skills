"""标定玩家 HUD 的三条状态条(HP/FP/耐力)+ 队友栏,写入探针配置。

为什么要三条(2026-08-17 用户指出的误判根因):只采 HP 一条时,"血条归零"
和"整片 HUD 消失(菜单/过场/加载)"读数完全一样,首场 8 次死亡候选里 3 次
是这个混淆造成的误判。濒死时 FP/耐力条仍在,菜单时三条一起消失 —— 多采
两条就把两种情况变成确定性可分。

用法: python nightreign_calibrate_hud.py <满血游戏截图.png|jpg>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from nightreign_probe import CONFIG_PATH, load_config

SCAN_TOP, SCAN_BOTTOM = 40, 420  # 玩家 HUD 区(跳过 Afterburner OSD)
MIN_RUN = 100
GAP = 3


def runs(mask_row: np.ndarray) -> tuple[int, int]:
    """一行里最长的连续 True 段(容忍 GAP 间隙),返回 (起点, 长度)。"""
    best = (0, 0)
    start, gap, length = -1, 0, 0
    for i, f in enumerate(mask_row):
        if f:
            if start < 0:
                start, length, gap = i, 1, 0
            else:
                length, gap = i - start + 1, 0
        elif start >= 0:
            gap += 1
            if gap > GAP:
                if length > best[1]:
                    best = (start, length)
                start = -1
    if start >= 0 and length > best[1]:
        best = (start, length)
    return best


def band_for(mask: np.ndarray, y0: int) -> dict | None:
    """从扫描区找一条实心长条,返回内圈矩形。"""
    per_row = [runs(mask[y]) for y in range(mask.shape[0])]
    best_y = max(range(len(per_row)), key=lambda y: per_row[y][1])
    bx, blen = per_row[best_y]
    if blen < MIN_RUN:
        return None
    top = bot = best_y
    while top > 0 and per_row[top - 1][1] >= blen * 0.6 and abs(per_row[top - 1][0] - bx) <= 12:
        top -= 1
    while bot < len(per_row) - 1 and per_row[bot + 1][1] >= blen * 0.6 and abs(per_row[bot + 1][0] - bx) <= 12:
        bot += 1
    h = bot - top + 1
    if h < 3:
        return None
    inset = max(1, h // 4)
    return {
        "x": max(0, bx - 2),
        "y": y0 + top + inset,
        "w": blen,
        "h": max(2, h - 2 * inset),
        "_fill": blen,
    }


def main() -> int:
    img = Image.open(sys.argv[1]).convert("RGB")
    a = np.asarray(img)[SCAN_TOP:SCAN_BOTTOM, : img.width // 2].astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]

    # 三条各自的颜色特征(HP=红橙主导, FP=蓝青主导, 耐力=绿主导)
    masks = {
        "hp": (r >= 90) & (r - np.maximum(g, b) >= 25),
        "fp": (b >= 80) & (b - r >= 20),
        "stamina": (g >= 80) & (g - r >= 20) & (g - b >= 10),
    }
    cfg = load_config()
    bars = {}
    hp_band = band_for(masks["hp"], SCAN_TOP)
    if hp_band is None:
        print("!! HP 条必须找到,标定失败")
        return 2

    for name, mask in masks.items():
        if name == "hp":
            band = hp_band
        else:
            # 锚定 HP 条:FP/耐力紧贴其下方,只在这个窄窗口找。全区搜索会
            # 把蓝天(FP 掩码)、草地(耐力掩码)当成条 —— 首次标定就踩了。
            top = hp_band["y"] + hp_band["h"]
            win = mask[top - SCAN_TOP : top - SCAN_TOP + 130, : hp_band["x"] + 1400]
            win = win.copy()
            win[:, : max(0, hp_band["x"] - 20)] = False  # 起点必须与 HP 对齐
            band = band_for(win, top)
        if band is None:
            print(f"[warn] 未找到 {name} 条")
            continue
        fill = band.pop("_fill")
        # 右侧留成长余量(血/FP 上限会随等级变长);耐力上限较稳
        grow = 2.5 if name in ("hp", "fp") else 1.6
        band["w"] = min(int(fill * grow), img.width // 2 - band["x"] - 4)
        bars[name] = band
        print(f"{name}: {band} (标定时填充 {fill}px)")

    if "hp" not in bars:
        print("!! HP 条必须找到,标定失败")
        return 2

    # 队友栏:HP 条下方约 1.5 倍条距处的左侧区域(存在性判据,不看具体值)
    hp = bars["hp"]
    step = (bars.get("stamina", hp)["y"] - hp["y"]) or 60
    cfg["bar"] = {k: hp[k] for k in ("x", "y", "w", "h")}
    cfg["bars"] = bars
    cfg["allyPanel"] = {
        "x": hp["x"] - 240 if hp["x"] > 260 else 60,
        "y": hp["y"] + int(step * 2.2),
        "w": 520,
        "h": int(step * 3),
    }
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"\n配置已写入 {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    sys.exit(main())
