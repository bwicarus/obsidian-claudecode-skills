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


# ──────────────────────── coach_chat (多轮复盘对话) ────────────────────────
COACH_CHAT_PROMPT = """你是一名世界级的循证健身教练 / 运动科学博士,正在和运动员**复盘刚结束的 {day_id} 训练**。
通过对话了解训练实情(感受 / 哪里吃力 / 酸痛或不适 / 睡眠恢复 / 可用时间 / 器械限制 / RPE 真实感受),
信息足够后给出对**下次**该训练的计划调整。

{literature}

【本次训练实际】({date} · {day_id})
{actual}

【对应计划目标(每动作 sets×rep_range @ weight, RIR, step)】
{planned}

【最近几次同日历史】
{history}

【本次客观分析(系统已生成,供你承接)】
{analysis}

【对话记录】(你=教练,用户=运动员)
{transcript}

任务:像真人教练一样回应用户**最新一条**。先判断信息是否足够负责任地调下次计划:
- 信息不足(关键缺口:具体哪个动作吃力 / 哪里酸痛或受限 / 可用时间 / 器械 / 睡眠恢复 / RPE 真实感受)
  → 自然地**追问 1-2 个最关键的问题**, ready=false, proposal=null。
- 信息足够 → 给一段中文复盘 + 调整说明(放进 reply), 并产出 proposal(每个动作的新 prescribed)。
- **尽快收敛**:最多追问一轮;用户已答复关键问题、无重大安全未知时,**直接 ready=true 给出 proposal**,
  不要反复追问或只讲不调。若用户明确要"直接给计划",必须 ready=true 出 proposal(哪怕仅基于现有信息)。
- 用户提到的限制必须显式落到 proposal:例 "肩疼"→ 该动作换 lengthened-bias 变体或降量并在 reasoning 说明;
  "只有 30 分钟"→ 砍 junk volume / 缩 rest;"某动作很轻松"→ 加重或加 rep。

**整个回复必须是一个严格 JSON 对象**(不要 markdown 代码块外的任何字,不要在 JSON 前后写解释),格式:
{{
  "reply": "对用户说的话(中文,自然口语,可含追问;所有要对用户讲的内容都放这里)",
  "ready": false,
  "proposal": null
}}
当 ready=true 时, proposal 为(结构同计划调整):
{{
  "overall_reasoning": "整体调整逻辑(中文)",
  "exercises": [
    {{"id":"db_bench_flat","prescribed":{{"sets":4,"rep_range":[6,10],"rir_target":2,"rest_seconds":180,"start_weight_kg":11.25,"weight_step_kg":1.25}},"change_summary":"+1.25kg","reasoning":"中文,引用对话实情/历史/研究"}}
  ]
}}

proposal 规则(同计划调整):
- 保留该日**所有**动作(不增删,顺序保留),只调 prescribed 内字段
- start_weight_kg 必须按 weight_step_kg 取整(step=1.25 → 1.25 的倍数)
- reasoning 必须引用对话实情 / 历史数据 / 研究依据,不空话
- 信息不足以调的动作维持当前值
- 数字与术语用纯文本,术语带括号:RIR(剩余次数) / RPE(自感强度) / ROM(动作幅度)
"""


