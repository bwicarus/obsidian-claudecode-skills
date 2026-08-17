"""本地 Qwen 视频扫描 — 读事件片段,产出流程叙事 + 导航标记。

═══ 分工(2026-08-17 用户定案)═══
  本地 Qwen(看**视频流**)  → 这段发生了什么、怎么演变的;哪几秒能看清敌人
  Codex(看**精选图像**)    → 拿流程叙事 + 按指引裁出的高质量帧做最终分析

为什么这么分:离散截图把握不了"流程和走向",那是连续画面才有的信息;而
精细辨认(读小字、认武器、比对细节)多图更强,且实际消费者是 Codex。

技术约束:llama-mtmd-cli 的视频输入要**独占显存**(13GB),不能和常驻 server
共存,所以按批处理跑 —— 打完一场后集中扫,不跟游戏抢卡。

用法:
  python nightreign_clip_scan.py <session_dir> [--limit N] [--min-loss 2000]
产物:
  <session>/refined/clip-scans.json      流程叙事 + 关键时刻
  <session>/refined/picked/<eventId>/    按导航裁出的高分辨率精选帧(给 Codex)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

LLAMA_DIR = Path(r"C:\Users\bwica\Desktop\llama")
MODEL = LLAMA_DIR / "models/qwen3.8-q3/Qwen3.8-27B-UD-Q3_K_XL.gguf"
MMPROJ = LLAMA_DIR / "models/qwen3.8-q3/mmproj-F16.gguf"
CLI = LLAMA_DIR / "llama-mtmd-cli.exe"
SCAN_CONTRACT = "nightreign-clip-scan/1"

PROMPT = """这是《艾尔登法环:黑夜君临》一段战斗录像({secs} 秒)。

HUD 说明:左上三条(橙/蓝/绿)是玩家自己的 HP/FP/耐力;左侧带头像的列表是**队友**
(Boochi、n1993r 这类玩家 ID,是友方);屏幕**下方居中**的横条加中文名才是**敌人**。

请回答两件事:

【一、事情经过】用 4-8 句话讲清这段的**流程和走向**:玩家开始在做什么 → 敌人
怎么出现的 → 交战怎么发展 → 有没有转折(喝药/被击飞/队友驰援) → 最后是什么局面。
重点是**演变过程**,不是逐帧罗列。

【二、看清敌人的时刻】给出 1-3 个时间点(第几秒),在那些时刻敌人**完整露出全貌**
——不贴脸、不被爆炸火焰糊住、能看出体型和四肢。格式必须严格如下,每行一个:
LOOK=<秒数> <一句话说明这时能看清什么>

如果全程都看不清敌人,写 LOOK=none 并说明原因。用中文。"""


def scan_clip(clip: Path, seconds: float, timeout: float) -> dict:
    cmd = [str(CLI), "-m", str(MODEL), "--mmproj", str(MMPROJ), "-ngl", "99",
           "-c", "16384", "--video", str(clip), "-p",
           PROMPT.format(secs=round(seconds, 1)),
           "--temp", "0.3", "-n", "900"]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          cwd=str(LLAMA_DIR), timeout=timeout)
    text = proc.stdout or ""
    # cli 会把加载日志也打到 stdout;正文在最后一段(去掉 think 块与日志行)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    lines = [ln for ln in text.splitlines()
             if not re.match(r"^\d+\.\d+\.\d+\.\d+\s+[IWE]\s", ln)
             and not ln.startswith(("load_", "llama_", "clip_", "main:", "build:"))]
    body = "\n".join(lines).strip()
    looks = []
    for m in re.finditer(r"LOOK=\s*([\d.]+|none)\s*(.*)", body):
        if m.group(1) == "none":
            continue
        looks.append({"second": float(m.group(1)), "why": m.group(2).strip()})
    return {"narrative": body, "looks": looks,
            "scanSeconds": round(time.monotonic() - t0, 1),
            "returncode": proc.returncode}


def extract_frames(clip: Path, looks: list[dict], out_dir: Path,
                   ffmpeg: str) -> list[dict]:
    """按 Qwen 的导航,从片段里裁出高分辨率精选帧,供 Codex 精细分析。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    picked = []
    for i, look in enumerate(looks):
        name = f"look{i:02d}_{look['second']:.1f}s.jpg"
        proc = subprocess.run(
            [ffmpeg, "-y", "-ss", str(look["second"]), "-i", str(clip),
             "-frames:v", "1", "-q:v", "2", str(out_dir / name)],
            capture_output=True, text=True, timeout=90,
            creationflags=0x08000000)
        if proc.returncode == 0 and (out_dir / name).exists():
            picked.append({"file": name, **look})
        else:
            print(f"  [warn] 抽帧 {look['second']}s 失败")
    return picked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-loss", type=int, default=2000)
    ap.add_argument("--timeout", type=float, default=1800.0)
    a = ap.parse_args()

    sess = Path(a.session)
    cfg = json.loads(
        (sess / "session.json").read_text("utf-8")).get("config", {})
    ffmpeg = cfg.get("ffmpeg", "ffmpeg")

    clips = []
    for man in sorted((sess / "evidence").glob("*/manifest.json")):
        m = json.loads(man.read_text("utf-8"))
        if m.get("clip"):
            clips.append((man.parent, m))
    if not clips:
        print("没有 clip.mp4 —— 该 session 采于片段功能上线前,"
              "或 ffmpeg 不可用。用新采的场次重试。")
        return 1
    if a.limit:
        clips = clips[: a.limit]
    print(f"扫描 {len(clips)} 段片段(本地 Qwen 视频输入,独占显存)")

    results = []
    for i, (d, man) in enumerate(clips, 1):
        clip = d / man["clip"]["file"]
        print(f"[{i}/{len(clips)}] {d.name} {man['clip']['seconds']}s ...")
        try:
            r = scan_clip(clip, man["clip"]["seconds"], a.timeout)
        except subprocess.SubprocessError as exc:
            print(f"  失败: {exc}")
            continue
        picked = extract_frames(
            clip, r["looks"], sess / "refined" / "picked" / d.name, ffmpeg)
        results.append({"eventId": d.name, **r, "picked": picked})
        print(f"  {r['scanSeconds']}s | 关键时刻 {len(r['looks'])} 个 | "
              f"精选帧 {len(picked)} 张")
        for lk in r["looks"]:
            print(f"     {lk['second']:.1f}s: {lk['why'][:60]}")

    out = sess / "refined" / "clip-scans.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {"contract": SCAN_CONTRACT, "model": MODEL.name, "items": results},
        ensure_ascii=False, indent=2), "utf-8")
    print(f"\n→ {out}")
    print(f"→ 精选帧目录 {sess / 'refined' / 'picked'}(供 Codex 分析)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
