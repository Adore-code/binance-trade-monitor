import unittest
from unittest.mock import patch
from positions import Positions


class PositionTests(unittest.TestCase):
    def setUp(self):
        self.p = Positions()
        self.p.snapshot(['XRPUSDT'], False)

    def tearDown(self):
        self.p.close()

    def response(self, path, params):
        self.assertEqual(params['symbol'], 'XRPUSDT')
        if 'openInterest' in path:
            return dict(symbol='XRPUSDT', openInterest='1234.567', time=1700000000000)
        self.assertEqual(params['period'], '5m')
        long = '0.6' if 'topLong' in path else '0.4'
        return [dict(symbol='XRPUSDT', longAccount=long, shortAccount=str(1-float(long)),
                     longShortRatio='1.5', timestamp=1700000000000)]

    def test_distinct_metrics_and_partial_failure_retains_previous(self):
        with patch('positions.fetch_json', side_effect=self.response):
            self.p.refresh('XRPUSDT')
        row = self.p.snapshot(['XRPUSDT'], False)['rows'][0]
        self.assertEqual(row['metrics']['open_interest']['value'], 1234.567)
        self.assertEqual(row['metrics']['top_positions']['long'], .6)
        self.assertEqual(row['metrics']['global_accounts']['long'], .4)
        def failed(path, params):
            if 'topLong' in path:
                return []
            return self.response(path, params)
        with patch('positions.fetch_json', side_effect=failed):
            self.p.refresh('XRPUSDT')
        metrics = self.p.snapshot(['XRPUSDT'], False)['rows'][0]['metrics']
        self.assertTrue(metrics['top_positions']['error'])
        self.assertEqual(metrics['top_positions']['long'], .6)
        self.assertFalse(metrics['global_accounts']['error'])

    def test_nonblocking_cache_dedup_and_disabled(self):
        with patch.object(self.p.pool, 'submit') as submit:
            self.p.snapshot(['XRPUSDT'], False)
            submit.assert_not_called()
            self.p.snapshot(['XRPUSDT'], True)
            self.p.snapshot(['XRPUSDT'], True)
            self.assertEqual(submit.call_count, 1)
            with patch('positions.fetch_json', side_effect=self.response):
                self.p.refresh('XRPUSDT')
            self.p.snapshot(['XRPUSDT'], True)
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.p.snapshot([], False)['rows'], [])
        self.assertEqual(self.p.cache, {})

    def test_invalid_data_is_not_shown_as_zero(self):
        with patch('positions.fetch_json', return_value={'openInterest':'NaN'}):
            self.p.refresh('XRPUSDT')
        metrics = self.p.snapshot(['XRPUSDT'], False)['rows'][0]['metrics']
        for metric in metrics.values():
            self.assertTrue(metric['error'])
            self.assertNotIn('time', metric)
            self.assertNotIn('value', metric)
