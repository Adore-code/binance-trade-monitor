"""Bounded, non-blocking cache for public USDT futures positioning data."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import math
import threading
import time
import urllib.parse
import urllib.request


def fetch_json(path, params):
    url = 'https://fapi.binance.com' + path + '?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.load(response)


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('无效数值')
    return value


class Positions:
    def __init__(self):
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='positions')
        self.cache = {}
        self.pending = {}
        self.closed = False

    def snapshot(self, symbols, active):
        with self.lock:
            for symbol in list(self.cache):
                if symbol not in symbols and symbol not in self.pending:
                    del self.cache[symbol]
            rows = []
            for symbol in symbols:
                row = self.cache.setdefault(symbol, dict(symbol=symbol, metrics={}, checked=float('-inf')))
                if active and not self.closed and symbol not in self.pending and time.monotonic()-row['checked'] >= 60:
                    self.pending[symbol] = self.pool.submit(self.refresh, symbol)
                result = copy.deepcopy(row)
                result.pop('checked')
                result['loading'] = symbol in self.pending
                rows.append(result)
            return dict(active=active, period='5m', rows=rows)

    def refresh(self, symbol):
        endpoints = [('open_interest', '/fapi/v1/openInterest'),
                     ('top_positions', '/futures/data/topLongShortPositionRatio'),
                     ('global_accounts', '/futures/data/globalLongShortAccountRatio')]
        for key, path in endpoints:
            with self.lock:
                if self.closed:
                    break
            try:
                params = dict(symbol=symbol)
                if key != 'open_interest':
                    params.update(period='5m', limit=1)
                data = fetch_json(path, params)
                if key == 'open_interest':
                    metric = dict(value=positive(data['openInterest']), time=int(data['time']))
                else:
                    data = data[-1]
                    long, short = positive(data['longAccount']), positive(data['shortAccount'])
                    if long > 1 or short > 1:
                        raise ValueError('无效占比')
                    metric = dict(long=long, short=short, ratio=positive(data['longShortRatio']), time=int(data['timestamp']))
                if data['symbol'] != symbol or metric['time'] <= 0:
                    raise ValueError('币种或数据时间异常')
                metric.update(error='', fetched_at=int(time.time()*1000))
                with self.lock:
                    self.cache[symbol]['metrics'][key] = metric
            except Exception as exc:
                with self.lock:
                    metric = self.cache[symbol]['metrics'].setdefault(key, {})
                    metric['error'] = '更新失败：' + str(exc)
        with self.lock:
            self.cache[symbol]['checked'] = time.monotonic()
            self.pending.pop(symbol, None)

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
