import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from monitor import Monitor, parse_trade, search_filters, validate_monitor_config


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        Path(self.temp.name,'config.json').write_text(json.dumps({'symbols':['XRPUSDT'],'large_trade_usdt':20000}))
        self.m=Monitor(directory=self.temp.name)

    def tearDown(self):
        self.m.close()
        self.temp.cleanup()

    def test_config_legacy_and_custom_markets(self):
        self.assertEqual(self.m.config['mode'],'spot')
        self.assertEqual(self.m.config['symbols'],['XRPUSDT'])
        c=validate_monitor_config({'mode':'both','symbols':'XRP/USDT','futures_symbols':'btcusdt, ethusdt'})
        self.assertEqual(c['futures_symbols'],['BTCUSDT','ETHUSDT'])
        for c in [{'mode':'invalid'},{'mode':'both','futures_symbols':''},{'mode':'spot','symbols':[]},{'mode':'futures','futures_symbols':'BTCUSD_PERP'}]:
            with self.assertRaises(ValueError):validate_monitor_config(c)

    def test_independent_thresholds_and_legacy_default(self):
        self.assertEqual(self.m.config['futures_large_trade_usdt'],20000)
        self.m.update_config({'mode':'both','symbols':['XRPUSDT'],'futures_symbols':['XRPUSDT'],
                              'large_trade_usdt':10000,'futures_large_trade_usdt':50000})
        raw=dict(e='trade',s='XRPUSDT',t=1,p='2',q='10000',T=1793847760936,m=False)
        self.assertEqual(parse_trade(raw,self.m.child_config('spot')['large_trade_usdt'])['large'],1)
        self.assertEqual(parse_trade(dict(raw,e='aggTrade',a=1),self.m.child_config('futures')['large_trade_usdt'],'futures')['large'],0)
        saved=json.loads(Path(self.temp.name,'config.json').read_text())
        self.assertEqual(saved['futures_large_trade_usdt'],50000)
        for value in [0,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):validate_monitor_config({'futures_large_trade_usdt':value})

    def test_storage_totals_no_double_count_and_low_disk_warning(self):
        root=Path(self.temp.name)
        files={'logs/XRPUSDT/2026-11-08/trades_10.csv':101,
               'logs/futures/XRPUSDT/2026-11-08/trades_10.csv':202,
               'logs/legacy/XRPUSDT/old.csv':303,
               'logs/futures/legacy/old.csv':404,'monitor-error.log':505}
        for name,size in files.items():
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'x'*size)
        expected=sum(p.stat().st_size for p in (root/'logs').rglob('*') if p.is_file())+505
        with patch('monitor.shutil.disk_usage',return_value=SimpleNamespace(total=100*1024**3,used=98*1024**3,free=2*1024**3)):
            stats=self.m.storage_usage(refresh=True)
        self.assertEqual(stats['total_bytes'],expected)
        self.assertEqual(stats['total_bytes'],sum(g['bytes'] for g in stats['groups'].values()))
        self.assertEqual(stats['total_bytes'],sum(stats['kinds'].values()))
        self.assertEqual(stats['groups']['legacy']['bytes'],707)
        self.assertEqual(stats['kinds']['csv'],101+202+303+404)
        self.assertTrue(stats['low_space'])
        self.assertFalse(stats['partial'])
        (root/'monitor-error.log').write_bytes(b'xx')
        self.assertEqual(self.m.storage_usage()['total_bytes'],expected)
        self.assertEqual(self.m.storage_usage(refresh=True)['total_bytes'],expected-503)

    def test_modes_only_start_enabled_collectors(self):
        spot,future=self.m.markets.values()
        with patch.object(spot,'start') as s,patch.object(future,'start') as f:
            self.m.start();self.assertEqual(s.call_count,1);self.assertEqual(f.call_count,0)
            self.m.pause()
            self.m.update_config({'mode':'futures','symbols':['XRPUSDT'],'futures_symbols':['BTCUSDT']})
            self.assertEqual(f.call_count,0)
            self.m.start();self.assertEqual(f.call_count,1);self.assertEqual(s.call_count,1)
            self.m.update_config({'mode':'both','symbols':['XRPUSDT'],'futures_symbols':['BTCUSDT']})
            self.assertEqual(s.call_count,2);self.assertEqual(f.call_count,2)

    def test_same_symbol_and_id_are_independent_in_storage_search_and_logs(self):
        raw=dict(e='trade',s='XRPUSDT',t=42,p='2',q='10000',T=1793847760936,m=False)
        future=dict(raw,e='aggTrade',a=42,m=True,st=1)
        for market,event in [('spot',raw),('futures',future)]:
            child=self.m.markets[market]
            trade=parse_trade(event,20000,market)
            child.save_batch([trade,trade]);child.sync_split_logs()
            records=list(child.export_rows(symbol='XRPUSDT'))
            self.assertEqual(len(records),1);self.assertEqual(records[0]['market'],market)
            self.assertEqual(records[0]['side'],'sell' if market=='futures' else 'buy')
            filters=search_filters({'symbol':'XRPUSDT','start':'2026-11-01T00:00','end':'2026-12-01T00:00'})
            self.assertEqual(next(child.search_rows(filters))['rows'][0]['market'],market)
            files=list(child.logs.glob('XRPUSDT/*/trades_*.csv'))
            with files[0].open(encoding='utf-8-sig') as file:self.assertEqual(next(csv.DictReader(file))['market'],market)
        self.assertNotEqual(self.m.markets['spot'].logs,self.m.markets['futures'].logs)

    def test_futures_event_validation_and_partial_status(self):
        with self.assertRaises(ValueError):
            parse_trade(dict(e='trade',m=False),20000,'futures')
        with self.assertRaises(ValueError):
            parse_trade(dict(e='aggTrade',m=False,st=2),20000,'futures')
        self.m.config['mode']='both';self.m.running=True
        self.m.markets['spot'].status='live'
        self.m.markets['futures'].status='error'
        self.m.markets['futures'].error='test disconnect'
        self.assertEqual(self.m.snapshot()['status'],'partial')
