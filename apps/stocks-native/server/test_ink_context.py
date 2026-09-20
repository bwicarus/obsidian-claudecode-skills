import asyncio
import base64
from datetime import datetime, timezone
import json
import itertools
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from ink_context import InkStandby, jpeg_dimensions

try:
    from voice import VoiceSession
    import app as gateway
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:
    VoiceSession = None
    gateway = None


# A generated, public-domain 2 x 2 white JPEG, not a user screenshot.
JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/'
    '2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/'
    '8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/'
    '8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iiigD//2Q=='
)
SEQUENCE = itertools.count(1)


def payload(scope='scope-1', age=0, jpeg=JPEG, **extra):
    return {
        'id': 'client-1', 'sequence': next(SEQUENCE), 'stockCode': '000001', 'scopeID': scope,
        'capturedAt': datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat(),
        'sourceTime': '2026-09-20T15:00:00Z', 'cardIDs': ['chart'], 'inkCardIDs': ['chart'],
        'bounds': {'chart': {'x': 0, 'y': 0, 'width': 2, 'height': 2}},
        'cards': [{'id': 'chart', 'title': '分时', 'data': {'price': 11.7}}],
        'jpegBase64': base64.b64encode(jpeg).decode(), 'cleared': False, **extra,
    }


def context(scope='scope-1', code='000001', **view):
    return {'screen': 'stock_detail', 'selectedCode': code, 'chartPeriodID': 'intraday',
            'metrics': {'price': '11.70'},
            'viewState': {'detailPresented': True, 'inkScopeID': scope, **view}}


class InkStandbyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ink = InkStandby(self.directory.name, 'session-test')

    def tearDown(self):
        self.ink.close()
        self.directory.cleanup()

    def test_upload_stores_private_file_and_content_digest_ignores_capture_clock(self):
        receipt = self.ink.update(payload(), '000001')
        entry = self.ink.pin(context())
        self.assertEqual(receipt, {'id': 'client-1', 'status': 'stored'})
        self.assertEqual(jpeg_dimensions(Path(entry['path']).read_bytes()), (2, 2))
        self.assertEqual(Path(entry['path']).parent, self.ink.directory)
        self.ink.update(payload(age=1, id='retry'), '000001')
        self.assertEqual(self.ink.current['id'], entry['id'])
        self.assertEqual(len(self.ink.entries), 1)
        if os.name != 'nt':
            self.assertEqual(Path(entry['path']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.ink.directory.stat().st_mode & 0o777, 0o700)

    def test_invalid_stock_age_scope_cards_and_payload_never_create_files(self):
        cases = [payload(stockCode='000002'), payload(age=901), payload(age=-61),
                 payload(scope=''), payload(cardIDs=['a', 'b', 'c', 'd']),
                 payload(jpegBase64='not base64'), payload(jpeg=JPEG + b'x' * 225_281)]
        for value in cases:
            with self.subTest(value_keys=[key for key in value if key != 'jpegBase64']):
                with self.assertRaises(ValueError):
                    self.ink.update(value, '000001')
                self.assertIsNone(self.ink.current)
        self.assertFalse(self.ink.directory.exists())

    def test_pin_rejects_other_stock_range_hidden_or_expired_context(self):
        self.ink.update(payload(), '000001')
        for value in [context(code='000002'), context(scope='other-range'),
                      context(detailPresented=False), context(settingsPresented=True),
                      context(selectionEditorPresented=True)]:
            self.assertIsNone(self.ink.pin(value))
        self.ink.current['captured'] = time.time() - 901
        self.assertIsNone(self.ink.pin(context()))

    def test_clear_removes_standby_and_close_removes_files(self):
        self.ink.update(payload(), '000001')
        self.assertEqual(self.ink.update(payload(cleared=True), '000001')['status'], 'cleared')
        cleared = self.ink.pin(context())
        self.assertIsNone(cleared['path'])
        self.assertIn('已擦除', self.ink.text(cleared))
        self.ink.close()
        self.assertFalse(self.ink.directory.exists())

    def test_retention_bounds_keep_pinned_image_during_new_uploads(self):
        self.ink.update(payload(), '000001')
        protected = self.ink.current['id']
        for index in range(12):
            self.ink.update(payload(sourceTime=f'data-version-{index}'), '000001', protected)
        self.assertTrue((self.ink.directory / (protected + '.jpg')).exists())
        self.assertLessEqual(len(list(self.ink.directory.glob('*.jpg'))), 7)

    def test_late_scope_upload_or_clear_cannot_replace_newer_snapshot(self):
        older = payload(scope='scope-1')
        newer = payload(scope='scope-2')
        self.ink.update(newer, '000001')
        entry = self.ink.current
        for stale in [older, {**older, 'cleared': True}]:
            with self.assertRaises(ValueError):
                self.ink.update(stale, '000001')
            self.assertIs(self.ink.current, entry)
        self.assertEqual(self.ink.update(newer, '000001')['status'], 'stored')

    def test_orphan_cleanup_only_removes_old_uuid_directories(self):
        root = Path(self.directory.name) / 'ink-standby'
        old = root / 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        recent = root / 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        unrelated = root / 'unrelated-data'
        for folder in [old, recent, unrelated]:
            folder.mkdir(parents=True)
            (folder / 'image.jpg').write_bytes(JPEG)
        for folder in [old, unrelated]:
            os.utime(folder, (time.time() - 86401, time.time() - 86401))
        InkStandby(self.directory.name, 'cleanup-test')
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())


