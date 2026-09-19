"""Isolated Codex v3 voice sessions for the StocksNative validation app."""
import asyncio
import contextlib
import fractions
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid

from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame, AudioResampler

log = logging.getLogger(__name__)

PROMPT = """你是股票原生 App 的语音助手，用简洁中文交流。
这是功能验证版：可以回答问题、通过股票工具查询真实数据。报价可能是最近交易日快照，必须说明数据日期，不能称为实时行情。
需要股票数值时委派后台调用工具；当前股票可能已切换，所有关于“当前股票”的查询必须用 stocks_current，不能沿用上文代码。没有数据就明确说明，禁止编造。只查询和切换展示股票，不进行交易、记账或修改生产配置。
无需主动欢迎或总结。等待用户说话。只在有结果时简洁回答一次。"""

VOICE_RULES = """你是股票 App 的语音对话表面，默认简洁中文。
你自己看不到任何股票行情，也没有股票查询工具。后台才有真实行情工具。
凡涉及具体股票的名称、价格、走势、数据日期或当前选中股票，必须立即委派给后台执行。
不要依据训练知识或旧对话猜报价。收到后台的工具返回前，不得回答任何股票数字或宣布查询完成。
用户说“当前股票”时，把原话交给后台调用 stocks_current；查指定代码交给后台调用 stocks_detail。
后台结果是权威，只简短说一次结果和数据日期。不要对用户解释内部系统分工。
纯闲聊、复述一句话可以直接回答。用户没有提出请求时保持安静。"""


def safe_error(exc):
    value = str(exc)
    value = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', value)
    value = re.sub(r'\b(?:sk-|eyJ)[A-Za-z0-9_.-]{15,}', '[redacted]', value)
    return value[:600]


class MicrophoneTrack(MediaStreamTrack):
    kind = 'audio'

    def __init__(self):
        super().__init__()
        self.queue = asyncio.Queue(maxsize=20)
        self.samples = 0
        self.started = None

    def push(self, data):
        if len(data) != 1920:
            raise ValueError('需要 48kHz、单声道 PCM16 的 20ms 音频帧')
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(data)

    async def recv(self):
        loop = asyncio.get_running_loop()
        if self.started is None:
            self.started = loop.time()
        await asyncio.sleep(max(0, self.started + self.samples / 48000 - loop.time()))
        data = self.queue.get_nowait() if not self.queue.empty() else bytes(1920)
        frame = AudioFrame(format='s16', layout='mono', samples=960)
        frame.planes[0].update(data)
        frame.sample_rate = 48000
        frame.pts = self.samples
        frame.time_base = fractions.Fraction(1, 48000)
        self.samples += 960
        return frame


