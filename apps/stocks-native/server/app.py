"""Private native app gateway; the existing web application's data stays read-only."""
import asyncio
from collections import defaultdict, deque
import contextlib
import json
import logging
import os
from pathlib import Path
import time

from aiohttp import web, WSMsgType
from auth import AuthStore, AuthError
from data import StockDataStore, DataUnavailable, StockNotFound
from voice import VoiceSession, safe_error

log = logging.getLogger(__name__)


@web.middleware
async def errors(request, handler):
    try:
        response = await handler(request)
    except AuthError:
        response = web.json_response({'error': '配对码或设备凭据无效，请重新配对'}, status=401)
    except StockNotFound:
        response = web.json_response({'error': '未找到该股票'}, status=404)
    except DataUnavailable:
        response = web.json_response({'error': '行情数据暂不可用，请稍后重试'}, status=503)
    except (ValueError, TypeError, KeyError):
        response = web.json_response({'error': '请求格式不正确'}, status=400)
    except web.HTTPException:
        raise
    except Exception as exc:
        log.error('Request failed: %s', safe_error(exc))
        response = web.json_response({'error': '服务暂不可用'}, status=500)
    response.headers['Cache-Control'] = 'no-store'
    return response


async def identity(request, device_id=None):
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        raise AuthError('Missing bearer')
    return await asyncio.to_thread(request.app['auth'].authenticate, header[7:],
                                   device_id or request.headers.get('X-Device-ID'))


async def pair(request):
    # Only loopback nginx can reach this server; nginx replaces this header.
    peer = request.headers.get('X-Real-IP', request.remote or 'unknown')
    now = time.monotonic()
    bucket = request.app['pair_attempts'][peer]
    while bucket and now - bucket[0] > 600:
        bucket.popleft()
    if len(bucket) >= 10:
        return web.json_response({'error': '尝试过于频繁，请十分钟后重试'}, status=429)
    bucket.append(now)
    data = await request.json()
    result = await asyncio.to_thread(request.app['auth'].pair,
        data['code'], data['deviceId'], data['name'])
    return web.json_response(result)


async def stocks(request):
    await identity(request)
    result = await asyncio.to_thread(request.app['data'].list_stocks,
        request.query.get('q', '')[:80], int(request.query.get('limit', 50)))
    return web.json_response(result)


async def detail(request):
    await identity(request)
    result = await asyncio.to_thread(request.app['data'].stock_detail, request.match_info['code'])
    return web.json_response(result)


async def health(request):
    return web.json_response({'status': 'ok', 'version': '0.2.0'})


async def voice(request):
    device_id = request.query.get('deviceId')
    if not device_id:
        raise AuthError('Missing device')
    await identity(request, device_id)
    active = request.app['voices']
    if device_id in active:
        return web.json_response({'error': '该设备已有通话，请先结束原通话'}, status=409)
    if len(active) >= 4:
        return web.json_response({'error': '当前通话数量已满'}, status=429)
    # Reserve before awaiting prepare, so concurrent requests cannot both pass.
    active[device_id] = None
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16384)
    session = None
    start_task = None
    watch_task = None
    writes = asyncio.Lock()
    async def emit_json(event):
        async with writes:
            if not ws.closed:
                await ws.send_json(event)
        if event.get('type') == 'error' or (event.get('type') == 'state' and event.get('state') == 'closed'):
            # Keep Codex's reader available to answer the stop RPC during cleanup.
            asyncio.create_task(ws.close())
    async def emit_audio(data):
        async with writes:
            if not ws.closed:
                await ws.send_bytes(data)
    async def start(code):
        try:
            await session.start(code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Voice start failed: %s', safe_error(exc))
            await emit_json({'type': 'error', 'message': '语音连接失败：' + safe_error(exc)})
    async def watch():
        started = time.monotonic()
        while not ws.closed:
            await asyncio.sleep(10)
            now = time.monotonic()
            if (not start_task and now - started > 30) or (session and now - session.last_activity > 600):
                await emit_json({'type': 'state', 'state': 'closed', 'reason': 'idle'})
    try:
        await ws.prepare(request)
        session = VoiceSession(device_id, request.app['state'], request.app['data'], emit_json, emit_audio)
        active[device_id] = (ws, session)
        watch_task = asyncio.create_task(watch())
        async for msg in ws:
            try:
                if msg.type == WSMsgType.BINARY:
                    if start_task and session.ready.is_set():
                        session.audio(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    obj = json.loads(msg.data)
                    kind = obj.get('type')
                    if kind == 'start' and start_task is None:
                        start_task = asyncio.create_task(start(obj.get('stockCode')))
                    elif kind == 'stop':
                        await ws.send_json({'type': 'state', 'state': 'closed'})
                        break
                    elif kind == 'text' and session.ready.is_set():
                        await session.text(str(obj.get('text', '')))
                    elif kind == 'stock.select':
                        await session.select_stock(str(obj.get('code', '')))
                    else:
                        raise ValueError('请先等待语音连接就绪')
                elif msg.type == WSMsgType.ERROR:
                    break
            except (ValueError, KeyError, StockNotFound) as exc:
                await emit_json({'type': 'error', 'message': safe_error(exc)})
    finally:
        for task in (start_task, watch_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        try:
            if session:
                await session.close()
        finally:
            active.pop(device_id, None)
            await ws.close()
    return ws


async def shutdown(app):
    for entry in list(app['voices'].values()):
        if entry:
            await entry[0].close(code=1001, message=b'Server shutdown')


def create_app():
    state = Path(os.environ.get('STOCKS_MVP_STATE_DIR', '/var/lib/stocks-native')).resolve()
    app = web.Application(middlewares=[errors], client_max_size=16384)
    app['state'] = state
    app['auth'] = AuthStore(state)
    app['data'] = StockDataStore(os.environ.get('STOCKS_DATA_ROOT', '/root/webapp/data/stocks'))
    app['pair_attempts'] = defaultdict(deque)
    app['voices'] = {}
    app.add_routes([web.get('/api/health', health), web.post('/api/pair', pair),
                    web.get('/api/stocks', stocks), web.get('/api/stocks/{code}', detail),
                    web.get('/voice', voice)])
    app.on_shutdown.append(shutdown)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), host='127.0.0.1', port=int(os.environ.get('STOCKS_PORT', '5012')),
                access_log=None, shutdown_timeout=20)
