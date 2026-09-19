"""Meaningful boundary checks; fixtures never use production paths."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auth import AuthError, AuthStore, REVIEW_PAIRING_TTL_SECONDS
from data import DataUnavailable, StockDataStore, StockNotFound


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1_790_000_000.0
        self.store = AuthStore(self.directory.name, clock=lambda: self.now, token_ttl_seconds=1200)

    def test_pairing_is_single_use_hashed_and_device_bound(self):
        code = self.store.create_pairing_code()["code"]
        receipt = self.store.pair(code.lower(), "ipad-1", "Stock iPad")
        identity = self.store.authenticate(receipt["token"], "ipad-1")
        self.assertEqual(identity["name"], "Stock iPad")
        with self.assertRaises(AuthError):
            self.store.pair(code, "ipad-2", "Other")
        with self.assertRaises(AuthError):
            self.store.authenticate(receipt["token"], "ipad-2")
        contents = self.store.db_path.read_bytes()
        self.assertNotIn(receipt["token"].encode(), contents)
        self.assertNotIn(code.replace("-", "").encode(), contents)

    def test_expiry_and_revocation(self):
        expired = self.store.create_pairing_code()["code"]
        self.now += 600
        with self.assertRaises(AuthError):
            self.store.pair(expired, "device-a", "Expired")
        first = self.store.pair(self.store.create_pairing_code()["code"], "device-a", "A")
        second = self.store.pair(self.store.create_pairing_code()["code"], "device-b", "B")
        self.assertEqual(self.store.revoke_device("device-a"), 1)
        with self.assertRaises(AuthError):
            self.store.authenticate(first["token"])
        self.assertEqual(self.store.authenticate(second["token"])["deviceId"], "device-b")
        self.now += 1200
        with self.assertRaises(AuthError):
            self.store.authenticate(second["token"])

    def test_simultaneous_redemption_has_one_winner(self):
        code = self.store.create_pairing_code()["code"]

        def redeem(index):
            try:
                return self.store.pair(code, f"device-{index}", "Concurrent test")
            except AuthError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(redeem, range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_separate_store_does_not_recognize_token(self):
        receipt = self.store.pair(self.store.create_pairing_code()["code"], "device", "Test")
        other = AuthStore(Path(self.directory.name) / "other", clock=lambda: self.now)
        with self.assertRaises(AuthError):
            other.authenticate(receipt["token"])

    def test_review_code_is_long_lived_single_use_and_disables_ai(self):
        with self.assertRaises(ValueError):
            self.store.create_pairing_code(601)
        result = self.store.create_review_pairing_code()
        self.assertEqual(result["validForSeconds"], REVIEW_PAIRING_TTL_SECONDS)
        self.assertFalse(result["aiEnabled"])
        self.now += 601
        receipt = self.store.pair(result["code"], "apple-review", "App Review")
        self.assertFalse(receipt["aiEnabled"])
        self.assertFalse(self.store.authenticate(receipt["token"])["aiEnabled"])
        with self.assertRaises(AuthError):
            self.store.pair(result["code"], "apple-review-2", "App Review 2")

    def test_review_code_expires_after_seven_days(self):
        result = self.store.create_review_pairing_code()
        self.now += REVIEW_PAIRING_TTL_SECONDS
        with self.assertRaises(AuthError):
            self.store.pair(result["code"], "apple-review", "App Review")

    def test_verified_apple_subject_issues_device_bound_token_without_storing_subject(self):
        receipt = self.store.apple_login("001234.abcdef.stable-subject", "ipad-apple", "iPad")
        identity = self.store.authenticate(receipt["token"], "ipad-apple")
        self.assertTrue(identity["aiEnabled"])
        self.assertEqual(identity["name"], "iPad")
        self.assertNotIn(b"stable-subject", self.store.db_path.read_bytes())


class DataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.payload = {"ok": True, "source": "fixture", "generated_at": "2026-09-18T15:30:01", "rows": [
            {"code": "000001", "name": "平安银行", "price": 11.7, "change_pct": .78, "turnover": 999920000},
            {"code": "000002", "name": "Test Missing", "price": None, "change_pct": "NaN"},
        ]}
        self.snapshot = self.root / "stocks.json"
        self.snapshot.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        self.database = self.root / "stocks.db"
        connection = sqlite3.connect(self.database)
        connection.executescript("""
            CREATE TABLE daily_quotes(trade_date TEXT, code TEXT, name TEXT, open REAL, high REAL, low REAL,
                price REAL, volume REAL, change_pct REAL, turnover_rate REAL, market_cap REAL);
            CREATE INDEX idx_daily_quotes_code ON daily_quotes(code, trade_date);
            CREATE TABLE stock_industries(code TEXT, industry TEXT);
            CREATE TABLE stock_concepts(code TEXT, concept TEXT, name TEXT, updated_at TEXT);
            CREATE TABLE daily_feature_groups(trade_date TEXT, code TEXT, feature_group TEXT,
                checks_json TEXT, metrics_json TEXT, status TEXT, error TEXT, updated_at TEXT);
            CREATE TABLE daily_chips(trade_date TEXT, code TEXT, his_low REAL, his_high REAL,
                cost_5pct REAL, cost_15pct REAL, cost_50pct REAL, cost_85pct REAL, cost_95pct REAL,
                weight_avg REAL, winner_rate REAL, updated_at TEXT);
            CREATE TABLE daily_news(code TEXT, items_json TEXT, fetched_date TEXT, updated_at TEXT);
            CREATE TABLE daily_top_list(trade_date TEXT, code TEXT, reason TEXT, net_amount REAL, net_rate REAL);
            CREATE TABLE daily_hsgt_top10(trade_date TEXT, code TEXT, rank INTEGER, amount REAL,
                net_amount REAL, buy REAL, sell REAL);
            CREATE TABLE daily_limit(trade_date TEXT, code TEXT, up_limit REAL, down_limit REAL);
            CREATE TABLE daily_hsgt_total(trade_date TEXT, north_money REAL, south_money REAL);
            CREATE TABLE daily_sector_flow(trade_date TEXT, sector_code TEXT, sector_name TEXT,
                pct_change REAL, net_amount REAL, net_amount_rate REAL);
            INSERT INTO stock_industries VALUES('000001', '银行');
            INSERT INTO stock_concepts VALUES('000001', '金融科技', '平安银行', '2026-09-18');
            INSERT INTO daily_quotes VALUES('2026-09-17', '000001','平安银行',11.68,11.74,11.57,11.61,691925,-.2,1.1,1000);
            INSERT INTO daily_quotes VALUES('2026-09-18', '000001','平安银行',11.59,11.82,11.56,11.7,NULL,.78,1.2,1001);
            INSERT INTO daily_quotes VALUES('2026-09-16', '000001','平安银行',NULL,12,11,11.7,1,0,1,1000);
            INSERT INTO daily_quotes VALUES('2026-09-15', '000001','平安银行',13,12,11,11.7,1,0,1,1000);
            INSERT INTO daily_quotes VALUES('2026-09-19', '000001','平安银行',12,12,12,12,1,1,1,1002);
            INSERT INTO daily_feature_groups VALUES('2026-09-18','000001','technical','{}','{"macd_hist":0.2,"kdj_k":55}', 'done',NULL,'x');
            INSERT INTO daily_feature_groups VALUES('2026-09-18','000001','fund','{}','{"latest_main_inflow":1000000}', 'done',NULL,'x');
            INSERT INTO daily_chips VALUES('2026-09-18','000001',9,13,10,10.5,11.2,11.8,12,11.3,.8,'x');
            INSERT INTO daily_news VALUES('000001','[{"title":"半年报","date":"2026-08-29","url":"https://example.com/a","column":"财报"}]','2026-09-18','x');
            INSERT INTO daily_hsgt_total VALUES('2026-09-18',100,200);
            INSERT INTO daily_sector_flow VALUES('2026-09-18','BK1','银行',1.2,300,2.1);
        """)
        connection.commit()
        connection.close()
        self.store = StockDataStore(self.root)

    def test_real_field_mapping_nulls_search_and_date_bounds(self):
        result = self.store.list_stocks("银行")
        self.assertEqual(result["asOf"], "2026-09-18T15:30:01")
        self.assertEqual(result["items"][0]["sector"], "银行")
        self.assertEqual(result["items"][0]["changePct"], .78)
        missing = self.store.list_stocks("000002")["items"][0]
        self.assertIsNone(missing["price"])
        self.assertIsNone(missing["changePct"])
        detail = self.store.stock_detail("000001")
        self.assertEqual([row["time"] for row in detail["candles"]], ["2026-09-17", "2026-09-18"])
        self.assertIsNone(detail["candles"][-1]["volume"])
        self.assertEqual(detail["omittedCandles"], 2)
        self.assertEqual(detail["technical"]["metrics"]["macd_hist"], .2)
        self.assertEqual(detail["fund"]["metrics"]["latest_main_inflow"], 1000000)
        self.assertEqual(detail["chips"]["cost50"], 11.2)
        self.assertEqual(detail["concepts"], ["金融科技"])
        self.assertEqual(detail["announcements"][0]["title"], "半年报")
        json.dumps(detail, allow_nan=False)

        overview = self.store.market_overview()
        self.assertEqual(overview["rising"], 1)
        self.assertEqual(overview["hotSectors"][0]["name"], "银行")

    def test_reads_do_not_change_source_and_connection_rejects_writes(self):
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.iterdir()}
        self.store.list_stocks()
        self.store.stock_detail("000001")
        with self.store._connect() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM daily_quotes")
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.iterdir()}
        self.assertEqual(before, after)

    def test_missing_database_is_reported_and_not_created(self):
        self.database.unlink()
        detail = self.store.stock_detail("000001")
        self.assertFalse(self.database.exists())
        self.assertEqual(detail["candles"], [])
        self.assertIn("candle_data_unavailable", detail["warnings"])
        self.assertEqual(detail["stock"]["price"], 11.7)

    def test_missing_or_corrupt_snapshot_does_not_serve_stale_cache(self):
        self.store.list_stocks()
        self.snapshot.write_text("invalid", encoding="utf-8")
        with self.assertRaises(DataUnavailable):
            self.store.list_stocks()
        self.snapshot.unlink()
        with self.assertRaises(DataUnavailable):
            self.store.list_stocks()

    def test_constructor_does_not_create_source_and_invalid_codes_are_rejected(self):
        missing = self.root / "missing"
        StockDataStore(missing)
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError):
            self.store.stock_detail("000001' OR 1=1")
        with self.assertRaises(StockNotFound):
            self.store.stock_detail("999999")


if __name__ == "__main__":
    unittest.main()
