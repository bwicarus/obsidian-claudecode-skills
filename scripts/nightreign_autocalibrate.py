"""黑夜君临探针自动标定:等游戏回前台 → 抓屏 → 找血条 → 写配置 → 拉起探针。

游戏是独占全屏,失焦即最小化,所以不能要求用户停住配合抓图。本脚本后台等待,
前台窗口标题含 NIGHTREIGN 且稳定 2 秒后自动完成整条链,全程无需用户操作。

血条自动定位:在屏幕上部(前 300 行)逐行找"红/橙占优"像素的最长连续段
(容忍 2px 间隙)。HP 条是 200px+ 的实心长条;Afterburner OSD 文字、图标等
只会产生短碎段,阈值 120px 天然排除。取连续若干行组成的条带作为矩形,
右侧留生长余量(血条会随血上限变长)。
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nightreign_probe import (  # noqa: E402
    CONFIG_PATH,
    STATE_ROOT,
    _foreground_title,
    _set_dpi_aware,
    load_config,
)

PYTHON = sys.executable
PROBE = Path(__file__).parent / "nightreign_probe.py"
TITLE_KEY = "NIGHTREIGN"
WAIT_TIMEOUT_S = 15 * 60
STABLE_S = 2.0
SCAN_ROWS = 300  # 只扫屏幕上部
MIN_RUN_PX = 120  # 4K 下 HP 条实心段远超此值;OSD 文字碎段远低于
GAP_TOLERANCE = 2
GROWTH_FACTOR = 2.5  # 右侧生长余量
R_MIN, DOMINANCE = 90, 25


def classify(r: int, g: int, b: int) -> bool:
    return r >= R_MIN and (r - (g if g > b else b)) >= DOMINANCE


def longest_run(row_flags: list[bool]) -> tuple[int, int]:
    """返回 (起点, 长度) 的最长连续 True 段,容忍 GAP_TOLERANCE 的间隙。"""
    best_start, best_len = 0, 0
    start, gap, length = -1, 0, 0
    for i, f in enumerate(row_flags):
        if f:
            if start < 0:
                start, length, gap = i, 1, 0
            else:
                length = i - start + 1
                gap = 0
        elif start >= 0:
            gap += 1
            if gap > GAP_TOLERANCE:
                if length > best_len:
                    best_start, best_len = start, length
                start = -1
    if start >= 0 and length > best_len:
        best_start, best_len = start, length
    return best_start, best_len


def detect_bar(raw: bytes, width: int, rows: int) -> dict | None:
    per_row: list[tuple[int, int]] = []
    for y in range(rows):
        base = y * width * 4
        flags = [
            classify(raw[base + x * 4 + 2], raw[base + x * 4 + 1], raw[base + x * 4])
            for x in range(width // 2)  # 血条在左半屏
        ]
        per_row.append(longest_run(flags))
    best_y = max(range(rows), key=lambda y: per_row[y][1])
    bx, blen = per_row[best_y]
    if blen < MIN_RUN_PX:
        return None
    # 向上下扩展成条带:行的段要够长且 x 范围重叠
    y0 = y1 = best_y
    while y0 > 0:
        x, ln = per_row[y0 - 1]
        if ln >= blen * 0.6 and abs(x - bx) <= 12:
            y0 -= 1
        else:
            break
    while y1 < rows - 1:
        x, ln = per_row[y1 + 1]
        if ln >= blen * 0.6 and abs(x - bx) <= 12:
            y1 += 1
        else:
            break
    band_h = y1 - y0 + 1
    if band_h < 3:
        return None
    # 取条带内圈,避开描边;宽度按生长余量放大
    inner_y = y0 + max(1, band_h // 4)
    inner_h = max(2, band_h - 2 * max(1, band_h // 4))
    w = min(int(blen * GROWTH_FACTOR), width // 2 - bx - 4)
    return {
        "x": max(0, bx - 2),
        "y": inner_y,
        "w": w,
        "h": inner_h,
        "detectedFillPx": blen,
        "detectedBandRows": band_h,
    }


def main() -> int:
    import mss

    _set_dpi_aware()
    print(f"等待前台窗口含 {TITLE_KEY!r}(至多 {WAIT_TIMEOUT_S // 60} 分钟)…")
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    stable_since = None
    while True:
        if time.monotonic() > deadline:
            print("超时:游戏一直没回前台,退出。之后重跑本脚本即可。")
            return 1
        front = TITLE_KEY in _foreground_title().upper()
        now = time.monotonic()
        if front:
            if stable_since is None:
                stable_since = now
                print("游戏回到前台,等待画面稳定…")
            elif now - stable_since >= STABLE_S:
                break
        else:
            stable_since = None
        time.sleep(0.25)

    with mss.mss() as sct:
        mon = sct.monitors[1]
        shot = sct.grab(mon)
        png_path = STATE_ROOT / f"calibration-{datetime.now():%Y%m%d-%H%M%S}.png"
        mss.tools.to_png(shot.rgb, shot.size, output=str(png_path))
        print(f"抓屏 {shot.width}x{shot.height} → {png_path}")
        bar = detect_bar(bytes(shot.raw), shot.width, min(SCAN_ROWS, shot.height))

    if bar is None:
        print("未检测到血条(最长红段 <120px)。截图已存,需要人工看图定位。")
        return 2

    detected = {k: bar.pop(k) for k in ("detectedFillPx", "detectedBandRows")}
    print(f"血条矩形 {bar} 检出填充 {detected['detectedFillPx']}px "
          f"条带高 {detected['detectedBandRows']} 行")

    cfg = load_config()
    cfg["bar"] = bar
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"配置已写入 {CONFIG_PATH}")

    log = (STATE_ROOT / "probe.out.log").open("ab")
    proc = subprocess.Popen(
        [PYTHON, str(PROBE)],
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    print(f"探针已拉起 PID {proc.pid},日志 {STATE_ROOT / 'probe.out.log'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
