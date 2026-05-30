"""健身页 Flask blueprint。/private/fitness/* 走现有 webapp 登录态。

routes:
  GET  /private/fitness/                 主页 + 本周总览 + 选今日训练
  GET  /private/fitness/log/<day_id>     当天训练录入(预填上次重量)
  GET  /private/fitness/history          历史记录 / 单动作进度曲线
  GET  /private/fitness/plan             计划详情(只读,看动作 + 视频)
  GET  /api/fitness/plan                 返回 plan JSON
  POST /api/fitness/log                  录入一组训练
  GET  /api/fitness/last/<exercise_id>   返回该动作最近 N 次记录(预填用)
  GET  /api/fitness/history              所有训练历史(图表/列表用)
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, g, jsonify, render_template,
    request, send_from_directory, session,
)

bp = Blueprint("fitness", __name__, url_prefix="/private/fitness")
api_bp = Blueprint("fitness_api", __name__, url_prefix="/api/fitness")

DATA_ROOT = Path(os.environ.get("WEBAPP_DATA", "/home/bwicarus/webapp/data"))
PLAN_PATH = Path(__file__).parent / "static" / "fitness-plan.json"


# ───────────────────────── DB ─────────────────────────
def _user_db_path(username: str) -> Path:
    user_dir = DATA_ROOT / "users" / username / "private"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / "fitness.db"


def _db(username: str) -> sqlite3.Connection:
    db = sqlite3.connect(str(_user_db_path(username)))
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS fitness_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            day_id TEXT NOT NULL,
            exercise_id TEXT NOT NULL,
            set_no INTEGER NOT NULL,
            weight_kg REAL,
            reps INTEGER,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_log_date ON fitness_log(date)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_log_ex ON fitness_log(exercise_id, date)")
    # upsert 用唯一约束:同一 (date, exercise_id, set_no) 只存一行,覆盖式更新
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_log_set "
        "ON fitness_log(date, exercise_id, set_no)"
    )
    # 用户自定义视频列表覆盖(默认 plan.json 的 videos 不对/不够时,自己 paste 覆盖)
    db.execute("""
        CREATE TABLE IF NOT EXISTS fitness_video_override (
            exercise_id TEXT PRIMARY KEY,
            videos_json TEXT NOT NULL,  -- list of {"video_id": "xxx", "title": "xxx"}
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # AI 调整后的动作 prescribed 覆盖(per-user,优先于 plan.json)
    db.execute("""
        CREATE TABLE IF NOT EXISTS fitness_exercise_override (
            exercise_id TEXT PRIMARY KEY,
            prescribed_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'ai',  -- 'ai' | 'manual'
            reasoning TEXT,
            change_summary TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # AI 训练分析(每次完成训练后)
    db.execute("""
        CREATE TABLE IF NOT EXISTS fitness_session_analysis (
            date TEXT NOT NULL,
            day_id TEXT NOT NULL,
            analysis_json TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, day_id)
        )
    """)
    # 用户级 fitness 设置(AI 模型 / effort / 自动分析开关 等)
    db.execute("""
        CREATE TABLE IF NOT EXISTS fitness_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return db


# fitness_settings 默认值
DEFAULT_FITNESS_SETTINGS = {
    "ai_model":                   "opus",   # opus | sonnet | haiku
    "ai_effort":                  "max",    # low | medium | high | xhigh | max
    "auto_analyze_after_finish":  "1",      # 完成训练后自动调 AI 分析
    "auto_suggest_after_analyze": "1",      # 分析后自动调 suggest 下次计划
    "deload_check_weeks":         "6",      # 多少周提示 deload 一次
}


def _get_settings(db) -> dict:
    rows = db.execute("SELECT key, value FROM fitness_settings").fetchall()
    out = dict(DEFAULT_FITNESS_SETTINGS)
    for r in rows:
        out[r["key"]] = r["value"]
    return out


def _set_setting(db, key: str, value: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO fitness_settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value),
    )


