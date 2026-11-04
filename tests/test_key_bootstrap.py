"""Synthetic management bootstrap only; browser traffic is fulfilled or blocked."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from playwright.sync_api import sync_playwright

from quota_guard.core import GuardError
from quota_guard.platform_keys import ConsoleKeyManager, KeyPreparationError


# Independent literals describe the allowed observation sources. No real
# account data, browser profile, credentials, or live endpoint is used.
KEY_PAGE = 'https://platform.deepseek.com/api_keys'
KEY_LIST = 'https://platform.deepseek.com/api/v0/users/get_api_keys'
USAGE_PAGE = 'https://platform.deepseek.com/usage'
COST_GET = 'https://platform.deepseek.com/api/v0/usage/by_api_key/cost?start=synthetic'
SESSION = 'Bearer synthetic-session:punctuation!allowed'


class KeyBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.callbacks = {}
        self.events = []
        self.api = Mock(spec=['get', 'post'])
        self.context = SimpleNamespace(request=self.api,
            on=Mock(side_effect=lambda event, callback: self.callbacks.update({event: callback})),
            remove_listener=Mock(side_effect=lambda event, _callback: self.callbacks.pop(event)))
        self.page = Mock(spec=['context', 'url', 'goto', 'wait_for_timeout'])
        self.page.context, self.page.url = self.context, KEY_PAGE
        self.page.wait_for_timeout.side_effect = self.advance
        self.page.goto.side_effect = self.navigate
        self.manager = ConsoleKeyManager(self.context,
            emit=lambda event, **fields: self.events.append((event, fields)))
        self.clock_patch = patch('quota_guard.platform_keys.time.monotonic', side_effect=lambda: self.clock)
        self.notice_patch = patch('quota_guard.platform_keys.dismiss_optional_notice', return_value=False)
        self.clock_patch.start()
        self.notice = self.notice_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.notice_patch.stop)

    def advance(self, milliseconds):
        self.clock += milliseconds / 1000

    def navigate(self, url, **_kwargs):
        self.page.url = url

    def request(self, *, url=KEY_LIST, method='GET', bearer=SESSION, extra=None):
        headers = {'authorization': bearer, **(extra or {})}
        return SimpleNamespace(method=method, url=url,
                               header_value=Mock(side_effect=headers.get))

    def emit_request(self, request):
        self.callbacks['request'](request)

    def assert_failure(self, reason):
        with self.assertRaises(KeyPreparationError) as caught:
            self.manager.prepare(self.page)
        self.assertEqual(caught.exception.reason_code, reason)
        self.assertEqual(self.manager.failure_reason, reason)
        self.assertEqual(self.manager._headers, {})
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()
        return caught.exception

    def test_first_list_request_is_reused_without_second_navigation_or_callback_header_reads(self):
        request = self.request()
        with self.manager.capture(self.page):
            self.emit_request(request)
            request.header_value.assert_not_called()
            self.manager.prepare(self.page)
            self.assertEqual(self.manager._headers['Authorization'], SESSION)
            self.assertIsNone(self.manager.failure_reason)
        self.page.goto.assert_not_called()
        self.context.on.assert_called_once()
        self.context.remove_listener.assert_called_once()
        self.assertEqual(self.callbacks, {})
        self.assertNotIn(SESSION, json.dumps(self.events))
        self.api.post.assert_not_called()

    def test_standalone_prepare_observes_context_request_without_page_events(self):
        request = self.request()
        def navigate(url, **kwargs):
            self.navigate(url, **kwargs)
            self.emit_request(request)
            request.header_value.assert_not_called()
        self.page.goto.side_effect = navigate
        self.manager.prepare(self.page)
        self.assertEqual(self.manager._headers['Authorization'], SESSION)
        self.assertEqual([call.args[0] for call in self.page.goto.call_args_list], [KEY_PAGE])
        self.assertEqual(self.callbacks, {})
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_no_list_request_uses_only_new_known_cost_get(self):
        stale = self.request(url=COST_GET, bearer='Bearer stale-not-observed-in-cost-phase')
        fresh = self.request(url=COST_GET)
        def navigate(url, **kwargs):
            self.navigate(url, **kwargs)
            self.emit_request(stale if url == KEY_PAGE else fresh)
        self.page.goto.side_effect = navigate
        self.manager.prepare(self.page)
        self.assertEqual([call.args[0] for call in self.page.goto.call_args_list], [KEY_PAGE, USAGE_PAGE])
        stale.header_value.assert_not_called()
        self.assertEqual(self.manager._headers['Authorization'], SESSION)
        self.assertTrue(any(fields.get('phase') == 'cost_read' for event, fields in self.events
                            if event == 'key_http_bootstrap_ready'))
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_cost_phase_does_not_accept_unrelated_sources_or_model_keys(self):
        for candidate in (
            self.request(url=COST_GET.replace('.com/', '.com.attacker.invalid/')),
            self.request(url=COST_GET.replace('/cost?', '/other?')),
            self.request(url=COST_GET, method='POST'),
        ):
            with self.subTest(url=candidate.url, method=candidate.method):
                self.manager.clear()
                self.page.goto.side_effect = lambda url, **kwargs: (
                    self.navigate(url, **kwargs), self.emit_request(candidate))
                self.assert_failure('KEY_READ_REQUEST_NOT_OBSERVED')
                candidate.header_value.assert_not_called()
        self.manager.clear()
        bad = self.request(url=COST_GET, bearer='Bearer sk-synthetic-model-key')
        self.page.goto.side_effect = lambda url, **kwargs: (self.navigate(url, **kwargs), self.emit_request(bad))
        self.assert_failure('KEY_AUTH_HEADER_INVALID')

    def test_rejected_list_headers_report_specific_reason_and_never_try_fallback(self):
        cases = (
            (self.request(bearer=None), 'KEY_AUTH_HEADER_MISSING'),
            (self.request(bearer='Bearer secret\r\nprivate-data'), 'KEY_AUTH_HEADER_INVALID'),
            (self.request(extra={'x-device-id': 'private\nvalue'}), 'KEY_CLIENT_HEADER_INVALID'),
            (self.request(bearer='Bearer sk-synthetic-model-key'), 'KEY_AUTH_HEADER_INVALID'),
        )
        for request, reason in cases:
            with self.subTest(reason=reason):
                self.manager.clear()
                self.page.goto.reset_mock()
                self.page.goto.side_effect = lambda url, **kwargs: (
                    self.navigate(url, **kwargs), self.emit_request(request))
                self.assert_failure(reason)
                self.assertEqual([call.args[0] for call in self.page.goto.call_args_list], [KEY_PAGE])
        self.assertNotIn('private-data', json.dumps(self.events))
        self.assertNotIn('private\\nvalue', json.dumps(self.events))

    def test_header_read_error_is_sanitized_and_clear_resets_reason(self):
        request = self.request()
        request.header_value.side_effect = RuntimeError('private-sdk-header-details')
        self.page.goto.side_effect = lambda url, **kwargs: (self.navigate(url, **kwargs), self.emit_request(request))
        error = self.assert_failure('KEY_HEADER_READ_FAILED')
        self.assertNotIn('private-sdk-header-details', str(error))
        self.assertNotIn('private-sdk-header-details', json.dumps(self.events))
        self.manager.clear()
        self.assertIsNone(self.manager.failure_reason)

    def test_missing_requests_have_bounded_wait_with_zero_writes(self):
        self.assert_failure('KEY_READ_REQUEST_NOT_OBSERVED')
        self.assertLessEqual(self.clock, 15.001)
        self.assertTrue(any(fields.get('reason_code') == 'KEY_LIST_REQUEST_NOT_OBSERVED'
                            for _event, fields in self.events))
        self.assertEqual(self.callbacks, {})

    def test_email_failure_preserves_specific_reason_without_fallback_or_header_read(self):
        request = self.request()
        self.notice.side_effect = GuardError('private-input-must-not-be-returned')
        with self.manager.capture(self.page):
            self.emit_request(request)
            error = self.assert_failure('KEY_NOTICE_REQUIRES_USER')
            request.header_value.assert_not_called()
        self.page.goto.assert_not_called()
        self.assertNotIn('private-input', str(error))

    def test_redirect_refuses_even_when_authorization_request_is_observed(self):
        def navigate(_url, **_kwargs):
            self.page.url = 'https://platform.deepseek.com/sign_in'
            self.emit_request(self.request())
        self.page.goto.side_effect = navigate
        self.assert_failure('KEY_PAGE_CHANGED')

    def test_diagnostic_failure_cannot_leak_or_leave_authorized_manager(self):
        self.manager._emit = Mock(side_effect=RuntimeError('private-diagnostic-text'))
        error = self.assert_failure('KEY_DIAGNOSTIC_FAILED')
        self.assertNotIn('private-diagnostic-text', str(error))
        self.page.goto.assert_not_called()
        self.assertEqual(self.callbacks, {})

    def test_new_capture_discards_previous_authorization_but_preserves_attempts(self):
        self.manager._observe(self.request())
        self.manager._attempted.add('synthetic-target')
        with self.manager.capture(self.page):
            self.assertEqual(self.manager._headers, {})
            self.assertEqual(self.manager._attempted, {'synthetic-target'})
            other_page = SimpleNamespace(context=self.context)
            with self.assertRaises(KeyPreparationError) as caught:
                with self.manager.capture(other_page):
                    self.fail('A different page must not join an active capture scope')
            self.assertEqual(caught.exception.reason_code, 'KEY_SESSION_MISMATCH')
        self.assertEqual(self.callbacks, {})

    def test_empty_device_model_is_accepted_by_real_preparation_path_with_safe_metadata(self):
        request = self.request(extra={'x-device-model': ''})
        self.page.goto.side_effect = lambda url, **kwargs: (self.navigate(url, **kwargs), self.emit_request(request))
        self.manager.prepare(self.page)
        self.assertEqual(self.manager._headers['x-device-model'], '')
        event = next(fields for name, fields in self.events if name == 'key_http_empty_header_accepted')
        self.assertEqual(event['header_name'], 'x-device-model')
        self.assertEqual(event['matched_request_count'], 1)
        self.assertEqual(event['source_kind'], 'key_list')
        self.assertIs(event['bearer_format_passed'], True)
        self.assertNotIn(SESSION, json.dumps(self.events))
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_failed_capture_and_changed_session_clear_material_without_resetting_attempts(self):
        self.manager._attempted.add('earlier-attempt')
        with self.assertRaisesRegex(GuardError, 'Synthetic interruption'):
            with self.manager.capture(self.page):
                self.manager._observe(self.request(extra={'x-device-model': ''}))
                self.manager._issued = object()
                self.emit_request(self.request())
                raise GuardError('Synthetic interruption')
        self.assertEqual(self.manager._headers, {})
        self.assertIsNone(self.manager._issued)
        self.assertEqual(self.manager._requests, [])
        self.assertEqual(self.manager._attempted, {'earlier-attempt'})
        self.assertEqual(self.callbacks, {})
        self.manager._observe(self.request())
        self.manager._issued = object()
        self.page.context = object()
        self.assert_failure('KEY_SESSION_MISMATCH')
        self.assertIsNone(self.manager._issued)
        self.assertEqual(self.manager._requests, [])
        self.assertEqual(self.manager._attempted, {'earlier-attempt'})

    def test_header_rejection_keeps_metadata_before_clearing_candidate_material(self):
        request = self.request(extra={'x-device-id': ''})
        self.page.goto.side_effect = lambda url, **kwargs: (self.navigate(url, **kwargs), self.emit_request(request))
        self.assert_failure('KEY_CLIENT_HEADER_INVALID')
        event = next(fields for name, fields in self.events if name == 'key_http_client_header_rejected')
        self.assertEqual((event['header_name'], event['rejection_kind']), ('x-device-id', 'empty'))
        self.assertEqual(event['matched_request_count'], 1)
        self.assertEqual(event['source_kind'], 'key_list')
        self.assertEqual(self.manager._requests, [])
        self.assertEqual(self.manager._matched_count, 0)
        self.assertEqual([call.args[0] for call in self.page.goto.call_args_list], [KEY_PAGE])


def synthetic_document(*, notice=False, filled=False, no_request=False):
    """Independent popup fixture: Key GET is emitted only after dismissal."""
    options = json.dumps({'notice': notice, 'filled': filled, 'noRequest': no_request})
    return '''<!doctype html><meta charset="utf-8">
<style>.ds-modal {position:fixed;inset:0;display:grid;place-items:center;background:#ccc}
.ds-modal-content {background:white;padding:20px}</style>
<main>Only synthetic Key bootstrap</main>
<script>
const options = ''' + options + ''';
window.events = [];
function readKeys() {
  if (options.noRequest) return;
  fetch('/api/v0/users/get_api_keys', {headers: {Authorization: 'Bearer synthetic-browser-session'}})
    .then(() => events.push('list-received'));
}
if (!options.notice) readKeys();
else {
  const outer = document.createElement('div');
  outer.className='ds-modal'; outer.setAttribute('aria-modal','true');
  outer.innerHTML = `<div class="ds-modal-content" role="dialog">
    <div><div class="ds-modal-content__title">绑定邮箱</div></div>
    <p>我们会通过该邮箱为你推送 DeepSeek API 的功能动态、商业信息和产品通知。</p>
    <input placeholder="请输入邮箱"><input placeholder="请输入验证码">
    <button id="send">发送验证码</button><button id="later">稍后再填</button><button id="bind">绑定</button>
    </div>`;
  document.body.append(outer);
  if (options.filled) outer.querySelector('input').value='private-synthetic-input';
  document.querySelector('#send').onclick=()=>events.push('send');
  document.querySelector('#bind').onclick=()=>events.push('bind');
  document.querySelector('#later').onclick=()=>{events.push('later');outer.remove();readKeys();};
}
</script>'''


class KeyBootstrapBrowserTests(unittest.TestCase):
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
        self.document = synthetic_document()
        self.received = []
        self.events = []
        self.api = Mock(spec=['get', 'post'])
        self.api.get.side_effect = AssertionError('Bootstrap must not call the API transport')
        self.api.post.side_effect = AssertionError('Bootstrap must never submit a mutation')
        self.context = self.browser.new_context(service_workers='block')
        # Browser routing does not intercept APIRequestContext. Guard that
        # independent transport too, so a regression cannot reach a live URL.
        self.api_patch = patch.object(type(self.context), 'request', new_callable=PropertyMock, return_value=self.api)
        self.api_patch.start()
        self.context.route('**/*', self.route)
        self.page = self.context.new_page()
        self.page.set_default_timeout(500)
        self.manager = ConsoleKeyManager(self.context,
            emit=lambda event, **fields: self.events.append((event, fields)))

    def tearDown(self):
        self.manager.clear()
        self.context.close()
        self.api_patch.stop()

    def route(self, route):
        self.received.append((route.request.method, route.request.url))
        if route.request.method != 'GET':
            route.abort()
        elif route.request.url in (KEY_PAGE, USAGE_PAGE):
            route.fulfill(status=200, content_type='text/html', body=self.document)
        elif route.request.url == KEY_LIST:
            route.fulfill(status=200, content_type='application/json',
                          body='{"code":0,"data":{"biz_code":0,"biz_data":{"api_keys":[]}}}')
        else:
            route.abort()

    def assert_no_writes(self):
        self.assertTrue(all(method == 'GET' for method, _url in self.received))
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_empty_email_reminder_after_reload_is_dismissed_before_list_request(self):
        self.document = synthetic_document(notice=True)
        self.manager.prepare(self.page)
        self.assertEqual(self.manager._headers['Authorization'], 'Bearer synthetic-browser-session')
        self.assertEqual(self.page.evaluate('events.filter(event => event !== "list-received")'), ['later'])
        self.assertEqual(self.page.locator('.ds-modal').count(), 0)
        self.assertIn(('GET', KEY_LIST), self.received)
        self.assert_no_writes()

    def test_filled_email_reminder_preserves_input_and_never_requests_list_or_writes(self):
        self.document = synthetic_document(notice=True, filled=True)
        with self.assertRaises(KeyPreparationError) as caught:
            self.manager.prepare(self.page)
        self.assertEqual(caught.exception.reason_code, 'KEY_NOTICE_REQUIRES_USER')
        self.assertEqual(self.page.locator('input').first.input_value(), 'private-synthetic-input')
        self.assertEqual(self.page.evaluate('events'), [])
        self.assertNotIn(('GET', KEY_LIST), self.received)
        self.assertNotIn('private-synthetic-input', str(caught.exception) + json.dumps(self.events))
        self.assert_no_writes()

    def test_no_reminder_first_navigation_is_reused(self):
        with self.manager.capture(self.page):
            self.page.goto(KEY_PAGE)
            self.page.wait_for_function('events.includes("list-received")')
            self.manager.prepare(self.page)
        self.assertEqual(self.received.count(('GET', KEY_PAGE)), 1)
        self.assertEqual(self.received.count(('GET', KEY_LIST)), 1)
        self.assertEqual(self.manager._headers['Authorization'], 'Bearer synthetic-browser-session')
        self.assert_no_writes()

    def test_no_requests_times_out_without_clicks_or_writes(self):
        self.document = synthetic_document(no_request=True)
        with patch.multiple('quota_guard.platform_keys', LIST_BOOTSTRAP_MS=300, BOOTSTRAP_TIMEOUT_MS=800):
            with self.assertRaises(KeyPreparationError) as caught:
                self.manager.prepare(self.page)
        self.assertIn(caught.exception.reason_code, ('KEY_READ_REQUEST_NOT_OBSERVED', 'KEY_BOOTSTRAP_TIMEOUT'))
        self.assertEqual(self.page.evaluate('events'), [])
        self.assertEqual(self.manager._headers, {})
        self.assert_no_writes()


if __name__ == '__main__':
    unittest.main()
