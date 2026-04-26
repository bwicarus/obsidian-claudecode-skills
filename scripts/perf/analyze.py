"""
性能数据分析脚本（MSI Afterburner HML 版）

解析 Afterburner 的 HardwareMonitoring.hml 日志，
分析各硬件指标与帧率波动的相关性，输出报告。

用法：
  python perf_analyze.py --hml "C:/Program Files (x86)/MSI Afterburner/HardwareMonitoring.hml"
  python perf_analyze.py --hml <路径> --output report.html

若 HML 中无帧率数据，可附加 PresentMon CSV：
  python perf_analyze.py --hml <路径> --presentmon frametimes.csv
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import pandas as pd
import numpy as np
from scipy import stats


# ── HML 解析 ──────────────────────────────────────────────────────────────────

def parse_hml(path: str) -> pd.DataFrame:
    """
    HML 格式（v1.6）：
      00  文件头
      01  硬件名
      02  列名行（逗号分隔，首两列为行号和时间戳）
      03  单位行（两行：量纲和精度）
      数字  数据行
    """
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()

    columns = None
    units = {}
    rows = []

    unit_lines_seen = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        tag = parts[0]

        if tag == "02":
            # 列名行：第1列=行号，第2列=时间戳，其余=传感器名
            columns = ["_tag", "timestamp"] + parts[2:]
        elif tag == "03" and columns is not None:
            # 两行单位：第一行是单位符号，第二行是精度/量程
            if unit_lines_seen == 0:
                for i, col in enumerate(columns[2:], start=2):
                    if i < len(parts):
                        u = parts[i].strip()
                        if u:
                            units[col] = u
            unit_lines_seen += 1
        elif tag.isdigit() and columns is not None and unit_lines_seen >= 1:
            if len(parts) < 3:
                continue
            row = {"_tag": parts[0], "timestamp": parts[1]}
            for i, col in enumerate(columns[2:], start=2):
                val = parts[i] if i < len(parts) else ""
                row[col] = val.strip()
            rows.append(row)

    if not rows:
        raise ValueError("HML 文件中未找到数据行，请确认已开始记录。")

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="%d-%m-%Y %H:%M:%S", errors="coerce")
    df = df.drop(columns=["_tag"])
    df = df.set_index("timestamp").sort_index()

    # 转数值
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 去掉全空列
    df = df.dropna(axis=1, how="all")

    return df, units


# ── PresentMon CSV 解析 ────────────────────────────────────────────────────────

def parse_presentmon(path: str) -> pd.DataFrame:
    """
    PresentMon 输出列包含 TimeInSeconds, msBetweenPresents 等。
    """
    df = pd.read_csv(path)
    time_col   = next((c for c in df.columns if "time" in c.lower() and "second" in c.lower()), None)
    ft_col     = next((c for c in df.columns if "msbetween" in c.lower() or "frametime" in c.lower()), None)

    if time_col is None or ft_col is None:
        raise ValueError(f"PresentMon CSV 列名不匹配，找到列：{list(df.columns)}")

    df = df[[time_col, ft_col]].copy()
    df.columns = ["time_s", "frametime_ms"]
    df = df.dropna()
    df["fps"] = 1000.0 / df["frametime_ms"]
    return df


# ── 帧率从 HML 提取（若有）────────────────────────────────────────────────────

def extract_fps_from_hml(df: pd.DataFrame) -> pd.Series | None:
    # 直接帧率列
    fps_col = next((c for c in df.columns if "framerate" in c.lower() or c.strip().lower() == "fps"), None)
    if fps_col:
        return df[fps_col].rename("fps")
    # Frametime 列（ms）→ 转 FPS
    ft_col = next((c for c in df.columns if "frametime" in c.lower()), None)
    if ft_col:
        ft = df[ft_col].replace(0, np.nan)
        fps = (1000.0 / ft).rename("fps")
        print(f"  → 从 Frametime 列计算 FPS（{ft_col}）")
        return fps
    return None


# ── 按秒聚合 ──────────────────────────────────────────────────────────────────

def resample_sensor(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("1s").mean()


def resample_fps(fps: pd.Series) -> pd.DataFrame:
    fps_1s = fps.resample("1s").mean().rename("fps_mean")
    # 每秒一个读数时用 rolling 窗口（10s）计算波动
    fps_std = fps_1s.rolling(10, min_periods=3).std().rename("fps_std")
    fps_p5  = fps_1s.rolling(10, min_periods=3).quantile(0.05).rename("fps_p5")
    return pd.concat([fps_1s, fps_std, fps_p5], axis=1)


# ── 合并帧率与传感器 ──────────────────────────────────────────────────────────

def merge_with_presentmon(sensor_1s: pd.DataFrame, pm_df: pd.DataFrame) -> pd.DataFrame:
    """
    PresentMon 给出 time_s（相对秒），假设与传感器同时开始，对齐后合并。
    """
    pm_df["second"] = pm_df["time_s"].astype(int)
    pm_agg = pm_df.groupby("second")["fps"].agg(
        fps_mean="mean",
        fps_std="std",
        fps_p5=lambda x: x.quantile(0.05)
    ).reset_index(drop=True)
    sensor_reset = sensor_1s.reset_index(drop=True)
    n = min(len(pm_agg), len(sensor_reset))
    return pd.concat([sensor_reset.iloc[:n], pm_agg.iloc[:n]], axis=1).dropna(subset=["fps_mean"])


# ── 相关性分析 ────────────────────────────────────────────────────────────────

def correlate(merged: pd.DataFrame, target: str = "fps_std") -> pd.DataFrame:
    sensor_cols = [c for c in merged.columns if c not in ("fps_mean", "fps_std", "fps_p5")]
    results = []
    for col in sensor_cols:
        common = merged[[col, target]].dropna()
        if len(common) < 10:
            continue
        r, p = stats.pearsonr(common[col], common[target])
        results.append({
            "指标": col,
            "相关系数 r": round(r, 4),
            "p值": round(p, 5),
            "显著(p<0.05)": "✓" if p < 0.05 else ""
        })
    if not results:
        return pd.DataFrame(columns=["指标", "相关系数 r", "p值", "显著(p<0.05)"])
    return pd.DataFrame(results).sort_values("相关系数 r", key=abs, ascending=False)


# ── 多元回归 ──────────────────────────────────────────────────────────────────

def regression(merged: pd.DataFrame, corr_df: pd.DataFrame, top_n: int = 8) -> str:
    try:
        from sklearn.linear_model import LinearRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import r2_score
    except ImportError:
        return "（跳过回归：pip install scikit-learn）"

    target = "fps_std"
    sig = corr_df[corr_df["显著(p<0.05)"] == "✓"]["指标"].head(top_n).tolist()
    if not sig:
        return "无显著指标，跳过回归。"

    sub = merged[sig + [target]].dropna()
    if len(sub) < 20:
        return f"样本不足（{len(sub)}行），跳过回归。"

    X = StandardScaler().fit_transform(sub[sig].values)
    y = sub[target].values
    model = LinearRegression().fit(X, y)
    r2 = r2_score(y, model.predict(X))

    lines = [f"R² = {r2:.4f}（解释了帧率波动 {r2*100:.1f}% 的方差）\n"]
    coef_df = pd.DataFrame({"指标": sig, "标准化系数": model.coef_}).sort_values(
        "标准化系数", key=abs, ascending=False
    )
    lines.append(coef_df.to_string(index=False))
    return "\n".join(lines)


# ── 报告 ──────────────────────────────────────────────────────────────────────

def print_report(merged: pd.DataFrame, corr_df: pd.DataFrame, units: dict) -> None:
    print("\n══════════════════════════════════════════════════")
    print("          帧率波动影响因素分析报告")
    print("══════════════════════════════════════════════════")
    print(f"采样时长：{len(merged)} 秒  |  传感器指标数：{len([c for c in merged.columns if 'fps' not in c])}")

    if "fps_mean" in merged.columns:
        print(f"平均 FPS：{merged['fps_mean'].mean():.1f}  ±  {merged['fps_mean'].std():.1f}")
        print(f"帧率波动（fps_std 均值）：{merged['fps_std'].mean():.2f}")
    else:
        print("⚠ 无帧率数据")
        return

    print("\n── 与帧率波动（fps_std）相关性最强的指标 ──")
    top_sig = corr_df[corr_df["显著(p<0.05)"] == "✓"].head(15)
    if top_sig.empty:
        print("无显著相关指标（可能数据量不足或帧率非常稳定）")
    else:
        print(top_sig.to_string(index=False))

    print("\n── 多元线性回归 ──")
    print(regression(merged, corr_df))
    print("══════════════════════════════════════════════════\n")


# ── 主函数 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="分析 Afterburner HML 日志中各指标与帧率波动的关系")
    parser.add_argument("--hml",        required=True, help="HardwareMonitoring.hml 路径")
    parser.add_argument("--presentmon", default=None,  help="PresentMon CSV 路径（若 HML 无帧率数据）")
    parser.add_argument("--output",     default=None,  help="输出 HTML 报告路径（可选）")
    parser.add_argument("--start",      default=None,  help="分析起始时间（HH:MM:SS），可选")
    parser.add_argument("--end",        default=None,  help="分析结束时间（HH:MM:SS），可选")
    args = parser.parse_args()

    print(f"解析 HML：{args.hml}")
    df, units = parse_hml(args.hml)
    print(f"  → {len(df)} 行，{len(df.columns)} 个指标，时间范围：{df.index[0]} ~ {df.index[-1]}")

    # 时间切片
    if args.start:
        df = df[df.index.strftime("%H:%M:%S") >= args.start]
    if args.end:
        df = df[df.index.strftime("%H:%M:%S") <= args.end]

    sensor_1s = resample_sensor(df)

    # 帧率来源
    fps_series = extract_fps_from_hml(df)
    if fps_series is not None:
        print(f"  → 从 HML 找到帧率列：{fps_series.name}")
        fps_1s = resample_fps(fps_series)
        merged = sensor_1s.join(fps_1s, how="inner").dropna(subset=["fps_mean"])
    elif args.presentmon:
        print(f"解析 PresentMon：{args.presentmon}")
        pm_df = parse_presentmon(args.presentmon)
        print(f"  → {len(pm_df)} 帧")
        merged = merge_with_presentmon(sensor_1s, pm_df)
    else:
        print("\n[!] HML 中无帧率数据，且未提供 --presentmon。")
        print("请在 Afterburner 设置 → 监控 → 勾选「Framerate」→ 启用记录，")
        print("或运行游戏时用 PresentMon 采集帧时间后加 --presentmon 参数。")
        print("\n当前可用指标：")
        for col in df.columns:
            u = units.get(col, "")
            print(f"  {col}  [{u}]" if u else f"  {col}")
        sys.exit(0)

    corr_df = correlate(merged)
    print_report(merged, corr_df, units)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("<html><meta charset='utf-8'><body>")
            f.write("<h2>相关性分析（按绝对相关系数排序）</h2>")
            f.write(corr_df.to_html(index=False))
            f.write("<h2>逐秒数据（后200行）</h2>")
            show_cols = ["fps_mean", "fps_std"] + [c for c in merged.columns if "fps" not in c][:12]
            f.write(merged[show_cols].tail(200).to_html())
            f.write("</body></html>")
        print(f"HTML 报告已写入：{args.output}")


if __name__ == "__main__":
    main()
