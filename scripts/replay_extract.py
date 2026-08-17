"""从录像里为每个危机段挑帧 —— 挑的依据是"哪一帧看得清敌人",不是"哪一帧发生了事"。

═══ 为什么这个脚本能存在 ═══
实时探针时代,能用的帧只有环形缓冲里恰好留下的那几张:间隔 0.4 秒、赶上动态
模糊就是一团糊、想看事件前 5 秒根本没有。于是"敌人是谁"这个问题经常无解 ——
不是模型不行,是**存下来的东西没法判**。

全程录像之后,像素全在。帧不再是"捞到什么算什么",而是可以**挑**:
  · 时间上任取 —— 事件前的远景(认种类)、变化时刻(认过程)、事后(认结果)
  · 同一时刻取多个候选,按清晰度挑最好的那张(模糊帧等于没有帧)
  · 挑中之后再回原片按 4K 原尺寸精抽 —— 缩小的图会把小字和轮廓抹掉,
    实测同一只怪:整屏缩到 896px 读成"负伤恶螣",原尺寸裁切读出"负伤恶魔"

两阶段正是为了兼顾这两头:先用小图快速扫清晰度(便宜),再对选中的少数时刻
原尺寸精抽(贵但只抽几张)。

用法:
  python replay_extract.py SESSION_DIR [--video PATH] [--lead 5] [--tail 2]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from offline_probe import FFMPEG, NO_WINDOW  # noqa: E402

SCAN_WIDTH = 960          # 扫描阶段的宽度:够算清晰度,不够认字
SCAN_FPS = 10.0           # 扫描采样率
MOMENT_SECONDS = 0.6      # 每 0.6 秒算一个"时刻",在时刻内挑最清晰的一帧
CONTRACT = "replay-frames/1"


def sharpness(gray: np.ndarray) -> float:
    """拉普拉斯方差 —— 动态模糊帧的高频能量会塌下去。

    不用 cv2:一个 3×3 卷积没必要引依赖,而且 numpy 版在小图上更快。
    """
    lap = (gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] +
           gray[1:-1, 2:] - 4.0 * gray[1:-1, 1:-1])
    return float(lap.var())


def scan_window(video: Path, t0: float, dur: float) -> list[tuple[float, float]]:
    """低清扫一段,返回 [(视频内秒数, 清晰度)]。"""
    h = 2  # 占位,真实高度由 ffmpeg 按比例决定
    cmd = [FFMPEG, "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
           "-i", str(video), "-vf", f"fps={SCAN_FPS},scale={SCAN_WIDTH}:-2",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=600,
                         creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(f"扫描失败 t0={t0}: "
                           f"{out.stderr.decode('utf-8', 'replace')[:200]}")
    # 高度未知 → 从字节数反推(宽已知,帧数 = dur*fps 的整数近似)
    n = max(1, int(round(dur * SCAN_FPS)))
    if len(out.stdout) < SCAN_WIDTH * 2:
        return []
    h = len(out.stdout) // (SCAN_WIDTH * n) if n else 0
    if h < 2:
        n = 1
        h = len(out.stdout) // SCAN_WIDTH
    per = SCAN_WIDTH * h
    rows = []
    for i in range(len(out.stdout) // per):
        g = np.frombuffer(out.stdout[i * per:(i + 1) * per],
                          np.uint8).reshape(h, SCAN_WIDTH).astype(np.float32)
        rows.append((t0 + i / SCAN_FPS, sharpness(g)))
    return rows


def pick_moments(scan: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """按固定时刻分桶,每桶取最清晰的一帧 —— 时间覆盖要均匀,清晰度要最好。"""
    if not scan:
        return []
    buckets: dict[int, tuple[float, float]] = {}
    t_base = scan[0][0]
    for t, s in scan:
        k = int((t - t_base) / MOMENT_SECONDS)
        if k not in buckets or s > buckets[k][1]:
            buckets[k] = (t, s)
    return [buckets[k] for k in sorted(buckets)]


def grab_full(video: Path, t: float, dst: Path) -> bool:
    """原尺寸精抽一帧 —— 缩小会把要认的东西抹掉,所以这一步不缩。"""
    out = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "2", "-y", str(dst)],
        capture_output=True, timeout=300, creationflags=NO_WINDOW)
    return out.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def episodes_from_ledger(path: Path) -> list[dict]:
    """把 ledger 里的 start/end 配成段;没有配对的 start 用最后时刻兜底。"""
    eps, cur = [], None
    last_t = 0.0
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        last_t = max(last_t, e.get("videoSecond", 0.0))
        if e["kind"] == "episode-start":
            cur = {"start": e["videoSecond"], "startEvent": e, "drops": []}
        elif e["kind"] == "hp-drop" and cur:
            cur["drops"].append(e["videoSecond"])
        elif e["kind"] == "episode-end" and cur:
            cur["end"] = e["videoSecond"]
            cur["endEvent"] = e
            eps.append(cur)
            cur = None
    if cur:
        cur["end"] = last_t
        cur["endEvent"] = None
        eps.append(cur)
    return eps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", help="offline_probe 产出的 session 目录")
    ap.add_argument("--video", help="录像路径;默认读 session.json")
    ap.add_argument("--lead", type=float, default=5.0,
                    help="事件前多久开始取帧(认敌人种类要远景)")
    ap.add_argument("--tail", type=float, default=2.0,
                    help="事件后多久停止(认结果)")
    ap.add_argument("--max-frames", type=int, default=14,
                    help="每段最多精抽几帧")
    a = ap.parse_args()

    sess = Path(a.session)
    meta = json.loads((sess / "session.json").read_text("utf-8"))
    video = Path(a.video) if a.video else Path(meta["video"])
    if not video.exists():
        raise SystemExit(f"找不到录像 {video}")

    eps = episodes_from_ledger(sess / "ledger.jsonl")
    print(f"{len(eps)} 个危机段 | 录像 {video.name}")
    root = sess / "replay"
    root.mkdir(exist_ok=True)
    index = []

    for i, ep in enumerate(eps, 1):
        t0 = max(0.0, ep["start"] - a.lead)
        t1 = ep["end"] + a.tail
        dur = max(1.0, t1 - t0)
        try:
            scan = scan_window(video, t0, dur)
        except RuntimeError as err:
            print(f"  [{i:02d}] 扫描失败,跳过:{err}")
            continue
        moments = pick_moments(scan)
        if not moments:
            print(f"  [{i:02d}] 窗口内没抽到帧,跳过")
            continue
        # 帧数超限时按清晰度取前 N,但**保留时间顺序**,别让画面跳来跳去
        if len(moments) > a.max_frames:
            keep = sorted(sorted(moments, key=lambda m: -m[1])[:a.max_frames])
        else:
            keep = moments

        d = root / f"ep{i:02d}"
        d.mkdir(exist_ok=True)
        frames = []
        for j, (t, s) in enumerate(keep):
            dst = d / f"{j:02d}_t{t:07.2f}.jpg"
            if not grab_full(video, t, dst):
                continue
            rel = t - ep["start"]
            phase = ("事前远景" if rel < -0.3 else
                     "命中前后" if rel <= (ep["end"] - ep["start"]) else "事后")
            frames.append({"file": dst.name, "videoSecond": round(t, 2),
                           "relToStart": round(rel, 2), "phase": phase,
                           "sharpness": round(s, 1)})
        sharp = [f["sharpness"] for f in frames]
        info = {"contract": CONTRACT, "episode": i,
                "start": ep["start"], "end": ep["end"],
                "drops": ep["drops"], "window": [round(t0, 2), round(t1, 2)],
                "frames": frames}
        (d / "manifest.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
        index.append(info)
        print(f"  [{i:02d}] {ep['start']:7.1f}s~{ep['end']:7.1f}s "
              f"掉血 {len(ep['drops'])} 次 → {len(frames)} 帧 "
              f"(清晰度 {min(sharp):.0f}~{max(sharp):.0f})" if sharp else
              f"  [{i:02d}] 无帧")

    (root / "index.json").write_text(
        json.dumps({"contract": CONTRACT, "video": str(video),
                    "episodes": index}, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n→ {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
