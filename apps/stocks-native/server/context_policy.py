"""Bounded screen context, per-section deltas and quote hysteresis.

Raw App snapshots stay in memory. Only a user turn or an active delegation may
send a prepared patch to a model. Accepted ledgers advance after the RPC succeeds.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time


def fingerprint(value):
    value = deepcopy(value)
    def strip(item):
        if isinstance(item, dict):
            for key in ('observedAtUtc', 'receivedAtUtc', 'occurredAtUtc', 'id'):
                item.pop(key, None)
            for child in item.values():
                strip(child)
        elif isinstance(item, list):
            for child in item:
                strip(child)
    strip(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode()).hexdigest()


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc).timestamp() if result.tzinfo is None else result.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def snapshot(context, now=None):
    result = deepcopy(context)
    now = time.time() if now is None else now
    actions = []
    period = result.get('chartPeriodID') or result.get('chartPeriod')
    for action in result.get('recentActions') or []:
        if not isinstance(action, dict) or action.get('stockCode') != result.get('selectedCode'):
            continue
        occurred = timestamp(action.get('occurredAtUtc'))
        if occurred is None or not -5 <= now - occurred <= 30:
            continue
        if action.get('kind') in ('chart_period', 'annotation', 'chart_selection', 'chart_range'):
            # Older clients used a display title on the root and raw ID on actions.
            if result.get('chartPeriodID') and action.get('chartPeriod') != period:
                continue
        actions.append(action)
    result['recentActions'] = actions[-3:]
    return result


def sections(context, audience):
    view = {key: context.get(key) for key in
            ('screen', 'selectedCode', 'selectedName', 'chartPeriod', 'chartPeriodID', 'viewState')}
    state = context.get('viewState') or {}
    card_ids = state.get('visibleCardIDs')
    has_card_visibility = card_ids is not None
    cards = {value for value in card_ids if isinstance(value, str)} if isinstance(card_ids, list) else set()
    if has_card_visibility:
        chart_visible = bool(cards.intersection({'chart', 'kline', 'intraday', 'klineChips'}))
        allowed_metrics = set()
        if 'quote' in cards or chart_visible:
            allowed_metrics.update({'open', 'high', 'low', 'close', 'volume', 'turnover', 'turnoverRate'})
        if 'macd' in cards:
            allowed_metrics.add('macdHist')
        if 'fund' in cards:
            allowed_metrics.add('mainInflow')
        order_book_visible = 'orderBook' in cards
        if not chart_visible and state.get('chartViewport') is not None:
            # A stale range summary must not survive a move to a page without a
            # chart. Do not mutate the cached App snapshot while projecting it.
            view['viewState'] = {**state, 'chartViewport': None}
    else:
        # Older clients describe fixed tabs and do not publish card IDs.
        research_visible = (not state or state.get('detailTab') == 'research'
                            or bool({'技术指标', '资金动向'}.intersection(context.get('visiblePanels') or [])))
        chart_visible = not state or state.get('detailTab') == 'chart'
        allowed_metrics = set(context.get('metrics') or {}) if research_visible else set()
        order_book_visible = True
    metrics = context.get('metrics') or {}
    quote = {key: context.get(key) for key in ('quoteAsOf', 'quoteTime', 'quoteSource', 'latestPointTime')}
    quote['metrics'] = {key: metrics[key] for key in ('price', 'changePct') if key in metrics}
    chart = context.get('chart') or {}
    selection_visible = chart_visible if has_card_visibility else True
    selection = chart.get('selectedPoint') if selection_visible and chart.get('selectionSource') == 'cursor' else None
    result = {'view': view, 'quote': quote, 'selection': selection or {},
              'requested': context.get('requestedData') or {},
              'actions': (context.get('recentActions') or [])[-(1 if audience == 'voice' else 3):]}
    if audience == 'backend':
        view['visiblePanels'] = context.get('visiblePanels') or []
        result.update(metrics={key: value for key, value in metrics.items()
                               if key not in ('price', 'changePct') and key in allowed_metrics},
                      chart=chart if chart_visible else {},
                      annotations=(context.get('annotations') or {}) if chart_visible else {},
                      orderBook=(context.get('orderBook') or {}) if order_book_visible else {})
    return result


def number(value):
    try:
        parsed = float(str(value).rstrip('%'))
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError):
        return None


def quote_is_jitter(previous, current, price_percent=0.05, change_points=0.05):
    """Compare to last *injected* value, so slow cumulative moves cannot disappear."""
    if previous.get('quoteAsOf') != current.get('quoteAsOf') or previous.get('quoteSource') != current.get('quoteSource'):
        return False
    before, after = previous.get('metrics', {}), current.get('metrics', {})
    if before.keys() != after.keys():
        return False
    for key in before:
        old, new = number(before[key]), number(after[key])
        if old is None or new is None:
            if before[key] != after[key]:
                return False
            continue
        if (old < 0) != (new < 0) or (old == 0) != (new == 0):
            return False
        tolerance = abs(old) * price_percent / 100 if key == 'price' else change_points
        delta = abs(new - old)
        if new != old and (delta >= tolerance or math.isclose(delta, tolerance, rel_tol=1e-9, abs_tol=1e-12)):
            return False
    return True


def prepare_patch(context, audience, ledger, same_scope, *, force_quote=False,
                  now=None, price_percent=0.05, change_points=0.05):
    now = time.monotonic() if now is None else now
    current = sections(context, audience)
    changed = {}
    accepted = {} if not same_scope else dict(ledger)
    for name, value in current.items():
        previous = ledger.get(name) if same_scope else None
        digest = fingerprint(value)
        if previous and previous['digest'] == digest:
            continue
        if (name == 'quote' and previous and not force_quote and now - previous['at'] < 60
                and quote_is_jitter(previous['value'], value, price_percent, change_points)):
            continue
        changed[name] = value
        accepted[name] = {'digest': digest, 'value': deepcopy(value), 'at': now}
    payload = {'mode': 'patch' if same_scope else 'replace',
               'selectedCode': context.get('selectedCode'), 'sections': changed}
    return payload, accepted


def requested_live_sections(text):
    value = str(text).casefold()
    if re.search(r'昨天|昨日|历史|上周|去年|这根|那根|光标|选中|yesterday|historical', value):
        return set()
    if re.search(r'买[一二三四五1-5]|卖[一二三四五1-5]|盘口|五档|order.?book|\bbid\b|\bask\b', value):
        return {'quote', 'orderBook'}
    if re.search(r'现价|多少钱|报价|价格|涨跌|涨幅|跌幅|涨多少|跌多少|涨了|跌了|股价|成交量|成交额|换手|量比|今开|最高|最低|\bprice\b|\bvolume\b', value):
        return {'quote'}
    return set()
