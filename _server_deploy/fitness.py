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
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
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
    # AI 后台 job(异步:点一下立即返回 job_id,AI 在服务器线程跑,结果落库;
    # 关页面/刷新都不影响,回来轮询拿结果)
    db.execute("""
        CREATE TABLE IF NOT EXISTS fitness_ai_job (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,            -- suggest_plan | analyze_session
            day_id TEXT,
            date TEXT,
            status TEXT NOT NULL,          -- running | done | error
            result_json TEXT,
            error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_aijob_kind_day "
               "ON fitness_ai_job(kind, day_id, status)")
    # 教练复盘对话(一次训练 = 一个会话, 按 (date, day_id) 标识;一行一条消息)
    db.execute("""
        CREATE TABLE IF NOT EXISTS fitness_coach_chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            day_id TEXT NOT NULL,
            role TEXT NOT NULL,            -- user | assistant
            content TEXT NOT NULL,
            proposal_json TEXT,            -- assistant 轮带的 prescribed 调整提案(可空)
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_coachchat_session "
               "ON fitness_coach_chat(date, day_id, id)")
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
    recent_workouts = [dict(r) for r in rows]
    # 推荐今天练哪天:每个 plan day 取最近一次训练日期,选「最久没练」的那天
    # (从没练过的优先)。轮转 PPL 自然得到 push→pull→legs 循环。
    last_by_day = {}
    for r in db.execute(
        "SELECT day_id, MAX(date) AS last FROM fitness_log GROUP BY day_id"
    ).fetchall():
        last_by_day[r["day_id"]] = r["last"]
    db.close()
    recommended_day = None
    if plan.get("days"):
        # 从没练过的 day 优先;否则 last 最早的
        never = [d["id"] for d in plan["days"] if d["id"] not in last_by_day]
        if never:
            recommended_day = never[0]
        else:
            recommended_day = min(plan["days"], key=lambda d: last_by_day.get(d["id"], ""))["id"]
    return render_template("fitness/home.html", plan=plan, recent=recent_workouts,
                           recommended_day=recommended_day, last_by_day=last_by_day)


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


@bp.route("/body")
def body_page():
    """3D 肌群强弱图(Three.js;数据来自 /api/fitness/balance_profile)。"""
    return render_template("fitness/body.html")


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


# ───────────────────────── AI 后台 job 基础设施 ─────────────────────────
# 异步:HTTP 立即返回 job_id,AI 在 daemon 线程里跑(实际算力在服务器),
# 结果落 fitness_ai_job 表。前端关页面/刷新都不影响,回来轮询拿结果。
# 参照 qa_browser 的 card-update job + pdf_reader 的 snippets-to-async 模式。

JOB_STALE_SECONDS = 900   # running 超过 15 分钟视为僵死(进程重启等)


def _job_set(db, job_id: str, status: str, result=None, error: str = None):
    db.execute(
        "UPDATE fitness_ai_job SET status=?, result_json=?, error=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status,
         json.dumps(result, ensure_ascii=False) if result is not None else None,
         error, job_id),
    )
    db.commit()


