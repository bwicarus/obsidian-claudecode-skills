"""Account-bound scheduled jobs. No process, model or push is started by this module."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from monitoring import MonitorError

UTC = timezone.utc
DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
OPERATIONS = ("task.upsert", "task.pause", "task.resume", "task.cancel")
DUE_GRACE_SECONDS = 60


class ScheduleError(MonitorError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _iso(stamp):
    return datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds") if stamp is not None else None


def _id(value, label="id"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value):
        raise ScheduleError("invalid_id", f"{label} 无效")
    return value


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ScheduleError("invalid_text", f"{label} 必须为 1–{maximum} 字符")
    return value.strip()


def _owner(value):
    return _text(value, "已认证账户", 300)


def _keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ScheduleError("invalid_request", "参数包含不支持的字段或不是对象")


def _has_owner(value):
    if isinstance(value, dict):
        return any(str(k).casefold() in ("owner", "ownerid", "owner_id") or _has_owner(v) for k, v in value.items())
    return isinstance(value, list) and any(_has_owner(v) for v in value)


def normalize_schedule(raw):
    _keys(raw, ("kind", "timezone", "at", "time", "days"))
    zone_name = raw.get("timezone")
    if not isinstance(zone_name, str) or not zone_name or len(zone_name) > 100:
        raise ScheduleError("timezone_required", "必须明确提供 IANA 时区，例如 Asia/Tokyo")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("invalid_timezone", "IANA 时区无效") from exc
    kind = raw.get("kind")
    if kind == "once":
        _keys(raw, ("kind", "timezone", "at"))
        try:
            at = datetime.fromisoformat(raw["at"].replace("Z", "+00:00"))
            if at.tzinfo is None:
                first, second = at.replace(tzinfo=zone, fold=0), at.replace(tzinfo=zone, fold=1)
                if first.utcoffset() != second.utcoffset():
                    raise ValueError("ambiguous or nonexistent local time")
                at = first
            local = at.astimezone(zone)
            stamp = at.timestamp()
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise ScheduleError("invalid_schedule", "一次时间必须是有效 ISO 时间；无偏移按指定时区解释，夏令时重复小时需明确偏移") from exc
        return {"kind": kind, "timezone": zone_name, "at": local.isoformat(timespec="seconds")}, stamp
    if kind not in ("daily", "weekly"):
        raise ScheduleError("invalid_schedule", "仅支持 once、daily、weekly")
    _keys(raw, ("kind", "timezone", "time", "days") if kind == "weekly" else ("kind", "timezone", "time"))
    clock = raw.get("time")
    if not isinstance(clock, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
        raise ScheduleError("invalid_schedule", "time 必须为 HH:MM")
    result = {"kind": kind, "timezone": zone_name, "time": clock}
    if kind == "weekly":
        days = raw.get("days")
        if not isinstance(days, list) or not days or any(day not in DAYS for day in days):
            raise ScheduleError("invalid_schedule", "weekly.days 必须为 MO 至 SU 的非空列表")
        result["days"] = [day for day in DAYS if day in days]
    return result, None


def next_run(schedule, after):
    """Strictly after the instant. DST gaps skip; overlapping hours fire only once."""
    if schedule["kind"] == "once":
        stamp = datetime.fromisoformat(schedule["at"]).timestamp()
        return stamp if stamp > after else None
    zone = ZoneInfo(schedule["timezone"])
    local = datetime.fromtimestamp(after, zone)
    hour, minute = map(int, schedule["time"].split(":"))
    for offset in range(9):
        day = local.date() + timedelta(days=offset)
        if schedule["kind"] == "weekly" and DAYS[day.weekday()] not in schedule["days"]:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        stamp = candidate.timestamp()
        # A nonexistent wall clock is never silently shifted by an hour.
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != candidate.replace(tzinfo=None):
            continue
        if stamp > after:
            return stamp
    raise ScheduleError("invalid_schedule", "无法计算下次执行时间")


class ScheduleService:
    def __init__(self, state_dir, clock=time.time):
        root = Path(state_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.db_path, self.clock = root / "schedules.sqlite3", clock
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(owner TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tasks(owner TEXT NOT NULL,id TEXT NOT NULL,document TEXT NOT NULL,
                    generation INTEGER NOT NULL,next_run REAL,PRIMARY KEY(owner,id));
                CREATE INDEX IF NOT EXISTS due_tasks ON tasks(next_run);
                CREATE TABLE IF NOT EXISTS receipts(owner TEXT NOT NULL,request_id TEXT NOT NULL,
                    digest TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(owner,request_id));
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,task_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,scheduled_at REAL NOT NULL,task TEXT NOT NULL,status TEXT NOT NULL,
                    token TEXT,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0,started REAL,finished REAL,
                    body TEXT,error TEXT,result TEXT,notification_id TEXT,
                    UNIQUE(owner,task_id,generation,scheduled_at));
                CREATE INDEX IF NOT EXISTS run_history ON runs(owner,task_id,scheduled_at);
                CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY,token TEXT NOT NULL,expires REAL NOT NULL);
            """)

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def catalog():
        return {"protocolVersion": 1, "actions": ["catalog", "list", "get", "runs", "mutate"],
                "operations": list(OPERATIONS), "kinds": ["reminder", "analysis"],
                "scheduleKinds": ["once", "daily", "weekly"], "timezoneRequired": True,
                "deliveryModes": ["auto", "call"], "callRequiresExplicitUserRequest": True,
                "analysisData": "到点读取指定股票最新可用报价及来源时间；不支持任意脚本、联网研究或交易",
                "dueGraceSeconds": DUE_GRACE_SECONDS, "missedPolicy": "跳过已错过周期；错过一次标记 expired；不补跑",
                "calendarPolicy": "每日/每周按指定时区的日历执行，不代表交易日；夏令时缺失时刻跳过，重复小时只执行一次",
                "mutationReceipt": "success 仅表示计划已保存；查看 runs 和通知回执确认执行与交付"}

    @staticmethod
    def _revision(db, owner):
        row = db.execute("SELECT revision FROM accounts WHERE owner=?", (owner,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _task(row):
        return {**json.loads(row["document"]), "nextRunAt": _iso(row["next_run"]), "version": row["generation"]}

    def list(self, owner):
        owner = _owner(owner)
        with self._db() as db:
            return {"revision": self._revision(db, owner), "tasks": [self._task(row) for row in
                    db.execute("SELECT * FROM tasks WHERE owner=? ORDER BY id", (owner,))]}

    def get(self, owner, task_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE owner=? AND id=?", (_owner(owner), _id(task_id))).fetchone()
            if row is None:
                raise ScheduleError("task_not_found", "当前账户没有此定时任务", 404)
            return {"revision": self._revision(db, owner), "task": self._task(row)}

    def runs(self, owner, task_id, limit=20):
        self.get(owner, task_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ScheduleError("invalid_limit", "limit 必须为 1–100")
        with self._db() as db:
            rows = db.execute("SELECT * FROM runs WHERE owner=? AND task_id=? ORDER BY scheduled_at DESC LIMIT ?",
                              (owner, task_id, limit)).fetchall()
            return {"taskId": task_id, "runs": [{"id": row["id"], "scheduledAt": _iso(row["scheduled_at"]),
                    "status": row["status"], "taskVersion": row["generation"], "attempts": row["attempts"],
                    "startedAt": _iso(row["started"]), "finishedAt": _iso(row["finished"]),
                    "body": row["body"], "error": row["error"], "notificationId": row["notification_id"],
                    "result": json.loads(row["result"]) if row["result"] else None} for row in rows]}

    def mutate(self, owner, request):
        owner = _owner(owner)
        _keys(request, ("requestId", "expectedRevision", "operation", "task", "id"))
        request_id = _id(request.get("requestId"), "requestId")
        operation = request.get("operation")
        if operation not in OPERATIONS:
            raise ScheduleError("unknown_operation", "不支持的定时任务操作")
        _keys(request, ("requestId", "expectedRevision", "operation", "task") if operation == "task.upsert"
              else ("requestId", "expectedRevision", "operation", "id"))
        try:
            payload = _json(request)
        except (ValueError, TypeError) as exc:
            raise ScheduleError("invalid_request", "请求必须为有效 JSON") from exc
        if len(payload.encode()) > 16000:
            raise ScheduleError("request_too_large", "定时任务请求过大", 413)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        now = self.clock()
        with self._db(write=True) as db:
            receipt = db.execute("SELECT * FROM receipts WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if receipt:
                if receipt["digest"] != digest:
                    raise ScheduleError("request_id_conflict", "requestId 已用于其他操作", 409)
                return {**json.loads(receipt["response"]), "replayed": True}
            revision = self._revision(db, owner)
            expected = request.get("expectedRevision")
            if expected is not None and (type(expected) is not int or expected != revision):
                raise ScheduleError("revision_conflict", "定时任务已更新，请刷新后重试", 409, {"currentRevision": revision})
            raw = request.get("task") if operation == "task.upsert" else None
            if operation == "task.upsert":
                _keys(raw, ("id", "kind", "title", "prompt", "codes", "schedule", "deliveryMode"))
            task_id = _id(raw.get("id", uuid.uuid4().hex) if raw is not None else request.get("id"))
            row = db.execute("SELECT * FROM tasks WHERE owner=? AND id=?", (owner, task_id)).fetchone()
            old = json.loads(row["document"]) if row else None
            generation = row["generation"] + 1 if row else 1
            if operation == "task.upsert":
                if not row and db.execute("SELECT COUNT(*) FROM tasks WHERE owner=?", (owner,)).fetchone()[0] >= 200:
                    raise ScheduleError("task_limit", "最多保存 200 个定时任务")
                kind = raw.get("kind")
                if kind not in ("reminder", "analysis"):
                    raise ScheduleError("invalid_kind", "kind 必须为 reminder 或 analysis")
                codes = raw.get("codes", [])
                if not isinstance(codes, list) or len(codes) > 10 or any(not isinstance(c, str) or not re.fullmatch(r"[0-9]{6}", c) for c in codes):
                    raise ScheduleError("invalid_codes", "codes 最多为 10 个六位股票代码")
                if kind == "analysis" and not codes:
                    raise ScheduleError("codes_required", "分析任务必须明确 1–10 个股票代码")
                mode = raw.get("deliveryMode", "auto")
                if mode not in ("auto", "call"):
                    raise ScheduleError("invalid_delivery", "deliveryMode 仅支持 auto 或用户主动要求的 call")
                schedule, _ = normalize_schedule(raw.get("schedule"))
                due = next_run(schedule, now)
                if due is None:
                    raise ScheduleError("schedule_in_past", "一次任务时间已过去，请提供未来时间")
                document = {"id": task_id, "kind": kind, "title": _text(raw.get("title"), "title", 120),
                            "prompt": _text(raw.get("prompt"), "prompt", 4000), "codes": list(dict.fromkeys(codes)),
                            "schedule": schedule, "deliveryMode": mode, "status": "active",
                            "createdAt": old["createdAt"] if old else _iso(now), "updatedAt": _iso(now)}
            else:
                if not row:
                    raise ScheduleError("task_not_found", "当前账户没有此定时任务", 404)
                document = old
                due = None
                if operation == "task.resume":
                    if old["status"] in ("cancelled", "completed"):
                        raise ScheduleError("task_finished", "任务已结束；重新安排请明确 task.upsert", 409)
                    due = next_run(old["schedule"], now)
                    document["status"] = "active" if due is not None else "expired"
                elif operation == "task.pause":
                    if old["status"] != "active":
                        raise ScheduleError("task_not_active", "只有有效任务可以暂停", 409)
                    document["status"] = "paused"
                else:
                    document["status"] = "cancelled"
                document["updatedAt"] = _iso(now)
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET document=excluded.document,generation=excluded.generation,next_run=excluded.next_run",
                       (owner, task_id, _json(document), generation, due))
            # A saved revision invalidates old pending/in-flight results, not old visual notices.
            db.execute("UPDATE runs SET status='cancelled',finished=?,error='任务已修改、暂停或取消',token=NULL,lease_until=NULL WHERE owner=? AND task_id=? AND status IN ('pending','running','ready')",
                       (now, owner, task_id))
            db.execute("INSERT INTO accounts VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET revision=excluded.revision", (owner, revision + 1))
            result = {"success": True, "requestId": request_id, "operation": operation, "revision": revision + 1,
                      "replayed": False, "task": {**document, "version": generation, "nextRunAt": _iso(due)}}
            db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (owner, request_id, digest, _json(result)))
            return result

    def tick(self):
        """Materialize each due occurrence once. Multiple gateway instances may call it."""
        now = self.clock()
        with self._db(write=True) as db:
            for row in db.execute("SELECT * FROM tasks WHERE next_run<=?", (now,)).fetchall():
                task = json.loads(row["document"])
                if task["status"] != "active":
                    continue
                missed = now - row["next_run"] > DUE_GRACE_SECONDS
                once = task["schedule"]["kind"] == "once"
                status = ("expired" if once else "skipped") if missed else "pending"
                db.execute("INSERT OR IGNORE INTO runs(id,owner,task_id,generation,scheduled_at,task,status,finished,error) VALUES(?,?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, row["owner"], row["id"], row["generation"], row["next_run"], row["document"], status,
                            now if missed else None, "错过执行时间，未补跑" if missed else None))
                due = None if once else next_run(task["schedule"], now)
                if once and missed:
                    task["status"] = "expired"
                db.execute("UPDATE tasks SET next_run=?,document=? WHERE owner=? AND id=?", (due, _json(task), row["owner"], row["id"]))

    @staticmethod
    def _current(db, row):
        task = db.execute("SELECT * FROM tasks WHERE owner=? AND id=?", (row["owner"], row["task_id"])).fetchone()
        return task is not None and task["generation"] == row["generation"] and json.loads(task["document"])["status"] == "active"

    def claim(self, lease_seconds=180):
        now = self.clock()
        with self._db(write=True) as db:
            rows = db.execute("SELECT * FROM runs WHERE status IN ('pending','ready') OR (status='running' AND lease_until<=?) ORDER BY CASE status WHEN 'ready' THEN 0 ELSE 1 END,scheduled_at", (now,)).fetchall()
            for row in rows:
                if not self._current(db, row):
                    db.execute("UPDATE runs SET status='cancelled',finished=? WHERE id=?", (now, row["id"]))
                    continue
                if row["status"] == "ready" and row["lease_until"] and row["lease_until"] > now:
                    continue
                task = json.loads(row["task"])
                if task["kind"] == "analysis" and row["status"] != "ready":
                    lease = db.execute("SELECT * FROM leases WHERE name='analysis'").fetchone()
                    if lease and lease["expires"] > now:
                        continue
                token = uuid.uuid4().hex
                ready = row["status"] == "ready"
                if row["attempts"] >= 3 and not ready:
                    db.execute("UPDATE runs SET status='failed',finished=?,error='执行多次中断，请查询后重新安排' WHERE id=?", (now, row["id"]))
                    self._complete_once(db, row)
                    continue
                if task["kind"] == "analysis" and not ready:
                    db.execute("INSERT OR REPLACE INTO leases VALUES('analysis',?,?)", (token, now + lease_seconds))
                db.execute("UPDATE runs SET status=?,token=?,lease_until=?,attempts=attempts+?,started=COALESCE(started,?) WHERE id=?",
                           ("ready" if ready else "running", token, now + lease_seconds, 0 if ready else 1, now, row["id"]))
                return {"id": row["id"], "token": token, "ownerId": row["owner"], "task": task,
                        "scheduledAt": _iso(row["scheduled_at"]), "ready": ready}
        return None

    def renew(self, job, lease_seconds=180):
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND token=? AND status IN ('running','ready')", (job["id"], job["token"])).fetchone()
            if not row or not self._current(db, row):
                return False
            db.execute("UPDATE runs SET lease_until=? WHERE id=?", (self.clock() + lease_seconds, row["id"]))
            db.execute("UPDATE leases SET expires=? WHERE name='analysis' AND token=?", (self.clock() + lease_seconds, job["token"]))
            return True

    def prepare(self, job, body, *, error=None, result=None):
        body = _text(body, "任务结果", 8000)
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND token=? AND status='running'", (job["id"], job["token"])).fetchone()
            if not row or not self._current(db, row) or row["lease_until"] <= self.clock():
                return False
            db.execute("UPDATE runs SET status='ready',body=?,error=?,result=? WHERE id=?", (body, error, _json(result or {}), row["id"]))
            # A small persistent cooldown also prevents a queue from opening AI continuously.
            db.execute("UPDATE leases SET expires=? WHERE name='analysis' AND token=?", (self.clock() + 30, job["token"]))
            return True

    @staticmethod
    def _complete_once(db, row):
        task_row = db.execute("SELECT * FROM tasks WHERE owner=? AND id=?", (row["owner"], row["task_id"])).fetchone()
        if task_row and task_row["generation"] == row["generation"]:
            task = json.loads(task_row["document"])
            if task["schedule"]["kind"] == "once":
                task["status"] = "completed"
                db.execute("UPDATE tasks SET document=? WHERE owner=? AND id=?", (_json(task), row["owner"], row["task_id"]))

    def publish(self, job, monitor):
        """Linearize cancellation against durable notification creation, with replay after a crash.

        The scheduler lock spans only the local notification DB write, never AI/network.
        Persisted body + stable requestId mean retry cannot create a second notification.
        """
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND token=? AND status='ready'", (job["id"], job["token"])).fetchone()
            if not row or not self._current(db, row) or row["lease_until"] <= self.clock():
                return None
            task = json.loads(row["task"])
            response = monitor.mutate(row["owner"], {"requestId": "schedule-" + row["id"], "operation": "notification.create",
                "notification": {"title": task["title"], "body": row["body"], "code": task["codes"][0] if task["codes"] else "",
                                 "severity": "urgent" if task["deliveryMode"] == "call" else "normal", "deliveryMode": task["deliveryMode"]}})
            if not response.get("success") or not response.get("notificationId"):
                raise RuntimeError("定时通知没有成功保存回执")
            db.execute("UPDATE runs SET status=?,notification_id=?,finished=?,token=NULL,lease_until=NULL WHERE id=?",
                       ("failed" if row["error"] else "completed", response["notificationId"], self.clock(), row["id"]))
            self._complete_once(db, row)
            return response["notificationId"]

    def abandon(self, job):
        """Release on worker shutdown; a prepared output can be published without rerunning AI."""
        with self._db(write=True) as db:
            row = db.execute("SELECT status FROM runs WHERE id=? AND token=? AND status IN ('running','ready')", (job["id"], job["token"])).fetchone()
            cancelled = db.execute("SELECT 1 FROM runs WHERE id=? AND status='cancelled'", (job["id"],)).fetchone()
            db.execute("UPDATE runs SET lease_until=? WHERE id=? AND token=? AND status IN ('running','ready')", (self.clock(), job["id"], job["token"]))
            if (row and row["status"] == "running") or cancelled:
                # Cancellation clears the run token; the lease's token still fences
                # the old worker from releasing a lease acquired by its successor.
                db.execute("UPDATE leases SET expires=? WHERE name='analysis' AND token=?", (self.clock(), job["token"]))


def dispatch_schedule_safe(service, owner, action, request=None):
    try:
        if service is None:
            raise ScheduleError("schedule_unavailable", "定时任务服务不可用", 503)
        _owner(owner)
        if request is not None and not isinstance(request, dict):
            raise ScheduleError("invalid_request", "request 必须为对象")
        request = copy.deepcopy(request or {})
        if _has_owner(request):
            raise ScheduleError("owner_not_allowed", "账户身份不能由工具参数指定", 403)
        if action in ("catalog", "list"):
            _keys(request, ())
            result = service.catalog() if action == "catalog" else service.list(owner)
        elif action == "get":
            _keys(request, ("id",))
            result = service.get(owner, request.get("id"))
        elif action == "runs":
            _keys(request, ("id", "limit"))
            result = service.runs(owner, request.get("id"), request.get("limit", 20))
        elif action == "mutate":
            result = service.mutate(owner, request)
        else:
            raise ScheduleError("unknown_action", "不支持的定时任务动作")
        return {"ok": True, "action": action, "result": result}
    except ScheduleError as exc:
        return {"ok": False, "action": action, "error": {"code": exc.code, "message": str(exc), "status": exc.status, "detail": exc.detail}}
