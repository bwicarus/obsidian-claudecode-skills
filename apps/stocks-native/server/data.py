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
from datetime import date, datetime, timedelta
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
        ):
            result[output] = _number(row.get(source))
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
        return {
            "asOf": snapshot["asOf"], "source": snapshot["source"],
            "stock": self._stock(row, sectors.get(code)), "candles": candles,
            "candlesAsOf": candles[-1]["time"] if candles else None,
            "omittedCandles": omitted, "warnings": warnings,
        }