def coach_chat(db: sqlite3.Connection, date: str, day_id: str, plan_data: dict,
               messages: list[dict], model: str = "", effort: str = "") -> dict:
    """多轮复盘对话(无状态:每轮把全量对话历史拼进单 prompt,因 ai_client.ask 无历史)。
    messages = [{"role":"user"|"assistant","content":str}, ...](含最新一条 user)。
    返回 {reply, ready, proposal}:ready=true 时 proposal 为 suggest_plan 同结构。"""
    day = next((d for d in plan_data["days"] if d["id"] == day_id), None)
    if not day:
        return {"error": f"unknown day_id {day_id}"}
    # 本次实际
    rows = db.execute(
        "SELECT exercise_id, set_no, weight_kg, reps FROM fitness_log "
        "WHERE date=? AND day_id=? ORDER BY exercise_id, set_no",
        (date, day_id),
    ).fetchall()
    name_by_id = {ex["id"]: ex["name"] for ex in day["exercises"]}
    actual_by_ex: dict[str, list] = {}
    for r in rows:
        actual_by_ex.setdefault(r["exercise_id"], []).append(
            {"w": r["weight_kg"], "r": r["reps"]})
    actual_lines = []
    for ex_id, sets in actual_by_ex.items():
        ss = " / ".join(
            f"{x['w']}kg×{x['r']}" if x['w'] is not None and x['r'] is not None else "—"
            for x in sets)
        actual_lines.append(f"  {name_by_id.get(ex_id, ex_id)} ({ex_id}): {ss}")
    actual_str = "\n".join(actual_lines) or "(无记录)"
    # 计划目标
    planned_lines = []
    for ex in day["exercises"]:
        p = ex.get("prescribed") or {}
        lo, hi = (p.get("rep_range") or [0, 0])
        planned_lines.append(
            f"  {ex['name']} ({ex['id']}): {p.get('sets')}×{lo}-{hi} @ "
            f"{p.get('start_weight_kg')}kg (RIR {p.get('rir_target')}, step {p.get('weight_step_kg')})")
    planned_str = "\n".join(planned_lines)
    # 历史(去掉今天)
    sessions = [s for s in _recent_sessions(db, day_id, n=4) if s["date"] != date][:3]
    history = _format_history(sessions, day["exercises"])
    # 客观分析
    latest = _latest_analysis(db, day_id)
    analysis_str = json.dumps(latest, ensure_ascii=False, indent=2) if latest else "(无)"
    # 对话记录
    tlines = []
    for m in messages:
        who = "用户" if m.get("role") == "user" else "教练"
        tlines.append(f"{who}: {m.get('content', '')}")
    transcript = "\n".join(tlines) or "(空)"

    prompt = COACH_CHAT_PROMPT.format(
        literature=LITERATURE_REF, date=date, day_id=day_id,
        actual=actual_str, planned=planned_str, history=history,
        analysis=analysis_str, transcript=transcript,
    )
    try:
        resp = _ask(prompt, model=model, effort=effort)
    except Exception as e:
        return {"error": f"ai_call_failed: {e}"}
    result = _extract_json(resp)
    if not result or "reply" not in result:
        # 容错:AI 没给干净 JSON → 整段当 reply(对话至少能继续)
        return {"reply": (resp or "").strip()[:2000] or "(无回复)",
                "ready": False, "proposal": None, "_parse_fallback": True}
    result.setdefault("ready", False)
    result.setdefault("proposal", None)
    return result


