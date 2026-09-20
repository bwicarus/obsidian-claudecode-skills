"""Private native app gateway; the existing web application's data stays read-only."""
import asyncio
from collections import defaultdict, deque
import contextlib
import json
import logging
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web, WSMsgType, ClientError
from apple_auth import AppleIdentityVerifier
from auth import AuthStore, AuthError
from data import StockDataStore, DataUnavailable, StockNotFound
from chips import build_chips
from live import LiveMarketSource
from selection import SelectionService, SelectionError, SelectionConflict
from voice import VoiceSession, closes_voice_connection, safe_error
from monitoring import MonitorService, MonitorError
from notification_delivery import NotificationDelivery
from monitor_runtime import MonitorRuntime
from plans import PlanService, PlanError
from reports import ReportService, ReportError
from news import NewsService
from schedules import ScheduleService, ScheduleError
from schedule_runtime import ScheduleRuntime
from voice_settings import VoiceSettingsService, VoiceSettingsError

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
    except (SelectionError, MonitorError, PlanError, ScheduleError, ReportError, VoiceSettingsError) as exc:
        result = {'error': str(exc), 'message': str(exc), 'code': exc.code, **exc.detail}
        if isinstance(exc, SelectionConflict):
            result['revision'] = exc.revision
        response = web.json_response(result, status=exc.status)
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
    paired = await asyncio.to_thread(request.app['auth'].authenticate, result['token'], data['deviceId'])
    await asyncio.to_thread(request.app['notifications'].register, paired['ownerId'], data['deviceId'],
                           {'pushToken': None, 'voipToken': None, 'notificationsEnabled': False})
    return web.json_response(result)


async def apple_login(request):
    peer = request.headers.get('X-Real-IP', request.remote or 'unknown')
    now = time.monotonic()
    bucket = request.app['pair_attempts']['apple:' + peer]
    while bucket and now - bucket[0] > 600:
        bucket.popleft()
    if len(bucket) >= 20:
        return web.json_response({'error': '尝试过于频繁，请稍后重试'}, status=429)
    bucket.append(now)
    data = await request.json()
    claims = await request.app['apple'].verify(data['identityToken'], data['rawNonce'])
    header = request.headers.get('Authorization', '')
    previous_token = header[7:] if header.startswith('Bearer ') else None
    result = await asyncio.to_thread(request.app['auth'].apple_login, claims['sub'],
                                     data['deviceId'], data['name'], previous_token)
    previous_owner = result.pop('previousOwnerId', None)
    owner = result.pop('ownerId')
    await asyncio.to_thread(request.app['notifications'].register, owner, data['deviceId'],
                           {'pushToken': None, 'voipToken': None, 'notificationsEnabled': False})
    if previous_owner:
        try:
            await asyncio.to_thread(request.app['selection'].move_library, previous_owner, owner)
        except SelectionConflict:
            result['libraryMigrationWarning'] = '账号已有观察池；原设备资料已保留，需要手动合并。'
        except Exception as exc:
            log.warning('Device library adoption failed: %s', safe_error(exc))
            result['libraryMigrationWarning'] = '登录成功，原设备观察池暂未迁移；原资料仍然保留。'
    return web.json_response(result)


async def selection_catalog(request):
    await identity(request)
    return web.json_response(request.app['selection'].catalog())


async def selection_library(request):
    caller = await identity(request)
    result = await asyncio.to_thread(request.app['selection'].load_library, caller['ownerId'])
    return web.json_response(result)


async def selection_evaluate(request):
    caller = await identity(request)
    payload = await request.json()
    result = await asyncio.to_thread(request.app['selection'].evaluate, caller['ownerId'], payload)
    return web.json_response(result)


async def selection_mutate(request):
    caller = await identity(request)
    payload = await request.json()
    result = await asyncio.to_thread(request.app['selection'].mutate, caller['ownerId'], payload)
    if result.get('success') and not result.get('replayed'):
        event = {'type': 'selection.changed', 'revision': result['revision'],
                 'requestId': result['requestId'], 'operation': payload.get('operation')}
        for entry in list(request.app['voices'].values()):
            if entry and getattr(entry[1], 'selection_owner', None) == caller['ownerId']:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await entry[1].emit_json(event)
    return web.json_response(result)


async def monitor_catalog(request):
    await identity(request)
    return web.json_response(request.app['monitor'].catalog())


