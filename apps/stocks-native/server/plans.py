"""Account-scoped advisory plans. Saving never starts monitoring or a trade."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid


LABELS = ("保守", "标准", "激进")
ACTIONS = ("持有", "加仓", "减仓", "清仓", "买入", "观望")
TARGET_KINDS = ("止盈", "止损", "买入", "无")
RULES = {
    "hard_stop": ("硬止损", "price"), "take_profit": ("止盈", "price"),
    "add_price": ("加仓价", "price"), "target_buy": ("建仓买点", "price"),
    "pct_stop": ("浮亏止损", "percent"), "pct_take": ("浮盈止盈", "percent"),
    "trailing_drawdown": ("峰值回撤", "percent"), "max_shares": ("最大股数", "shares"),
    "no_add": ("不再加仓", "none"),
}


class PlanError(ValueError):
    def __init__(self, code, message, status=400, detail=None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise PlanError("invalid_request", "方案必须是有效 JSON，数值必须有限") from exc


def _object(value, allowed, field):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise PlanError("invalid_plan", f"{field} 格式无效或包含未知字段")
    return value


def _text(value, field, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise PlanError("invalid_plan", f"{field} 必须是长度不超过 {maximum} 的文字")
    value = value.strip()
    if not empty and not value:
        raise PlanError("invalid_plan", f"{field} 不能为空")
    return value


def _identifier(value, field="id"):
    value = _text(value, field, 128)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise PlanError("invalid_plan", f"{field} 格式无效")
    return value


def _owner(value):
    return _text(value, "owner", 256)


def _choice(value, allowed, field):
    if not isinstance(value, str) or value not in allowed:
        raise PlanError("invalid_plan", f"{field} 不在支持范围内")
    return value


def _number(value, field, maximum=100000000, *, integer=False, optional=False):
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError("invalid_plan", f"{field} 必须是正数")
    try:
        valid = math.isfinite(value) and 0 < value <= maximum
    except OverflowError:
        valid = False
    if not valid or (integer and (not isinstance(value, int))):
        raise PlanError("invalid_plan", f"{field} 数值无效")
    return value


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
        raise PlanError("invalid_revision", "expectedRevision 必须是非负整数")
    return value


def _normalize_plan(raw):
    raw = _object(raw, ("code", "title", "summary", "mode", "recommendedVariantId", "variants", "basis"), "plan")
    code = _text(raw.get("code"), "code", 6)
    if not re.fullmatch(r"[0-9]{6}", code):
        raise PlanError("invalid_plan", "code 必须是六位股票代码")
    variants = raw.get("variants")
    if not isinstance(variants, list) or not 2 <= len(variants) <= 3:
        raise PlanError("invalid_plan", "方案需要 2 至 3 个档位")
    normalized, ids, labels = [], set(), set()
    for item in variants:
        item = _object(item, ("id", "label", "action", "targetPrice", "targetKind", "suggestedShares", "urgency", "reason", "rules"), "variant")
        vid = _identifier(item.get("id"), "variant.id")
        label = _choice(item.get("label"), LABELS, "variant.label")
        if vid in ids or label in labels:
            raise PlanError("invalid_plan", "档位编号和名称不能重复")
        ids.add(vid)
        labels.add(label)
        price = _number(item.get("targetPrice"), "targetPrice", optional=True)
        kind = _choice(item.get("targetKind", "无"), TARGET_KINDS, "targetKind")
        if (price is None) != (kind == "无"):
            raise PlanError("invalid_plan", "有目标价时需指定用途；无目标价时 targetKind 填无")
        rules = item.get("rules", [])
        if not isinstance(rules, list) or len(rules) > len(RULES):
            raise PlanError("invalid_plan", "rules 格式无效或过多")
        seen, normalized_rules = set(), []
        for rule in rules:
            rule = _object(rule, ("type", "value"), "rule")
            rule_type = _choice(rule.get("type"), RULES, "rule.type")
            if rule_type in seen:
                raise PlanError("invalid_plan", "同档规则类型不能重复")
            seen.add(rule_type)
            unit = RULES[rule_type][1]
            value = rule.get("value")
            if unit == "none":
                if value is not None:
                    raise PlanError("invalid_plan", "no_add 不接受数值")
            else:
                maximum = 10000 if rule_type == "pct_take" else 100 if unit == "percent" else 1000000000 if unit == "shares" else 100000000
                value = _number(value, "rule.value", maximum, integer=unit == "shares")
            normalized_rules.append({"type": rule_type, "value": value})
        normalized.append({
            "id": vid, "label": label, "action": _choice(item.get("action"), ACTIONS, "action"),
            "targetPrice": price, "targetKind": kind,
            "suggestedShares": _number(item.get("suggestedShares"), "suggestedShares", 1000000000, integer=True, optional=True),
            "urgency": _choice(item.get("urgency", "normal"), ("normal", "warn", "critical"), "urgency"),
            "reason": _text(item.get("reason"), "reason", 400), "rules": normalized_rules,
        })
    recommended = _identifier(raw.get("recommendedVariantId"), "recommendedVariantId")
    if recommended not in ids:
        raise PlanError("invalid_plan", "推荐档位必须属于当前方案")
    basis = _object(raw.get("basis"), ("marketAsOf", "referencePrice", "contextRevision"), "basis")
    as_of = _text(basis.get("marketAsOf"), "marketAsOf", 40)
    try:
        datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanError("invalid_plan", "marketAsOf 必须是行情日期或 ISO 时间") from exc
    context_revision = basis.get("contextRevision")
    if context_revision is not None:
        context_revision = _revision(context_revision)
    return {"code": code, "title": _text(raw.get("title"), "title", 80),
            "summary": _text(raw.get("summary", ""), "summary", 1200, empty=True),
            "mode": _choice(raw.get("mode", "unspecified"), ("watch", "position", "unspecified"), "mode"),
            "recommendedVariantId": recommended, "variants": normalized,
            "basis": {"marketAsOf": as_of, "referencePrice": _number(basis.get("referencePrice"), "referencePrice", optional=True),
                      "contextRevision": context_revision}}


def _source(value):
    allowed = ("sessionId", "threadId", "turnId", "messageId", "requestId", "scheduleId", "runId", "kind", "reportId")
    source = _object(value or {}, allowed, "source")
    return {key: _text(val, "source." + key, 200) for key, val in source.items() if val is not None}


class PlanService:
    def __init__(self, state_dir):
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.db_path = directory / "plans.sqlite3"
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(owner TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS plans(owner TEXT NOT NULL, id TEXT NOT NULL, code TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(owner,id));
                CREATE INDEX IF NOT EXISTS plans_by_stock ON plans(owner,code,status,created_at);
                CREATE TABLE IF NOT EXISTS receipts(owner TEXT NOT NULL, request_id TEXT NOT NULL,
                    digest TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(owner,request_id));
            """)
            db.commit()

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _account_revision(db, owner):
        row = db.execute("SELECT revision FROM accounts WHERE owner=?", (owner,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def catalog():
        return {"schemaVersion": 1, "operations": ["list", "get", "save", "archive"],
                "variantLabels": list(LABELS), "variantCount": {"min": 2, "max": 3},
                "actions": list(ACTIONS), "targetKinds": list(TARGET_KINDS),
                "modes": ["watch", "position", "unspecified"],
                "rules": [{"type": key, "label": value[0], "unit": value[1]} for key, value in RULES.items()],
                "saveFields": ["code", "title", "summary", "mode", "recommendedVariantId", "variants", "basis"],
                "notes": ["保存方案不启动盯盘、不创建持仓、不执行交易。", "无持仓或预算依据时 suggestedShares 填 null。",
                          "requestId 重试复用；expectedRevision 从 list/get 取得。", "basis.marketAsOf 必须引用已获取的行情时间，保存成功不是行情核实。"]}

    def list(self, owner, code=None, limit=20, include_archived=False):
        owner = _owner(owner)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise PlanError("invalid_limit", "limit 应为 1 至 100 的整数")
        if not isinstance(include_archived, bool):
            raise PlanError("invalid_request", "include_archived 必须是布尔值")
        clauses, args = ["owner=?"], [owner]
        if code is not None:
            if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
                raise PlanError("invalid_code", "code 必须是六位股票代码")
            clauses.append("code=?")
            args.append(code)
        if not include_archived:
            clauses.append("status='proposed'")
        with self._db() as db:
            rows = db.execute("SELECT document FROM plans WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC,rowid DESC LIMIT ?", (*args, limit)).fetchall()
            return {"revision": self._account_revision(db, owner), "items": [json.loads(row[0]) for row in rows], "asOf": _now()}

    def get(self, owner, plan_id):
        owner, plan_id = _owner(owner), _identifier(plan_id)
        with self._db() as db:
            row = db.execute("SELECT document FROM plans WHERE owner=? AND id=?", (owner, plan_id)).fetchone()
            if not row:
                raise PlanError("plan_not_found", "当前账户没有这份方案", 404)
            return {"revision": self._account_revision(db, owner), "plan": json.loads(row[0])}

    def save(self, owner, payload, source=None):
        return self._mutate(owner, payload, "save", source)

    def archive(self, owner, payload):
        return self._mutate(owner, payload, "archive")

    def bind_source(self, owner, plan_id, source):
        """Attach the observed Codex turn once; model inputs cannot set provenance."""
        owner, plan_id, source = _owner(owner), _identifier(plan_id), _source(source)
        with self._db(write=True) as db:
            row = db.execute("SELECT document FROM plans WHERE owner=? AND id=?", (owner, plan_id)).fetchone()
            if not row:
                raise PlanError("plan_not_found", "当前账户没有这份方案", 404)
            plan = json.loads(row[0])
            previous = plan.get("source", {})
            if not previous.get("sessionId") or previous.get("sessionId") != source.get("sessionId") or previous.get("turnId"):
                return plan
            plan["source"] = {**source, **previous}
            db.execute("UPDATE plans SET document=? WHERE owner=? AND id=?", (_json(plan), owner, plan_id))
            return plan

    def _mutate(self, owner, payload, operation, source=None):
        owner = _owner(owner)
        payload = _object(payload, ("requestId", "expectedRevision", "plan" if operation == "save" else "id"), "request")
        request_id = _identifier(payload.get("requestId"), "requestId")
        expected = _revision(payload.get("expectedRevision"))
        canonical = _json({"operation": operation, **payload})
        if len(canonical.encode()) > 32000:
            raise PlanError("request_too_large", "方案内容过大", 413)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self._db(write=True) as db:
            receipt = db.execute("SELECT digest,response FROM receipts WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise PlanError("request_id_conflict", "requestId 已用于不同内容", 409)
                return {**json.loads(receipt[1]), "replayed": True}
            revision = self._account_revision(db, owner)
            if expected != revision:
                raise PlanError("revision_conflict", "方案资料已更新，请刷新后重试", 409, {"currentRevision": revision})
            now = _now()
            if operation == "save":
                plan = _normalize_plan(payload.get("plan"))
                if db.execute("SELECT COUNT(*) FROM plans WHERE owner=?", (owner,)).fetchone()[0] >= 1000:
                    raise PlanError("plan_limit", "已达到 1000 份方案上限", 409)
                plan.update(id="plan-" + uuid.uuid4().hex, schemaVersion=1, revision=1, status="proposed",
                            source=_source(source), createdAt=now, updatedAt=now)
                db.execute("INSERT INTO plans(owner,id,code,status,created_at,document) VALUES (?,?,?,?,?,?)",
                           (owner, plan["id"], plan["code"], plan["status"], now, _json(plan)))
            else:
                plan_id = _identifier(payload.get("id"))
                row = db.execute("SELECT document FROM plans WHERE owner=? AND id=?", (owner, plan_id)).fetchone()
                if not row:
                    raise PlanError("plan_not_found", "当前账户没有这份方案", 404)
                plan = json.loads(row[0])
                if plan["status"] != "archived":
                    plan.update(status="archived", updatedAt=now, revision=plan["revision"] + 1)
                    db.execute("UPDATE plans SET status=?,document=? WHERE owner=? AND id=?", ("archived", _json(plan), owner, plan_id))
            revision += 1
            db.execute("INSERT INTO accounts(owner,revision) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET revision=excluded.revision", (owner, revision))
            result = {"success": True, "requestId": request_id, "revision": revision, "operation": operation,
                      "planId": plan["id"], "plan": plan, "replayed": False}
            db.execute("INSERT INTO receipts(owner,request_id,digest,response) VALUES (?,?,?,?)", (owner, request_id, digest, _json(result)))
            return result
