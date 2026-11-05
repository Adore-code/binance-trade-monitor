import csv
import io
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
from http.server import ThreadingHTTPServer
from datetime import datetime
from monitor import Monitor, handler_for, validate_config, parse_trade, SHANGHAI, search_filters


def event(symbol='XRPUSDT', trade_id=1, price='2.5', quantity='8000', maker=False):
    return dict(e='trade',s=symbol,t=trade_id,T=1793847760936,p=price,q=quantity,m=maker)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.m = Monitor(directory=self.temp.name)

    def tearDown(self):
        self.m.close()
        self.temp.cleanup()

    def test_config_normalization_and_legacy(self):
        self.assertEqual(validate_config({'symbols':'xrp/usdt, BTCUSDT，XRPUSDT'})['symbols'],['XRPUSDT','BTCUSDT'])
        self.assertEqual(validate_config({'symbol':'XRPUSDT'})['symbols'],['XRPUSDT'])
        for data in [{'symbols':[]},{'symbols':['BTCETH']},{'symbols':['../USDT']},{'large_trade_usdt':float('nan')}]:
            with self.assertRaises(ValueError): validate_config(data)

    def test_stop_flushes_pending_trade_and_start_is_idempotent(self):
        def collector():
            self.m.stop_event.wait()
            self.m.save_batch([parse_trade(event(),20000)])
        with patch.object(self.m,'run',side_effect=collector):
            self.m.start()
            first=self.m.thread
            self.m.start()
            self.assertIs(self.m.thread,first)
            self.m.pause()
            self.assertFalse(first.is_alive())
            self.assertEqual(self.m.snapshot()['status'],'stopped')
            self.assertEqual(len(list(self.m.export_rows())),1)
            self.assertTrue(list(self.m.logs.glob('XRPUSDT/*/trades_*.csv')))
            self.m.update_config({'symbols':['XRPUSDT']})
            self.assertEqual(self.m.status,'stopped')
            self.m.start()
            self.assertIsNot(self.m.thread,first)
            self.m.pause()

    def test_control_api_and_cross_origin_rejection(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.m))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:%s/api/control'%server.server_port
        try:
            req=urllib.request.Request(url,data=b'{"action":"stop"}',headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as response:self.assertEqual(response.status,200)
            self.assertEqual(self.m.status,'stopped')
            req.add_header('Origin','https://unrelated.example')
            with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(req)
            self.assertEqual(caught.exception.code,403)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_direction_and_exact_decimal_threshold(self):
        buy=parse_trade(event(price='0.1',quantity='3'),0.3)
        self.assertEqual(buy['amount'],'0.3')
        self.assertEqual(buy['large'],1)
        self.assertEqual(buy['side'],'buy')
        self.assertEqual(parse_trade(event(maker=True),20000)['side'],'sell')
        self.assertEqual(parse_trade(event(quantity='7999'),20000)['large'],0)

    def test_logs_deduplicate_by_symbol_and_survive_restart(self):
        batch=[parse_trade(event(),20000),parse_trade(event('BTCUSDT'),20000)]
        self.m.save_batch(batch*2)
        self.assertEqual(len(list(self.m.export_rows())),2)
        self.m.close()
        self.m=Monitor(directory=self.temp.name)
        self.m.save_batch(batch)
        self.assertEqual(len(list(self.m.export_rows())),2)
        self.assertEqual(len(self.m.snapshot()['large_trades']),2)

    def test_setting_changes_keep_history_and_original_threshold(self):
        self.m.save_batch([parse_trade(event(),20000)])
        self.m.update_config({'symbols':['BTCUSDT'],'large_trade_usdt':50000})
        self.assertEqual(self.m.snapshot()['trades'],[])
        rows=list(self.m.export_rows(True))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['threshold'],'20000')
        self.m.update_config({'symbols':['XRPUSDT'],'large_trade_usdt':50000})
        self.m.save_batch([parse_trade(event(trade_id=2),50000)])
        self.assertEqual(len(self.m.snapshot()['trades']),2)
        self.assertEqual(len(self.m.snapshot()['large_trades']),1)

    def test_recent_limit_does_not_truncate_logs(self):
        self.m.save_batch([parse_trade(event(trade_id=i),20000) for i in range(150)])
        self.assertEqual(len(self.m.snapshot()['trades']),100)
        self.assertEqual(len(list(self.m.export_rows())),150)

    def test_separate_symbol_logs_and_incremental_recovery(self):
        self.m.save_batch([parse_trade(event(),20000),parse_trade(event('BTCUSDT'),20000)])
        cursor=self.m.sync_split_logs()
        self.m.save_batch([parse_trade(event(trade_id=2,quantity='1'),20000)])
        cursor=self.m.sync_split_logs(cursor)
        self.m.sync_split_logs(cursor)
        self.m.sync_split_logs()  # Startup rebuild must not duplicate rows.
        date = datetime.fromtimestamp(event()['T']/1000, SHANGHAI)
        for symbol,count in [('XRPUSDT',2),('BTCUSDT',1)]:
            for filename,expected in [('trades',count),('large_trades',1)]:
                path=self.m.logs/symbol/date.strftime('%Y-%m-%d')/(filename+'_'+date.strftime('%H')+'.csv')
                with path.open(encoding='utf-8-sig',newline='') as file:
                    rows=list(csv.DictReader(file))
                self.assertEqual(len(rows),expected)
                self.assertEqual({r['symbol'] for r in rows},{symbol})
            self.assertTrue(all(row['symbol']==symbol for row in self.m.export_rows(symbol=symbol)))

    def test_hour_day_rollover_and_late_arrival(self):
        def at(trade_id, stamp):
            raw=event(trade_id=trade_id)
            raw['T']=int(datetime.fromisoformat(stamp).replace(tzinfo=SHANGHAI).timestamp()*1000)
            return parse_trade(raw,20000)
        self.m.save_batch([at(1,'2026-11-05T22:59:59.999'), at(2,'2026-11-05T23:00:00'), at(3,'2026-11-06T00:00:00')])
        cursor=self.m.sync_split_logs()
        self.m.save_batch([at(4,'2026-11-05T22:59:58')])
        self.m.sync_split_logs(cursor)
        self.m.sync_split_logs()
        for day,hour,ids in [('2026-11-05','22',{'1','4'}),('2026-11-05','23',{'2'}),('2026-11-06','00',{'3'})]:
            for kind in ['trades','large_trades']:
                with (self.m.logs/'XRPUSDT'/day/(kind+'_'+hour+'.csv')).open(encoding='utf-8-sig') as file:
                    rows=list(csv.DictReader(file))
                self.assertEqual({r['trade_id'] for r in rows},ids)
                self.assertEqual(len(rows),len(ids))

    def test_http_config_and_csv_exports(self):
        self.m.save_batch([parse_trade(event(),20000),parse_trade(event(trade_id=2,quantity='1'),20000)])
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.m))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:%s'%server.server_port
        try:
            req=urllib.request.Request(base+'/api/config',data=json.dumps({'symbols':['XRPUSDT','ETHUSDT'],'large_trade_usdt':30000}).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as r: self.assertEqual(r.status,200)
            with urllib.request.urlopen(base+'/api/state') as r: self.assertEqual(json.load(r)['config']['symbols'],['XRPUSDT','ETHUSDT'])
            for route,count in [('trades',2),('large',1)]:
                with urllib.request.urlopen(base+'/api/export/XRPUSDT/'+route+'.csv') as r:
                    rows=list(csv.DictReader(io.StringIO(r.read().decode('utf-8-sig'))))
                    self.assertEqual(len(rows),count)
                    self.assertIn('+08:00',rows[0]['time_shanghai'])
            query=urllib.parse.urlencode({'symbol':'XRPUSDT','start':'2026-11-01T00:00:00','end':'2026-12-01T00:00:00','kind':'large'})
            with urllib.request.urlopen(base+'/api/search?'+query) as r:
                result=json.load(r)
                self.assertEqual(result['total'],1)
            with urllib.request.urlopen(base+'/api/search.csv?'+query+'&cutoff='+str(result['cutoff'])) as r:
                rows=list(csv.DictReader(io.StringIO(r.read().decode('utf-8-sig'))))
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['trade_id'],'1')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base+'/api/search?symbol=XRPUSDT')
            self.assertEqual(caught.exception.code,400)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_search_boundaries_paging_and_snapshot(self):
        start=int(datetime(2026,11,5,23,59,tzinfo=SHANGHAI).timestamp()*1000)
        rows=[]
        for i in range(105):
            raw=event(trade_id=i)
            raw['T']=start+i*1000
            rows.append(parse_trade(raw,20000))
        self.m.save_batch(rows)
        filters=search_filters(dict(symbol='XRPUSDT',start='2026-11-05T23:59',end='2026-11-06T00:01'))
        first=next(self.m.search_rows(filters))
        self.assertEqual(first['total'],105)
        self.assertEqual(len(first['rows']),100)
        late=event(trade_id=999);late['T']=start
        self.m.save_batch([parse_trade(late,20000)])
        filters.update(cutoff=first['cutoff'],page=2)
        second=next(self.m.search_rows(filters))
        self.assertEqual(second['total'],105)
        self.assertEqual(len(second['rows']),5)
        self.assertFalse({r['seq'] for r in first['rows']}&{r['seq'] for r in second['rows']})
        self.assertEqual(len(list(self.m.search_rows(filters,export=True))),105)
        boundary=search_filters(dict(symbol='XRPUSDT',start='2026-11-05T23:59',end='2026-11-06T00:00'))
        boundary['cutoff']=first['cutoff']
        self.assertEqual(next(self.m.search_rows(boundary))['total'],60)
        for changes in [{'end':'2026-11-05T23:58'},{'start':'bad'},{'page':'0'},{'symbol':"XRPUSDT' OR 1=1"}]:
            with self.assertRaises(ValueError):
                search_filters(dict(symbol='XRPUSDT',start='2026-11-05T23:59',end='2026-11-06T00:01',**{})|changes)


if __name__=='__main__': unittest.main()
