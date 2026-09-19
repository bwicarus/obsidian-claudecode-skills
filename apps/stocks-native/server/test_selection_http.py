"""HTTP contract checks; temporary auth/selection state, no live voice or provider calls."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from app import create_app


class SelectionHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        market = root / "market"
        market.mkdir()
        with sqlite3.connect(market / "stocks.db") as db:
            db.execute("CREATE TABLE daily_quotes(trade_date TEXT,code TEXT,name TEXT,price REAL,turnover_rate REAL,change_pct REAL)")
            db.execute("INSERT INTO daily_quotes VALUES ('2026-09-18','000001','示例',10,5,1)")
        db.close()
        with patch.dict(os.environ, {"STOCKS_MVP_STATE_DIR": str(root / "state"), "STOCKS_DATA_ROOT": str(market)}):
            self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.first = self.app['auth'].apple_login('account-one', 'ipad-one', 'iPad')
        self.same = self.app['auth'].apple_login('account-one', 'phone-one', 'iPhone')
        self.other = self.app['auth'].apple_login('account-two', 'ipad-two', 'iPad')

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    def headers(self, identity):
        return {'Authorization': 'Bearer ' + identity['token'], 'X-Device-ID': identity['deviceId']}

    async def test_unauthenticated_catalog_and_wrong_device_rejected(self):
        response = await self.client.get('/api/selection/catalog')
        self.assertEqual(response.status, 401)
        headers = self.headers(self.first)
        headers['X-Device-ID'] = 'ipad-two'
        response = await self.client.get('/api/selection/library', headers=headers)
        self.assertEqual(response.status, 401)

    async def test_account_scope_receipt_retry_and_conflict_over_http(self):
        mutation = {'requestId': 'http-create', 'expectedRevision': 0,
                    'operation': 'group.create', 'payload': {'name': '观察组'}}
        response = await self.client.post('/api/selection/mutate', json=mutation, headers=self.headers(self.first))
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertEqual(result['revision'], 1)
        retry = await self.client.post('/api/selection/mutate', json=mutation, headers=self.headers(self.same))
        self.assertTrue((await retry.json())['replayed'])
        same = await self.client.get('/api/selection/library', headers=self.headers(self.same))
        self.assertEqual(len((await same.json())['groups']), 1)
        other = await self.client.get('/api/selection/library', headers=self.headers(self.other))
        self.assertEqual((await other.json())['groups'], [])
        mutation['requestId'] = 'http-stale'
        conflict = await self.client.post('/api/selection/mutate', json=mutation, headers=self.headers(self.first))
        self.assertEqual(conflict.status, 409)
        body = await conflict.json()
        self.assertEqual((body['code'], body['revision']), ('revision_conflict', 1))
        self.assertEqual(conflict.headers['Cache-Control'], 'no-store')

    async def test_evaluation_unknown_condition_is_actionable_error(self):
        response = await self.client.post('/api/selection/evaluate', headers=self.headers(self.first),
                                         json={'groups': [{'and': ['made_up_condition']}]})
        self.assertEqual(response.status, 400)
        self.assertIn('code', await response.json())

    async def test_review_identity_cannot_open_voice(self):
        code = self.app['auth'].create_review_pairing_code()['code']
        review = self.app['auth'].pair(code, 'review-device', 'Review')
        response = await self.client.get('/voice?deviceId=review-device', headers=self.headers(review))
        self.assertEqual(response.status, 403)
        self.assertEqual(self.app['voices'], {})


if __name__ == '__main__':
    unittest.main()