# ──────────────────────── 全身平衡体检 (balance_check) ────────────────────────
# 动作 → 容量桶摊派权重(复合动作把协同肌按权重摊给对应桶,否则后束/三头会被系统性算成 0)。
# 桶:h_push/v_push(=push)、h_pull/v_pull(=pull)、front_delt/side_delt/rear_delt、
#    biceps(肘屈)/triceps(肘伸)、quad/ham/glute/calf/core、anterior/posterior(躯干+腿前后链;手臂不计入链)。
# 权重循证近似(EMG/经验),不求精确,供比值方向判断 + AI 解读。新增/换动作时在此补一行。
MUSCLE_MAP = {
    # —— push 日 ——
    "db_bench_flat":      {"h_push": 1, "front_delt": 0.5, "triceps": 0.3, "anterior": 1},
    "db_bench_incline":   {"h_push": 1, "front_delt": 0.6, "triceps": 0.3, "anterior": 1},
    "db_shoulder_press":  {"v_push": 1, "front_delt": 1.0, "triceps": 0.3, "anterior": 1},
    "cable_lateral_raise": {"side_delt": 1},                      # 外展孤立,不计推/拉/链
    "db_lateral_raise":    {"side_delt": 1},
    "cable_tricep_pushdown": {"triceps": 1},
    "dip":                {"v_push": 1, "front_delt": 0.3, "triceps": 0.5, "anterior": 1},
    "db_overhead_tricep": {"triceps": 1},
    # —— pull 日 ——
    "chin_up":     {"v_pull": 1, "biceps": 0.4, "posterior": 1},
    "lat_pulldown": {"v_pull": 1, "biceps": 0.3, "posterior": 1},
    "db_row_single": {"h_pull": 1, "rear_delt": 0.3, "biceps": 0.3, "posterior": 1},
    "seated_row":  {"h_pull": 1, "rear_delt": 0.4, "biceps": 0.3, "posterior": 1},
    "face_pull":   {"h_pull": 0.5, "rear_delt": 1.0, "posterior": 1},
    "ez_curl":     {"biceps": 1},
    "db_incline_curl": {"biceps": 1},
    # —— legs 日 ——
    "goblet_squat":  {"quad": 1, "glute": 0.5, "ham": 0.2, "anterior": 1},
    "rdl":           {"ham": 1, "glute": 0.7, "posterior": 1},
    "leg_extension": {"quad": 1, "anterior": 1},
    "leg_curl":      {"ham": 1, "posterior": 1},
    "db_calf_raise": {"calf": 1, "posterior": 0.3},
    "hanging_leg_raise": {"core": 1, "anterior": 0.3},
}

# 六大拮抗比:(标签, 分子桶组, 分母桶组, 健康区间[lo,hi], 黄阈, 红阈方向说明)
# value=分子/分母。status 由 _ratio_status 按区间判。
_BAL_RATIOS = [
    {"name": "拉:推 容量比", "num": ["h_pull", "v_pull"], "den": ["h_push", "v_push"],
     "good": [0.8, 1.4], "yellow": 0.65, "red": 0.5, "dir": "low_bad",
     "hint": "拉应≥推(≥1:1);圆肩/久坐矫正期可 1.5–2:1。推远多于拉→肩胛前倾/圆肩/肩峰撞击风险"},
    {"name": "股四:腘绳 容量比", "num": ["quad"], "den": ["ham"],
     "good": [1.0, 1.5], "yellow": 1.8, "red": 2.2, "dir": "high_bad",
     "hint": "股四略多正常(深蹲也练后链);后链严重不足→ACL/腘绳拉伤风险升"},
    {"name": "后束:前束 容量比", "num": ["rear_delt"], "den": ["front_delt"],
     "good": [0.8, 1.5], "yellow": 0.5, "red": 0.3, "dir": "low_bad",
     "hint": "后束直接量应接近前束总刺激;后束欠练→圆肩/肩前移/推力受限(多数人后束严重不足)"},
    {"name": "垂直拉:水平拉 比", "num": ["v_pull"], "den": ["h_pull"],
     "good": [0.7, 1.4], "yellow": 1.6, "red": 2.0, "dir": "high_bad",
     "hint": "只做引体/下拉、不练划船→菱形/中斜方(肩胛后缩)不足,姿态差"},
    {"name": "二头:三头 容量比", "num": ["biceps"], "den": ["triceps"],
     "good": [0.7, 1.2], "yellow": 1.5, "red": 2.0, "dir": "high_bad",
     "hint": "三头肌量约上臂2/3,孤立量略多正常;只练弯举→肘伸不足"},
    {"name": "后链:前链 总比", "num": ["posterior"], "den": ["anterior"],
     "good": [0.85, 1.3], "yellow": 0.7, "red": 0.55, "dir": "low_bad",
     "hint": "后链应接近或略多于前链;全身前侧主导→姿态/肩健康风险"},
]


