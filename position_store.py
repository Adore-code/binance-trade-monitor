"""Durable position samples, with independently rebuilt hourly CSV files."""
import csv
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import sqlite3
import threading

ZONE = timezone(timedelta(hours=8))
FIELDS = ['seq', 'symbol', 'metric', 'time', 'time_shanghai', 'value', 'long', 'short', 'ratio', 'fetched_at']


class PositionStore:
    def __init__(self, directory):
        self.root = Path(directory)/'logs'/'futures'/'positions'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root/'positions.sqlite3'
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS samples (seq INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, metric TEXT, time INTEGER, time_shanghai TEXT, value REAL, long REAL, short REAL, ratio REAL, fetched_at INTEGER, UNIQUE(symbol, metric, time))')
            db.execute('CREATE INDEX IF NOT EXISTS samples_search ON samples(symbol,time,seq)')
            db.execute('CREATE TABLE IF NOT EXISTS dirty (symbol TEXT, hour INTEGER, PRIMARY KEY(symbol,hour))')
        self.recover()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, symbol, kind, metric):
        with self.lock:
            stamp = metric['time']
            hour = stamp//3600000*3600000
            values = dict(metric, symbol=symbol, metric=kind,
                          time_shanghai=datetime.fromtimestamp(stamp/1000, ZONE).isoformat(timespec='milliseconds'))
            with self.connect() as db:
                cursor = db.execute('INSERT OR IGNORE INTO samples ('+','.join(FIELDS[1:])+') VALUES ('+','.join('?' for _ in FIELDS[1:])+')', [values.get(k) for k in FIELDS[1:]])
                if cursor.rowcount:
                    db.execute('INSERT OR IGNORE INTO dirty VALUES (?,?)', (symbol,hour))
            self.recover()

    def recover(self):
        with self.connect() as db:
            for item in db.execute('SELECT * FROM dirty').fetchall():
                symbol,hour = item['symbol'],item['hour']
                date = datetime.fromtimestamp(hour/1000, ZONE)
                path = self.root/symbol/date.strftime('%Y-%m-%d')/date.strftime('positions_%H.csv')
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix('.tmp')
                with temp.open('w',encoding='utf-8-sig',newline='') as out:
                    writer = csv.writer(out)
                    writer.writerow(FIELDS)
                    writer.writerows(db.execute('SELECT '+','.join(FIELDS)+' FROM samples WHERE symbol=? AND time>=? AND time<? ORDER BY time,seq',(symbol,hour,hour+3600000)))
                os.replace(temp,path)
                db.execute('DELETE FROM dirty WHERE symbol=? AND hour=?',(symbol,hour))

    def symbols(self):
        with self.connect() as db:
            return [r[0] for r in db.execute('SELECT DISTINCT symbol FROM samples ORDER BY symbol')]

    def search(self, filters, metric='all', export=False):
        if metric not in ('all','open_interest','top_positions','global_accounts'):
            raise ValueError('无效持仓指标')
        with self.connect() as db:
            cutoff = filters['cutoff']
            if cutoff is None:
                cutoff = db.execute('SELECT COALESCE(MAX(seq),0) FROM samples').fetchone()[0]
            where = 'symbol=? AND time>=? AND time<? AND seq<=?'
            args = [filters['symbol'], filters['start'], filters['end'], cutoff]
            if metric != 'all':
                where += ' AND metric=?'
                args.append(metric)
            sql = 'SELECT '+','.join(FIELDS)+' FROM samples WHERE '+where+' ORDER BY time DESC,seq DESC'
            if export:
                for row in db.execute(sql,args):
                    yield dict(row)
            else:
                total = db.execute('SELECT COUNT(*) FROM samples WHERE '+where,args).fetchone()[0]
                pages = max(1,(total+99)//100)
                page = min(filters['page'],pages)
                rows = [dict(r) for r in db.execute(sql+' LIMIT 100 OFFSET ?',args+[(page-1)*100])]
                yield dict(rows=rows,total=total,page=page,pages=pages,cutoff=cutoff)