async def plans_list(request):
    caller = await identity(request)
    archived = request.query.get('includeArchived', '0')
    if archived not in ('0', '1'):
        raise ValueError('includeArchived must be 0 or 1')
    result = await asyncio.to_thread(request.app['plans'].list, caller['ownerId'],
        request.query.get('code'), int(request.query.get('limit', '100')), archived == '1')
    return web.json_response(result)


async def plan_item(request):
    caller = await identity(request)
    return web.json_response(await asyncio.to_thread(request.app['plans'].get, caller['ownerId'], request.match_info['id']))


async def plan_archive(request):
    caller = await identity(request)
    if not caller['aiEnabled']:
        return web.json_response({'error': '审核账号未开放策略修改'}, status=403)
    result = await asyncio.to_thread(request.app['plans'].archive, caller['ownerId'], await request.json())
    if not result.get('replayed'):
        event = {'type': 'plan.changed', 'revision': result['revision'], 'planId': result['planId'],
                 'code': result['plan']['code'], 'requestId': result['requestId'], 'operation': 'archive'}
        for entry in list(request.app['voices'].values()):
            if entry and getattr(entry[1], 'selection_owner', None) == caller['ownerId']:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await entry[1].emit_json(event)
    return web.json_response(result)


async def reports_list(request):
    caller = await identity(request)
    return web.json_response(await asyncio.to_thread(request.app['reports'].list, caller['ownerId'],
        request.query.get('code'), int(request.query.get('limit', '30')), request.query.get('before')))


async def report_item(request):
    caller = await identity(request)
    return web.json_response(await asyncio.to_thread(request.app['reports'].get, caller['ownerId'], request.match_info['id']))


async def research_signals(request):
    caller = await identity(request)
    codes = request.query.get('codes')
    return web.json_response(await asyncio.to_thread(request.app['reports'].signals, caller['ownerId'],
        codes.split(',') if codes is not None else None))


async def plan_preview(request):
    caller = await identity(request)
    return web.json_response(await asyncio.to_thread(request.app['reports'].preview, caller['ownerId'], await request.json()))


async def plan_activate(request):
    caller = await identity(request)
    if not caller['aiEnabled']:
        return web.json_response({'error': '审核账号未开放自动盯盘'}, status=403)
    result = await asyncio.to_thread(request.app['reports'].apply, caller['ownerId'], await request.json())
    adoption = result['adoption']
    for entry in list(request.app['voices'].values()):
        if entry and getattr(entry[1], 'selection_owner', None) == caller['ownerId']:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await entry[1].emit_json({'type': 'monitor.changed',
                    'requestId': result['requestId'], 'operation': 'plan.activate', 'code': adoption['code'], 'planId': adoption['planId']})
                await entry[1].emit_json({'type': 'report.changed', 'revision': result['revision'],
                    'requestId': result['requestId'], 'operation': 'plan.activate', 'code': adoption['code'],
                    'planId': adoption['planId'], 'reportId': adoption.get('reportId')})
    return web.json_response(result)


async def news_feed(request):
    await identity(request)
    refresh = request.query.get('refresh', '0')
    if refresh not in ('0', '1', 'false', 'true'):
        raise ValueError('refresh must be a boolean')
    return web.json_response(await request.app['news'].feed(category=request.query.get('category', 'macro'),
        code=request.query.get('code'), sector=request.query.get('sector'), limit=int(request.query.get('limit', '30')),
        refresh=refresh in ('1', 'true')))


async def legacy_research(request):
    await identity(request)
    return web.json_response(await asyncio.to_thread(request.app['news'].legacy_signals,
        request.query.get('code'), int(request.query.get('limit', '50'))))


async def monitor_library(request):
    caller = await identity(request)
    result = await asyncio.to_thread(request.app['monitor'].library, caller['ownerId'])
    result['pushConfigured'] = request.app['notifications'].configured()
    return web.json_response(result)


async def monitor_mutate(request):
    caller = await identity(request)
    if not caller['aiEnabled']:
        return web.json_response({'error': '审核账号未开放自动盯盘'}, status=403)
    payload = await request.json()
    return web.json_response(await asyncio.to_thread(request.app['monitor'].mutate, caller['ownerId'], payload))


async def notification_item(request):
    caller = await identity(request)
    item = await asyncio.to_thread(request.app['monitor'].get_notification, caller['ownerId'], request.match_info['id'])
    if not item:
        raise web.HTTPNotFound()
    return web.json_response(item)


