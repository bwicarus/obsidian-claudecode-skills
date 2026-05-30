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
    # 用户自定义视频列表覆盖(默认 plan.json 的 videos 不对/不够时,自己 paste 覆盖)
    db.execute("""
        CREATE TABLE IF NOT EXISTS fitness_video_override (
            exercise_id TEXT PRIMARY KEY,
            videos_json TEXT NOT NULL,  -- list of {"video_id": "xxx", "title": "xxx"}
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return db


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
    return render_template("fitness/log.html", plan=plan, day=day)


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
    """录入一组训练。
    body: {date, day_id, exercise_id, set_no, weight_kg, reps, note?}
    """
    username = _username()
    data = request.get_json(silent=True) or {}
    required = ("date", "day_id", "exercise_id", "set_no")
    for k in required:
        if k not in data:
            return jsonify({"ok": False, "error": f"missing {k}"}), 400
    db = _db(username)
    cur = db.execute(
        "INSERT INTO fitness_log (date, day_id, exercise_id, set_no, weight_kg, reps, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            data["date"], data["day_id"], data["exercise_id"], int(data["set_no"]),
            data.get("weight_kg"), data.get("reps"), data.get("note"),
        ),
    )
    db.commit()
    log_id = cur.lastrowid
    db.close()
    return jsonify({"ok": True, "id": log_id})


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