@unittest.skipIf(VoiceSession is None, 'voice dependencies unavailable; run in isolated VPS venv')
class InkVoiceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.session = VoiceSession('ink-test', self.directory.name, None, AsyncMock(), AsyncMock())
        self.session.supports_ui_context = True
        self.session.thread_id = 'thread-1'
        self.session.stock_code = '000001'
        self.session.ready.set()
        self.session.call = AsyncMock(return_value={})
        await self.session.update_context(context())

    async def asyncTearDown(self):
        for task in list(self.session.tasks):
            task.cancel()
        await asyncio.gather(*list(self.session.tasks), return_exceptions=True)
        await self.session.pc.close()
        self.session.ink.close()
        self.directory.cleanup()

    async def test_upload_does_not_call_model_and_voice_hint_has_no_image(self):
        await self.session.update_ink(payload())
        self.session.call.assert_not_awaited()
        pinned = self.session.pin_voice_turn_context()
        await self.session.inject_voice_context(pinned)
        call = self.session.call.await_args
        self.assertEqual(call.args[0], 'thread/realtime/appendText')
        params = call.args[1]
        self.assertEqual(params['role'], 'developer')
        self.assertIn('未向语音模型附图', params['text'])
        self.assertNotIn('jpegBase64', json.dumps(params))
        self.assertNotIn('input_image', json.dumps(params))

    async def test_steer_image_dedupe_advances_only_after_success_and_new_thread_resends(self):
        await self.session.update_ink(payload())
        pinned = self.session.pin_voice_turn_context()
        self.session.active_turn_id = 'turn-1'
        self.session.call.side_effect = RuntimeError('temporary failure')
        await self.session.inject_delegation_context(pinned)
        self.assertIsNone(self.session.ink_ledgers['backend'])
        self.session.call.side_effect = None
        await self.session.inject_delegation_context(pinned)
        params = self.session.call.await_args.args[1]
        self.assertEqual(self.session.call.await_args.args[0], 'turn/steer')
        self.assertEqual(params['expectedTurnId'], 'turn-1')
        self.assertEqual([item['type'] for item in params['input']], ['text', 'localImage'])
        self.assertTrue(Path(params['input'][1]['path']).is_file())
        count = self.session.call.await_count
        await self.session.inject_delegation_context(pinned)
        self.assertEqual(self.session.call.await_count, count)
        self.session.thread_id = 'thread-2'
        await self.session.inject_delegation_context(pinned)
        self.assertEqual(self.session.call.await_count, count + 1)

    async def test_new_scope_or_erasure_before_user_turn_does_not_inject_old_image(self):
        await self.session.update_ink(payload())
        await self.session.update_context(context(scope='scope-2'))
        self.assertIsNone(self.session.pin_voice_turn_context() and self.session.voice_turn_ink)
        await self.session.update_context(context())
        await self.session.update_ink(payload(cleared=True))
        self.session.pin_voice_turn_context()
        self.assertIsNone(self.session.voice_turn_ink['path'])
        self.session.active_turn_id = 'turn-cleared'
        await self.session.inject_delegation_context(self.session.voice_turn_context)
        params = self.session.call.await_args.args[1]
        self.assertEqual([item['type'] for item in params['input']], ['text'])
        self.assertIn('已擦除', params['input'][0]['text'])

    async def test_stock_change_and_old_pinned_turn_cannot_steer_image(self):
        await self.session.update_ink(payload())
        pinned = self.session.pin_voice_turn_context()
        self.session.stock_code = '000002'
        self.session.active_turn_id = 'turn-old'
        await self.session.inject_delegation_context(pinned)
        self.session.call.assert_not_awaited()
        self.session.closed = True
        with self.assertRaises(ValueError):
            await self.session.update_ink(payload())


@unittest.skipIf(gateway is None, 'aiohttp gateway dependencies unavailable')
class InkHTTPBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'STOCKS_MVP_STATE_DIR': self.directory.name,
                                     'STOCKS_DATA_ROOT': self.directory.name,
                                     'STOCKS_MONITOR_ENABLED': '0'}):
            self.app = gateway.create_app()
        self.fake = type('Session', (), {'closed': False, 'selection_owner': 'owner-1',
                                         'session_id': 'session-1', 'update_ink': AsyncMock(return_value={'id': 'client-1', 'status': 'stored'})})()
        self.app['voices']['device-1'] = (None, self.fake)
        self.identity = patch.object(gateway, 'identity', AsyncMock(return_value={'ownerId': 'owner-1', 'aiEnabled': True}))
        self.identity.start()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {'X-Device-ID': 'device-1', 'X-Voice-Session': 'session-1'}

    async def asyncTearDown(self):
        self.app['voices'].clear()
        await self.client.close()
        self.identity.stop()
        self.directory.cleanup()

    async def test_valid_composite_over_128kb_reaches_340kb_route_budget(self):
        comment = b'\xff\xfe' + struct.pack('>H', 60_002) + b'x' * 60_000
        image = JPEG[:2] + comment + comment + JPEG[2:]
        value = payload(jpeg=image)
        self.assertGreater(len(json.dumps(value).encode()), 128_000)
        response = await self.client.post('/api/voice/ink', json=value, headers=self.headers)
        self.assertEqual(response.status, 200, await response.text())
        self.fake.update_ink.assert_awaited_once()

    async def test_old_session_and_other_owner_never_store(self):
        for header, owner in [({'X-Voice-Session': 'old-session'}, 'owner-1'), ({}, 'other-owner')]:
            self.fake.selection_owner = owner
            response = await self.client.post('/api/voice/ink', json=payload(), headers={**self.headers, **header})
            self.assertEqual(response.status, 409)
        self.fake.update_ink.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
