import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from voice_transcripts import TranscriptStreams

try:
    from voice import VoiceSession
except ImportError:
    VoiceSession = None


class TranscriptStreamTests(unittest.TestCase):
    def setUp(self):
        self.stream = TranscriptStreams('session-a')

    def test_delta_before_created_keeps_id_and_final_replaces_draft(self):
        draft = self.stream.realtime_delta('assistant', '股价')
        self.stream.realtime_start('assistant', 'voice-1')
        growing = self.stream.realtime_delta('assistant', '正在上涨')
        final = self.stream.realtime_final('assistant', '股价有变化。', 'voice-1')
        self.assertEqual(draft['messageId'], growing['messageId'])
        self.assertEqual(growing['messageId'], final['messageId'])
        self.assertEqual(growing['text'], '股价正在上涨')
        self.assertEqual(final['text'], '股价有变化。')
        self.assertTrue(final['final'])
        self.assertFalse(draft['final'])

    def test_multiple_segments_retain_earlier_sentence(self):
        self.stream.realtime_start('assistant', 'voice-1')
        self.stream.realtime_delta('assistant', '第一')
        self.stream.realtime_segment('assistant', '第一句。')
        growing = self.stream.realtime_delta('assistant', '第二句')
        self.assertEqual(growing['text'], '第一句。\n第二句')
        final = self.stream.realtime_final('assistant', '第一句。第二句。', 'voice-1')
        self.assertEqual(final['text'], '第一句。第二句。')

    def test_roles_and_new_turns_do_not_overwrite_each_other(self):
        self.stream.realtime_start('assistant', 'a1')
        first = self.stream.realtime_delta('assistant', '第一轮')
        self.stream.realtime_start('user', 'u1')
        user = self.stream.realtime_delta('user', '插话')
        self.stream.realtime_final('assistant', '第一轮。', 'a1')
        self.assertIsNone(self.stream.realtime_delta('assistant', '迟到的字'))
        self.stream.realtime_start('assistant', 'a2')
        second = self.stream.realtime_delta('assistant', '第二轮')
        # Duplicate old created/done must not steal the active role's new turn.
        self.stream.realtime_start('assistant', 'a1')
        self.assertIsNone(self.stream.realtime_final('assistant', '第一轮。', 'a1'))
        second_more = self.stream.realtime_delta('assistant', '继续')
        self.assertEqual(second_more['text'], '第二轮继续')
        self.assertEqual(second['messageId'], second_more['messageId'])
        self.assertEqual(len({first['messageId'], user['messageId'], second['messageId']}), 3)

    def test_final_without_created_also_blocks_late_delta(self):
        final = self.stream.realtime_final('user', '结束了', 'u1')
        self.assertIsNotNone(final)
        self.assertIsNone(self.stream.realtime_delta('user', '迟到'))
        self.assertIsNone(self.stream.realtime_segment('user', '迟到分段'))

    def test_late_old_final_does_not_close_new_turn(self):
        self.stream.realtime_start('assistant', 'a1')
        self.stream.realtime_delta('assistant', '旧')
        self.stream.realtime_start('assistant', 'a2')
        newer = self.stream.realtime_delta('assistant', '新')
        old = self.stream.realtime_final('assistant', '旧完成', 'a1')
        current = self.stream.realtime_delta('assistant', '继续')
        self.assertNotEqual(old['messageId'], newer['messageId'])
        self.assertEqual(current['messageId'], newer['messageId'])
        self.assertEqual(current['text'], '新继续')

    def test_backend_snapshot_does_not_append_and_final_blocks_late_items(self):
        first = self.stream.backend_item('b1', 'i1', delta='查询')
        self.stream.backend_item('b1', 'i1', delta='完成')
        canonical = self.stream.backend_item('b1', 'i1', text='查询完成。')
        self.assertEqual(first['messageId'], canonical['messageId'])
        self.assertEqual(canonical['text'], '查询完成。')
        final = self.stream.finish_backend('b1')
        self.assertEqual(len(final), 1)
        self.assertTrue(final[0]['final'])
        self.assertEqual(final[0]['text'], '查询完成。')
        self.assertIsNone(self.stream.backend_item('b1', 'i1', delta='迟到'))
        self.assertIsNone(self.stream.backend_item('b1', 'i2', delta='迟到新条目'))
        self.assertEqual(self.stream.finish_backend('b1', failure='播报失败'), [])

    def test_backend_items_realtime_and_reconnect_have_distinct_ids(self):
        backend1 = self.stream.backend_item('same', 'i1', delta='正在查询')
        backend2 = self.stream.backend_item('same', 'i2', delta='结果')
        self.stream.realtime_start('assistant', 'same')
        realtime = self.stream.realtime_delta('assistant', '语音回复')
        reconnect = TranscriptStreams('session-b').backend_item('same', 'i1', delta='新连接')
        self.assertEqual(len({e['messageId'] for e in (backend1, backend2, realtime, reconnect)}), 4)

    def test_failure_replaces_provisional_text_and_closes_it(self):
        self.stream.backend_item('b1', 'i1', delta='尚未验证的数值')
        final = self.stream.finish_backend('b1', failure='数据未核实')
        self.assertEqual(final[0]['text'], '数据未核实')
        self.assertTrue(final[0]['final'])

    def test_storage_and_snapshots_are_bounded(self):
        event = self.stream.backend_item('long', 'item', delta='x' * 50000)
        self.assertEqual(len(event['text']), 32000)
        for index in range(140):
            self.stream.backend_item(str(index), 'item', delta='x')
            self.stream.finish_backend(str(index))
        self.assertLessEqual(len(self.stream.entries), 128)
        self.assertLessEqual(len(self.stream.finished_backend), 128)


