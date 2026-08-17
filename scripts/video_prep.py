"""视频预处理 — 把任意视频压成本地视觉模型吃得下的尺寸。

═══ 关键事实(实测,反直觉)═══
llama.cpp 的 mtmd **按时长采样,约每秒取一帧**,与视频里实际有多少帧无关:
    8 帧内容编码成 6.7 秒  → 29 chunks
    32 帧内容编码成 3.2 秒 → 17 chunks   ← 帧数是前者 4 倍,chunk 反而更少
所以 **视频有几秒 ≈ 模型看几帧**。推论:
  · 抽帧和加速对它是**同一件事**(都是减少采样点),加速更干净(不丢原始帧)
  · 把 30 秒事件压到 1.5 秒 = 模型只看 1-2 帧,抽出来的帧全浪费

═══ 三个正交旋钮(实测同一段内容)═══
    720p 原样 6.7s                          → 超过 11 分钟(不可用)
    360p     6.7s                          → 94 秒
    **720p + 加速到 3s + --image-max-tokens 512 → 69 秒**  ← 最优
最后这组保留了 720p 源(细节还在),靠"压时长"减少采样帧数、靠"限 token"
让模型自己降采样,流程叙事质量完好。分辨率不必牺牲,该调的是另外两个。

对照实验(同为 720p 加速到 3s,只改 token 上限)分离了各旋钮的贡献:
    压时长      11 分钟 → 193 秒
    再限 token  193 秒  → 69 秒(快 2.8 倍)
而且**限 token 的质量没有变差,反而更稳**:不限 token 那次编出了"玩家骑乘
到怪物背上并制服它"(游戏根本没有骑乘机制,真实是玩家被打到 13% 血);限了
之后描述回到"闪避反击、怪血量递减"。更多视觉 token ≠ 更好理解。

⚠ 但真正的风险不是速度是**幻觉**:同一段视频两次自由叙事天差地别。解法与
多图路径一致 —— **给确定性事实作锚**(把探针测的血量曲线写进 prompt,并明说
不要自己读血条)。实测加锚后叙事立刻贴合事实(火焰强攻→喝药→连挨两下→归零),
耗时仅 86 秒。**视频叙事必须带锚点用,不能裸放。**

这个脚本只做确定性处理,不调模型,所以任何视频都能用 —— 游戏录像、教学
录屏、会议录制都一样。

用法:
  python video_prep.py IN.mp4 OUT.mp4 [--target-seconds 4] [--height 720]
  python video_prep.py IN.mp4 --probe          # 只看信息不处理
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

FFMPEG = (r"C:\Users\bwica\AppData\Local\Microsoft\WinGet\Packages"
          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
          r"\ffmpeg-9.0-full_build\bin\ffmpeg.exe")
FFPROBE = FFMPEG.replace("ffmpeg.exe", "ffprobe.exe")


def which(path: str, fallback: str) -> str:
    return path if Path(path).exists() else (shutil.which(fallback) or fallback)


def probe(src: Path) -> dict:
    proc = subprocess.run(
        [which(FFPROBE, "ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
         "-of", "json", str(src)],
        capture_output=True, text=True, timeout=60, creationflags=0x08000000)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {proc.stderr.strip()[:200]}")
    st = json.loads(proc.stdout)["streams"][0]
    num, _, den = st.get("r_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    dur = float(st.get("duration") or 0)
    return {"width": int(st.get("width", 0)), "height": int(st.get("height", 0)),
            "fps": round(fps, 2), "seconds": round(dur, 2),
            "frames": int(st.get("nb_frames") or round(fps * dur))}


def plan(info: dict, target_seconds: float, height: int,
         keep_fps: float) -> dict:
    """算出抽帧与缩放参数。时长超标就抽帧压缩,内容顺序不变。"""
    src_sec = info["seconds"] or 0.1
    # 先按"保留多少 fps 的信息密度"定抽帧步长
    step = max(1, round((info["fps"] or keep_fps) / keep_fps))
    kept = max(1, info["frames"] // step)
    # 抽完若仍超目标时长,提高播放速率把时长压到目标(采样量随时长走)
    out_fps = max(1.0, kept / target_seconds) if kept / keep_fps > target_seconds \
        else keep_fps
    return {"step": step, "keptFrames": kept, "outFps": round(out_fps, 2),
            "outSeconds": round(kept / out_fps, 2), "height": height,
            "srcSeconds": src_sec}


def convert(src: Path, dst: Path, p: dict) -> Path:
    vf = f"select='not(mod(n\\,{p['step']}))',scale=-2:{p['height']}"
    proc = subprocess.run(
        [which(FFMPEG, "ffmpeg"), "-y", "-i", str(src), "-vf", vf,
         "-vsync", "0", "-r", str(p["outFps"]),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "27",
         "-an", str(dst)],
        capture_output=True, text=True, timeout=1800, creationflags=0x08000000)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"转码失败: {proc.stderr.strip()[-300:]}")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--target-seconds", type=float, default=4.0,
                    help="输出时长上限;mtmd 按时长采样(约1帧/秒),这直接决定"
                         "模型看到几帧。实测 3-4 秒配 --image-max-tokens 512 最划算")
    ap.add_argument("--height", type=int, default=720,
                    help="输出高度;720p 配合压时长+限token 也只要 69 秒,"
                         "不必牺牲分辨率")
    ap.add_argument("--keep-fps", type=float, default=8.0,
                    help="抽帧后保留的密度;因为最终按 target-seconds 压时长,"
                         "这里留宽松些即可")
    a = ap.parse_args()

    src = Path(a.src)
    info = probe(src)
    print(f"源: {info['width']}x{info['height']} {info['fps']}fps "
          f"{info['seconds']}s ≈{info['frames']}帧")
    if a.probe or not a.dst:
        p = plan(info, a.target_seconds, a.height, a.keep_fps)
        print(f"计划: 每 {p['step']} 帧取 1 → {p['keptFrames']} 帧,"
              f" 输出 {p['outFps']}fps/{p['outSeconds']}s @{p['height']}p")
        print(f"采样量相对原片 ≈ {p['outSeconds']/max(info['seconds'],0.01):.1%}")
        return 0

    p = plan(info, a.target_seconds, a.height, a.keep_fps)
    dst = convert(src, Path(a.dst), p)
    out = probe(dst)
    print(f"输出: {out['width']}x{out['height']} {out['fps']}fps "
          f"{out['seconds']}s ≈{out['frames']}帧 "
          f"({dst.stat().st_size//1024}KB)")
    print(f"时长压缩 {info['seconds']:.1f}s → {out['seconds']:.1f}s "
          f"({out['seconds']/max(info['seconds'],0.01):.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
