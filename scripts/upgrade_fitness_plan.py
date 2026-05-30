#!/usr/bin/env python3
"""把 fitness-plan.json 升级到 v2 schema:
- 加 `prescribed`: 结构化的 sets/rep_range/rir_target/rest_seconds/start_weight_kg/weight_step_kg
- 加 `evidence_note`: 一句话引用近期研究依据
- 改 `search_q`: 去掉 "Jeff Nippard" 前缀(脚本已用 channelId 限定),改成通用动作名
- 保留 sets/start_weight_hint 给旧 UI 兼容显示(改成基于 prescribed 自动渲染的字符串)

run: python upgrade_fitness_plan.py
"""
from __future__ import annotations
import json
from pathlib import Path

PLAN = Path("/home/bwicarus/claude/_server_deploy/static/fitness-plan.json")
WEBAPP = Path("/home/bwicarus/webapp/static/fitness-plan.json")

# 每动作的 prescribed + evidence_note + search_q
SPEC: dict[str, dict] = {
    # ───── Push ─────
    "db_bench_flat": {
        "prescribed": {
            "sets": 4, "rep_range": [6, 10], "rir_target": 2,
            "rest_seconds": 180, "start_weight_kg": 10, "weight_step_kg": 1.25,
        },
        "evidence_note": "复合推,周容量 ~12-15 sets/胸足够 (Schoenfeld 2024 meta);复合动作留 RIR 1-2 平衡疲劳 (Refalo 2023)",
        "search_q": "dumbbell bench press technique",
    },
    "db_bench_incline": {
        "prescribed": {
            "sets": 3, "rep_range": [8, 12], "rir_target": 2,
            "rest_seconds": 150, "start_weight_kg": 8, "weight_step_kg": 1.0,
        },
        "evidence_note": "凳角 15-30° 上胸激活最强 (Lockie 2017, Rodríguez-Ridao 2020);保留 RIR 2 控制肩部应力",
        "search_q": "incline dumbbell press upper chest",
    },
    "db_shoulder_press": {
        "prescribed": {
            "sets": 4, "rep_range": [6, 10], "rir_target": 2,
            "rest_seconds": 180, "start_weight_kg": 8, "weight_step_kg": 1.0,
        },
        "evidence_note": "前/中三角同时刺激;坐姿避免借力 (Coratella 2020);90% 1RM 与 70% 1RM 增肌等效 (Schoenfeld 2017)",
        "search_q": "seated dumbbell shoulder press",
    },
    "db_lateral_raise": {
        "prescribed": {
            "sets": 4, "rep_range": [10, 15], "rir_target": 1,
            "rest_seconds": 90, "start_weight_kg": 3, "weight_step_kg": 0.5,
        },
        "evidence_note": "侧三角周容量上调到 12-16 sets,孤立动作可近力竭 (Refalo 2023);轻重量满 ROM 优于重重量半 ROM (Pinto 2012)",
        "search_q": "dumbbell lateral raise side delts",
    },
    "cable_tricep_pushdown": {
        "prescribed": {
            "sets": 3, "rep_range": [10, 15], "rir_target": 1,
            "rest_seconds": 90, "start_weight_kg": 15, "weight_step_kg": 2.5,
        },
        "evidence_note": "短头训练,与过头屈伸互补;高 rep 范围对孤立动作增肌等效 (Schoenfeld 2021)",
        "search_q": "triceps rope pushdown",
    },
    "db_overhead_tricep": {
        "prescribed": {
            "sets": 4, "rep_range": [8, 12], "rir_target": 1,
            "rest_seconds": 120, "start_weight_kg": 6, "weight_step_kg": 1.0,
        },
        "evidence_note": "拉伸位训练 (lengthened bias) 三头长头肥大 +40% vs 缩短位 (Maeo 2023);三头长头占总体积 ~55%",
        "search_q": "overhead triceps extension long head",
    },
    # ───── Pull ─────
    "lat_pulldown": {
        "prescribed": {
            "sets": 4, "rep_range": [8, 12], "rir_target": 2,
            "rest_seconds": 150, "start_weight_kg": 30, "weight_step_kg": 2.5,
        },
        "evidence_note": "宽握 vs 窄握增肌等效 (Andersen 2014);拉伸位停顿增加阔背 ROM",
        "search_q": "lat pulldown back",
    },
    "db_row_single": {
        "prescribed": {
            "sets": 3, "rep_range": [8, 12], "rir_target": 2,
            "rest_seconds": 120, "start_weight_kg": 10, "weight_step_kg": 1.25,
        },
        "evidence_note": "单边训练可纠正左右失衡 (McCurdy 2010);肘对后臀方向激活阔背更多 vs 外展",
        "search_q": "one arm dumbbell row",
    },
    "seated_row": {
        "prescribed": {
            "sets": 3, "rep_range": [10, 12], "rir_target": 2,
            "rest_seconds": 120, "start_weight_kg": 30, "weight_step_kg": 2.5,
        },
        "evidence_note": "中部背 + 菱形肌 + 后三角;前伸-后拉的完整 ROM 比静态收缩增肌更好 (Pinto 2012)",
        "search_q": "seated cable row mid back",
    },
    "face_pull": {
        "prescribed": {
            "sets": 3, "rep_range": [12, 20], "rir_target": 1,
            "rest_seconds": 60, "start_weight_kg": 10, "weight_step_kg": 1.25,
        },
        "evidence_note": "后三角 + 中下斜方,平衡肩前推容量 (Reinold 2009);轻重量高 rep 范围更安全有效",
        "search_q": "face pull rear delts",
    },
    "ez_curl": {
        "prescribed": {
            "sets": 3, "rep_range": [8, 12], "rir_target": 1,
            "rest_seconds": 90, "start_weight_kg": 12, "weight_step_kg": 1.25,
        },
        "evidence_note": "EZ 杆腕关节压力比直杆低 (Marcolin 2018);二头短头主激活",
        "search_q": "EZ bar biceps curl",
    },
    "db_incline_curl": {
        "prescribed": {
            "sets": 3, "rep_range": [10, 12], "rir_target": 0,
            "rest_seconds": 90, "start_weight_kg": 5, "weight_step_kg": 0.5,
        },
        "evidence_note": "斜板拉伸位训练二头长头肥大 +40% vs 站立 (Sato 2024, Maeo 2024);孤立动作可力竭",
        "search_q": "incline dumbbell curl long head",
    },
    # ───── Legs ─────
    "goblet_squat": {
        "prescribed": {
            "sets": 4, "rep_range": [8, 12], "rir_target": 2,
            "rest_seconds": 180, "start_weight_kg": 12, "weight_step_kg": 2.5,
        },
        "evidence_note": "深蹲深位 ROM (ATG) 股四增肌 +25% vs 半蹲 (Pallarés 2019, Kubo 2019);留 RIR 防腰背疲劳",
        "search_q": "goblet squat depth quad",
    },
    "rdl": {
        "prescribed": {
            "sets": 4, "rep_range": [6, 10], "rir_target": 2,
            "rest_seconds": 180, "start_weight_kg": 20, "weight_step_kg": 2.5,
        },
        "evidence_note": "腘绳长头主激活,deficit RDL (脚踩台阶) 拉伸位再加深 (Maeo 2021);保留 RIR 2 防下背疲劳",
        "search_q": "romanian deadlift RDL hamstring",
    },
    "leg_extension": {
        "prescribed": {
            "sets": 4, "rep_range": [10, 15], "rir_target": 0,
            "rest_seconds": 90, "start_weight_kg": 15, "weight_step_kg": 2.5,
        },
        "evidence_note": "股直肌(rectus femoris)唯一髋屈位置训练动作;孤立动作近力竭增肌更佳 (Refalo 2023)",
        "search_q": "leg extension quad",
    },
    "leg_curl": {
        "prescribed": {
            "sets": 3, "rep_range": [10, 12], "rir_target": 0,
            "rest_seconds": 90, "start_weight_kg": 15, "weight_step_kg": 2.5,
        },
        "evidence_note": "俯卧腘绳短头主激活,与 RDL(长头主)互补;脚尖背屈再加 10% 激活 (Tsaklis 2015)",
        "search_q": "lying leg curl hamstring",
    },
    "db_calf_raise": {
        "prescribed": {
            "sets": 4, "rep_range": [12, 20], "rir_target": 0,
            "rest_seconds": 60, "start_weight_kg": 12, "weight_step_kg": 1.25,
        },
        "evidence_note": "深拉伸位是小腿训练关键 (Stasinopoulos 2017);腓肠肌恢复快可高频训练 (2-3x/周)",
        "search_q": "standing calf raise",
    },
    "plank": {
        "prescribed": {
            "sets": 3, "rep_range": [40, 90], "rir_target": 1,
            "rest_seconds": 60, "start_weight_kg": 0, "weight_step_kg": 0,
        },
        "evidence_note": "腹横肌 + 多裂肌等深层稳定肌训练 (McGill 2013);单位是秒不是次数",
        "search_q": "plank core stability",
    },
}


