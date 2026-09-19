"""Read only the requested stock components and return bounded tool results."""

from __future__ import annotations

import asyncio
import copy
import re
from typing import Any


CONTEXT_SECTIONS = ('quote', 'orderBook', 'technical', 'fund', 'chips',
                    'announcements', 'peers', 'kline', 'intraday')
CHART_PERIODS = ('m5', 'm15', 'm30', 'm60', 'day', 'week', 'month')
ANALYSIS_SECTIONS = frozenset(('technical', 'fund', 'chips', 'announcements', 'peers'))
QUOTE_FIELDS = (
    'code', 'name', 'price', 'prevClose', 'open', 'high', 'low', 'changeAmount',
    'changePct', 'volume', 'turnover', 'turnoverRate', 'amplitude', 'volumeRatio',
    'peDynamic', 'pb', 'marketCap', 'floatMarketCap', 'upLimit', 'downLimit',
    'innerVolume', 'outerVolume', 'quoteTime',
)

STOCKS_CONTEXT_TOOL = {
    'type': 'function',
    'name': 'stocks_context',
    'description': (
        '按需读取当前股票的指定数据组件。只请求回答问题必需的 sections；'
        '报价及盘口复用短时行情缓存，技术、资金、筹码、公告和同行为带日期的服务器数据。'
        'K线和分时最多返回最近120点，公告和同行最多10条。'
        'asOf/source 按组件列出；时间为空表示来源未提供，不可当作实时。'
        '结果只更新请求的组件，其他界面上下文保持不变。'
    ),
    'inputSchema': {
        'type': 'object',
        'properties': {
            'code': {'type': 'string', 'pattern': '^[0-9]{6}$',
                     'description': '六位股票代码；省略时使用此会话当前选中的股票。'},
            'sections': {'type': 'array', 'items': {'type': 'string', 'enum': list(CONTEXT_SECTIONS)},
                         'minItems': 1, 'maxItems': len(CONTEXT_SECTIONS), 'uniqueItems': True},
            'period': {'type': 'string', 'enum': list(CHART_PERIODS), 'default': 'day',
                       'description': '仅用于 kline，默认日K。'},
        },
        'required': ['sections'],
        'additionalProperties': False,
    },
}


def _fields(value: dict, fields) -> dict:
    return {key: copy.deepcopy(value[key]) for key in fields if key in value}


def _tail_rows(value: Any, limit: int) -> list[dict]:
    return [copy.deepcopy(row) for row in value if isinstance(row, dict)][-limit:] if isinstance(value, list) else []


async def fetch_context_sections(data_store, live_source, code: str,
                                 sections_requested: list[str], period: str = 'day') -> dict[str, Any]:
    """Fetch independent requested sources once, preserving their market dates.

    ``asOf`` and ``source`` are maps keyed by section because a live quote and
    a daily analysis may have different dates. Missing timestamps stay null;
    this function never substitutes the retrieval clock for a market date.
    """
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
        raise ValueError('code must contain six digits')
    if (not isinstance(sections_requested, list) or not sections_requested or
            len(sections_requested) > len(CONTEXT_SECTIONS) or
            any(not isinstance(item, str) or item not in CONTEXT_SECTIONS for item in sections_requested)):
        raise ValueError('sections must be a nonempty list of supported components')
    if not isinstance(period, str) or period not in CHART_PERIODS:
        raise ValueError('unsupported chart period')
    requested = list(dict.fromkeys(sections_requested))
    jobs = {}
    if any(section in {'quote', 'orderBook'} for section in requested):
        jobs['quote'] = live_source.quotes([code])
    if ANALYSIS_SECTIONS.intersection(requested):
        jobs['analysis'] = asyncio.to_thread(data_store.stock_detail, code, candle_limit=1)
    if 'kline' in requested:
        jobs['kline'] = live_source.kline(code, period, 120)
    if 'intraday' in requested:
        jobs['intraday'] = live_source.minute(code)
    responses = await asyncio.gather(*jobs.values(), return_exceptions=True)
    providers = dict(zip(jobs, responses))
    result = {'code': code, 'asOf': {}, 'source': {}, 'sections': {}, 'warnings': []}

    def warn(message):
        if message not in result['warnings']:
            result['warnings'].append(message)

    for section in requested:
        provider = ('analysis' if section in ANALYSIS_SECTIONS else
                    'quote' if section == 'orderBook' else section)
        response = providers[provider]
        result['asOf'][section] = None
        result['source'][section] = 'server_analysis' if provider == 'analysis' else 'tencent'
        result['sections'][section] = None
        if isinstance(response, asyncio.CancelledError):
            raise response
        if isinstance(response, Exception) or not isinstance(response, dict):
            warn(section + '_source_unavailable')
            continue

        if provider == 'quote':
            quote = response.get(code)
            if not isinstance(quote, dict) or quote.get('code', code) != code:
                warn(section + '_unavailable')
                continue
            result['asOf'][section] = quote.get('quoteTime') or None
            if not result['asOf'][section]:
                warn(section + '_market_time_unavailable')
            if section == 'quote':
                value = _fields(quote, QUOTE_FIELDS)
            else:
                value = {side: [copy.deepcopy(row) for row in (quote.get(side) or [])[:5]
                                if isinstance(row, dict)] for side in ('bids', 'asks')}
            result['sections'][section] = value
        elif provider == 'analysis':
            result['source'][section] = response.get('source') or 'server_analysis'
            value = response.get(section)
            if section in ('announcements', 'peers'):
                fields = (('title', 'date', 'category', 'url') if section == 'announcements' else
                          ('code', 'name', 'price', 'changePct', 'turnoverRate', 'marketCap'))
                result['sections'][section] = [_fields(row, fields) for row in (value or [])[:10]
                                               if isinstance(row, dict)] if isinstance(value, list) else []
                # The collector does not expose a common market timestamp for
                # peer rows or a fetch timestamp for the announcement batch.
                warn(section + ('_use_item_dates' if section == 'announcements' else '_market_time_unavailable'))
            elif isinstance(value, dict):
                result['asOf'][section] = value.get('asOf') or None
                if section in ('technical', 'fund'):
                    selected = _fields(value, ('asOf', 'metrics', 'checks'))
                    selected['history'] = _tail_rows(value.get('history'), 30)
                else:
                    selected = _fields(value, ('asOf', 'low', 'high', 'cost5', 'cost15', 'cost50',
                                               'cost85', 'cost95', 'average', 'winnerRate'))
                result['sections'][section] = selected
            else:
                warn(section + '_unavailable')
            if 'stock_panel_database_unavailable' in (response.get('warnings') or []):
                warn(section + '_may_be_incomplete')
        else:
            if response.get('code', code) != code:
                warn(section + '_stock_mismatch')
                continue
            rows = _tail_rows(response.get('rows'), 120)
            if section == 'kline':
                result['sections'][section] = {'period': period, 'rows': rows}
                result['asOf'][section] = rows[-1].get('time') if rows else None
            else:
                trade_date = response.get('tradeDate') or None
                result['sections'][section] = {
                    'tradeDate': trade_date, 'previousClose': response.get('previousClose'), 'rows': rows,
                }
                latest_time = rows[-1].get('time') if rows else None
                result['asOf'][section] = (f'{trade_date} {latest_time}' if trade_date and latest_time else None)
            if not rows:
                warn(section + '_unavailable')
    return result
