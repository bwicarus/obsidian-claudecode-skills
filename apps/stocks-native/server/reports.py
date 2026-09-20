"""Immutable stock reports and explicit, resumable adoption of absolute-price plans.

Reports, plans and monitoring have separate databases. Durable intents and stable
downstream request IDs make retries safe; a partial adoption is reported as such.
Saving or scheduling a report never starts monitoring.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

from monitoring import MonitorError
from plans import PlanError, _normalize_plan, _source


MAX_BASIS_AGE_SECONDS = 72 * 3600
PRICE_RULES = {"target_buy": ("买入观察", "below"), "add_price": ("加仓观察", "below"),
               "hard_stop": ("止损观察", "below"), "take_profit": ("止盈观察", "above")}
DIRECTIONS = ("bullish", "neutral", "bearish")
CONFIDENCES = ("low", "medium", "high")
UNSUPPORTED_LABELS = {"pct_stop": "浮亏百分比止损需要持仓成本", "pct_take": "浮盈百分比止盈需要持仓成本",
                      "trailing_drawdown": "峰值回撤需要持仓峰值记录", "max_shares": "最大股数需要持仓管理",
                      "no_add": "不再加仓约束需要持仓管理", "conflicting_target_price": "目标价与同类规则价格冲突"}


class ReportError(ValueError):
    def __init__(self, code, message, status=400, detail=None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ReportError("invalid_request", "报告必须是有效 JSON，数值必须有限") from exc


def _object(value, allowed, field):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ReportError("invalid_report", f"{field} 格式无效或包含未知字段")
    return value


def _text(value, field, maximum, empty=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ReportError("invalid_report", f"{field} 必须是长度不超过 {maximum} 的文字")
    value = value.strip()
    if not empty and not value:
        raise ReportError("invalid_report", f"{field} 不能为空")
    return value


def _id(value, field="id"):
    value = _text(value, field, 128)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ReportError("invalid_id", f"{field} 格式无效")
    return value


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ReportError("invalid_code", "code 必须是六位股票代码")
    return value


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
        raise ReportError("invalid_revision", "expectedRevision 必须是非负整数")
    return value


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _source_checked(value):
    try:
        return _source(value)
    except PlanError as exc:
        raise ReportError(exc.code, str(exc), exc.status, exc.detail) from exc


def _normalize_plan_checked(value):
    try:
        return _normalize_plan(value)
    except PlanError as exc:
        raise ReportError(exc.code, str(exc), exc.status, exc.detail) from exc


def validate_report(raw):
    """Validate before scheduling publication; model provenance is never accepted."""
    raw = _object(raw, ("code", "title", "summary", "direction", "confidence", "points", "risks", "basis", "sources", "planId"), "report")
    code = _code(raw.get("code"))
    if raw.get("direction") not in DIRECTIONS or raw.get("confidence") not in CONFIDENCES:
        raise ReportError("invalid_report", "direction 或 confidence 不在支持范围内")
    arrays = {}
    for key in ("points", "risks"):
        items = raw.get(key, [])
        if not isinstance(items, list) or len(items) > 12:
            raise ReportError("invalid_report", f"{key} 最多 12 项")
        arrays[key] = [_text(item, key, 600) for item in items]
    basis = raw.get("basis")
    # The plan validator is the single source of truth for market basis fields.
    normalized_basis = _normalize_plan_checked({"code": code, "title": "basis", "summary": "", "mode": "unspecified",
        "recommendedVariantId": "a", "variants": [
            {"id": "a", "label": "保守", "action": "观望", "targetKind": "无", "reason": "校验依据"},
            {"id": "b", "label": "标准", "action": "观望", "targetKind": "无", "reason": "校验依据"}], "basis": basis})["basis"]
    sources = raw.get("sources", [])
    if not isinstance(sources, list) or len(sources) > 12:
        raise ReportError("invalid_report", "sources 最多 12 项")
    normalized_sources = []
    for source in sources:
        source = _object(source, ("title", "url", "asOf"), "source")
        item = {"title": _text(source.get("title"), "source.title", 160)}
        if source.get("url") is not None:
            url = _text(source["url"], "source.url", 2000)
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if not valid:
                raise ReportError("invalid_report", "source.url 必须是 HTTP(S) 来源链接")
            item["url"] = url
        if source.get("asOf") is not None:
            item["asOf"] = _text(source["asOf"], "source.asOf", 80)
        normalized_sources.append(item)
    return {"code": code, "title": _text(raw.get("title"), "title", 120),
            "summary": _text(raw.get("summary"), "summary", 2400), "direction": raw["direction"],
            "confidence": raw["confidence"], **arrays, "basis": normalized_basis, "sources": normalized_sources,
            "planId": _id(raw["planId"], "planId") if raw.get("planId") is not None else None}


class ReportService:
    def __init__(self, state_dir, plans, monitor, clock=time.time):
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.db_path, self.plans, self.monitor, self.clock = directory / "reports.sqlite3", plans, monitor, clock
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(owner TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS reports(owner TEXT NOT NULL, id TEXT NOT NULL, code TEXT NOT NULL,
                    created_at TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(owner,id));
                CREATE INDEX IF NOT EXISTS reports_by_stock ON reports(owner,code);
                CREATE TABLE IF NOT EXISTS intents(owner TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY(owner,request_id));
                CREATE TABLE IF NOT EXISTS receipts(owner TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
                    response TEXT NOT NULL, PRIMARY KEY(owner,request_id));
                CREATE TABLE IF NOT EXISTS adoptions(owner TEXT NOT NULL, plan_id TEXT NOT NULL, code TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY(owner,plan_id));
            """)
            db.commit()

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _revision(db, owner):
        row = db.execute("SELECT revision FROM accounts WHERE owner=?", (owner,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _bump(db, owner):
        db.execute("INSERT INTO accounts(owner,revision) VALUES (?,1) ON CONFLICT(owner) DO UPDATE SET revision=revision+1", (owner,))
        return ReportService._revision(db, owner)

    @staticmethod
    def catalog():
        return {"schemaVersion": 1, "operations": ["list", "get", "signals", "save", "preview", "apply"],
                "directions": list(DIRECTIONS), "confidences": list(CONFIDENCES),
                "reportFields": ["code", "title", "summary", "direction", "confidence", "points", "risks", "basis", "sources", "planId"],
                "supportedMonitorRules": list(PRICE_RULES), "maxBasisAgeSeconds": MAX_BASIS_AGE_SECONDS,
                "notes": ["save 仅保存不可变报告及可选方案；不启动盯盘、不创建持仓、不执行交易。",
                          "expectedRevision 是报告库版本，从 list/get/preview 读取。requestId 重试复用原内容。",
                          "只有用户明确选择操作卡或要求按已展示条件开启时才 apply；先 preview 展示全部条件。",
                          "持仓百分比、峰值回撤、最大股数和不加仓规则尚不支持采用；任一不支持则整卡拒绝。",
                          "同方案只能采用一档；重复点击不恢复已暂停或删除的规则。换档需新方案。",
                          "超过 72 小时或无带时区依据的方案需先刷新分析。提醒默认 normal。",
                          "跨库通过持久意图与幂等回执恢复，partial 状态必须明示，不能宣称全部成功。"]}

    def _raw_adoption(self, db, owner, plan_id):
        row = db.execute("SELECT document FROM adoptions WHERE owner=? AND plan_id=?", (owner, plan_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _adoption_status(self, adoption, library):
        if not adoption:
            return None
        known = {r["id"]: r for r in library["rules"]}
        rules = [{"id": rid, "state": known[rid]["state"], "enabled": known[rid]["enabled"]}
                 for rid in adoption["ruleIds"] if rid in known]
        missing = [rid for rid in adoption["ruleIds"] if rid not in known]
        enabled = sum(int(rule["enabled"]) for rule in rules)
        complete = adoption["completionStatus"] == "complete"
        if not complete:
            state = "partial"
        elif not rules:
            state = "removed"
        elif missing:
            state = "partial"
        elif not enabled:
            state = "paused"
        elif enabled != len(rules):
            state = "partially_paused"
        else:
            priority = ("triggered", "stale", "data_unavailable", "confirming", "cooldown", "market_closed", "waiting", "watching")
            states = {rule["state"] for rule in rules}
            state = next((item for item in priority if item in states), "waiting")
        return {key: value for key, value in adoption.items() if key not in ("requests", "error")} | {
            "state": state, "enabledCount": enabled, "ruleCount": len(rules), "missingRuleIds": missing, "rules": rules}

    def _hydrate(self, db, owner, report, library):
        current = db.execute("SELECT document FROM reports WHERE owner=? AND id=?", (owner, report["id"])).fetchone()
        if current:
            report = json.loads(current[0])
        plan_id = report.get("planId")
        plan = self.plans.get(owner, plan_id)["plan"] if plan_id else None
        adoption = self._adoption_status(self._raw_adoption(db, owner, plan_id), library) if plan_id else None
        return {**report, "plan": plan, "adoption": adoption}

    def list(self, owner, code=None, limit=30, before=None):
        owner = _text(owner, "owner", 256)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ReportError("invalid_limit", "limit 应为 1 至 100 的整数")
        clauses, args = ["owner=?"], [owner]
        if code is not None:
            clauses.append("code=?")
            args.append(_code(code))
        library = self.monitor.library(owner)
        with self._db() as db:
            if before is not None:
                row = db.execute("SELECT rowid FROM reports WHERE owner=? AND id=?", (owner, _id(before, "before"))).fetchone()
                if not row:
                    raise ReportError("invalid_cursor", "报告分页位置已失效")
                clauses.append("rowid<?")
                args.append(row[0])
            rows = db.execute("SELECT document FROM reports WHERE " + " AND ".join(clauses) + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1)).fetchall()
            reports = [self._hydrate(db, owner, json.loads(row[0]), library) for row in rows[:limit]]
            adoption_rows = db.execute("SELECT document FROM adoptions WHERE owner=?" + (" AND code=?" if code else "") + " ORDER BY rowid DESC LIMIT ?",
                                       (owner, code, limit) if code else (owner, limit)).fetchall()
            events = [{"id": item["id"], "kind": "adoption", "code": item["code"], "planId": item["planId"],
                       "reportId": item.get("reportId"), "createdAt": item["createdAt"],
                       "adoption": self._adoption_status(item, library)} for item in (json.loads(row[0]) for row in adoption_rows)]
            return {"revision": self._revision(db, owner), "items": reports, "events": events,
                    "nextCursor": reports[-1]["id"] if len(rows) > limit else None, "asOf": _iso(self.clock())}

    def get(self, owner, report_id):
        owner, report_id = _text(owner, "owner", 256), _id(report_id)
        library = self.monitor.library(owner)
        with self._db() as db:
            row = db.execute("SELECT document FROM reports WHERE owner=? AND id=?", (owner, report_id)).fetchone()
            if not row:
                raise ReportError("report_not_found", "当前账户没有这份报告", 404)
            return {"revision": self._revision(db, owner), "report": self._hydrate(db, owner, json.loads(row[0]), library)}

    def signals(self, owner, codes=None):
        owner = _text(owner, "owner", 256)
        if codes is not None and (not isinstance(codes, list) or len(codes) > 100):
            raise ReportError("invalid_codes", "codes 最多 100 个股票代码")
        codes = list(dict.fromkeys(_code(code) for code in codes)) if codes is not None else None
        library = self.monitor.library(owner)
        with self._db() as db:
            clauses, args = ["owner=?"], [owner]
            if codes is not None:
                clauses.append("code IN (" + ",".join("?" for _ in codes) + ")")
                args.extend(codes)
            rows = db.execute("SELECT document FROM (SELECT document,created_at,rowid," +
                              "ROW_NUMBER() OVER (PARTITION BY code ORDER BY created_at DESC,rowid DESC) AS rank " +
                              "FROM reports WHERE " + " AND ".join(clauses) + ") WHERE rank=1 ORDER BY created_at DESC,rowid DESC", args).fetchall()
            # Plans have their own lifetime and revision. Read only this owner's
            # bounded plan library so a standalone conversation strategy appears
            # even when no formal report was requested.
            with self.plans._db() as plan_db:
                plan_rows = plan_db.execute("SELECT document FROM plans WHERE owner=? ORDER BY created_at DESC,rowid DESC", (owner,)).fetchall()
                plans = [json.loads(row[0]) for row in plan_rows]
            plan_by_id = {plan["id"]: plan for plan in plans}
            linked_plan_ids = {json.loads(row[0]).get("planId") for row in db.execute("SELECT document FROM reports WHERE owner=?", (owner,))}
            items = {}
            for row in rows:
                report = json.loads(row[0])
                plan = plan_by_id.get(report["planId"])
                active = bool(plan and plan["status"] == "proposed")
                adoption = self._adoption_status(self._raw_adoption(db, owner, report["planId"]), library) if report["planId"] else None
                variant_id = adoption["variantId"] if adoption else plan["recommendedVariantId"] if plan else None
                variant = next((v for v in plan["variants"] if v["id"] == variant_id), None) if active else None
                items[report["code"]] = {key: report[key] for key in ("code", "title", "summary", "direction", "confidence", "createdAt", "planId")} | {
                    "reportId": report["id"], "marketAsOf": report["basis"]["marketAsOf"],
                    "action": variant["action"] if variant else None, "targetPrice": variant["targetPrice"] if variant else None,
                    "planStatus": plan["status"] if plan else None, "hasStrategy": active,
                    "adoption": adoption}
            seen = set()
            for plan in plans:
                code = plan["code"]
                if (plan["status"] != "proposed" or plan["id"] in linked_plan_ids or code in seen
                        or (codes is not None and code not in codes)):
                    continue
                seen.add(code)
                current = items.get(code)
                if current and current["createdAt"] > plan["createdAt"]:
                    continue
                adoption = self._adoption_status(self._raw_adoption(db, owner, plan["id"]), library)
                variant_id = adoption["variantId"] if adoption else plan["recommendedVariantId"]
                variant = next(v for v in plan["variants"] if v["id"] == variant_id)
                items[code] = {"code": code, "reportId": None, "planId": plan["id"], "title": plan["title"],
                    "summary": plan["summary"], "direction": "unrated", "confidence": "unrated", "createdAt": plan["createdAt"],
                    "marketAsOf": plan["basis"]["marketAsOf"], "action": variant["action"], "targetPrice": variant["targetPrice"],
                    "planStatus": plan["status"], "hasStrategy": True,
                    "adoption": adoption}
            # One lightweight row per stock; do not silently drop older stocks
            # after an arbitrary global 100-row cutoff.
            for item in items.values():
                item["summary"] = item["summary"][:240]
            items = dict(sorted(items.items(), key=lambda item: item[1]["createdAt"], reverse=True))
            # Older standalone cards remain visible in history even when a newer
            # plan wins the per-stock signal. Return every adoption in this
            # account so clients can restore its current paused/removed state.
            adoptions = {row["plan_id"]: self._adoption_status(json.loads(row["document"]), library)
                         for row in db.execute("SELECT plan_id,document FROM adoptions WHERE owner=?", (owner,))}
            return {"revision": self._revision(db, owner), "items": items, "adoptions": adoptions, "asOf": _iso(self.clock())}

    def _receipt(self, db, owner, request_id, digest):
        row = db.execute("SELECT digest,response FROM receipts WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
        if row and row[0] != digest:
            raise ReportError("request_id_conflict", "requestId 已用于不同内容", 409)
        return json.loads(row[1]) if row else None

    def _request(self, owner, payload, operation, allowed):
        owner = _text(owner, "owner", 256)
        payload = _object(payload, ("requestId", "expectedRevision", *allowed), "request")
        request_id, expected = _id(payload.get("requestId"), "requestId"), _revision(payload.get("expectedRevision"))
        canonical = _json({"operation": operation, **payload})
        if len(canonical.encode()) > 64000:
            raise ReportError("request_too_large", "报告内容过大", 413)
        return owner, request_id, expected, hashlib.sha256(canonical.encode()).hexdigest()

    def save(self, owner, payload, source=None, *, _retry=0):
        owner, request_id, expected, digest = self._request(owner, payload, "save", ("report", "plan"))
        report, source = validate_report(payload.get("report")), _source_checked(source)
        plan = _normalize_plan_checked(payload["plan"]) if payload.get("plan") is not None else None
        if plan and (report["planId"] or plan["code"] != report["code"] or plan["basis"] != report["basis"]):
            raise ReportError("invalid_report", "新方案必须与报告使用同一股票和行情依据，不能同时指定 planId")
        if report["planId"] and self.plans.get(owner, report["planId"])["plan"]["code"] != report["code"]:
            raise ReportError("invalid_report", "报告和关联方案股票不一致")
        with self._db(write=True) as db:
            receipt = self._receipt(db, owner, request_id, digest)
            if receipt:
                return {**receipt, "report": self._hydrate(db, owner, receipt["report"], self.monitor.library(owner)), "replayed": True}
            row = db.execute("SELECT digest,document FROM intents WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if row:
                if row[0] != digest:
                    raise ReportError("request_id_conflict", "requestId 已用于不同内容", 409)
            else:
                revision = self._revision(db, owner)
                if expected != revision:
                    raise ReportError("revision_conflict", "报告资料已更新，请刷新后重试", 409, {"currentRevision": revision})
                report_id, now = "report-" + uuid.uuid4().hex, _iso(self.clock())
                report.update(id=report_id, schemaVersion=1, createdAt=now, source={**source, "reportId": report_id})
                intent = {"report": report, "planRequest": None}
                if plan:
                    intent["planRequest"] = {"requestId": "report-plan-" + hashlib.sha256((owner + "\0" + request_id).encode()).hexdigest()[:40],
                        "expectedRevision": self.plans.list(owner, limit=1)["revision"], "plan": plan}
                db.execute("INSERT INTO intents VALUES (?,?,?,?)", (owner, request_id, digest, _json(intent)))
                # Reserve the account revision when admitting the durable intent,
                # so two different concurrent requests cannot both accept it.
                self._bump(db, owner)
        # Intent commits before touching the plan database. An interrupted plan
        # write is replayed using precisely the same durable request payload.
        with self._db(write=True) as db:
            receipt = self._receipt(db, owner, request_id, digest)
            if receipt:
                return {**receipt, "report": self._hydrate(db, owner, receipt["report"], self.monitor.library(owner)), "replayed": True}
            intent = json.loads(db.execute("SELECT document FROM intents WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()[0])
            report = intent["report"]
            if intent["planRequest"]:
                try:
                    saved = self.plans.save(owner, intent["planRequest"], source=report["source"])
                except PlanError as exc:
                    # A conflict proves no receipt exists for this request. Keep
                    # the revised request durable before any new downstream write.
                    if exc.code == "revision_conflict":
                        intent["planRequest"]["expectedRevision"] = exc.detail["currentRevision"]
                        db.execute("UPDATE intents SET document=? WHERE owner=? AND request_id=?", (_json(intent), owner, request_id))
                        db.commit()
                        if _retry >= 3:
                            raise ReportError("plan_busy", "方案库正在更新，请用同一 requestId 重试", 409)
                        return self.save(owner, payload, source, _retry=_retry + 1)
                    raise
                report["planId"] = saved["planId"]
            db.execute("INSERT INTO reports(owner,id,code,created_at,document) VALUES (?,?,?,?,?)",
                       (owner, report["id"], report["code"], report["createdAt"], _json(report)))
            revision = self._revision(db, owner)
            receipt = {"success": True, "requestId": request_id, "revision": revision, "reportId": report["id"],
                       "report": report, "replayed": False}
            db.execute("INSERT INTO receipts VALUES (?,?,?,?)", (owner, request_id, digest, _json(receipt)))
            db.execute("DELETE FROM intents WHERE owner=? AND request_id=?", (owner, request_id))
            return {**receipt, "report": self._hydrate(db, owner, report, self.monitor.library(owner))}

    def bind_source(self, owner, report_id, source):
        owner, report_id, source = _text(owner, "owner", 256), _id(report_id), _source_checked(source)
        with self._db(write=True) as db:
            row = db.execute("SELECT document FROM reports WHERE owner=? AND id=?", (owner, report_id)).fetchone()
            if not row:
                raise ReportError("report_not_found", "当前账户没有这份报告", 404)
            report = json.loads(row[0])
            previous = report["source"]
            if previous.get("sessionId") and previous["sessionId"] == source.get("sessionId") and not previous.get("turnId"):
                report["source"] = {**source, **previous}
                db.execute("UPDATE reports SET document=? WHERE owner=? AND id=?", (_json(report), owner, report_id))
                if report["planId"]:
                    self.plans.bind_source(owner, report["planId"], report["source"])
            return self._hydrate(db, owner, report, self.monitor.library(owner))

    def preview(self, owner, payload):
        owner = _text(owner, "owner", 256)
        payload = _object(payload, ("planId", "variantId"), "request")
        plan_id, variant_id = _id(payload.get("planId"), "planId"), _id(payload.get("variantId"), "variantId")
        plan = self.plans.get(owner, plan_id)["plan"]
        variant = next((item for item in plan["variants"] if item["id"] == variant_id), None)
        if not variant:
            raise ReportError("variant_not_found", "方案中没有这个档位", 404)
        unsupported = [rule["type"] for rule in variant["rules"] if rule["type"] not in PRICE_RULES]
        conditions, seen = [], set()
        for item in variant["rules"]:
            if item["type"] in PRICE_RULES:
                label, op = PRICE_RULES[item["type"]]
                condition = {"metric": "price", "op": op, "threshold": item["value"]}
                key = (op, item["value"])
                if key not in seen:
                    conditions.append({"type": item["type"], "label": label, **condition})
                    seen.add(key)
        target_types = {"买入": "target_buy", "止损": "hard_stop", "止盈": "take_profit"}
        if variant["targetPrice"] is not None:
            rule_type = target_types[variant["targetKind"]]
            same_type = [r for r in variant["rules"] if r["type"] == rule_type]
            if same_type and same_type[0]["value"] != variant["targetPrice"]:
                unsupported.append("conflicting_target_price")
            label, op = PRICE_RULES[rule_type]
            if (op, variant["targetPrice"]) not in seen:
                conditions.append({"type": rule_type, "label": label, "metric": "price", "op": op, "threshold": variant["targetPrice"]})
        now, reason, stale = self.clock(), None, False
        try:
            when = datetime.fromisoformat(plan["basis"]["marketAsOf"].replace("Z", "+00:00"))
            age = now - when.timestamp() if when.tzinfo else None
            if age is None or age < -300 or age > MAX_BASIS_AGE_SECONDS:
                raise ValueError()
        except (ValueError, OverflowError):
            stale, reason = True, "行情依据已超过 72 小时或时间不明确，请刷新分析后采用新方案。"
        if plan["status"] != "proposed":
            reason = "方案已归档，请生成新方案。"
        elif unsupported:
            reason = "此档含尚不支持的条件，整张卡不能采用：" + "、".join(unsupported)
        elif not conditions:
            reason = "此档没有可执行的价格盯盘条件。"
        with self._db() as db:
            adoption = self._adoption_status(self._raw_adoption(db, owner, plan_id), self.monitor.library(owner))
            if adoption and adoption["variantId"] != variant_id:
                reason = "该方案已采用其他档位；请生成新方案后再选择，避免同时启用相互矛盾的档位。"
            elif adoption and adoption["completionStatus"] == "complete":
                reason = "此档已采用；重复点击不会恢复已暂停或删除的规则。"
            return {"revision": self._revision(db, owner), "planId": plan_id, "variantId": variant_id, "code": plan["code"],
                    "variantLabel": variant["label"], "conditions": conditions,
                    "unsupported": [UNSUPPORTED_LABELS.get(item, item) for item in unsupported], "unsupportedRuleTypes": unsupported,
                    "canApply": reason is None, "reason": reason, "stale": stale, "adoption": adoption,
                    "severity": "normal", "marketAsOf": plan["basis"]["marketAsOf"],
                    "notes": "每个价位独立盯盘，触达持续 10 秒后提醒；默认普通通知，不下单、不写入持仓。"}

    def apply(self, owner, payload):
        owner, request_id, expected, digest = self._request(owner, payload, "apply", ("planId", "variantId"))
        plan_id, variant_id = _id(payload.get("planId"), "planId"), _id(payload.get("variantId"), "variantId")
        with self._db() as db:
            receipt = self._receipt(db, owner, request_id, digest)
            if receipt:
                return {**receipt, "adoption": self._adoption_status(self._raw_adoption(db, owner, plan_id), self.monitor.library(owner)), "replayed": True}
        preview = self.preview(owner, {"planId": plan_id, "variantId": variant_id})
        with self._db(write=True) as db:
            receipt = self._receipt(db, owner, request_id, digest)
            if receipt:
                return {**receipt, "adoption": self._adoption_status(self._raw_adoption(db, owner, plan_id), self.monitor.library(owner)), "replayed": True}
            row = db.execute("SELECT digest FROM intents WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if row and row[0] != digest:
                raise ReportError("request_id_conflict", "requestId 已用于不同内容", 409)
            adoption = self._raw_adoption(db, owner, plan_id)
            if adoption and adoption["variantId"] != variant_id:
                raise ReportError("variant_conflict", preview["reason"], 409)
            if adoption and adoption["completionStatus"] == "complete":
                result = {"success": True, "requestId": request_id, "revision": self._revision(db, owner), "operation": "apply",
                          "adoption": self._adoption_status(adoption, self.monitor.library(owner)), "replayed": True}
                db.execute("INSERT INTO receipts VALUES (?,?,?,?)", (owner, request_id, digest, _json(result)))
                db.execute("DELETE FROM intents WHERE owner=? AND request_id=?", (owner, request_id))
                return result
            if not preview["canApply"]:
                raise ReportError("stale_plan" if preview["stale"] else "unsupported_plan", preview["reason"], 409, {"preview": preview})
            revision = self._revision(db, owner)
            if not row and expected != revision:
                raise ReportError("revision_conflict", "报告资料已更新，请刷新后重试", 409, {"currentRevision": revision})
            if not adoption:
                library = self.monitor.library(owner)
                if len(library["rules"]) + len(preview["conditions"]) > 200:
                    raise ReportError("rule_limit", "当前盯盘规则额度不足，整张卡未启用", 409)
                key = hashlib.sha256((owner + "\0" + plan_id).encode()).hexdigest()[:32]
                plan = self.plans.get(owner, plan_id)["plan"]
                adoption = {"id": "adoption-" + key, "planId": plan_id, "variantId": variant_id, "code": preview["code"],
                            "reportId": plan["source"].get("reportId"), "createdAt": _iso(self.clock()),
                            "completionStatus": "partial", "ruleIds": [], "requests": []}
                for index, condition in enumerate(preview["conditions"]):
                    rid = f"plan-{key}-{index}"
                    adoption["ruleIds"].append(rid)
                    adoption["requests"].append({"requestId": f"adopt-{key}-{index}", "operation": "rule.upsert", "rule": {
                        "id": rid, "title": f"{plan['code']} · {preview['variantLabel']} · {condition['label']}",
                        "code": plan["code"], "match": "all", "severity": "normal", "enabled": True,
                        "confirmSeconds": 10, "cooldownSeconds": 300, "rearmPercent": 0.05,
                        "conditions": [{key: condition[key] for key in ("metric", "op", "threshold")}]}})
                db.execute("INSERT INTO adoptions VALUES (?,?,?,?)", (owner, plan_id, adoption["code"], _json(adoption)))
                self._bump(db, owner)
            if not row:
                db.execute("INSERT INTO intents VALUES (?,?,?,?)", (owner, request_id, digest, _json({"planId": plan_id})))
        # Each monitor request is durable before execution. Replaying its receipt
        # never upserts again, so a user's later pause/delete is left untouched.
        try:
            for request in adoption["requests"]:
                self.monitor.mutate(owner, request)
        except Exception as exc:
            with self._db() as db:
                status = self._adoption_status(self._raw_adoption(db, owner, plan_id), self.monitor.library(owner))
            raise ReportError("adoption_partial", "部分盯盘尚未完成，请用原 requestId 重试；已存在的规则状态保留。", 503,
                              {"adoption": status, "cause": exc.code if isinstance(exc, MonitorError) else "monitor_write_failed"}) from exc
        with self._db(write=True) as db:
            adoption = self._raw_adoption(db, owner, plan_id)
            if adoption["completionStatus"] != "complete":
                adoption["completionStatus"] = "complete"
                db.execute("UPDATE adoptions SET document=? WHERE owner=? AND plan_id=?", (_json(adoption), owner, plan_id))
                self._bump(db, owner)
            result = {"success": True, "requestId": request_id, "revision": self._revision(db, owner), "operation": "apply",
                      "adoption": self._adoption_status(adoption, self.monitor.library(owner)), "replayed": False}
            db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)", (owner, request_id, digest, _json(result)))
            db.execute("DELETE FROM intents WHERE owner=? AND request_id=?", (owner, request_id))
            return result
