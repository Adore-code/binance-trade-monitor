"""Bounded, non-blocking cache for public USDT futures positioning data."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import math
import threading
import time
import urllib.parse
import urllib.request
from position_store import PositionStore


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
    def __init__(self, directory=None):
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='positions')
        self.cache = {}
        self.pending = {}
        self.closed = False
        self.store = PositionStore(directory) if directory is not None else None
        self.stop_event = threading.Event()
        self.scheduler = None

    def watch(self, provider):
        def run():
            while not self.stop_event.is_set():
                symbols, active = provider()
                self.snapshot(symbols, active)
                self.stop_event.wait(1)
        self.scheduler = threading.Thread(target=run, daemon=True, name='position-scheduler')
        self.scheduler.start()

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
                if self.store:
                    try:
                        self.store.save(symbol, key, metric)
                        with self.lock:
                            self.cache[symbol]['metrics'][key]['log_error'] = ''
                    except Exception as exc:
                        with self.lock:
                            self.cache[symbol]['metrics'][key]['log_error'] = '持仓日志写入失败：'+str(exc)
            except Exception as exc:
                with self.lock:
                    metric = self.cache[symbol]['metrics'].setdefault(key, {})
                    metric['error'] = '更新失败：' + str(exc)
        with self.lock:
            self.cache[symbol]['checked'] = time.monotonic()
            self.pending.pop(symbol, None)

    def close(self):
        self.stop_event.set()
        if self.scheduler:
            self.scheduler.join()
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
