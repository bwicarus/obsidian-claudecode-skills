"""从录像离线重建血条时间轴 — 实时探针的兜底,也是重放能力。

═══ 为什么需要(2026-08-17 实测教训)═══
第一次全程录像的那一场,实时探针**进程已经退出**却没有任何东西报警,
36 分钟的游戏一个事件都没记 —— 又一次静默失败。

而录像完好。这正是"全程录像"最大的价值:**它是对所有实时采集失败的兜底**。
像素全在,时间轴可以事后重建。

更进一步,这让整个系统符合三层存储的本意:
  · 原始层(录像)不可重来 → 所以要录全
  · 提炼层(信号/事件)可以重跑 → 换算法、改阈值、修 bug 后重新提取即可
实时探针从此降级成"顺手做的加速",不再是唯一的信号来源。

═══ 实现要点 ═══
不解码整帧:用 ffmpeg 的 crop 只输出血条那一小条(1400×9),rawvideo 管道
喂给 Python。4K 逐帧解码很贵,但硬件解码 + crop 后每帧只有 37KB,36 分钟
的素材几分钟就能跑完。

测量逻辑直接复用 nightreign_probe 的 measure_bar(第五版,锚填充段均匀性,
对血条变色免疫 —— 实测这一场血条就是粉色的)。

用法:
  python offline_probe.py RECORDING.mp4 [--fps 8] [--out SESSION_DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nightreign_probe import (  # noqa: E402
    EpisodeTracker, HudState, StabilityFilter, classify_change,
    count_cool_pixels, load_config, measure_bar,
)

FFMPEG = (r"C:\Users\bwica\AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-9.0-full_build\bin\ffmpeg.exe")
NO_WINDOW = 0x08000000
LEDGER_CONTRACT = "game-ledger/2"


def probe_video(path: Path) -> dict:
    out = subprocess.run(
        [FFMPEG.replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error",
         "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-show_entries",
         "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise SystemExit(f"ffprobe 失败: {out.stderr.strip()[:300]}")
    j = json.loads(out.stdout)
    st = j["streams"][0]
    num, _, den = st["r_frame_rate"].partition("/")
    return {"w": st["width"], "h": st["height"],
            "fps": float(num) / float(den or 1),
            "seconds": float(j["format"]["duration"])}


def crop_stream(path: Path, rect: dict, fps: float, scale_x: float,
                scale_y: float):
    """只解码并输出血条那一小块,rawvideo 走管道。"""
    x = int(rect["x"] * scale_x)
    y = int(rect["y"] * scale_y)
    w = max(2, int(rect["w"] * scale_x))
    h = max(2, int(rect["h"] * scale_y))
    cmd = [FFMPEG, "-v", "error", "-i", str(path),
           "-vf", f"fps={fps},crop={w}:{h}:{x}:{y}",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    return proc, w, h


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video")
    ap.add_argument("--fps", type=float, default=8.0,
                    help="重建采样率;跟实时探针一致(8Hz)便于对照")
    ap.add_argument("--out", help="输出 session 目录;默认按录像名生成")
    a = ap.parse_args()

    video = Path(a.video)
    if not video.exists():
        raise SystemExit(f"找不到 {video}")
    info = probe_video(video)
    cfg = load_config()
    bars = cfg["bars"]
    barcfg, cool = cfg["bar"], cfg["cool"]

    # 血条坐标是按 4K 屏标定的;录像分辨率不同则按比例缩放
    scale_x = info["w"] / 3840
    scale_y = info["h"] / 2160
    print(f"录像 {info['w']}x{info['h']} {info['fps']:.0f}fps "
          f"{info['seconds']/60:.1f} 分钟 | 坐标缩放 {scale_x:.3f}")

    # 起始墙钟时间:优先读录制器写的 meta,否则用文件名里的时间戳
    meta_path = video.with_suffix("").with_suffix(".meta.json")
    if not meta_path.exists():
        meta_path = video.parent / (video.stem + ".meta.json")
    if meta_path.exists():
        base_epoch = json.loads(meta_path.read_text("utf-8"))["startedAtEpoch"]
        print(f"基准时刻取自 {meta_path.name}")
    else:
        stamp = video.stem.split("-")[0] + "-" + video.stem.split("-")[1]
        base_epoch = datetime.strptime(stamp, "%Y%m%d-%H%M%S").timestamp()
        print("[warn] 没有 meta.json,基准时刻退回文件名(可能差几秒)")
    base_dt = datetime.fromtimestamp(base_epoch, timezone.utc).astimezone()

    out_dir = Path(a.out) if a.out else (
        video.parent.parent / "nightreign" / f"offline-{video.stem}")
    (out_dir / "evidence").mkdir(parents=True, exist_ok=True)
    samples = (out_dir / "samples.csv").open("w", encoding="utf-8")
    samples.write("ts,hp_cols,ghost_cols,total_cols,fp,stamina,hud\n")
    ledger = (out_dir / "ledger.jsonl").open("w", encoding="utf-8")

    procs = {}
    for name in ("hp", "fp", "stamina"):
        procs[name] = crop_stream(video, bars[name], a.fps, scale_x, scale_y)

    stability = StabilityFilter(int(cfg["stabilityTicks"]),
                                int(cfg["stabilityToleranceCols"]))
    hud = HudState(int(cfg["hudGoneThreshold"]), int(cfg["hudConfirmTicks"]))
    episode = EpisodeTracker(float(cfg["episodeQuietSeconds"]))
    hit_px = int(cfg["hitDropCols"])
    ep_open = int(cfg["epOpenCols"])
    heal_close = int(cfg["healCloseCols"])

    seq = 0
    frame_i = 0
    last_hud = "on"
    t0 = time.monotonic()

    def emit(kind, before, after, extra=None):
        nonlocal seq
        seq += 1
        ts = (base_dt + timedelta(seconds=frame_i / a.fps)).isoformat(
            timespec="milliseconds")
        line = {"contract": LEDGER_CONTRACT, "eventId": f"e{seq:04d}",
                "ts": ts, "game": "nightreign", "channel": "hud",
                "kind": kind, "pxBefore": before, "pxAfter": after,
                "delta": after - before, "videoSecond": round(frame_i / a.fps, 2)}
        if extra:
            line.update(extra)
        ledger.write(json.dumps(line, ensure_ascii=False) + "\n")
        return line

    try:
        while True:
            raw = {}
            for name, (proc, w, h) in procs.items():
                need = w * h * 4
                buf = proc.stdout.read(need)
                if len(buf) < need:
                    raise StopIteration
                raw[name] = (buf, w, h)

            hp_raw, hp_ghost, hp_total, _ = measure_bar(
                raw["hp"][0], raw["hp"][1], raw["hp"][2],
                skip_left=int(barcfg["skipLeft"] * scale_x),
                ref_cols=int(barcfg["refCols"] * scale_x),
                tol=barcfg["tol"], run=int(barcfg["run"] * scale_x),
                sat_solid=barcfg["satSolid"])
            fp_lit = count_cool_pixels(raw["fp"][0], cool["minLevel"],
                                       cool["dominance"])
            st_lit = count_cool_pixels(raw["stamina"][0], cool["minLevel"],
                                       cool["dominance"])
            hud_state = hud.feed(fp_lit, st_lit)
            now = frame_i / a.fps

            ts = (base_dt + timedelta(seconds=now)).isoformat(
                timespec="milliseconds")
            samples.write(f"{ts},{hp_raw},{hp_ghost},{hp_total},"
                          f"{fp_lit},{st_lit},{hud_state}\n")

            if hud_state != last_hud:
                emit(f"hud-{hud_state}", 0, 0,
                     {"fp": fp_lit, "stamina": st_lit})
                if hud_state == "gone" and episode.active:
                    s = episode.close(stability.confirmed or hp_raw, now, "gone")
                    emit("episode-end", s["pxBefore"], s["pxAfter"], s)
                last_hud = hud_state

            if hud_state == "on":
                tr = stability.feed(hp_raw)
                if tr is not None:
                    prev_c, cur_c = tr
                    kind = classify_change(
                        prev_c, cur_c,
                        drop_threshold=int(cfg["dropThresholdCols"]))
                    if kind == "hp-drop":
                        d = prev_c - cur_c
                        was = episode.active
                        if was or d >= ep_open:
                            if episode.on_decrease(prev_c, cur_c, now):
                                emit("episode-start", prev_c, cur_c,
                                     {"fp": fp_lit, "stamina": st_lit,
                                      "ghostCols": hp_ghost})
                            elif d >= hit_px:
                                emit("hp-drop", prev_c, cur_c)
                    elif kind == "hp-gain" and cur_c - prev_c >= heal_close:
                        if episode.active:
                            s = episode.close(prev_c, now, "heal")
                            emit("episode-end", s["pxBefore"], prev_c, s)
                        emit("hp-gain", prev_c, cur_c)
            if episode.quiet_elapsed(now):
                cur = stability.confirmed if stability.confirmed is not None else hp_raw
                s = episode.close(cur, now, "quiet")
                emit("episode-end", s["pxBefore"], cur, s)

            frame_i += 1
            if frame_i % (int(a.fps) * 120) == 0:
                el = time.monotonic() - t0
                done = now / info["seconds"]
                print(f"  {now/60:5.1f}/{info['seconds']/60:.1f} 分钟 "
                      f"({done:4.0%}) | 已耗时 {el/60:.1f} 分钟 "
                      f"| 事件 {seq}")
    except StopIteration:
        pass
    finally:
        for proc, _, _ in procs.values():
            proc.stdout.close()
            proc.terminate()
        samples.close()
        ledger.close()

    (out_dir / "session.json").write_text(json.dumps({
        "contract": LEDGER_CONTRACT, "game": "nightreign",
        "source": "offline", "video": str(video),
        "startedAt": base_dt.isoformat(timespec="milliseconds"),
        "sampleFps": a.fps, "videoInfo": info, "config": cfg,
    }, ensure_ascii=False, indent=2), "utf-8")

    el = time.monotonic() - t0
    print(f"\n完成:{frame_i} 帧 / {seq} 事件 / 耗时 {el/60:.1f} 分钟")
    print(f"→ {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
