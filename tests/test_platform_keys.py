"""Candidate public-static Key contracts with mocks only; never sends requests."""
import copy
from dataclasses import FrozenInstanceError, fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from quota_guard.core import GuardError
from quota_guard.platform_keys import ConsoleKeyManager, DeleteTarget


FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'platform_keys_contract_20261031.json').read_text(encoding='utf-8'))


class Response:
    def __init__(self, url, payload, *, status=200, headers=None, body=None):
        self.url, self.status = url, status
        self.headers = headers if headers is not None else {'content-type': 'application/json'}
        self._body = json.dumps(payload).encode() if body is None else body
        self.disposed = False

    def body(self):
        return self._body

    def dispose(self):
        self.disposed = True


class PlatformKeyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = copy.deepcopy(FIXTURE)
        self.events = []
        self.api = Mock(spec=['get', 'post'])
        self.context = SimpleNamespace(request=self.api)
        self.manager = ConsoleKeyManager(self.context,
            emit=lambda event, **data: self.events.append((event, data)))
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        self.expected = [{key: row[key] for key in ('name', 'tracking_id')} for row in rows]
        self.list_response = Response(self.fixture['list_url'], self.fixture['list_response'])
        self.delete_response = Response(self.fixture['delete_url'], self.fixture['synthetic_delete_response'])
        self.api.get.return_value = self.list_response
        self.api.post.return_value = self.delete_response
        self.observed_headers = {
            'authorization': 'Bearer fixture-browser-session',
            'x-client-bundle-id': 'fixture.bundle', 'x-client-platform': 'web',
            'x-client-version': '1.0.0', 'x-client-locale': 'zh_CN',
            'x-client-timezone-offset': '28800', 'x-device-id': 'fixture-device',
            'x-device-model': 'fixture-model', 'cookie': 'fixture-cookie-private',
            'x-client-unlisted-secret': 'fixture-unlisted-private',
        }
        self.request = SimpleNamespace(method='GET', url=self.fixture['list_url'],
            header_value=Mock(side_effect=lambda name: self.observed_headers.get(name)))
        self.manager._observe(self.request)

    def target(self, expected=None):
        return self.manager.read_target(self.expected[0]['name'], self.expected[0]['tracking_id'],
            self.expected if expected is None else expected)

    def replace_list(self, payload=None, **kwargs):
        self.list_response = Response(self.fixture['list_url'],
            self.fixture['list_response'] if payload is None else payload, **kwargs)
        self.api.get.return_value = self.list_response

    def test_independent_fixture_exact_get_and_delete_contract(self):
        target = self.target()
        timeline = []
        self.manager._emit = lambda *_args, **_kwargs: timeline.append('prepared')
        def post(*args, **kwargs):
            timeline.append('post')
            self.assertEqual(timeline, ['prepared', 'before_send', 'post'])
            self.assertEqual(args, (self.fixture['delete_url'],))
            self.assertEqual(kwargs['data'], self.fixture['delete_body'])
            return self.delete_response
        self.api.post.side_effect = post
        result = self.manager.submit_once(target, before_send=lambda: timeline.append('before_send'))
        self.assertEqual(result, {'submitted': True})
        self.assertNotIn('absent', result)
        self.assertEqual(self.api.get.call_args.args, (self.fixture['list_url'],))
        self.assertEqual(self.api.get.call_count, 1)
        self.assertEqual(self.api.post.call_count, 1)
        for call in (self.api.get.call_args, self.api.post.call_args):
            self.assertEqual(call.kwargs['max_redirects'], 0)
            self.assertEqual(call.kwargs['max_retries'], 0)
            self.assertFalse(call.kwargs['ignore_https_errors'])
            self.assertFalse(call.kwargs['fail_on_status_code'])
            self.assertGreater(call.kwargs['timeout'], 0)
        self.assertTrue(self.list_response.disposed)
        self.assertTrue(self.delete_response.disposed)

    def test_target_is_frozen_minimal_and_does_not_copy_other_fields(self):
        self.fixture['list_response']['data']['biz_data']['api_keys'][0]['unused_private'] = 'fixture-private'
        self.replace_list()
        target = self.target()
        self.assertEqual({item.name for item in fields(target)}, {'name', 'tracking_id', 'sensitive_id', 'created_at'})
        self.assertEqual(target.created_at, self.fixture['delete_body']['created_at'])
        with self.assertRaises(FrozenInstanceError):
            target.name = 'Changed'
        self.assertNotIn('fixture-private', repr(target))
        self.assertNotIn(target.sensitive_id, repr(target))

    def test_only_explicit_observed_headers_are_forwarded_and_clear_removes_them(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        expected_names = {'Authorization', 'Accept', 'x-client-bundle-id', 'x-client-platform',
                          'x-client-version', 'x-client-locale', 'x-client-timezone-offset',
                          'x-device-id', 'x-device-model'}
        for call in (self.api.get.call_args, self.api.post.call_args):
            self.assertEqual(set(call.kwargs['headers']), expected_names)
        looked_up = {call.args[0] for call in self.request.header_value.call_args_list}
        self.assertNotIn('cookie', looked_up)
        self.assertNotIn('x-client-unlisted-secret', looked_up)
        serialized = json.dumps(self.events)
        for value in ('fixture-browser-session', 'fixture-device', 'fixture-cookie-private', 'fixture-unlisted-private'):
            self.assertNotIn(value, serialized)
        self.manager.clear()
        self.assertEqual(self.manager._headers, {})
        with self.assertRaises(GuardError):
            self.target()

    def test_unrelated_requests_or_model_credentials_cannot_authorize(self):
        cases = (
            {'url': self.fixture['list_url'] + '?unobserved=1'},
            {'url': self.fixture['list_url'] + '#fragment'},
            {'url': self.fixture['list_url'].replace('https:', 'http:')},
            {'url': self.fixture['list_url'].replace('.com/', '.com.attacker.invalid/')},
            {'url': self.fixture['list_url'].replace('platform.deepseek.com', 'user@platform.deepseek.com')},
            {'url': self.fixture['list_url'].replace('.com/', '.com:443/')},
            {'url': self.fixture['delete_url']}, {'method': 'POST'},
            {'bearer': 'Bearer sk-synthetic-model-secret'}, {'bearer': 'Bearer secret\nInjected: value'},
        )
        for case in cases:
            with self.subTest(case=case):
                self.manager.clear()
                request = SimpleNamespace(method=case.get('method', 'GET'),
                    url=case.get('url', self.fixture['list_url']),
                    header_value=lambda name: case.get('bearer', 'Bearer fixture-session') if name == 'authorization' else None)
                self.manager._observe(request)
                with self.assertRaises(GuardError):
                    self.target()
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_invalid_observed_header_rejects_entire_authorization(self):
        self.manager.clear()
        self.observed_headers['x-device-model'] = 'model\r\nInjected: value'
        self.manager._observe(self.request)
        with self.assertRaises(GuardError):
            self.target()
        self.api.get.assert_not_called()

    def test_missing_and_empty_device_model_are_distinct_and_preserved(self):
        # Independent literals mirror the saved web wrapper's default shape.
        self.observed_headers = {
            'authorization': 'Bearer synthetic-web-session',
            'x-client-bundle-id': 'com.deepseek.chat', 'x-client-platform': 'web',
            'x-client-version': '1.0.0', 'x-client-locale': 'zh_CN',
            'x-client-timezone-offset': '28800',
            'x-device-id': '00000000-0000-4000-8000-000000000001', 'x-device-model': '',
        }
        self.manager._observe(self.request)
        self.assertEqual(self.manager._options()['headers']['x-device-model'], '')
        accepted = [fields for event, fields in self.events if event == 'key_http_empty_header_accepted']
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]['header_name'], 'x-device-model')
        self.assertEqual(accepted[0]['value_length'], 0)
        self.assertIs(accepted[0]['bearer_format_passed'], True)
        self.observed_headers.pop('x-device-model')
        self.manager._observe(self.request)
        self.assertNotIn('x-device-model', self.manager._options()['headers'])
        self.observed_headers['x-device-model'] = '  synthetic model  '
        self.manager._observe(self.request)
        self.assertEqual(self.manager._options()['headers']['x-device-model'], '  synthetic model  ')
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_other_empty_client_headers_still_reject_with_named_safe_metadata(self):
        for name in ('x-client-bundle-id', 'x-client-platform', 'x-client-version',
                     'x-client-locale', 'x-client-timezone-offset', 'x-device-id'):
            with self.subTest(name=name):
                previous = self.observed_headers[name]
                self.observed_headers[name] = ''
                self.manager._observe(self.request)
                self.assertEqual(self.manager._headers, {})
                self.assertEqual(self.manager._observation_reason, 'KEY_CLIENT_HEADER_INVALID')
                event, fields = self.events[-1]
                self.assertEqual(event, 'key_http_client_header_rejected')
                self.assertEqual(fields['header_name'], name)
                self.assertEqual(fields['rejection_kind'], 'empty')
                self.assertEqual(fields['value_type'], 'str')
                self.assertEqual(fields['value_length'], 0)
                self.observed_headers[name] = previous
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_invalid_client_values_remain_rejected_without_logging_values(self):
        class PrivateValue:
            def __str__(self):
                raise AssertionError('Do not stringify header values')
            def __repr__(self):
                raise AssertionError('Do not represent header values')
        cases = [(False, 'type', 'bool', None), (7, 'type', 'int', None),
                 (PrivateValue(), 'type', 'other', None),
                 ('private-value-' * 1000, 'too_long', 'str', 1025)]
        cases.extend(('private-value' + char, 'control_char', 'str', 14) for char in ('\r', '\n', '\0', '\x7f'))
        for value, kind, value_type, length in cases:
            with self.subTest(kind=kind, value_type=value_type):
                self.observed_headers['x-device-model'] = value
                self.manager._observe(self.request)
                self.assertEqual(self.manager._headers, {})
                event, fields = self.events[-1]
                self.assertEqual(event, 'key_http_client_header_rejected')
                self.assertEqual(fields['rejection_kind'], kind)
                self.assertEqual(fields['value_type'], value_type)
                self.assertEqual(fields.get('value_length'), length)
                self.assertNotIn('private-value', json.dumps(fields))
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_rejected_candidate_discards_old_authorization_and_target_but_not_attempts(self):
        target = self.target()
        self.manager._attempted.add('synthetic-earlier-attempt')
        self.observed_headers['x-device-id'] = ''
        self.manager._observe(self.request)
        self.assertEqual(self.manager._headers, {})
        self.assertIsNone(self.manager._issued)
        self.assertEqual(self.manager._attempted, {'synthetic-earlier-attempt'})
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=lambda: None)
        self.api.post.assert_not_called()

    def test_prepare_uses_own_page_exact_request_and_cleans_listener(self):
        self.manager.clear()
        callbacks = {}
        page = Mock(spec=['context', 'url', 'goto', 'wait_for_timeout'])
        page.context, page.url = self.context, 'https://platform.deepseek.com/api_keys'
        self.context.on = Mock(side_effect=lambda event, callback: callbacks.update({event: callback}))
        self.context.remove_listener = Mock()
        page.goto.side_effect = lambda *_args, **_kwargs: callbacks['request'](self.request)
        with patch('quota_guard.platform_keys.dismiss_optional_notice', return_value=False):
            self.manager.prepare(page)
            self.manager.prepare(page)
        page.goto.assert_called_once()
        self.assertEqual(page.goto.call_args.args, ('https://platform.deepseek.com/api_keys',))
        self.context.remove_listener.assert_called_once_with('request', self.manager._queue_request)
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_prepare_rejects_other_context_or_redirect_or_expired_bootstrap(self):
        page = Mock(spec=['context', 'url', 'goto', 'wait_for_timeout'])
        page.context = object()
        with self.assertRaises(GuardError):
            self.manager.prepare(page)
        page.goto.assert_not_called()
        self.manager.clear()
        self.context.on, self.context.remove_listener = Mock(), Mock()
        page.context, page.url = self.context, 'https://platform.deepseek.com/sign_in'
        with self.assertRaises(GuardError):
            self.manager.prepare(page)
        self.assertEqual(self.manager._headers, {})
        page.url = 'https://platform.deepseek.com/api_keys'
        ticks = iter([0, 20])
        with patch('quota_guard.platform_keys.time.monotonic', side_effect=lambda: next(ticks, 20)):
            with self.assertRaises(GuardError):
                self.manager.prepare(page)
        self.api.get.assert_not_called()

    def test_complete_expected_set_and_name_identity_must_match(self):
        cases = [self.expected[:-1], self.expected + [self.expected[0]],
                 [dict(item, name='Wrong') if index == 2 else item for index, item in enumerate(self.expected)]]
        for expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(GuardError):
                    self.target(expected)
        self.api.post.assert_not_called()

    def test_duplicate_ids_or_sensitive_ids_and_bad_target_fields_refuse(self):
        mutations = [
            lambda rows: rows[1].update(tracking_id=rows[0]['tracking_id']),
            lambda rows: rows[1].update(sensitive_id=rows[0]['sensitive_id']),
            lambda rows: rows[0].update(tracking_id='not-a-tracking-id'),
            lambda rows: rows[0].update(name=' Fixture target '),
            lambda rows: rows[0].update(sensitive_id='sk-full-secret-without-masking'),
            lambda rows: rows[0].update(sensitive_id=None),
            lambda rows: rows[0].update(created_at=True),
            lambda rows: rows[0].update(created_at=1793203200.5),
            lambda rows: rows[0].update(created_at=-1),
            lambda rows: rows.pop(),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = copy.deepcopy(self.fixture['list_response'])
                mutation(payload['data']['biz_data']['api_keys'])
                self.replace_list(payload)
                with self.assertRaises(GuardError):
                    self.target()
                self.assertTrue(self.list_response.disposed)
        self.api.post.assert_not_called()

    def test_uncertain_http_or_business_results_reject_and_dispose(self):
        bad_payloads = [[], {}, {'code': True, 'data': {'biz_code': 0, 'biz_data': {}}},
                        {'code': 0, 'data': {'biz_code': 3, 'biz_data': None}},
                        {'code': 0, 'data': {'biz_code': False, 'biz_data': None}},
                        {'code': 0, 'data': {'biz_code': 0}},
                        {'code': 0, 'data': {'biz_code': 0, 'biz_data': []}}]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                self.replace_list(payload)
                with self.assertRaises(GuardError):
                    self.target()
                self.assertTrue(self.list_response.disposed)
        for status in (201, 302, 401, 403, 429, 500):
            with self.subTest(status=status):
                self.manager._observe(self.request)
                self.replace_list(status=status)
                with self.assertRaises(GuardError):
                    self.target()
                self.assertTrue(self.list_response.disposed)
        self.api.post.assert_not_called()

    def test_malformed_json_or_wrong_url_or_oversized_body_refuse(self):
        cases = [dict(body=b'{"code":0,"code":1}'), dict(body=b'{"code":NaN}'),
                 dict(body=b'not-json'), dict(headers={'content-type': 'text/html'}),
                 dict(headers={'content-type': 'application/json', 'content-length': '1000001'}),
                 dict(body=b' ' * 1000001)]
        for kwargs in cases:
            with self.subTest(kwargs={key: type(value).__name__ for key, value in kwargs.items()}):
                self.replace_list(**kwargs)
                with self.assertRaises(GuardError):
                    self.target()
                self.assertTrue(self.list_response.disposed)
        self.replace_list()
        self.list_response.url = 'https://other.invalid/'
        with self.assertRaises(GuardError):
            self.target()
        self.assertTrue(self.list_response.disposed)
        self.assertEqual(self.manager._headers, {})

    def test_copied_target_or_old_read_or_clear_cannot_authorize_submission(self):
        target = self.target()
        with self.assertRaises(GuardError):
            self.manager.submit_once(replace(target), before_send=lambda: None)
        self.target()
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=lambda: None)
        target = self.target()
        self.manager.clear()
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=lambda: None)
        self.api.post.assert_not_called()

    def test_gate_failure_prevents_post_and_consumes_attempt(self):
        target = self.target()
        failure = GuardError('Synthetic pre-send cancellation')
        gate = Mock(side_effect=failure)
        with self.assertRaises(GuardError) as caught:
            self.manager.submit_once(target, before_send=gate)
        self.assertIs(caught.exception, failure)
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=gate)
        gate.assert_called_once()
        self.api.post.assert_not_called()

    def test_network_failure_does_not_retry_or_leak_and_blocks_new_attempt(self):
        target = self.target()
        self.api.post.side_effect = RuntimeError('Authorization: fixture-private-server-text')
        with self.assertRaises(GuardError) as caught:
            self.manager.submit_once(target, before_send=lambda: None)
        self.assertNotIn('fixture-private-server-text', str(caught.exception))
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=lambda: None)
        with self.assertRaises(GuardError):
            self.target()
        self.api.post.assert_called_once()

    def test_success_also_cannot_be_repeated(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=lambda: None)
        self.api.post.assert_called_once()

    def test_fresh_read_after_submission_is_minimal_and_cannot_unlock_second_post(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        payload = copy.deepcopy(self.fixture['list_response'])
        payload['data']['biz_data']['api_keys'].pop(0)
        self.replace_list(payload)
        self.assertEqual(self.manager.read_keys(), self.expected[1:])
        self.assertEqual(self.api.get.call_count, 2)
        self.assertIsNone(self.manager._issued)
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=lambda: None)
        self.replace_list()
        with self.assertRaises(GuardError):
            self.target()
        self.api.post.assert_called_once()

    def test_read_keys_allows_empty_complete_list_but_never_issues_delete_target(self):
        payload = copy.deepcopy(self.fixture['list_response'])
        payload['data']['biz_data']['api_keys'] = []
        self.replace_list(payload)
        self.assertEqual(self.manager.read_keys(), [])
        self.assertIsNone(self.manager._issued)
        with self.assertRaises(GuardError):
            self.target()
        self.api.post.assert_not_called()

    def test_post_submission_read_failure_stays_unknown_and_preserves_attempt_guard(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        self.api.get.side_effect = RuntimeError('private-post-read-failure')
        with self.assertRaises(GuardError) as caught:
            self.manager.read_keys()
        self.assertNotIn('private-post-read-failure', str(caught.exception))
        self.api.get.side_effect = None
        self.replace_list(status=401)
        with self.assertRaises(GuardError):
            self.manager.read_keys()
        # Even authorization invalidation followed by a fresh observation
        # cannot make an already submitted target eligible again.
        self.manager._observe(self.request)
        self.replace_list()
        with self.assertRaises(GuardError):
            self.target()
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=lambda: None)
        self.api.post.assert_called_once()

    def test_read_keys_requires_identity_but_not_unused_action_metadata(self):
        for field, value in (('tracking_id', self.expected[0]['tracking_id']),
                             ('sensitive_id', 'sk-not-a-redacted-key')):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.fixture['list_response'])
                payload['data']['biz_data']['api_keys'][1][field] = value
                self.replace_list(payload)
                if field == 'tracking_id':
                    with self.assertRaises(GuardError):
                        self.manager.read_keys()
                else:
                    self.assertEqual(self.manager.read_keys(), self.expected)
                self.assertIsNone(self.manager._issued)
                self.assertTrue(self.list_response.disposed)
        self.api.post.assert_not_called()

    def test_post_business_error_or_different_returned_target_is_not_success(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                self.manager.clear()
                self.manager._observe(self.request)
                target = self.target()
                payload = copy.deepcopy(self.fixture['synthetic_delete_response'])
                if mismatch:
                    payload['data']['biz_data'] = {'api_key': self.fixture['list_response']['data']['biz_data']['api_keys'][1]}
                else:
                    payload['data']['biz_code'] = 1
                response = Response(self.fixture['delete_url'], payload)
                self.api.post.return_value = response
                with self.assertRaises(GuardError):
                    self.manager.submit_once(target, before_send=lambda: None)
                self.assertTrue(response.disposed)

    def test_empty_delete_metadata_only_reports_submitted(self):
        for metadata in (None, {}, {'api_key': None}, {'api_key': {}}, {'unrelated': 'synthetic'}):
            with self.subTest(metadata=metadata):
                self.manager.clear()
                self.manager._observe(self.request)
                target = self.target()
                payload = {'code': 0, 'data': {'biz_code': 0, 'biz_data': metadata}}
                response = Response(self.fixture['delete_url'], payload)
                self.api.post.return_value = response
                result = self.manager.submit_once(target, before_send=lambda: None)
                self.assertEqual(result, {'submitted': True})
                self.assertNotIn('absent', result)
                self.assertTrue(response.disposed)

    def test_get_exception_text_is_sanitized(self):
        self.api.get.side_effect = RuntimeError('fixture-private-http-headers')
        with self.assertRaises(GuardError) as caught:
            self.target()
        self.assertNotIn('fixture-private-http-headers', str(caught.exception))
        self.api.post.assert_not_called()

    def test_pre_send_callback_or_logging_failure_cannot_leak_or_send(self):
        for component in ('callback', 'logging'):
            with self.subTest(component=component):
                self.manager._emit = lambda *_args, **_kwargs: None
                target = self.target()
                failure = Mock(side_effect=RuntimeError('fixture-private-failure-details'))
                if component == 'logging':
                    self.manager._emit = failure
                with self.assertRaises(GuardError) as caught:
                    self.manager.submit_once(target, before_send=failure if component == 'callback' else lambda: None)
                self.assertNotIn('fixture-private-failure-details', str(caught.exception))
                self.api.post.assert_not_called()
                self.manager.clear()
                self.manager._observe(self.request)


class PlatformKeyLoopbackTests(unittest.TestCase):
    """Exercise the installed HTTP transport against a local synthetic server."""
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()

    def setUp(self):
        fixture = copy.deepcopy(FIXTURE)
        self.received = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                received.append({'method': 'GET', 'path': self.path,
                                 'headers': dict(self.headers), 'body': b''})
                self.send_json(fixture['list_response'])

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                received.append({'method': 'POST', 'path': self.path,
                                 'headers': dict(self.headers), 'body': body})
                if self.server.disconnect_post:
                    # The server has received the complete mutation request,
                    # then closes before any response. A client retry would be
                    # visible as a second recorded POST.
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                self.send_json(fixture['synthetic_delete_response'])

            def send_json(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.disconnect_post = False
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.list_url = base + '/api/v0/users/get_api_keys'
        self.edit_url = base + '/api/v0/users/edit_api_keys'
        self.url_patch = patch.multiple('quota_guard.platform_keys', LIST_URL=self.list_url, EDIT_URL=self.edit_url)
        self.url_patch.start()
        self.request_context = self.pw.request.new_context()
        self.manager = ConsoleKeyManager(SimpleNamespace(request=self.request_context))
        self.header_values = {'authorization': 'Bearer synthetic-loopback-session',
                              'x-device-id': 'synthetic-loopback-device',
                              'x-client-platform': 'web', 'cookie': 'never-forward-this'}
        self.manager._observe(SimpleNamespace(method='GET', url=self.list_url,
            header_value=lambda name: self.header_values.get(name)))
        self.expected = [{key: row[key] for key in ('name', 'tracking_id')}
                         for row in fixture['list_response']['data']['biz_data']['api_keys']]

    def tearDown(self):
        self.manager.clear()
        self.request_context.dispose()
        self.url_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def target(self):
        return self.manager.read_target(self.expected[0]['name'], self.expected[0]['tracking_id'], self.expected)

    def test_real_transport_sends_exact_json_contract_and_observed_headers(self):
        target = self.target()
        gate = []
        def before_send():
            self.assertEqual([item['method'] for item in self.received], ['GET'])
            gate.append('ready')
        result = self.manager.submit_once(target, before_send=before_send)
        self.assertEqual(result, {'submitted': True})
        self.assertEqual(gate, ['ready'])
        self.assertEqual([(item['method'], item['path']) for item in self.received], [
            ('GET', '/api/v0/users/get_api_keys'), ('POST', '/api/v0/users/edit_api_keys')])
        post = self.received[1]
        self.assertEqual(json.loads(post['body']), FIXTURE['delete_body'])
        for item in self.received:
            headers = {key.lower(): value for key, value in item['headers'].items()}
            self.assertEqual(headers['authorization'], 'Bearer synthetic-loopback-session')
            self.assertEqual(headers['x-device-id'], 'synthetic-loopback-device')
            self.assertEqual(headers['x-client-platform'], 'web')
            self.assertNotIn('cookie', headers)
        self.assertEqual({key.lower(): value for key, value in post['headers'].items()}['content-type'], 'application/json')

    def test_received_then_disconnected_post_is_never_retried(self):
        target = self.target()
        self.server.disconnect_post = True
        gate = Mock()
        with self.assertRaises(GuardError) as caught:
            self.manager.submit_once(target, before_send=gate)
        self.assertNotIn('synthetic-loopback-session', str(caught.exception))
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=gate)
        gate.assert_called_once()
        posts = [item for item in self.received if item['method'] == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0]['body']), FIXTURE['delete_body'])


if __name__ == '__main__':
    unittest.main()