def _ratio_status(val, spec) -> str:
    """green / yellow / red。dir=low_bad:值越低越糟;high_bad:越高越糟。"""
    if val is None:
        return "na"
    if spec["dir"] == "low_bad":
        if val < spec["red"]:
            return "red"
        if val < spec["yellow"]:
            return "yellow"
        return "green"
    else:  # high_bad
        if val > spec["red"]:
            return "red"
        if val > spec["yellow"]:
            return "yellow"
        return "green"


# ── 力量(est-1RM)拮抗平衡：动作负重"模态"标注 + 同模态拮抗对 ──
# est-1RM 跨模态量纲不一致(哑铃单手 vs 器械满栈 vs 绳索阻力),直接相除会出假象。
# 故只在 *同一模态* 内比 est-1RM;跨模态/缺动作一律 insufficient(诚实降级,不硬算)。
_EXERCISE_MODALITY = {
    "db_bench_flat": "db", "db_bench_incline": "db", "db_shoulder_press": "db",
    "db_row_single": "db", "db_incline_curl": "db", "db_overhead_tricep": "db",
    "db_lateral_raise": "db", "db_calf_raise": "db", "goblet_squat": "db",
    "lat_pulldown": "machine", "seated_row": "machine",
    "leg_extension": "machine", "leg_curl": "machine",
    "cable_lateral_raise": "cable", "cable_tricep_pushdown": "cable", "face_pull": "cable",
    "ez_curl": "barbell", "rdl": "barbell",
    "chin_up": "bodyweight", "dip": "bodyweight", "hanging_leg_raise": "bodyweight",
}

# 力量拮抗对:只在同模态、两侧各≥2组时算 est-1RM 比;否则 insufficient。
# good=[lo,hi] 绿区;绿区外到 yel=[lo,hi] 为 yellow,再外为 red(双向偏离都不好)。
# weak_low/weak_high:比值偏低/偏高时"弱"的那块肌肉(用于 3D 上色)。
_STRENGTH_PAIRS = [
    {"key": "pull_push_h", "name": "水平拉:推 力量比",
     "num": ["db_row_single", "seated_row"], "den": ["db_bench_flat", "db_bench_incline"],
     "good": [0.85, 1.25], "yel": [0.70, 1.45], "weak_low": "back", "weak_high": "chest",
     "evidence": "中等(肩健康共识+群体数据):水平拉应≈水平推。拉显著弱(<0.7)→肩胛后缩不足、圆肩/肩峰撞击风险(Reinold 2009;scapular force couple)。本系统用训练负荷 est-1RM 近似,非等速测力",
     "hint": "推拉力量应大致相当;拉明显弱→圆肩倾向"},
    {"key": "ham_quad", "name": "腘绳:股四 力量比(H:Q)",
     "num": ["leg_curl"], "den": ["leg_extension"],
     "good": [0.55, 0.85], "yel": [0.45, 1.05], "weak_low": "hamstrings", "weak_high": "quads",
     "evidence": "强(就方向):等速 H:Q≈0.6(Coombs&Garbutt 2002);低 H:Q 与腘绳拉伤/ACL 风险相关(Croisier 2008)。本系统用同器械负荷比近似,仅方向参考、非等速力矩比",
     "hint": "腘绳应达股四约 0.6;过低→后链弱、拉伤/ACL 风险升"},
    {"key": "bi_tri", "name": "二头:三头 力量比",
     "num": ["ez_curl", "db_incline_curl"], "den": ["cable_tricep_pushdown", "db_overhead_tricep"],
     "good": [0.5, 1.0], "yel": [0.4, 1.3], "weak_low": None, "weak_high": "triceps",
     "evidence": "弱(解剖):三头横截面>二头,力量上肘伸通常≥肘屈;无公认硬比,仅同模态干净对时判",
     "hint": "三头力量通常≥二头;二头明显强(>1.3)→只练弯举、肘伸不足"},
    {"key": "vpull_hpull", "name": "垂直拉:水平拉 力量比",
     "num": ["lat_pulldown", "chin_up"], "den": ["db_row_single", "seated_row"],
     "good": [0.8, 1.3], "yel": [0.6, 1.6], "weak_low": "back", "weak_high": "back",
     "evidence": "弱-中:同模态下下拉≈划船;只做下拉不划船→菱形/中斜方(肩胛后缩)欠练",
     "hint": "下拉与划船力量应相当"},
]


