"""Synthetic HTTP/session contracts; no account credentials or network access."""
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quota_guard.platform_http import (BASE, COST_PATH, MAX_BODY_BYTES, ConsoleCostReader,
                                      CostReadError, cost_metadata, cost_url, is_cost_endpoint)


class Response:
    def __init__(self, url, *, status=200, body=b'{"cost":0.009999999999999999}', headers=None):
        self.url, self.status, self._body = url, status, body
        self.headers = headers or {'content-type': 'application/json'}
        self.disposed = False

    def body(self):
        return self._body

    def dispose(self):
        self.disposed = True


class HTTPReaderTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.api = Mock(spec=['get'])
        self.reader = ConsoleCostReader(SimpleNamespace(request=self.api),
            emit=lambda event, **data: self.events.append((event, data)))
        self.url = cost_url('2026-10-29', '2026-10-30')
        self.response = Response(self.url)
        self.api.get.return_value = self.response

    def observe(self, *, url=None, method='GET', value='Bearer synthetic-session-credential'):
        request = SimpleNamespace(url=url or self.url, method=method,
                                  header_value=lambda name: value if name == 'authorization' else None)
        self.reader._observe(request)

    def read(self):
        return self.reader.read('2026-10-29', '2026-10-30')

    def test_http_uses_exact_interval_decimal_no_redirects_or_retries(self):
        self.observe()
        url, payload = self.read()
        self.assertEqual(url, self.url)
        self.assertEqual(payload['cost'], Decimal('0.009999999999999999'))
        args, kwargs = self.api.get.call_args
        self.assertEqual(args, (self.url,))
        self.assertEqual(kwargs['max_redirects'], 0)
        self.assertEqual(kwargs['max_retries'], 0)
        self.assertGreater(kwargs['timeout'], 0)
        self.assertFalse(kwargs['ignore_https_errors'])
        self.assertEqual(set(kwargs['headers']), {'Authorization', 'Accept'})
        self.assertTrue(self.response.disposed)
        self.assertNotIn('synthetic-session-credential', json.dumps(self.events))

    def test_unrelated_requests_and_model_keys_cannot_authorize(self):
        bad = ({'url': self.url.replace('https:', 'http:')},
               {'url': self.url.replace('platform.deepseek.com', 'platform.deepseek.com.attacker.invalid')},
               {'url': self.url.replace('platform.deepseek.com', 'user@platform.deepseek.com')},
               {'url': self.url.replace('platform.deepseek.com', 'platform.deepseek.com:443')},
               {'url': BASE + '/api/v0/other'}, {'url': self.url + '#fragment'},
               {'method': 'POST'}, {'value': 'Bearer sk-synthetic-model-key'},
               {'value': 'Bearer secret\nInjected: value'}, {'value': None})
        for fields in bad:
            with self.subTest(fields=fields):
                self.reader.clear()
                self.observe(**fields)
                with self.assertRaises(CostReadError) as caught:
                    self.read()
                self.assertEqual(caught.exception.reason_code, 'AUTH_REQUIRED')
        self.api.get.assert_not_called()

    def test_auth_error_clears_authorization(self):
        self.observe()
        self.response.status = 401
        with self.assertRaises(CostReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.reason_code, 'AUTH_REQUIRED')
        with self.assertRaises(CostReadError):
            self.read()
        self.api.get.assert_called_once()
        self.assertTrue(self.response.disposed)

    def test_redirect_does_not_follow_or_retain_authorization(self):
        self.observe()
        self.response.status = 302
        self.response.headers['location'] = 'https://other.invalid/'
        with self.assertRaises(CostReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.reason_code, 'REDIRECT_REJECTED')
        self.assertIsNone(self.reader._authorization)
        self.api.get.assert_called_once()

    def test_http_error_is_not_a_zero_cost_result(self):
        for status, retryable in ((400, False), (429, True), (503, True)):
            with self.subTest(status=status):
                self.observe()
                self.response.status = status
                with self.assertRaises(CostReadError) as caught:
                    self.read()
                self.assertEqual(caught.exception.reason_code, 'HTTP_ERROR')
                self.assertEqual(caught.exception.retryable, retryable)

    def test_untrusted_sdk_error_text_never_leaves_adapter(self):
        self.observe()
        self.api.get.side_effect = RuntimeError('Authorization: Bearer synthetic-private-data')
        with self.assertRaises(CostReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.reason_code, 'NETWORK_ERROR')
        self.assertNotIn('synthetic-private-data', str(caught.exception))
        self.assertNotIn('synthetic-private-data', json.dumps(self.events))

    def test_invalid_or_oversized_body_is_rejected_and_disposed(self):
        cases = ((b'not json', {'content-type': 'application/json'}, 'RESPONSE_NOT_JSON'),
                 (b'{"cost":NaN}', {'content-type': 'application/json'}, 'RESPONSE_NOT_JSON'),
                 (b'{}', {'content-type': 'text/html'}, 'RESPONSE_NOT_JSON'),
                 (b'{}', {'content-type': 'application/json', 'content-length': str(MAX_BODY_BYTES + 1)}, 'RESPONSE_TOO_LARGE'))
        for body, headers, code in cases:
            with self.subTest(code=code, body=body):
                self.observe()
                self.response = Response(self.url, body=body, headers=headers)
                self.api.get.return_value = self.response
                with self.assertRaises(CostReadError) as caught:
                    self.read()
                self.assertEqual(caught.exception.reason_code, code)
                self.assertTrue(self.response.disposed)

    def test_bootstrap_observes_only_own_usage_request_and_removes_listener(self):
        callbacks = {}
        page = Mock(spec=['on', 'remove_listener', 'goto', 'wait_for_timeout', 'url'])
        page.url = BASE + '/usage'
        page.on.side_effect = lambda name, fn: callbacks.update({name: fn})
        request = SimpleNamespace(method='GET', url=self.url,
                                  header_value=lambda _name: 'Bearer synthetic-own-session')
        page.goto.side_effect = lambda *_args, **_kwargs: callbacks['request'](request)
        self.reader.prepare(page)
        self.reader.prepare(page)
        page.goto.assert_called_once()
        self.assertEqual(page.goto.call_args.args, (BASE + '/usage',))
        page.remove_listener.assert_called_once_with('request', self.reader._observe)
        self.api.get.assert_not_called()

    def test_bootstrap_expiry_does_not_try_calendar_or_unauthed_http(self):
        page = Mock(spec=['on', 'remove_listener', 'goto', 'wait_for_timeout', 'url'])
        page.url = BASE + '/usage'
        with patch('quota_guard.platform_http.time.monotonic', side_effect=[0, 20]):
            with self.assertRaises(CostReadError) as caught:
                self.reader.prepare(page)
        self.assertEqual(caught.exception.reason_code, 'AUTH_REQUIRED')
        self.assertIsNone(self.reader._authorization)
        self.api.get.assert_not_called()
        page.remove_listener.assert_called_once()

    def test_cost_metadata_contains_only_counts_and_numeric_ranges(self):
        payload = {'code': 0, 'data': {'biz_code': 0, 'biz_data': {'start': 1, 'end': 2, 'data': [
            {'currency': 'CNY', 'series': [{'api_key': {'tracking_id': 'private-person'},
              'buckets': [{'cost': 'secret-value', 'time': 1}]}], 'function_series': []}
        ]}}}
        fields = cost_metadata(payload)
        expected = {'response_code': 0, 'biz_code': 0,
            'response_start': 1, 'response_end': 2, 'group_count': 1, 'series_count': 1,
            'bucket_count': 1, 'function_series_count': 0}
        self.assertEqual({key: fields[key] for key in expected}, expected)
        self.assertEqual(fields['bucket_kind'], 'missing')
        self.assertEqual(fields['bucket_first_time'], 1)
        self.assertEqual(fields['bucket_last_time'], 1)
        self.assertEqual(fields['function_bucket_count'], 0)
        self.assertNotIn('function_bucket_first_time', fields)
        self.assertNotIn('function_bucket_last_time', fields)
        for prefix in ('response', 'business'):
            self.assertFalse(fields[prefix + '_message_present'])
            self.assertEqual(fields[prefix + '_message_type'], 'missing')
            self.assertEqual(fields[prefix + '_message_length'], 0)
        for value in ('private-person', 'secret-value'):
            self.assertNotIn(value, json.dumps(fields))

    def test_fixed_endpoint_and_bad_dates(self):
        self.assertTrue(is_cost_endpoint(self.url))
        for first, last in (('bad', '2026-10-30'), ('2026-10-31', '2026-10-30')):
            with self.assertRaises(CostReadError):
                cost_url(first, last)


if __name__ == '__main__':
    unittest.main()