class VoiceSession:
    def __init__(self, device_id, state_dir, data_store, emit_json, emit_audio):
        self.device_id = device_id
        self.state_dir = Path(state_dir)
        self.data_store = data_store
        self.emit_json = emit_json
        self.emit_audio = emit_audio
        self.session_id = str(uuid.uuid4())
        self.thread_id = None
        self.stock_code = None
        self.proc = None
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.input = MicrophoneTrack()
        self.pending = {}
        self.next_id = 0
        self.write_lock = asyncio.Lock()
        self.tasks = set()
        self.sdp = asyncio.get_running_loop().create_future()
        self.ready = asyncio.Event()
        self.closed = False
        self.last_activity = time.monotonic()
        self.received_frames = 0
        self.sent_frames = 0
        self.backend_turns = 0
        self.turns = {}
        self.active_turn_id = None
        self.text_pending = None
        self.voice_request = None
        self.delegation_pending = False
        self.reply_lock = asyncio.Lock()
        self.speech_receipts = []
        self.journal = self.state_dir / ('voice-' + hashlib.sha256(device_id.encode()).hexdigest()[:24] + '.jsonl')
        self.thread_file = self.journal.with_suffix('.thread')

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def record(self, event):
        with self.journal.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'at': time.time(), **event}, ensure_ascii=False) + '\n')

    async def event(self, event):
        if not self.closed:
            await self.emit_json(event)

    async def send(self, obj):
        async with self.write_lock:
            self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + '\n').encode())
            await self.proc.stdin.drain()

    async def call(self, method, params, timeout=45):
        self.next_id += 1
        key = self.next_id
        f = asyncio.get_running_loop().create_future()
        self.pending[key] = f
        try:
            await self.send({'id': key, 'method': method, 'params': params})
            return await asyncio.wait_for(f, timeout)
        finally:
            self.pending.pop(key, None)

    def turn_state(self, turn_id):
        if turn_id not in self.turns:
            request = self.text_pending
            manual = request is not None and request.get('turnId') in (None, turn_id)
            if manual:
                request['turnId'] = turn_id
            voice_request = self.voice_request if not manual else None
            if voice_request:
                self.voice_request = None
            self.turns[turn_id] = {
                'turnId': turn_id, 'requestId': request['requestId'] if manual else (
                    voice_request['requestId'] if voice_request else str(uuid.uuid4())),
                'source': 'text' if manual else 'voice', 'messages': {}, 'tools': [],
                'usage': None, 'started': False, 'finishing': False, 'finished': False,
                'inputText': request['text'] if manual else (voice_request['text'] if voice_request else ''),
            }
        return self.turns[turn_id]

    def capture_final(self, turn_id, item):
        # The protocol's terminal phase is final_answer; commentary is never spoken.
        if item.get('type') == 'agentMessage' and item.get('phase') in ('final_answer', 'final'):
            self.turn_state(turn_id)['messages'][item.get('id', 'final')] = item.get('text', '')

    async def fail_turn(self, state, message):
        state['finished'] = True
        receipt = {'type': 'task', 'state': 'failed', 'requestId': state['requestId'],
                   'turnId': state.get('turnId'), 'source': state['source'],
                   'dataVerified': False, 'message': message}
        self.record(receipt)
        await self.event(receipt)
        await self.event({'type': 'error', 'message': message, 'requestId': state['requestId']})

    async def finish_turn(self, turn):
        turn_id = turn['id']
        state = self.turn_state(turn_id)
        try:
            for item in turn.get('items', []):
                self.capture_final(turn_id, item)
            if turn.get('status') != 'completed':
                await self.fail_turn(state, '后台请求未完成：' + safe_error(turn.get('error') or turn.get('status')))
                return
            answer = '\n\n'.join(text.strip() for text in state['messages'].values() if text.strip())
            if not answer:
                await self.fail_turn(state, '后台已结束，但没有收到本轮最终回答，请重试。')
                return
            verified = any(tool.get('success') and tool.get('dataReturned') for tool in state['tools'])
            has_number = bool(re.search(r'\d|[零〇一二两三四五六七八九十百千万亿]+\s*(?:元|块|股|手|％|%)', answer))
            stock_claim = bool(re.search(r'股票|股价|价格|报价|行情|收盘|开盘|涨|跌|成交|市值|换手|量比|元|资金|代码|K线|\b\d{6}\b',
                                        state['inputText'] + '\n' + answer))
            self.record({'type': 'backend.final', 'requestId': state['requestId'], 'turnId': turn_id,
                         'text': answer, 'dataVerified': verified, 'source': state['source']})
            if state['source'] == 'text' and has_number and stock_claim and not verified:
                await self.fail_turn(state, '本轮没有取得成功的行情数据回执，股票数值尚未核实，请重试。')
                return
            # All background results, including automatic audio delegations, use
            # this single outlet. Do not also append user text to Realtime.
            async with self.reply_lock:
                if self.closed:
                    return
                spoken = {'requestId': state['requestId'], 'turnId': turn_id, 'text': answer}
                self.speech_receipts.append(spoken)
                self.speech_receipts = self.speech_receipts[-32:]
                try:
                    await self.call('thread/realtime/appendSpeech', {'threadId': self.thread_id, 'text': answer})
                except Exception:
                    with contextlib.suppress(ValueError):
                        self.speech_receipts.remove(spoken)
                    raise
            state['finished'] = True
            receipt = {'type': 'task', 'state': 'completed', 'requestId': state['requestId'],
                       'turnId': turn_id, 'source': state['source'], 'dataVerified': verified,
                       'toolCount': len(state['tools']), 'speech': 'submitted', 'text': answer}
            self.record(receipt)
            await self.event(receipt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.fail_turn(state, '后台结果未能回送语音：' + safe_error(exc))

    async def read(self):
        try:
            while line := await self.proc.stdout.readline():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if 'id' in obj and ('result' in obj or 'error' in obj):
                    f = self.pending.get(obj['id'])
                    if f and not f.done():
                        if 'error' in obj:
                            f.set_exception(RuntimeError(safe_error(obj['error'])))
                        else:
                            f.set_result(obj.get('result', {}))
                    continue
                method = obj.get('method', '')
                p = obj.get('params') or {}
                tid = p.get('threadId') or p.get('thread_id')
                if 'id' in obj:
                    self.task(self.respond_tool(obj) if method == 'item/tool/call'
                              else self.send({'id': obj['id'], 'error': {'code': -32601, 'message': 'Unavailable in StocksNative MVP'}}))
                    continue
                if tid != self.thread_id:
                    continue
                if method == 'thread/realtime/sdp' and not self.sdp.done():
                    self.sdp.set_result(p['sdp'])
                elif method == 'thread/realtime/error':
                    await self.event({'type': 'error', 'message': safe_error(p)})
                elif method == 'thread/realtime/closed' and not self.closed:
                    await self.event({'type': 'state', 'state': 'closed', 'reason': p.get('reason')})
                elif method == 'turn/started':
                    turn_id = p['turn']['id']
                    state = self.turn_state(turn_id)
                    if not state['started']:
                        self.backend_turns += 1
                        state['started'] = True
                    self.active_turn_id = turn_id
                    self.delegation_pending = False
                    receipt = {'type': 'task', 'state': 'running', 'requestId': state['requestId'],
                               'turnId': turn_id, 'source': state['source']}
                    self.record(receipt)
                    await self.event(receipt)
                elif method == 'item/completed':
                    self.capture_final(p['turnId'], p.get('item') or {})
                elif method == 'turn/completed':
                    turn = p['turn']
                    state = self.turn_state(turn['id'])
                    if self.active_turn_id == turn['id']:
                        self.active_turn_id = None
                    self.delegation_pending = False
                    if not state['finishing']:
                        state['finishing'] = True
                        self.task(self.finish_turn(turn))
                elif method == 'thread/tokenUsage/updated':
                    turn_id = p.get('turnId') or self.active_turn_id
                    state = self.turn_state(turn_id) if turn_id else None
                    usage = p.get('tokenUsage', {}).get('last')
                    if state:
                        state['usage'] = usage
                    self.record({'type': 'usage', 'requestId': state['requestId'] if state else None,
                                 'turnId': turn_id, 'usage': usage})
        finally:
            for f in list(self.pending.values()):
                if not f.done():
                    f.set_exception(RuntimeError('Codex 连接已关闭'))
            if not self.closed:
                await self.event({'type': 'error', 'message': 'Codex 进程已退出，请重新连接'})

    async def stderr(self):
        while line := await self.proc.stderr.readline():
            log.debug('Codex %s: %s', self.session_id, safe_error(line.decode(errors='replace')))

    async def respond_tool(self, obj):
        p = obj['params']
        success = True
        try:
            if p.get('threadId') != self.thread_id:
                raise ValueError('错误的会话归属')
            args = p.get('arguments') or {}
            name = p.get('tool')
            if name == 'stocks_search':
                result = await asyncio.to_thread(self.data_store.list_stocks, str(args.get('query', ''))[:80], 12)
            elif name == 'stocks_current':
                if not self.stock_code:
                    result = {'message': '当前未选中股票'}
                else:
                    result = await asyncio.to_thread(self.data_store.stock_detail, self.stock_code, 10)
            elif name == 'stocks_detail':
                result = await asyncio.to_thread(self.data_store.stock_detail, str(args.get('code', '')), 30)
            else:
                raise ValueError('此能力不在验证版范围内')
        except Exception as e:
            success = False
            result = {'error': safe_error(e)}
        state = self.turn_state(p['turnId']) if p.get('threadId') == self.thread_id else None
        receipt = {'type': 'tool', 'name': p.get('tool'), 'success': success,
                   'requestId': state['requestId'] if state else None, 'turnId': p.get('turnId'),
                   'callId': p.get('callId'), 'code': result.get('stock', {}).get('code'),
                   'asOf': result.get('asOf'),
                   'dataReturned': bool(result.get('asOf') and (result.get('stock') or result.get('items')))}
        if state:
            state['tools'].append(receipt)
        self.record(receipt)
        await self.send({'id': obj['id'], 'result': {'success': success, 'contentItems': [
            {'type': 'inputText', 'text': json.dumps(result, ensure_ascii=False)}]}})
        await self.event(receipt)

    async def select_stock(self, code):
        result = await asyncio.to_thread(self.data_store.stock_detail, code, 1)
        self.stock_code = result['stock']['code']
        await self.event({'type': 'stock.selected', 'code': self.stock_code})
        self.record({'type': 'stock.selected', 'code': self.stock_code})

    def on_dc(self, raw):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        kind = obj.get('type')
        if kind == 'session.started':
            self.ready.set()
        elif kind == 'delegation.created':
            if self.active_turn_id is None:
                self.delegation_pending = time.monotonic()
            self.record({'type': 'voice.delegation', 'sessionId': self.session_id})
        elif kind == 'turn.done':
            turn = obj.get('turn') or {}
            text = turn.get('transcript')
            if text:
                self.last_activity = time.monotonic()
                event = {'type': 'transcript', 'role': turn.get('role'), 'text': text, 'final': True}
                if turn.get('role') == 'user':
                    active = self.turns.get(self.active_turn_id)
                    request_id = active['requestId'] if active and active['source'] == 'voice' else str(uuid.uuid4())
                    event['requestId'] = request_id
                    event['source'] = 'voice'
                    if active and active['source'] == 'voice':
                        active['inputText'] = text
                    else:
                        self.voice_request = {'requestId': request_id, 'text': text}
                elif turn.get('role') == 'assistant':
                    normalized = re.sub(r'\W+', '', text).casefold()
                    for receipt in self.speech_receipts:
                        if re.sub(r'\W+', '', receipt['text']).casefold() == normalized:
                            event.update(requestId=receipt['requestId'], turnId=receipt['turnId'])
                            self.speech_receipts.remove(receipt)
                            break
                self.record(event)
                self.task(self.event(event))
        elif kind == 'error':
            self.task(self.event({'type': 'error', 'message': safe_error(obj.get('error'))}))

    async def consume_audio(self, track):
        resampler = AudioResampler(format='s16', layout='mono', rate=48000)
        pending = bytearray()
        try:
            while not self.closed:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    pending.extend(bytes(out.planes[0])[:out.samples * 2])
                    while len(pending) >= 1920:
                        data = bytes(pending[:1920])
                        del pending[:1920]
                        await self.emit_audio(data)
                        self.sent_frames += 1
        except Exception as e:
            if not self.closed:
                await self.event({'type': 'error', 'message': '语音下行中断：' + safe_error(e)})

    async def start(self, code=None):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        await self.event({'type': 'state', 'state': 'connecting', 'sessionId': self.session_id})
        if code:
            await self.select_stock(code)
        args = [os.environ.get('STOCKS_CODEX', '/opt/codex/0.155.1/bin/codex'),
                '-c', 'forced_login_method="chatgpt"', '-c', 'features.plugins=false',
                '-c', 'features.memories=false', 'app-server', '--listen', 'stdio://']
        env = {k: v for k, v in os.environ.items() if k not in ('OPENAI_API_KEY', 'OPENAI_BASE_URL')}
        self.proc = await asyncio.create_subprocess_exec(*args, cwd=str(self.state_dir), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024)
        self.task(self.read())
        self.task(self.stderr())
        await self.call('initialize', {'clientInfo': {'name': 'stocks_native', 'version': '0.2.0'},
                         'capabilities': {'experimentalApi': True}})
        await self.send({'method': 'initialized'})
        tools = [
            {'type': 'function', 'name': 'stocks_search', 'description': '按代码或名称搜索股票，返回带日期的行情快照。',
             'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query'], 'additionalProperties': False}},
            {'type': 'function', 'name': 'stocks_current', 'description': '查询此语音会话当前选中的股票及数据日期。',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
            {'type': 'function', 'name': 'stocks_detail', 'description': '按六位股票代码查询报价和近期K线，数据可能是收盘快照。',
             'inputSchema': {'type': 'object', 'properties': {'code': {'type': 'string'}}, 'required': ['code'], 'additionalProperties': False}},
        ]
        params = {'cwd': str(self.state_dir), 'model': 'gpt-5.6-sol', 'modelProvider': 'openai',
                  'approvalPolicy': 'never', 'sandbox': 'read-only', 'environments': [],
                  'developerInstructions': PROMPT, 'config': {'model_reasoning_effort': 'medium'},
                  'serviceName': 'stocks-native-mvp'}
        previous = self.thread_file.read_text().strip() if self.thread_file.exists() else None
        if previous:
            try:
                r = await self.call('thread/resume', {**params, 'threadId': previous})
                self.thread_id = r['thread']['id']
            except Exception:
                log.warning('Could not resume owned thread %s', previous)
        if not self.thread_id:
            r = await self.call('thread/start', {**params, 'dynamicTools': tools, 'ephemeral': False})
            self.thread_id = r['thread']['id']
            tmp = self.thread_file.with_suffix('.tmp')
            tmp.write_text(self.thread_id)
            tmp.replace(self.thread_file)
        self.pc.addTrack(self.input)
        dc = self.pc.createDataChannel('oai-events')
        dc.on('message')(self.on_dc)
        @self.pc.on('track')
        def received(track):
            if track.kind == 'audio':
                self.task(self.consume_audio(track))
        await self.pc.setLocalDescription(await self.pc.createOffer())
        initial = [{'role': 'developer', 'text': VOICE_RULES + '\n当前选中代码：' + (self.stock_code or '尚未选择')}]
        if self.journal.exists():
            with self.journal.open('rb') as f:
                f.seek(max(0, self.journal.stat().st_size - 24000))
                rows = f.read().decode('utf-8', errors='replace').splitlines()
            for line in rows[-20:]:
                with contextlib.suppress(ValueError):
                    e = json.loads(line)
                    if e.get('type') == 'transcript' and e.get('role') in ('user', 'assistant'):
                        initial.append({'role': e['role'], 'text': e['text'][:500]})
            initial = initial[:1] + initial[1:][-8:]
        await self.call('thread/realtime/start', {'threadId': self.thread_id, 'version': 'v3',
            'voice': 'sol', 'outputModality': 'audio', 'includeStartupContext': False,
            'initialItems': initial, 'clientManagedHandoffs': True, 'codexResponseHandoffMode': 'thinking',
            'codexResponsesAsItems': False, 'flushTranscriptTailOnSessionEnd': False,
            'transport': {'type': 'webrtc', 'sdp': self.pc.localDescription.sdp}})
        sdp = await asyncio.wait_for(self.sdp, 40)
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type='answer'))
        await asyncio.wait_for(self.ready.wait(), 30)
        await self.event({'type': 'state', 'state': 'active', 'sessionId': self.session_id, 'threadId': self.thread_id})

    async def text(self, text):
        value = text.strip()[:4000]
        if not value:
            raise ValueError('请输入要询问的内容')
        self.last_activity = time.monotonic()
        request_id = str(uuid.uuid4())
        delegation_waiting = self.delegation_pending and time.monotonic() - self.delegation_pending < 30
        if self.text_pending or self.active_turn_id or delegation_waiting:
            receipt = {'type': 'task', 'state': 'rejected', 'requestId': request_id,
                       'message': '当前查询仍在处理，请稍后再问。'}
            self.record(receipt)
            self.task(self.event(receipt))
            self.task(self.event({'type': 'transcript', 'role': 'assistant', 'final': True,
                                 'text': receipt['message'], 'requestId': request_id, 'source': 'control'}))
            return
        if self.closed or not self.ready.is_set():
            raise ValueError('语音连接尚未就绪')
        request = {'requestId': request_id, 'turnId': None, 'text': value}
        self.text_pending = request
        # Return before turn/start so the WebSocket can keep receiving microphone
        # packets throughout the model turn. The pending slot is reserved now.
        event = {'type': 'transcript', 'role': 'user', 'text': value, 'final': True,
                 'requestId': request_id, 'source': 'text'}
        self.record(event)
        self.task(self.submit_text(request, event))

    async def submit_text(self, request, user_event):
        try:
            await self.event(user_event)
            result = await self.call('turn/start', {
                'threadId': self.thread_id, 'input': [{'type': 'text', 'text': request['text']}],
                'clientUserMessageId': request['requestId'], 'environments': [],
            })
            turn_id = result['turn']['id']
            state = self.turn_state(turn_id)
            state.update(requestId=request['requestId'], source='text', inputText=request['text'])
            if not state['finishing']:
                self.active_turn_id = turn_id
            self.record({'type': 'request.accepted', 'requestId': request['requestId'], 'turnId': turn_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = {'requestId': request['requestId'], 'turnId': request.get('turnId'), 'source': 'text'}
            await self.fail_turn(state, '文字请求未能提交后台：' + safe_error(exc))
        finally:
            if self.text_pending is request:
                self.text_pending = None

    def audio(self, data):
        self.input.push(data)
        self.received_frames += 1

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.thread_id and self.proc and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                await self.call('thread/realtime/stop', {'threadId': self.thread_id}, timeout=8)
        with contextlib.suppress(Exception):
            await self.pc.close()
        self.input.stop()
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 6)
            except asyncio.TimeoutError:
                self.proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), 4)
                if self.proc.returncode is None:
                    self.proc.kill()
                    await self.proc.wait()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.record({'type': 'session.closed', 'sessionId': self.session_id, 'receivedFrames': self.received_frames,
                     'sentFrames': self.sent_frames, 'backendTurns': self.backend_turns})
