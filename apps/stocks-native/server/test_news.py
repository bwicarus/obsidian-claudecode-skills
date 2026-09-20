import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from data import StockDataStore, _announcement_url
from news import NewsService, _safe_url


class NewsChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = StockDataStore(self.root)
        with sqlite3.connect(self.data.db_path) as connection:
            connection.execute("CREATE TABLE daily_news(code TEXT, items_json TEXT, fetched_date TEXT)")
        self.service = NewsService(self.data, self.root / "state")

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_macro_source_metadata_and_cached_without_network(self):
        self.service._get_json = AsyncMock(return_value={"result": {"status": {"code": 0}, "data": [
            {"title": "<b>金融</b>&amp;市场", "ctime": "1789920000", "media_name": "来源媒体",
             "url": "https://finance.sina.com.cn/test.html", "intro": "摘要"}]}})
        first = await self.service.feed()
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(first["items"][0]["title"], "金融&市场")
        self.assertEqual(first["items"][0]["source"], "来源媒体")
        second = await self.service.feed(refresh=True)
        self.assertEqual(second["status"], "cached")
        self.assertEqual(first["fetchedAt"], second["fetchedAt"])
        self.service._get_json.assert_awaited_once()

    async def test_stock_announcements_merge_and_restore_article_link(self):
        with sqlite3.connect(self.data.db_path) as connection:
            connection.execute("INSERT INTO daily_news VALUES(?,?,?)", ("000001", json.dumps([
                {"title": "公司公告", "date": "2026-09-18", "art_code": "AN202609181822383350"}]), "2026-09-18"))
        self.service._get_json = AsyncMock(return_value={"code": 0, "result": {"cmsArticleWebOld": [
            {"title": "公司新闻", "date": "2026-09-20 10:00:00", "url": "http://finance.eastmoney.com/a/123.html",
             "mediaName": "来源", "content": "摘要"}]}})
        result = await self.service.feed("stock", code="000001")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["publishedAt"], "2026-09-20T02:00:00Z")
        self.assertTrue(result["items"][0]["url"].startswith("https://"))
        self.assertIn("art_code=AN202609181822383350", result["items"][1]["url"])

    async def test_failed_refresh_preserves_retrieval_and_news_time(self):
        original = time.time() - 400
        self.service._cache["macro:"] = {"items": [{"id": "one", "publishedAt": "2026-01-01T00:00:00Z"}],
                                          "fetchedEpoch": original, "attemptEpoch": original}
        self.service._fetch_public = AsyncMock(side_effect=ValueError("bad source response"))
        result = await self.service.feed()
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["asOf"], "2026-01-01T00:00:00Z")
        self.assertIn("news_refresh_failed_cached_data", result["warnings"])
        self.assertEqual(self.service._cache["macro:"]["fetchedEpoch"], original)
        await self.service.feed(refresh=True)
        self.service._fetch_public.assert_awaited_once()

    async def test_unavailable_and_legacy_ai_summary_are_not_fresh(self):
        self.service._fetch_public = AsyncMock(side_effect=asyncio.TimeoutError())
        result = await self.service.feed()
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["fetchedAt"])
        (self.root / "sector_news.json").write_text(json.dumps({"银行": {"text": "旧摘要", "ts": "2026-06-18T10:00:00"}}), encoding="utf-8")
        result = await self.service.feed("sector", sector="银行")
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["items"][0]["isLegacy"])
        self.assertIn("legacy_ai_summary_not_live_news", result["warnings"])

    async def test_concurrent_requests_share_one_fetch(self):
        gate = asyncio.Event()

        async def fetch(*args):
            await gate.wait()
            return []

        self.service._fetch_public = AsyncMock(side_effect=fetch)
        tasks = [asyncio.create_task(self.service.feed()) for _ in range(4)]
        await asyncio.sleep(0.02)
        gate.set()
        await asyncio.gather(*tasks)
        self.service._fetch_public.assert_awaited_once()

    async def test_invalid_input_never_fetches(self):
        self.service._fetch_public = AsyncMock()
        for args in (("bad",), ("stock",), ("stock", "../"), ("sector", None, "")):
            with self.assertRaises(ValueError):
                await self.service.feed(*args)
        for limit in (0, 61, True, "10"):
            with self.assertRaises(ValueError):
                await self.service.feed(limit=limit)
        self.service._fetch_public.assert_not_awaited()

    def test_links_only_use_known_public_publishers(self):
        for url in ("javascript:alert(1)", "https://eastmoney.com.evil.test/x", "https://localhost/x",
                    "https://name:password@finance.sina.com.cn/x", "https://finance.sina.com.cn:1234/x"):
            self.assertIsNone(_safe_url(url))
        self.assertEqual(_safe_url("http://finance.sina.com.cn/x"), "https://finance.sina.com.cn/x")
        self.assertIsNone(_announcement_url({"url": "https://evil.test/foo"}))

    def test_legacy_history_is_read_only_filtered_and_not_live_rule(self):
        with sqlite3.connect(self.data.db_path) as connection:
            connection.execute("CREATE TABLE expert_signals(id INTEGER, ts TEXT, code TEXT, expert TEXT, metric TEXT, threshold REAL)")
            connection.execute("INSERT INTO expert_signals VALUES(1,'2026-07-01T10:00:00','000001','趋势','price',12.0)")
            connection.execute("INSERT INTO expert_signals VALUES(2,'2026-07-02T10:00:00','000002','量能','volume_ratio',2.0)")
        result = self.service.legacy_signals("000001")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["occurredAt"], "2026-07-01T02:00:00Z")
        self.assertTrue(result["items"][0]["isHistorical"])
        self.assertEqual(result["status"], "historical")
        self.assertIn("legacy_history_not_active_monitoring", result["warnings"])
        with sqlite3.connect(self.data.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM expert_signals").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