def _exercise_prescribed_with_override(db, plan, ex_id):
    """读 plan.prescribed + 用户 override(若存在 override 优先)。"""
    row = db.execute(
        "SELECT prescribed_json FROM fitness_exercise_override WHERE exercise_id=?",
        (ex_id,),
    ).fetchone()
    if row:
        try:
            return json.loads(row["prescribed_json"])
        except Exception:
            pass
    # fallback plan.json
    for d in plan.get("days", []):
        for ex in d.get("exercises", []):
            if ex["id"] == ex_id:
                return ex.get("prescribed") or {}
    return {}


def _videos_for(db, exercise_id: str, plan: dict) -> list[dict]:
    """合并 user override + plan 默认。优先 override(用户自定义)。"""
    row = db.execute(
        "SELECT videos_json FROM fitness_video_override WHERE exercise_id = ?",
        (exercise_id,)
    ).fetchone()
    if row:
        try:
            return json.loads(row["videos_json"])
        except Exception:
            pass
    # 回落 plan.json 默认
    for d in plan.get("days", []):
        for ex in d.get("exercises", []):
            if ex["id"] == exercise_id:
                return ex.get("videos", [])
    return []


# ───────────────────────── helpers ─────────────────────────
def _username() -> str:
    u = session.get("username")
    if not u:
        abort(401)
    return u


def _load_plan() -> dict:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


# ───────────────────────── pages ─────────────────────────
@bp.route("/")
def home():
    plan = _load_plan()
    username = _username()
    db = _db(username)
    # 最近 30 天训练日数
    rows = db.execute(
        "SELECT date, day_id, COUNT(*) AS n FROM fitness_log "
        "WHERE date >= date('now', '-30 day') GROUP BY date ORDER BY date DESC"
    ).fetchall()
    db.close()
    recent_workouts = [dict(r) for r in rows]
    return render_template("fitness/home.html", plan=plan, recent=recent_workouts)


@bp.route("/log/<day_id>")
def log_page(day_id: str):
    plan = _load_plan()
    day = next((d for d in plan["days"] if d["id"] == day_id), None)
    if not day:
        abort(404)
    # 估算训练总时间(每组做组 ~30s + 组间 rest_seconds + 动作切换 60s)
    SET_TIME = 30
    SWITCH_TIME = 60
    est_sec = 0
    total_sets = 0
    for ex in day["exercises"]:
        p = ex.get("prescribed", {}) or {}
        s = int(p.get("sets") or 3)
        rest = int(p.get("rest_seconds") or 120)
        est_sec += s * SET_TIME + max(0, s - 1) * rest + SWITCH_TIME
        total_sets += s
    return render_template(
        "fitness/log.html", plan=plan, day=day,
        est_minutes=round(est_sec / 60),
        total_sets=total_sets,
    )


@bp.route("/history")
def history_page():
    plan = _load_plan()
    return render_template("fitness/history.html", plan=plan)


@bp.route("/plan")
def plan_page():
    plan = _load_plan()
    return render_template("fitness/plan.html", plan=plan)


# ───────────────────────── API ─────────────────────────
@api_bp.route("/plan")
def api_plan():
    return jsonify(_load_plan())


@api_bp.route("/log", methods=["POST"])
def api_log():
    """录入/更新一组训练 (upsert: 同 date+ex+set_no 的覆盖)。
    body: {date, day_id, exercise_id, set_no, weight_kg?, reps?, note?}
    weight_kg / reps 可为 null,允许只填一半(autosave 场景)。
    """
    username = _username()
    data = request.get_json(silent=True) or {}
    required = ("date", "day_id", "exercise_id", "set_no")
    for k in required:
        if k not in data:
            return jsonify({"ok": False, "error": f"missing {k}"}), 400
    db = _db(username)
    db.execute(
        "INSERT INTO fitness_log (date, day_id, exercise_id, set_no, weight_kg, reps, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(date, exercise_id, set_no) DO UPDATE SET "
        "  weight_kg = excluded.weight_kg, "
        "  reps      = excluded.reps, "
        "  note      = excluded.note, "
        "  day_id    = excluded.day_id",
        (
            data["date"], data["day_id"], data["exercise_id"], int(data["set_no"]),
            data.get("weight_kg"), data.get("reps"), data.get("note"),
        ),
    )
    db.commit()
    row = db.execute(
        "SELECT id FROM fitness_log WHERE date=? AND exercise_id=? AND set_no=?",
        (data["date"], data["exercise_id"], int(data["set_no"])),
    ).fetchone()
    db.close()
    return jsonify({"ok": True, "id": row["id"] if row else None})


