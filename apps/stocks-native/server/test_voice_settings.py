"""Temporary accounts and mocked capabilities; no model or realtime calls."""
import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from app import create_app
from voice import VoiceSession
from voice_settings import DEFAULTS, VoiceSettingsService, normalize_catalog


CATALOG = normalize_catalog([
    {"id": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol", "defaultReasoningEffort": "low",
     "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}]},
    {"id": "gpt-5.6-luna", "displayName": "GPT-5.6-Luna", "defaultReasoningEffort": "high",
     "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
    {"id": "no-reasoning", "supportedReasoningEfforts": []},
], {"voices": {"v1": ["sol", "cove"], "v2": ["marin"]}})
CHANGED = {"backendModel": "gpt-5.6-luna", "effort": "high", "voice": "cove"}


class VoiceSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        with patch.dict(os.environ, {"STOCKS_MVP_STATE_DIR": str(root / "state"),
                                    "STOCKS_DATA_ROOT": str(root / "market"), "STOCKS_MONITOR_ENABLED": "0"}):
            self.app = create_app()
        self.service = self.app['voice_settings']
        self.service._loader = AsyncMock(return_value=CATALOG)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.first = self.app['auth'].apple_login('settings-account-one', 'settings-ipad', 'iPad')
        self.same = self.app['auth'].apple_login('settings-account-one', 'settings-phone', 'iPhone')
        self.other = self.app['auth'].apple_login('settings-account-two', 'settings-other', 'iPad')
        self.owner = self.app['auth'].authenticate(self.first['token'], self.first['deviceId'])['ownerId']

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    @staticmethod
    def headers(account):
        return {'Authorization': 'Bearer ' + account['token'], 'X-Device-ID': account['deviceId']}

    async def test_authentication_review_and_device_boundaries(self):
        for method, route in [('get', 'catalog'), ('get', 'settings'), ('post', 'settings')]:
            response = await getattr(self.client, method)('/api/voice/' + route)
            self.assertEqual(response.status, 401)
        wrong = self.headers(self.first) | {'X-Device-ID': self.other['deviceId']}
        self.assertEqual((await self.client.get('/api/voice/catalog', headers=wrong)).status, 401)
        review = self.app['auth'].pair(self.app['auth'].create_review_pairing_code()['code'], 'review-settings', 'Review')
        for method, route in [('get', 'catalog'), ('get', 'settings'), ('post', 'settings')]:
            response = await getattr(self.client, method)('/api/voice/' + route, headers=self.headers(review))
            self.assertEqual(response.status, 403)
        self.service._loader.assert_not_awaited()

    async def test_defaults_then_same_account_devices_share_persisted_settings(self):
        response = await self.client.get('/api/voice/settings', headers=self.headers(self.first))
        self.assertEqual(await response.json(), {'settings': DEFAULTS, 'applies': 'next_connection'})
        response = await self.client.post('/api/voice/settings', headers=self.headers(self.first), json=CHANGED)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'settings': CHANGED, 'applies': 'next_connection'})
        same = await self.client.get('/api/voice/settings', headers=self.headers(self.same))
        other = await self.client.get('/api/voice/settings', headers=self.headers(self.other))
        self.assertEqual((await same.json())['settings'], CHANGED)
        self.assertEqual((await other.json())['settings'], DEFAULTS)
        restarted = VoiceSettingsService(self.service.root, catalog_loader=AsyncMock(return_value=CATALOG))
        self.assertEqual(restarted.load(self.owner), CHANGED)

    async def test_real_catalog_whitelist_rejects_unknown_options_and_owner_injection(self):
        invalid = [CHANGED | {'backendModel': 'invented'}, CHANGED | {'effort': 'ultra'},
                   CHANGED | {'voice': 'marin'}, CHANGED | {'ownerId': 'someone-else'},
                   CHANGED | {'effort': None}]
        for payload in invalid:
            response = await self.client.post('/api/voice/settings', headers=self.headers(self.first), json=payload)
            self.assertEqual(response.status, 400, payload)
        self.assertEqual(self.service.load(self.owner), DEFAULTS)
        await self.service.save(self.owner, {'backendModel': 'no-reasoning', 'effort': '', 'voice': 'sol'})
        self.assertEqual(self.service.load(self.owner)['effort'], '')

    async def test_catalog_shape_and_concurrent_cache_use_no_voice_session(self):
        results = await asyncio.gather(*(self.service.catalog() for _ in range(4)))
        self.service._loader.assert_awaited_once()
        self.assertEqual(results[0]['voices'], [{'id': 'sol', 'displayName': 'sol'}, {'id': 'cove', 'displayName': 'cove'}])
        results[0]['voices'].clear()
        self.assertEqual(len((await self.service.catalog())['voices']), 2)
        response = await self.client.get('/api/voice/catalog', headers=self.headers(self.first))
        self.assertEqual(await response.json(), CATALOG)
        self.assertEqual(self.app['voices'], {})

    async def test_catalog_failure_is_explicit_and_does_not_replace_saved_preferences(self):
        self.service._loader.side_effect = RuntimeError('provider unavailable')
        response = await self.client.get('/api/voice/catalog', headers=self.headers(self.first))
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['code'], 'voice_catalog_unavailable')
        response = await self.client.get('/api/voice/settings', headers=self.headers(self.first))
        self.assertEqual((await response.json())['settings'], DEFAULTS)

    async def test_save_does_not_change_existing_connection_snapshot(self):
        current = object.__new__(VoiceSession)
        current.selection_owner, current.voice_settings_service = self.owner, self.service
        await current.load_voice_preferences()
        await self.service.save(self.owner, CHANGED)
        self.assertEqual(current.voice_preferences, DEFAULTS)
        following = object.__new__(VoiceSession)
        following.selection_owner, following.voice_settings_service = self.owner, self.service
        self.assertEqual(await following.load_voice_preferences(), CHANGED)

    async def test_connection_applies_settings_to_new_and_resumed_thread_and_realtime_voice(self):
        await self.service.save(self.owner, CHANGED)
        for resumed in (False, True):
            session = VoiceSession('settings-' + str(resumed), self.directory.name, None,
                                   AsyncMock(), AsyncMock(), selection_owner=self.owner)
            session.voice_settings_service = self.service
            await session.pc.close()
            session.pc = SimpleNamespace(addTrack=lambda _: None,
                createDataChannel=lambda _: SimpleNamespace(on=lambda _: lambda callback: None),
                on=lambda _: lambda callback: callback, setLocalDescription=AsyncMock(),
                createOffer=AsyncMock(return_value='offer'), localDescription=SimpleNamespace(sdp='offer'))
            if resumed:
                session.thread_file.write_text('existing-thread')
            session.read, session.stderr, session.send = AsyncMock(), AsyncMock(), AsyncMock()
            calls = []
            class RealtimeBoundary(Exception):
                pass
            async def rpc(method, params, **kwargs):
                calls.append((method, params))
                if method in ('thread/start', 'thread/resume'):
                    return {'thread': {'id': 'existing-thread' if resumed else 'new-thread'}}
                if method == 'thread/realtime/start':
                    raise RealtimeBoundary()
                return {}
            session.call = rpc
            with patch('voice.asyncio.create_subprocess_exec', AsyncMock(return_value=SimpleNamespace(returncode=0))):
                with self.assertRaises(RealtimeBoundary):
                    await session.start()
            await asyncio.gather(*session.tasks)
            params = next(p for method, p in calls if method == ('thread/resume' if resumed else 'thread/start'))
            self.assertEqual(params['model'], CHANGED['backendModel'])
            self.assertEqual(params['config']['model_reasoning_effort'], CHANGED['effort'])
            realtime = next(p for method, p in calls if method == 'thread/realtime/start')
            self.assertEqual(realtime['voice'], CHANGED['voice'])
            self.assertEqual(realtime['version'], 'v3')


if __name__ == '__main__':
    unittest.main()