async def notification_device(request):
    caller = await identity(request)
    payload = await request.json()
    if payload.get('deviceId', caller['deviceId']) != caller['deviceId']:
        raise AuthError('device mismatch')
    result = await asyncio.to_thread(request.app['notifications'].register, caller['ownerId'], caller['deviceId'], payload)
    return web.json_response(result)


async def notification_presence(request):
    caller = await identity(request)
    payload = await request.json()
    return web.json_response(await asyncio.to_thread(request.app['notifications'].presence,
        caller['ownerId'], caller['deviceId'], payload.get('foreground')))


async def notification_receipt(request):
    caller = await identity(request)
    payload = await request.json()
    notice_id = payload.get('notificationId', '')
    item = await asyncio.to_thread(request.app['monitor'].get_notification, caller['ownerId'], notice_id)
    if not item:
        raise web.HTTPNotFound()
    channel, outcome = payload.get('channel'), payload.get('outcome')
    if channel == 'call':
        result = await asyncio.to_thread(request.app['notifications'].call_receipt, caller['ownerId'],
            caller['deviceId'], notice_id, payload.get('callId'), outcome)
        await asyncio.to_thread(request.app['monitor'].mark_delivery, caller['ownerId'], notice_id, {'call': outcome})
    elif channel == 'visual' and outcome == 'displayed':
        await asyncio.to_thread(request.app['notifications'].receipt,
                               caller['ownerId'], notice_id, channel, caller['deviceId'], outcome)
        await asyncio.to_thread(request.app['monitor'].mark_delivery, caller['ownerId'], notice_id, {'visual': outcome})
        result = {'success': True}
    else:
        raise ValueError('invalid delivery receipt')
    return web.json_response(result)


async def notification_call(request):
    caller = await identity(request)
    call = await asyncio.to_thread(request.app['notifications'].call, caller['ownerId'], caller['deviceId'], request.match_info['id'])
    if call.get('notificationId'):
        item = await asyncio.to_thread(request.app['monitor'].get_notification, caller['ownerId'], call['notificationId'])
        if item:
            call.update(code=item.get('code'), title=item.get('title'))
            if item.get('status') == 'resolved':
                call['valid'] = False
        else:
            call['valid'] = False
    return web.json_response(call)


