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
ANALYZE_PROMPT = """你是一名循证健身教练。看用户一次刚完成的训练数据,给简洁分析。

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
- next_action 只能是固定 enum: "+1.25kg" / "+2.5kg" / "+1 rep" / "hold" / "-5% deload" / "swap_to:<id>" / "rest_more"
- 中文输出 summary / note / insights / warnings
- 专业术语带括号中文: RIR(剩余次数) / RPE(自感强度) 等
"""


def analyze_session(db: sqlite3.Connection, date: str, day_id: str,
                    plan_data: dict) -> dict:
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
        date=date, day_id=day_id,
        actual=actual_str, planned=planned_str, history=history,
    )
    try:
        resp = ask(prompt)
    except Exception as e:
        return {"error": f"ai_call_failed: {e}"}
    result = _extract_json(resp)
    if not result:
        return {"error": "json_parse_failed", "raw": resp[:500]}
    result["_raw_chars"] = len(resp or "")
    return result


# ──────────────────────── suggest_plan ────────────────────────
SUGGEST_PROMPT = """你是一名循证健身教练。看用户最近训练历史 + 上次 AI 分析,
给本次 {day_id} 训练每个动作的具体推荐。

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
- 保留默认计划的所有动作(不增删,顺序也保留)
- 只调 prescribed 内字段
- start_weight_kg 必须按 weight_step_kg 取整
- 没有历史的动作维持默认
- change_summary 用一眼能看的短语: "+1.25kg" / "维持" / "-5% deload" / "+1 reps target" / "RIR 调 1→2" / etc.
- reasoning 一句话中文,引用历史数据
"""


def suggest_plan(db: sqlite3.Connection, day_id: str, plan_data: dict) -> dict:
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
        day_id=day_id,
        default=default_str,
        history=history,
        latest_analysis=latest_str,
    )
    try:
        resp = ask(prompt)
    except Exception as e:
        return {"error": f"ai_call_failed: {e}"}
    result = _extract_json(resp)
    if not result:
        return {"error": "json_parse_failed", "raw": resp[:500]}
    result["_raw_chars"] = len(resp or "")
    return result
