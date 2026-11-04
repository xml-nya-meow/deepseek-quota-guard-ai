"""Playwright 1.64.1 empty-vs-missing header observation on loopback only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from quota_guard.platform_keys import ConsoleKeyManager


FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'platform_keys_contract_20261031.json').read_text(encoding='utf-8'))


class EmptyHeaderTransportTests(unittest.TestCase):
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

    def test_browser_observation_and_http_replay_preserve_empty_and_missing_states(self):
        self.assertEqual(version('playwright'), '1.64.1')
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                received.append(('GET', self.path, {key.lower(): value for key, value in self.headers.items()}))
                self.send_response(200)
                if self.path == '/':
                    self.send_header('Content-Type', 'text/html')
                    body = b'<!doctype html><title>Synthetic loopback only</title>'
                else:
                    self.send_header('Content-Type', 'application/json')
                    body = json.dumps(FIXTURE['list_response']).encode()
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                received.append(('POST', self.path, {}))
                self.send_response(405)
                self.end_headers()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        list_url = base + '/api/v0/users/get_api_keys'
        context = self.browser.new_context(service_workers='block')
        # Only this exact loopback origin may be requested by the browser.
        context.route('**/*', lambda route: route.continue_()
                      if route.request.method == 'GET' and route.request.url.startswith(base + '/') else route.abort())
        page = context.new_page()
        observed = []
        context.on('request', lambda request: observed.append(request) if request.url == list_url else None)
        manager = ConsoleKeyManager(context)
        expected = [{field: row[field] for field in ('name', 'tracking_id')}
                    for row in FIXTURE['list_response']['data']['biz_data']['api_keys']]
        # Independent web-wrapper-shaped literals; all values are synthetic.
        headers = {'Authorization': 'Bearer synthetic-loopback-only',
                   'x-client-bundle-id': 'com.deepseek.chat', 'x-client-platform': 'web',
                   'x-client-version': '1.0.0', 'x-client-locale': 'zh_CN',
                   'x-client-timezone-offset': '28800',
                   'x-device-id': '00000000-0000-4000-8000-000000000001'}
        try:
            page.goto(base + '/')
            # Keep both fixed endpoints on loopback as an extra boundary. This
            # test only calls read_keys: no mutation, delete target, or permit.
            with patch.multiple('quota_guard.platform_keys', LIST_URL=list_url,
                                EDIT_URL=base + '/api/v0/users/edit_api_keys'):
                for present in (True, False):
                    with self.subTest(header_present=present):
                        sent = {**headers, **({'x-device-model': ''} if present else {})}
                        page.evaluate('args => fetch(args.url, {headers: args.headers}).then(response => response.json())',
                                      {'url': list_url, 'headers': sent})
                        request = observed[-1]
                        # Reading outside the request callback avoids a nested
                        # synchronous Playwright operation in its event handler.
                        self.assertEqual(request.header_value('x-device-model'), '' if present else None)
                        manager._observe(request)
                        self.assertEqual(manager.read_keys(), expected)
                        # Both the browser GET and APIRequestContext GET must
                        # preserve presence, not merely compare get(..., '').
                        last_two = [entry for entry in received if entry[1] == '/api/v0/users/get_api_keys'][-2:]
                        self.assertEqual(len(last_two), 2)
                        for method, _path, actual_headers in last_two:
                            self.assertEqual(method, 'GET')
                            self.assertEqual('x-device-model' in actual_headers, present)
                            if present:
                                self.assertEqual(actual_headers['x-device-model'], '')
                self.assertTrue(all(method == 'GET' for method, _path, _headers in received))
                self.assertEqual(len(observed), 2)
        finally:
            manager.clear()
            context.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
