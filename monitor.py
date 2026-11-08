"""Multi-symbol public trade monitor with persistent SQLite logs."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import csv
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sqlite3
import shutil
import threading
import time
import urllib.parse
import urllib.request
from positions import Positions
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
SHANGHAI = timezone(timedelta(hours=8))


def search_filters(params):
    symbol = params.get('symbol', '').upper().replace('/', '').strip()
    if not re.fullmatch(r'[A-Z0-9]{2,20}USDT', symbol):
        raise ValueError('请选择要查询的币种')
    stamps = []
    for key in ('start', 'end'):
        value = params.get(key, '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?', value):
            raise ValueError('请选择有效的开始和结束时间（北京时间）')
        stamps.append(int(datetime.fromisoformat(value).replace(tzinfo=SHANGHAI).timestamp()*1000))
    if stamps[0] >= stamps[1]:
        raise ValueError('结束时间必须晚于开始时间')
    kind = params.get('kind', 'trades')
    if kind not in ('trades', 'large'):
        raise ValueError('成交类型不正确')
    page = int(params.get('page', '1'))
    cutoff = int(params['cutoff']) if 'cutoff' in params else None
    if not 1 <= page <= 10000000 or (cutoff is not None and cutoff < 0):
        raise ValueError('分页参数不正确')
    return dict(symbol=symbol, start=stamps[0], end=stamps[1], large=kind=='large', page=page, cutoff=cutoff)


def validate_config(data):
    raw = data.get('symbols', data.get('symbol', ['XRPUSDT', 'BTCUSDT', 'ETHUSDT']))
    if isinstance(raw, str):
        raw = re.split(r'[,，;；\s]+', raw.strip())
    if not isinstance(raw, list):
        raise ValueError('币种应为列表，或用逗号分隔')
    symbols = []
    for item in raw:
        symbol = str(item).upper().replace('/', '').strip()
        if not re.fullmatch(r'[A-Z0-9]{2,20}USDT', symbol):
            raise ValueError('请输入 USDT 交易对，例如 XRPUSDT, BTCUSDT')
        if symbol not in symbols:
            symbols.append(symbol)
    threshold = float(data.get('large_trade_usdt', 20000))
    if not 1 <= len(symbols) <= 20:
        raise ValueError('支持同时监控 1–20 个币种')
    if not math.isfinite(threshold) or not 1 <= threshold <= 1e12:
        raise ValueError('大额门槛应为 1 到 1 万亿 USDT')
    return dict(symbols=symbols, large_trade_usdt=threshold)


def parse_trade(event, threshold, market='spot'):
    expected = 'aggTrade' if market == 'futures' else 'trade'
    if event.get('e') != expected or type(event.get('m')) is not bool:
        raise ValueError('无效成交事件')
    if market == 'futures' and event.get('st', 1) != 1:
        raise ValueError('仅支持 USDT 本位合约')
    symbol = event['s']
    if not re.fullmatch(r'[A-Z0-9]{2,20}USDT', symbol):
        raise ValueError('无效交易对')
    price, quantity = Decimal(event['p']), Decimal(event['q'])
    if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
        raise ValueError('无效价格或数量')
    amount = price * quantity
    trade_id, timestamp = event['a' if market == 'futures' else 't'], event['T']
    if type(trade_id) is not int or type(timestamp) is not int or min(trade_id, timestamp) < 0:
        raise ValueError('无效成交编号或时间')
    return dict(symbol=symbol, trade_id=trade_id, time=timestamp,
                price=str(price), quantity=str(quantity), amount=str(amount),
                side='sell' if event['m'] else 'buy', large=int(amount >= Decimal(str(threshold))),
                threshold=str(threshold), received_at=int(time.time()*1000))


class MarketMonitor:
    def __init__(self, base=None, directory=ROOT, market='spot', config=None, persist=True):
        self.directory = Path(directory)
        self.market = market
        self.persist = persist
        self.logs = self.directory / 'logs'
        if market == 'futures':
            self.logs = self.logs / 'futures'
        self.logs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.changed = threading.Event()
        self.revision = 0
        self.status, self.error = 'connecting', None
        self.last_received = {}
        self.thread = None
        self.export_thread = None
        self.log_error = None
        self.logger = logging.getLogger('monitor.' + str(id(self)))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = RotatingFileHandler(self.logs / 'service.log', maxBytes=5_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        self.logger.addHandler(handler)
        path = self.directory / 'config.json'
        self.config = validate_config(config if config is not None else (json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}))
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS trades (
                seq INTEGER PRIMARY KEY, symbol TEXT NOT NULL, trade_id INTEGER NOT NULL,
                time INTEGER NOT NULL, price TEXT NOT NULL, quantity TEXT NOT NULL,
                amount TEXT NOT NULL, side TEXT NOT NULL, large INTEGER NOT NULL,
                threshold TEXT NOT NULL, received_at INTEGER NOT NULL,
                UNIQUE(symbol,trade_id))''')
            db.execute('CREATE INDEX IF NOT EXISTS trade_symbol_seq ON trades(symbol,seq DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS trade_symbol_time ON trades(symbol,time)')
            db.execute('CREATE INDEX IF NOT EXISTS large_symbol_seq ON trades(symbol,seq DESC) WHERE large=1')
            db.execute('CREATE VIEW IF NOT EXISTS large_trades AS SELECT * FROM trades WHERE large=1')

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.logs / 'trades.sqlite3'), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def update_config(self, data):
        config = validate_config(data)
        with self.lock:
            if self.persist:
                temp = self.directory / 'config.json.tmp'
                temp.write_text(json.dumps(config, indent=2), encoding='utf-8')
                os.replace(str(temp), str(self.directory / 'config.json'))
            self.config = config
            self.revision += 1
            self.status, self.error = ('stopped' if self.stop_event.is_set() else 'connecting'), None
            self.changed.set()
        self.logger.info('Configuration changed: %s', json.dumps(config))

    def save_batch(self, trades):
        if not trades:
            return
        with self.db() as db:
            db.executemany('''INSERT OR IGNORE INTO trades
                (symbol,trade_id,time,price,quantity,amount,side,large,threshold,received_at)
                VALUES(:symbol,:trade_id,:time,:price,:quantity,:amount,:side,:large,:threshold,:received_at)''', trades)

    def snapshot(self):
        with self.lock:
            config = dict(self.config)
            result = dict(service='binance-local-monitor', config=config, status=self.status, error=self.error,
                          last_received=dict(self.last_received), log_path=str(self.logs), log_error=self.log_error)
        trades, large = [], []
        with self.db() as db:
            for symbol in config['symbols']:
                trades.extend(dict(r) for r in db.execute('SELECT * FROM trades WHERE symbol=? ORDER BY seq DESC LIMIT 100', (symbol,)))
                large.extend(dict(r) for r in db.execute('SELECT * FROM trades WHERE symbol=? AND large=1 ORDER BY seq DESC LIMIT 100', (symbol,)))
            result['cursor'] = db.execute('SELECT COALESCE(MAX(seq),0) FROM trades').fetchone()[0]
        result.update(trades=sorted(trades, key=lambda r:r['seq'], reverse=True),
                      large_trades=sorted(large, key=lambda r:r['seq'], reverse=True))
        result['market'] = self.market
        for row in result['trades'] + result['large_trades']:
            row['market'] = self.market
        return result

    def export_rows(self, large=False, symbol=None):
        with self.db() as db:
            conditions, params = [], []
            if large:
                conditions.append('large=1')
            if symbol:
                conditions.append('symbol=?')
                params.append(symbol)
            query = 'SELECT * FROM trades ' + ('WHERE '+' AND '.join(conditions) if conditions else '') + ' ORDER BY seq'
            for row in db.execute(query, params):
                yield dict(row, market=self.market)

    def search_rows(self, filters, export=False):
        with self.db() as db:
            db.execute('BEGIN')
            cutoff = filters['cutoff']
            if cutoff is None:
                cutoff = db.execute('SELECT COALESCE(MAX(seq),0) FROM trades').fetchone()[0]
            where = 'symbol=? AND time>=? AND time<? AND seq<=?' + (' AND large=1' if filters['large'] else '')
            args = (filters['symbol'], filters['start'], filters['end'], cutoff)
            if export:
                for row in db.execute('SELECT * FROM trades WHERE '+where+' ORDER BY time DESC,seq DESC', args):
                    yield dict(row, market=self.market)
            else:
                total = db.execute('SELECT COUNT(*) FROM trades WHERE '+where, args).fetchone()[0]
                rows = [dict(r, market=self.market) for r in db.execute('SELECT * FROM trades WHERE '+where+' ORDER BY time DESC,seq DESC LIMIT 100 OFFSET ?', args+((filters['page']-1)*100,))]
                yield dict(rows=rows, total=total, page=filters['page'], pages=max(1,(total+99)//100), cutoff=cutoff)

    def history_symbols(self):
        with self.db() as db:
            return sorted(set(self.config['symbols']) | {r[0] for r in db.execute('SELECT DISTINCT symbol FROM trades')})

    def sync_split_logs(self, after=None):
        """Split by trade time in UTC+8, including late arrivals and startup recovery."""
        with self.db() as db:
            db.execute('BEGIN')
            cutoff = db.execute('SELECT COALESCE(MAX(seq),0) FROM trades').fetchone()[0]
            buckets = db.execute('''SELECT DISTINCT symbol, CAST((time+28800000)/3600000 AS INTEGER)
                FROM trades WHERE seq>? AND seq<=?''', (after or 0, cutoff)).fetchall()
            for symbol, bucket in buckets:
                start = bucket * 3600000 - 28800000
                local_time = datetime.fromtimestamp(start/1000, SHANGHAI)
                directory = self.logs / symbol / local_time.strftime('%Y-%m-%d')
                directory.mkdir(parents=True, exist_ok=True)
                hour = local_time.strftime('%H')
                for large, name in [(False, 'trades_'+hour+'.csv'), (True, 'large_trades_'+hour+'.csv')]:
                    path = directory / name
                    rebuild = after is None or not path.exists()
                    target = path.with_suffix('.csv.tmp') if rebuild else path
                    with target.open('w' if rebuild else 'a', encoding='utf-8-sig' if rebuild else 'utf-8', newline='') as file:
                        writer = csv.writer(file)
                        if rebuild:
                            writer.writerow(['seq','symbol','trade_id','time','price','quantity','amount','side','large','threshold','received_at','market'])
                        query = 'SELECT * FROM trades WHERE symbol=? AND time>=? AND time<? AND seq>? AND seq<=?' + (' AND large=1' if large else '') + ' ORDER BY seq'
                        for row in db.execute(query, (symbol, start, start+3600000, 0 if rebuild else after, cutoff)):
                            writer.writerow(list(row)+[self.market])
                    if rebuild:
                        os.replace(target, path)
        if after is None:
            # Keep former unbounded files as backups after successful hourly conversion.
            for symbol in {symbol for symbol, _ in buckets}:
                for name in ('trades.csv', 'large_trades.csv'):
                    old = self.logs / symbol / name
                    if old.exists():
                        archived = self.logs / 'legacy' / symbol / (str(time.time_ns())+'_'+name)
                        archived.parent.mkdir(parents=True, exist_ok=True)
                        root = self.logs.resolve()
                        if root not in old.resolve().parents or root not in archived.resolve().parents:
                            raise ValueError('Invalid log archive path')
                        old.replace(archived)
        return cutoff

    def run_log_export(self):
        cursor = None
        while not self.stop_event.is_set():
            try:
                cursor = self.sync_split_logs(cursor)
                self.log_error = None
            except Exception as exc:
                self.log_error = '分币种 CSV 写入失败：'+str(exc)
                self.logger.error('%s', self.log_error)
                cursor = None  # Recover partial writes by rebuilding atomically.
            self.stop_event.wait(1)

    def proxy_options(self):
        proxy = urllib.request.getproxies().get('https') or urllib.request.getproxies().get('http')
        if not proxy:
            return {}
        parsed = urllib.parse.urlsplit(proxy)
        if parsed.scheme not in ('http', 'https'):
            raise ValueError('后台支持 HTTP CONNECT 代理，请使用 http://host:port')
        result = dict(http_proxy_host=parsed.hostname, http_proxy_port=parsed.port or 80, proxy_type='http')
        if parsed.username:
            result['http_proxy_auth'] = (urllib.parse.unquote(parsed.username), urllib.parse.unquote(parsed.password or ''))
        return result

    def run(self):
        import websocket
        failures = 0
        while not self.stop_event.is_set():
            self.changed.clear()
            with self.lock:
                config, revision = dict(self.config), self.revision
                self.status = 'connecting'
            ws, batch = None, []
            try:
                suffix = '@aggTrade' if self.market == 'futures' else '@trade'
                endpoint = 'wss://fstream.binance.com/market/stream?streams=' if self.market == 'futures' else 'wss://data-stream.binance.vision:443/stream?streams='
                streams = '/'.join(s.lower()+suffix for s in config['symbols'])
                ws = websocket.create_connection(endpoint+streams,
                                                  timeout=12, **self.proxy_options())
                ws.settimeout(1)
                with self.lock:
                    if revision == self.revision:
                        self.status, self.error = 'live', None
                self.logger.info('Connected: %s; disconnected periods may contain missing trades', ','.join(config['symbols']))
                flushed = pinged = received = time.monotonic()
                while not self.stop_event.is_set() and revision == self.revision:
                    try:
                        opcode, raw = ws.recv_data(control_frame=True)
                        received = time.monotonic()
                        if opcode == websocket.ABNF.OPCODE_CLOSE:
                            raise ConnectionError('币安连接已关闭')
                        if opcode == websocket.ABNF.OPCODE_TEXT:
                            event = json.loads(raw).get('data', {})
                            trade = parse_trade(event, config['large_trade_usdt'], self.market)
                            if trade['symbol'] in config['symbols']:
                                batch.append(trade)
                                with self.lock:
                                    self.last_received[trade['symbol']] = trade['received_at']
                                failures = 0
                    except websocket.WebSocketTimeoutException:
                        pass
                    now = time.monotonic()
                    if batch and (now-flushed >= .25 or len(batch) >= 2000):
                        self.save_batch(batch)
                        batch.clear()
                        flushed = now
                    if now-pinged > 15:
                        ws.ping()
                        pinged = now
                    if now-received > 45:
                        raise TimeoutError('45 秒未收到成交或连接心跳')
            except Exception as exc:
                failures += 1
                self.logger.error('Collector failure: %s', exc)
                with self.lock:
                    if revision == self.revision:
                        self.status, self.error = 'error', str(exc) + '；自动重连，断线期间可能遗漏成交。'
            finally:
                try:
                    self.save_batch(batch)
                except Exception as exc:
                    self.logger.critical('Log write failed, collector stopped: %s', exc)
                    with self.lock:
                        self.status, self.error = 'error', '日志写入失败，采集已停止：'+str(exc)
                    self.stop_event.set()
                if ws:
                    ws.close(timeout=1)
            if not self.stop_event.is_set() and revision == self.revision:
                self.changed.wait(min(30, 2**min(failures, 5)))

    def start(self):
        import websocket
        with self.lifecycle_lock:
            if self.thread and self.thread.is_alive():
                return
            if self.export_thread and self.export_thread.is_alive():
                self.stop_event.set()
                self.export_thread.join()
            self.stop_event.clear()
            self.changed.clear()
            self.status, self.error = 'connecting', None
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.export_thread = threading.Thread(target=self.run_log_export, daemon=True)
            self.export_thread.start()

    def pause(self):
        with self.lifecycle_lock:
            self.status = 'stopping'
            self.stop_event.set()
            self.changed.set()
            if self.thread:
                self.thread.join()
            if self.export_thread:
                self.export_thread.join()
                self.sync_split_logs()
            self.thread = self.export_thread = None
            self.status, self.error = 'stopped', None
            self.logger.info('Monitoring stopped; pending logs flushed')

    def close(self):
        self.pause()
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)


def validate_monitor_config(data):
    mode = data.get('mode', 'spot')
    if mode not in ('spot', 'futures', 'both'):
        raise ValueError('请选择现货、合约或同时监控')
    spot = validate_config(dict(symbols=data.get('symbols') or ['XRPUSDT'], large_trade_usdt=data.get('large_trade_usdt',20000)))
    future = validate_config(dict(symbols=data.get('futures_symbols') or spot['symbols'], large_trade_usdt=data.get('futures_large_trade_usdt',spot['large_trade_usdt'])))
    if mode in ('spot','both') and not data.get('symbols', ['XRPUSDT']):
        raise ValueError('请填写现货币种')
    if mode in ('futures','both') and 'futures_symbols' in data and not data['futures_symbols']:
        raise ValueError('请填写合约币种')
    return dict(mode=mode, symbols=spot['symbols'], futures_symbols=future['symbols'], large_trade_usdt=spot['large_trade_usdt'], futures_large_trade_usdt=future['large_trade_usdt'])


class Monitor:
    """Coordinate independent collectors and stores without migrating existing spot data."""
    def __init__(self, directory=ROOT):
        self.directory = Path(directory)
        path = self.directory/'config.json'
        self.config = validate_monitor_config(json.loads(path.read_text(encoding='utf-8')) if path.exists() else {})
        self.lock = threading.RLock()
        self.storage_lock = threading.Lock()
        self.storage_cache = None
        self.storage_cached_at = 0
        self.positions = Positions(directory)
        self.running = False
        self.markets = {name: MarketMonitor(directory=directory, market=name, config=self.child_config(name), persist=False)
                        for name in ('spot','futures')}
        for child in self.markets.values():
            child.status = 'stopped'
            child.stop_event.set()
        self.positions.watch(lambda: (list(self.config['futures_symbols']), self.running and self.config['mode'] in ('both','futures')))

    def child_config(self, name):
        return dict(symbols=self.config['futures_symbols' if name=='futures' else 'symbols'], large_trade_usdt=self.config['futures_large_trade_usdt' if name=='futures' else 'large_trade_usdt'])

    def storage_usage(self, refresh=False):
        with self.storage_lock:
            if not refresh and self.storage_cache and time.monotonic()-self.storage_cached_at < 15:
                return self.storage_cache
            root = self.directory/'logs'
            groups = {k:dict(bytes=0,files=0) for k in ('spot','futures','legacy','runtime')}
            kinds = {k:0 for k in ('csv','database','service','other')}
            errors = []
            def count(path,group):
                try:
                    if path.is_symlink():
                        errors.append(str(path)+': skipped link')
                        return
                    size = path.stat().st_size
                    groups[group]['bytes'] += size
                    groups[group]['files'] += 1
                    name = path.name.lower()
                    kind = 'csv' if name.endswith('.csv') else 'database' if any(x in name for x in ('.sqlite3','.db')) else 'service' if '.log' in name else 'other'
                    kinds[kind] += size
                except OSError as exc:
                    errors.append(str(exc))
            for directory,dirs,files in os.walk(root,followlinks=False,onerror=lambda e:errors.append(str(e))):
                dirs[:] = [d for d in dirs if not (Path(directory)/d).is_symlink()]
                for name in files:
                    path = Path(directory)/name
                    parts = path.relative_to(root).parts
                    group = 'legacy' if 'legacy' in parts[:-1] else 'futures' if parts[0]=='futures' else 'runtime' if len(parts)==1 and '.log' in name else 'spot'
                    count(path,group)
            for name in ('monitor.log','monitor-error.log','monitor.db','monitor.db-wal','monitor.db-shm','monitor.db-journal'):
                path = self.directory/name
                if path.exists():
                    count(path,'runtime')
            disk = shutil.disk_usage(root)
            free_pct = disk.free/disk.total*100 if disk.total else 0
            self.storage_cache = dict(total_bytes=sum(v['bytes'] for v in groups.values()),
                file_count=sum(v['files'] for v in groups.values()),groups=groups,kinds=kinds,
                disk_total_bytes=disk.total,disk_used_bytes=disk.used,disk_free_bytes=disk.free,
                disk_free_percent=free_pct,low_space=disk.free<5*1024**3 or free_pct<10,
                path=str(root),updated_at=int(time.time()*1000),partial=bool(errors),error_count=len(errors))
            self.storage_cached_at = time.monotonic()
            return self.storage_cache

    def enabled(self):
        return ('spot','futures') if self.config['mode']=='both' else (self.config['mode'],)

    def for_market(self, market):
        if market not in self.markets:
            raise ValueError('无效市场类型')
        return self.markets[market]

    def update_config(self, data):
        config = validate_monitor_config(data)
        with self.lock:
            temp = self.directory/'config.json.tmp'
            temp.write_text(json.dumps(config,indent=2),encoding='utf-8')
            os.replace(temp,self.directory/'config.json')
            self.config = config
            for name, child in self.markets.items():
                if child.config != self.child_config(name):
                    child.update_config(self.child_config(name))
                if self.running and name in self.enabled():
                    child.start()
                elif child.thread:
                    child.pause()

    def start(self):
        with self.lock:
            self.running = True
            for name in self.enabled():
                self.markets[name].start()

    def pause(self):
        with self.lock:
            self.running = False
            for child in self.markets.values():
                child.pause()

    def close(self):
        with self.lock:
            self.running = False
            for child in self.markets.values():
                child.close()
        self.positions.close()

    def snapshot(self):
        # Reads must remain available while control operations wait for collectors to flush.
        config = dict(self.config)
        enabled = ('spot','futures') if config['mode']=='both' else (config['mode'],)
        markets = {name: child.snapshot() for name,child in self.markets.items()}
        states = [markets[name]['status'] for name in enabled]
        status = 'stopped' if not self.running else ('live' if all(s=='live' for s in states) else 'partial' if 'live' in states else 'error' if 'error' in states else 'connecting')
        return dict(service='binance-local-monitor',config=config,status=status,markets=markets,
                    error='；'.join(name+': '+markets[name]['error'] for name in enabled if markets[name]['error']),
                    log_error='；'.join(name+': '+value['log_error'] for name,value in markets.items() if value['log_error']),
                    log_path=str(self.directory/'logs'))


def handler_for(monitor):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, content, mime='application/json; charset=utf-8'):
            raw = content.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path in ('/api/positions/search', '/api/positions/search.csv', '/api/positions/symbols'):
                try:
                    store = monitor.positions.store
                    if parsed.path.endswith('/symbols'):
                        self.respond(200,json.dumps(sorted(set(store.symbols()+monitor.config['futures_symbols']))))
                        return
                    params = {k:v[0] for k,v in urllib.parse.parse_qs(parsed.query,keep_blank_values=True).items()}
                    filters = search_filters(params)
                    metric = params.get('metric','all')
                    if metric not in ('all','open_interest','top_positions','global_accounts'):
                        raise ValueError('无效持仓指标')
                    if parsed.path.endswith('.csv'):
                        from position_store import FIELDS
                        self.send_response(200)
                        self.send_header('Content-Type','text/csv; charset=utf-8')
                        self.send_header('Content-Disposition','attachment; filename="positions-'+filters['symbol']+'.csv"')
                        self.send_header('Connection','close')
                        self.end_headers()
                        self.close_connection = True
                        buffer = io.StringIO()
                        writer = csv.writer(buffer)
                        self.wfile.write(b'\xef\xbb\xbf')
                        writer.writerow(FIELDS)
                        self.wfile.write(buffer.getvalue().encode('utf-8'))
                        for row in store.search(filters,metric,export=True):
                            buffer.seek(0)
                            buffer.truncate(0)
                            writer.writerow([row[k] for k in FIELDS])
                            self.wfile.write(buffer.getvalue().encode('utf-8'))
                    else:
                        self.respond(200,json.dumps(next(store.search(filters,metric))))
                except (ValueError,OverflowError) as exc:
                    self.respond(400,json.dumps({'error':str(exc)}))
                except (BrokenPipeError,ConnectionResetError):
                    pass
                return
            if parsed.path == '/api/positions' and hasattr(monitor, 'positions'):
                config = dict(monitor.config)
                enabled = config['mode'] in ('both', 'futures')
                self.respond(200, json.dumps(monitor.positions.snapshot(
                    config['futures_symbols'] if enabled else [], monitor.running and enabled)))
                return
            if parsed.path == '/api/storage' and hasattr(monitor,'storage_usage'):
                try:
                    self.respond(200,json.dumps(monitor.storage_usage(refresh=urllib.parse.parse_qs(parsed.query).get('refresh')==['1'])))
                except OSError as exc:
                    self.respond(500,json.dumps({'error':'无法读取磁盘空间：'+str(exc)}))
                return
            if parsed.path in ('/api/search', '/api/search.csv'):
                try:
                    params = {k:v[0] for k,v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
                    market = params.get('market','spot')
                    selected = monitor.for_market(market) if hasattr(monitor,'for_market') else monitor
                    filters = search_filters(params)
                    if parsed.path == '/api/search.csv':
                        self.export_csv(filters['large'], filters['symbol'], selected.search_rows(filters, export=True), market)
                    else:
                        self.respond(200, json.dumps(next(selected.search_rows(filters))))
                except (ValueError, OverflowError, OSError) as exc:
                    self.respond(400, json.dumps({'error': str(exc)}))
                return
            if parsed.path == '/api/symbols':
                market = urllib.parse.parse_qs(parsed.query).get('market',['spot'])[0]
                if market not in ('spot','futures'):
                    self.respond(400,'{"error":"Invalid market"}')
                    return
                selected = monitor.for_market(market) if hasattr(monitor,'for_market') else monitor
                self.respond(200, json.dumps(selected.history_symbols()))
                return
            if self.path == '/api/state':
                self.respond(200, json.dumps(monitor.snapshot()))
            elif re.fullmatch(r'/api/export/(spot|futures)/[A-Z0-9]{2,20}USDT/(trades|large)\.csv', self.path):
                parts = self.path.split('/')
                self.export_csv(self.path.endswith('/large.csv'), parts[4], market=parts[3])
            elif re.fullmatch(r'/api/export/[A-Z0-9]{2,20}USDT/(trades|large)\.csv', self.path):
                self.export_csv(self.path.endswith('/large.csv'), self.path.split('/')[3])
            elif self.path == '/app.js':
                self.respond(200, (ROOT / 'app.js').read_text(encoding='utf-8'), 'text/javascript; charset=utf-8')
            elif self.path in ('/', '/index.html'):
                self.respond(200, (ROOT / 'index.html').read_text(encoding='utf-8'), 'text/html; charset=utf-8')
            else:
                self.respond(404, '{}')

        def export_csv(self, large, symbol, rows=None, market='spot'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="'+market+'-'+symbol+'-'+('large-trades' if large else 'all-trades')+'.csv"')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            fields = ['market','seq','symbol','trade_id','time','time_shanghai','side','price','quantity','amount','large','threshold','received_at']
            self.wfile.write(b'\xef\xbb\xbf')
            writer.writerow(fields)
            try:
                self.wfile.write(buffer.getvalue().encode('utf-8'))
                selected = monitor.for_market(market) if hasattr(monitor,'for_market') else monitor
                for row in (rows if rows is not None else selected.export_rows(large, symbol)):
                    buffer.seek(0)
                    buffer.truncate(0)
                    row['time_shanghai'] = datetime.fromtimestamp(row['time']/1000, SHANGHAI).isoformat(timespec='milliseconds')
                    writer.writerow([row[key] for key in fields])
                    self.wfile.write(buffer.getvalue().encode('utf-8'))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if self.path not in ('/api/config', '/api/control', '/api/shutdown'):
                self.respond(404, '{}')
                return
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                self.respond(403, '{}')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096 or not self.headers.get('Content-Type', '').startswith('application/json'):
                    raise ValueError('请求格式错误')
                data = json.loads(self.rfile.read(length))
                if self.path == '/api/config':
                    monitor.update_config(data)
                elif self.path == '/api/control':
                    action = data.get('action')
                    if action not in ('start', 'stop'):
                        raise ValueError('无效控制操作')
                    (monitor.start if action == 'start' else monitor.pause)()
                else:
                    # Shutdown runs off the request thread so HTTP shutdown cannot deadlock.
                    def shutdown():
                        monitor.pause()
                        self.server.shutdown()
                    threading.Thread(target=shutdown, daemon=True).start()
                self.respond(200, '{"ok":true}')
            except (ValueError, TypeError, AttributeError) as exc:
                self.respond(400, json.dumps({'error': str(exc)}))

        def log_message(self, *_):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    monitor = Monitor()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(monitor))
    monitor.start()
    print('Monitor: http://127.0.0.1:%s  (Ctrl+C to stop)' % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        monitor.close()


if __name__ == '__main__':
    main()