def _run_ai_job(username: str, job_id: str, kind: str, day_id: str, date: str):
    """daemon 线程:跑 AI + 写结果。自己开 DB 连接(sqlite 跨线程不共享)。"""
    db = _db(username)
    try:
        from fitness_coach import suggest_plan, analyze_session, coach_chat, balance_check
        plan = _load_plan()
        s = _get_settings(db)
        if kind == "suggest_plan":
            r = suggest_plan(db, day_id, plan, model=s["ai_model"], effort=s["ai_effort"])
        elif kind == "balance_check":
            r = balance_check(db, plan, model=s["ai_model"], effort=s["ai_effort"])
        elif kind == "coach_chat":
            # 读该会话全量消息(含刚 endpoint 插入的最新 user 条)→ 多轮对话
            msgs = [
                {"role": row["role"], "content": row["content"]}
                for row in db.execute(
                    "SELECT role, content FROM fitness_coach_chat "
                    "WHERE date=? AND day_id=? ORDER BY id", (date, day_id),
                ).fetchall()
            ]
            r = coach_chat(db, date, day_id, plan, msgs,
                           model=s["ai_model"], effort=s["ai_effort"])
            if "error" not in r:
                # 落 assistant 回复(带 proposal)
                prop = r.get("proposal")
                db.execute(
                    "INSERT INTO fitness_coach_chat (date, day_id, role, content, proposal_json) "
                    "VALUES (?, ?, 'assistant', ?, ?)",
                    (date, day_id, r.get("reply", ""),
                     json.dumps(prop, ensure_ascii=False) if prop else None),
                )
                db.commit()
        else:  # analyze_session
            r = analyze_session(db, date, day_id, plan,
                                model=s["ai_model"], effort=s["ai_effort"])
            if "error" not in r:
                db.execute(
                    "INSERT OR REPLACE INTO fitness_session_analysis "
                    "(date, day_id, analysis_json) VALUES (?, ?, ?)",
                    (date, day_id, json.dumps(r, ensure_ascii=False)),
                )
                db.commit()
        if "error" in r:
            _job_set(db, job_id, "error", result=r, error=str(r.get("error"))[:500])
        else:
            _job_set(db, job_id, "done", result=r)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[fitness] ai job {kind} exception: {e}\n{tb}", flush=True)
        try:
            _job_set(db, job_id, "error",
                     result={"error": f"{type(e).__name__}: {e}", "traceback": tb[:1500]},
                     error=f"{type(e).__name__}: {e}"[:500])
        except Exception:
            pass
    finally:
        db.close()


def _start_ai_job(username: str, kind: str, day_id: str, date: str) -> dict:
    """创建 job + 起线程。同 (kind, day_id[, date]) 已有 running 则复用,不重复跑。"""
    db = _db(username)
    try:
        # dedup:已有 running 的同类 job 就复用。coach_chat **不 dedup**(每轮消息不同,
        # 否则第二句会拿到第一句的 job → 同一会话连发会串)。
        row = None
        if kind == "coach_chat":
            row = None
        elif kind == "suggest_plan":
            row = db.execute(
                "SELECT * FROM fitness_ai_job WHERE kind=? AND day_id=? AND status='running' "
                "ORDER BY created_at DESC LIMIT 1", (kind, day_id),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM fitness_ai_job WHERE kind=? AND day_id=? AND date=? AND status='running' "
                "ORDER BY created_at DESC LIMIT 1", (kind, day_id, date),
            ).fetchone()
        # 复用前先判僵死:服务器重启会杀掉 daemon 线程但 DB 行仍 running →
        # 这种僵尸会卡住后续同类请求达 15min。stale 的标 error 不复用,让新 job 起。
        if row and _maybe_mark_stale(db, row) != "running":
            row = None
        if row:
            return {"job_id": row["id"], "status": "running", "reused": True}
        job_id = uuid.uuid4().hex[:16]
        db.execute(
            "INSERT INTO fitness_ai_job (id, kind, day_id, date, status) "
            "VALUES (?, ?, ?, ?, 'running')", (job_id, kind, day_id, date),
        )
        db.commit()
    finally:
        db.close()
    t = threading.Thread(target=_run_ai_job, args=(username, job_id, kind, day_id, date),
                         daemon=True)
    t.start()
    return {"job_id": job_id, "status": "running", "reused": False}


def _job_row_to_dict(row) -> dict:
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except Exception:
            result = None
    return {
        "id": row["id"], "kind": row["kind"], "day_id": row["day_id"],
        "date": row["date"], "status": row["status"], "error": row["error"],
        "result": result, "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _maybe_mark_stale(db, row) -> str:
    """running 太久(进程可能重启过)→ 标 error,返回最新 status。"""
    if row["status"] != "running":
        return row["status"]
    try:
        upd = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - upd).total_seconds()
        if age > JOB_STALE_SECONDS:
            _job_set(db, row["id"], "error",
                     result={"error": "job 超时(服务器可能重启过),请重试"},
                     error="stale_timeout")
            return "error"
    except Exception:
        pass
    return "running"


@api_bp.route("/ai/suggest_plan", methods=["POST"])
def api_ai_suggest_plan():
    """启动「调整本日计划」AI job(异步)。body: {day_id}。返回 {job_id, status}。"""
    username = _username()
    data = request.get_json(silent=True) or {}
    day_id = data.get("day_id")
    if not day_id:
        return jsonify({"ok": False, "error": "missing day_id"}), 400
    r = _start_ai_job(username, "suggest_plan", day_id, "")
    return jsonify({"ok": True, **r})


