"""敌人图鉴 — 把多次遭遇的画像聚合成一本从实战里长出来的怪物志。

单次裁定给的是"这一次是谁打的、怎么打的";跨次聚合才有累积价值:同一个敌人
遇到 5 次,它的外形描述会互相印证(共识=可信,分歧=看错了),攻击模式会补全
(每次可能只看到一招),危险度可以按真实承伤统计而不是拍脑袋。

这是三层存储里的第三层(物化视图):只读事件台账 + 裁定结果,不碰原始帧,
可以随时按新规则重算。

用法: python nightreign_bestiary.py <session_dir> [<session_dir> ...]
输出: state/game-ledger/nightreign/bestiary.json + bestiary.md
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"C:\claude\state\game-ledger\nightreign")
BESTIARY_CONTRACT = "nightreign-bestiary/1"

# 这些字段每次遭遇可能只看到一部分,聚合时全部保留,由人/AI 事后比对
NARRATIVE_FIELDS = (
    "appearance", "approach", "attackMotion", "telegraph",
    "effects", "counterplay", "sceneContext",
)


def clean_name(raw: str) -> str | None:
    """规整敌人名。看不清/推测的一律归到未识别,不进图鉴污染统计。"""
    if not raw:
        return None
    name = re.sub(r"[（(].*?[)）]", "", str(raw)).strip()
    if not name or len(name) > 24:
        return None
    if any(w in name for w in ("看不清", "不明", "未知", "无法", "没有", "unknown")):
        return None
    return name


def collect(sessions: list[Path]) -> dict:
    entries: dict[str, dict] = defaultdict(
        lambda: {"encounters": 0, "totalLossPct": 0.0, "worst": None,
                 "outcomes": defaultdict(int), "sightings": [],
                 **{f: [] for f in NARRATIVE_FIELDS}})
    unidentified = 0
    for sess in sessions:
        vf = sess / "refined" / "verdicts-local.json"
        if not vf.exists():
            print(f"[skip] {sess.name} 无 verdicts-local.json")
            continue
        data = json.loads(vf.read_text("utf-8"))
        full = 8800.0
        for item in data.get("items", []):
            name = clean_name(item.get("attacker", ""))
            if not name:
                unidentified += 1
                continue
            e = entries[name]
            e["encounters"] += 1
            pct = round(item.get("lossPx", 0) / full * 100, 1)
            e["totalLossPct"] += pct
            e["outcomes"][item.get("outcome", "unclear")] += 1
            e["sightings"].append({
                "session": sess.name, "id": item["id"], "ts": item.get("ts", "")[11:19],
                "lossPct": pct, "outcome": item.get("outcome"),
                "confidence": item.get("confidence"),
            })
            if e["worst"] is None or pct > e["worst"]["lossPct"]:
                e["worst"] = e["sightings"][-1]
            for f in NARRATIVE_FIELDS:
                v = item.get(f)
                if v and isinstance(v, str) and len(v) > 8:
                    e[f].append(v.strip())
    out = {}
    for name, e in entries.items():
        out[name] = {
            "encounters": e["encounters"],
            "avgLossPct": round(e["totalLossPct"] / e["encounters"], 1),
            "maxLossPct": e["worst"]["lossPct"] if e["worst"] else 0,
            "outcomes": dict(e["outcomes"]),
            "worst": e["worst"],
            "sightings": e["sightings"],
            **{f: e[f] for f in NARRATIVE_FIELDS},
        }
    return {"contract": BESTIARY_CONTRACT,
            "sessions": [s.name for s in sessions],
            "unidentified": unidentified,
            "enemies": dict(sorted(out.items(),
                                   key=lambda kv: -kv[1]["avgLossPct"]))}


def to_markdown(b: dict) -> str:
    lines = ["# 黑夜君临 · 实战怪物志", "",
             f"来自 {len(b['sessions'])} 场采集,"
             f"{sum(e['encounters'] for e in b['enemies'].values())} 次遭遇"
             f"(另有 {b['unidentified']} 次未能识别攻击者)。", "",
             "按平均承伤排序 —— 这是**你自己的**数据,不是攻略抄来的。", ""]
    for name, e in b["enemies"].items():
        oc = " / ".join(f"{k}×{v}" for k, v in e["outcomes"].items())
        lines += [f"## {name}", "",
                  f"- 遭遇 **{e['encounters']}** 次 · 平均承伤 **{e['avgLossPct']}%** · "
                  f"最惨一次 **{e['maxLossPct']}%**",
                  f"- 结局分布:{oc}"]
        if e["worst"]:
            w = e["worst"]
            lines.append(f"- 最惨:{w['session']} {w['ts']} ({w['id']})")
        for field, label in (("appearance", "外形"), ("approach", "接近方式"),
                             ("attackMotion", "攻击动作"), ("telegraph", "攻击预兆"),
                             ("effects", "特效"), ("counterplay", "应对")):
            vals = e.get(field) or []
            if not vals:
                continue
            lines += ["", f"**{label}**"]
            for v in vals[:3]:
                lines.append(f"> {v}")
            if len(vals) > 3:
                lines.append(f"> …另有 {len(vals)-3} 次观察")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    sessions = ([Path(a) for a in args] if args
                else sorted(p for p in ROOT.glob("2026*") if p.is_dir()))
    b = collect(sessions)
    (ROOT / "bestiary.json").write_text(
        json.dumps(b, ensure_ascii=False, indent=2), "utf-8")
    md = ROOT / "bestiary.md"
    md.write_text(to_markdown(b), "utf-8")
    print(f"图鉴 {len(b['enemies'])} 种敌人 → {md}")
    for name, e in b["enemies"].items():
        print(f"  {name:12s} 遭遇{e['encounters']:2d} 均伤{e['avgLossPct']:5.1f}% "
              f"最惨{e['maxLossPct']:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
