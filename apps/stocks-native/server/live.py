"""Small async Tencent market-data adapter for the native gateway.

Only public quote endpoints are used. Results are cached in-process so App
refreshes do not multiply upstream traffic. The existing collector remains the
source of truth for durable daily data.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import math
import time
from typing import Any

import aiohttp


HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://stockapp.finance.qq.com/"}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _secid(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def parse_quote_payload(payload: bytes) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for line in payload.decode("gbk", errors="replace").splitlines():
        if "=" not in line:
            continue
        parts = line.split("=", 1)[1].strip().rstrip(";").strip('"').split("~")
        if len(parts) < 50 or len(parts[2]) != 6:
            continue
        code = parts[2]
        try:
            quote_time = datetime.strptime(parts[30], '%Y%m%d%H%M%S').replace(
                tzinfo=timezone(timedelta(hours=8))).isoformat()
        except ValueError:
            quote_time = None
        amount = _number(parts[37])
        float_cap = _number(parts[44])
        market_cap = _number(parts[45])
        bids = [{"price": _number(parts[9 + index * 2]), "volume": _number(parts[10 + index * 2])}
                for index in range(5)]
        asks = [{"price": _number(parts[19 + index * 2]), "volume": _number(parts[20 + index * 2])}
                for index in range(5)]
        output[code] = {
            "code": code, "name": parts[1], "price": _number(parts[3]),
            "quoteTime": quote_time, "quoteSource": "tencent",
            "prevClose": _number(parts[4]), "open": _number(parts[5]),
            "volume": _number(parts[6]), "outerVolume": _number(parts[7]),
            "innerVolume": _number(parts[8]), "changeAmount": _number(parts[31]),
            "changePct": _number(parts[32]), "high": _number(parts[33]),
            "low": _number(parts[34]),
            "turnover": amount * 10_000 if amount is not None else None,
            "turnoverRate": _number(parts[38]), "peDynamic": _number(parts[39]),
            "amplitude": _number(parts[43]),
            "floatMarketCap": float_cap * 100_000_000 if float_cap is not None else None,
            "marketCap": market_cap * 100_000_000 if market_cap is not None else None,
            "pb": _number(parts[46]), "upLimit": _number(parts[47]),
            "downLimit": _number(parts[48]), "volumeRatio": _number(parts[49]),
            "bids": bids, "asks": asks,
        }
    return output


def parse_minute_payload(payload: dict[str, Any], code: str) -> dict[str, Any]:
    secid = _secid(code)
    root = (payload.get("data") or {}).get(secid) or {}
    minute = root.get("data") or {}
    quote = (root.get("qt") or {}).get(secid) or []
    previous_close = _number(quote[4]) if len(quote) > 4 else _number(minute.get("pre_close"))
    rows = []
    previous_volume = 0.0
    for raw in minute.get("data") or []:
        fields = raw.split()
        if len(fields) < 3:
            continue
        price = _number(fields[1])
        cumulative_volume = _number(fields[2]) or 0.0
        cumulative_turnover = _number(fields[3]) if len(fields) > 3 else None
        if price is None:
            continue
        average = (cumulative_turnover / (cumulative_volume * 100.0)
                   if cumulative_turnover is not None and cumulative_volume > 0 else None)
        rows.append({
            "time": f"{fields[0][:2]}:{fields[0][2:]}", "price": price,
            "volume": max(0.0, cumulative_volume - previous_volume),
            "averagePrice": round(average, 3) if average is not None else None,
        })
        previous_volume = cumulative_volume
    return {"code": code, "tradeDate": minute.get("date") or "",
            "previousClose": previous_close, "rows": rows}


class LiveMarketSource:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10, connect=4)
            self._session = aiohttp.ClientSession(timeout=timeout, headers=HEADERS)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _cached(self, key: str, ttl: float, loader):
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < ttl:
                return cached[1]
            value = await loader()
            self._cache[key] = (time.monotonic(), value)
            if len(self._cache) > 256:
                for old_key in list(self._cache)[:64]:
                    self._cache.pop(old_key, None)
            return value

    async def quotes(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        unique = sorted({code for code in codes if len(code) == 6})[:100]
        if not unique:
            return {}
        key = "quotes:" + ",".join(unique)

        async def load():
            client = await self._client()
            url = "https://qt.gtimg.cn/q=" + ",".join(_secid(code) for code in unique)
            async with client.get(url) as response:
                response.raise_for_status()
                return parse_quote_payload(await response.read())
        return await self._cached(key, 5, load)

    async def minute(self, code: str) -> dict[str, Any]:
        async def load():
            client = await self._client()
            url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={_secid(code)}"
            async with client.get(url) as response:
                response.raise_for_status()
                return parse_minute_payload(await response.json(content_type=None), code)
        return await self._cached("minute:" + code, 10, load)

    async def kline(self, code: str, period: str, count: int) -> dict[str, Any]:
        if period not in {"m5", "m15", "m30", "m60", "day", "week", "month"}:
            raise ValueError("unsupported chart period")
        count = max(20, min(int(count), 320))

        async def load():
            client = await self._client()
            secid = _secid(code)
            paths = [
                ("https://ifzq.gtimg.cn/appstock/app/kline/mkline", f"{secid},{period},,{count}"),
                ("https://web.ifzq.gtimg.cn/appstock/app/kline/mkline", f"{secid},{period},,{count}"),
                ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", f"{secid},{period},,,{count},qfq"),
            ]
            for url, parameter in paths:
                try:
                    async with client.get(url, params={"param": parameter}, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            continue
                        response.raise_for_status()
                        root = ((await response.json(content_type=None)).get("data") or {}).get(secid) or {}
                        raw = root.get(period) or root.get("qfq" + period) or []
                        rows = []
                        for item in raw:
                            if len(item) < 6:
                                continue
                            values = [_number(item[index]) for index in range(1, 6)]
                            if any(value is None for value in values[:4]):
                                continue
                            rows.append({"time": item[0], "open": values[0], "close": values[1],
                                         "high": values[2], "low": values[3], "volume": values[4]})
                        if rows:
                            return {"code": code, "period": period, "rows": rows}
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    continue
            return {"code": code, "period": period, "rows": []}
        return await self._cached(f"kline:{code}:{period}:{count}", 60, load)