def _strength_status(r, spec):
    lo, hi = spec["good"]
    ylo, yhi = spec["yel"]
    if lo <= r <= hi:
        return "green"
    if ylo <= r <= yhi:
        return "yellow"
    return "red"


def _compute_strength_ratios(per_ex):
    """per_ex: {exid: {"e1rm": float, "sets": int, "modality": str}}。
    每对只在同模态、两侧都有(且各≥2组)时算 est-1RM 比;否则 insufficient(给出缺口提示)。"""
    def side(exids):
        by_mod = {}                       # mod -> (e1rm, low_conf)
        for exid in exids:
            d = per_ex.get(exid)
            if not d or d["e1rm"] <= 0 or d["sets"] < 2:
                continue
            if d["e1rm"] > by_mod.get(d["modality"], (0, False))[0]:
                by_mod[d["modality"]] = (d["e1rm"], d.get("low_conf", False))
        return by_mod

    def missing(spec):
        n, d = side(spec["num"]), side(spec["den"])
        if not n and not d:
            return "两侧都缺数据;先补这对动作"
        if not n:
            return "缺「" + "/".join(spec["num"]) + "」(≥2组)"
        if not d:
            return "缺「" + "/".join(spec["den"]) + "」(≥2组)"
        return "两侧设备不同(跨模态不可比),需同器械再比"

    out = []
    for spec in _STRENGTH_PAIRS:
        n, d = side(spec["num"]), side(spec["den"])
        shared = set(n) & set(d)
        base = {"name": spec["name"], "key": spec["key"], "evidence": spec["evidence"],
                "hint": spec["hint"], "weak_low": spec["weak_low"], "weak_high": spec["weak_high"]}
        if not shared:
            out.append({**base, "value": None, "status": "insufficient",
                        "modality": None, "missing": missing(spec)})
            continue
        # 选两侧都有、较重(更可信)的模态
        mod = max(shared, key=lambda m: min(n[m][0], d[m][0]))
        nv, ncf = n[mod]
        dv, dcf = d[mod]
        r = round(nv / dv, 2)
        status = _strength_status(r, spec)
        low_conf = ncf or dcf
        if low_conf and status == "red":
            status = "yellow"            # 高 rep Epley 外推不可信 → 封顶 yellow,不判 red
        out.append({**base, "value": r, "status": status, "low_conf": low_conf,
                    "modality": mod, "num_1rm": nv, "den_1rm": dv})
    return out


