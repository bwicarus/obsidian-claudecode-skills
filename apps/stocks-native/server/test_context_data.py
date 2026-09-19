import copy
import unittest
from unittest.mock import AsyncMock, Mock

from context_data import STOCKS_CONTEXT_TOOL, fetch_context_sections


class ContextDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Mock()
        self.live = Mock(quotes=AsyncMock(), kline=AsyncMock(), minute=AsyncMock())
        self.quote = {
            'code': '000001', 'name': '平安银行', 'price': 12.34,
            'quoteTime': '2026-09-18T15:00:00+08:00',
            'bids': [{'price': index, 'volume': 100} for index in range(8)],
            'asks': [{'price': index, 'volume': 200} for index in range(8)],
            'unrequestedDebug': 'not model context',
        }
        self.live.quotes.return_value = {'000001': self.quote}
        self.detail = {
            'asOf': '2026-09-19T03:00:00+08:00', 'source': 'daily_collector',
            'stock': {'price': 999}, 'candles': [{'close': 999}],
            'technical': {'asOf': '2026-09-18', 'metrics': {'macd': 0.1}, 'checks': {},
                          'history': [{'tradeDate': str(index)} for index in range(40)]},
            'fund': {'asOf': '2026-09-17', 'metrics': {'mainInflow': 123}, 'history': []},
            'chips': {'asOf': '2026-09-16', 'average': 12, 'winnerRate': 0.4},
            'announcements': [{'title': '公告', 'date': '2026-09-18', 'url': 'https://example.test',
                               'unrelated': 'omit'} for _ in range(20)],
            'peers': [{'code': '600000', 'name': '同行', 'price': 9, 'unrelated': 'omit'} for _ in range(20)],
            'warnings': [],
        }
        self.store.stock_detail.return_value = self.detail

    async def test_quote_only_does_not_fetch_analysis_or_return_book(self):
        result = await fetch_context_sections(self.store, self.live, '000001', ['quote'])
        self.live.quotes.assert_awaited_once_with(['000001'])
        self.store.stock_detail.assert_not_called()
        self.live.kline.assert_not_awaited()
        self.live.minute.assert_not_awaited()
        self.assertEqual(set(result['sections']), {'quote'})
        self.assertEqual(result['sections']['quote']['price'], 12.34)
        self.assertNotIn('bids', result['sections']['quote'])
        self.assertNotIn('unrequestedDebug', result['sections']['quote'])
        self.assertEqual(result['asOf']['quote'], self.quote['quoteTime'])

    async def test_book_and_quote_share_one_fetch_and_cannot_mutate_cache(self):
        original = copy.deepcopy(self.quote)
        result = await fetch_context_sections(self.store, self.live, '000001', ['orderBook', 'quote', 'orderBook'])
        self.live.quotes.assert_awaited_once()
        self.assertEqual(len(result['sections']['orderBook']['bids']), 5)
        self.assertEqual(result['asOf']['orderBook'], self.quote['quoteTime'])
        result['sections']['orderBook']['bids'][0]['price'] = 0.99
        self.assertEqual(self.quote, original)

    async def test_missing_quote_timestamp_is_not_replaced_by_fetch_time(self):
        del self.quote['quoteTime']
        result = await fetch_context_sections(self.store, self.live, '000001', ['quote'])
        self.assertIsNone(result['asOf']['quote'])
        self.assertIn('quote_market_time_unavailable', result['warnings'])

    async def test_analysis_returns_only_requested_bounded_components_and_own_dates(self):
        original = copy.deepcopy(self.detail)
        result = await fetch_context_sections(self.store, self.live, '000001',
                                              ['technical', 'fund', 'chips', 'announcements', 'peers'])
        self.store.stock_detail.assert_called_once_with('000001', candle_limit=1)
        self.live.quotes.assert_not_awaited()
        self.assertEqual(set(result['sections']), {'technical', 'fund', 'chips', 'announcements', 'peers'})
        self.assertNotIn('stock', result)
        self.assertEqual(result['asOf']['fund'], '2026-09-17')
        self.assertEqual(result['asOf']['chips'], '2026-09-16')
        self.assertIsNone(result['asOf']['peers'])
        self.assertIsNone(result['asOf']['announcements'])
        self.assertEqual(result['sections']['announcements'][0]['date'], '2026-09-18')
        self.assertEqual(len(result['sections']['technical']['history']), 30)
        self.assertEqual(len(result['sections']['announcements']), 10)
        self.assertEqual(len(result['sections']['peers']), 10)
        self.assertNotIn('unrelated', result['sections']['peers'][0])
        result['sections']['technical']['metrics']['macd'] = 100
        self.assertEqual(self.detail, original)

    async def test_charts_request_only_chart_sources_and_keep_latest_120(self):
        self.live.kline.return_value = {'code': '000001', 'period': 'm5',
                                        'rows': [{'time': f'point-{index}', 'close': index} for index in range(150)]}
        self.live.minute.return_value = {'code': '000001', 'tradeDate': '20260918', 'previousClose': 12,
                                         'rows': [{'time': f'{index // 60:02}:{index % 60:02}', 'price': index}
                                                  for index in range(140)]}
        result = await fetch_context_sections(self.store, self.live, '000001', ['kline', 'intraday'], period='m5')
        self.live.kline.assert_awaited_once_with('000001', 'm5', 120)
        self.live.minute.assert_awaited_once_with('000001')
        self.live.quotes.assert_not_awaited()
        self.store.stock_detail.assert_not_called()
        self.assertEqual(len(result['sections']['kline']['rows']), 120)
        self.assertEqual(result['sections']['kline']['rows'][0]['close'], 30)
        self.assertEqual(result['asOf']['kline'], 'point-149')
        self.assertEqual(len(result['sections']['intraday']['rows']), 120)
        self.assertEqual(result['asOf']['intraday'], '20260918 02:19')

    async def test_partial_failure_does_not_hide_success_or_fetch_other_sources(self):
        self.live.quotes.side_effect = OSError('upstream is unavailable')
        result = await fetch_context_sections(self.store, self.live, '000001', ['quote', 'fund'])
        self.assertIsNone(result['sections']['quote'])
        self.assertEqual(result['sections']['fund']['metrics']['mainInflow'], 123)
        self.assertEqual(result['warnings'], ['quote_source_unavailable'])
        self.live.kline.assert_not_awaited()
        self.live.minute.assert_not_awaited()

    async def test_validation_rejects_invalid_arguments_before_fetch(self):
        for code, sections, period in [('bad', ['quote'], 'day'), ('000001', [], 'day'),
                                       ('000001', ['everything'], 'day'), ('000001', ['quote'], 'intraday'),
                                       ('000001', 'quote', 'day'), ('000001', [None], 'day')]:
            with self.subTest(code=code, sections=sections, period=period):
                with self.assertRaises(ValueError):
                    await fetch_context_sections(self.store, self.live, code, sections, period)
        self.live.quotes.assert_not_awaited()
        self.store.stock_detail.assert_not_called()
        self.assertEqual(STOCKS_CONTEXT_TOOL['inputSchema']['required'], ['sections'])


if __name__ == '__main__':
    unittest.main()