async def stocks(request):
    await identity(request)
    result = await asyncio.to_thread(request.app['data'].list_stocks,
        request.query.get('q', '')[:80], int(request.query.get('limit', 50)))
    try:
        quotes = await request.app['live'].quotes([item['code'] for item in result['items']])
        for item in result['items']:
            request.app['data'].overlay_live(item, quotes.get(item['code']))
    except (ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning('Live list quote unavailable: %s', safe_error(exc))
        result.setdefault('warnings', []).append('live_quote_unavailable')
    return web.json_response(result)


async def detail(request):
    await identity(request)
    result = await asyncio.to_thread(request.app['data'].stock_detail, request.match_info['code'])
    try:
        quotes = await request.app['live'].quotes([request.match_info['code']])
        request.app['data'].overlay_live(result['stock'], quotes.get(request.match_info['code']))
    except (ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning('Live detail quote unavailable: %s', safe_error(exc))
        result.setdefault('warnings', []).append('live_quote_unavailable')
    return web.json_response(result)


async def market_overview(request):
    await identity(request)
    result = await asyncio.to_thread(request.app['data'].market_overview)
    return web.json_response(result)


async def realtime(request):
    await identity(request)
    raw = request.query.get('codes', '')
    codes = [code.strip() for code in raw.split(',') if code.strip()][:100]
    if any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError('invalid stock code')
    try:
        quotes = await request.app['live'].quotes(codes)
        return web.json_response({'items': list(quotes.values()), 'warning': None})
    except (ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning('Realtime quote unavailable: %s', safe_error(exc))
        return web.json_response({'items': [], 'warning': 'realtime_source_unavailable'})


async def intraday(request):
    await identity(request)
    code = request.match_info['code']
    if len(code) != 6 or not code.isdigit():
        raise ValueError('invalid stock code')
    try:
        return web.json_response(await request.app['live'].minute(code))
    except (ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning('Intraday quote unavailable: %s', safe_error(exc))
        return web.json_response({'code': code, 'tradeDate': '', 'previousClose': None,
                                  'rows': [], 'warning': 'realtime_source_unavailable'})


async def kline(request):
    await identity(request)
    code = request.match_info['code']
    if len(code) != 6 or not code.isdigit():
        raise ValueError('invalid stock code')
    period = request.query.get('period', 'day')
    count = int(request.query.get('count', 180))
    try:
        return web.json_response(await request.app['live'].kline(code, period, count))
    except (ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning('K-line source unavailable: %s', safe_error(exc))
        return web.json_response({'code': code, 'period': period, 'rows': [],
                                  'warning': 'realtime_source_unavailable'})


async def health(request):
    return web.json_response({'status': 'ok', 'version': '0.2.0', 'contextProtocol': 2,
                              'chartDataProtocol': 1, 'selectionProtocol': 1, 'monitorProtocol': 1,
                              'pushConfigured': request.app['notifications'].configured()})


async def chips(request):
    await identity(request)
    code = request.match_info['code']
    start, end = request.query.get('start'), request.query.get('end')
    key = (code, start, end)
    cache = request.app['chip_cache']
    cached = cache.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        return web.json_response(cached[1])
    async def load():
        payload = await asyncio.to_thread(request.app['data'].chip_history, code, start, end)
        quote, warning = None, None
        try:
            quotes = await asyncio.wait_for(request.app['live'].quotes([code]), timeout=4)
            quote = quotes.get(code)
            if quote is None:
                warning = 'live_quote_unavailable'
        except (ClientError, asyncio.TimeoutError, OSError):
            warning = 'live_quote_unavailable'
        return await asyncio.to_thread(build_chips, payload, quote, warning)
    result = await asyncio.wait_for(load(), timeout=8)
    cache[key] = (time.monotonic(), result)
    if len(cache) > 128:
        for old_key in list(cache)[:32]:
            cache.pop(old_key, None)
    return web.json_response(result)


async def voice_ink(request):
    device_id = request.headers.get('X-Device-ID')
    if not device_id:
        raise AuthError('Missing device')
    caller = await identity(request, device_id)
    if not caller['aiEnabled']:
        raise web.HTTPForbidden()
    if request.content_length is None or request.content_length > 340_000:
        raise web.HTTPRequestEntityTooLarge(max_size=340_000, actual_size=request.content_length or 0)
    payload = await request.clone(client_max_size=340_000).json()
    active = request.app['voices'].get(device_id)
    session = active[1] if active else None
    if (not session or session.closed or session.selection_owner != caller['ownerId']
            or session.session_id != request.headers.get('X-Voice-Session')):
        return web.json_response({'error': '通话已结束，笔迹保留在本机'}, status=409)
    result = await session.update_ink(payload)
    return web.json_response(result)


async def voice_settings_get(request):
    caller = await identity(request)
    if not caller['aiEnabled']:
        raise web.HTTPForbidden()
    settings = await asyncio.to_thread(request.app['voice_settings'].load, caller['ownerId'])
    return web.json_response({'settings': settings, 'applies': 'next_connection'})


async def voice_settings_save(request):
    caller = await identity(request)
    if not caller['aiEnabled']:
        raise web.HTTPForbidden()
    settings = await request.app['voice_settings'].save(caller['ownerId'], await request.json())
    return web.json_response({'settings': settings, 'applies': 'next_connection'})


async def voice_catalog(request):
    caller = await identity(request)
    if not caller['aiEnabled']:
        raise web.HTTPForbidden()
    return web.json_response(await request.app['voice_settings'].catalog())


async def voice(request):
    device_id = request.query.get('deviceId')
    if not device_id:
        raise AuthError('Missing device')
    caller = await identity(request, device_id)
    if not caller["aiEnabled"]:
        return web.json_response({'error': '审核账号未开放 AI 助手'}, status=403)
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
    call_id = None
    call_notice = None
    writes = asyncio.Lock()
    async def emit_json(event):
        async with writes:
            if not ws.closed:
                await ws.send_json(event)
        if closes_voice_connection(event):
            # Keep Codex's reader available to answer the stop RPC during cleanup.
            asyncio.create_task(ws.close())
    async def emit_audio(data):
        async with writes:
            if not ws.closed:
                await ws.send_bytes(data)
    async def start(code, capabilities):
        try:
            await session.start(code, capabilities)
            if call_notice:
                if await asyncio.to_thread(request.app['notifications'].reserve_delivery, caller['ownerId'], call_notice['id'], 'spoken', 'account'):
                    await session.announce_notification(call_notice)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Voice start failed: %s', safe_error(exc))
            await emit_json({'type': 'error', 'fatal': True,
                             'message': '语音连接失败：' + safe_error(exc)})
    async def watch():
        started = time.monotonic()
        idle_seconds = max(60, int(os.environ.get('STOCKS_VOICE_IDLE_SECONDS', '1200')))
        while not ws.closed:
            await asyncio.sleep(10)
            now = time.monotonic()
            await asyncio.to_thread(request.app['notifications'].voice_presence, caller['ownerId'], device_id)
            try:
                await identity(request, device_id)
            except AuthError:
                await emit_json({'type': 'state', 'state': 'closed', 'reason': 'account_changed'})
                return
            if call_id:
                call = await asyncio.to_thread(request.app['notifications'].call, caller['ownerId'], device_id, call_id)
                if not call.get('valid'):
                    await emit_json({'type': 'state', 'state': 'closed', 'reason': 'call_ended'})
                    return
            delegation_busy = (session and session.delegation_pending and
                               now - session.delegation_pending < 30)
            busy = session and (session.active_turn_id or session.text_pending or delegation_busy or
                                session.user_speaking or session.assistant_speaking)
            if busy:
                session.last_activity = now
                continue
            if (not start_task and now - started > 30) or (session and now - session.last_activity > idle_seconds):
                await emit_json({'type': 'state', 'state': 'closed', 'reason': 'idle'})
    try:
        await ws.prepare(request)
        session = VoiceSession(device_id, request.app['state'], request.app['data'], emit_json, emit_audio,
                               live_source=request.app['live'],
                               selection_service=request.app['selection'], selection_owner=caller['ownerId'])
        session.monitor_service = request.app['monitor']
        session.plan_service = request.app['plans']
        session.report_service = request.app['reports']
        session.notification_delivery = request.app['notifications']
        session.voice_settings_service = request.app['voice_settings']
        active[device_id] = (ws, session)
        await asyncio.to_thread(request.app['notifications'].voice_presence, caller['ownerId'], device_id)
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
                        zone = obj.get('clientTimeZone')
                        if zone is not None:
                            if not isinstance(zone, str) or not zone or len(zone) > 80:
                                raise ValueError('设备时区无效')
                            try:
                                ZoneInfo(zone)
                            except (ZoneInfoNotFoundError, ValueError) as exc:
                                raise ValueError('设备时区无效') from exc
                            session.client_time_zone = zone
                        if obj.get('callId'):
                            call = await asyncio.to_thread(request.app['notifications'].call, caller['ownerId'], device_id, str(obj['callId']))
                            if not call.get('valid') or call.get('status') != 'answered':
                                await emit_json({'type': 'error', 'fatal': True, 'message': '来电已结束或尚未接听'})
                                break
                            call_id = str(obj['callId'])
                            call_notice = await asyncio.to_thread(request.app['monitor'].get_notification, caller['ownerId'], call['notificationId'])
                            if not call_notice or call_notice.get('status') == 'resolved':
                                await emit_json({'type': 'error', 'fatal': True, 'message': '提醒已处理'})
                                break
                        start_task = asyncio.create_task(start(obj.get('stockCode'), obj.get('capabilities', '')))
                    elif kind == 'stop':
                        await ws.send_json({'type': 'state', 'state': 'closed', 'reason': 'manual'})
                        break
                    elif kind == 'thread.new':
                        session.reset_thread()
                        await emit_json({'type': 'state', 'state': 'closed', 'reason': 'new_thread'})
                        break
                    elif kind == 'text' and session.ready.is_set():
                        await session.text(str(obj.get('text', '')))
                    elif kind == 'stock.select':
                        await session.select_stock(str(obj.get('code', '')))
                    elif kind == 'ui.context':
                        session.task(session.update_context(obj.get('context') or {}))
                    elif kind == 'capability.result':
                        session.capability_result(str(obj.get('actionId', '')),
                                                  str(obj.get('success', '')).lower() == 'true',
                                                  str(obj.get('message', '')))
                    else:
                        raise ValueError('请先等待语音连接就绪')
                elif msg.type == WSMsgType.ERROR:
                    break
            except (ValueError, KeyError, StockNotFound) as exc:
                await emit_json({'type': 'error', 'fatal': False, 'message': safe_error(exc)})
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
            if call_id and call_notice:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(request.app['notifications'].call_receipt, caller['ownerId'], device_id,
                                            call_notice['id'], call_id, 'ended')
            active.pop(device_id, None)
            await asyncio.to_thread(request.app['notifications'].voice_presence, caller['ownerId'], device_id, False)
            await ws.close()
    return ws


async def shutdown(app):
    if app.get('schedule_runtime'):
        await app['schedule_runtime'].close()
    if app.get('monitor_runtime'):
        await app['monitor_runtime'].close()
    for entry in list(app['voices'].values()):
        if entry:
            await entry[0].close(code=1001, message=b'Server shutdown')
    await app['live'].close()
    await app['news'].close()


async def startup(app):
    if os.environ.get('STOCKS_MONITOR_ENABLED') == '1':
        app['monitor_runtime'] = MonitorRuntime(app)
        app['monitor_runtime'].start()
        app['schedule_runtime'] = ScheduleRuntime(app)
        app['schedule_runtime'].start()


def create_app():
    state = Path(os.environ.get('STOCKS_MVP_STATE_DIR', '/var/lib/stocks-native')).resolve()
    app = web.Application(middlewares=[errors], client_max_size=128000)
    app['state'] = state
    app['auth'] = AuthStore(state)
    app['apple'] = AppleIdentityVerifier(os.environ.get('APPLE_CLIENT_ID', 'space.bwicarus.stocksnative'))
    app['data'] = StockDataStore(os.environ.get('STOCKS_DATA_ROOT', '/root/webapp/data/stocks'))
    app['selection'] = SelectionService(app['data'], state)
    app['monitor'] = MonitorService(state)
    app['plans'] = PlanService(state)
    app['reports'] = ReportService(state, app['plans'], app['monitor'])
    app['news'] = NewsService(app['data'], state)
    app['schedules'] = ScheduleService(state)
    app['notifications'] = NotificationDelivery(state)
    app['voice_settings'] = VoiceSettingsService(state)
    app['live'] = LiveMarketSource()
    app['pair_attempts'] = defaultdict(deque)
    app['voices'] = {}
    app['chip_cache'] = {}
    app.add_routes([web.get('/api/health', health), web.post('/api/pair', pair),
                    web.post('/api/voice/ink', voice_ink),
                    web.get('/api/voice/settings', voice_settings_get),
                    web.post('/api/voice/settings', voice_settings_save),
                    web.get('/api/voice/catalog', voice_catalog),
                    web.post('/api/auth/apple', apple_login),
                    web.get('/api/monitor/catalog', monitor_catalog),
                    web.get('/api/monitor/library', monitor_library),
                    web.post('/api/monitor/mutate', monitor_mutate),
                    web.get('/api/plans', plans_list),
                    web.post('/api/plans/archive', plan_archive),
                    web.post('/api/plans/preview', plan_preview),
                    web.post('/api/plans/activate', plan_activate),
                    web.get('/api/plans/{id}', plan_item),
                    web.get('/api/reports', reports_list),
                    web.get('/api/reports/{id}', report_item),
                    web.get('/api/research/signals', research_signals),
                    web.get('/api/research/legacy', legacy_research),
                    web.get('/api/news', news_feed),
                    web.post('/api/notifications/device', notification_device),
                    web.post('/api/notifications/presence', notification_presence),
                    web.post('/api/notifications/receipt', notification_receipt),
                    web.get('/api/notifications/call/{id}', notification_call),
                    web.get('/api/notifications/{id}', notification_item),
                    web.get('/api/selection/catalog', selection_catalog),
                    web.get('/api/selection/library', selection_library),
                    web.post('/api/selection/evaluate', selection_evaluate),
                    web.post('/api/selection/mutate', selection_mutate),
                    web.get('/api/market/overview', market_overview),
                    web.get('/api/realtime', realtime),
                    web.get('/api/stocks', stocks),
                    web.get('/api/stocks/{code}/intraday', intraday),
                    web.get('/api/stocks/{code}/kline', kline),
                    web.get('/api/stocks/{code}/chips', chips),
                    web.get('/api/stocks/{code}', detail),
                    web.get('/voice', voice)])
    app.on_shutdown.append(shutdown)
    app.on_startup.append(startup)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(), host='127.0.0.1', port=int(os.environ.get('STOCKS_PORT', '5012')),
                access_log=None, shutdown_timeout=20)
