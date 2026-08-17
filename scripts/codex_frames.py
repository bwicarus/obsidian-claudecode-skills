"""把抽好的帧交给 Codex CLI 做分析 — 分工链的最后一环。

═══ 为什么走 Codex ═══
2026-08-17 读源码确认:所谓"视频输入"在 Qwen3-VL 全家(transformers/vLLM/
SGLang/llama.cpp)本质都是 **N 帧独立编码 + 文本时间戳** —— 官方自己就废除了
temporal position id(`t_index is always 0`,注释原文"我们用 timestamps 编码
时序")。所以"送视频"并不比"送帧+时间戳"多任何东西,区别只在时间戳的格式。

既然如此,抽帧我们自己做(可按信号挑时刻、按清晰度挑帧、能去重,避开
llama.cpp 硬编码 4fps 会复制帧的坑),分析交给更强的 Codex —— 它本来就是
这套系统的实际消费者。

═══ 送什么 ═══
  帧      按 frame_pick.py 选出的关键帧,**原分辨率**(缩放是可判读性头号杀手)
  时间戳  每帧标注秒数,模仿官方 `<3.0 seconds>` 的对齐格式
  事实    探针测的血量曲线 —— 视觉模型读进度条是公认失败模式,必须给锚

用法:
  python codex_frames.py FRAME_DIR [--facts facts.json] [--model gpt-5.5]
  python codex_frames.py --session SESSION --event e0155
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

CODEX = os.environ.get("APP_CODEX", r"C:\Users\bwica\AppData\Roaming\npm\codex.cmd")
# skill 模板里的 C:\gpt 已不存在(实测 os error 2);用项目根即可
WORKDIR = r"C:\claude"
TEMP_DIR = Path(r"C:\claude\temp")
NO_WINDOW = 0x08000000


def codex_base() -> list[str]:
    return (["cmd.exe", "/d", "/c", CODEX]
            if CODEX.lower().endswith((".cmd", ".bat")) else [CODEX])


def build_prompt(frames: list[dict], facts: dict | None) -> str:
    lines = [
        "这是《艾尔登法环:黑夜君临》一次战斗的连续截图,按时间顺序排列。",
        "",
        "帧的时间标注(相对首次受击的秒数):",
    ]
    for i, f in enumerate(frames):
        lines.append(f"  第{i+1}张 <{f['second']:+.1f} seconds>")
    if facts and facts.get("changes"):
        lines += ["", "⚠ 血量数据由**像素测量**精确给出,不要自己读血条,直接采信:"]
        for c in facts["changes"]:
            verb = "掉血" if c["deltaPct"] < 0 else "回血"
            lines.append(f"  {c['offset']:+6.2f}s  {verb} {abs(c['deltaPct']):.1f}%"
                         f"  → 剩 {c['remainPct']:.0f}%")
    lines += [
        "",
        "⚠ HUD:左上三条(橙/蓝/绿)是玩家自己的 HP/FP/耐力;左侧带头像的列表是"
        "**队友**(Boochi、n1993r 这类玩家 ID,友方);屏幕**下方居中**的横条加"
        "中文名才是**敌人**。",
        "",
        "请回答:",
        "1. 敌人是谁(读下方居中横条上的中文名)",
        "2. 这段的流程与走向:玩家开始在做什么 → 敌人怎么出现 → 交战如何发展"
        " → 有无转折 → 最终局面",
        "3. 上面每一次血量变化分别是什么造成的(哪一击/什么动作/还是玩家自己喝药)",
        "4. 这个敌人的外形画像:体型、颜色、肢体、武器、攻击方式、可辨识的预兆",
        "5. 玩家犯了什么错,下次怎么应对",
        "",
        "看不清就明说看不清,不要编造。",
    ]
    return "\n".join(lines)


def call_codex(prompt: str, images: list[Path], model: str,
               timeout: float) -> tuple[str, float]:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt",
                                     dir=TEMP_DIR, delete=False) as f:
        out_path = f.name
    cmd = codex_base() + [
        "exec", "--sandbox", "read-only", "--skip-git-repo-check",
        "--color", "never", "--output-last-message", out_path, "-C", WORKDIR,
    ]
    if images:
        # --image 是可变数量参数(clap 的 <FILE>...),会贪婪吃掉后面的 prompt,
        # 表现为 "Reading prompt from stdin... No prompt provided"。必须用
        # -- 显式终止,或把多个路径用逗号并成一个值。
        cmd += ["--image", ",".join(str(i) for i in images)]
    if model:
        cmd += ["--model", model]
    cmd += ["--", prompt]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW, encoding="utf-8",
                          errors="replace")
    # 出声,不静默:捕获了 stderr 却不看返回码,就是自找的调试地狱
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise RuntimeError(f"codex 退出码 {proc.returncode}: {err}")
    text = (Path(out_path).read_text("utf-8", errors="replace").strip()
            if Path(out_path).exists() else "")
    if not text:
        tail = (proc.stdout or "").strip()[-600:]
        raise RuntimeError(f"codex 无输出文件。stdout 尾部: {tail}")
    Path(out_path).unlink(missing_ok=True)
    return text, time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("frame_dir", nargs="?")
    ap.add_argument("--session")
    ap.add_argument("--event")
    ap.add_argument("--facts")
    ap.add_argument("--model", default="")
    ap.add_argument("--timeout", type=float, default=900.0)
    a = ap.parse_args()

    if a.session:
        if not a.event:
            raise SystemExit("--session 需要配 --event")
        sess = Path(a.session)
        frame_dir = sess / "refined" / "picked-codex" / a.event
        if not frame_dir.exists():
            import sys
            subprocess.run(
                [sys.executable,
                 str(Path(__file__).with_name("frame_pick.py")),
                 "--from-session", str(sess), "--event", a.event,
                 str(frame_dir)], check=True)
        facts_path = sess / "for-codex" / a.event / "facts.json"
    else:
        frame_dir = Path(a.frame_dir)
        facts_path = Path(a.facts) if a.facts else frame_dir / "facts.json"

    picked_meta = frame_dir / "picked.json"
    if picked_meta.exists():
        frames = json.loads(picked_meta.read_text("utf-8"))["picked"]
    else:
        frames = [{"file": f.name, "second": 0.0}
                  for f in sorted(frame_dir.glob("*.jpg"))]
    images = [frame_dir / f["file"] for f in frames]
    images = [p for p in images if p.exists()]
    if not images:
        raise SystemExit(f"{frame_dir} 里没有帧")

    facts = None
    if facts_path.exists():
        facts = json.loads(facts_path.read_text("utf-8"))

    prompt = build_prompt(frames, facts)
    print(f"送 Codex:{len(images)} 帧"
          + (f" + {len(facts['changes'])} 条确定性事实" if facts and facts.get("changes") else "")
          + (f" | 模型 {a.model}" if a.model else " | 默认模型"))
    text, secs = call_codex(prompt, images, a.model, a.timeout)
    print(f"耗时 {secs:.1f}s\n" + "=" * 64)
    print(text or "(空回复 —— 检查 codex 是否需要先登录)")
    out = frame_dir / "codex-verdict.md"
    out.write_text(text, "utf-8")
    print("=" * 64 + f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
