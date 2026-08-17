"""把事件证据整理成 Codex 可直接消费的文件夹 — 不经任何本地模型。

═══ 分工(2026-08-17 用户定案)═══
  多帧图片 → **直接给 Codex**(丢个文件夹即可,精细辨认它更强,且它才是
             实际消费者:用户会问它"刚才怎么死的")
  视频流   → 本地 Qwen 专职,负责流程与走向(见 nightreign_clip_scan.py);
             这个能力一旦跑通,任何视频内容都能复用,不限于游戏

所以本脚本不调用任何模型,只做确定性整理:把该看的东西按事件摆好,配一份
人和 AI 都能读的说明,剩下的交给 Codex。

产物结构:
  <session>/for-codex/
    README.md                  怎么读这批材料
    <eventId>/
      frames/                  证据帧(原分辨率,不缩放)
      nameplate/               敌人名字条裁图(原尺寸,读名字用)
      clip.mp4                 该窗口的连续片段(给视频模型)
      facts.json               探针测到的确定性事实(血量曲线/掉血时刻)

用法: python nightreign_export_for_codex.py <session_dir> [--min-loss 2000]
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

EXPORT_CONTRACT = "nightreign-codex-export/1"

README = """# 事件证据包 — 给 Codex 的说明

每个 `e####/` 是一次玩家受伤事件。看之前先读 `facts.json`。

## facts.json 是**确定性事实**,不要质疑它

血量数字来自像素测量(8Hz 采样),不是模型看图猜的。已经反复验证过:
视觉模型读进度条是公认失败模式 —— 曾把三次掉血说成两次、喝药时刻差了
4.6 秒。所以:

- **掉血时刻、幅度、次数以 facts.json 为准**
- 你的任务是解释 **为什么**:谁打的、用什么动作、玩家犯了什么错
- 不要复述血量数字,也不要自己从画面读血条

## frames/ 是证据帧

文件名里的 `+0.4s` 是相对首次受击的秒数,负数表示事发前。取样覆盖
`-8s` 到 `+2s`,并额外在每个真实掉血时刻各取一帧(manifest 里 `atChange`)。

**帧是原分辨率,不要缩小再看**。多次实测缩放是可判读性的头号杀手:
同一模型同一帧,整屏缩到 896px 会把"负伤恶魔"读成"负伤恶螳"。

## nameplate/ 是敌人名字条

游戏把精英/Boss 的名字写在屏幕下方居中。这里裁的是**未降采样**的原图区域,
**敌人名字以这里为准**。

⚠ 别把队友当敌人:左侧带头像的列表是队友(Boochi、n1993r 这类玩家 ID),
屏幕下方居中带中文名的横条才是敌人。曾有一次模型把队友 ID 当成了凶手。

## clip.mp4 是连续片段

给支持视频输入的模型看流程与走向用。离散帧回答"这一刻什么样",视频回答
"这段怎么演变的"。
"""


def build(session: Path, min_loss: int) -> Path:
    work_path = session / "refined" / "work.json"
    if not work_path.exists():
        import subprocess
        import sys

        subprocess.run(
            [sys.executable,
             str(Path(__file__).with_name("nightreign_extract_evidence.py")),
             str(session), "--min-loss", str(min_loss)], check=True)
    work = json.loads(work_path.read_text("utf-8"))

    # 曲线:给每个事件配上它窗口内的真实血量变化(确定性事实)
    def sec(t: str) -> float:
        return int(t[11:13]) * 3600 + int(t[14:16]) * 60 + float(t[17:23])

    samples = []
    csv = session / "samples.csv"
    if csv.exists():
        header = csv.read_text("utf-8").splitlines()[0].split(",")
        hp_i = header.index("hp") if "hp" in header else 1
        for line in csv.read_text("utf-8").splitlines()[1:]:
            parts = line.split(",")
            if len(parts) <= hp_i:
                continue
            try:
                samples.append((sec(parts[0]), int(parts[hp_i])))
            except (ValueError, IndexError):
                continue
    full = max((v for _, v in samples), default=1) or 1

    out_root = session / "for-codex"
    out_root.mkdir(exist_ok=True)
    (out_root / "README.md").write_text(README, "utf-8")

    exported = 0
    for w in sorted(work["items"], key=lambda x: -x["lossPx"]):
        if w["lossPx"] < min_loss:
            continue
        ev_dir = session / "evidence" / w["id"]
        dst = out_root / w["id"]
        (dst / "frames").mkdir(parents=True, exist_ok=True)
        (dst / "nameplate").mkdir(exist_ok=True)

        for f in w["frames"]:
            src = Path(f["path"])
            if src.exists():
                shutil.copy2(src, dst / "frames" / src.name)
            plate = src.with_name(src.stem + "_plate.jpg")
            if plate.exists():
                shutil.copy2(plate, dst / "nameplate" / plate.name)
        clip = ev_dir / "clip.mp4"
        if clip.exists():
            shutil.copy2(clip, dst / "clip.mp4")

        a0 = sec(w["ts"])
        window = [(round(t - a0, 2), v) for t, v in samples
                  if -9.5 <= t - a0 <= 4.0]
        changes = []
        for (t0, v0), (t1, v1) in zip(window, window[1:]):
            delta = (v1 - v0) / full * 100
            if abs(delta) >= 8:
                changes.append({"offset": t1, "deltaPct": round(delta, 1),
                                "remainPct": round(v1 / full * 100, 1)})
        (dst / "facts.json").write_text(json.dumps({
            "contract": EXPORT_CONTRACT,
            "eventId": w["id"], "startedAt": w["ts"], "endedAt": w["endTs"],
            "durationMs": w["durationMs"], "hits": w["drops"],
            "endedBy": w["endedBy"],
            "lossPct": round(w["lossPx"] / full * 100, 1),
            "note": "以下变化由像素测量得出,不是从画面读的;请解释成因,不要复述数字",
            "changes": changes,
            "curve": [{"offset": t, "hpPct": round(v / full * 100, 1)}
                      for t, v in window],
        }, ensure_ascii=False, indent=2), "utf-8")
        exported += 1

    print(f"导出 {exported} 个事件 → {out_root}")
    print("直接把这个目录交给 Codex;先读 README.md 再看 facts.json")
    return out_root


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session")
    ap.add_argument("--min-loss", type=int, default=2000)
    a = ap.parse_args()
    build(Path(a.session), a.min_loss)
