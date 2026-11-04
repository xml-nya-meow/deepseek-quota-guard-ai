import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from quota_guard.browser_service import parse_cost_response, cost_query_matches
from quota_guard.billing import HEADERS, inspect_export
from quota_guard.core import GuardError
from quota_guard.instance import FolderLock

class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.start = int(datetime(2026,10,29,tzinfo=timezone(timedelta(hours=8))).timestamp())
        self.url = f'https://platform.deepseek.com/api/v0/usage/by_api_key/cost?start={self.start}&end={self.start+86400}&tz=28800'
        self.payload = {'code':0,'data':{'biz_code':0,'biz_data':{'start':self.start,'end':self.start+86400,'bucket':'1d','data':[{'currency':'CNY','series':[{'api_key':{'tracking_id':'fixture-key-001'},'model':'test-model','buckets':[{'time':self.start,'cost':'107.000001'}]}]}]}}}
    def parse(self, payload=None, url=None):
        return parse_cost_response(payload or self.payload,url or self.url,'2026-10-29','2026-10-29')
    def test_exact_money(self):
        self.assertEqual(self.parse(), {'fixture-key-001':Decimal('107.000001')})
    def test_cost_reply_must_match_selected_date_window(self):
        self.assertTrue(cost_query_matches(self.url, '2026-10-29', '2026-10-29'))
        self.assertFalse(cost_query_matches(self.url, '2026-10-28', '2026-10-29'))
        self.assertFalse(cost_query_matches(self.url + '&start=0', '2026-10-29', '2026-10-29'))
        self.assertFalse(cost_query_matches(self.url.replace('https:', 'http:'), '2026-10-29', '2026-10-29'))
    def test_unknown_empty_not_zero(self):
        self.payload['data']['biz_data']['data'] = []
        with self.assertRaises(GuardError): self.parse()
    def test_explicit_zero_series(self):
        self.payload['data']['biz_data']['data'][0]['series'][0]['buckets'][0]['cost']='0'
        self.assertEqual(self.parse()['fixture-key-001'],0)
    def test_wrong_timezone(self):
        with self.assertRaises(GuardError): self.parse(url=self.url.replace('28800','0'))
    def test_wrong_currency(self):
        self.payload['data']['biz_data']['data'][0]['currency']='USD'
        with self.assertRaises(GuardError): self.parse()
    def test_duplicate_buckets(self):
        series=self.payload['data']['biz_data']['data'][0]['series'][0]
        series['buckets']*=2
        with self.assertRaises(GuardError): self.parse()
    def test_bad_money(self):
        self.payload['data']['biz_data']['data'][0]['series'][0]['buckets'][0]['cost']='NaN'
        with self.assertRaises(GuardError): self.parse()
    def test_missing_period(self):
        self.payload['data']['biz_data']['start']+=86400
        with self.assertRaises(GuardError): self.parse()
    def test_export_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            for kind, header in HEADERS.items():
                (Path(folder)/f'{kind}-2026-09-30_2026-10-29.csv').write_text(','.join(header)+'\n',encoding='utf-8-sig')
            result=inspect_export(folder,'2026-10-29')
            self.assertEqual(result['status'],'empty')
            self.assertNotIn('totals',result)
    def test_export_invalid_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            for kind, header in HEADERS.items():
                (Path(folder)/f'{kind}-2026-10-29_2026-10-29.csv').write_text(','.join(header)+'\n',encoding='utf-8')
            with self.assertRaises(GuardError): inspect_export(folder,'2026-09-01')
    def test_single_instance_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            lock=FolderLock(folder)
            with self.assertRaises(GuardError): FolderLock(folder)
            lock.close()
            next_lock=FolderLock(folder); next_lock.close()

if __name__=='__main__': unittest.main()
