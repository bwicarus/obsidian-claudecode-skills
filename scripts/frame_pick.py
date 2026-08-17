"""自己抽帧 — 按信号与可辨识度选帧,替代 llama.cpp 硬编码 4fps 的盲抽。

═══ 为什么自己抽 ═══
2026-08-17 读 llama.cpp 源码确认:`--video` 只是 `--image` 的别名(同一个
参数、同一个 vector),它多做的只有"调 ffmpeg 按写死的 fps=4 抽帧"和"每 5
秒插一条纯文本时间戳";M-RoPE 的时间维度 `pos.t = pos_0` 是常量,**没有
时间轴**。而且 ffmpeg 的 fps filter 在目标帧率高于源帧率时会**复制帧** ——
实测一段 8 帧/6.7 秒的素材被复制成 27 帧,近 70% 是冗余,白烧 context。

所以抽帧这件事自己做更好:能按信号挑时刻、能按清晰度挑帧、能去重。

═══ 选帧策略(按优先级)═══
  ① 信号时刻:探针测到的每次血量变化(掉血/回血)各取一帧 —— 因果所在
  ② 上下文:事件前若干秒各取一帧 —— 敌人还在远处、轮廓完整,能认种类
  ③ 结果:事件后取帧 —— 倒地提示/死亡画面/是否脱身
每个目标时刻在邻域内挑**拉普拉斯方差最高**的一帧(避开动态模糊),并对相邻
选帧做感知去重(避免连续几张几乎一样)。

用法:
  python frame_pick.py VIDEO.mp4 OUT_DIR [--at 3.2 --at 5.0] [--count 8]
  python frame_pick.py --from-session SESSION_DIR --event e0155 OUT_DIR
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

FFMPEG = (r"C:\Users\bwica\AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-9.0-full_build\bin\ffmpeg.exe")
NO_WINDOW = 0x08000000


def sharpness(path: Path) -> float:
    """拉普拉斯方差:动态模糊帧显著偏低。"""
    import numpy as np
    from PIL import Image

    im = Image.open(path).convert("L")
    im.thumbnail((320, 320))
    a = np.asarray(im, dtype="float32")
    lap = (-4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1]
           + a[1:-1, :-2] + a[1:-1, 2:])
    return float(np.var(lap))


def phash(path: Path) -> int:
    """8x8 均值感知哈希,用于相邻选帧去重。"""
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(path).convert("L").resize((8, 8)), dtype="float32")
    bits = (a > a.mean()).flatten()
    out = 0
    for b in bits:
        out = (out << 1) | int(b)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def grab(video: Path, second: float, dst: Path) -> bool:
    proc = subprocess.run(
        [FFMPEG, "-y", "-ss", f"{max(0.0, second):.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "2", str(dst)],
        capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
    return proc.returncode == 0 and dst.exists()


def pick_best(video: Path, target: float, tmp: Path, window: float,
              probes: int) -> tuple[Path, float, float] | None:
    """在 target ±window 内取 probes 个候选,返回最清晰的一张。"""
    best = None
    step = (2 * window) / max(1, probes - 1) if probes > 1 else 0.0
    for i in range(probes):
        t = target - window + step * i
        cand = tmp / f"c_{i}.jpg"
        if not grab(video, t, cand):
            continue
        s = sharpness(cand)
        if best is None or s > best[2]:
            if best is not None:
                best[0].unlink(missing_ok=True)
            best = [cand.rename(tmp / f"best_{target:.2f}.jpg"), t, s]
        else:
            cand.unlink(missing_ok=True)
    return tuple(best) if best else None


def pick(video: Path, targets: list[float], out_dir: Path, *,
         window: float = 0.35, probes: int = 3, dedupe: int = 6) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_tmp"
    tmp.mkdir(exist_ok=True)
    picked: list[dict] = []
    last_hash: int | None = None
    for target in sorted(targets):
        got = pick_best(video, target, tmp, window, probes)
        if not got:
            print(f"[warn] {target:.2f}s 抽帧失败")
            continue
        src, actual, sharp = got
        h = phash(src)
        if last_hash is not None and hamming(h, last_hash) <= dedupe:
            print(f"[skip] {actual:.2f}s 与上一张几乎相同(汉明 "
                  f"{hamming(h, last_hash)}),丢弃")
            src.unlink(missing_ok=True)
            continue
        last_hash = h
        name = f"{len(picked):02d}_{actual:+.2f}s.jpg"
        src.rename(out_dir / name)
        picked.append({"file": name, "second": round(actual, 2),
                       "target": round(target, 2), "sharpness": round(sharp, 1)})
    for f in tmp.glob("*.jpg"):
        f.unlink(missing_ok=True)
    tmp.rmdir()
    return picked


def targets_from_session(session: Path, event_id: str) -> tuple[Path, list[float], dict]:
    """从探针台账取信号时刻(变化点)+ 上下文/结果时刻。"""
    ev_dir = session / "evidence" / event_id
    manifest = json.loads((ev_dir / "manifest.json").read_text("utf-8"))
    clip_info = manifest.get("clip")
    if not clip_info:
        raise SystemExit(f"{event_id} 没有 clip.mp4(采于片段功能上线前)")
    video = ev_dir / clip_info["file"]
    start_off = clip_info.get("startOffset", -8.0)

    events = [json.loads(l) for l in
              (session / "ledger.jsonl").read_text("utf-8").splitlines() if l.strip()]
    start = next((e for e in events if e.get("eventId") == event_id), None)
    if start is None:
        raise SystemExit(f"台账里没有 {event_id}")

    def sec(t: str) -> float:
        return int(t[11:13]) * 3600 + int(t[14:16]) * 60 + float(t[17:23])

    a0 = sec(start["ts"])
    rows = []
    for line in (session / "samples.csv").read_text("utf-8").splitlines()[1:]:
        parts = line.split(",")
        try:
            rows.append((sec(parts[0]) - a0, int(parts[1])))
        except (ValueError, IndexError):
            continue
    full = max((v for _, v in rows), default=1) or 1
    changes = []
    for (t0, v0), (t1, v1) in zip(rows, rows[1:]):
        d = (v1 - v0) / full * 100
        if abs(d) >= 8 and start_off <= t1 <= start_off + clip_info["seconds"]:
            changes.append({"offset": round(t1, 2), "deltaPct": round(d, 1),
                            "remainPct": round(v1 / full * 100, 1)})

    # 视频内秒数 = 事件偏移 - 片段起始偏移
    def to_video_sec(off: float) -> float:
        return max(0.0, off - start_off)

    targets = [to_video_sec(c["offset"]) for c in changes]
    span = clip_info["seconds"]
    for extra in (0.3, span * 0.25, span * 0.5, span - 0.4):  # 上下文与结果
        if all(abs(extra - t) > 0.5 for t in targets):
            targets.append(round(extra, 2))
    return video, targets, {"changes": changes, "clip": clip_info,
                            "startOffset": start_off}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", nargs="?")
    ap.add_argument("out_dir")
    ap.add_argument("--at", type=float, action="append", default=[],
                    help="指定抽帧时刻(秒),可重复")
    ap.add_argument("--count", type=int, default=0,
                    help="没给 --at 时均匀抽这么多帧")
    ap.add_argument("--from-session", help="从探针 session 取信号时刻")
    ap.add_argument("--event", help="配合 --from-session 的事件 id")
    ap.add_argument("--window", type=float, default=0.35)
    ap.add_argument("--probes", type=int, default=3)
    a = ap.parse_args()

    meta: dict = {}
    if a.from_session:
        if not a.event:
            raise SystemExit("--from-session 需要配 --event")
        video, targets, meta = targets_from_session(Path(a.from_session), a.event)
        print(f"信号驱动:{len(meta['changes'])} 个变化点 + 上下文/结果 → "
              f"{len(targets)} 个目标时刻")
    else:
        video = Path(a.video)
        targets = list(a.at)
        if not targets:
            probe = subprocess.run(
                [FFMPEG.replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error",
                 "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
            dur = float(probe.stdout.strip() or 1.0)
            n = a.count or 8
            targets = [dur * (i + 0.5) / n for i in range(n)]
            print(f"均匀抽 {n} 帧(时长 {dur:.1f}s)")

    out = Path(a.out_dir)
    picked = pick(video, targets, out, window=a.window, probes=a.probes)
    (out / "picked.json").write_text(json.dumps(
        {"video": str(video), "picked": picked, **meta},
        ensure_ascii=False, indent=2), "utf-8")
    print(f"\n选出 {len(picked)} 帧 → {out}")
    for p in picked:
        print(f"  {p['file']}  清晰度 {p['sharpness']:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
