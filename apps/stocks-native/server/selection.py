"""Account-scoped screening and watchlists, independent of the legacy writer.

The 25 criterion identifiers, OR-of-AND/NOT semantics and single-condition
impact counts are adapted from the deployed export_stock_candidates.py and
stocks-webapp screen/query + screen/impact. Only published market snapshots are
read. No production module is imported, no collector or AI process is started.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


CRITERIA = [
    ("price_below_limit", "股价低于上限", "basic", ["max_price"]),
    ("price_above_min", "股价高于下限", "basic", ["min_price"]),
    ("is_star_market", "科创板（688）", "basic", []),
    ("is_bse", "北交所（92）", "basic", []),
    ("turnover_in_range", "换手率在区间", "basic", ["turnover_rate_min", "turnover_rate_max"]),
    ("is_limit_up", "今日涨停", "basic", []),
    ("hot_sector", "热门板块", "basic", []),
    ("sector_leader", "板块龙头", "basic", []),
    ("above_ma5", "收盘价在五日线上", "technical", []),
    ("ma_aligned_bullish", "均线多头排列", "technical", []),
    ("near_ma10", "回踩10日线", "technical", []),
    ("break_60d_high", "创60日新高", "technical", []),
    ("volume_expanding", "成交量放量", "technical", []),
    ("volume_contracting", "量能萎缩", "technical", []),
    ("macd_expanding", "MACD 放量向上", "technical", []),
    ("kdj_up", "KDJ 向上", "technical", []),
    ("kdj_recent_cross", "近日 KDJ 金叉", "technical", ["kdj_cross_days"]),
    ("weekly_up", "周 K 向上", "technical", []),
    ("monthly_up", "月 K 向上", "technical", []),
    ("net_capital_inflow", "当日资金净流入", "fund", []),
    ("main_fund_5d_inflow", "五日主力净流入", "fund", []),
    ("main_force_present", "当前有主力在场", "fund", []),
    ("profit_ratio_above", "筹码获利比例 ≥ 阈值", "chips", ["profit_ratio_above_value"]),
    ("profit_ratio_below", "筹码获利比例 ≤ 阈值", "chips", ["profit_ratio_below_value"]),
    ("chip_concentration_below", "筹码集中度 ≤ 阈值", "chips", ["chip_concentration_max_value"]),
]
PARAMETERS = [
    ("max_price", "股价上限（元）", 80, 0, 100000),
    ("min_price", "股价下限（元）", 0, 0, 100000),
    ("turnover_rate_min", "换手率下限（%）", 1, 0, 1000),
    ("turnover_rate_max", "换手率上限（%）", 15, 0, 1000),
    ("profit_ratio_above_value", "获利盘下限（%）", 90, 0, 100),
    ("profit_ratio_below_value", "获利盘上限（%）", 50, 0, 100),
    ("chip_concentration_max_value", "集中度上限（%）", 15, 0, 100),
    ("kdj_cross_days", "KDJ 金叉回看交易日", 5, 1, 60),
]
KEYS = {c[0] for c in CRITERIA}
DEFAULT_PARAMETERS = {p[0]: p[2] for p in PARAMETERS}
SMART_ATTRIBUTES = [
    {"id": "hot_sector", "label": "热点板块成员", "status": "available"},
    {"id": "holding", "label": "当前持仓", "status": "pending_migration"},
    {"id": "ever_held", "label": "曾持仓", "status": "pending_migration"},
    {"id": "ai_a", "label": "旧 AI A 档（待迁移）", "status": "pending_migration"},
    {"id": "expert_bull", "label": "研判偏多（待接新标签）", "status": "pending_migration"},
]
OPERATIONS = ["group.create", "group.update", "group.delete", "group.add",
              "group.remove", "group.refresh", "group.reorder", "preset.save",
              "preset.delete", "preset.run"]
CODE = re.compile(r"[0-9]{6}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


class SelectionError(ValueError):
    def __init__(self, code, message, status=400, detail=None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


class SelectionConflict(SelectionError):
    def __init__(self, revision, message="资料已更新，请刷新后重试"):
        super().__init__("revision_conflict", message, 409, {"currentRevision": revision})
        self.revision = revision


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _json(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _codes(value):
    if not isinstance(value, list) or len(value) > 1000:
        raise SelectionError("invalid_codes", "codes 必须是至多 1000 个股票代码的数组")
    if any(not isinstance(c, str) or not CODE.fullmatch(c) for c in value):
        raise SelectionError("invalid_codes", "股票代码必须是六位数字")
    return list(dict.fromkeys(value))


def normalize_definition(value):
    if not isinstance(value, dict):
        raise SelectionError("invalid_definition", "筛选定义必须是对象")
    params = dict(DEFAULT_PARAMETERS)
    supplied = value.get("parameters", {})
    if not isinstance(supplied, dict) or set(supplied) - set(params):
        raise SelectionError("invalid_parameters", "包含未知筛选参数")
    for key, _, _, minimum, maximum in PARAMETERS:
        v = _number(supplied.get(key, params[key]))
        if v is None or not minimum <= v <= maximum or (key == "kdj_cross_days" and not v.is_integer()):
            raise SelectionError("invalid_parameters", f"筛选参数 {key} 超出允许范围")
        params[key] = int(v) if key == "kdj_cross_days" else v
    if params["min_price"] > params["max_price"] or params["turnover_rate_min"] > params["turnover_rate_max"]:
        raise SelectionError("invalid_parameters", "筛选区间下限不能大于上限")
    raw_groups = value.get("groups", [])
    if not isinstance(raw_groups, list) or len(raw_groups) > 30:
        raise SelectionError("invalid_groups", "最多允许 30 个条件组")
    groups, ids = [], set()
    for index, group in enumerate(raw_groups):
        if not isinstance(group, dict):
            raise SelectionError("invalid_groups", "条件组必须是对象")
        gid = str(group.get("id") or f"g{index + 1}")
        if not IDENTIFIER.fullmatch(gid) or gid in ids:
            raise SelectionError("invalid_groups", "条件组 ID 无效或重复")
        ids.add(gid)
        item = {"id": gid, "name": str(group.get("name") or f"条件组 {index + 1}")[:60],
                "enabled": group.get("enabled", True)}
        if not isinstance(item["enabled"], bool):
            raise SelectionError("invalid_groups", "enabled 必须是布尔值")
        for op in ("and", "not"):
            keys = group.get(op, [])
            if not isinstance(keys, list) or len(keys) > 25 or any(not isinstance(k, str) or k not in KEYS for k in keys):
                raise SelectionError("unknown_criterion", "条件组包含未知条件")
            item[op] = list(dict.fromkeys(keys))
        groups.append(item)
    disabled = value.get("disabled", [])
    allowed = {f"{g['id']}|{prefix}{k}" for g in groups
               for prefix, op in (("", "and"), ("not:", "not")) for k in g[op]}
    if not isinstance(disabled, list) or any(not isinstance(k, str) or k not in allowed for k in disabled):
        raise SelectionError("invalid_disabled", "试关条件必须属于对应条件组")
    return {"parameters": params, "groups": groups, "disabled": list(dict.fromkeys(disabled))}


def _effective_groups(definition):
    disabled = set(definition["disabled"])
    return [{**g, "and": [k for k in g["and"] if f"{g['id']}|{k}" not in disabled],
             "not": [k for k in g["not"] if f"{g['id']}|not:{k}" not in disabled]}
            for g in definition["groups"] if g["enabled"]]


def _failures(checks, group):
    return [k for k in group["and"] if checks.get(k) is not True] + [
        "not:" + k for k in group["not"] if checks.get(k) is not False]


def _limit_up_threshold(code, name):
    # Same board-first ordering as the deployed screener's June 11 fix.
    if code.startswith(("300", "301", "302", "688", "689")):
        return 19.9
    if code.startswith(("4", "8", "92")):
        return 29.9
    if "ST" in name.upper() or name.upper().startswith("S"):
        return 4.9
    return 9.9


def criterion_checks(row, technical, fund, sector, params):
    """Preserve nullable checks; parameterized predicates use raw metrics."""
    checks = {key: None for key in KEYS}
    for feature in (technical, fund):
        for key, v in _json(feature.get("checks_json")).items():
            if key in checks and (v is True or v is False):
                checks[key] = v
    tm, fm = _json(technical.get("metrics_json")), _json(fund.get("metrics_json"))
    code, name = str(row["code"]), str(row.get("name") or "")
    price, turnover, change = (_number(row.get(k)) for k in ("price", "turnover_rate", "change_pct"))
    checks["price_below_limit"] = price <= params["max_price"] if price is not None else None
    checks["price_above_min"] = price >= params["min_price"] if price is not None else None
    checks["is_star_market"], checks["is_bse"] = code.startswith("688"), code.startswith("92")
    checks["turnover_in_range"] = params["turnover_rate_min"] <= turnover <= params["turnover_rate_max"] if turnover is not None else None
    up = _number(row.get("up_limit"))
    checks["is_limit_up"] = (price >= up * .999 if up and price is not None else
                             change >= _limit_up_threshold(code, name) if change is not None else None)
    for key in ("hot_sector", "sector_leader"):
        checks[key] = sector.get(key)  # None if the membership dataset is absent.
    profit, conc = _number(tm.get("profit_ratio")), _number(tm.get("chip_concentration"))
    checks["profit_ratio_above"] = profit * 100 >= params["profit_ratio_above_value"] if profit is not None else None
    checks["profit_ratio_below"] = profit * 100 <= params["profit_ratio_below_value"] if profit is not None else None
    checks["chip_concentration_below"] = conc <= params["chip_concentration_max_value"] if conc is not None else None
    ago = _number(tm.get("kdj_cross_days_ago"))
    if ago is not None:
        checks["kdj_recent_cross"] = 0 <= ago < params["kdj_cross_days"]
    elif params["kdj_cross_days"] != DEFAULT_PARAMETERS["kdj_cross_days"]:
        # Alternate legacy sources may omit days_ago: do not invent whether a
        # cached 5-day True matches a newly requested window.
        checks["kdj_recent_cross"] = None
    return checks, {**tm, **fm}


class SelectionService:
    def __init__(self, data_store, state_root):
        self.data_store = data_store
        self.state_root = Path(state_root).resolve()
        self.db_path = self.state_root / "selection.sqlite3"
        self._market_lock = threading.Lock()
        self._market_cache = {}

    def catalog(self):
        return {"schemaVersion": 1,
                "criteria": [{"id": k, "label": label, "category": category, "parameters": p}
                             for k, label, category, p in CRITERIA],
                "parameters": [{"id": k, "label": label, "type": "integer" if k == "kdj_cross_days" else "number",
                                "minimum": low, "maximum": high, "default": default}
                               for k, label, default, low, high in PARAMETERS],
                "defaults": normalize_definition({"groups": [{"id": "g1", "name": "基础筛选",
                              "and": ["price_below_limit", "turnover_in_range"], "not": []}]}),
                "smartAttributes": copy.deepcopy(SMART_ATTRIBUTES), "operations": list(OPERATIONS),
                "logic": "groups_or_conditions_and; exclusion_requires_false; missing_is_unknown",
                "impactMeaning": "关闭单个条件后该组新增通过数"}

    @staticmethod
    def _owner(owner):
        if not isinstance(owner, str) or not owner or len(owner) > 500:
            raise SelectionError("invalid_owner", "缺少有效账户", 401)
        return hashlib.sha256(owner.encode()).hexdigest()

    @staticmethod
    def _empty():
        return {"revision": 0, "asOf": None, "groups": [], "presets": [], "lastRun": None}

    def _load(self, owner, connection=None):
        key = self._owner(owner)
        if connection is None and not self.db_path.exists():
            return self._empty()
        close = connection is None
        if close:
            connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            record = connection.execute("SELECT document FROM libraries WHERE owner=?", (key,)).fetchone()
            return json.loads(record[0]) if record else self._empty()
        finally:
            if close:
                connection.close()

    def _market(self, requested_date=None):
        # Generation publishing swaps stocks.db's target; include that resolved
        # path as well as mtime/size. A 5s ceiling also handles a live WAL writer
        # in fixtures or development without retaining indefinite stale data.
        source = getattr(self.data_store, "db_path", None)
        if source is None:
            return self._read_market(requested_date)
        try:
            resolved = Path(source).resolve()
            stat = resolved.stat()
            key = (str(resolved), stat.st_mtime_ns, stat.st_size, requested_date)
        except OSError:
            return self._read_market(requested_date)
        with self._market_lock:
            cached = self._market_cache.get(key)
            if cached and time.monotonic() - cached[0] < 5:
                return cached[1]
            result = self._read_market(requested_date)
            self._market_cache = {key: (time.monotonic(), result)}
            return result

    def _read_market(self, requested_date=None):
        """One SQLite read transaction, using one published generation only."""
        warnings = []
        try:
            with self.data_store._connect() as conn:
                conn.execute("BEGIN")
                if requested_date:
                    asof = requested_date
                else:
                    dates = conn.execute("SELECT trade_date,COUNT(*) n FROM daily_quotes GROUP BY trade_date ORDER BY trade_date DESC").fetchall()
                    if not dates:
                        raise SelectionError("market_unavailable", "暂无选股行情快照", 503)
                    asof = next((r["trade_date"] for r in dates if r["n"] > 3000), dates[0]["trade_date"])
                rows = [dict(r) for r in conn.execute("SELECT * FROM daily_quotes WHERE trade_date=? ORDER BY code", (asof,))]
                features, sectors = {}, {}
                try:
                    for feature in conn.execute("SELECT code,feature_group,checks_json,metrics_json FROM daily_feature_groups WHERE trade_date=? AND feature_group IN ('technical','fund')", (asof,)):
                        features.setdefault(feature["code"], {})[feature["feature_group"]] = dict(feature)
                except sqlite3.Error:
                    warnings.append("feature_data_unavailable")
                try:
                    sector_rows = conn.execute("SELECT code,sector,is_hot_sector,is_sector_leader FROM sector_membership WHERE trade_date=?", (asof,)).fetchall()
                    for row in rows:
                        sectors[row["code"]] = {"sectors": [], "hot_sector": False, "sector_leader": False}
                    for sr in sector_rows:
                        item = sectors.setdefault(sr["code"], {"sectors": [], "hot_sector": False, "sector_leader": False})
                        if sr["sector"] and sr["sector"] not in item["sectors"]:
                            item["sectors"].append(sr["sector"])
                        item["hot_sector"] |= bool(sr["is_hot_sector"])
                        item["sector_leader"] |= bool(sr["is_sector_leader"])
                    if not sector_rows:
                        sectors = {}
                        warnings.append("sector_data_unavailable")
                except sqlite3.Error:
                    warnings.append("sector_data_unavailable")
                try:
                    limits = {r["code"]: dict(r) for r in conn.execute("SELECT code,up_limit,down_limit FROM daily_limit WHERE trade_date=?", (asof,))}
                    for row in rows:
                        row.update({k: v for k, v in limits.get(row["code"], {}).items() if k != "code"})
                except sqlite3.Error:
                    warnings.append("limit_price_using_board_threshold")
        except sqlite3.Error as exc:
            raise SelectionError("market_unavailable", "选股行情快照不可用", 503) from exc
        return asof, rows, features, sectors, warnings

    def _evaluate(self, definition, market):
        asof, rows, features, sectors, warnings = market
        effective = [g for g in _effective_groups(definition) if g["and"] or g["not"]]
        stats = {g["id"]: {"id": g["id"], "name": g["name"], "enabled": g["enabled"],
                  "baselinePassed": 0, "unknown": 0, "impacts": {k: 0 for k in g["and"]},
                  "notImpacts": {k: 0 for k in g["not"]}} for g in effective}
        and_keys = {k for g in effective for k in g["and"]}
        not_keys = {k for g in effective for k in g["not"]} - and_keys
        items, unknown = [], 0
        for row in rows:
            if not CODE.fullmatch(str(row.get("code", ""))):
                continue
            feature = features.get(row["code"], {})
            sector = sectors.get(row["code"], {})
            checks, metrics = criterion_checks(row, feature.get("technical", {}), feature.get("fund", {}), sector, definition["parameters"])
            matched, possible = [], False
            for group in effective:
                failures = _failures(checks, group)
                acc = stats[group["id"]]
                if not failures:
                    matched.append(group["id"])
                    acc["baselinePassed"] += 1
                else:
                    if len(failures) == 1:
                        f = failures[0]
                        acc["notImpacts" if f.startswith("not:") else "impacts"][f[4:] if f.startswith("not:") else f] += 1
                    could_pass = all(checks.get(k) is not False for k in group["and"]) and all(checks.get(k) is not True for k in group["not"])
                    if could_pass:
                        acc["unknown"] += 1
                        possible = True
            passed = bool(matched) or not effective
            if possible and not passed:
                unknown += 1
            if passed:
                stock = self.data_store._stock(row, " / ".join(sector.get("sectors", [])) or None)
                stock.update({"checks": checks, "passed": True, "matchedGroups": matched,
                              "score": sum(checks[k] is True for k in and_keys) + sum(checks[k] is False for k in not_keys),
                              "required": len(and_keys) + len(not_keys)})
                items.append(stock)
        group_stats = []
        disabled = set(definition["disabled"])
        for original in definition["groups"]:
            item = stats.get(original["id"], {"id": original["id"], "name": original["name"], "enabled": original["enabled"], "baselinePassed": 0, "unknown": 0, "impacts": {}, "notImpacts": {}})
            item["disabled"] = [d for d in disabled if d.startswith(original["id"] + "|")]
            group_stats.append(item)
        return {"asOf": asof, "source": "published_daily_snapshot", "total": len(rows), "passed": len(items),
                "unknown": unknown, "items": items, "groups": group_stats, "warnings": list(warnings),
                "unfiltered": not effective, "definition": definition}

    def evaluate(self, owner, request):
        self._owner(owner)
        if not isinstance(request, dict):
            raise SelectionError("invalid_request", "筛选请求必须是对象")
        library = self._load(owner)
        if request.get("presetId"):
            preset = self._find(library["presets"], request["presetId"], "preset")
            if preset.get("status") == "needs_migration":
                raise SelectionError("needs_migration", "该旧方案含未迁移条件，请先编辑确认", 409)
            definition = normalize_definition(preset["definition"])
        else:
            definition = normalize_definition(request)
        market = self._market()
        result = self._evaluate(definition, market)
        if request.get("groupId"):
            group = self._find(library["groups"], request["groupId"], "group")
            codes, status, warnings = self._group_codes(group, market)
            result["items"] = [r for r in result["items"] if r["code"] in set(codes)]
            result["passed"] = len(result["items"])
            result["groupStatus"] = status
            result["warnings"].extend(warnings)
        query = str(request.get("query") or "").casefold().strip()[:80]
        if query:
            result["items"] = [r for r in result["items"] if query in r["code"] or query in str(r["name"] or "").casefold()]
        sort = request.get("sort", "code")
        if sort not in {"code", "name", "price", "changePct", "turnover", "turnoverRate", "marketCap", "score"}:
            raise SelectionError("invalid_sort", "不支持的排序字段")
        descending = request.get("descending", sort in {"changePct", "turnover", "score"})
        if not isinstance(descending, bool):
            raise SelectionError("invalid_sort", "descending 必须是布尔值")
        known = [r for r in result["items"] if r.get(sort) is not None]
        missing = [r for r in result["items"] if r.get(sort) is None]
        result["items"] = sorted(known, key=lambda r: (r[sort], r["code"]), reverse=descending) + missing
        limit = self._integer(request.get("limit", 100), 1, 1000, "limit")
        offset = self._integer(request.get("offset", 0), 0, 100000, "offset")
        result["matched"] = len(result["items"])
        result["items"] = result["items"][offset:offset + limit]
        result.update({"offset": offset, "limit": limit, "revision": library["revision"]})
        if request.get("includeHistory") is True:
            result["history"] = self._history(definition, market[0])
        return result

    def _history(self, definition, asof):
        """Bounded historical group effectiveness, not an AI ranking backtest.

        As in the original group-stats, a day's selection uses only that day's
        checks/metrics and the current requested thresholds. T+1 return is
        compared to the equal-weight return of stocks with both daily closes.
        Dates without a next close are not treated as zero-return samples.
        """
        with self.data_store._connect() as conn:
            dates = conn.execute("SELECT trade_date,COUNT(*) n FROM daily_quotes WHERE trade_date<=? GROUP BY trade_date ORDER BY trade_date DESC LIMIT 15", (asof,)).fetchall()
        complete = [r["trade_date"] for r in dates if r["n"] > 3000]
        selected = sorted((complete or [r["trade_date"] for r in dates])[:11])
        groups = [g for g in _effective_groups(definition) if g["and"] or g["not"]]
        acc = {g["id"]: {"id": g["id"], "name": g["name"], "sampleDays": 0, "hits": 0,
                          "sumExcess": 0., "sumX": 0., "sumY": 0., "sumY2": 0., "sumXY": 0., "n": 0}
               for g in groups}
        previous = self._market(selected[0]) if selected else None
        for tomorrow in selected[1:]:
            following = self._market(tomorrow)
            prices = {r["code"]: _number(r.get("price")) for r in following[1]}
            returns = {}
            for row in previous[1]:
                p0, p1 = _number(row.get("price")), prices.get(row["code"])
                if p0 is not None and p0 > 0 and p1 is not None and p1 > 0:
                    returns[row["code"]] = (p1 / p0 - 1) * 100
            if returns:
                baseline = sum(returns.values()) / len(returns)
                evaluated = self._evaluate(definition, previous)
                group_codes = {g["id"]: set() for g in groups}
                for item in evaluated["items"]:
                    for gid in item["matchedGroups"]:
                        group_codes[gid].add(item["code"])
                for group in groups:
                    item = acc[group["id"]]
                    hits = group_codes[group["id"]] & set(returns)
                    item["sampleDays"] += 1
                    item["hits"] += len(hits)
                    item["sumExcess"] += sum(returns[c] - baseline for c in hits)
                    for code, ret in returns.items():
                        x, y = int(code in hits), ret - baseline
                        item["sumX"] += x
                        item["sumY"] += y
                        item["sumY2"] += y * y
                        item["sumXY"] += x * y
                        item["n"] += 1
            previous = following
        output = []
        for item in acc.values():
            n, sx, sy = item["n"], item["sumX"], item["sumY"]
            denominator = math.sqrt(max(0., (n * sx - sx * sx) * (n * item["sumY2"] - sy * sy)))
            output.append({"id": item["id"], "name": item["name"], "sampleDays": item["sampleDays"],
                "hits": item["hits"], "averageHits": item["hits"] / item["sampleDays"] if item["sampleDays"] else None,
                "meanExcessPct": item["sumExcess"] / item["hits"] if item["hits"] else None,
                "ic": (n * item["sumXY"] - sx * sy) / denominator if denominator else None})
        return {"asOf": asof, "days": max(0, len(selected) - 1), "groups": output,
                "status": "ready" if len(selected) > 1 else "insufficient_history",
                "method": "T+1 close return minus equal-weight market; historical checks with current thresholds"}

    @staticmethod
    def _integer(value, low, high, key):
        v = _number(value)
        if v is None or not v.is_integer() or not low <= v <= high:
            raise SelectionError("invalid_value", f"{key} 必须在 {low}–{high} 范围")
        return int(v)

    @staticmethod
    def _find(items, item_id, kind):
        for item in items:
            if item["id"] == item_id:
                return item
        raise SelectionError("not_found", f"{kind} 不存在", 404)

    def _rules(self, value):
        if not isinstance(value, dict):
            raise SelectionError("invalid_rules", "智能组必须包含规则")
        match = value.get("match", "all")
        attrs = value.get("attrs", [])
        allowed = {a["id"] for a in SMART_ATTRIBUTES}
        if match not in {"all", "any"} or not isinstance(attrs, list) or any(not isinstance(a, str) or a not in allowed for a in attrs):
            raise SelectionError("invalid_rules", "无效智能组属性")
        result = {"match": match, "attrs": list(dict.fromkeys(attrs)),
                  "limit": self._integer(value.get("limit", 50), 1, 200, "limit")}
        if "definition" in value:
            result["definition"] = normalize_definition(value["definition"])
            if not any(g["and"] or g["not"] for g in _effective_groups(result["definition"])):
                raise SelectionError("invalid_rules", "智能筛选组不能使用空规则或全部禁用的规则")
        if not result["attrs"] and "definition" not in result:
            raise SelectionError("invalid_rules", "智能组至少需要一个属性或筛选条件")
        return result

    def _group_codes(self, group, market):
        if group["kind"] == "manual":
            return list(group["codes"]), "ready", []
        rules = group["rules"]
        unavailable = [a for a in rules["attrs"] if a != "hot_sector"]
        if unavailable:
            # Do not weaken an ALL/ANY expression by quietly dropping legacy
            # AI/holdings terms. The entire group is explicitly blocked.
            return [], "needs_migration", ["smart_attribute_unavailable:" + a for a in unavailable]
        sets = []
        if "hot_sector" in rules["attrs"]:
            if "sector_data_unavailable" in market[4]:
                return [], "data_unavailable", ["sector_data_unavailable"]
            sets.append({code for code, sector in market[3].items() if sector.get("hot_sector") is True})
        if "definition" in rules:
            evaluation = self._evaluate(rules["definition"], market)
            sets.append({r["code"] for r in evaluation["items"]})
        if not sets:
            return [], "invalid_rules", ["smart_rules_empty"]
        codes = set.intersection(*sets) if rules["match"] == "all" else set.union(*sets)
        return sorted(codes)[:rules["limit"]], "ready", []

    def _decorate(self, library):
        result = copy.deepcopy(library)
        market = None
        for group in result["groups"]:
            try:
                if group["kind"] == "smart" and market is None:
                    market = self._market()
                codes, status, warnings = self._group_codes(group, market)
                group.update({"codes": codes, "status": status, "warnings": warnings,
                              "evaluatedAsOf": market[0] if group["kind"] == "smart" else None})
            except SelectionError as exc:
                group.update({"codes": [], "status": "data_unavailable", "warnings": [exc.code]})
        return result

    def load_library(self, owner):
        return self._decorate(self._load(owner))

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 60:
            raise SelectionError("invalid_name", "名称必须为 1–60 个字符")
        return value.strip()

    def _settings(self, value):
        if not isinstance(value, dict):
            raise SelectionError("invalid_settings", "分组设置必须是对象")
        allowed = {"realtimeEnabled", "realtimeIntervalSec", "refreshIntervalSec"}
        if set(value) - allowed:
            raise SelectionError("invalid_settings", "未知分组设置；本版本不启动自动 AI 任务")
        result = {"realtimeEnabled": value.get("realtimeEnabled", True),
                  "realtimeIntervalSec": self._integer(value.get("realtimeIntervalSec", 5), 3, 60, "realtimeIntervalSec"),
                  "refreshIntervalSec": self._integer(value.get("refreshIntervalSec", 60), 10, 3600, "refreshIntervalSec")}
        if not isinstance(result["realtimeEnabled"], bool):
            raise SelectionError("invalid_settings", "realtimeEnabled 必须是布尔值")
        return result

    def _apply(self, library, operation, payload, owner):
        now, extra = _now(), {}
        if operation == "group.create":
            if len(library["groups"]) >= 50:
                raise SelectionError("library_limit", "最多创建 50 个观察组")
            kind = payload.get("kind", "manual")
            if kind not in {"manual", "smart"}:
                raise SelectionError("invalid_kind", "分组类型必须为 manual 或 smart")
            group = {"id": "group-" + uuid.uuid4().hex, "name": self._name(payload.get("name")),
                     "kind": kind, "codes": _codes(payload.get("codes", [])),
                     "settings": self._settings(payload.get("settings", {})), "updatedAt": now}
            if kind == "smart":
                if group["codes"]:
                    raise SelectionError("smart_readonly", "智能组成员由规则计算")
                group["rules"] = self._rules(payload.get("rules"))
            library["groups"].append(group)
            extra["groupId"] = group["id"]
        elif operation == "group.reorder":
            ids = payload.get("ids")
            if not isinstance(ids, list) or any(not isinstance(gid, str) for gid in ids) or len(ids) != len(library["groups"]) or set(ids) != {g["id"] for g in library["groups"]}:
                raise SelectionError("invalid_order", "排序需要每个分组 ID 恰好一次")
            library["groups"] = [self._find(library["groups"], gid, "group") for gid in ids]
        elif operation.startswith("group."):
            ids = payload.get("groupIds") if operation in {"group.add", "group.remove"} else None
            if ids is None:
                ids = [payload.get("id")]
            if not isinstance(ids, list) or not ids or len(ids) > 50 or any(not isinstance(gid, str) for gid in ids):
                raise SelectionError("invalid_groups", "请选择有效观察组")
            groups = [self._find(library["groups"], gid, "group") for gid in dict.fromkeys(ids)]
            for group in groups:
                if operation == "group.delete":
                    library["groups"].remove(group)
                elif operation == "group.update":
                    if "name" in payload:
                        group["name"] = self._name(payload["name"])
                    if "kind" in payload and payload["kind"] != group["kind"]:
                        raise SelectionError("kind_immutable", "请新建分组以改变手动/智能类型")
                    if "settings" in payload:
                        if not isinstance(payload["settings"], dict):
                            raise SelectionError("invalid_settings", "分组设置必须是对象")
                        group["settings"] = self._settings({**group["settings"], **payload["settings"]})
                    if "rules" in payload:
                        if group["kind"] != "smart":
                            raise SelectionError("invalid_rules", "只有智能组可设置规则")
                        group["rules"] = self._rules(payload["rules"])
                elif operation in {"group.add", "group.remove"}:
                    if group["kind"] != "manual":
                        raise SelectionError("smart_readonly", "智能组成员由规则计算，不能手动增删")
                    codes = _codes(payload.get("codes", []))
                    group["codes"] = (_codes(list(dict.fromkeys(group["codes"] + codes))) if operation == "group.add"
                                      else [c for c in group["codes"] if c not in set(codes)])
                elif operation == "group.refresh":
                    codes, status, warnings = self._group_codes(group, self._market())
                    extra["refresh"] = {"groupId": group["id"], "count": len(codes), "status": status, "warnings": warnings}
                group["updatedAt"] = now
        elif operation == "preset.save":
            preset_id = payload.get("id")
            if preset_id:
                preset = self._find(library["presets"], preset_id, "preset")
            else:
                if len(library["presets"]) >= 100:
                    raise SelectionError("library_limit", "最多保存 100 个方案")
                preset = {"id": "preset-" + uuid.uuid4().hex}
                library["presets"].append(preset)
            preset.update({"name": self._name(payload.get("name")),
                           "definition": normalize_definition(payload.get("definition")), "updatedAt": now,
                           "status": "ready", "warnings": []})
            extra["presetId"] = preset["id"]
        elif operation == "preset.delete":
            library["presets"].remove(self._find(library["presets"], payload.get("id"), "preset"))
        elif operation == "preset.run":
            preset = self._find(library["presets"], payload.get("id"), "preset")
            if preset.get("status") == "needs_migration":
                raise SelectionError("needs_migration", "该旧方案含未迁移条件，请先编辑确认", 409)
            evaluation = self._evaluate(normalize_definition(preset["definition"]), self._market())
            library["lastRun"] = {"presetId": preset["id"], "asOf": evaluation["asOf"], "executedAt": now,
                                  "passed": evaluation["passed"], "unknown": evaluation["unknown"]}
            evaluation["items"] = evaluation["items"][:100]
            extra["evaluation"] = evaluation
        elif operation == "legacy.import":
            extra = self._import_apply(library, payload)
        return extra

    def mutate(self, owner, request, *, _internal=False):
        key = self._owner(owner)
        if not isinstance(request, dict):
            raise SelectionError("invalid_request", "操作请求必须是对象")
        request_id, operation = request.get("requestId"), request.get("operation")
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise SelectionError("invalid_request_id", "缺少有效 requestId")
        if operation not in OPERATIONS and not (_internal and operation == "legacy.import"):
            raise SelectionError("unknown_operation", "不支持的操作")
        expected = self._integer(request.get("expectedRevision"), 0, 2**53 - 1, "expectedRevision")
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            raise SelectionError("invalid_payload", "payload 必须是对象")
        try:
            canonical = _canonical(request)
        except (TypeError, ValueError) as exc:
            raise SelectionError("invalid_request", "无效 JSON 请求") from exc
        if len(canonical.encode()) > 128000:
            raise SelectionError("request_too_large", "操作内容过大", 413)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        self.state_root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("CREATE TABLE IF NOT EXISTS libraries(owner TEXT PRIMARY KEY, document TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS receipts(owner TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(owner,request_id))")
            conn.execute("BEGIN IMMEDIATE")
            receipt = conn.execute("SELECT digest,response FROM receipts WHERE owner=? AND request_id=?", (key, request_id)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise SelectionError("request_id_conflict", "同一个 requestId 已用于其他操作", 409)
                result = json.loads(receipt[1])
                result["replayed"] = True
                return result
            library = self._load(owner, conn)
            if library["revision"] != expected:
                raise SelectionConflict(library["revision"])
            extra = self._apply(library, operation, payload, owner)
            library["revision"] += 1
            library["asOf"] = _now()
            result = {"success": True, "revision": library["revision"], "requestId": request_id,
                      "replayed": False, "operation": operation, "library": self._decorate(library), **extra}
            conn.execute("INSERT INTO libraries(owner,document) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET document=excluded.document", (key, _canonical(library)))
            conn.execute("INSERT INTO receipts(owner,request_id,digest,response) VALUES (?,?,?,?)", (key, request_id, digest, _canonical(result)))
            conn.commit()
            return result
        finally:
            conn.close()

    @staticmethod
    def _legacy_definition(settings):
        if not isinstance(settings, dict):
            raise SelectionError("invalid_legacy", "旧筛选配置必须为对象")
        groups = settings.get("criteria_groups")
        if groups is None:
            groups = [{"and": [k for k, enabled in settings.get("criteria", {}).items() if enabled], "not": []}]
        if not isinstance(groups, list):
            raise SelectionError("invalid_legacy", "旧条件组格式不正确")
        normalized, unknown = [], []
        for index, raw in enumerate(groups):
            group = {"and": raw, "not": []} if isinstance(raw, list) else raw
            if not isinstance(group, dict):
                raise SelectionError("invalid_legacy", "旧条件组格式不正确")
            item = {"id": "g" + str(index + 1), "name": str(group.get("name") or f"条件组 {index + 1}"), "and": [], "not": []}
            for op in ("and", "not"):
                for key in group.get(op, []) or []:
                    aliases = ["hot_sector", "sector_leader"] if key == "hot_sector_leader" else ["profit_ratio_above" if key == "profit_ratio_90" else key]
                    for alias in aliases:
                        if alias in KEYS:
                            item[op].append(alias)
                        else:
                            unknown.append(str(alias))
            normalized.append(item)
        parameters = {key: settings[key] for key in DEFAULT_PARAMETERS if key in settings}
        return normalize_definition({"groups": normalized, "parameters": parameters}), list(dict.fromkeys(unknown))

    def _import_apply(self, library, payload):
        """Only invoked by import_legacy, never by the public operation list."""
        imports = library.setdefault("legacyImports", [])
        digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        if digest in imports:
            return {"imported": {"groups": 0, "presets": 0}, "alreadyImported": True}
        groups_count = presets_count = 0
        settings, configs = payload.get("settings", {}), payload.get("configs", {})
        if not isinstance(configs, dict):
            raise SelectionError("invalid_legacy", "旧方案必须为名称映射")
        definitions = [("旧版默认筛选", settings)] if settings else []
        definitions += list(configs.items())
        for name, raw in definitions:
            definition, unknown = self._legacy_definition(raw)
            library["presets"].append({"id": "preset-" + uuid.uuid4().hex,
                "name": str(name)[:60], "definition": definition, "updatedAt": _now(),
                "status": "needs_migration" if unknown else "ready", "warnings": ["unknown_legacy_criterion:" + k for k in unknown],
                "legacyDefinition": copy.deepcopy(raw)})
            presets_count += 1
        watch = payload.get("watchlist", {})
        if isinstance(watch, list):
            tabs = [{"name": "旧版观察池", "codes": watch}]
        elif isinstance(watch, dict):
            tabs = watch.get("tabs")
            if tabs is None:
                tabs = [{"name": "旧版观察池", "codes": watch.get("codes", [])}] if watch else []
        else:
            raise SelectionError("invalid_legacy", "旧观察池格式不正确")
        if not isinstance(tabs, list):
            raise SelectionError("invalid_legacy", "旧观察池 tabs 必须是数组")
        for tab in tabs:
            if not isinstance(tab, dict):
                raise SelectionError("invalid_legacy", "旧观察组格式不正确")
            old_settings = tab.get("settings") or {}
            group = {"id": "group-" + uuid.uuid4().hex, "name": str(tab.get("name") or "旧版观察池")[:60],
                "kind": "smart" if tab.get("smart") else "manual", "codes": [], "updatedAt": _now(),
                "settings": self._settings({"realtimeEnabled": bool(old_settings.get("realtime_enabled", True)),
                    "realtimeIntervalSec": old_settings.get("realtime_interval_sec", 5)}),
                "legacySource": copy.deepcopy(tab)}
            if group["kind"] == "smart":
                group["rules"] = self._rules(tab.get("smart_rules"))
                if any(attr != "hot_sector" for attr in group["rules"]["attrs"]):
                    group["status"] = "needs_migration"
            else:
                group["codes"] = _codes(tab.get("codes", []))
            library["groups"].append(group)
            groups_count += 1
        if len(library["groups"]) > 50 or len(library["presets"]) > 100:
            raise SelectionError("library_limit", "迁移后超过观察组或方案上限，未写入")
        imports.append(digest)
        return {"imported": {"groups": groups_count, "presets": presets_count}}

    def import_legacy(self, owner, payload, request_id):
        """Explicit administrative migration; does not read any old private file.

        Caller supplies a private snapshot {settings, configs, watchlist}. Existing
        native groups/presets are appended to, never overwritten. Legacy source
        is retained in the private account library for later migration review.
        """
        if not isinstance(payload, dict) or set(payload) - {"settings", "configs", "watchlist"}:
            raise SelectionError("invalid_legacy", "仅接受 settings/configs/watchlist")
        # A repeat after another successful mutation needs the original revision
        # in its digest, so resolve its receipt before constructing the request.
        key = self._owner(owner)
        if self.db_path.exists():
            with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
                receipt = conn.execute("SELECT response FROM receipts WHERE owner=? AND request_id=?", (key, request_id)).fetchone()
                if receipt:
                    old = json.loads(receipt[0])
                    expected = old["revision"] - 1
                else:
                    expected = self._load(owner, conn)["revision"]
        else:
            expected = 0
        return self.mutate(owner, {"requestId": request_id, "expectedRevision": expected,
            "operation": "legacy.import", "payload": payload}, _internal=True)

    def move_library(self, source_owner, target_owner):
        """Copy a device library to its verified account only if target is empty.

        Source is retained for recovery. Authentication code alone may call this
        after verifying the identity association; it is not an HTTP operation.
        """
        source, target = self._owner(source_owner), self._owner(target_owner)
        if source == target or not self.db_path.exists():
            return {"success": True, "copied": False}
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            src = self._load(source_owner, conn)
            dst = self._load(target_owner, conn)
            if not src["groups"] and not src["presets"]:
                return {"success": True, "copied": False}
            if dst["groups"] or dst["presets"] or dst["revision"]:
                raise SelectionConflict(dst["revision"], "账户已有资料；设备资料保留，需要手动合并")
            copied = copy.deepcopy(src)
            copied["revision"] += 1
            copied["asOf"] = _now()
            conn.execute("INSERT INTO libraries(owner,document) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET document=excluded.document", (target, _canonical(copied)))
            conn.commit()
            return {"success": True, "copied": True, "revision": copied["revision"]}