def _balance_profile(db: sqlite3.Connection, plan_data: dict, window_days: int = 28) -> dict:
    """跨所有训练日聚合近 window_days 的容量到拮抗桶,算六大平衡比 + 每动作 est-1RM。纯客观,不调 AI。"""
    import datetime as _dt
    # 窗口起始(本地日期)
    today = _dt.datetime.utcfromtimestamp(__import__("time").time() + 9 * 3600).date()
    since = (today - _dt.timedelta(days=window_days - 1)).strftime("%Y-%m-%d")
    weeks = max(1.0, window_days / 7.0)
    rows = db.execute(
        "SELECT date, exercise_id, weight_kg, reps FROM fitness_log WHERE date>=?",
        (since,),
    ).fetchall()
    buckets: dict[str, float] = {}
    raw_sets: dict[str, float] = {}        # 名义组数(不摊派,仅该动作主桶计)——这里用 exercise 计数
    est1rm: dict[str, float] = {}
    est1rm_reps: dict[str, int] = {}
    ex_sets: dict[str, int] = {}
    ex_name = {ex["id"]: ex["name"] for d in plan_data.get("days", []) for ex in d["exercises"]}
    total_sets = 0
    unmapped = set()
    for r in rows:
        exid = r["exercise_id"]
        total_sets += 1
        ex_sets[exid] = ex_sets.get(exid, 0) + 1
        w = r["weight_kg"]
        reps = r["reps"]
        # est-1RM(Epley),取该动作最高;记录贡献该最高值的 reps(用于高 rep 外推降权)
        if w is not None and reps is not None and reps > 0:
            e = w * (1 + reps / 30.0)
            if e > est1rm.get(exid, 0):
                est1rm[exid] = round(e, 1)
                est1rm_reps[exid] = reps
        m = MUSCLE_MAP.get(exid)
        if not m:
            unmapped.add(exid)
            continue
        for bucket, wt in m.items():
            buckets[bucket] = buckets.get(bucket, 0) + wt
    # 周均
    wk = {k: round(v / weeks, 1) for k, v in buckets.items()}

    def _sum(keys):
        return sum(wk.get(k, 0) for k in keys)

    ratios = []
    for spec in _BAL_RATIOS:
        num = _sum(spec["num"])
        den = _sum(spec["den"])
        # 数据充分度:任一侧周均 < 3 组 → insufficient(只展示不判定)
        insufficient = num < 3 or den < 3
        val = round(num / max(den, 0.1), 2) if den > 0 else None
        ratios.append({
            "name": spec["name"], "value": val,
            "num_sets": round(num, 1), "den_sets": round(den, 1),
            "good_range": spec["good"], "hint": spec["hint"],
            "status": "insufficient" if insufficient else _ratio_status(val, spec),
        })
    # —— 力量(est-1RM)拮抗比(主信号);跨模态/缺动作 → insufficient ——
    per_ex = {exid: {"e1rm": est1rm.get(exid, 0.0), "sets": ex_sets.get(exid, 0),
                     "modality": _EXERCISE_MODALITY.get(exid, "other"),
                     "low_conf": est1rm_reps.get(exid, 0) > 12}   # 高 rep Epley 外推不可信
              for exid in set(list(est1rm) + list(ex_sets))}
    strength_ratios = _compute_strength_ratios(per_ex)
    n_eval = sum(1 for s in strength_ratios if s["status"] != "insufficient")
    if total_sets < 8:
        data_suff = "insufficient"
    elif n_eval >= len(_STRENGTH_PAIRS):
        data_suff = "full"
    else:
        data_suff = "partial"

    return {
        "window_days": window_days, "weeks": round(weeks, 1), "since": since,
        "total_logged_sets": total_sets,
        "weekly_sets_by_bucket": wk,
        "est_1rm": {ex_name.get(k, k): v for k, v in sorted(est1rm.items())},
        "ratios": ratios,                       # 容量比(辅助/覆盖度,不再驱动上色)
        "strength_ratios": strength_ratios,     # 力量比(主信号,驱动 3D 上色)
        "data_sufficiency": data_suff,
        "unmapped_exercises": sorted(unmapped),
    }


