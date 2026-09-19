import tempfile
import unittest

from voice import VoiceSession


def context(price='12.34', observed='2026-09-19T08:00:00Z'):
    return {
        'screen': 'stock_detail',
        'selectedCode': '000001',
        'selectedName': '平安银行',
        'quoteAsOf': '2026-09-19',
        'observedAtUtc': observed,
        'chartPeriod': '分时',
        'latestPointTime': '14:55',
        'metrics': {'price': price, 'changePct': '+1.20%'},
        'visiblePanels': ['分时', '行情指标'],
        'recentActions': [{
            'id': 'action-1', 'kind': 'chart_period', 'label': '切换图表：分时',
            'occurredAtUtc': observed, 'stockCode': '000001', 'chartPeriod': 'intraday',
        }],
    }


class ContextInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()

        async def emit_json(_):
            return None

        async def emit_audio(_):
            return None

        self.session = VoiceSession('context-test', self.directory.name, None, emit_json, emit_audio)
        self.session.supports_ui_context = True
        self.session.thread_id = 'thread-1'
        self.session.stock_code = '000001'
        self.session.ready.set()
        self.calls = []

        async def call(method, params, timeout=45):
            self.calls.append((method, params, timeout))
            return {}

        self.session.call = call

    async def asyncTearDown(self):
        await self.session.pc.close()
        self.directory.cleanup()

    async def test_refresh_only_stores_latest_until_user_turn(self):
        await self.session.update_context(context())
        await self.session.update_context(context(observed='2026-09-19T08:00:10Z'))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.session.ui_context_revision, 1)

        await self.session.inject_voice_context()
        self.assertEqual([item[0] for item in self.calls], ['thread/realtime/appendText'])
        self.assertIn('audience=voice', self.calls[0][1]['text'])

        await self.session.inject_voice_context()
        self.assertEqual(len(self.calls), 1)

        await self.session.update_context(context(price='12.35', observed='2026-09-19T08:00:20Z'))
        await self.session.inject_voice_context()
        self.assertEqual(len(self.calls), 2)
        self.assertIn('12.35', self.calls[-1][1]['text'])

    async def test_delegation_steers_full_context_into_active_turn(self):
        await self.session.update_context(context())
        self.session.active_turn_id = 'turn-1'

        await self.session.inject_delegation_context()

        self.assertEqual([item[0] for item in self.calls], ['turn/steer'])
        params = self.calls[0][1]
        self.assertEqual(params['expectedTurnId'], 'turn-1')
        self.assertIn('audience=backend', params['input'][0]['text'])
        state = self.session.turn_state('turn-1')
        self.assertTrue(state['contextInjected'])
        self.assertEqual(state['contextCode'], '000001')

        self.session.active_turn_id = 'turn-2'
        await self.session.inject_delegation_context()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.session.turn_state('turn-2')['contextInjected'])

    async def test_delegation_uses_snapshot_pinned_when_user_started_speaking(self):
        await self.session.update_context(context(price='12.34'))
        pinned = self.session.pin_voice_turn_context()
        await self.session.update_context(context(price='13.00', observed='2026-09-19T08:01:00Z'))
        self.session.active_turn_id = 'turn-pinned'

        await self.session.inject_delegation_context(pinned)

        text = self.calls[0][1]['input'][0]['text']
        self.assertIn('12.34', text)
        self.assertNotIn('13.00', text)

    async def test_failed_steer_never_marks_context_as_injected(self):
        await self.session.update_context(context())
        self.session.active_turn_id = 'turn-failed'

        async def fail_call(method, params, timeout=45):
            self.calls.append((method, params, timeout))
            raise RuntimeError('forced failure')

        self.session.call = fail_call
        await self.session.inject_delegation_context()

        self.assertFalse(self.session.turn_state('turn-failed')['contextInjected'])
        self.assertIsNone(self.session.backend_context_digest)

    async def test_old_client_context_gets_server_receive_time(self):
        old_context = context()
        old_context.pop('observedAtUtc')

        await self.session.update_context(old_context)

        self.assertIn('receivedAtUtc', self.session.ui_context)
        self.session.active_turn_id = 'turn-old-client'
        await self.session.inject_delegation_context()
        self.assertTrue(self.session.turn_state('turn-old-client')['contextObservedAtUtc'])

    async def test_stale_context_from_previous_stock_is_not_injected(self):
        await self.session.update_context(context())
        self.session.stock_code = '600000'
        self.session.active_turn_id = 'turn-new-stock'

        pinned = self.session.pin_voice_turn_context()
        await self.session.inject_voice_context(pinned)
        await self.session.inject_delegation_context(pinned)

        self.assertIsNone(pinned)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.session.turn_state('turn-new-stock')['contextInjected'])


if __name__ == '__main__':
    unittest.main()
