"""Account-scoped deterministic monitoring and a durable notification outbox.

No model or delivery provider is called here. A signal and its visual notice are
committed together before the separately leased AI worker can enrich the notice.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


CHINA = timezone(timedelta(hours=8))
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
CODE = re.compile(r"[0-9]{6}\Z")
METRICS = {
    "price": ("最新价", "元"), "changePct": ("涨跌幅", "%"),
    "volumeRatio": ("量比", "倍"), "turnoverRate": ("换手率", "%"),
    "amplitude": ("振幅", "%"), "nearLimitPct": ("距涨停价", "%"),
}
PERCENT_METRICS = {"changePct", "turnoverRate", "amplitude", "nearLimitPct"}
OPERATIONS = ("rule.upsert", "rule.pause", "rule.resume", "rule.delete",
              "notification.create", "notification.read", "notification.resolve")
MAX_QUOTE_AGE = 60
MAX_AI_ATTEMPTS = 3


class MonitorError(ValueError):
    def __init__(self, code, message, status=400, detail=None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _identifier(value, label="id"):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise MonitorError("invalid_id", f"{label} 无效")
    return value


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MonitorError("invalid_text", f"{label} 必须为 1–{maximum} 字符的文本")
    return value.strip()


def _owner(value):
    if not isinstance(value, str) or not value or len(value) > 300:
        raise MonitorError("invalid_owner", "缺少已认证账户", 401)
    return value


def _code(value):
    if not isinstance(value, str) or not CODE.fullmatch(value):
        raise MonitorError("invalid_code", "股票代码必须是六位数字")
    return value


def _trading(timestamp):
    local = datetime.fromtimestamp(timestamp, CHINA)
    minute = local.hour * 60 + local.minute + local.second / 60
    return local.weekday() < 5 and (570 <= minute <= 690 or 780 <= minute <= 900)


def _quote_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _new_runtime():
    return {"armed": True, "state": "waiting", "confirmSince": None,
            "lastQuote": None, "lastTriggered": None, "lastEventAt": None}


class MonitorService:
    def __init__(self, state_dir, clock=time.time):
        self.state_root = Path(state_dir).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_root / "monitoring.sqlite3"
        self.clock = clock
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(owner TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS rules(owner TEXT NOT NULL, id TEXT NOT NULL, document TEXT NOT NULL,
                    runtime TEXT NOT NULL, PRIMARY KEY(owner,id));
                CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    document TEXT NOT NULL, created REAL NOT NULL, ai_state TEXT NOT NULL,
                    ai_attempts INTEGER NOT NULL DEFAULT 0, ai_lease REAL, ai_token TEXT,
                    ai_after REAL NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS notifications_owner ON notifications(owner,created);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    rule_id TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(owner TEXT NOT NULL, request_id TEXT NOT NULL,
                    digest TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(owner,request_id));
            """)
            if "ai_after" not in {row[1] for row in db.execute("PRAGMA table_info(notifications)")}:
                db.execute("ALTER TABLE notifications ADD COLUMN ai_after REAL NOT NULL DEFAULT 0")

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
        return {"protocolVersion": 1,
                "metrics": [{"id": key, "label": value[0], "unit": value[1]} for key, value in METRICS.items()],
                "operators": [{"id": "above", "label": "达到或高于"}, {"id": "below", "label": "达到或低于"}],
                "matches": ["all", "any"], "severities": ["normal", "important", "urgent"],
                "operations": list(OPERATIONS),
                "defaults": {"confirmSeconds": 10, "cooldownSeconds": 300, "rearmPercent": 0.05},
                "quoteMaxAgeSeconds": MAX_QUOTE_AGE,
                "nearLimitPctDefinition": "(涨停价-最新价)/涨停价*100；负数表示已超过涨停价",
                "rearmPolicy": "百分比指标按百分点恢复，其余指标按阈值的百分比恢复；已读不重新武装",
                "tradingHours": "Asia/Shanghai 工作日 09:30–11:30、13:00–15:00；节假日无新鲜行情时不触发"}

    @staticmethod
    def _revision(db, owner):
        row = db.execute("SELECT revision FROM accounts WHERE owner=?", (owner,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _bump(db, owner):
        db.execute("INSERT INTO accounts(owner,revision) VALUES (?,1) ON CONFLICT(owner) DO UPDATE SET revision=revision+1", (owner,))

    def _library(self, db, owner):
        rules, summary = [], {}
        for row in db.execute("SELECT document,runtime FROM rules WHERE owner=? ORDER BY id", (owner,)):
            document, runtime = json.loads(row[0]), json.loads(row[1])
            state = runtime["state"] if document["enabled"] else "paused"
            rules.append({**document, "state": state, "armed": runtime["armed"],
                          "lastEventAt": runtime["lastEventAt"],
                          "lastQuoteAt": _iso(runtime["lastQuote"]) if runtime["lastQuote"] is not None else None})
            item = summary.setdefault(document["code"], {"ruleCount": 0, "enabledCount": 0,
                "unreadCount": 0, "state": "paused", "lastEventAt": None})
            item["ruleCount"] += 1
            item["enabledCount"] += int(document["enabled"])
            priority = {"paused": 0, "watching": 1, "waiting": 2, "market_closed": 3,
                        "cooldown": 4, "confirming": 5, "data_unavailable": 6, "stale": 7, "triggered": 8}
            if priority.get(state, 0) >= priority.get(item["state"], 0):
                item["state"] = state
            if runtime["lastEventAt"] and (not item["lastEventAt"] or runtime["lastEventAt"] > item["lastEventAt"]):
                item["lastEventAt"] = runtime["lastEventAt"]
        notifications = []
        for row in db.execute("SELECT document FROM notifications WHERE owner=? ORDER BY created DESC,id DESC", (owner,)):
            notice = json.loads(row[0])
            if len(notifications) < 200:
                notifications.append(notice)
            if notice["code"]:
                item = summary.setdefault(notice["code"], {"ruleCount": 0, "enabledCount": 0,
                    "unreadCount": 0, "state": "paused", "lastEventAt": None})
                item["unreadCount"] += int(notice["status"] == "unread")
        return {"revision": self._revision(db, owner), "rules": rules,
                "notifications": notifications, "summary": summary}

    def library(self, owner):
        with self._db() as db:
            return self._library(db, _owner(owner))

    def _normalize_rule(self, value, old=None):
        if not isinstance(value, dict):
            raise MonitorError("invalid_rule", "rule 必须是对象")
        data = {**(old or {}), **value}
        result = {"id": _identifier(data.get("id") or uuid.uuid4().hex),
                  "title": _text(data.get("title"), "规则名称", 120), "code": _code(data.get("code")),
                  "match": data.get("match", "all"), "severity": data.get("severity", "normal"),
                  "enabled": data.get("enabled", True)}
        if result["match"] not in ("all", "any") or result["severity"] not in ("normal", "important", "urgent"):
            raise MonitorError("invalid_rule", "组合方式或提醒等级无效")
        if not isinstance(result["enabled"], bool):
            raise MonitorError("invalid_rule", "enabled 必须是布尔值")
        for key, default, maximum in (("confirmSeconds", 10, 3600), ("cooldownSeconds", 300, 86400), ("rearmPercent", 0.05, 50)):
            number = _number(data.get(key, default))
            if number is None or not 0 <= number <= maximum:
                raise MonitorError("invalid_rule", f"{key} 超出允许范围")
            result[key] = number
        conditions = data.get("conditions")
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= 12:
            raise MonitorError("invalid_conditions", "规则必须有 1–12 个条件")
        result["conditions"] = []
        for condition in conditions:
            if not isinstance(condition, dict) or condition.get("metric") not in METRICS or condition.get("op") not in ("above", "below"):
                raise MonitorError("invalid_conditions", "条件指标或比较方式无效")
            threshold = _number(condition.get("threshold"))
            if threshold is None or abs(threshold) > 1e9:
                raise MonitorError("invalid_conditions", "阈值必须是有限数值")
            result["conditions"].append({"metric": condition["metric"], "op": condition["op"], "threshold": threshold})
        return result

    def _insert_notice(self, db, owner, value, *, evidence=None, rule_id=None, pending=True):
        if not isinstance(value, dict):
            raise MonitorError("invalid_notification", "notification 必须是对象")
        code = value.get("code", "")
        if code:
            _code(code)
        severity = value.get("severity", "normal")
        if severity not in ("normal", "important", "urgent"):
            raise MonitorError("invalid_notification", "提醒等级无效")
        delivery_mode = value.get("deliveryMode", "auto")
        if delivery_mode not in ("auto", "call"):
            raise MonitorError("invalid_notification", "投递方式无效")
        now, nid = self.clock(), uuid.uuid4().hex
        notice = {"id": nid, "ruleId": rule_id, "code": code,
                  "title": _text(value.get("title"), "提醒标题", 120),
                  "body": _text(value.get("body"), "提醒内容", 8000), "severity": severity,
                  "aiState": "pending" if pending else "complete", "createdAt": _iso(now),
                  "status": "unread", "evidence": evidence or {}, "delivery": {}}
        notice["deliveryMode"] = delivery_mode
        notice["originalBody"] = notice["body"]
        db.execute("INSERT INTO notifications(id,owner,document,created,ai_state) VALUES (?,?,?,?,?)",
                   (nid, owner, _canonical(notice), now, notice["aiState"]))
        return notice

    def mutate(self, owner, payload):
        owner = _owner(owner)
        if not isinstance(payload, dict):
            raise MonitorError("invalid_request", "请求必须是对象")
        request_id = _identifier(payload.get("requestId"), "requestId")
        operation = payload.get("operation")
        if operation not in OPERATIONS:
            raise MonitorError("unknown_operation", "不支持的盯盘操作")
        try:
            canonical = _canonical(payload)
        except (ValueError, TypeError) as exc:
            raise MonitorError("invalid_request", "请求必须是有效 JSON") from exc
        if len(canonical.encode()) > 64000:
            raise MonitorError("request_too_large", "操作内容过大", 413)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self._db(write=True) as db:
            receipt = db.execute("SELECT digest,response FROM receipts WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise MonitorError("request_id_conflict", "requestId 已用于其他操作", 409)
                return {**json.loads(receipt[1]), "replayed": True}
            revision = self._revision(db, owner)
            expected = payload.get("expectedRevision")
            if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected != revision):
                raise MonitorError("revision_conflict", "盯盘资料已更新，请刷新后重试", 409, {"currentRevision": revision})
            if operation == "rule.upsert":
                raw = payload.get("rule")
                if not isinstance(raw, dict):
                    raise MonitorError("invalid_rule", "rule 必须是对象")
                row = db.execute("SELECT document,runtime FROM rules WHERE owner=? AND id=?", (owner, str(raw.get("id", "")))).fetchone()
                old = json.loads(row[0]) if row else None
                if not old and db.execute("SELECT COUNT(*) FROM rules WHERE owner=?", (owner,)).fetchone()[0] >= 200:
                    raise MonitorError("rule_limit", "最多保存 200 条规则")
                rule = self._normalize_rule(raw, old)
                runtime = json.loads(row[1]) if row else _new_runtime()
                if old and any(old[k] != rule[k] for k in ("code", "conditions", "match", "confirmSeconds", "rearmPercent")):
                    runtime = _new_runtime()
                rule["version"] = old.get("version", 0) + 1 if old else 1
                rule["updatedAt"] = _iso(self.clock())
                db.execute("INSERT INTO rules(owner,id,document,runtime) VALUES (?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET document=excluded.document,runtime=excluded.runtime",
                           (owner, rule["id"], _canonical(rule), _canonical(runtime)))
            elif operation.startswith("rule."):
                rid = _identifier(payload.get("id"))
                row = db.execute("SELECT document,runtime FROM rules WHERE owner=? AND id=?", (owner, rid)).fetchone()
                if not row:
                    raise MonitorError("rule_not_found", "规则不存在", 404)
                if operation == "rule.delete":
                    db.execute("DELETE FROM rules WHERE owner=? AND id=?", (owner, rid))
                else:
                    rule, runtime = json.loads(row[0]), json.loads(row[1])
                    rule["enabled"] = operation == "rule.resume"
                    runtime["confirmSince"] = None
                    runtime["state"] = "waiting" if rule["enabled"] else "paused"
                    db.execute("UPDATE rules SET document=?,runtime=? WHERE owner=? AND id=?", (_canonical(rule), _canonical(runtime), owner, rid))
            elif operation == "notification.create":
                # AI-created text is already a finished notice: do not feed it back to AI.
                notice = self._insert_notice(db, owner, payload.get("notification"), pending=False)
            else:
                nid = _identifier(payload.get("id"))
                row = db.execute("SELECT document FROM notifications WHERE owner=? AND id=?", (owner, nid)).fetchone()
                if not row:
                    raise MonitorError("notification_not_found", "提醒不存在", 404)
                notice = json.loads(row[0])
                if notice["status"] != "resolved":
                    notice["status"] = "resolved" if operation == "notification.resolve" else "read"
                    notice["updatedAt"] = _iso(self.clock())
                    db.execute("UPDATE notifications SET document=? WHERE owner=? AND id=?", (_canonical(notice), owner, nid))
            self._bump(db, owner)
            result = {"success": True, "requestId": request_id, "revision": self._revision(db, owner),
                      "operation": operation, "replayed": False, "library": self._library(db, owner)}
            if operation == "notification.create":
                result["notificationId"] = notice["id"]
            db.execute("INSERT INTO receipts(owner,request_id,digest,response) VALUES (?,?,?,?)", (owner, request_id, digest, _canonical(result)))
            return result

    def active_codes(self):
        with self._db() as db:
            documents = [json.loads(row[0]) for row in db.execute("SELECT document FROM rules")]
            return sorted({d["code"] for d in documents if d["enabled"]})

    @staticmethod
    def _values(rule, quote):
        values = {}
        for condition in rule["conditions"]:
            metric = condition["metric"]
            if metric == "nearLimitPct":
                limit, price = _number(quote.get("upLimit")), _number(quote.get("price"))
                value = (limit - price) / limit * 100 if limit is not None and limit > 0 and price is not None and price > 0 else None
            else:
                value = _number(quote.get(metric))
                if metric == "price" and value is not None and value <= 0:
                    value = None
            if value is None:
                return None
            values[metric] = value
        return values

    def evaluate_quotes(self, quotes):
        notices, now = [], self.clock()
        with self._db(write=True) as db:
            for row in db.execute("SELECT owner,id,document,runtime FROM rules").fetchall():
                rule, runtime = json.loads(row["document"]), json.loads(row["runtime"])
                if not rule["enabled"]:
                    continue
                quote = quotes.get(rule["code"]) if isinstance(quotes, dict) else None
                timestamp = _quote_timestamp(quote.get("quoteTime")) if isinstance(quote, dict) else None
                unavailable = None
                if not _trading(now):
                    unavailable = "market_closed"
                elif timestamp is None or not isinstance(quote, dict) or (quote.get("code") and quote["code"] != rule["code"]):
                    unavailable = "data_unavailable"
                elif not -5 <= now - timestamp <= MAX_QUOTE_AGE or not _trading(timestamp):
                    unavailable = "stale"
                values = self._values(rule, quote) if unavailable is None else None
                if unavailable is None and values is None:
                    unavailable = "data_unavailable"
                if unavailable:
                    runtime["state"], runtime["confirmSince"] = unavailable, None
                elif runtime["lastQuote"] is not None and timestamp <= runtime["lastQuote"]:
                    # A cached quote may update no confirmation or recovery state.
                    continue
                else:
                    if runtime["lastQuote"] is not None and timestamp - runtime["lastQuote"] > MAX_QUOTE_AGE:
                        runtime["confirmSince"] = None
                    runtime["lastQuote"] = timestamp
                    matches, recoveries = [], []
                    for condition in rule["conditions"]:
                        metric, threshold = condition["metric"], condition["threshold"]
                        value = values[metric]
                        margin = rule["rearmPercent"] if metric in PERCENT_METRICS else abs(threshold) * rule["rearmPercent"] / 100
                        above = condition["op"] == "above"
                        matches.append(value >= threshold if above else value <= threshold)
                        recoveries.append(value < threshold - margin if above else value > threshold + margin)
                    matches = all(matches) if rule["match"] == "all" else any(matches)
                    recovered = any(recoveries) if rule["match"] == "all" else all(recoveries)
                    if not runtime["armed"] and recovered:
                        runtime["armed"] = True
                    cooling = runtime["lastTriggered"] is not None and now - runtime["lastTriggered"] < rule["cooldownSeconds"]
                    if not runtime["armed"]:
                        runtime["state"], runtime["confirmSince"] = "triggered", None
                    elif not matches:
                        runtime["state"], runtime["confirmSince"] = ("cooldown" if cooling else "watching"), None
                    elif cooling:
                        runtime["state"], runtime["confirmSince"] = "cooldown", None
                    else:
                        if runtime["confirmSince"] is None:
                            runtime["confirmSince"] = timestamp
                        runtime["state"] = "confirming"
                        if timestamp - runtime["confirmSince"] >= rule["confirmSeconds"]:
                            evidence = {"ruleVersion": rule["version"], "quoteTime": quote["quoteTime"],
                                        "observedAt": _iso(now), "quoteSource": quote.get("quoteSource"),
                                        "values": values, "conditions": rule["conditions"], "match": rule["match"],
                                        "confirmedSeconds": timestamp - runtime["confirmSince"]}
                            descriptions = [f"{METRICS[c['metric']][0]} {values[c['metric']]:g}{METRICS[c['metric']][1]}（阈值 {c['threshold']:g}）" for c in rule["conditions"]]
                            notice = self._insert_notice(db, row["owner"], {
                                "code": rule["code"], "title": rule["title"], "severity": rule["severity"],
                                "body": f"{rule['code']} 已触发规则：" + "；".join(descriptions)}, evidence=evidence, rule_id=rule["id"])
                            db.execute("INSERT INTO events(id,owner,rule_id,document) VALUES (?,?,?,?)",
                                       (notice["id"], row["owner"], rule["id"], _canonical(evidence)))
                            runtime.update(armed=False, state="triggered", confirmSince=None,
                                           lastTriggered=now, lastEventAt=notice["createdAt"])
                            self._bump(db, row["owner"])
                            notices.append({**notice, "ownerId": row["owner"]})
                db.execute("UPDATE rules SET runtime=? WHERE owner=? AND id=?", (_canonical(runtime), row["owner"], row["id"]))
        return notices

    def claim_ai_job(self, lease_seconds=180):
        now = self.clock()
        with self._db(write=True) as db:
            rows = db.execute("SELECT * FROM notifications WHERE (ai_state='pending' AND ai_after<=?) OR (ai_state='running' AND ai_lease<=?) ORDER BY created,id", (now, now)).fetchall()
            for row in rows:
                notice = json.loads(row["document"])
                if notice["status"] == "resolved":
                    notice.update(aiState="failed", aiCancelled=True, aiError="提醒已处理，取消未开始的分析")
                    db.execute("UPDATE notifications SET ai_state='failed',document=?,ai_token=NULL,ai_lease=NULL WHERE id=?", (_canonical(notice), row["id"]))
                    self._bump(db, row["owner"])
                    continue
                if row["ai_attempts"] >= MAX_AI_ATTEMPTS:
                    notice.update(aiState="failed", aiError="分析任务多次中断，已保留原始信号")
                    db.execute("UPDATE notifications SET ai_state='failed',document=?,ai_token=NULL,ai_lease=NULL WHERE id=?", (_canonical(notice), row["id"]))
                    self._bump(db, row["owner"])
                    continue
                token = uuid.uuid4().hex
                notice["aiState"] = "running"
                db.execute("UPDATE notifications SET ai_state='running',document=?,ai_attempts=ai_attempts+1,ai_lease=?,ai_token=? WHERE id=?",
                           (_canonical(notice), now + min(600, max(10, lease_seconds)), token, row["id"]))
                return {"id": token, "jobId": token, "ownerId": row["owner"], "notification": notice,
                        "attempt": row["ai_attempts"] + 1}
        return None

    def defer_ai_job(self, job_id, delay_seconds, reason="同股信号合并等待"):
        """Release an unstarted claim without charging an AI attempt."""
        delay = _number(delay_seconds)
        if delay is None or not 1 <= delay <= 86400:
            raise MonitorError("invalid_delay", "延后时间必须是 1–86400 秒")
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM notifications WHERE ai_token=? AND ai_state='running'", (job_id,)).fetchone()
            if not row:
                return None
            notice = json.loads(row["document"])
            notice.update(aiState="pending", aiDeferredUntil=_iso(self.clock() + delay), aiDeferredReason=str(reason)[:200])
            db.execute("UPDATE notifications SET document=?,ai_state='pending',ai_after=?,ai_attempts=MAX(0,ai_attempts-1),ai_token=NULL,ai_lease=NULL WHERE id=?",
                       (_canonical(notice), self.clock() + delay, row["id"]))
            return notice

    def finish_ai_job(self, job_id, body, error=None):
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM notifications WHERE ai_token=?", (job_id,)).fetchone()
            if not row:
                return None
            notice = json.loads(row["document"])
            if row["ai_state"] != "running":
                return notice
            if error is not None:
                notice.update(aiState="failed", aiError=str(error)[:500])
            else:
                notice["body"] = _text(body, "分析内容", 8000)
                notice["aiState"] = "complete"
            notice.pop("aiDeferredUntil", None)
            notice.pop("aiDeferredReason", None)
            notice["updatedAt"] = _iso(self.clock())
            db.execute("UPDATE notifications SET document=?,ai_state=?,ai_lease=NULL WHERE id=?", (_canonical(notice), notice["aiState"], row["id"]))
            self._bump(db, row["owner"])
            return notice

    def get_notification(self, owner, notification_id):
        with self._db() as db:
            row = db.execute("SELECT document FROM notifications WHERE owner=? AND id=?", (_owner(owner), notification_id)).fetchone()
            return json.loads(row[0]) if row else None

    def pending_notifications(self):
        """Internal dispatcher view. Delivery receipts must be checked before sending."""
        with self._db() as db:
            return [{**json.loads(row["document"]), "ownerId": row["owner"]}
                    for row in db.execute("SELECT owner,document FROM notifications ORDER BY created,id")
                    if json.loads(row["document"])["status"] != "resolved"]

    def mark_delivery(self, owner, notification_id, delivery):
        """Persist channel receipts; delivery never implies read or resolved."""
        if not isinstance(delivery, dict):
            raise MonitorError("invalid_delivery", "投递记录必须为对象")
        with self._db(write=True) as db:
            row = db.execute("SELECT document FROM notifications WHERE owner=? AND id=?", (_owner(owner), notification_id)).fetchone()
            if not row:
                return None
            notice = json.loads(row[0])
            notice["delivery"].update(copy.deepcopy(delivery))
            db.execute("UPDATE notifications SET document=? WHERE owner=? AND id=?", (_canonical(notice), owner, notification_id))
            return notice