def _muscle_status(profile: dict) -> dict:
    """每块肌肉 green/yellow/red/insufficient(给 3D 人体上色)。
    **主信号=力量比**(同模态 est-1RM 拮抗对);力量比 insufficient/未覆盖的肌肉回退容量绝对值。
    每块带 source:'strength'(力量比驱动) / 'volume'(容量兜底)。"""
    wk = profile.get("weekly_sets_by_bucket", {})

    def vol_status(v, low=4.0, verylow=2.0):
        if v < 0.5:
            return "insufficient"
        if v < verylow:
            return "red"
        if v < low:
            return "yellow"
        return "green"

    out = {}

    def put_vol(key, label, bucket=None, raw=None):
        v = raw if raw is not None else wk.get(bucket, 0)
        out[key] = {"status": vol_status(v), "label": label, "weekly": round(v, 1), "source": "volume"}

    # —— 1) 全部先用容量兜底 ——
    put_vol("chest", "胸", raw=wk.get("h_push", 0) + wk.get("v_push", 0))
    put_vol("back",  "背 / 拉", raw=wk.get("h_pull", 0) + wk.get("v_pull", 0))
    put_vol("front_delt", "前束(三角前)", "front_delt")
    put_vol("side_delt",  "中束(三角中)", "side_delt")
    put_vol("rear_delt",  "后束(三角后)", "rear_delt")
    put_vol("biceps", "二头", "biceps")
    put_vol("triceps", "三头", "triceps")
    put_vol("core", "核心 / 腹", "core")
    put_vol("glutes", "臀", "glute")
    put_vol("calves", "小腿", "calf")
    put_vol("quads", "股四头", "quad")
    put_vol("hamstrings", "腘绳", "ham")

    # —— 2) 力量比覆盖(主信号):可评的对 → 弱侧给力量状态、强侧绿 ——
    spec_by_key = {p["key"]: p for p in _STRENGTH_PAIRS}

    def put_str(key, status, s):
        if key and key in out:
            out[key] = {"status": status, "label": out[key]["label"], "source": "strength",
                        "metric": s["name"], "value": s.get("value")}

    for s in profile.get("strength_ratios", []):
        if s["status"] == "insufficient":
            continue
        spec = spec_by_key.get(s["key"])
        if not spec:
            continue
        r = s["value"]
        lo, hi = spec["good"]
        if s["status"] == "green":
            put_str(s["weak_low"], "green", s)
            put_str(s["weak_high"], "green", s)
        elif r < lo:                                   # 偏低 → weak_low 弱
            put_str(s["weak_low"], s["status"], s)
            if s["weak_high"] != s["weak_low"]:        # 同肌肉(如 vpull_hpull 都是 back)别用 green 盖掉失衡
                put_str(s["weak_high"], "green", s)
        else:                                          # 偏高 → weak_high 弱
            put_str(s["weak_high"], s["status"], s)
            if s["weak_low"] != s["weak_high"]:
                put_str(s["weak_low"], "green", s)
    return out


BALANCE_PROMPT = """你是一名世界级的循证健身教练 / 运动科学博士。下面是用户近 {weeks} 周(跨 Push/Pull/Legs)的训练数据。
**全身平衡体检以「力量比」为主信号**(同模态 est-1RM 相除的拮抗肌群力量对比)——这能反映用户『一开始就有的体态/力量失衡』,而非只看训练量(练得多≠强)。容量比仅作辅助(覆盖度:某肌群练没练够、1RM 可不可信)。

{literature}

【力量比(主信号;同模态 est-1RM 拮抗对,后端已算,你只解读不重算)】
{strength}
说明:status=insufficient = 该对两侧**不在同一负重模态**(哑铃/器械/绳索量纲不可比)**或缺动作**——这种**绝不下失衡结论**,只用 missing 字段提示"补哪个同器械动作就能解锁这条力量比"。evidence 字段标了证据强度:在 risk 里如实区分『等速研究支持(如 H:Q)』vs『经验法则(如 bench:OHP)』,别把经验法则讲成"研究证实"。

【容量比(辅助/覆盖度;周均有效组数,非力量,别据此下力量失衡结论)】
{ratios}

【各拮抗桶周均组数(覆盖度)】
{buckets}

【各动作 est-1RM(Epley,kg;仅同模态内可比)】
{est1rm}

数据说明:近 {weeks} 周共 {total_sets} 个记录组,data_sufficiency={data_suff}。
- 力量比 insufficient → 不下结论。容量极低(某肌群周<2组)→ 提示"练得太少",但务必区分『没练』(容量低)与『练了也相对弱』(力量比偏低=既有体态问题的关键信号,优先纠正)。
- **误报代价 > 漏报**:宁可说"数据不够",不要凭跨模态假象判失衡、让用户做不必要的纠正训练牺牲增肌主线。

输出严格 JSON(只输出 {{...}}):
{{
  "overall_balance_grade": "balanced|minor_imbalance|moderate_imbalance|major_imbalance",
  "summary": "2-3 句中文:最突出的 1-2 个失衡 + 总体方向",
  "data_sufficiency": "full|partial|insufficient",
  "imbalances": [
    {{
      "ratio_name": "水平拉:推 力量比",
      "measured": 0.6,
      "severity": "yellow|red",
      "risk": "中文,具体风险 + 自然引用 1 篇文献(如 Reinold 2009 / Schoenfeld / Cavaliere)",
      "root_cause": "中文,指出哪些动作堆太多/哪些缺位(用具体动作名+组数)"
    }}
  ],
  "corrective_actions": [
    {{
      "concrete": "中文具体处方,如『Pull 日加 3 组面拉 + 坐姿划船 3→4 组,目标水平拉≥水平推』",
      "suggested_exercise_ids": ["face_pull","seated_row"],
      "delta_sets_per_week": "+5",
      "expected_after": "拉:推 ≈ 1:1"
    }}
  ],
  "do_not_overcorrect": "中文:提醒哪些非对称是合理的(矫正期偏好/个人薄弱强化),别机械追求 1:1 牺牲主线"
}}

约束:
- imbalances 只列 status=yellow/red 的;balanced 时 imbalances=[]
- 全中文;术语带括号:RIR(剩余次数)/ROM(动作幅度)/后束(三角肌后束)等
- suggested_exercise_ids 优先用现有动作 id(face_pull/seated_row/rdl/leg_curl 等),缺合适动作才标 "NEW:动作名"
- 别让平衡比凌驾增肌主线(do_not_overcorrect 必填)
"""


