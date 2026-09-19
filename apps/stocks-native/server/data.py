"""Read-only view of the existing stock snapshot and daily quote database.

The constructor does not open or create any files. Public methods are synchronous;
an async web server should use ``await asyncio.to_thread(store.list_stocks, ...)``.
No production application code is imported and no collection jobs are started.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class DataUnavailable(RuntimeError):
    """The source snapshot cannot be read; callers should return HTTP 503."""


class StockNotFound(LookupError):
    """The code is valid but absent from the source snapshot."""


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _limit(value: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("limit must be an integer") from exc


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _json_array(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


class StockDataStore:
    def __init__(self, data_root: str | os.PathLike[str] | None = None):
        self.root = Path(data_root or os.environ.get("STOCKS_DATA_DIR", "/root/webapp/data/stocks")).resolve()
        self.snapshot_path = self.root / "stocks.json"
        self.db_path = self.root / "stocks.db"
        self._lock = threading.Lock()
        self._signature: tuple[int, int] | None = None
        self._snapshot: dict[str, Any] | None = None

    def _read_snapshot(self) -> dict[str, Any]:
        with self._lock:
            try:
                stat = self.snapshot_path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if signature == self._signature and self._snapshot is not None:
                    return self._snapshot
                with self.snapshot_path.open("r", encoding="utf-8") as source:
                    payload = json.load(source)
                if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                    raise ValueError("expected a rows array")
                if payload.get("ok") is False:
                    raise ValueError("snapshot reports a failed collection")
                rows = [row for row in payload["rows"] if isinstance(row, dict)
                        and re.fullmatch(r"[0-9]{6}", str(row.get("code", "")))]
                snapshot = {
                    "asOf": payload.get("generated_at"),
                    "source": payload.get("source"),
                    "rows": rows,
                    "byCode": {str(row["code"]): row for row in rows},
                }
            except (OSError, ValueError, TypeError) as exc:
                # Never silently serve a cached value as though it were fresh.
                raise DataUnavailable("Stock snapshot is unavailable") from exc
            self._signature = signature
            self._snapshot = snapshot
            return snapshot

    @contextmanager
    def _connect(self):
        # mode=ro is intentional. immutable=1 would ignore updates by the existing
        # production writer. Do not set journal_mode or create production indexes.
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 2000")
            connection.row_factory = sqlite3.Row
            yield connection
        finally:
            connection.close()

    def _sectors(self, codes: list[str]) -> tuple[dict[str, str], list[str]]:
        if not codes:
            return {}, []
        try:
            with self._connect() as connection:
                placeholders = ",".join("?" for _ in codes)
                rows = connection.execute(
                    "SELECT code, industry FROM stock_industries "
                    f"WHERE code IN ({placeholders}) ORDER BY code, industry", codes
                ).fetchall()
            sectors: dict[str, list[str]] = {}
            for row in rows:
                if row["industry"]:
                    sectors.setdefault(row["code"], []).append(row["industry"])
            return {code: " / ".join(names) for code, names in sectors.items()}, []
        except sqlite3.Error:
            return {}, ["sector_data_unavailable"]

    @staticmethod
    def _stock(row: dict[str, Any], sector: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {"code": str(row["code"]), "name": row.get("name"), "sector": sector}
        for output, source in (
            ("price", "price"), ("changePct", "change_pct"),
            ("changeAmount", "change_amount"), ("turnover", "turnover"),
            ("turnoverRate", "turnover_rate"), ("open", "open"),
            ("high", "high"), ("low", "low"), ("prevClose", "prev_close"),
            ("volume", "volume"), ("marketCap", "market_cap"),
            ("floatMarketCap", "float_market_cap"), ("amplitude", "amplitude"),
            ("volumeRatio", "volume_ratio"), ("peDynamic", "pe_dynamic"),
            ("pb", "pb"), ("speed", "speed"), ("change5m", "change_5m"),
            ("change60d", "change_60d"), ("changeYtd", "change_ytd"),
            ("upLimit", "up_limit"), ("downLimit", "down_limit"),
            ("innerVolume", "inner_vol"), ("outerVolume", "outer_vol"),
        ):
            result[output] = _number(row.get(source))
        for output, source in (("bids", "bids"), ("asks", "asks")):
            value = row.get(source)
            result[output] = value if isinstance(value, list) else []
        return result

    @staticmethod
    def overlay_live(result: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
        if not live:
            return result
        for key, value in live.items():
            if key not in {"code"} and value is not None:
                result[key] = value
        return result

    def list_stocks(self, q: str = "", limit: int = 50) -> dict[str, Any]:
        maximum = _limit(limit, 100)
        query = str(q or "").strip().casefold()[:80]
        snapshot = self._read_snapshot()
        rows = snapshot["rows"]
        if query:
            rows = [row for row in rows if query in str(row["code"]).casefold()
                    or query in str(row.get("name") or "").casefold()]
        matched = rows[:maximum]
        sectors, warnings = self._sectors([str(row["code"]) for row in matched])
        return {
            "asOf": snapshot["asOf"], "source": snapshot["source"], "total": len(rows),
            "items": [self._stock(row, sectors.get(str(row["code"]))) for row in matched],
            "warnings": warnings,
        }

    def stock_detail(self, code: str, candle_limit: int = 180) -> dict[str, Any]:
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("code must contain six digits")
        maximum = _limit(candle_limit, 260)
        snapshot = self._read_snapshot()
        row = snapshot["byCode"].get(code)
        if row is None:
            raise StockNotFound(code)
        sectors, warnings = self._sectors([code])
        # Bound both the output and source date range. A stale snapshot remains
        # dated honestly instead of moving its window to today's date.
        try:
            reference_date = date.fromisoformat(str(snapshot["asOf"])[:10])
        except ValueError:
            reference_date = datetime.now().date()
        earliest = (reference_date - timedelta(days=550)).isoformat()
        candles: list[dict[str, Any]] = []
        omitted = 0
        try:
            with self._connect() as connection:
                quotes = connection.execute(
                    "SELECT trade_date, open, high, low, price, volume FROM daily_quotes "
                    "WHERE code = ? AND trade_date >= ? AND trade_date <= ? "
                    "ORDER BY trade_date DESC LIMIT ?",
                    (code, earliest, reference_date.isoformat(), maximum),
                ).fetchall()
            for quote in reversed(quotes):
                values = {key: _number(quote[column]) for key, column in
                          (("open", "open"), ("high", "high"), ("low", "low"), ("close", "price"))}
                # A chart candle needs all four prices. Incomplete rows are
                # explicitly counted; never invent a price or draw a zero candle.
                if any(value is None or value <= 0 for value in values.values()):
                    omitted += 1
                    continue
                if values["high"] < max(values["open"], values["close"], values["low"]) or \
                        values["low"] > min(values["open"], values["close"]):
                    omitted += 1
                    continue
                candles.append({"time": quote["trade_date"], **values, "volume": _number(quote["volume"])})
        except sqlite3.Error:
            warnings.append("candle_data_unavailable")
        if omitted:
            warnings.append("incomplete_candles_omitted")
        panels = self.stock_panels(code)
        warnings.extend(panel for panel in panels.pop("warnings", []) if panel not in warnings)
        return {
            "asOf": snapshot["asOf"], "source": snapshot["source"],
            "stock": self._stock(row, sectors.get(code)), "candles": candles,
            "candlesAsOf": candles[-1]["time"] if candles else None,
            "omittedCandles": omitted, "warnings": warnings, **panels,
        }

    def market_overview(self) -> dict[str, Any]:
        snapshot = self._read_snapshot()
        rows = snapshot["rows"]
        rising = falling = flat = limit_up = limit_down = 0
        turnover = 0.0
        for row in rows:
            change = _number(row.get("change_pct"))
            price = _number(row.get("price"))
            upper = _number(row.get("up_limit"))
            lower = _number(row.get("down_limit"))
            amount = _number(row.get("turnover"))
            turnover += amount or 0.0
            if change is not None:
                if change > 0.001:
                    rising += 1
                elif change < -0.001:
                    falling += 1
                else:
                    flat += 1
            if price is not None and ((upper is not None and price >= upper - 0.005) or (upper is None and change is not None and change >= 9.8)):
                limit_up += 1
            if price is not None and ((lower is not None and price <= lower + 0.005) or (lower is None and change is not None and change <= -9.8)):
                limit_down += 1
        warnings: list[str] = []
        north = south = None
        hot_sectors: list[dict[str, Any]] = []
        try:
            with self._connect() as connection:
                flow = connection.execute(
                    "SELECT trade_date, north_money, south_money FROM daily_hsgt_total "
                    "ORDER BY trade_date DESC LIMIT 1"
                ).fetchone()
                if flow:
                    north, south = _number(flow["north_money"]), _number(flow["south_money"])
                sectors = connection.execute(
                    "SELECT trade_date, sector_code, sector_name, pct_change, net_amount, net_amount_rate "
                    "FROM daily_sector_flow WHERE trade_date = (SELECT MAX(trade_date) FROM daily_sector_flow) "
                    "ORDER BY ABS(COALESCE(net_amount, 0)) DESC LIMIT 8"
                ).fetchall()
                hot_sectors = [{
                    "code": row["sector_code"], "name": row["sector_name"],
                    "changePct": _number(row["pct_change"]), "netAmount": _number(row["net_amount"]),
                    "netAmountRate": _number(row["net_amount_rate"]), "tradeDate": row["trade_date"],
                } for row in sectors]
        except sqlite3.Error:
            warnings.append("market_overview_database_unavailable")
        return {
            "asOf": snapshot["asOf"], "rising": rising, "falling": falling,
            "flat": flat, "limitUp": limit_up, "limitDown": limit_down,
            "turnover": turnover, "northMoney": north, "southMoney": south,
            "hotSectors": hot_sectors, "warnings": warnings,
        }

    def stock_panels(self, code: str) -> dict[str, Any]:
        warnings: list[str] = []
        technical: dict[str, Any] | None = None
        fund: dict[str, Any] | None = None
        chips: dict[str, Any] | None = None
        peers: list[dict[str, Any]] = []
        announcements: list[dict[str, Any]] = []
        concepts: list[str] = []
        signals: dict[str, Any] = {"topList": [], "northbound": [], "limits": []}
        try:
            with self._connect() as connection:
                for group in ("technical", "fund"):
                    group_rows = connection.execute(
                        "SELECT trade_date, checks_json, metrics_json FROM daily_feature_groups "
                        "WHERE code = ? AND feature_group = ? AND status = 'done' "
                        "ORDER BY trade_date DESC LIMIT 30", (code, group)
                    ).fetchall()
                    history = [{"tradeDate": row["trade_date"], **_json_object(row["metrics_json"])}
                               for row in reversed(group_rows)]
                    payload = ({"asOf": group_rows[0]["trade_date"],
                                "metrics": _json_object(group_rows[0]["metrics_json"]),
                                "checks": _json_object(group_rows[0]["checks_json"]),
                                "history": history} if group_rows else None)
                    if group == "technical":
                        technical = payload
                    else:
                        fund = payload

                chip = connection.execute(
                    "SELECT * FROM daily_chips WHERE code = ? ORDER BY trade_date DESC LIMIT 1", (code,)
                ).fetchone()
                if chip:
                    chips = {"asOf": chip["trade_date"], "low": _number(chip["his_low"]),
                             "high": _number(chip["his_high"]), "cost5": _number(chip["cost_5pct"]),
                             "cost15": _number(chip["cost_15pct"]), "cost50": _number(chip["cost_50pct"]),
                             "cost85": _number(chip["cost_85pct"]), "cost95": _number(chip["cost_95pct"]),
                             "average": _number(chip["weight_avg"]), "winnerRate": _number(chip["winner_rate"])}

                concepts = [row["concept"] for row in connection.execute(
                    "SELECT DISTINCT concept FROM stock_concepts WHERE code = ? AND concept <> '' ORDER BY concept LIMIT 16",
                    (code,)
                ).fetchall()]

                industry = connection.execute(
                    "SELECT industry FROM stock_industries WHERE code = ? AND industry <> '' ORDER BY industry LIMIT 1",
                    (code,)
                ).fetchone()
                if industry:
                    peer_rows = connection.execute(
                        "SELECT q.code, q.name, q.price, q.change_pct, q.turnover_rate, q.market_cap "
                        "FROM stock_industries i JOIN daily_quotes q ON q.code = i.code "
                        "AND q.trade_date = (SELECT MAX(q2.trade_date) FROM daily_quotes q2 WHERE q2.code = q.code) "
                        "WHERE i.industry = ? AND i.code <> ? ORDER BY q.change_pct DESC LIMIT 10",
                        (industry["industry"], code)
                    ).fetchall()
                    peers = [{"code": row["code"], "name": row["name"], "price": _number(row["price"]),
                              "changePct": _number(row["change_pct"]),
                              "turnoverRate": _number(row["turnover_rate"]),
                              "marketCap": _number(row["market_cap"])} for row in peer_rows]

                news = connection.execute(
                    "SELECT items_json, fetched_date FROM daily_news WHERE code = ? ORDER BY fetched_date DESC LIMIT 1",
                    (code,)
                ).fetchone()
                if news:
                    announcements = [{"title": item.get("title"), "date": item.get("date"),
                                      "category": item.get("column"), "url": item.get("url")}
                                     for item in _json_array(news["items_json"])[:20]]

                for key, sql in (
                    ("topList", "SELECT trade_date, reason, net_amount, net_rate FROM daily_top_list WHERE code = ? ORDER BY trade_date DESC LIMIT 5"),
                    ("northbound", "SELECT trade_date, rank, amount, net_amount, buy, sell FROM daily_hsgt_top10 WHERE code = ? ORDER BY trade_date DESC LIMIT 5"),
                    ("limits", "SELECT trade_date, up_limit, down_limit FROM daily_limit WHERE code = ? ORDER BY trade_date DESC LIMIT 5"),
                ):
                    signals[key] = [dict(row) for row in connection.execute(sql, (code,)).fetchall()]
        except sqlite3.Error:
            warnings.append("stock_panel_database_unavailable")
        return {"technical": technical, "fund": fund, "chips": chips,
                "peers": peers, "concepts": concepts, "announcements": announcements,
                "signals": signals, "warnings": warnings}

    def chip_history(self, code: str, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("code must contain six digits")
        for value in (start, end):
            if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("chip range must use YYYY-MM-DD")
        today = datetime.now(timezone(timedelta(hours=8))).date()
        last = date.fromisoformat(end) if end else today
        first = date.fromisoformat(start) if start else last - timedelta(days=549)
        if last > today or first > last or (last - first).days > 549:
            raise ValueError("chip range must be at most 550 days and not in the future")
        snapshot = self._read_snapshot()
        if code not in snapshot['byCode']:
            raise StockNotFound(code)
        history = []
        warning = None
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT trade_date, open, high, low, price AS close, volume, turnover_rate "
                    "FROM daily_quotes WHERE code = ? AND trade_date BETWEEN ? AND ? "
                    "ORDER BY trade_date DESC LIMIT ?",
                    (code, first.isoformat(), last.isoformat(), 550 if start else 90),
                ).fetchall()
                history = [dict(row) for row in reversed(rows)]
        except sqlite3.Error:
            warning = 'chip_history_unavailable'
        return {'code': code, 'start': first.isoformat() if start else (
                    history[0]['trade_date'] if history else (last - timedelta(days=89)).isoformat()),
                'end': last.isoformat(), 'history': history, 'warning': warning}
