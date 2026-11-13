"""Offline key/browser bootstrap plus synthetic direct HTTP collection.

Browser traffic is routed locally. APIRequestContext is replaced with a fake
transport because page routes do not intercept APIRequestContext traffic.
"""
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from quota_guard.browser_service import BASE, COST_PATH, TZ, BrowserService, key_fingerprint
from quota_guard.platform_http import ConsoleCostReader
from test_key_table import TARGET, SURVIVOR, document, key_row
from test_platform_http import Response


START_DATE = '2026-10-29'
END_DATE = '2026-10-30'
START = int(datetime(2026, 10, 29, tzinfo=TZ).timestamp())
FINISH = int(datetime(2026, 10, 31, tzinfo=TZ).timestamp())
DEFAULT_START = FINISH - 30 * 86400


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 30, 12, tzinfo=TZ)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


def cost_payload(start, target_cost, survivor_cost):
    return {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
        'start': start, 'end': FINISH, 'bucket': 86400, 'data': [
            {'currency': 'CNY', 'series': [
                {'api_key': {'tracking_id': tracking}, 'model': 'synthetic-model',
                 'buckets': [{'time': START, 'cost': cost}]}
                for tracking, cost in ((TARGET, target_cost), (SURVIVOR, survivor_cost))
            ], 'function_series': []}
        ]
    }}}


def collecting_usage_document():
    # No calendar. The default page request establishes only this app's own
    # authentication; its body must not become ledger evidence.
    return ('<!doctype html><h1>Usage</h1><script>fetch(' + json.dumps(
        COST_PATH + f'?start={DEFAULT_START}&end={FINISH}&tz=28800') +
        ', {headers:{Authorization:"Bearer synthetic-session"}}).catch(()=>{});</script>')


def optional_notice_markup():
    # A real blocking overlay on each navigation exercises the adapter's hooks,
    # in addition to the focused notice tests. No account values are included.
    return '''<div class="ds-modal" aria-modal="true"
      style="position:fixed;inset:0;z-index:9999;background:#fff">
      <div class="ds-modal-content" role="dialog">
        <div class="ds-modal-content__title">绑定邮箱</div>
        <p>我们会通过该邮箱为你推送 DeepSeek API 的功能动态、商业信息和产品通知。</p>
        <input placeholder="请输入邮箱"><input placeholder="请输入验证码">
        <button>发送验证码</button><button>绑定</button>
        <button onclick="this.closest('.ds-modal').remove()">稍后再填</button>
      </div></div>'''


class CollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(__file__).resolve().parents[1] / 'browser-runtime'
        if not runtime.is_dir():
            runtime = Path(__file__).resolve().parents[2] / 'browser-runtime'
        if runtime.is_dir():
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(runtime)
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True, executable_path=cls.pw.chromium.executable_path)
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.keys = [{'name': 'Fixture target', 'tracking_id': TARGET},
                     {'name': 'Fixture survivor', 'tracking_id': SURVIVOR}]
        self.selected_payload = cost_payload(START, '42.000001', '0.000007')
        self.default_payload = cost_payload(DEFAULT_START, '999.99', '888.88')
        self.key_visits = self.usage_visits = 0
        self.blocked_requests = []
        self.http_calls = []
        self.with_notices = False
        self.context = self.browser.new_context(timezone_id='Asia/Shanghai')
        # Every request is fulfilled from synthetic data or explicitly aborted.
        self.context.route('**/*', self.route_request)
        self.page = self.context.new_page()
        self.page.set_default_timeout(2500)
        self.service = object.__new__(BrowserService)
        self.service.page = self.page
        self.service.context = SimpleNamespace(request=SimpleNamespace(get=self.http_get))
        # This suite exercises the retained read-only protocol/parser adapter.
        # Product AI interaction is covered separately by test_web_operator and
        # the real 4B acceptance command, never silently replaced in production.
        self.service._create_cost_reader = lambda **_kwargs: ConsoleCostReader(
            self.service.context, emit=self.service._log)
        self.service.key_table_timeout = 1
        self.service.key_table_stable_seconds = .05
        self.service.anchor = None
        self.service.known_keys = set()

    def tearDown(self):
        self.context.close()

    def http_get(self, url, **kwargs):
        self.http_calls.append((url, kwargs))
        self.assertEqual(parse_qs(urlparse(url).query),
                         {'start': [str(START)], 'end': [str(FINISH)], 'tz': ['28800']})
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer synthetic-session')
        return Response(url, body=json.dumps(self.selected_payload).encode())

    def route_request(self, route):
        parsed = urlparse(route.request.url)
        if parsed.scheme != 'https' or parsed.netloc != 'platform.deepseek.com':
            self.blocked_requests.append(parsed.path)
            route.abort()
        elif parsed.path == '/api_keys':
            self.key_visits += 1
            markup = document(key_row() + key_row('Fixture survivor', SURVIVOR))
            if self.with_notices:
                markup += optional_notice_markup()
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=markup)
        elif parsed.path == '/usage':
            self.usage_visits += 1
            markup = collecting_usage_document()
            if self.with_notices:
                markup += optional_notice_markup()
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=markup)
        elif parsed.path == COST_PATH:
            route.fulfill(status=200, content_type='application/json', body=json.dumps(self.default_payload))
        else:
            self.blocked_requests.append(parsed.path)
            route.abort()

    def collect(self):
        with patch('quota_guard.browser_service.datetime', FixedDateTime):
            result = self.service._collect(START_DATE, account_id='fixture-local-account',
                                           known_keys={TARGET, SURVIVOR})
        self.assertEqual(self.page.url, BASE + '/api_keys')
        self.assertEqual(result['keys'], self.keys)
        self.assertEqual(result['account_id'], 'fixture-local-account')
        self.assertEqual((result['start_date'], result['end_date']), (START_DATE, END_DATE))
        self.assertEqual(self.service.anchor, key_fingerprint(self.keys))
        self.assertEqual(self.service.known_keys, {TARGET, SURVIVOR})
        self.assertEqual(self.blocked_requests, [])
        return result

    def test_default_page_response_cannot_replace_direct_requested_period(self):
        result = self.collect()
        self.assertEqual(result['totals'], {TARGET: Decimal('42.000001'), SURVIVOR: Decimal('0.000007')})
        self.assertEqual(result['source_id'], sha256(
            json.dumps(self.selected_payload, sort_keys=True, default=str).encode()).hexdigest())
        self.assertEqual(result['reason_code'], 'OK')
        self.assertEqual(self.key_visits, 2)
        self.assertEqual(self.usage_visits, 1)
        self.assertEqual(len(self.http_calls), 1)

    def test_next_poll_reuses_session_without_loading_usage_page(self):
        self.collect()
        self.collect()
        self.assertEqual(self.usage_visits, 1)
        self.assertEqual(self.key_visits, 4)
        self.assertEqual(len(self.http_calls), 2)

    def test_malformed_selected_reply_never_falls_back_to_valid_default_costs(self):
        self.selected_payload['data']['biz_data']['data'] = {'unexpected': 'synthetic-shape'}
        result = self.collect()
        self.assertIsNone(result['totals'])
        self.assertEqual(result['source_id'], '')
        self.assertFalse(result['retryable'])
        self.assertEqual(result['reason_code'], 'SCHEMA_UNSUPPORTED')
        self.assertIn('费用结构不是列表', result['message'])

    def test_empty_response_is_retryable_unknown_instead_of_zero(self):
        self.selected_payload['data']['biz_data']['data'][0]['series'] = []
        result = self.collect()
        self.assertIsNone(result['totals'])
        self.assertEqual(result['reason_code'], 'NO_USAGE_DATA')
        self.assertTrue(result['retryable'])

    def test_optional_email_reminders_do_not_block_full_collection(self):
        self.with_notices = True
        result = self.collect()
        self.assertEqual(result['totals'], {TARGET: Decimal('42.000001'), SURVIVOR: Decimal('0.000007')})
        self.assertEqual(self.page.locator('.ds-modal').count(), 0)


if __name__ == '__main__':
    unittest.main()