def balance_check(db: sqlite3.Connection, plan_data: dict,
                  model: str = "", effort: str = "") -> dict:
    """全身平衡体检:聚合客观容量比 → AI 解读失衡 + 纠正建议。返回 {profile, ai}。"""
    profile = _balance_profile(db, plan_data)
    if profile["total_logged_sets"] < 8:
        return {"profile": profile, "ai": {
            "overall_balance_grade": "balanced",
            "summary": "记录的训练组数太少(需要先多练几次、覆盖 Push/Pull/Legs)才能做平衡体检。",
            "data_sufficiency": "insufficient", "imbalances": [], "corrective_actions": [],
        }}
    def _sline(s):
        if s["status"] == "insufficient":
            return f"  {s['name']}: insufficient — {s.get('missing','')}  [证据:{s['evidence']}]"
        return (f"  {s['name']}: {s['value']} (={s['num_1rm']}/{s['den_1rm']}kg, 模态 {s['modality']}, "
                f"状态 {s['status']}) — {s['hint']}  [证据:{s['evidence']}]")
    strength_str = "\n".join(_sline(s) for s in profile["strength_ratios"])
    ratios_str = "\n".join(
        f"  {r['name']}: {r['value']}  (分子 {r['num_sets']} 组 / 分母 {r['den_sets']} 组, "
        f"健康区间 {r['good_range']}, 状态 {r['status']}) — {r['hint']}"
        for r in profile["ratios"]
    )
    buckets_str = json.dumps(profile["weekly_sets_by_bucket"], ensure_ascii=False)
    est1rm_str = json.dumps(profile["est_1rm"], ensure_ascii=False)
    prompt = BALANCE_PROMPT.format(
        literature=LITERATURE_REF, weeks=profile["weeks"],
        strength=strength_str, ratios=ratios_str, buckets=buckets_str, est1rm=est1rm_str,
        total_sets=profile["total_logged_sets"], data_suff=profile["data_sufficiency"],
    )
    try:
        resp = _ask(prompt, model=model, effort=effort)
    except Exception as e:
        return {"profile": profile, "ai": {"error": f"ai_call_failed: {e}"}}
    ai = _extract_json(resp)
    if not ai:
        ai = {"error": "json_parse_failed", "raw": (resp or "")[:500]}
    return {"profile": profile, "ai": ai}


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