@api_bp.route("/ai/analyze_session", methods=["POST"])
def api_ai_analyze_session():
    """启动「分析本次训练」AI job(异步)。body: {date, day_id}。返回 {job_id, status}。"""
    username = _username()
    data = request.get_json(silent=True) or {}
    date = data.get("date") or time.strftime("%Y-%m-%d")
    day_id = data.get("day_id")
    if not day_id:
        return jsonify({"ok": False, "error": "missing day_id"}), 400
    r = _start_ai_job(username, "analyze_session", day_id, date)
    return jsonify({"ok": True, **r})


@api_bp.route("/balance_profile")
def api_balance_profile():
    """同步(无 AI,秒返回)的客观平衡画像 + 每肌群强弱状态,给 3D 肌群图上色。"""
    username = _username()
    db = _db(username)
    try:
        from fitness_coach import _balance_profile, _muscle_status
        plan = _load_plan()
        profile = _balance_profile(db, plan)
        muscles = _muscle_status(profile)
    finally:
        db.close()
    return jsonify({"ok": True, "profile": profile, "muscles": muscles})


@api_bp.route("/ai/balance_check", methods=["POST"])
def api_ai_balance_check():
    """启动「全身平衡体检」AI job(异步,跨所有训练日聚合)。返回 {job_id, status}。"""
    username = _username()
    r = _start_ai_job(username, "balance_check", "", "")
    return jsonify({"ok": True, **r})


@api_bp.route("/ai/job/<job_id>")
def api_ai_job(job_id: str):
    """轮询 job 状态/结果。status: running | done | error。"""
    username = _username()
    db = _db(username)
    row = db.execute("SELECT * FROM fitness_ai_job WHERE id=?", (job_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"ok": False, "error": "job not found"}), 404
    _maybe_mark_stale(db, row)
    row = db.execute("SELECT * FROM fitness_ai_job WHERE id=?", (job_id,)).fetchone()
    db.close()
    return jsonify({"ok": True, "job": _job_row_to_dict(row)})


@api_bp.route("/ai/jobs")
def api_ai_jobs():
    """列最近 job(刷新/重进页面恢复用)。query: ?day_id=&kind=&date=&limit=。"""
    username = _username()
    db = _db(username)
    sql = "SELECT * FROM fitness_ai_job WHERE 1=1"
    params = []
    for k in ("kind", "day_id", "date"):
        v = request.args.get(k)
        if v:
            sql += f" AND {k}=?"
            params.append(v)
    limit = min(int(request.args.get("limit", 20)), 50)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    # 顺手把僵死的标 error
    for row in rows:
        if row["status"] == "running":
            _maybe_mark_stale(db, row)
    rows = db.execute(sql, params).fetchall()
    db.close()
    return jsonify({"ok": True, "jobs": [_job_row_to_dict(r) for r in rows]})


# ───────────────────────── 教练复盘对话 ─────────────────────────
@api_bp.route("/ai/coach_chat", methods=["POST"])
def api_coach_chat():
    """发一条复盘消息。body: {date, day_id, message}。
    立即写入 user 消息 + 启动 coach_chat job(异步,不 dedup),返回 {job_id}。"""
    username = _username()
    data = request.get_json(silent=True) or {}
    day_id = data.get("day_id")
    date = data.get("date") or time.strftime("%Y-%m-%d")
    message = (data.get("message") or "").strip()
    if not day_id or not message:
        return jsonify({"ok": False, "error": "missing day_id or message"}), 400
    db = _db(username)
    try:
        db.execute(
            "INSERT INTO fitness_coach_chat (date, day_id, role, content) "
            "VALUES (?, ?, 'user', ?)", (date, day_id, message),
        )
        db.commit()
    finally:
        db.close()
    r = _start_ai_job(username, "coach_chat", day_id, date)
    return jsonify({"ok": True, **r})


@api_bp.route("/ai/coach_chat/messages")
def api_coach_chat_messages():
    """取某次训练(date, day_id)的全部复盘消息(刷新/换设备恢复对话)。"""
    username = _username()
    day_id = request.args.get("day_id")
    date = request.args.get("date") or time.strftime("%Y-%m-%d")
    if not day_id:
        return jsonify({"ok": False, "error": "missing day_id"}), 400
    db = _db(username)
    rows = db.execute(
        "SELECT role, content, proposal_json, created_at FROM fitness_coach_chat "
        "WHERE date=? AND day_id=? ORDER BY id", (date, day_id),
    ).fetchall()
    db.close()
    msgs = [{
        "role": r["role"], "content": r["content"],
        "proposal": json.loads(r["proposal_json"]) if r["proposal_json"] else None,
        "created_at": r["created_at"],
    } for r in rows]
    return jsonify({"ok": True, "messages": msgs})


