"""本地 Qwen 扫描事件片段 — 产出流程叙事 + 全貌帧导航。

═══ 分工(2026-08-17 用户定案)═══
  本地 Qwen  → ①这段的事情流程与走向(离散判断把握不了叙事)
               ②哪几秒能看清敌人全貌(导航)
  Codex      → 拿流程叙事 + 按导航裁出的高分辨率帧做最终分析
用户另指出:某一帧发生了什么反而不重要(掉血时刻探针已精确到 0.125s),
重要的是哪一帧有敌人全貌 —— 选帧标准因此是"可辨识度"而非"事件时刻"。

═══ 为什么不用原生视频输入 ═══
llama.cpp 的 --video 能跑通(mtmd 路径),但实测不实用:
  3.2 秒片段 → 约 5 分钟;6.7 秒片段 → 超过 11 分钟仍未出结果
  单帧 embedding 达 2040 tokens,超过默认 512 物理 batch,长片段直接
  "failed to prepare attention ubatches";加大 -ub 虽不崩但更慢
  且要独占 13GB 显存,不能与常驻 server 共存
而同样 8 帧走多图接口做完整流程叙事只要 5.5 分钟、简洁裁定只要 10-20 秒。
故送**密集帧序列 + 显式时间标注**,语义上仍是"看一段过程"。
clip.mp4 照常落盘,留给将来运行时改进或换用支持视频的云端模型。

用法(需先起 llama-server,见 nightreign_local_verdict.py 文件头):
  python nightreign_clip_scan.py <session_dir> [--limit N] [--min-loss 2000]
产物:
  <session>/refined/clip-scans.json      流程叙事 + 关键时刻
  <session>/refined/picked/<eventId>/    按导航裁出的高分辨率精选帧(给 Codex)
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "http://127.0.0.1:8099/v1/chat/completions"
SCAN_CONTRACT = "nightreign-clip-scan/2"

PROMPT = """这是《艾尔登法环:黑夜君临》一次战斗的连续画面({n} 帧,覆盖 {secs} 秒,
方括号里是相对首次受击的秒数)。

HUD 说明:左上三条(橙/蓝/绿)是玩家自己的 HP/FP/耐力;左侧带头像的列表是**队友**
(Boochi、n1993r 这类玩家 ID,是友方);屏幕**下方居中**的横条加中文名才是**敌人**。

请回答两件事:

【一、事情经过】用 4-8 句话讲清这段的**流程和走向**:玩家开始在做什么 → 敌人
怎么出现的 → 交战怎么发展 → 有没有转折(喝药/被击飞/队友驰援) → 最后是什么局面。
重点是**演变过程**,不是逐帧罗列。

【二、看清敌人的时刻】给出 1-3 个时间点,在那些时刻敌人**完整露出全貌**——
不贴脸不占满屏、不被爆炸火焰糊住、能看出体型和四肢结构。用方括号里的秒数。
格式必须严格如下,每行一个:
LOOK=<秒数> <一句话说明这时能看清什么>

全程都看不清敌人就写 LOOK=none 并说明原因。用中文。"""


def _b64(path: Path, side: int | None) -> str:
    from PIL import Image

    im = Image.open(path).convert("RGB")
    if side:
        im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86)
    return base64.b64encode(buf.getvalue()).decode()


def scan_frames(frames: list[tuple[float, Path]], seconds: float,
                side: int, timeout: float) -> dict:
    """送密集帧序列 + 时间标注,让模型讲流程并标出可辨识时刻。"""
    content: list[dict] = [{"type": "text", "text": PROMPT.format(
        n=len(frames), secs=round(seconds, 1))}]
    for off, path in frames:
        content.append({"type": "text", "text": f"[{off:+.1f}s]"})
        content.append({"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{_b64(path, side)}"}})
    body = json.dumps({
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3, "max_tokens": 1200,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    t0 = time.monotonic()
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read())
    body_text = out["choices"][0]["message"]["content"].strip()
    looks = []
    for m in re.finditer(r"LOOK=\s*([+-]?[\d.]+|none)\s*(.*)", body_text):
        if m.group(1) == "none":
            continue
        try:
            looks.append({"offset": float(m.group(1)),
                          "why": m.group(2).strip()})
        except ValueError:
            continue
    return {"narrative": body_text, "looks": looks,
            "scanSeconds": round(time.monotonic() - t0, 1),
            "promptTokens": out.get("usage", {}).get("prompt_tokens")}


def export_picked(frames: list[tuple[float, Path]], looks: list[dict],
                  out_dir: Path) -> list[dict]:
    """按导航把对应时刻的帧以**原分辨率**导出,供 Codex 精细分析。

    不缩放:多次实测(名字条、整屏)都证明缩放是可判读性的头号杀手。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    picked = []
    for i, look in enumerate(looks):
        nearest = min(frames, key=lambda f: abs(f[0] - look["offset"]))
        name = f"look{i:02d}_{nearest[0]:+.1f}s.jpg"
        (out_dir / name).write_bytes(nearest[1].read_bytes())
        picked.append({"file": name, "actualOffset": nearest[0], **look})
    return picked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-loss", type=int, default=2000)
    ap.add_argument("--side", type=int, default=1024,
                    help="送模型的边长;流程理解不需要读小字,1024 够且更快")
    ap.add_argument("--timeout", type=float, default=1200.0)
    a = ap.parse_args()

    sess = Path(a.session)
    work_path = sess / "refined" / "work.json"
    if not work_path.exists():
        import subprocess
        import sys as _s
        subprocess.run([_s.executable,
                        str(Path(__file__).with_name("nightreign_extract_evidence.py")),
                        str(sess), "--min-loss", str(a.min_loss)], check=True)
    work = json.loads(work_path.read_text("utf-8"))
    items = sorted(work["items"], key=lambda w: -w["lossPx"])
    if a.limit:
        items = items[: a.limit]
    print(f"扫描 {len(items)} 段(本地 Qwen,密集帧序列 @{a.side}px)")

    results = []
    for i, w in enumerate(items, 1):
        frames = [(f["offset"], Path(f["path"])) for f in w["frames"]]
        frames = [f for f in frames if f[1].exists()]
        if len(frames) < 3:
            print(f"[{i}/{len(items)}] {w['id']} 帧不足,跳过")
            continue
        span = frames[-1][0] - frames[0][0]
        try:
            r = scan_frames(frames, span, a.side, a.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[{i}/{len(items)}] {w['id']} 失败: {exc}")
            continue
        picked = export_picked(
            frames, r["looks"], sess / "refined" / "picked" / w["id"])
        results.append({"eventId": w["id"], "ts": w["ts"],
                        "lossPx": w["lossPx"], **r, "picked": picked})
        print(f"[{i}/{len(items)}] {w['id']} {r['scanSeconds']:5.1f}s | "
              f"导航 {len(r['looks'])} 个 | 精选 {len(picked)} 张")
        for lk in r["looks"]:
            print(f"     {lk['offset']:+.1f}s: {lk['why'][:58]}")

    out = sess / "refined" / "clip-scans.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {"contract": SCAN_CONTRACT, "items": results},
        ensure_ascii=False, indent=2), "utf-8")
    print(f"\n→ {out}")
    print(f"→ 精选帧 {sess / 'refined' / 'picked'}(供 Codex 分析)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
