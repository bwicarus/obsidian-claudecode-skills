"""Bounded local chip estimate, adapted from stocks-project/scripts/local_cyq.py.

This preserves the deployed calculator's NumPy branch: historical holdings
decay by turnover and each day's volume uses a triangular price distribution.
It is a model of holdings from OHLCV, not observed individual trade costs.
The pure-Python implementation avoids the original fallback's different
uniform distribution and needs no additional gateway dependency.
"""
from __future__ import annotations

import math


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_row(row):
    result = {key: number(row.get(key)) for key in
              ('open', 'high', 'low', 'close', 'volume', 'turnover_rate')}
    result['trade_date'] = row.get('trade_date')
    if any(result[key] is None or result[key] <= 0 for key in ('open', 'high', 'low', 'close')):
        return None
    if not result['low'] <= min(result['open'], result['close']) <= max(result['open'], result['close']) <= result['high']:
        return None
    if result['volume'] is None or result['volume'] < 0 or result['turnover_rate'] is None:
        return None
    return result


def chip_distribution(history):
    """Return real per-bin model weights; never interpolate cost quantiles."""
    rows = [row for value in history if (row := _valid_row(value)) is not None]
    if len(rows) < 3:
        return []
    low = min(row['low'] for row in rows) * 0.95
    high = max(row['high'] for row in rows) * 1.05
    width = rows[-1]['close'] * 0.001
    count = max(50, min(800, round((high - low) / width)))
    step = (high - low) / count
    if not math.isfinite(step) or step <= 0:
        return []
    bins = [0.0] * count
    for row in rows:
        decay = 1.0 - max(0.0, min(100.0, row['turnover_rate'])) / 100.0
        bins = [weight * decay for weight in bins]
        shares = row['volume'] * 100
        if shares <= 0:
            continue
        first = max(0, min(count - 1, int((row['low'] - low) / step)))
        last = max(first, min(count - 1, int((row['high'] - low) / step)))
        if first == last:
            bins[first] += shares
            continue
        average = (row['open'] + row['close']) / 2
        peak = max(first, min(last, int((average - low) / step)))
        distance = max(peak - first, last - peak, 1)
        weights = [max(0.0, 1 - abs(index - peak) / distance) for index in range(first, last + 1)]
        total = sum(weights)
        if total:
            for index, weight in enumerate(weights, first):
                bins[index] += shares * weight / total
    total = sum(bins)
    if not math.isfinite(total) or total <= 0:
        return []
    # Cent-level prices can coincide at low nominal prices. Merge those buckets
    # instead of returning duplicate IDs to the native chart.
    prices = {}
    for index, weight in enumerate(bins):
        if weight > 0:
            price = round(low + (index + 0.5) * step, 2)
            prices[price] = prices.get(price, 0.0) + weight / total * 100
    return [{'price': price, 'percent': round(percent, 6)} for price, percent in prices.items()]


def build_chips(payload, live=None, live_warning=None):
    """Overlay only a dated quote inside the chosen window, then summarize."""
    history = [dict(row) for row in payload['history']]
    warning = payload.get('warning')
    source = 'local-cyq-estimate:daily_quotes'
    live_date = str((live or {}).get('quoteTime') or '')[:10]
    if live and live.get('code') == payload['code'] and payload['start'] <= live_date <= payload['end']:
        overlay = {'trade_date': live_date, 'open': live.get('open'), 'high': live.get('high'),
                   'low': live.get('low'), 'close': live.get('price'), 'volume': live.get('volume'),
                   'turnover_rate': live.get('turnoverRate')}
        if _valid_row(overlay) is not None:
            history = [row for row in history if row['trade_date'] != live_date]
            history.append(overlay)
            history.sort(key=lambda row: row['trade_date'])
            source += '+tencent'
        else:
            warning = warning or 'live_quote_incomplete'
    elif live_warning:
        warning = warning or live_warning
    valid = [row for item in history if (row := _valid_row(item)) is not None]
    rows = chip_distribution(valid)
    current = valid[-1]['close'] if valid else None
    if len(valid) < len(history):
        warning = warning or 'incomplete_chip_history_omitted'
    result = {'code': payload['code'], 'start': payload['start'], 'end': payload['end'],
              'rows': rows, 'currentPrice': current, 'averageCost': None, 'winnerRate': None,
              'cost5': None, 'cost95': None, 'concentration': None,
              'source': source, 'warning': warning}
    if not rows:
        result['warning'] = warning or 'insufficient_chip_history'
        return result
    total = sum(row['percent'] for row in rows)
    result['averageCost'] = round(sum(row['price'] * row['percent'] for row in rows) / total, 4)
    result['winnerRate'] = round(min(100.0, sum(row['percent'] for row in rows if row['price'] <= current) / total * 100), 4)
    cumulative = 0.0
    for row in rows:
        cumulative += row['percent'] / total
        if result['cost5'] is None and cumulative >= 0.05:
            result['cost5'] = row['price']
        if cumulative >= 0.95:
            result['cost95'] = row['price']
            break
    lower, upper = result['cost5'], result['cost95']
    result['concentration'] = round((upper - lower) / (upper + lower) * 100, 4)
    return result