@api_bp.route("/ai/coach_chat/apply", methods=["POST"])
def api_coach_chat_apply():
    """把对话产出的 proposal 落成 exercise_override(下次该日计划生效)。
    body: {proposal: {exercises:[{id, prescribed, change_summary, reasoning}]}}。"""
    username = _username()
    data = request.get_json(silent=True) or {}
    proposal = data.get("proposal") or {}
    exercises = proposal.get("exercises") or []
    if not exercises:
        return jsonify({"ok": False, "error": "proposal 无 exercises"}), 400
    db = _db(username)
    n = 0
    try:
        for ex in exercises:
            ex_id = ex.get("id")
            pres = ex.get("prescribed")
            if not ex_id or not isinstance(pres, dict):
                continue
            db.execute(
                "INSERT OR REPLACE INTO fitness_exercise_override "
                "(exercise_id, prescribed_json, source, reasoning, change_summary, updated_at) "
                "VALUES (?, ?, 'ai_chat', ?, ?, CURRENT_TIMESTAMP)",
                (ex_id, json.dumps(pres, ensure_ascii=False),
                 ex.get("reasoning"), ex.get("change_summary")),
            )
            n += 1
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "applied": n})


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


def _est_1rm(weight, reps) -> float:
    """Epley 公式估算 1RM。weight*(1+reps/30)。自重(0)返回 0。"""
    try:
        w = float(weight); r = float(reps)
    except (TypeError, ValueError):
        return 0.0
    if w <= 0 or r <= 0:
        return 0.0
    return round(w * (1 + r / 30.0), 1)


def _exercise_pr(db, exercise_id: str, before_date: str = None) -> dict:
    """该动作历史个人纪录(PR)。before_date 给定则只看该日之前(排除今天在录的)。

    返回 {best_weight, best_1rm, best_reps_at_top, has_history}。
    """
    sql = "SELECT weight_kg, reps FROM fitness_log WHERE exercise_id=? AND weight_kg IS NOT NULL AND reps IS NOT NULL"
    params = [exercise_id]
    if before_date:
        sql += " AND date < ?"
        params.append(before_date)
    rows = db.execute(sql, params).fetchall()
    best_w = 0.0
    best_1rm = 0.0
    best_reps_at_top = 0
    for r in rows:
        w, reps = r["weight_kg"], r["reps"]
        if w is None or reps is None:
            continue
        if w > best_w:
            best_w = w; best_reps_at_top = reps
        elif w == best_w and reps > best_reps_at_top:
            best_reps_at_top = reps
        e = _est_1rm(w, reps)
        if e > best_1rm:
            best_1rm = e
    return {
        "best_weight": best_w,
        "best_1rm": best_1rm,
        "best_reps_at_top": best_reps_at_top,
        "has_history": len(rows) > 0,
    }


@api_bp.route("/pr/<exercise_id>")
def api_pr(exercise_id: str):
    """该动作 PR(给前端 saveSet 后判断是否破纪录)。"""
    username = _username()
    db = _db(username)
    pr = _exercise_pr(db, exercise_id, before_date=time.strftime("%Y-%m-%d"))
    db.close()
    return jsonify({"ok": True, "pr": pr})


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
    pr = _exercise_pr(db2 := _db(username), exercise_id,
                      before_date=time.strftime("%Y-%m-%d"))
    db2.close()
    return jsonify({
        "ok": True,
        "exercise_id": exercise_id,
        "exercise_name": spec.get("name"),
        "evidence_note": spec.get("evidence_note", ""),
        "last_date": last_date,
        "last_sets": last_sets,
        "recommendation": rec,
        "pr": pr,
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
      ?source=auto  YT 自带 caption + Google 机翻(默认,快+免费)
      ?source=hq    优先 YouTube 英文字幕原文(无字幕才 Cloud STT)+ LLM 精翻(质量优先)
      ?source=stt   纯 Cloud STT(仅底层 fallback)
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
