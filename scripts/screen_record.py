"""全程录屏 — 让原始层真正原始。

═══ 为什么(2026-08-17 晚上撞了三次墙之后的结论)═══
探针只存"它当时认为重要的帧"。一旦事后想换方法分析,素材已经没了:
  · 光流要相邻帧      → 证据帧间隔 1-2.7 秒,算出来是噪声
  · V-JEPA 2 要连续帧 → 8 帧重复采样补齐,对它是 8 张不相关的图
  · 看清命中瞬间      → 命中在血条变化**之前**,固定偏移取样跨过去了
三条路线指向同一个前提:**我们从来没存过连续帧**。

这正是自己写进 evidence-quality-lessons.md 的教训("采集不可重来"),而实现
里却留着这个缺陷 —— 原始层不够原始。全程录像才是真正完整的原始层:探针
退化成"只提供精确时间轴",所有像素分析都能事后无限重跑、换方法重跑。

═══ 技术选择 ═══
ddagrab(Desktop Duplication API)→ NVENC 硬件编码:桌面纹理直接在 GPU 上
交给编码器,不走 CPU 内存,对游戏帧率影响可忽略。gdigrab 是 CPU 路径,会抢
资源,只在 ddagrab 不可用时兜底。

**时间戳对齐是这套东西的命根子**:录制开始的墙钟时间写进 meta.json,
事件时刻减去它就是视频内偏移。ffmpeg 启动有几百毫秒的初始化,所以记录的是
**首帧真正落盘的时刻**(通过 -progress 管道拿 out_time),不是进程启动时刻。

用法:
  python screen_record.py start [--fps 60] [--height 1080] [--dir DIR]
  python screen_record.py stop
  python screen_record.py status
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

FFMPEG = (r"C:\Users\bwica\AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-9.0-full_build\bin\ffmpeg.exe")
REC_ROOT = Path(r"C:\claude\state\game-ledger\recordings")
STATE = REC_ROOT / "current.json"
NO_WINDOW = 0x08000000
RECORD_CONTRACT = "screen-recording/1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def build_cmd(out: Path, fps: int, height: int, quality: int,
              progress: Path) -> list[str]:
    """ddagrab 硬件路径:桌面纹理留在 GPU,直接喂 NVENC。"""
    vf = f"ddagrab=output_idx=0:framerate={fps}"
    if height:
        # ⚠ ddagrab 输出 d3d11,scale_cuda 需要 cuda,二者无法直连;
        # hwmap=derive_device=cuda 在这台机器上也失败(-40)。所以缩放只能
        # hwdownload 回内存走 CPU —— 实测 1080p60 直接跟不上(AcquireNextFrame
        # 超时)。默认不缩放走纯 GPU,录制期不跟游戏抢 CPU;缩放留给事后处理。
        vf += f",hwdownload,format=bgra,scale=-2:{height},format=nv12"
    return [
        FFMPEG, "-hide_banner", "-loglevel", "warning",
        "-init_hw_device", "d3d11va",
        "-filter_complex", vf,
        "-c:v", "hevc_nvenc", "-preset", "p4", "-tune", "hq",
        "-rc", "constqp", "-qp", str(quality),
        # 关 B 帧:压缩域 motion vector 的 source 偏移会有正有负,后期麻烦;
        # 且短 GOP 让"跳到任意时刻抽帧"更快更准。
        "-bf", "0", "-g", str(fps * 2),
        # 录一小时的素材,绝不能因为退出方式不对就整份报废:fragmented MP4
        # 把索引随片段滚动写盘,强杀/断电后已落盘部分仍可读可 seek。
        # 普通 MP4 的 moov atom 写在结尾,非优雅退出 = 文件全废(实测踩过)。
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-progress", str(progress), "-nostats",
        "-y", str(out),
    ]


def start(fps: int, height: int, quality: int, tag: str) -> int:
    if STATE.exists():
        st = json.loads(STATE.read_text("utf-8"))
        if _alive(st.get("pid")):
            print(f"已在录制中(PID {st['pid']}):{st['file']}")
            return 1
        print("[warn] 发现残留状态但进程已退出,覆盖之")

    REC_ROOT.mkdir(parents=True, exist_ok=True)
    free_gb = __import__("shutil").disk_usage(REC_ROOT).free / 1e9
    if free_gb < 20:
        print(f"[error] 剩余空间仅 {free_gb:.1f} GB,拒绝开录(实测游戏画面"
              f"4K30 约 10-15 GB/小时)")
        return 3
    print(f"剩余空间 {free_gb:.0f} GB")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}{'-' + tag if tag else ''}"
    out = REC_ROOT / f"{name}.mp4"
    progress = REC_ROOT / f"{name}.progress"

    t_launch = time.time()
    proc = subprocess.Popen(
        build_cmd(out, fps, height, quality, progress),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=(REC_ROOT / f"{name}.err.log").open("wb"),
        creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)

    # 等首帧真正落盘,拿到精确的时间基准(ffmpeg 初始化有几百 ms)
    first_frame_at = None
    for _ in range(100):
        time.sleep(0.1)
        if proc.poll() is not None:
            err = (REC_ROOT / f"{name}.err.log").read_text("utf-8", errors="replace")
            print(f"[error] ffmpeg 立即退出:\n{err[-800:]}")
            return 2
        if progress.exists():
            txt = progress.read_text("utf-8", errors="replace")
            # 必须 frame>0:失败时 ffmpeg 也会写 frame=0/progress=end,
            # 只判"有 frame=" 会把失败当成功(实测踩过)
            frames_done = 0
            if "frame=" in txt:
                try:
                    frames_done = int(txt.rsplit("frame=", 1)[1].split()[0])
                except (ValueError, IndexError):
                    frames_done = 0
            if "progress=end" in txt and frames_done == 0:
                err = (REC_ROOT / f"{name}.err.log").read_text(
                    "utf-8", errors="replace")
                print("[error] ffmpeg 没产出任何帧就结束了:")
                print(err[-600:])
                return 2
            if frames_done > 0 and "out_time_us=" in txt:
                # 已产出首帧:用"现在 - 已录时长"反推录制起点
                try:
                    us = int(txt.rsplit("out_time_us=", 1)[1].split()[0])
                    first_frame_at = time.time() - us / 1e6
                except (ValueError, IndexError):
                    first_frame_at = time.time()
                break
    if first_frame_at is None:
        print("[warn] 10 秒内未确认首帧,用启动时刻当基准(对齐可能差几百 ms)")
        first_frame_at = t_launch

    meta = {
        "contract": RECORD_CONTRACT,
        "file": out.name,
        "pid": proc.pid,
        "startedAtEpoch": round(first_frame_at, 3),
        "startedAtIso": datetime.fromtimestamp(
            first_frame_at, timezone.utc).astimezone().isoformat(
                timespec="milliseconds"),
        "fps": fps, "height": height, "qp": quality,
        "note": "事件时刻(epoch) - startedAtEpoch = 视频内秒数",
    }
    STATE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    (REC_ROOT / f"{name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    print(f"开始录制 → {out}")
    print(f"  基准时刻 {meta['startedAtIso']}  {fps}fps @{height}p qp={quality}")
    return 0


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        # 不解码:中文 Windows 的 tasklist 输出是 GBK,按 utf-8 解会抛
        # UnicodeDecodeError,导致这里返回 None、停止逻辑失效、录制进程失控。
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, timeout=30,
            creationflags=NO_WINDOW).stdout
        return str(pid).encode() in out
    except (OSError, subprocess.SubprocessError):
        return False


def stop() -> int:
    if not STATE.exists():
        print("没有正在进行的录制")
        return 1
    st = json.loads(STATE.read_text("utf-8"))
    pid = st.get("pid")
    if not _alive(pid):
        print("录制进程已不在,清理状态")
        STATE.unlink(missing_ok=True)
        return 0
    # 优雅结束:ffmpeg 认自己 stdin 上的 'q'。CTRL_BREAK_EVENT 实测不生效
    # (进程不理会,10 秒后被强杀,moov 没写成 → 文件报废)。
    sent = False
    try:
        import ctypes
        # 跨进程写 stdin 需要拿到当初的管道;这里改用 ffmpeg 支持的另一条路:
        # 给它的控制台发 CTRL_C。若失败再退回 taskkill(有 fragmented 兜底)。
        ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, pid)
        sent = True
    except Exception:
        pass
    if not sent:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                       timeout=30, creationflags=NO_WINDOW)
    for _ in range(100):
        time.sleep(0.1)
        if not _alive(pid):
            break
    else:
        print("[warn] 未在 10 秒内退出,强杀(有 fragmented MP4 兜底,文件仍可读)")
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    f = REC_ROOT / st["file"]
    size = f.stat().st_size / 1e9 if f.exists() else 0
    print(f"停止录制:{f}  {size:.2f} GB")
    # 出声,不静默:录了一小时结果文件读不了,必须当场知道
    probe = subprocess.run(
        [FFMPEG.replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
        capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    if probe.returncode == 0 and probe.stdout.strip():
        print(f"  ✓ 文件可读,时长 {float(probe.stdout.strip()):.1f}s")
    else:
        print(f"  ✗ 文件校验失败!{(probe.stderr or '').strip()[:200]}")
    STATE.unlink(missing_ok=True)
    return 0


def status() -> int:
    if not STATE.exists():
        print("未在录制")
        return 0
    st = json.loads(STATE.read_text("utf-8"))
    f = REC_ROOT / st["file"]
    live = _alive(st.get("pid"))
    dur = time.time() - st["startedAtEpoch"]
    size = f.stat().st_size / 1e9 if f.exists() else 0
    print(f"{'录制中' if live else '进程已退出(状态残留)'}:{f.name}")
    print(f"  已录 {dur/60:.1f} 分钟 | {size:.2f} GB | "
          f"{size*1000/max(dur/60,0.01):.0f} MB/分钟")
    print(f"  基准 {st['startedAtIso']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("start", "stop", "status"))
    ap.add_argument("--fps", type=int, default=30,
                    help="分析用 30 足够(探针才 8Hz,光流需相邻帧而 33ms 够近)")
    ap.add_argument("--height", type=int, default=0,
                    help="0=原生分辨率走纯 GPU(推荐,不抢 CPU);"
                         "给具体值会 hwdownload 走 CPU 缩放,60fps 可能跟不上")
    ap.add_argument("--qp", type=int, default=28,
                    help="constqp 质量,越小越好越大;25 对分析足够")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.action == "start":
        return start(a.fps, a.height, a.qp, a.tag)
    if a.action == "stop":
        return stop()
    return status()


if __name__ == "__main__":
    sys.exit(main())