@api_bp.route("/today_sets/<exercise_id>")
def api_today_sets(exercise_id: str):
    """返回指定日期(默认今天)该动作所有已存的组(刷新后恢复用)。

    query: ?date=YYYY-MM-DD(可选,默认今天)
    """
    username = _username()
    date = request.args.get("date") or time.strftime("%Y-%m-%d")
    db = _db(username)
    rows = db.execute(
        "SELECT id, set_no, weight_kg, reps, note FROM fitness_log "
        "WHERE exercise_id=? AND date=? ORDER BY set_no",
        (exercise_id, date),
    ).fetchall()
    db.close()
    return jsonify({"ok": True, "date": date, "sets": [dict(r) for r in rows]})


# ───────────────────────── AI 教练 ─────────────────────────
@api_bp.route("/settings", methods=["GET"])
def api_settings_get():
    username = _username()
    db = _db(username)
    s = _get_settings(db)
    db.close()
    return jsonify({"ok": True, "settings": s, "defaults": DEFAULT_FITNESS_SETTINGS})


@api_bp.route("/settings", methods=["POST"])
def api_settings_set():
    """body: {key1: value1, key2: value2, ...}"""
    username = _username()
    data = request.get_json(silent=True) or {}
    db = _db(username)
    valid = set(DEFAULT_FITNESS_SETTINGS.keys())
    saved = {}
    for k, v in data.items():
        if k in valid:
            _set_setting(db, k, str(v))
            saved[k] = str(v)
    db.commit()
    db.close()
    return jsonify({"ok": True, "saved": saved})


@api_bp.route("/ai/suggest_plan", methods=["POST"])
def api_ai_suggest_plan():
    """让 AI 看历史 + 上次 analysis 给出本日所有动作的 prescribed 建议。

    body: {day_id: "push" | "pull" | "legs"}
    返回: AI 输出的 JSON (overall_reasoning + exercises[].{id, prescribed, change_summary, reasoning})
    """
    username = _username()
    data = request.get_json(silent=True) or {}
    day_id = data.get("day_id")
    if not day_id:
        return jsonify({"ok": False, "error": "missing day_id"}), 400
    db = _db(username)
    try:
        from fitness_coach import suggest_plan
        plan = _load_plan()
        s = _get_settings(db)
        r = suggest_plan(db, day_id, plan,
                         model=s["ai_model"], effort=s["ai_effort"])
    finally:
        db.close()
    if "error" in r:
        return jsonify({"ok": False, **r}), 500
    return jsonify({"ok": True, **r})


@api_bp.route("/ai/analyze_session", methods=["POST"])
def api_ai_analyze_session():
    """AI 分析一次完成的训练。

    body: {date: "YYYY-MM-DD", day_id: "push"}
    """
    username = _username()
    data = request.get_json(silent=True) or {}
    date = data.get("date") or time.strftime("%Y-%m-%d")
    day_id = data.get("day_id")
    if not day_id:
        return jsonify({"ok": False, "error": "missing day_id"}), 400
    db = _db(username)
    try:
        from fitness_coach import analyze_session
        plan = _load_plan()
        s = _get_settings(db)
        r = analyze_session(db, date, day_id, plan,
                            model=s["ai_model"], effort=s["ai_effort"])
        if "error" not in r:
            # 持久化分析(下次 suggest_plan 用)
            db.execute(
                "INSERT OR REPLACE INTO fitness_session_analysis "
                "(date, day_id, analysis_json) VALUES (?, ?, ?)",
                (date, day_id, json.dumps(r, ensure_ascii=False)),
            )
            db.commit()
    finally:
        db.close()
    if "error" in r:
        return jsonify({"ok": False, **r}), 500
    return jsonify({"ok": True, **r})


