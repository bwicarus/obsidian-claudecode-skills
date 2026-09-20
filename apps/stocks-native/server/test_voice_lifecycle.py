import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from voice import VoiceSession, closes_voice_connection


class VoiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = []

        async def emit_json(event):
            self.events.append(event)

        async def emit_audio(_):
            return None

        self.session = VoiceSession(
            'lifecycle-test', self.directory.name, None, emit_json, emit_audio)

    async def asyncTearDown(self):
        for task in list(self.session.tasks):
            task.cancel()
        if self.session.tasks:
            await asyncio.gather(*list(self.session.tasks), return_exceptions=True)
        await self.session.pc.close()
        self.directory.cleanup()

    async def test_only_fatal_errors_or_closed_state_close_connection(self):
        self.assertFalse(closes_voice_connection({'type': 'error', 'fatal': False}))
        self.assertFalse(closes_voice_connection({'type': 'error'}))
        self.assertTrue(closes_voice_connection({'type': 'error', 'fatal': True}))
        self.assertTrue(closes_voice_connection({'type': 'state', 'state': 'closed'}))
        self.assertFalse(closes_voice_connection({'type': 'state', 'state': 'active'}))

    async def test_recent_transcripts_scans_past_noise_and_has_stable_ids(self):
        self.session.state_dir.mkdir(parents=True, exist_ok=True)
        with self.session.journal.open('w', encoding='utf-8') as journal:
            for index in range(12):
                event = {'type': 'transcript', 'role': 'user' if index % 2 == 0 else 'assistant',
                         'text': f'message-{index}', 'requestId': f'request-{index}'}
                journal.write(json.dumps(event, ensure_ascii=False) + '\n')
            # More than one read block of non-transcript tail verifies that the
            # implementation does not inspect only a fixed number of final rows.
            for index in range(1600):
                journal.write(json.dumps({'type': 'usage', 'index': index, 'padding': 'x' * 80}) + '\n')

        first = self.session.recent_transcripts(8)
        second = self.session.recent_transcripts(8)

        self.assertEqual([item['text'] for item in first], [f'message-{i}' for i in range(4, 12)])
        self.assertEqual([item['id'] for item in first], [item['id'] for item in second])
        self.assertEqual(first[0]['id'], 'request-4:user')
        self.assertEqual(first[-1]['id'], 'request-11:assistant')

    async def test_reset_thread_removes_saved_threads_and_hides_old_history(self):
        self.session.state_dir.mkdir(parents=True, exist_ok=True)
        self.session.record({'type': 'transcript', 'role': 'user', 'text': 'old',
                             'messageId': 'old-message'})
        legacy = self.session.journal.with_suffix('.thread')
        current = self.session.journal.with_suffix('.thread-v2')
        legacy.write_text('legacy-thread')
        current.write_text('current-thread')

        self.session.reset_thread()

        self.assertFalse(legacy.exists())
        self.assertFalse(current.exists())
        self.assertEqual(self.session.recent_transcripts(8), [])
        self.session.record({'type': 'transcript', 'role': 'assistant', 'text': 'new',
                             'messageId': 'new-message'})
        self.assertEqual(self.session.recent_transcripts(8), [
            {'id': 'new-message', 'role': 'assistant', 'text': 'new'}])

    async def test_speaking_flags_span_turn_without_transcript(self):
        self.session.on_dc(json.dumps({'type': 'turn.created', 'turn': {'role': 'user'}}))
        self.assertTrue(self.session.user_speaking)
        self.session.on_dc(json.dumps({'type': 'turn.done', 'turn': {'role': 'user'}}))
        self.assertFalse(self.session.user_speaking)

        self.session.on_dc(json.dumps({'type': 'turn.created', 'turn': {'role': 'assistant'}}))
        self.assertTrue(self.session.assistant_speaking)
        self.session.on_dc(json.dumps({'type': 'turn.done', 'turn': {'role': 'assistant'}}))
        self.assertFalse(self.session.assistant_speaking)

    async def test_live_transcript_includes_persisted_message_id(self):
        self.session.on_dc(json.dumps({
            'type': 'turn.done',
            'turn': {'id': 'realtime-turn', 'role': 'user', 'transcript': '查看平安银行'},
        }, ensure_ascii=False))
        await asyncio.sleep(0)

        transcript = next(event for event in self.events if event.get('type') == 'transcript')
        self.assertTrue(transcript['messageId'].endswith(':user'))
        restored = self.session.recent_transcripts(1)
        self.assertEqual(restored[0]['id'], transcript['messageId'])
        self.assertEqual(restored[0]['text'], '查看平安银行')

    async def test_close_stops_realtime_before_terminating_process(self):
        class FakeStdin:
            def close(self):
                return None

        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.stdin = FakeStdin()

            async def wait(self):
                self.returncode = 0
                return 0

        calls = []

        async def call(method, params, timeout=45):
            calls.append((method, params, timeout))
            return {}

        self.session.thread_id = 'thread-live'
        self.session.proc = FakeProcess()
        self.session.call = call

        await self.session.close()

        self.assertEqual(calls, [
            ('thread/realtime/stop', {'threadId': 'thread-live'}, 8)])

    async def test_failed_first_thread_write_does_not_publish_unresumable_pointer(self):
        async def rpc(method, params, timeout=45):
            if method == 'thread/start':
                return {'thread': {'id': 'not-yet-persisted-thread'}}
            if method == 'thread/inject_items':
                raise PermissionError('thread-store: permission denied')
            return {}

        self.session.call = rpc
        self.session.send = AsyncMock()
        # No reader tasks, real Codex process or voice transport are needed to
        # reproduce a store failure immediately after thread/start returns.
        self.session.task = lambda coroutine: coroutine.close()
        with patch('voice.asyncio.create_subprocess_exec', new=AsyncMock(return_value=object())):
            with self.assertRaises(PermissionError):
                await self.session.start()

        self.assertFalse(self.session.thread_file.exists())
        marker = self.session.thread_file.with_suffix(self.session.thread_file.suffix + '.capabilities.json')
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
