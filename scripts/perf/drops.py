"""
帧率低点原因分析脚本

找出帧率跌破阈值的时间段，对比低帧与正常帧时段的硬件数据差异，
给出每次低帧事件的详情。

用法：
  python drops.py --hml "C:/Program Files (x86)/MSI Afterburner/HardwareMonitoring.hml"
  python drops.py --hml <路径> --threshold 50 --start 14:30:00 --end 14:45:00
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

# ── HML 解析（与 analyze.py 相同）────────────────────────────────────────────

def parse_hml(path: str) -> pd.DataFrame:
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    columns = None
    unit_lines_seen = 0
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        tag = parts[0]
        if tag == "02":
            columns = ["_tag", "timestamp"] + parts[2:]
        elif tag == "03" and columns is not None:
            unit_lines_seen += 1
        elif tag.isdigit() and columns is not None and unit_lines_seen >= 1:
            if len(parts) < 3:
                continue
            row = {"_tag": parts[0], "timestamp": parts[1]}
            for i, col in enumerate(columns[2:], start=2):
                row[col] = parts[i] if i < len(parts) else ""
            rows.append(row)
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="%d-%m-%Y %H:%M:%S", errors="coerce")
    df = df.drop(columns=["_tag"]).set_index("timestamp").sort_index()
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(axis=1, how="all")


KEY_COLS = [
    "GPU1 power", "GPU1 temperature", "GPU1 usage", "GPU1 core clock",
    "GPU1 memory usage", "GPU1 memory clock",
    "GPU2 usage",
    "CPU usage", "CPU power", "CPU temperature", "CPU clock",
    "RAM usage", "Commit charge",
    "GPU1 power limit", "GPU1 temp limit",
]

DETAIL_COLS = [
    "GPU1 power", "GPU2 usage", "GPU1 memory usage", "CPU power", "RAM usage"
]


def find_fps_col(df: pd.DataFrame) -> str | None:
    return next(
        (c for c in df.columns if "framerate" in c.lower() or c.strip().lower() == "fps"),
        None
    )


def main():
    parser = argparse.ArgumentParser(description="分析帧率低点原因")
    parser.add_argument("--hml",       required=True,        help="HardwareMonitoring.hml 路径")
    parser.add_argument("--threshold", type=float, default=50, help="帧率低点阈值，默认 50")
    parser.add_argument("--start",     default=None,         help="分析起始时间 HH:MM:SS")
    parser.add_argument("--end",       default=None,         help="分析结束时间 HH:MM:SS")
    args = parser.parse_args()

    print(f"解析 HML：{args.hml}")
    df = parse_hml(args.hml)
    print(f"  → {len(df)} 行，时间范围：{df.index[0]} ~ {df.index[-1]}")

    if args.start:
        df = df[df.index.strftime("%H:%M:%S") >= args.start]
    if args.end:
        df = df[df.index.strftime("%H:%M:%S") <= args.end]

    fps_col = find_fps_col(df)
    if fps_col is None:
        print("[!] 未找到帧率列（Framerate/fps），请确认 Afterburner 已记录帧率。")
        sys.exit(1)

    fps = df[fps_col]
    normal_fps = fps[fps >= args.threshold].mean()
    drops = df[fps < args.threshold].copy()

    print(f"\n正常平均帧率：{normal_fps:.1f} FPS")
    print(f"跌破 {args.threshold} FPS 的时间点：{len(drops)} 个")

    if drops.empty:
        print("没有找到低帧时间点。")
        return

    # ── 对比分析 ──────────────────────────────────────────────────────────────
    normal = df[fps >= args.threshold]
    cols = [c for c in KEY_COLS if c in df.columns]

    print(f"\n{'=' * 65}")
    print(f"{'指标':<35} {'正常均值':>10} {'低帧均值':>10} {'差异':>8}")
    print(f"{'=' * 65}")
    for col in cols:
        n_mean = normal[col].mean()
        d_mean = drops[col].mean()
        if pd.isna(n_mean) or pd.isna(d_mean):
            continue
        diff = d_mean - n_mean
        pct = abs(diff) / (abs(n_mean) + 1e-9)
        flag = " <--" if pct > 0.05 else ""
        print(f"{col:<35} {n_mean:>10.1f} {d_mean:>10.1f} {diff:>+8.1f}{flag}")

    # ── 按连续事件分组 ─────────────────────────────────────────────────────────
    drop_idx = drops.index.tolist()
    events: list[list] = []
    group: list = [drop_idx[0]]
    for t in drop_idx[1:]:
        if (t - group[-1]).total_seconds() <= 3:
            group.append(t)
        else:
            events.append(group)
            group = [t]
    events.append(group)

    # ── 逐事件输出 ─────────────────────────────────────────────────────────────
    detail_cols = [c for c in DETAIL_COLS if c in df.columns]
    print(f"\n\n── {len(events)} 次低帧事件详情（阈值 < {args.threshold} FPS）──\n")

    for i, event in enumerate(events, 1):
        event_df = df.loc[event]
        fps_vals = event_df[fps_col]
        duration = (event[-1] - event[0]).total_seconds() + 1

        print(f"[事件 {i}]  {event[0].strftime('%H:%M:%S')} ~ {event[-1].strftime('%H:%M:%S')}"
              f"  持续 {duration:.0f}s  FPS: {fps_vals.min():.1f} ~ {fps_vals.max():.1f}")

        # 特征诊断
        gpu1_power = event_df.get("GPU1 power", pd.Series(dtype=float)).mean()
        gpu2_usage = event_df.get("GPU2 usage", pd.Series(dtype=float)).mean()
        normal_gpu1 = normal.get("GPU1 power", pd.Series(dtype=float)).mean()

        reasons = []
        if not np.isnan(gpu1_power) and not np.isnan(normal_gpu1):
            if gpu1_power < normal_gpu1 * 0.5:
                reasons.append(f"独显功耗骤降（{gpu1_power:.0f}W vs 正常 {normal_gpu1:.0f}W）→ 游戏切换/加载界面/窗口失焦")
            elif gpu1_power > normal_gpu1 * 0.95:
                mem = event_df.get("GPU1 memory usage", pd.Series(dtype=float)).mean()
                normal_mem = normal.get("GPU1 memory usage", pd.Series(dtype=float)).mean()
                if not np.isnan(mem) and mem > normal_mem * 1.05:
                    reasons.append(f"显存压力（{mem:.0f}MB vs 正常 {normal_mem:.0f}MB）→ 加载新场景/资产")
                else:
                    reasons.append("GPU 满载但帧率不足 → 场景复杂度超出显卡能力")
        if not np.isnan(gpu2_usage) and gpu2_usage > 10:
            reasons.append(f"核显介入（GPU2 使用率 {gpu2_usage:.0f}%）→ 独显卸载/混合显卡切换")

        if reasons:
            for r in reasons:
                print(f"   原因：{r}")
        else:
            print("   原因：未识别（数据不足）")
        print()


if __name__ == "__main__":
    main()
