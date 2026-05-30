#!/usr/bin/env python3
"""把引体架解锁的 3 个动作加进 fitness-plan.json:
- chin_up         → Pull 日开头(最大复合,体力充沛时做)
- dip             → Push 日第 5(主推完后做)
- hanging_leg_raise → Legs 日(替代 plank)

幂等:已存在的 ID 跳过。自重动作 start_weight_kg=0 + weight_step_kg=0,
fitness.py 的 _compute_recommendation 对自重做了特殊处理(不加重,
只调 target_reps 直到 rep_high → 提示加负重)。
"""
from __future__ import annotations
import json
from pathlib import Path

PLAN = Path("/home/bwicarus/claude/_server_deploy/static/fitness-plan.json")
WEBAPP = Path("/home/bwicarus/webapp/static/fitness-plan.json")

NEW: dict[str, list[dict]] = {
    "pull": [
        {
            "_insert": "prepend",
            "id": "chin_up",
            "name": "反握引体 (chin-up)",
            "prescribed": {
                "sets": 4, "rep_range": [5, 10], "rir_target": 1,
                "rest_seconds": 180, "start_weight_kg": 0, "weight_step_kg": 1.25,
            },
            "evidence_note": "拉类王者复合,反握二头激活 +20% vs 正握 (Youdas 2010);EMG 阔背与 lat pulldown 等效但闭链全身参与 (Doma 2013)。完整做不到时:negatives 慢下降(5 秒)或脚踩凳助力",
            "search_q": "chin up pull up form",
            "sets": "4 × 5-10",
            "start_weight_hint": "自重 (能 10+ reps 后挂负重)",
            "tips": [
                "正手宽距 = 阔背主;反手肩宽 = 二头 + 阔背平衡(我们用反手)",
                "肩胛骨先下沉,胸顶向横杆,不耸肩",
                "上拉到下巴过杆,下放到肘部完全伸直(全 ROM)",
                "完整做不到:先做 negatives(从顶位慢下 5s)或脚踩凳助力",
            ],
        }
    ],
    "push": [
        {
            "_insert": "after",
            "_after_id": "cable_tricep_pushdown",
            "id": "dip",
            "name": "双杠臂屈伸 (dip,需双杠)",
            "prescribed": {
                "sets": 3, "rep_range": [6, 10], "rir_target": 1,
                "rest_seconds": 150, "start_weight_kg": 0, "weight_step_kg": 1.25,
            },
            "evidence_note": "复合三头 + 下胸 + 前三角;前倾躯干练胸,垂直躯干练三头 (Bergstrom 2012)。架子没双杠则跳过,改加一组哑铃过头三头",
            "search_q": "dips chest triceps",
            "sets": "3 × 6-10",
            "start_weight_hint": "自重 (能 10+ reps 后挂负重)",
            "tips": [
                "想练胸:躯干前倾 ~30°,肘略外展",
                "想练三头:躯干直立,肘紧贴体侧",
                "下放到上臂与地面平行,不要更深(肩前囊压力大)",
                "完整做不到:先做 bench dip(脚撑地)或 negatives",
            ],
        }
    ],
    "legs": [
        {
            "_insert": "replace",
            "_replace_id": "plank",
            "id": "hanging_leg_raise",
            "name": "悬垂举腿",
            "prescribed": {
                "sets": 3, "rep_range": [8, 15], "rir_target": 1,
                "rest_seconds": 90, "start_weight_kg": 0, "weight_step_kg": 0,
            },
            "evidence_note": "腹直肌下部激活 +35% vs plank,核心动态收缩更全 (Escamilla 2010);难度可调:屈膝 → 直腿 → 直腿到水平 → toes-to-bar",
            "search_q": "hanging leg raise abs",
            "sets": "3 × 8-15",
            "start_weight_hint": "自重 (从屈膝开始)",
            "tips": [
                "握杆 + 全身悬垂,肩胛骨主动下沉(不要松垮挂着)",
                "屈膝抬到腰位 → 进阶直腿到水平 → 大神 toes-to-bar",
                "用腹肌发力,不要荡秋千借力",
                "下放控制 2 秒离心,避免甩落",
            ],
        }
    ],
}


def main():
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    added = []
    replaced = []
    skipped = []

    for day_id, exs in NEW.items():
        day = next((d for d in plan["days"] if d["id"] == day_id), None)
        if not day:
            print(f"⚠ 没有 day {day_id}")
            continue
        existing = {e["id"] for e in day["exercises"]}
        for spec in exs:
            ex_id = spec["id"]
            mode = spec.pop("_insert", "append")
            if ex_id in existing:
                skipped.append(ex_id)
                continue
            new_ex = {k: v for k, v in spec.items() if not k.startswith("_")}
            if mode == "prepend":
                day["exercises"].insert(0, new_ex)
            elif mode == "after":
                after_id = spec.get("_after_id")
                idx = next((i for i, e in enumerate(day["exercises"]) if e["id"] == after_id), None)
                if idx is None:
                    day["exercises"].append(new_ex)
                else:
                    day["exercises"].insert(idx + 1, new_ex)
            elif mode == "replace":
                replace_id = spec.get("_replace_id")
                idx = next((i for i, e in enumerate(day["exercises"]) if e["id"] == replace_id), None)
                if idx is None:
                    day["exercises"].append(new_ex)
                else:
                    day["exercises"][idx] = new_ex
                    replaced.append(f"{replace_id} → {ex_id}")
                    continue
            else:
                day["exercises"].append(new_ex)
            added.append(ex_id)

    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if WEBAPP.parent.exists():
        WEBAPP.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"添加: {added}")
    print(f"替换: {replaced}")
    print(f"跳过(已存在): {skipped}")


if __name__ == "__main__":
    main()