@api_bp.route("/exercise_override/<exercise_id>", methods=["GET"])
def api_exercise_override_get(exercise_id: str):
    username = _username()
    db = _db(username)
    row = db.execute(
        "SELECT prescribed_json, source, reasoning, change_summary, updated_at "
        "FROM fitness_exercise_override WHERE exercise_id=?",
        (exercise_id,),
    ).fetchone()
    db.close()
    if not row:
        return jsonify({"ok": True, "override": None})
    return jsonify({"ok": True, "override": {
        "prescribed": json.loads(row["prescribed_json"]),
        "source": row["source"],
        "reasoning": row["reasoning"],
        "change_summary": row["change_summary"],
        "updated_at": row["updated_at"],
    }})


@api_bp.route("/exercise_override/<exercise_id>", methods=["POST"])
def api_exercise_override_set(exercise_id: str):
    """接受 AI 建议(或手动)写入 override。
    body: {prescribed: {...}, source?: "ai"|"manual", reasoning?, change_summary?}
    """
    username = _username()
    data = request.get_json(silent=True) or {}
    pres = data.get("prescribed")
    if not isinstance(pres, dict):
        return jsonify({"ok": False, "error": "missing prescribed"}), 400
    db = _db(username)
    db.execute(
        "INSERT OR REPLACE INTO fitness_exercise_override "
        "(exercise_id, prescribed_json, source, reasoning, change_summary, updated_at) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        (
            exercise_id, json.dumps(pres, ensure_ascii=False),
            data.get("source", "ai"),
            data.get("reasoning"),
            data.get("change_summary"),
        ),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True})