def render_sets_text(p: dict) -> str:
    lo, hi = p["rep_range"]
    return f"{p['sets']} × {lo}-{hi}"


def render_weight_hint(p: dict) -> str:
    w = p["start_weight_kg"]
    if w <= 0:
        return "自重"
    return f"{w} kg 起"


def main():
    plan = json.loads(PLAN.read_text(encoding="utf-8"))

    # 顶层 metadata 更新
    plan["description"] = (
        "Push / Pull / Legs 3 天循环,基于 2022-2024 evidence-based hypertrophy 研究:"
        "拉伸位优先 (Maeo/Sato 2023-2024)、周容量 12-18 sets/肌、复合 RIR 1-2 + 孤立 RIR 0-1 (Refalo 2023)、"
        "rep 范围 5-30 内增肌等效 (Schoenfeld 2021)。"
        "适合家庭设备(可调哑铃 + 综合训练机 + EZ 杆 + 训练凳)。视频列表(Nippard + Cavaliere)只是起点,你能加自己找的。"
    )
    plan["schedule_hint"] = "周一/周三/周五(休 vs 强度可调成 PPL/PPL 6 天 或 Upper-Lower 4 天提高频率)"
    plan["progression_rule"] = (
        "双重渐进 (Double Progression):上次全部组到区间上限 → 加 weight_step_kg;"
        "任一组 < 区间下限 → 减 10%;区间内 → 同重量,目标 +1 rep"
    )
    plan["deload_rule"] = (
        "每 6-8 周一次 deload 周:所有重量降 50%,组数 -1,练 1 周让 CNS / 关节 / 结缔组织恢复 (Helms 2018)"
    )
    plan["warmup"] = "5 分钟动态有氧 + 关节 ROM + 主复合动作 2 组 50% 重量 × 5 reps"

    # 改每个动作
    miss = []
    for day in plan["days"]:
        for ex in day["exercises"]:
            ex_id = ex["id"]
            spec = SPEC.get(ex_id)
            if not spec:
                miss.append(ex_id)
                continue
            ex["prescribed"] = spec["prescribed"]
            ex["evidence_note"] = spec["evidence_note"]
            ex["search_q"] = spec["search_q"]
            # 兼容旧 UI 显示
            ex["sets"] = render_sets_text(spec["prescribed"])
            ex["start_weight_hint"] = render_weight_hint(spec["prescribed"])

    if miss:
        print(f"⚠ 没有 spec 的动作 (跳过): {miss}")

    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if WEBAPP.parent.exists():
        WEBAPP.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已升级 {PLAN}")
    print(f"  动作: {sum(len(d['exercises']) for d in plan['days'])}")
    print(f"  已加 prescribed/evidence_note 的: {sum(len(d['exercises']) for d in plan['days']) - len(miss)}")


if __name__ == "__main__":
    main()