@unittest.skipIf(VoiceSession is None, 'VoiceSession runtime dependencies unavailable')
class TranscriptDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = []

        async def emit(event):
            self.events.append(event)

        self.session = VoiceSession('stream-test', self.directory.name, None, emit, AsyncMock())
        self.session.thread_id = 'thread-test'

    async def asyncTearDown(self):
        for task in list(self.session.tasks):
            task.cancel()
        await asyncio.gather(*list(self.session.tasks), return_exceptions=True)
        await self.session.pc.close()
        self.directory.cleanup()

    async def test_burst_coalesces_and_final_immediately_replaces_pending(self):
        self.session.transcript_streams.realtime_start('assistant', 'a1')
        for text in ('第', '一', '句'):
            self.session.stream_realtime({'role': 'assistant', 'delta': text})
        self.assertEqual(len(self.session.transcript_updates), 1)
        await self.session.transcript_flush
        self.assertEqual([event['text'] for event in self.events], ['第一句'])
        self.session.stream_realtime({'role': 'assistant', 'delta': '草稿'})
        self.session.on_dc(json.dumps({'type': 'turn.done', 'turn': {
            'role': 'assistant', 'id': 'a1', 'transcript': '第一句。'}}))
        await asyncio.sleep(0)
        self.assertEqual(self.events[-1]['text'], '第一句。')
        self.assertTrue(self.events[-1]['final'])
        self.assertEqual(len(self.session.transcript_updates), 0)
        await self.session.transcript_flush
        self.assertEqual(len(self.events), 2)
        self.assertEqual(len(self.session.recent_transcripts()), 1)

    async def test_final_tail_after_close_is_persisted_without_sending_or_new_tasks(self):
        self.session.transcript_streams.realtime_start('assistant', 'tail')
        self.session.closed = True
        self.session.on_dc(json.dumps({'type': 'turn.done', 'turn': {
            'role': 'assistant', 'id': 'tail', 'transcript': '通话结束时的最后一句。'}}))
        self.assertEqual(len(self.session.tasks), 0)
        self.assertIsNone(self.session.transcript_flush)
        self.assertEqual(self.session.transcript_updates, {})
        await asyncio.sleep(0)
        self.assertEqual(self.events, [])
        restored = self.session.recent_transcripts()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['text'], '通话结束时的最后一句。')

    async def test_read_forwards_only_visible_deltas_and_canonical_item(self):
        reader = asyncio.StreamReader()
        self.session.proc = SimpleNamespace(stdout=reader)
        for method, params in (
            ('item/started', {'turnId': 'b1', 'item': {'type': 'agentMessage', 'id': 'i1'}}),
            ('item/agentMessage/delta', {'turnId': 'b1', 'itemId': 'i1', 'delta': '结果'}),
            ('item/reasoning/textDelta', {'turnId': 'b1', 'itemId': 'secret', 'delta': '不可显示'}),
            ('item/completed', {'turnId': 'b1', 'item': {'type': 'agentMessage', 'id': 'i1', 'text': '结果。'}}),
            ('thread/realtime/transcript/delta', {'role': 'assistant', 'delta': '语音'}),
            ('thread/realtime/transcript/delta', {'role': 'assistant', 'delta': '草稿'}),
        ):
            reader.feed_data((json.dumps({'method': method, 'params': {'threadId': 'thread-test', **params}}) + '\n').encode())
        reader.feed_eof()
        await self.session.read()
        await self.session.transcript_flush
        texts = [event['text'] for event in self.events if event['type'] == 'transcript']
        self.assertEqual(texts, ['结果。', '语音草稿'])
        self.assertEqual(len({event['messageId'] for event in self.events if event['type'] == 'transcript'}), 2)

    async def test_backend_final_and_append_speech_echo_are_not_duplicated(self):
        self.session.call = AsyncMock(return_value={})
        self.session.transcript_streams.backend_item('b1', 'i1', delta='你好')
        await self.session.finish_turn({'id': 'b1', 'status': 'completed', 'items': [
            {'id': 'i1', 'type': 'agentMessage', 'text': '你好。', 'phase': 'final_answer'}]})
        await asyncio.sleep(0)
        self.session.on_dc(json.dumps({'type': 'turn.created', 'turn': {'id': 'a1', 'role': 'assistant'}}))
        self.session.stream_realtime({'role': 'assistant', 'delta': '你好'})
        self.session.on_dc(json.dumps({'type': 'turn.done', 'turn': {'id': 'a1', 'role': 'assistant', 'transcript': '你好。'}}))
        await asyncio.sleep(0)
        transcripts = [event for event in self.events if event['type'] == 'transcript']
        self.assertEqual(len(transcripts), 1)
        self.assertTrue(transcripts[0]['final'])
        self.assertEqual(transcripts[0]['text'], '你好。')

    async def test_failed_append_speech_reports_error_without_second_final(self):
        self.session.call = AsyncMock(side_effect=RuntimeError('transport unavailable'))
        self.session.transcript_streams.backend_item('b1', 'i1', delta='你好')
        await self.session.finish_turn({'id': 'b1', 'status': 'completed', 'items': [
            {'id': 'i1', 'type': 'agentMessage', 'text': '你好。', 'phase': 'final_answer'}]})
        await asyncio.sleep(0)
        transcripts = [event for event in self.events if event['type'] == 'transcript']
        self.assertEqual(len(transcripts), 1)
        self.assertEqual(transcripts[0]['text'], '你好。')
        self.assertTrue(any(event['type'] == 'error' for event in self.events))


if __name__ == '__main__':
    unittest.main()
