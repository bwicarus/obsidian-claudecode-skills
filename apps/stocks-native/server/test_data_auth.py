"""Meaningful boundary checks; fixtures never use production paths."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auth import AuthError, AuthStore
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
            CREATE TABLE daily_quotes(trade_date TEXT, code TEXT, open REAL, high REAL, low REAL, price REAL, volume REAL);
            CREATE INDEX idx_daily_quotes_code ON daily_quotes(code, trade_date);
            CREATE TABLE stock_industries(code TEXT, industry TEXT);
            INSERT INTO stock_industries VALUES('000001', '银行');
            INSERT INTO daily_quotes VALUES('2026-09-17', '000001',11.68,11.74,11.57,11.61,691925);
            INSERT INTO daily_quotes VALUES('2026-09-18', '000001',11.59,11.82,11.56,11.7,NULL);
            INSERT INTO daily_quotes VALUES('2026-09-16', '000001',NULL,12,11,11.7,1);
            INSERT INTO daily_quotes VALUES('2026-09-15', '000001',13,12,11,11.7,1);
            INSERT INTO daily_quotes VALUES('2026-09-19', '000001',12,12,12,12,1);
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
        json.dumps(detail, allow_nan=False)

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
