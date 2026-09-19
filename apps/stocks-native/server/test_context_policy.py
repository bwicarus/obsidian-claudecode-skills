import copy
import unittest

from context_policy import prepare_patch, quote_is_jitter, requested_live_sections, sections, snapshot


def screen(price='1000', change='1.00%', code='000001'):
    return {
        'screen': 'stock_detail', 'selectedCode': code, 'selectedName': '测试股票',
        'quoteAsOf': '2026-09-18', 'quoteTime': '2026-09-18T14:00:00+08:00',
        'quoteSource': 'tencent', 'latestPointTime': '14:00',
        'chartPeriod': '日K', 'chartPeriodID': 'day',
        'viewState': {'detailTab': 'chart', 'inspectorMode': 'voice'},
        'metrics': {'price': price, 'changePct': change, 'macd': '0.17', 'turnover': '123456'},
        'chart': {'selectionSource': 'cursor', 'selectedPoint': {'time': '2026-09-17', 'close': 998},
                  'history': [{'time': '2026-09-17', 'close': 998}]},
        'annotations': {'strokes': [{'id': 'ink', 'points': [[1, 2], [3, 4]]}]},
        'recentActions': [],
    }


class ContextPolicyTests(unittest.TestCase):
    def baseline(self, context=None, audience='voice', now=100):
        context = context or screen()
        _, ledger = prepare_patch(context, audience, {}, False, now=now)
        return ledger

    def test_single_component_change_emits_only_that_section(self):
        initial = screen()
        ledger = self.baseline(initial, 'backend')
        changed = copy.deepcopy(initial)
        changed['chart']['selectedPoint'] = {'time': '2026-09-16', 'close': 997}
        changed['chart']['selectionSource'] = 'cursor'
        patch, _ = prepare_patch(changed, 'voice', self.baseline(initial), True, now=101)
        self.assertEqual(set(patch['sections']), {'selection'})
        changed = copy.deepcopy(initial)
        changed['annotations'] = {'strokes': []}
        patch, _ = prepare_patch(changed, 'backend', ledger, True, now=101)
        self.assertEqual(set(patch['sections']), {'annotations'})

    def test_voice_excludes_full_metrics_history_and_ink(self):
        result = sections(screen(), 'voice')
        self.assertEqual(set(result['quote']['metrics']), {'price', 'changePct'})
        self.assertNotIn('metrics', result)
        self.assertNotIn('chart', result)
        self.assertNotIn('annotations', result)
        self.assertNotIn('history', result['selection'])
        self.assertEqual(result['selection']['close'], 998)

    def test_small_moves_compare_last_accepted_value_and_accumulate(self):
        initial = screen()
        ledger = self.baseline(initial)
        original_ledger = copy.deepcopy(ledger)
        patch, not_accepted = prepare_patch(screen(price='1000.30'), 'voice', ledger, True, now=105)
        self.assertEqual(patch['sections'], {})
        self.assertEqual(not_accepted['quote'], ledger['quote'])
        patch, accepted = prepare_patch(screen(price='1000.60'), 'voice', not_accepted, True, now=110)
        self.assertEqual(set(patch['sections']), {'quote'})
        self.assertEqual(accepted['quote']['value']['metrics']['price'], '1000.60')
        self.assertEqual(ledger, original_ledger)

    def test_jitter_requires_both_price_and_change_below_threshold(self):
        previous = sections(screen(), 'voice')['quote']
        for price, change, expected in [('1000.40', '1.04%', True), ('1000.60', '1.01%', False),
                                         ('1000.10', '1.06%', False), ('1000.50', '1.00%', False)]:
            with self.subTest(price=price, change=change):
                current = sections(screen(price=price, change=change), 'voice')['quote']
                self.assertEqual(quote_is_jitter(previous, current), expected)

    def test_change_crossing_zero_bypasses_noise_band(self):
        for before, after in [('-0.01%', '0.01%'), ('0.01%', '-0.01%'), ('0.00%', '0.01%')]:
            with self.subTest(before=before, after=after):
                previous = sections(screen(change=before), 'voice')['quote']
                current = sections(screen(change=after), 'voice')['quote']
                self.assertFalse(quote_is_jitter(previous, current))

    def test_exact_decimal_change_threshold_is_not_hidden_by_float_roundoff(self):
        previous = sections(screen(change='0.25%'), 'voice')['quote']
        current = sections(screen(change='0.30%'), 'voice')['quote']
        self.assertFalse(quote_is_jitter(previous, current))

    def test_explicit_quote_request_bypasses_jitter_without_full_context(self):
        context = screen(price='1000.01')
        patch, _ = prepare_patch(context, 'voice', self.baseline(), True, force_quote=True, now=101)
        self.assertEqual(set(patch['sections']), {'quote'})
        self.assertEqual(patch['sections']['quote']['metrics']['price'], '1000.01')

    def test_timestamp_only_quote_waits_for_sixty_second_turn_boundary(self):
        ledger = self.baseline(now=100)
        changed = screen()
        changed['quoteTime'] = '2026-09-18T14:00:50+08:00'
        changed['latestPointTime'] = '14:01'
        patch, unaccepted = prepare_patch(changed, 'voice', ledger, True, now=159.99)
        self.assertEqual(patch['sections'], {})
        self.assertEqual(unaccepted['quote']['at'], 100)
        # Preparing a patch at a user turn supplies the clock. There is no timer
        # or background injection and the supplied ledger itself is unchanged.
        patch, accepted = prepare_patch(changed, 'voice', unaccepted, True, now=160)
        self.assertEqual(set(patch['sections']), {'quote'})
        self.assertEqual(accepted['quote']['at'], 160)
        self.assertEqual(ledger['quote']['at'], 100)

    def test_stock_scope_switch_replaces_and_clears_old_sections(self):
        initial = screen()
        initial['recentActions'] = [{'kind': 'search', 'label': '旧股票动作', 'stockCode': '000001'}]
        switched = screen(code='600000')
        switched['chart'] = {}
        switched['annotations'] = {}
        patch, accepted = prepare_patch(switched, 'backend', self.baseline(initial, 'backend'),
                                         False, now=101)
        self.assertEqual(patch['mode'], 'replace')
        self.assertEqual(patch['selectedCode'], '600000')
        self.assertEqual(patch['sections']['selection'], {})
        self.assertEqual(patch['sections']['actions'], [])
        self.assertEqual(patch['sections']['annotations'], {})
        self.assertEqual(accepted['view']['value']['selectedCode'], '600000')

    def test_empty_selection_and_actions_emit_explicit_clear(self):
        initial = screen()
        initial['recentActions'] = [{'kind': 'search', 'label': '搜索股票', 'stockCode': '000001'}]
        changed = copy.deepcopy(initial)
        changed['chart'] = {'selectionSource': 'none'}
        changed['recentActions'] = []
        patch, _ = prepare_patch(changed, 'voice', self.baseline(initial), True, now=101)
        self.assertEqual(patch['sections'], {'selection': {}, 'actions': []})

    def test_snapshot_actions_expire_at_thirty_seconds_and_match_scope(self):
        context = screen()
        # 2026-09-18T06:00:00Z; snapshots are deterministic with this clock.
        now = 1789711200
        context['recentActions'] = [
            {'id': 'expired', 'kind': 'search', 'stockCode': '000001', 'occurredAtUtc': '2026-09-18T05:59:29Z'},
            {'id': 'other-stock', 'kind': 'search', 'stockCode': '600000', 'occurredAtUtc': '2026-09-18T05:59:55Z'},
            {'id': 'other-period', 'kind': 'annotation', 'stockCode': '000001', 'chartPeriod': 'week',
             'occurredAtUtc': '2026-09-18T05:59:55Z'},
            {'id': 'unknown-time', 'kind': 'search', 'stockCode': '000001'},
            {'id': 'boundary', 'kind': 'search', 'stockCode': '000001', 'occurredAtUtc': '2026-09-18T05:59:30Z'},
            {'id': 'current', 'kind': 'annotation', 'stockCode': '000001', 'chartPeriod': 'day',
             'occurredAtUtc': '2026-09-18T05:59:55Z'},
        ]
        result = snapshot(context, now=now)
        self.assertEqual([action['id'] for action in result['recentActions']], ['boundary', 'current'])
        self.assertEqual(len(context['recentActions']), 6)
        self.assertEqual(snapshot(context, now=now + 31)['recentActions'], [])

    def test_historical_question_avoids_refreshing_selected_historical_point(self):
        self.assertEqual(requested_live_sections('这根光标选中的K线价格多少钱'), set())
        self.assertEqual(requested_live_sections('现在股价多少钱'), {'quote'})
        self.assertEqual(requested_live_sections('现在买一卖一盘口怎样'), {'quote', 'orderBook'})


if __name__ == '__main__':
    unittest.main()
