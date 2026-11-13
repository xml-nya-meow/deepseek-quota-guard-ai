"""Owned loopback fixtures only: no browser, model or platform credentials.

These checks validate the acceptance backend/transport, not AI success. A
second loopback server is an escape detector for redirect tests; no external
network destination is contacted, even if redirect protection regresses.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import urlencode

from quota_guard.operator_fixture import (
    COST, EDIT, LIST, ORIGIN, LoopbackTransport, OfflineContext,
    OfflinePlatform, OfflineResponse, envelope,
)


def delete_payload(key):
    return {'action': 'delete', 'name': None, 'redacted_key': key['sensitive_id'],
            'created_at': key['created_at'], 'tracking_id': key['tracking_id']}


def cost_path(ids):
    return COST + '?' + urlencode({'start': '1793289600', 'end': '1793376000',
                                  'tz': '28800', 'api_key_tracking_id': ids}, doseq=True)


class OfflineBackendTests(unittest.TestCase):
    def setUp(self):
        self.platform = OfflinePlatform(seed=9901)

    def key(self, identity):
        return next(key for key in self.platform.keys if key['tracking_id'] == identity)

    def test_envelope_and_normal_dsh_identity_contract(self):
        self.assertEqual(envelope({'x': 1}), {'code': 0, 'msg': '', 'data':
            {'biz_code': 0, 'biz_msg': '', 'biz_data': {'x': 1}}})
        status, media, body = self.platform.handle('GET', LIST)
        self.assertEqual((status, media), (200, 'application/json'))
        payload = json.loads(body)['data']['biz_data']
        self.assertEqual(len(payload['api_keys']), 3)
        self.assertEqual(payload['key_quota']['total']['used'], 2)
        dsh = next(key for key in payload['api_keys'] if key['key_type'] == 'DSH')
        self.assertEqual(dsh['sensitive_id'], '')
        self.assertEqual({key['tracking_id'] for key in payload['api_keys']},
                         {key['tracking_id'] for key in self.platform.initial})

    def test_seed_changes_position_but_identity_controls_target_selection(self):
        positions = set()
        for seed in range(9900, 9920):
            platform = OfflinePlatform(seed=seed)
            positions.add(next(i for i, key in enumerate(platform.keys)
                               if key['tracking_id'] == platform.target_id))
            key = next(key for key in platform.keys if key['tracking_id'] == platform.target_id)
            status, _, _ = platform.handle('POST', EDIT, json.dumps(delete_payload(key)).encode())
            self.assertEqual(status, 200)
            self.assertEqual(platform.keys, [item for item in platform.initial
                                            if item['tracking_id'] != platform.target_id])
        self.assertEqual(positions, {0, 1, 2})

    def test_cost_only_returns_requested_tracking_ids_and_preserves_date_bounds(self):
        ids = [self.platform.target_id]
        status, _, body = self.platform.handle('GET', cost_path(ids), source='browser')
        self.assertEqual(status, 200)
        data = json.loads(body)['data']['biz_data']
        self.assertEqual((data['start'], data['end']), (1793289600, 1793376000))
        series = data['data'][0]['series']
        self.assertEqual([row['api_key']['tracking_id'] for row in series], ids)
        self.assertEqual(series[0]['buckets'][0]['cost'], '1.25')
        self.assertEqual(self.platform.events[-1]['ids'], ids)
        self.assertEqual(self.platform.events[-1]['source'], 'browser')
        self.assertEqual(self.platform.keys, self.platform.initial)

    def test_wrong_endpoint_identity_mask_timestamp_or_extra_field_never_delete(self):
        original = delete_payload(self.key(self.platform.target_id))
        bad = [('POST', '/wrong', original),
               ('POST', EDIT, {**original, 'tracking_id': 'unlisted-target'}),
               ('POST', EDIT, {**original, 'redacted_key': 'sk-wrong*****xxxx'}),
               ('POST', EDIT, {**original, 'created_at': original['created_at'] + 1}),
               ('POST', EDIT, {**original, 'extra': True})]
        for method, path, payload in bad:
            with self.subTest(path=path, payload=payload):
                self.assertEqual(self.platform.handle(method, path, json.dumps(payload).encode())[0], 400)
                self.assertEqual(self.platform.keys, self.platform.initial)
        self.assertFalse(any(event['kind'] == 'delete_applied' for event in self.platform.events))

    def test_dsh_entry_cannot_use_normal_delete(self):
        dsh = next(key for key in self.platform.keys if key['key_type'] == 'DSH')
        status, _, _ = self.platform.handle('POST', EDIT, json.dumps(delete_payload(dsh)).encode())
        self.assertEqual(status, 400)
        self.assertEqual(self.platform.keys, self.platform.initial)

    def test_response_drop_applies_once_and_snapshot_is_detached(self):
        self.platform.fault = 'response_drop'
        payload = json.dumps(delete_payload(self.key(self.platform.target_id))).encode()
        self.assertEqual(self.platform.handle('POST', EDIT, payload), (None, None, None))
        self.assertEqual(self.platform.handle('POST', EDIT, payload)[0], 400)
        state = self.platform.snapshot()
        self.assertEqual(sum(event['kind'] == 'delete_applied' for event in state['events']), 1)
        self.assertEqual({key['tracking_id'] for key in state['keys']},
                         {key['tracking_id'] for key in state['initial_keys']} - {self.platform.target_id})
        state['keys'].clear()
        state['events'].clear()
        self.assertEqual(len(self.platform.keys), 2)
        self.assertTrue(self.platform.events)

    def test_public_page_does_not_embed_score_target_or_seed(self):
        for variant in ('direct', 'menu', 'confirm2', 'native_confirm'):
            platform = OfflinePlatform(seed=9933, variant=variant)
            for path in ('/usage', '/api_keys'):
                with self.subTest(variant=variant, path=path):
                    status, media, body = platform.handle('GET', path)
                    self.assertEqual(status, 200)
                    self.assertIn('text/html', media)
                    text = body.decode()
                    self.assertNotIn(platform.target_id, text)
                    self.assertNotIn(platform.control_id, text)
                    self.assertNotIn('9933', text)
                    self.assertNotIn('backend-truth', text)
                    self.assertNotIn('__CONFIG__', text)

    def test_offline_response_matches_read_adapter_interface(self):
        response = OfflineResponse(ORIGIN + LIST, 200, {'Content-Type': 'application/json'}, b'{"code":0}')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers, {'content-type': 'application/json'})
        self.assertEqual(response.body(), b'{"code":0}')
        self.assertEqual(response.json(), {'code': 0})
        self.assertIsNone(response.dispose())


class LoopbackTransportTests(unittest.TestCase):
    def setUp(self):
        self.platform = OfflinePlatform(seed=9971).start()
        self.addCleanup(self.platform.close)
        self.transport = LoopbackTransport(self.platform.origin)

    def test_only_owned_loopback_backend_origins_are_accepted(self):
        for origin in ('https://127.0.0.1:1234', 'http://localhost:1234',
                       'http://127.0.0.2:1234', 'http://example.invalid:1234',
                       'http://127.0.0.1:1234/path'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                LoopbackTransport(origin)

    def test_get_maps_platform_url_to_owned_server_without_changing_identity(self):
        response = self.transport.get(ORIGIN + LIST)
        self.assertEqual(response.url, ORIGIN + LIST)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()['data']['biz_data']['api_keys'], self.platform.keys)
        self.assertEqual(self.platform.events[-1]['source'], 'api-context')

    def test_external_lookalike_and_fragment_urls_are_refused_before_any_send(self):
        original_http = self.transport.http
        self.transport.http = Mock()
        for url in ('http://platform.deepseek.com' + LIST, 'https://example.invalid' + LIST,
                    'https://platform.deepseek.com.evil.invalid' + LIST,
                    'https://platform.deepseek.com:443' + LIST,
                    'https://user@platform.deepseek.com' + LIST,
                    ORIGIN + LIST + '#fragment', '/api_keys'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.transport.fetch(url)
        self.transport.http.open.assert_not_called()
        self.transport.http = original_http
        self.assertEqual(self.platform.events, [])

    def test_script_post_and_fetch_post_are_both_forbidden(self):
        key = next(key for key in self.platform.keys if key['tracking_id'] == self.platform.target_id)
        payload = json.dumps(delete_payload(key)).encode()
        with self.assertRaisesRegex(AssertionError, 'OFFLINE_SCRIPT_POST_FORBIDDEN'):
            self.transport.post(ORIGIN + EDIT, data=payload)
        with self.assertRaisesRegex(AssertionError, 'OFFLINE_SCRIPT_POST_FORBIDDEN'):
            self.transport.fetch(ORIGIN + EDIT, method='POST', body=payload)
        self.assertEqual(self.platform.events, [])
        self.assertEqual(self.platform.keys, self.platform.initial)

    def test_browser_route_can_submit_exact_payload_and_report_response(self):
        proxy = OfflineContext(Mock(), self.platform.origin)
        target = next(key for key in self.platform.keys if key['tracking_id'] == self.platform.target_id)
        request = SimpleNamespace(url=ORIGIN + EDIT, method='POST',
                                  post_data_buffer=json.dumps(delete_payload(target)).encode())
        route = Mock()
        proxy.route_bottom(route, request)
        route.abort.assert_not_called()
        route.fulfill.assert_called_once()
        self.assertEqual(route.fulfill.call_args.kwargs['status'], 200)
        post = next(event for event in self.platform.events if event['kind'] == 'post_arrival')
        self.assertEqual(post['source'], 'browser')
        self.assertEqual(self.platform.keys, [key for key in self.platform.initial
                                            if key['tracking_id'] != self.platform.target_id])

    def test_response_drop_aborts_route_but_backend_deletion_remains_visible(self):
        self.platform.fault = 'response_drop'
        proxy = OfflineContext(Mock(), self.platform.origin)
        target = next(key for key in self.platform.keys if key['tracking_id'] == self.platform.target_id)
        route = Mock()
        request = SimpleNamespace(url=ORIGIN + EDIT, method='POST',
                                  post_data_buffer=json.dumps(delete_payload(target)).encode())
        proxy.route_bottom(route, request)
        route.abort.assert_called_once_with('failed')
        route.fulfill.assert_not_called()
        response = self.transport.get(ORIGIN + LIST)
        self.assertNotIn(self.platform.target_id,
                         {key['tracking_id'] for key in response.json()['data']['biz_data']['api_keys']})

    def test_browser_route_blocks_unexpected_origin_without_backend_access(self):
        proxy = OfflineContext(Mock(), self.platform.origin)
        route = Mock()
        proxy.route_bottom(route, SimpleNamespace(url='https://example.invalid/usage',
                                                  method='GET', post_data_buffer=None))
        route.abort.assert_called_once_with('failed')
        route.fulfill.assert_not_called()
        self.assertEqual(self.platform.events, [])

    def test_redirect_never_reaches_a_second_owned_server(self):
        arrivals = []
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                arrivals.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')
        receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        receiver_thread.start()
        location = 'http://127.0.0.1:' + str(receiver.server_port) + '/escape-detector'
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', location)
                self.send_header('Content-Length', '0')
                self.end_headers()
        redirector = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        redirect_thread = threading.Thread(target=redirector.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            transport = LoopbackTransport('http://127.0.0.1:' + str(redirector.server_port))
            try:
                response = transport.get(ORIGIN + LIST)
            except (ValueError, HTTPError):
                pass
            else:
                self.assertEqual(response.status, 302)
            self.assertEqual(arrivals, [], 'redirect escaped the one fixture origin')
        finally:
            redirector.shutdown()
            redirector.server_close()
            redirect_thread.join(3)
            receiver.shutdown()
            receiver.server_close()
            receiver_thread.join(3)


if __name__ == '__main__':
    unittest.main()