@api_bp.route("/exercise_override/<exercise_id>", methods=["DELETE"])
def api_exercise_override_delete(exercise_id: str):
    username = _username()
    db = _db(username)
    db.execute("DELETE FROM fitness_exercise_override WHERE exercise_id=?", (exercise_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@api_bp.route("/exercise_overrides")
def api_exercise_overrides_list():
    """列出当前用户所有 override(给 UI 显示哪些动作被 AI 调整过)。"""
    username = _username()
    db = _db(username)
    rows = db.execute(
        "SELECT exercise_id, prescribed_json, source, reasoning, change_summary, updated_at "
        "FROM fitness_exercise_override ORDER BY updated_at DESC"
    ).fetchall()
    db.close()
    return jsonify({"ok": True, "overrides": [
        {
            "exercise_id": r["exercise_id"],
            "prescribed": json.loads(r["prescribed_json"]),
            "source": r["source"],
            "reasoning": r["reasoning"],
            "change_summary": r["change_summary"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]})


@api_bp.route("/workout_meta")
def api_workout_meta():
    """返回某 date+day_id 训练的元信息(起点时间 + 已存组数)。

    query: ?date=YYYY-MM-DD&day_id=push
    """
    username = _username()
    date = request.args.get("date") or time.strftime("%Y-%m-%d")
    day_id = request.args.get("day_id", "")
    db = _db(username)
    row = db.execute(
        "SELECT MIN(created_at) AS first_at, COUNT(*) AS cnt "
        "FROM fitness_log WHERE date=? AND day_id=?",
        (date, day_id),
    ).fetchone()
    db.close()
    return jsonify({
        "ok": True,
        "date": date,
        "day_id": day_id,
        "first_at": row["first_at"],   # SQLite 默认 UTC 字符串(无 TZ 后缀)
        "count": row["cnt"] or 0,
    })


@api_bp.route("/log/<int:log_id>", methods=["DELETE"])
def api_log_delete(log_id: int):
    username = _username()
    db = _db(username)
    db.execute("DELETE FROM fitness_log WHERE id = ?", (log_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@api_bp.route("/last/<exercise_id>")
def api_last(exercise_id: str):
    """返回该动作最近一次完整记录(预填用)。"""
    username = _username()
    db = _db(username)
    last_row = db.execute(
        "SELECT date FROM fitness_log WHERE exercise_id = ? "
        "ORDER BY date DESC LIMIT 1", (exercise_id,)
    ).fetchone()
    if not last_row:
        db.close()
        return jsonify({"ok": True, "sets": []})
    rows = db.execute(
        "SELECT id, set_no, weight_kg, reps, note FROM fitness_log "
        "WHERE exercise_id = ? AND date = ? ORDER BY set_no",
        (exercise_id, last_row["date"]),
    ).fetchall()
    db.close()
    return jsonify({
        "ok": True,
        "date": last_row["date"],
        "sets": [dict(r) for r in rows],
    })


# ───────────────────────── recommendation ─────────────────────────
def _round_step(weight: float, step: float) -> float:
    """按 weight_step_kg 取整(避免 1.234 kg 这种)。"""
    if step <= 0:
        return round(weight, 1)
    n = round(weight / step)
    return round(n * step, 2)


def _exercise_spec(plan: dict, exercise_id: str, db=None) -> dict | None:
    """找到动作的 dict。若 db 提供,prescribed 优先用 override。"""
    for d in plan.get("days", []):
        for ex in d.get("exercises", []):
            if ex["id"] == exercise_id:
                out = dict(ex)
                if db is not None:
                    row = db.execute(
                        "SELECT prescribed_json FROM fitness_exercise_override WHERE exercise_id=?",
                        (exercise_id,),
                    ).fetchone()
                    if row:
                        try:
                            out["prescribed"] = json.loads(row["prescribed_json"])
                        except Exception:
                            pass
                return out
    return None


def _compute_recommendation(spec: dict, last_sets: list[dict]) -> dict:
    """Double Progression 算法。

    输入:
      spec: plan.json 里该动作的字典(含 prescribed 字段)
      last_sets: 上次该动作的所有组,e.g. [{set_no, weight_kg, reps}, ...]
    输出 dict:
      {weight_kg, target_reps, target_sets, rir_target, rest_seconds, reason, source}
    """
    p = spec.get("prescribed") or {}
    rep_low, rep_high = (p.get("rep_range") or [8, 12])
    target_sets = p.get("sets") or 3
    rir = p.get("rir_target") or 2
    rest = p.get("rest_seconds") or 120
    start_w = p.get("start_weight_kg") or 0
    step = p.get("weight_step_kg") or 1.0

    base = {
        "target_sets": target_sets,
        "rir_target": rir,
        "rest_seconds": rest,
    }

    if not last_sets:
        return {
            **base,
            "weight_kg": start_w,
            "target_reps": rep_low,
            "reason": f"首次,从起步重量 {start_w} kg × 下限 {rep_low} reps 开始",
            "source": "initial",
        }

    weights = [s["weight_kg"] for s in last_sets if s.get("weight_kg") is not None]
    reps_list = [s["reps"] for s in last_sets if s.get("reps") is not None]
    if not weights or not reps_list:
        return {
            **base,
            "weight_kg": start_w,
            "target_reps": rep_low,
            "reason": "上次数据不全,回到起步",
            "source": "fallback",
        }

    # 上次主重量:最常出现的 weight(单组热身放轻不影响推荐)
    last_w = max(set(weights), key=weights.count)
    # 该重量下的所有 reps
    reps_at_w = [s["reps"] for s in last_sets
                 if s.get("weight_kg") == last_w and s.get("reps") is not None]
    min_reps = min(reps_at_w) if reps_at_w else min(reps_list)
    max_reps = max(reps_at_w) if reps_at_w else max(reps_list)

    # ── 自重动作特殊处理 ──
    bodyweight = (start_w == 0) and (last_w == 0 or last_w is None)
    if bodyweight:
        if min_reps >= rep_high:
            # 自重达上限 → 提示加负重(或维持自重 +reps)
            if step > 0:
                return {
                    **base,
                    "weight_kg": 0,
                    "target_reps": rep_high,
                    "reason": f"自重 {min_reps} reps ≥{rep_high},可挂 {step} kg 负重(或维持自重继续加 reps)",
                    "source": "bodyweight_progression",
                }
            return {
                **base,
                "weight_kg": 0,
                "target_reps": min_reps + 1,
                "reason": f"自重已能 {min_reps} reps,目标 +1",
                "source": "bodyweight_progression",
            }
        if min_reps < rep_low:
            return {
                **base,
                "weight_kg": 0,
                "target_reps": max(rep_low - 2, 3),
                "reason": f"自重做不到 {rep_low} reps,试 negatives(5s 慢下降)或脚踩凳助力",
                "source": "bodyweight_regress",
            }
        target = min(min_reps + 1, rep_high)
        return {
            **base,
            "weight_kg": 0,
            "target_reps": target,
            "reason": f"自重区间内,上次 {min_reps}-{max_reps} reps → 目标 {target}",
            "source": "bodyweight_rep_progress",
        }
    # 所有组到上限 → 加重
    if min_reps >= rep_high:
        new_w = _round_step(last_w + step, step)
        return {
            **base,
            "weight_kg": new_w,
            "target_reps": rep_low,
            "reason": f"上次 {last_w} kg 全部 ≥{rep_high} reps → 加 {step} kg,目标回 {rep_low} reps",
            "source": "progress",
        }
    # 任一组没到下限 → 减 10%
    if min_reps < rep_low:
        new_w = _round_step(last_w * 0.9, step)
        if new_w >= last_w:   # step 较大时避免不变
            new_w = _round_step(last_w - step, step)
        return {
            **base,
            "weight_kg": max(new_w, 0),
            "target_reps": rep_low,
            "reason": f"上次 {last_w} kg 有组只做了 {min_reps} reps(<{rep_low})→ 减约 10% 巩固姿势",
            "source": "deload",
        }
    # 区间内 → 同重量,目标 +1 rep
    target = min(min_reps + 1, rep_high)
    return {
        **base,
        "weight_kg": last_w,
        "target_reps": target,
        "reason": f"上次 {last_w} kg × {min_reps}-{max_reps} reps,区间内 → 同重量,目标 {target} reps",
        "source": "rep_progress",
    }


@api_bp.route("/recommend/<exercise_id>")
def api_recommend(exercise_id: str):
    """基于历史 + plan.prescribed 给出本次推荐。"""
    username = _username()
    plan = _load_plan()
    db = _db(username)
    spec = _exercise_spec(plan, exercise_id, db=db)
    if not spec:
        db.close()
        return jsonify({"ok": False, "error": "unknown exercise_id"}), 404
    last_row = db.execute(
        "SELECT date FROM fitness_log WHERE exercise_id = ? "
        "ORDER BY date DESC LIMIT 1", (exercise_id,)
    ).fetchone()
    last_sets: list[dict] = []
    last_date = None
    if last_row:
        last_date = last_row["date"]
        rows = db.execute(
            "SELECT set_no, weight_kg, reps FROM fitness_log "
            "WHERE exercise_id = ? AND date = ? ORDER BY set_no",
            (exercise_id, last_date),
        ).fetchall()
        last_sets = [dict(r) for r in rows]
    db.close()

    rec = _compute_recommendation(spec, last_sets)
    return jsonify({
        "ok": True,
        "exercise_id": exercise_id,
        "exercise_name": spec.get("name"),
        "evidence_note": spec.get("evidence_note", ""),
        "last_date": last_date,
        "last_sets": last_sets,
        "recommendation": rec,
    })


@api_bp.route("/history")
def api_history():
    """所有训练历史(给图表 / 列表用)。
    可选参数: ?exercise_id=xxx 只取该动作 / ?days=N 限定天数
    """
    username = _username()
    db = _db(username)
    sql = "SELECT id, date, day_id, exercise_id, set_no, weight_kg, reps, note FROM fitness_log WHERE 1=1"
    params: list = []
    ex_id = request.args.get("exercise_id")
    if ex_id:
        sql += " AND exercise_id = ?"
        params.append(ex_id)
    days = request.args.get("days")
    if days:
        try:
            d = int(days)
            sql += f" AND date >= date('now', '-{d} day')"
        except ValueError:
            pass
    sql += " ORDER BY date DESC, set_no ASC"
    rows = db.execute(sql, params).fetchall()
    db.close()
    return jsonify({"ok": True, "rows": [dict(r) for r in rows]})


@api_bp.route("/videos/<exercise_id>", methods=["GET"])
def api_videos_get(exercise_id: str):
    """合并 user override + plan 默认的视频列表。"""
    username = _username()
    db = _db(username)
    plan = _load_plan()
    vids = _videos_for(db, exercise_id, plan)
    db.close()
    return jsonify({"ok": True, "videos": vids})


@api_bp.route("/videos/<exercise_id>", methods=["POST"])
def api_videos_set(exercise_id: str):
    """完整替换该动作的视频列表(覆盖 plan 默认)。
    body: {videos: [{video_id, title?}, ...]}
    """
    username = _username()
    data = request.get_json(silent=True) or {}
    videos = data.get("videos") or []
    # 验证: 每个元素必须含 video_id
    clean = []
    for v in videos:
        if isinstance(v, str):
            clean.append({"video_id": v, "title": ""})
        elif isinstance(v, dict) and v.get("video_id"):
            clean.append({"video_id": v["video_id"], "title": v.get("title", "")})
    db = _db(username)
    db.execute(
        "INSERT INTO fitness_video_override (exercise_id, videos_json, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(exercise_id) DO UPDATE SET videos_json=excluded.videos_json, updated_at=CURRENT_TIMESTAMP",
        (exercise_id, json.dumps(clean, ensure_ascii=False))
    )
    db.commit()
    db.close()
    return jsonify({"ok": True, "videos": clean})


@api_bp.route("/videos/<exercise_id>/reset", methods=["POST"])
def api_videos_reset(exercise_id: str):
    """删除 override → 回落到 plan 默认。"""
    username = _username()
    db = _db(username)
    db.execute("DELETE FROM fitness_video_override WHERE exercise_id = ?", (exercise_id,))
    db.commit()
    db.close()
    return jsonify({"ok": True})


# ───────────────────────── YouTube 字幕 (Gemini Flash 翻译) ─────────────────────
@api_bp.route("/subtitles/<video_id>")
def api_subtitles(video_id: str):
    """拉 + 翻 YouTube 字幕(全局缓存,首次 ~5-30 秒,之后秒出)。

    query:
      ?source=auto  YT 自带 caption(默认,快+免费)
      ?source=stt   Cloud Speech-to-Text 高质量(慢 ~20s+,烧赠金,完整句子)
      ?force=1      强制重新拉 + 翻译(忽略 cache)
    """
    _username()
    from youtube_subtitles import get_or_translate
    force = request.args.get("force") == "1"
    source = request.args.get("source", "auto")
    r = get_or_translate(video_id, target_lang="zh", source=source, force=force)
    code = 200 if r.get("status") == "ready" else 500
    return jsonify({"ok": r.get("status") == "ready", **r}), code


@api_bp.route("/subtitles/<video_id>/status")
def api_subtitles_status(video_id: str):
    """只查 cache,不触发翻译。前端可用来 prefetch 显示「已缓存」标记。"""
    _username()
    from youtube_subtitles import has_cached
    source = request.args.get("source", "auto")
    return jsonify({"ok": True, "cached": has_cached(video_id, source=source)})


@api_bp.route("/today")
def api_today():
    """今日 + 今日已记录的训练(主页用)。"""
    username = _username()
    today = time.strftime("%Y-%m-%d")
    db = _db(username)
    rows = db.execute(
        "SELECT id, day_id, exercise_id, set_no, weight_kg, reps FROM fitness_log "
        "WHERE date = ? ORDER BY id ASC",
        (today,),
    ).fetchall()
    db.close()
    return jsonify({"ok": True, "today": today, "logs": [dict(r) for r in rows]})


def register(app):
    """供 app.py 调:app.register_blueprint(...)。"""
    app.register_blueprint(bp)
    app.register_blueprint(api_bp)
