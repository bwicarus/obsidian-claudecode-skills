"""Focused contracts for chip estimates and the existing fund history source."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from chips import build_chips, chip_distribution
from data import StockDataStore


def history():
    return [{'trade_date': f'2026-09-{day:02d}', 'open': 10, 'high': 11, 'low': 9,
             'close': 10, 'volume': 100 + day, 'turnover_rate': 2}
            for day in range(14, 19)]


class ChipTests(unittest.TestCase):
    def test_density_is_bounded_normalized_and_has_triangular_peak(self):
        rows = chip_distribution(history())
        self.assertGreater(len(rows), 5)
        self.assertLessEqual(len(rows), 800)
        self.assertEqual(len(rows), len({row['price'] for row in rows}))
        self.assertAlmostEqual(sum(row['percent'] for row in rows), 100, places=3)
        peak = max(rows, key=lambda row: row['percent'])
        self.assertAlmostEqual(peak['price'], 10, delta=.04)
        self.assertGreater(peak['percent'], rows[0]['percent'])
        json.dumps(rows, allow_nan=False)

    def test_full_turnover_replaces_old_holdings_and_sparse_data_is_empty(self):
        data = history()
        data[-1].update(open=20, high=20, low=20, close=20, turnover_rate=100)
        rows = chip_distribution(data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['percent'], 100)
        self.assertEqual(chip_distribution(data[:2]), [])
        self.assertEqual(chip_distribution([dict(row, volume=float('inf')) for row in data]), [])

    def test_live_quote_replaces_same_day_and_never_leaks_into_historical_range(self):
        payload = {'code': '000001', 'start': '2026-09-14', 'end': '2026-09-18', 'history': history()}
        live = {'code': '000001', 'quoteTime': '2026-09-18T15:00:00+08:00',
                'open': 20, 'high': 20, 'low': 20, 'price': 20, 'volume': 500, 'turnoverRate': 100}
        result = build_chips(payload, live)
        self.assertEqual(result['currentPrice'], 20)
        self.assertEqual(len(result['rows']), 1)
        self.assertIn('tencent', result['source'])
        self.assertIsNone(result['warning'])
        for key in ('winnerRate', 'concentration'):
            self.assertTrue(0 <= result[key] <= 100)
        old = dict(payload, end='2026-09-17', history=history()[:-1])
        self.assertEqual(build_chips(old, live), build_chips(old))
        wrong = dict(live, code='000002')
        self.assertEqual(build_chips(payload, wrong), build_chips(payload))

    def test_empty_history_never_reconstructs_density_from_quantiles(self):
        result = build_chips({'code': '000001', 'start': '2026-09-01', 'end': '2026-09-18', 'history': []})
        self.assertEqual(result['rows'], [])
        self.assertIsNone(result['averageCost'])
        self.assertEqual(result['warning'], 'insufficient_chip_history')


class ChipDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'stocks.json').write_text(json.dumps({'rows': [{'code': '000001'}]}))
        self.path = self.root / 'stocks.db'
        self.fund = {f'{side}_{size}_amount': float(index + 1) for index, (side, size) in enumerate(
            (side, size) for side in ('buy', 'sell') for size in ('sm', 'md', 'lg', 'elg'))}
        self.fund.update(latest_main_inflow=1200, latest_main_ratio=15)
        with sqlite3.connect(self.path) as connection:
            connection.execute('CREATE TABLE daily_quotes(code,trade_date,open,high,low,price,volume,turnover_rate)')
            connection.executemany('INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?,?)',
                [('000001', row['trade_date'], row['open'], row['high'], row['low'], row['close'],
                  row['volume'], row['turnover_rate']) for row in history()])
            connection.execute('CREATE TABLE daily_feature_groups(code,trade_date,feature_group,status,checks_json,metrics_json)')
            connection.execute('INSERT INTO daily_feature_groups VALUES (?,?,?,?,?,?)',
                ('000001', '2026-09-18', 'fund', 'done', '{}', json.dumps(self.fund)))
        connection.close()
        self.store = StockDataStore(self.root)

    def test_history_is_read_only_bounded_and_validates_dates(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = self.store.chip_history('000001', '2026-09-15', '2026-09-17')
        self.assertEqual([row['trade_date'] for row in result['history']], ['2026-09-15', '2026-09-16', '2026-09-17'])
        self.assertEqual(self.store.chip_history('000001', end='2026-09-18')['start'], '2026-09-14')
        with self.assertRaises(ValueError):
            self.store.chip_history('000001', '2020-01-01', '2026-09-18')
        with self.assertRaises(ValueError):
            self.store.chip_history('000001', '2026-09-19', '2026-09-18')
        with self.assertRaises(ValueError):
            self.store.chip_history('../stocks', '2026-09-14', '2026-09-18')
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        with self.store._connect() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('DELETE FROM daily_quotes')

    def test_fund_history_retains_all_eight_real_amounts_and_main_metrics(self):
        # Later optional panel tables intentionally need not exist for this test.
        result = self.store.stock_panels('000001')['fund']
        self.assertEqual(result['metrics'], self.fund)
        self.assertEqual(result['history'], [{'tradeDate': '2026-09-18', **self.fund}])


if __name__ == '__main__':
    unittest.main()
