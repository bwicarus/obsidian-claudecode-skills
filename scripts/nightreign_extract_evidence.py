"""从 session 的证据序列构造裁定工作包(给 AI 看的输入)。

第二版(2026-08-17):探针改为按事件落盘多帧序列后,这里不再挑单帧,而是
把整段序列按相对时刻排好交给 AI —— 敌人在远处的帧负责"这是什么东西",
贴脸帧负责"打中了什么部位",事后帧负责"结果如何"。

用法: python nightreign_extract_evidence.py <session_dir> [--min-loss 150]
输出: <session>/refined/work.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

WORK_CONTRACT = "nightreign-verdict-work/2"


def build(session_dir: Path, min_loss: int) -> Path:
    events = [
        json.loads(line)
        for line in (session_dir / "ledger.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    starts = {e["eventId"]: e for e in events if e["kind"] == "episode-start"}
    ends = [e for e in events if e["kind"] == "episode-end"]

    # episode-start 与其后的第一条 episode-end 配对(台账顺序即时间顺序)
    order = [e for e in events if e["kind"] in ("episode-start", "episode-end")]
    pairs = []
    pending = None
    for e in order:
        if e["kind"] == "episode-start":
            pending = e
        elif pending is not None:
            pairs.append((pending, e))
            pending = None

    work = []
    for start, end in pairs:
        loss = end.get("lossPx", end["pxBefore"] - end.get("pxMin", end["pxAfter"]))
        if loss < min_loss:
            continue
        ev_dir = session_dir / "evidence" / start["eventId"]
        manifest_path = ev_dir / "manifest.json"
        if not manifest_path.exists():
            print(f"[warn] {start['eventId']} 无证据序列,跳过(段仍在台账里)")
            continue
        manifest = json.loads(manifest_path.read_text("utf-8"))
        frames = sorted(manifest["frames"], key=lambda f: f["offset"])
        work.append({
            "id": start["eventId"],
            "ts": start["ts"],
            "endTs": end["ts"],
            "lossPx": loss,
            "pxBefore": end["pxBefore"],
            "drops": end.get("drops"),
            "durationMs": end.get("durationMs"),
            "endedBy": end.get("endedBy"),
            "fpAtStart": start.get("fp"),
            "staminaAtStart": start.get("stamina"),
            "frames": [
                {"offset": f["offset"], "sharpness": f["sharpness"],
                 "path": str(ev_dir / f["file"])}
                for f in frames
            ],
        })

    out = session_dir / "refined"
    out.mkdir(exist_ok=True)
    path = out / "work.json"
    path.write_text(
        json.dumps({"contract": WORK_CONTRACT, "session": session_dir.name,
                    "minLossPx": min_loss, "items": work},
                   ensure_ascii=False, indent=2), "utf-8")
    print(f"工作包 {len(work)} 条 → {path}")
    for w in work:
        offs = ",".join(f"{f['offset']:+.1f}" for f in w["frames"])
        print(f"  {w['id']} {w['ts'][11:19]} 掉{w['lossPx']}px/{w['drops']}次 "
              f"endedBy={w['endedBy']} 帧[{offs}]")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session")
    ap.add_argument("--min-loss", type=int, default=150)
    a = ap.parse_args()
    build(Path(a.session), a.min_loss)
