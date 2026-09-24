import asyncio
import copy
import sys
import time
import unittest
import json
import tempfile
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_jev_context import JevContext, TOOL_DATA, TOOL_NEEDS, build_tool_context, original_passage, load_review_candidates, build_review_candidates, refresh_review_candidates
from voice_cli_runner import Runner


class InjectionTests(unittest.IsolatedAsyncioTestCase):
    def make(self, probability=.97, delay=0, choice='reader_card',
             context_choice='reader_context', context_probability=.98):
        r = Runner.__new__(Runner)
        r.settings = {'jevContextEnabled': True, 'jevWaitMilliseconds': 70,
                      'contextInjectEnabled': True, 'contextInjectOn': 'delegationSteer'}
        r.thread_id = 'thread'; r.session_no = 1
        r.backend_busy = True; r._turn = {'id': 'backend-turn'}; r._delegation_seq = 1
        r._ctx = {'thread': 'thread', 'page_key': 'book:44',
                  'fp': {'backend_state': '', 'backend_text': ''}}
        r._ink_sent = set(); r.events = []; r.transcripts = []
        r.messages = []; r.logs = []; r.predictions = []
        r.log = lambda kind, **kw: r.logs.append({'kind': kind, **kw})
        r._log_body = lambda value: value
        r._ink_standby_pending = lambda snap: []
        r.snap = {'contextStatus': 'ready', 'selectedItems': [{'kind': 'text', 'text': '麻疹'}],
                  'currentPage': {'file': 'book', 'page': 44, 'textAvailable': True,
                                  'text': '【当前页之前】\nOLDER_PAGE\n【当前页】\n[01] 麻疹 CURRENT_PAGE\n【当前页之后】\nNEXT_PAGE'}}
        r._ctx_snapshot = lambda: r.snap
        r._ctx_build = lambda snap: {'state': '新鲜状态 '+str(snap['selectedItems']),
                                     'text': snap['currentPage']['text'], 'fp_state': 'state', 'fp_text': 'text'}
        class App:
            async def call(self, method, params, **kwargs):
                r.messages.append((method, copy.deepcopy(params)))
        r.app = App()
        def predict(state, settings):
            r.predictions.append(state)
            time.sleep(delay)
            return {'choice': choice, 'probability': probability,
                    'contextChoice': context_choice, 'contextProbability': context_probability}
        r._jev = JevContext(r.settings, r._ctx_snapshot, r._jev_dialogue, r._jev_task_state, r.log, predict)
        key = r._jev_key('voice-turn')
        r._jev.observe(key, '给麻疹加注解', new_turn=True)
        r._jev.delegated(key, '给麻疹加注解')
        return r, key

    async def prepare(self, r):
        r._jev.assistant_started(r._jev.latest)
        await r._jev.records[r._jev.latest]['event'].wait()

    def review_fixture(self):
        return {'status': 'ready', 'total': 1, 'new': 1, 'due': 0,
                'cards': [{'gid': 'card_real', 'index': 0, 'noteId': 123,
                           'bookTitle': '复习书', 'page': 9, 'kind': 'new',
                           'frontText': '题面完整内容', 'backText': '答案完整内容',
                           'gradable': False, 'blockedReason': 'not-exported'}]}

    async def test_review_prefetch_offline_only_injects_cards_once(self):
        r, key = self.make(choice='review_deck')
        r.snap = None
        r.settings['jevWaitMilliseconds'] = 1500
        reads = []
        def read():
            reads.append(True)
            return self.review_fixture()
        r._jev.review_source = read
        await self.prepare(r)
        self.assertEqual(r.messages, [])  # preparation never starts speaking or grading
        await r._ctx_on_delegation(key, 1)
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(r.messages), 1)
        self.assertEqual(r.messages[0][0], 'turn/steer')
        text = r.messages[0][1]['input'][0]['text']
        for wanted in ('card_real', '"index": 0', '题面完整内容', '答案完整内容',
                       'not-exported', '未播题', '后台依据用户请求'):
            self.assertIn(wanted, text)
        self.assertNotIn('CURRENT_PAGE', text)
        self.assertNotIn('当前选中对象', text)

    async def test_review_page_change_does_not_discard_local_cards(self):
        r, key = self.make(choice='review_deck')
        r.settings['jevWaitMilliseconds'] = 1500
        r._jev.review_source = self.review_fixture
        await self.prepare(r)
        r.snap = {'contextStatus': 'unavailable'}
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(len(r.messages), 1)
        self.assertIn('card_real', r.messages[0][1]['input'][0]['text'])

    async def test_review_new_round_does_not_receive_previous_cards(self):
        r, key = self.make(choice='review_deck')
        r.settings['jevWaitMilliseconds'] = 1500
        r._jev.review_source = self.review_fixture
        await self.prepare(r)
        r._jev.observe('thread:1:new-turn', '不复习了', new_turn=True)
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_review_no_context_or_low_confidence_never_reads_cards(self):
        for kwargs in ({'context_choice': 'no_extra_context'}, {'probability': .4}):
            r, key = self.make(choice='review_deck', **kwargs)
            def forbidden():
                self.fail('Skipped requests must not read the card library')
            r._jev.review_source = forbidden
            await self.prepare(r)
            await r._ctx_on_delegation(key, 1)
            self.assertEqual(r.messages, [])

    async def test_review_read_failure_is_not_reported_as_empty_deck(self):
        r, key = self.make(choice='review_deck')
        r.settings['jevWaitMilliseconds'] = 1500
        def fail():
            raise OSError('replica unreadable')
        r._jev.review_source = fail
        await self.prepare(r)
        await r._ctx_on_delegation(key, 1)
        text = r.messages[0][1]['input'][0]['text']
        self.assertIn('未能提前取齐', text)
        self.assertIn('不代表没有待复习卡', text)
        self.assertNotIn('CURRENT_PAGE', text)

    async def test_slow_review_read_does_not_inject_after_deadline(self):
        r, key = self.make(choice='review_deck')
        def slow():
            time.sleep(.12)
            return self.review_fixture()
        r._jev.review_source = slow
        await r._ctx_on_delegation(key, 1)
        await r._jev.records[key]['event'].wait()
        self.assertEqual(r.messages, [])
        self.assertTrue(any(x.get('reason') == 'timeout' for x in r.logs))

    def test_review_loader_preserves_all_candidates_and_excludes_not_due(self):
        import review_deck
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'replication-data/book/document-notes.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'items': {'x': {'created': 1, 'card': {
                'gid': 'card_fixture', 'cards': [
                    {'front': '未学题', 'back': '未学答', '_ratingUnavailable': True,
                     '_ratingUnavailableReason': 'not-exported'},
                    {'front': '到期题', 'back': '到期答', '_next': 1},
                    {'front': '未来题', 'back': '未来答', '_next': int(time.time()*1000)+86400000},
                    {'front': '删除题', 'back': '删除答', '_removed': True}]}}}}), encoding='utf-8')
            before = path.read_bytes()
            with patch.object(review_deck, 'default_root', return_value=root):
                result = build_review_candidates()
            self.assertEqual(result['total'], 2)
            self.assertEqual(result['due'], 1)
            self.assertEqual(result['new'], 1)
            self.assertEqual({c['index'] for c in result['cards']}, {0, 1})
            self.assertIn('未学答', [c['backText'] for c in result['cards']])
            self.assertEqual(path.read_bytes(), before)

    def test_review_oversize_queue_is_not_silently_truncated(self):
        import review_deck
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'replication-data').mkdir()
            huge = [dict(gid='card_large', index=0, kind='new', frontText='题', backText='答'*49000)]
            with patch.object(review_deck, 'default_root', return_value=root), patch.object(review_deck, 'collect', return_value=huge):
                result = build_review_candidates()
            self.assertEqual(result['status'], 'too_large')
            self.assertNotIn('cards', result)

    def test_review_request_reads_ready_file_without_running_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ready.json'
            with patch('voice_jev_context.review_snapshot_path', return_value=path), patch('voice_jev_context.build_review_candidates', return_value=self.review_fixture()) as build:
                refresh_review_candidates()
                build.assert_called_once()
            with patch('voice_jev_context.review_snapshot_path', return_value=path), patch('voice_jev_context.build_review_candidates', side_effect=AssertionError('request must not rebuild')):
                result = load_review_candidates()
            self.assertEqual(result['cards'][0]['backText'], '答案完整内容')
            self.assertEqual(result['total'], 1)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_review_missing_or_stale_file_never_rebuilds_on_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ready.json'
            with patch('voice_jev_context.review_snapshot_path', return_value=path), patch('voice_jev_context.build_review_candidates', side_effect=AssertionError('no synchronous rebuild')):
                self.assertEqual(load_review_candidates()['status'], 'unavailable')
                path.write_text(json.dumps(dict(self.review_fixture(), contract='review-candidates/1', expiresAtMs=1)), encoding='utf-8')
                self.assertEqual(load_review_candidates()['status'], 'stale')

    def test_background_refresh_removes_cards_no_longer_due(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ready.json'
            empty = {'status': 'ready', 'total': 0, 'new': 0, 'due': 0, 'cards': []}
            with patch('voice_jev_context.review_snapshot_path', return_value=path), patch('voice_jev_context.build_review_candidates', side_effect=[self.review_fixture(), empty]):
                refresh_review_candidates()
                self.assertEqual(load_review_candidates()['total'], 1)
                refresh_review_candidates()
                self.assertEqual(load_review_candidates()['total'], 0)

    async def test_prepared_cache_uses_existing_steer_and_keeps_target(self):
        r,key = self.make(); await self.prepare(r)
        self.assertEqual(r.messages, [])
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(len(r.messages), 1)
        method,args = r.messages[0]
        self.assertEqual(method, 'turn/steer')
        self.assertEqual(args['expectedTurnId'], 'backend-turn')
        text = args['input'][0]['text']
        self.assertIn('Jev 推荐：reader_card', text)
        self.assertIn('麻疹 CURRENT_PAGE', text)
        self.assertIn('文件标识（调用时原样使用）：book', text)
        self.assertNotIn('OLDER_PAGE', text)
        self.assertNotIn('NEXT_PAGE', text)

    async def test_delegation_can_arrive_before_assistant_reply(self):
        r,key = self.make()
        pending = asyncio.create_task(r._ctx_on_delegation(key, 1))
        await asyncio.sleep(.01)
        self.assertEqual(len(r.predictions), 1)
        r._jev.assistant_started(r._jev.latest)
        await pending
        self.assertEqual(len(r.predictions), 1)
        self.assertIn('Jev 推荐', r.messages[0][1]['input'][0]['text'])

    async def test_no_handoff_never_injects(self):
        r,key = self.make(); r._jev.records[key]['request'] = ''
        await self.prepare(r)
        await asyncio.sleep(.01)
        self.assertTrue(r.predictions)
        self.assertEqual(r.messages, [])

    async def test_duplicate_delegation_and_reply_send_once_even_when_backend_loops(self):
        r, key = self.make(delay=.02)
        r._jev.assistant_started(key)
        await asyncio.gather(r._ctx_on_delegation(key, 1), r._ctx_on_delegation(key, 2))
        r._turn = {'id': 'backend-loop'}
        r._jev.assistant_started(key)
        await r._ctx_on_delegation(key, 3)
        self.assertEqual(len(r.predictions), 1)
        self.assertEqual(len(r.messages), 1)

    async def test_sender_reserves_round_before_network_await(self):
        r, key = self.make(); await self.prepare(r)
        bundle = await r._jev.for_delegation(key)
        await asyncio.gather(*[r._ctx_inject_backend(True, True, bundle) for _ in range(2)])
        self.assertEqual(len(r.messages), 1)

    async def test_same_round_accepts_different_transcription_and_delegation_wording(self):
        r, key = self.make(); r._jev.records[key]['request'] = ''
        r._jev.observe(key, '给 麻 疹 加 注 解', completed=True)
        await self.prepare(r)
        r._jev.delegated(key, '请给我选中的麻疹加个注解')
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(len(r.messages), 1)
        self.assertEqual(len(r.predictions), 1)

    async def test_old_created_event_or_reply_cannot_revive_previous_round(self):
        r, key = self.make(); await self.prepare(r)
        new_key = r._jev_key('new-user-turn')
        r._jev.observe(new_key, '给風しん加注解', new_turn=True)
        r._jev.delegated(new_key, '给風しん加注解')
        r._jev.observe(key, '给麻疹加注解', new_turn=True)
        r._jev.assistant_started(key)
        self.assertEqual(r._jev.latest, new_key)
        await r._ctx_on_delegation(key, 1)
        await r._ctx_on_delegation(new_key, 2)
        self.assertEqual(len(r.messages), 1)
        self.assertEqual(len(r.predictions), 2)
        self.assertIn('给風しん加注解', r.messages[0][1]['input'][0]['text'])

    async def test_missing_round_id_never_guesses_latest(self):
        r, key = self.make()
        await r._ctx_on_delegation(None, 1)
        self.assertEqual(r.predictions, [])
        self.assertEqual(r.messages, [])

    async def test_failed_steer_never_appends_packet_for_next_turn(self):
        r, key = self.make()
        async def failed(*args, **kwargs): return {'ok':False, 'error':'turn finished'}
        r.steer_running_turn = failed
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])
        await r._ctx_on_delegation(key, 2)
        self.assertEqual(len(r.predictions), 1)

    async def test_legacy_pending_packet_is_discarded(self):
        r, _ = self.make()
        r._ctx_pending = {'content':[{'type':'input_text','text':'OLD_PAGE'}]}
        await r._ctx_flush_pending()
        self.assertEqual(r.messages, [])
        self.assertIsNone(r._ctx_pending)

    async def test_delayed_result_cannot_be_retried_by_duplicate_delegation(self):
        r, key = self.make(delay=.10)
        await r._ctx_on_delegation(key, 1)
        await r._jev.records[key]['event'].wait()
        await r._ctx_on_delegation(key, 2)
        self.assertEqual(len(r.predictions), 1)
        self.assertEqual(r.messages, [])

    async def test_session_restart_cannot_use_old_voice_result(self):
        r, key = self.make(); await self.prepare(r)
        r._jev.reset(); r.session_no += 1
        new_key = r._jev_key('voice-turn')
        self.assertNotEqual(key, new_key)
        r._jev.observe(new_key, '加注解', new_turn=True)
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_thread_switch_rejects_prepared_bundle_at_sender(self):
        r, key = self.make(); await self.prepare(r)
        bundle = await r._jev.for_delegation(key)
        r.thread_id = 'different-thread'
        await r._ctx_inject_backend(True, True, bundle)
        self.assertEqual(r.messages, [])

    async def test_timeout_sends_nothing_and_late_result_is_not_injected(self):
        r,key = self.make(delay=.15); r._jev.assistant_started(r._jev.latest)
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])
        await r._jev.records[key]['event'].wait()
        self.assertEqual(r.messages, [])

    async def test_late_voice_reply_no_context_does_not_send_legacy_page(self):
        # Delegation starts Jev immediately; a late voice reply reuses it.
        r, key = self.make(delay=.04, choice='assistant_handoff', context_choice='no_extra_context')
        pending = asyncio.create_task(r._ctx_on_delegation(key, 1))
        await asyncio.sleep(.06)
        r._jev.assistant_started(r._jev.latest)
        await pending
        self.assertEqual(r.messages, [])
        await r._jev.records[key]['event'].wait()
        self.assertEqual(r.messages, [])

    async def test_direct_turn_keeps_user_request_without_unapproved_context(self):
        r, _ = self.make()
        async def ensure(): pass
        r.ensure_app = ensure
        def forbidden(*args):
            self.fail('A direct/rescue entry must not build legacy context in Jev mode')
        r._ctx_build = forbidden
        await r.turn('现在重启一下吧')
        self.assertEqual(r.messages, [('turn/start', {'threadId':'thread',
            'input':[{'type':'text','text':'现在重启一下吧'}]})])

    async def test_invalid_bundle_is_rejected_at_shared_sender(self):
        r, key = self.make(); await self.prepare(r)
        bundle = await r._jev.for_delegation(key)
        r.snap['selectedItems'][0]['text'] = 'CHANGED_SELECTION'
        before = copy.deepcopy(r._ctx)
        await r._ctx_inject_backend(with_text=True, via_steer=True, jev_bundle=bundle)
        self.assertEqual(r.messages, [])
        self.assertEqual(r._ctx, before)

    async def test_failed_jev_does_not_inject_full_page(self):
        r, key = self.make()
        def fail(*args): raise ConnectionError('simulated')
        r._jev.predictor = fail
        await self.prepare(r)
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_newer_request_drops_old_delegation(self):
        r,key = self.make(); await self.prepare(r)
        r._delegation_seq = 2
        r._jev.observe(r._jev_key('new-user-turn'), '改成風しん', new_turn=True)
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_changed_selection_skips_old_recommendation_without_full_page_fallback(self):
        r,key = self.make(); await self.prepare(r)
        r.snap['selectedItems'][0]['text'] = '風しん'
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_40_percent_handoff_does_not_inject(self):
        r,key = self.make(probability=.4); await self.prepare(r)
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])
        self.assertTrue(any(x.get('reason') == 'low_confidence_handoff' for x in r.logs))

    async def test_switch_off_does_not_query(self):
        r,key = self.make(); r.settings['jevContextEnabled'] = False
        r._jev.assistant_started(r._jev.latest)
        await r._ctx_on_delegation(key, 1)
        self.assertFalse(r.predictions)
        self.assertNotIn('Jev 推荐', r.messages[0][1]['input'][0]['text'])

    async def test_new_tool_receipt_invalidates_cache(self):
        r,key = self.make(); await self.prepare(r)
        r.events.append({'seq': 3, 'kind': 'item/completed', 'itemType': 'mcpToolCall',
                         'tool': 'reader_card', 'status': 'completed'})
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_omitted_neighbor_is_not_marked_as_delivered(self):
        r,key = self.make(); await self.prepare(r)
        original = r._ctx_build
        def build(snap):
            r._ctx['sent_pages'] = {('book', 43): ('part', time.time()),
                                    ('book', 44): ('full', time.time()),
                                    ('book', 45): ('part', time.time())}
            return original(snap)
        r._ctx_build = build
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r._ctx.get('sent_pages', {}), {})
        self.assertTrue(r._ctx['fp']['backend_state'].startswith('jev-tool:'))

    async def test_no_context_does_not_send_even_empty_message_or_build_payload(self):
        r,key = self.make(context_choice='no_extra_context'); await self.prepare(r)
        before = copy.deepcopy(r._ctx)
        def forbidden(*args):
            self.fail('No-context path must not build text/images or send state')
        r._ctx_build = forbidden
        r._ink_standby_pending = forbidden
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])
        self.assertEqual(r._ctx, before)
        self.assertTrue(any(x['kind'] == 'jev_context_skipped' for x in r.logs))

    async def test_no_context_handoff_does_not_fall_back_to_full_page(self):
        r,key = self.make(choice='assistant_handoff', probability=.3,
                          context_choice='no_extra_context')
        await self.prepare(r); await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])
        self.assertTrue(any(x['kind'] == 'jev_context_skipped' for x in r.logs))

    async def test_handoff_even_when_context_question_says_needed_does_not_inject(self):
        r,key = self.make(choice='assistant_handoff')
        await self.prepare(r); await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_changed_request_does_not_reuse_no_context_decision(self):
        r,key = self.make(context_choice='no_extra_context'); await self.prepare(r)
        r._jev.delegated(key, '现在给这个词加注解')
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_uncertain_no_context_keeps_information(self):
        r,key = self.make(context_choice='no_extra_context', context_probability=.4)
        await self.prepare(r); await r._ctx_on_delegation(key, 1)
        self.assertEqual(len(r.messages), 1)
        self.assertIn('CURRENT_PAGE', r.messages[0][1]['input'][0]['text'])

    async def test_annotation_only_includes_selected_passage_and_exact_bind_data(self):
        r,key = self.make()
        r.snap['currentPage']['text'] = r.snap['currentPage']['text'].replace(
            '【当前页之后】', '[02] UNRELATED_PARAGRAPH\n【当前页之后】')
        r.snap['currentPage']['highlightSource'] = {'revision': 'rev1', 'markers': [{'marker': 'HUGE_MARKER_TABLE'}]}
        await self.prepare(r)
        def forbidden(*args):
            self.fail('Jev path must bypass original full-context builder')
        r._ctx_build = forbidden
        await r._ctx_on_delegation(key, 1)
        payload = r.messages[0][1]['input'][0]['text']
        self.assertIn('[01] 麻疹 CURRENT_PAGE', payload)
        self.assertIn('rev1', payload)
        for forbidden_text in ('OLDER_PAGE', 'NEXT_PAGE', 'UNRELATED_PARAGRAPH', 'HUGE_MARKER_TABLE'):
            self.assertNotIn(forbidden_text, payload)
        sent_log = next(x for x in r.logs if x['kind'] == 'ctx_steer')
        self.assertEqual(sent_log['body'], payload)
        self.assertEqual(sent_log['chars'], len(payload))
        self.assertEqual(sent_log['contextSource'], 'jev-tool-data')

    async def test_navigation_uses_identity_without_selected_text_or_page_body(self):
        r,key = self.make(choice='reader_command'); await self.prepare(r)
        await r._ctx_on_delegation(key, 1)
        payload = r.messages[0][1]['input'][0]['text']
        self.assertIn('第 44 页', payload)
        self.assertNotIn('CURRENT_PAGE', payload)
        self.assertNotIn('selected_items', payload)

    async def test_annotation_preserves_source_context_but_omits_other_card_body(self):
        r,key = self.make()
        opening = '⟦CARD_START id="other" label="風しん" anchor="風しん"⟧'
        embedded = opening + 'UNRELATED_CARD_BODY [99] 麻疹' + '⟦CARD_END⟧'
        passage = '[05] 前文解释 麻疹（麻疹ウイルス）与風しん' + embedded + ' 的比较及后文。'
        r.snap['currentPage']['text'] = '【当前页】\n' + passage
        r.snap['selectedItems'][0]['context'] = passage
        await self.prepare(r); await r._ctx_on_delegation(key, 1)
        payload = r.messages[0][1]['input'][0]['text']
        self.assertNotIn('UNRELATED_CARD_BODY', payload)
        self.assertNotIn('[99]', payload)
        self.assertIn('[05] 前文解释 麻疹（麻疹ウイルス）与風しん', payload)
        self.assertIn('的比较及后文。', payload)
        self.assertNotIn('CARD_START', payload)
        self.assertEqual(next(x['body'] for x in r.logs if x['kind']=='ctx_steer'), payload)

    def test_selected_card_and_source_text_are_preserved(self):
        a = '⟦CARD_START id="chosen" label="甲" anchor="甲"⟧CHOSEN_BODY⟦CARD_END⟧'
        b = '⟦CARD_START id="other" label="乙" anchor="乙"⟧OTHER_BODY⟦CARD_END⟧'
        text = '[01] 前文甲' + a + '中间乙' + b + '后文'
        for items in ([{'kind': 'card', 'ref': {'id': 'chosen'}}],
                      [{'kind': 'text', 'text': '甲'}]):
            r, _ = self.make()
            r.snap['selectedItems'] = items
            result = self.packet('reader_card', r.snap, text)
            self.assertIn('CHOSEN_BODY', result)
            self.assertNotIn('OTHER_BODY', result)
            self.assertEqual(original_passage(text), '[01] 前文甲中间乙后文')
        incomplete = '[02] 原文⟦CARD_START id="broken"⟧后续正文'
        self.assertEqual(original_passage(incomplete), incomplete)

    async def test_anki_source_does_not_quote_other_annotation_as_original(self):
        r,key = self.make(choice='reader_anki_draft')
        r.snap['currentPage']['text'] = ('【当前页】\n[01] 麻疹与風しん'
            '⟦CARD_START id="other" anchor="風しん"⟧EXISTING_CONTEXT⟦CARD_END⟧')
        await self.prepare(r); await r._ctx_on_delegation(key, 1)
        payload = r.messages[0][1]['input'][0]['text']
        self.assertNotIn('EXISTING_CONTEXT', payload)
        self.assertIn('[01] 麻疹与風しん', payload)

    async def test_knowledge_answer_skips_all_extra_messages(self):
        r,key = self.make(choice='knowledge_answer'); await self.prepare(r)
        r.snap['currentPage']['page'] = 45  # unrelated reading changes do not revive old injection
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(r.messages, [])

    async def test_chain_log_preserves_actual_long_payload(self):
        r,key = self.make()
        target = '长选区' * 3000
        r.snap['selectedItems'][0]['text'] = target
        r.snap['currentPage']['text'] = '【当前页】\n[01] ' + target
        await self.prepare(r); await r._ctx_on_delegation(key, 1)
        payload = r.messages[0][1]['input'][0]['text']
        self.assertGreater(len(payload), 8000)
        self.assertEqual(next(x['body'] for x in r.logs if x['kind']=='ctx_steer'), payload)

    def test_every_catalog_route_has_an_explicit_data_policy(self):
        import json
        path = Path(__file__).resolve().parent / 'fixtures' / 'jev-routing-question.json'
        criteria = json.loads(path.read_text(encoding='utf-8'))['criteria']
        self.assertEqual(set(criteria), set(TOOL_DATA))
        self.assertEqual(set(criteria), set(TOOL_NEEDS))

    def card_fixture(self):
        r, _ = self.make()
        r.snap['selectedItems'] = [{'kind': 'card', 'ref': 'c_chosen'}]
        text = ('[01] 原文甲⟦CARD_START id="c_chosen" n="2" revision="124" label="甲" learning="card_learning" cardIndex="0"⟧'
                'CHOSEN_CONTENT⟦CARD_END⟧；原文乙'
                '⟦CARD_START id="c_other" n="3" revision="124" label="乙"⟧OTHER_CONTENT⟦CARD_END⟧')
        return r.snap, text

    def packet(self, route, snapshot, text, tasks=None, request='修改这张卡'):
        return build_tool_context({'choice':route, 'probability':.9, 'request':request}, snapshot, text, tasks or [])[0]

    def test_page_edit_preserves_target_body_and_version_delete_omits_body(self):
        snap, text = self.card_fixture()
        # Rendered-card edit fixture (not a learning placement).
        text = text.replace(' learning="card_learning" cardIndex="0"', '')
        edit = self.packet('reader_page_card_edit', snap, text)
        delete = self.packet('reader_page_card_delete', snap, text)
        self.assertIn('CHOSEN_CONTENT', edit)
        for payload in (edit, delete):
            self.assertIn('"id": "c_chosen"', payload)
            self.assertIn('"revision": 124', payload)
            self.assertNotIn('OTHER_CONTENT', payload)
            self.assertNotIn('c_other', payload)
        self.assertNotIn('CHOSEN_CONTENT', delete)

    def test_explicit_card_number_overrides_focus_without_adding_other_bodies(self):
        snap, text = self.card_fixture()
        payload = self.packet('reader_page_card_edit', snap, text, request='修改第3张卡')
        self.assertIn('c_other', payload)
        self.assertIn('OTHER_CONTENT', payload)
        self.assertNotIn('CHOSEN_CONTENT', payload)

    def test_learning_identity_never_uses_placement_revision(self):
        snap, text = self.card_fixture()
        payload = self.packet('reader_learning_card_delete', snap, text)
        self.assertIn('"id": "card_learning"', payload)
        self.assertIn('"cardIndex": 0', payload)
        self.assertIn('尚不齐全', payload)
        self.assertNotIn('124', payload)
        self.assertNotIn('CHOSEN_CONTENT', payload)
        self.assertNotIn('c_chosen', payload)

    def test_review_context_works_without_open_book_and_preserves_entity_versions(self):
        snap = {'contextStatus':'unavailable', 'review': {'id':'card_review', 'cardIndex':0,
                'entityRevision':9, 'stateRevision':11, 'front':'REVIEW_QUESTION', 'back':'REVIEW_ANSWER'}}
        payload = self.packet('reader_review_answer', snap, 'UNRELATED_BOOK')
        self.assertIn('REVIEW_QUESTION', payload)
        self.assertIn('REVIEW_ANSWER', payload)
        self.assertNotIn('UNRELATED_BOOK', payload)
        delete = self.packet('reader_learning_card_delete', snap, 'UNRELATED_BOOK')
        self.assertIn('"stateRevision": 11', delete)
        self.assertNotIn('REVIEW_ANSWER', delete)

    def test_anki_keeps_epub_target_and_known_nodes(self):
        snap = {'contextStatus':'ready', 'currentPage':{'file':'epub-book', 'section':0,
                'highlightSource':{'target':{'kind':'epub','section':0}}},
                'selectedItems':[{'kind':'text', 'text':'甲', 'nodeIds':['kj:0123456789']}]}
        payload = self.packet('reader_anki_draft', snap, '[01] 甲的完整含义与背景。')
        self.assertIn('"kind": "epub"', payload)
        self.assertIn('"section": 0', payload)
        self.assertIn('kj:0123456789', payload)
        self.assertIn('甲的完整含义与背景。', payload)

    def test_global_actions_do_not_receive_page_or_unrelated_tool_receipts(self):
        snap, text = self.card_fixture()
        tasks = [{'tool':'reader_card','text':'UNRELATED_RECEIPT'},
                 {'tool':'schedule_create','text':'RELATED_SCHEDULE'}]
        for route in ('schedule_delete', 'schedule_enable', 'schedule_runs', 'notify_ack', 'voice_status', 'reader_camera_snap'):
            payload = self.packet(route, snap, text, tasks, request='处理这个任务')
            for forbidden in ('c_chosen', 'CHOSEN_CONTENT', 'OTHER_CONTENT', 'UNRELATED_RECEIPT', '当前位置'):
                self.assertNotIn(forbidden, payload, route)
        self.assertIn('RELATED_SCHEDULE', self.packet('schedule_delete', snap, text, tasks))

    async def test_unrelated_selected_drawing_is_not_appended_to_global_action(self):
        r, key = self.make(choice='voice_status')
        r.snap['selectedItems'] = [{'kind':'drawing', 'ref':'UNRELATED_DRAWING'}]
        def forbidden(*args):
            self.fail('Selected drawing must not cause unrelated image preparation')
        r._ink_standby_pending = forbidden
        await self.prepare(r)
        await r._ctx_on_delegation(key, 1)
        self.assertEqual(len(r.messages), 1)
        self.assertNotIn('UNRELATED_DRAWING', r.messages[0][1]['input'][0]['text'])

    def test_every_nonempty_route_renders_with_missing_data_without_null_json(self):
        for route, fields in TOOL_DATA.items():
            if fields is None: continue
            payload = self.packet(route, {}, '', request='本次请求')
            self.assertIn('Jev 推荐：' + route, payload)
            self.assertNotIn(': null', payload)
            self.assertNotIn('：{}', payload)


if __name__ == '__main__':
    unittest.main(verbosity=2)
