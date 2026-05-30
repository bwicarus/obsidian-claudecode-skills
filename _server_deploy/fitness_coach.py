"""健身 AI 教练:调用 Claude(ai_client.ask)分析训练 + 调整计划。

两个对外函数:
- analyze_session(db, date, day_id, plan_data) → dict
  看本次实际 vs 计划 + 最近 3 次同 day_id 历史 → JSON 分析

- suggest_plan(db, day_id, plan_data) → dict
  看最近 4 周历史 + 最近 1 次 analysis → 调整每个动作的 prescribed

设计原则:
- 保留 PPL 框架(不让 AI 决定休息/换 split)
- AI 只调动作细节: sets / rep_range / weight / rir / rest
- 手动触发(从 UI 按钮调,不自动跑)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/bwicarus/claude/scripts")
from ai_client import ask  # noqa: E402

# 默认 Opus + max effort。用户在 fitness 设置面板可调。
DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "max"


def _ask(prompt: str, model: str = "", effort: str = "") -> str:
    """走 Claude (model/effort 从参数或默认)。"""
    return ask(
        prompt,
        claude_model=model or DEFAULT_MODEL,
        claude_effort=effort or DEFAULT_EFFORT,
    )


def _extract_json(text: str) -> dict:
    """从 AI 返回里提 JSON。容忍 ```json fence + 前后乱七八糟。"""
    if not text:
        return {}
    # 找 ```json ... ``` 块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 找最外层 {…}
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start: end + 1])
        except json.JSONDecodeError:
            pass
    return {}


def _recent_sessions(db: sqlite3.Connection, day_id: str, n: int = 4) -> list[dict]:
    """拿最近 n 次该 day_id 的训练(去重 date)。"""
    rows = db.execute(
        "SELECT DISTINCT date FROM fitness_log WHERE day_id=? "
        "ORDER BY date DESC LIMIT ?",
        (day_id, n),
    ).fetchall()
    out = []
    for r in rows:
        date = r["date"]
        sets = db.execute(
            "SELECT exercise_id, set_no, weight_kg, reps "
            "FROM fitness_log WHERE date=? AND day_id=? ORDER BY exercise_id, set_no",
            (date, day_id),
        ).fetchall()
        # group by exercise
        by_ex: dict[str, list] = {}
        for s in sets:
            by_ex.setdefault(s["exercise_id"], []).append({
                "set": s["set_no"], "w": s["weight_kg"], "r": s["reps"],
            })
        out.append({"date": date, "by_exercise": by_ex})
    return out


def _latest_analysis(db: sqlite3.Connection, day_id: str) -> dict | None:
    row = db.execute(
        "SELECT analysis_json FROM fitness_session_analysis "
        "WHERE day_id=? ORDER BY date DESC LIMIT 1",
        (day_id,),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["analysis_json"])
    except json.JSONDecodeError:
        return None


def _format_history(sessions: list[dict], plan_exercises: list[dict]) -> str:
    """把 sessions 格式化成给 AI 看的紧凑字符串。"""
    if not sessions:
        return "(无历史)"
    name_by_id = {ex["id"]: ex["name"] for ex in plan_exercises}
    lines = []
    for s in sessions:
        lines.append(f"\n=== {s['date']} ===")
        for ex_id, sets in s["by_exercise"].items():
            name = name_by_id.get(ex_id, ex_id)
            sets_str = " / ".join(
                f"{x['w']}kg×{x['r']}" if x['w'] is not None and x['r'] is not None
                else "—" for x in sets
            )
            lines.append(f"  {name} ({ex_id}): {sets_str}")
    return "\n".join(lines)


# ──────────────────────── analyze_session ────────────────────────
LITERATURE_REF = """【可引用的循证训练文献库】(自然引用,不堆砌)

# 训练量 / 频率 / 容量曲线
- Schoenfeld 2017 meta: 周容量 ≥10 sets/肌显著优于 <5(剂量-效应曲线)
- Schoenfeld 2019: 频率在 volume 等同时差别小,2x/周略胜 1x
- Baz-Valle 2022 systematic review: MAV(最大适应容量)12-20 sets/肌/周
  个体差异大,部分人 30+ sets 仍进步
- Heaselgrave 2019: junk volume(过低强度组)对增肌无贡献
- Aube 2020: 周 20 sets 后边际收益快速降
- Mangine 2015: 高频(5x/周)vs 低频在 volume 等同时同效
- Schoenfeld 2024 update: 跨研究 12-22 sets/肌/周为高响应区
- Wernbom 2007 综述: frequency + intensity + volume 三因素综合

# 力竭距离 / RIR / RPE / 强度
- Refalo 2023 systematic review: 孤立动作近力竭收益 > 复合;复合留 RIR 1-2
- Pelland 2024 meta: RIR 0-5 范围内无显著肥大差异(volume 等同)
- Helms 2024: autoregulation RPE 比固定 %1RM 更精准
- Latella 2019: training to failure 的疲劳成本(神经 + 关节)
- Hackett 2018: RPE-based autoregulation
- Krzysztofik 2019: ≥70% 1RM 阈值激活高阈值运动单位
- Carroll 2017: 神经适应 vs 肌肥大不同时间窗

# 拉伸位 / ROM / 动作选择
- Maeo 2023: 拉伸位训练长头肌肥大 +40%(overhead triceps)
- Sato 2024: 斜板二头(incline curl)拉伸位 > 站立
- Pedrosa 2022: lengthened-bias 训练在多关节同样有效
- Pinto 2012: 完整 ROM > 半 ROM(同重量)
- Bloomquist 2013: 深蹲深位(ATG)股四肥大 +25% vs 半蹲
- Vargas 2021: 离心 tempo 2-4s 增肌优于快放
- Kubo 2019: 深蹲深度对股四的影响
- Tsaklis 2015: 脚尖背屈对腘绳激活 +10%
- Andersen 2014: lat pulldown 宽 vs 窄握增肌等效

# 动作特定 EMG / 角度
- Lockie 2017: 上斜 15-30° 上胸激活最强(>45° 三角肌主导)
- Rodríguez-Ridao 2020: incline 30° optimum upper chest
- Coratella 2020: cable lateral 拉伸位有阻力 > 哑铃峰值在中段
- Reinold 2009: face pull 后三角 + 中下斜方
- Youdas 2010: 反握引体二头激活 +20% vs 正握
- Doma 2013: chin-up EMG ≈ lat pulldown 但闭链全身参与
- Marcolin 2018: EZ 杆腕关节压力 < 直杆

# 休息 / 间歇
- Schoenfeld 2016: 3 min rest 增肌 > 1 min(同 volume)

# 周期化 / Deload / 长期渐进
- Helms 2018: deload 6-8 周一次,减 40-50% volume + intensity
- Israetel MEV/MV/MAV/MRV framework(Renaissance Periodization)
- Helms 2019: cut/bulk phases 营养与训练协同
- Pearson 2021: 长期可持续 progression ≤5%/周

# 营养 / 恢复
- Phillips 2014: 蛋白质 1.6-2.2 g/kg/d 增肌 optimum
- Burd 2010: leucine threshold ~3g 触发肌肉蛋白合成
- Walberg 1988: 睡眠 <6h 抗阻训练增肌减半
- Murach 2018: satellite cells 恢复机制
- Mannarino 2024: 中年男性恢复速度 / 容量耐受
"""

ANALYZE_PROMPT = """你是一名世界级的循证健身教练 / 运动科学博士。深度思考下面问题。

{literature}

任务:深度分析一次完成的训练,不是简单看数字:
- 推断真实疲劳(reps 完成度 + 时间间隔 + RIR target 完成情况)
- 跟历史对比识别 stagnation(同 W×R 连续 ≥3 次)、overreach(完成率<70%)
- 跨动作识别(eg 卧推顶上但飞鸟掉了 → 三头疲劳 cap 了卧推?)
- 给具体 RPE 估算 + 每个动作 verdict + next_action
- key_insights / warnings 引用具体研究或经验法则,提供 ≥3 条 insights
- 别罗列文献,挑跟本次最相关的 1-3 篇精确引用

【本次训练】({date} · {day_id} 日)
{actual}

【本次训练】({date} · {day_id} 日)
{actual}

【对应计划(每个动作的目标 sets×rep_range @ weight)】
{planned}

【最近 3 次同日历史(参考趋势)】
{history}

输出严格 JSON(不要 markdown 不要解释,只输出 {{...}}):
{{
  "completion_rate_pct": 95,
  "rpe_estimate": 7.5,
  "verdict": "progress" | "stagnation" | "overreach" | "fatigue" | "deload_due",
  "summary": "1-2 句话整体评价",
  "per_exercise": [
    {{
      "id": "db_bench_flat",
      "verdict": "progress|stagnation|fail|skipped",
      "note": "上次 8/8/7/6 → 这次 9/8/8/7,下次可 +1.25kg",
      "next_action": "+1.25kg" | "+1 rep" | "hold" | "-5% deload" | "swap_to:incline_press"
    }}
  ],
  "key_insights": ["..."],
  "warnings": ["..."]
}}

约束:
- per_exercise 只列**实际有记录**的动作(没做的不列)
- next_action 固定 enum: "+1.25kg" / "+2.5kg" / "+5kg" / "+1 rep" / "hold" /
  "-5% deload" / "-10% deload" / "swap_to:<exercise_id>" / "rest_more" /
  "add_set" / "drop_set"
- 全部中文输出 summary / note / insights / warnings
- 术语带括号:RIR(剩余次数) / RPE(自感强度) / hypertrophy(肌肥大) / MAV(最大适应容量)
- key_insights 至少 2 条,必须引用研究或趋势数据,不要"加油"鸡汤
- per_exercise 的 note 加 rationale_brief 字段(简短引用 Schoenfeld/Maeo/Refalo 等)
"""


def analyze_session(db: sqlite3.Connection, date: str, day_id: str,
                    plan_data: dict, model: str = "", effort: str = "") -> dict:
    """分析一次完成的训练。返回 JSON dict(失败时 {}}。"""
    # 拉本次实际 + 计划
    rows = db.execute(
        "SELECT exercise_id, set_no, weight_kg, reps FROM fitness_log "
        "WHERE date=? AND day_id=? ORDER BY exercise_id, set_no",
        (date, day_id),
    ).fetchall()
    if not rows:
        return {"error": "no_data_for_session"}
    actual_by_ex: dict[str, list] = {}
    for r in rows:
        actual_by_ex.setdefault(r["exercise_id"], []).append({
            "set": r["set_no"], "w": r["weight_kg"], "r": r["reps"],
        })
    # 格式化
    day = next((d for d in plan_data["days"] if d["id"] == day_id), None)
    if not day:
        return {"error": f"unknown day_id {day_id}"}
    name_by_id = {ex["id"]: ex["name"] for ex in day["exercises"]}
    actual_lines = []
    for ex_id, sets in actual_by_ex.items():
        name = name_by_id.get(ex_id, ex_id)
        sets_str = " / ".join(
            f"{x['w']}kg×{x['r']}" if x['w'] is not None and x['r'] is not None
            else "—" for x in sets
        )
        actual_lines.append(f"  {name} ({ex_id}): {sets_str}")
    actual_str = "\n".join(actual_lines)

    planned_lines = []
    for ex in day["exercises"]:
        p = ex.get("prescribed") or {}
        if ex["id"] not in actual_by_ex:
            continue   # 没做的不放参考
        lo, hi = (p.get("rep_range") or [0, 0])
        planned_lines.append(
            f"  {ex['name']} ({ex['id']}): "
            f"{p.get('sets')}×{lo}-{hi} @ {p.get('start_weight_kg')}kg "
            f"(RIR {p.get('rir_target')})"
        )
    planned_str = "\n".join(planned_lines)

    # 历史(去掉今天)
    sessions = _recent_sessions(db, day_id, n=4)
    sessions = [s for s in sessions if s["date"] != date][:3]
    history = _format_history(sessions, day["exercises"])

    prompt = ANALYZE_PROMPT.format(
        literature=LITERATURE_REF,
        date=date, day_id=day_id,
        actual=actual_str, planned=planned_str, history=history,
    )
    try:
        resp = _ask(prompt, model=model, effort=effort)
    except Exception as e:
        return {"error": f"ai_call_failed: {e}"}
    result = _extract_json(resp)
    if not result:
        return {"error": "json_parse_failed", "raw": resp[:500]}
    result["_raw_chars"] = len(resp or "")
    return result


# ──────────────────────── suggest_plan ────────────────────────
SUGGEST_PROMPT = """你是一名世界级的循证健身教练 / 运动科学博士。深度思考给用户调整
本次 {day_id} 训练。

{literature}

思考要素(深度):
- Double Progression:全 rep_range 上限 → 加重;<下限 → 减 10%;中间 → +1 rep
- 检查 stagnation(同重连续 ≥3 次同 reps)→ 换 rep range / RIR / 变体
- 平衡容量(肌群周 sets 在 MAV-MRV 区间,Baz-Valle 2022)
- 关节疲劳征兆(同侧重复力竭 / 完成率掉)→ 降量或换 lengthened bias 变体
- 长头肌(三头长头 / 二头长头 / 腘绳长头)优先拉伸位 (Maeo 2023)
- 起步周(无历史)维持默认,先建基线 RPE 反馈环
- 复合动作留 RIR 1-2,孤立动作可近力竭 (Refalo 2023)
- 上斜推 angle 15-30° (Lockie 2017)
- 自重动作做不到下限 → negatives / band-assisted;能 reps_high+ → 加负重

【当前默认计划(动作 + 默认 prescribed)】
{default}

【最近 4 次同日历史】
{history}

【最近一次 AI 分析(若有)】
{latest_analysis}

任务:对每个动作给出本次推荐 prescribed,严格 JSON 输出:
{{
  "overall_reasoning": "1-2 句话整体编排逻辑(中文)",
  "exercises": [
    {{
      "id": "db_bench_flat",
      "prescribed": {{
        "sets": 4,
        "rep_range": [6, 10],
        "rir_target": 2,
        "rest_seconds": 180,
        "start_weight_kg": 11.25,
        "weight_step_kg": 1.25
      }},
      "change_summary": "+1.25kg",
      "reasoning": "上次 4 组全 ≥8 reps,可加重(中文)"
    }}
  ]
}}

规则:
- 保留默认计划的**所有**动作(不增删,顺序保留)
- 只调 prescribed 内字段
- start_weight_kg 必须按 weight_step_kg 取整(weight_step_kg=1.25 → 1.25 的倍数)
- 没有历史的动作维持默认(先建基线)
- change_summary 一眼能看的短语:
  "+1.25kg" / "+2.5kg" / "维持" / "-5% deload" / "+1 reps target" /
  "RIR 调 1→2" / "rest 调 90→120s" / "rep range 调 6-10→8-12" / etc.
- reasoning 一句中文,**必须**引用历史数据 OR 研究依据,不能空话
"""


def suggest_plan(db: sqlite3.Connection, day_id: str, plan_data: dict,
                 model: str = "", effort: str = "") -> dict:
    """看历史 + 上次分析,给出 day_id 的新 prescribed 建议。"""
    day = next((d for d in plan_data["days"] if d["id"] == day_id), None)
    if not day:
        return {"error": f"unknown day_id {day_id}"}

    default_lines = []
    for ex in day["exercises"]:
        p = ex.get("prescribed") or {}
        default_lines.append(
            f"  {ex['name']} ({ex['id']}): {json.dumps(p, ensure_ascii=False)}"
        )
    default_str = "\n".join(default_lines)

    sessions = _recent_sessions(db, day_id, n=4)
    history = _format_history(sessions, day["exercises"])
    latest = _latest_analysis(db, day_id)
    latest_str = json.dumps(latest, ensure_ascii=False, indent=2) if latest else "(无)"

    prompt = SUGGEST_PROMPT.format(
        literature=LITERATURE_REF,
        day_id=day_id,
        default=default_str,
        history=history,
        latest_analysis=latest_str,
    )
    try:
        resp = _ask(prompt, model=model, effort=effort)
    except Exception as e:
        return {"error": f"ai_call_failed: {e}"}
    result = _extract_json(resp)
    if not result:
        return {"error": "json_parse_failed", "raw": resp[:500]}
    result["_raw_chars"] = len(resp or "")
    return result
