import csv
from datetime import datetime
import tempfile
import threading
import unittest
from unittest.mock import patch
from position_store import PositionStore, ZONE
from positions import Positions


class PositionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PositionStore(self.temp.name)
        self.start = int(datetime(2026,11,8,23,59,59,tzinfo=ZONE).timestamp()*1000)

    def tearDown(self):
        self.temp.cleanup()

    def filters(self, **kwargs):
        return dict(symbol='XRPUSDT',start=self.start,end=self.start+200000,page=1,cutoff=None,**kwargs)

    def test_hour_day_split_dedup_and_recovery(self):
        for stamp in [self.start,self.start+1000,self.start]:
            self.store.save('XRPUSDT','open_interest',dict(time=stamp,value=123,fetched_at=stamp))
        files = sorted(self.store.root.rglob('*.csv'))
        self.assertEqual(len(files),2)
        self.assertEqual(files[0].name,'positions_23.csv')
        self.assertEqual(files[1].parent.name,'2026-11-09')
        for file in files:
            with file.open(encoding='utf-8-sig') as source:
                self.assertEqual(len(list(csv.DictReader(source))),1)
        with patch('position_store.os.replace',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.store.save('XRPUSDT','top_positions',dict(time=self.start,long=.6,short=.4,ratio=1.5))
        recovered = PositionStore(self.temp.name)
        self.assertEqual(next(recovered.search(self.filters()))['total'],3)
        with files[0].open(encoding='utf-8-sig') as source:
            self.assertEqual(len(list(csv.DictReader(source))),2)

    def test_boundaries_paging_export_and_metric_filter(self):
        for i in range(105):
            self.store.save('XRPUSDT','open_interest',dict(time=self.start+i*1000,value=i))
        f=self.filters()
        result=next(self.store.search(f))
        self.assertEqual((result['total'],len(result['rows']),result['pages']),(105,100,2))
        f.update(cutoff=result['cutoff'],page=2)
        self.store.save('XRPUSDT','open_interest',dict(time=self.start+105000,value=105))
        self.assertEqual(len(next(self.store.search(f))['rows']),5)
        self.assertEqual(len(list(self.store.search(f,export=True))),105)
        self.assertEqual(next(self.store.search(f,'top_positions'))['total'],0)
        f.update(start=self.start,end=self.start+1000)
        self.assertEqual(next(self.store.search(f))['total'],1)
        self.assertEqual(self.store.symbols(),['XRPUSDT'])

    def test_background_collection_without_page(self):
        p=Positions(self.temp.name)
        saved=threading.Event()
        def response(path,params):
            if 'openInterest' in path:
                return dict(symbol='XRPUSDT',openInterest='10',time=self.start)
            return [dict(symbol='XRPUSDT',longAccount='.6',shortAccount='.4',longShortRatio='1.5',timestamp=self.start)]
        original=p.store.save
        def save(*args):
            original(*args)
            if args[1]=='global_accounts':saved.set()
        with patch('positions.fetch_json',side_effect=response),patch.object(p.store,'save',side_effect=save):
            try:
                p.watch(lambda:(['XRPUSDT'],True))
                self.assertTrue(saved.wait(5))
            finally:
                p.close()
        self.assertEqual(next(self.store.search(self.filters()))['total'],3)
