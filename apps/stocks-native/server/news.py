"""Bounded, non-AI news fetching and read-only legacy expert history.

Only fixed publisher endpoints are fetched. Cached data retains its original
retrieval/publication time; failed refreshes never masquerade as fresh news.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import aiohttp
from yarl import URL

from data import _announcement_url, _number

CHINA = timezone(timedelta(hours=8))
MAX_CACHE_KEYS = 48
MAX_ITEMS = 60
MAX_RESPONSE_BYTES = 2_000_000


def _text(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()[:limit]


def _timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            result = datetime.fromtimestamp(float(value), timezone.utc)
        else:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if result.tzinfo is None:
                result = result.replace(tzinfo=CHINA)
        return result.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _safe_url(raw: Any) -> str | None:
    try:
        value = urlsplit(str(raw or ""))
        host = (value.hostname or "").lower()
        allowed = any(host == domain or host.endswith("." + domain)
                      for domain in ("sina.com.cn", "sina.cn", "eastmoney.com"))
        if not allowed or value.scheme not in {"http", "https"} or value.username or value.password \
                or value.port not in {None, 80, 443}:
            return None
        return urlunsplit(("https", host, value.path, value.query, ""))
    except ValueError:
        return None


def _item(title: Any, published: Any, source: Any, url: Any, *, category: str,
          summary: Any = "", code: str | None = None, sector: str | None = None,
          legacy: bool = False) -> dict[str, Any] | None:
    title = _text(title, 240)
    if not title:
        return None
    stamp = _timestamp(published)
    link = _safe_url(url)
    identifier = hashlib.sha256(f"{category}|{code}|{link}|{title}|{stamp}".encode()).hexdigest()[:24]
    return {"id": identifier, "title": title, "summary": _text(summary, 500),
            "publishedAt": stamp, "source": _text(source, 100) or "来源未标注",
            "url": link, "category": category, "code": code, "sector": sector,
            "isLegacy": legacy}


class NewsService:
    def __init__(self, data_store, state_dir: str | Path):
        self.data = data_store
        self.cache_path = Path(state_dir) / "news-cache.json"
        self._cache: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(3)
        try:
            if self.cache_path.stat().st_size <= 8_000_000:
                cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(cache, dict):
                    self._cache = {key: entry for key, entry in list(cache.items())[-MAX_CACHE_KEYS:]
                                   if isinstance(entry, dict) and isinstance(entry.get("items"), list)
                                   and isinstance(entry.get("fetchedEpoch"), (int, float))}
        except (OSError, ValueError, TypeError):
            pass

    async def close(self):
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._session:
            await self._session.close()
            self._session = None

    async def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        if url == "https://search-api-web.eastmoney.com/search/jsonp":
            # This publisher returns HTTP 406 for aiohttp's HTTP negotiation.
            # The ordinary standard-library request matches its public search page.
            async with self._semaphore:
                return await asyncio.to_thread(self._search_json, url, params)
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12, connect=5),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        async with self._semaphore:
            # The publisher accepts percent-encoded JSON; form '+' encoding is rejected.
            target = URL(url + "?" + urlencode(params, quote_via=quote), encoded=True)
            referer = "https://so.eastmoney.com/" if "eastmoney.com" in url else "https://finance.sina.com.cn/"
            async with self._session.get(target, headers={"Referer": referer}, allow_redirects=False) as response:
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(32_768):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise ValueError("publisher response exceeded limit")
                text = chunks.decode("utf-8-sig")
                if text.startswith("stocksNews(") and text.rstrip().endswith(")"):
                    text = text[len("stocksNews("):].rstrip()[:-1]
                return json.loads(text)

    @staticmethod
    def _search_json(url: str, params: dict[str, Any]) -> Any:
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        target = url + "?" + urlencode(params, quote_via=quote)
        request = Request(target, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"})
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("publisher response exceeded limit")
        text = raw.decode("utf-8-sig").strip()
        if text.startswith("stocksNews(") and text.endswith(")"):
            text = text[len("stocksNews("):-1]
        return json.loads(text)

    async def _fetch_public(self, category: str, code: str | None, sector: str | None) -> list[dict]:
        items = []
        if category == "macro":
            payload = await self._get_json("https://feed.mix.sina.com.cn/api/roll/get",
                                           {"pageid": 153, "lid": 2509, "num": MAX_ITEMS, "page": 1})
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            if result.get("status", {}).get("code") != 0 or not isinstance(result.get("data"), list):
                raise ValueError("publisher returned no news list")
            for row in result["data"]:
                if not isinstance(row, dict):
                    continue
                item = _item(row.get("title"), row.get("ctime"), row.get("media_name") or "新浪财经",
                             row.get("url"), category=category, summary=row.get("intro"))
                if item:
                    items.append(item)
        else:
            query = code if category == "stock" else sector
            parameters = {"uid": "", "keyword": query, "type": ["cmsArticleWebOld"],
                          "client": "web", "clientType": "web", "clientVersion": "curr",
                          "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                    "pageIndex": 1, "pageSize": MAX_ITEMS, "preTag": "", "postTag": ""}}}
            payload = await self._get_json("https://search-api-web.eastmoney.com/search/jsonp",
                                           {"cb": "stocksNews", "param": json.dumps(parameters, ensure_ascii=False)})
            rows = payload.get("result", {}).get("cmsArticleWebOld") if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(rows, list):
                raise ValueError("publisher returned no news list")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = _item(row.get("title"), row.get("date"), row.get("mediaName") or "东方财富",
                             row.get("url"), category=category, summary=row.get("content"), code=code, sector=sector)
                if item:
                    items.append(item)
        return items

    def _local_items(self, category: str, code: str | None, sector: str | None) -> tuple[list[dict], list[str]]:
        items, warnings = [], []
        if category == "stock":
            try:
                with self.data._connect() as connection:
                    row = connection.execute("SELECT items_json, fetched_date FROM daily_news WHERE code=? "
                                             "ORDER BY fetched_date DESC LIMIT 1", (code,)).fetchone()
                if row:
                    for raw in json.loads(row["items_json"]):
                        if not isinstance(raw, dict):
                            continue
                        item = _item(raw.get("title"), raw.get("date"), "公司公告 · 东方财富",
                                     _announcement_url(raw), category=category, code=code,
                                     summary=raw.get("column"))
                        if item:
                            items.append(item)
            except (sqlite3.Error, OSError, TypeError, ValueError):
                warnings.append("announcement_cache_unavailable")
        else:
            # Optional old generated summaries are never called an up-to-date feed.
            path = self.data.root / ("macro_news.json" if category == "macro" else "sector_news.json")
            try:
                if path.stat().st_size > 1_000_000:
                    return [], ["legacy_news_cache_too_large"]
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload = payload if category == "macro" else payload.get(sector, {})
                if isinstance(payload, dict) and payload.get("text"):
                    item = _item("旧版宏观新闻摘要" if category == "macro" else f"旧版板块摘要 · {sector}",
                                 payload.get("ts"), "旧版 AI 新闻摘要", None, category=category,
                                 summary=payload["text"], sector=sector, legacy=True)
                    if item:
                        items.append(item)
                        warnings.append("legacy_ai_summary_not_live_news")
            except (OSError, ValueError, TypeError):
                pass
        return items, warnings

    def _persist(self):
        ordered = sorted(self._cache.items(), key=lambda entry: entry[1].get("fetchedEpoch", 0))
        self._cache = dict(ordered[-MAX_CACHE_KEYS:])
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_name(f".news-cache-{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(self._cache, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(self.cache_path)
        except (OSError, ValueError):
            pass  # The bounded in-memory cache remains usable.

    async def _refresh(self, key: str, category: str, code: str | None, sector: str | None):
        now = time.time()
        previous = self._cache.get(key)
        local, warnings = await asyncio.to_thread(self._local_items, category, code, sector)
        try:
            public = await self._fetch_public(category, code, sector)
            # Existing summaries are fallback only; no mixing old AI copy into a fresh feed.
            items = public + [item for item in local if not item["isLegacy"]]
            by_id = {item["id"]: item for item in items}
            entry = {"items": sorted(by_id.values(), key=lambda item: item.get("publishedAt") or "", reverse=True)[:MAX_ITEMS],
                     "fetchedEpoch": now, "attemptEpoch": now, "failed": False,
                     "warnings": [w for w in warnings if w != "legacy_ai_summary_not_live_news"]}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, KeyError):
            if previous and previous.get("items"):
                entry = {**previous, "attemptEpoch": now, "failed": True,
                         "warnings": list(dict.fromkeys(previous.get("warnings", []) + ["news_refresh_failed_cached_data"]))}
            else:
                entry = {"items": local[:MAX_ITEMS], "fetchedEpoch": 0, "attemptEpoch": now,
                         "failed": True, "warnings": warnings + ["news_source_unavailable"]}
        self._cache[key] = entry
        self._persist()
        return entry

    async def feed(self, category: str = "macro", code: str | None = None, sector: str | None = None,
                   limit: int = 30, refresh: bool = False) -> dict[str, Any]:
        if category not in {"macro", "sector", "stock"}:
            raise ValueError("category must be macro, sector or stock")
        if category == "stock" and (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)):
            raise ValueError("stock news requires a six digit code")
        if category == "sector" and (not isinstance(sector, str) or not 1 <= len(sector.strip()) <= 80):
            raise ValueError("sector news requires a sector name of up to 80 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ITEMS:
            raise ValueError("limit must be between 1 and 60")
        code = code if category == "stock" else None
        sector = sector.strip() if category == "sector" else None
        key = f"{category}:{code or sector or ''}"
        now = time.time()
        entry = self._cache.get(key)
        age = now - entry.get("fetchedEpoch", 0) if entry else float("inf")
        retry_age = now - entry.get("attemptEpoch", 0) if entry else float("inf")
        ttl = 600 if category == "stock" else 300
        fetched = False
        if not entry or ((refresh or age > ttl or entry.get("failed")) and retry_age > (60 if entry and entry.get("failed") else 30)):
            task = self._tasks.get(key)
            if task is None:
                task = asyncio.create_task(self._refresh(key, category, code, sector))
                self._tasks[key] = task
            try:
                entry = await asyncio.shield(task)
                fetched = True
            finally:
                if task.done():
                    self._tasks.pop(key, None)
        items = entry.get("items", [])[:limit]
        as_of = max((item.get("publishedAt") or "" for item in items), default="") or None
        warnings = list(entry.get("warnings", []))
        if as_of and datetime.now(timezone.utc) - datetime.fromisoformat(as_of.replace("Z", "+00:00")) > timedelta(days=7):
            warnings.append("latest_news_older_than_7_days")
        status = ("stale" if items else "unavailable") if entry.get("failed") else ("fresh" if fetched else "cached")
        return {"category": category, "code": code, "sector": sector, "items": items,
                "fetchedAt": _timestamp(entry.get("fetchedEpoch")) if entry.get("fetchedEpoch") else None,
                "asOf": as_of, "status": status, "warnings": list(dict.fromkeys(warnings))}

    def legacy_signals(self, code: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Shared old expert history is informational, never an active user rule."""
        if code is not None and not re.fullmatch(r"[0-9]{6}", str(code)):
            raise ValueError("code must contain six digits")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        items, warnings = [], []
        definitions = (("expert_signals", "signal", "ts"), ("expert_conclusions", "conclusion", "ts"),
                       ("expert_watch_levels", "watchLevel", "created_at"), ("expert_circuit", "circuit", "updated_at"))
        try:
            with self.data._connect() as connection:
                for table, kind, clock in definitions:
                    try:
                        where, args = (" WHERE code = ?", [code]) if code else ("", [])
                        rows = connection.execute(f"SELECT * FROM {table}{where} ORDER BY {clock} DESC LIMIT ?", args + [limit]).fetchall()
                    except sqlite3.Error:
                        warnings.append(f"{table}_unavailable")
                        continue
                    for row in rows:
                        item = self._legacy_item(dict(row), kind, clock)
                        if item:
                            items.append(item)
        except (sqlite3.Error, OSError):
            warnings.append("legacy_signals_database_unavailable")
        items.sort(key=lambda item: item.get("occurredAt") or "", reverse=True)
        items = items[:limit]
        return {"items": items, "asOf": items[0]["occurredAt"] if items else None,
                "status": "historical" if items else "unavailable",
                "warnings": ["legacy_history_not_active_monitoring"] + warnings}

    @staticmethod
    def _legacy_item(row: dict[str, Any], kind: str, clock: str) -> dict[str, Any] | None:
        code = str(row.get("code") or "")
        if not re.fullmatch(r"[0-9]{6}", code):
            return None
        stamp = _timestamp(row.get(clock))
        title = {"signal": f"{_text(row.get('expert'), 30)}信号", "conclusion": "旧版 AI 研判",
                 "watchLevel": f"{_text(row.get('expert'), 30)}触发条件", "circuit": "风险状态"}[kind]
        key = row.get("id") or f"{code}:{row.get('trade_date')}"
        variants = []
        if kind == "conclusion":
            try:
                decoded = json.loads(row.get("advice_json") or "[]")
                if isinstance(decoded, list):
                    variants = [{str(k)[:60]: _text(v, 500) for k, v in value.items()
                                 if k in {"label", "buy", "stop", "take", "reason", "action"}}
                                for value in decoded[:6] if isinstance(value, dict)]
            except (TypeError, ValueError):
                pass
        item = {"id": f"legacy:{kind}:{key}", "kind": kind, "code": code,
                "name": _text(row.get("name"), 50) or None, "occurredAt": stamp,
                "tradeDate": row.get("trade_date"), "title": title,
                "summary": _text(row.get("text") or row.get("note") or row.get("trip_note"), 3000),
                "variants": variants, "source": "legacy", "isHistorical": True}
        for field in ("expert", "metric", "op", "state", "verdict", "confidence", "action", "reason", "bias"):
            if row.get(field) is not None:
                item[field] = _text(row[field], 500)
        for field in ("threshold", "value", "strength"):
            if row.get(field) is not None:
                item[field] = _number(row[field])
        return item
