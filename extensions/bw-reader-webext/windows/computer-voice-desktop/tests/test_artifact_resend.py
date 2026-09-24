import asyncio
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_jev_injection import InjectionTests
from voice_artifact_resend import ArtifactResender


class ResendTests(unittest.IsolatedAsyncioTestCase):
    def make(self, status='attempted'):
        r, key = InjectionTests().make(choice='resend_latest_artifact', context_choice='no_extra_context')
        r.settings['jevResendEnabled'] = True
        r.snap['revision'] = 9
        r.snap['activeReading'] = {'sourceInstanceId': 'source'}
        calls = []
        state = {'status': status}
        def rpc(body=None, request_key=None):
            if body is not None:
                calls.append(body)
                return {'ok': True, 'status': 'attempted', 'title': '原注解', 'artifactId': 'original'}
            return {'ok': True, **state, 'error': 'simulated-failure'}
        action = ArtifactResender(r, rpc)
        r._jev.artifact_source = lambda: {'id': 'original', 'title': '原注解'}
        r._jev.on_prepared = action.prepared
        return r, key, action, calls, state

    async def test_notice_before_receipt_success_stays_silent(self):
        r, key, action, calls, state = self.make()
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(r.messages), 1)
        text = r.messages[0][1]['input'][0]['text']
        self.assertIn('已根据本轮用户请求尝试重发', text)
        self.assertIn('保持沉默', text)
        self.assertNotIn('CURRENT_PAGE', text)
        self.assertNotIn('已送达', text)
        self.assertTrue(action.jobs)
        state['status'] = 'delivered'
        await asyncio.gather(*action.jobs)
        self.assertEqual(len(r.messages), 1)
        r._jev.assistant_started(key)
        await r._ctx_on_delegation(key, 2)
        self.assertEqual(len(calls), 1)

    async def test_failure_alone_adds_receipt(self):
        r, key, action, calls, state = self.make('failed')
        await r._ctx_on_delegation(key, 1)
        await asyncio.gather(*action.jobs)
        self.assertEqual(len(r.messages), 2)
        self.assertIn('发送失败', r.messages[1][1]['input'][0]['text'])
        rows = [x for x in r.logs if x.get('contextSource') == 'artifact-resend-failure']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'ctx_steer')
        self.assertEqual(rows[0]['body'], r.messages[1][1]['input'][0]['text'])
        self.assertEqual(rows[0]['backendTurnId'], 'backend-turn')

    async def test_failure_new_turn_is_logged_only_after_acceptance(self):
        r, key, action, calls, state = self.make()
        r.backend_busy = False
        await action._failure('thread', '失败回执', key)
        self.assertEqual(r.messages[0][0], 'turn/start')
        rows = [x for x in r.logs if x.get('contextSource') == 'artifact-resend-failure']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'ctx_backend')
        self.assertEqual(rows[0]['body'], '失败回执')

    async def test_notice_rejection_is_not_logged_as_injected(self):
        r, key, action, calls, state = self.make()
        async def reject(*args, **kwargs):
            raise RuntimeError('delivery rejected')
        r.app.call = reject
        await action._failure('thread', '失败回执', key)
        self.assertFalse(any(x.get('contextSource') == 'artifact-resend-failure' for x in r.logs))
        self.assertTrue(any(x['kind'] == 'artifact_failure_notice_error' for x in r.logs))

    async def test_no_delegation_can_send_but_does_not_inject(self):
        r, key, action, calls, state = self.make('delivered')
        r._jev.assistant_started(key)
        await r._jev.records[key]['event'].wait()
        await asyncio.gather(*action.jobs)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r.messages, [])

    async def test_low_confidence_does_not_send(self):
        r, key, action, calls, state = self.make()
        r._jev.predictor = lambda *_: {'choice': 'resend_latest_artifact', 'probability': .4}
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(calls, [])
        self.assertEqual(r.messages, [])

    async def test_stale_round_does_not_send(self):
        r, key, action, calls, state = self.make()
        rec = r._jev.records[key]
        rec.update(result={'choice':'resend_latest_artifact','probability':.99}, artifact={'id':'original'})
        r._jev.observe('thread:1:new-round', '取消', new_turn=True)
        await action.prepared(rec, r.snap)
        self.assertEqual(calls, [])

if __name__ == '__main__':
    unittest.main()
