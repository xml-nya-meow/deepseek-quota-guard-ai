"""Offline lifecycle mocks and routed Chromium login-preservation checks."""
from concurrent.futures import Future
import json
import os
from pathlib import Path
from queue import Queue
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from quota_guard.browser_service import BASE, BrowserService, check_identity, key_fingerprint
from quota_guard.core import AccountMismatch, GuardError
from quota_guard.revocation_permit import KeyInspectionError
from quota_guard.usage_page import select_usage_date_range


def browser_stack(label):
    page = Mock(name=f"{label}-page", spec=["is_closed", "goto", "set_default_timeout",
                                          "locator", "get_by_role", "add_locator_handler"])
    page.is_closed.return_value = False
    context = Mock(name=f"{label}-context", spec=["new_page", "close"])
    context.new_page.return_value = page
    browser = Mock(name=f"{label}-browser", spec=["is_connected", "new_context", "close", "version"])
    browser.is_connected.return_value = True
    browser.new_context.return_value = context
    browser.version = "fixture-browser-version"
    chromium = Mock(name=f"{label}-chromium", spec=["launch"])
    chromium.launch.return_value = browser
    playwright = Mock(name=f"{label}-playwright", spec=["chromium", "stop"])
    playwright.chromium = chromium
    entry = Mock(name=f"{label}-entry", spec=["start"])
    entry.start.return_value = playwright
    return SimpleNamespace(page=page, context=context, browser=browser,
                           playwright=playwright, entry=entry)


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        # Construct without starting BrowserService's worker thread. Worker
        # tests below run its queue loop synchronously with an explicit sentinel.
        self.service = object.__new__(BrowserService)
        self.service.queue = Queue()
        self.service.closed = False
        self.service.pw = self.service.context = None
        self.service.page = self.service.browser = None
        self.service.anchor = None
        self.service.known_keys = set()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        # These lifecycle tests mock the driver itself, so keep them independent
        # of the host's current local TCP permissions as well.
        loopback = patch('quota_guard.browser_service.check_browser_loopback')
        loopback.start()
        self.addCleanup(loopback.stop)

    def install_stack(self, stack):
        self.service.pw = stack.playwright
        self.service.browser = stack.browser
        self.service.context = stack.context
        self.service.page = stack.page
        self.service.anchor = "fixture-account-anchor"
        self.service.known_keys = {"fixture-known-key"}

    def assert_disposed(self):
        for attribute in ("pw", "browser", "context", "page"):
            self.assertIsNone(getattr(self.service, attribute), attribute)
        self.assertFalse(self.service.anchor)
        self.assertEqual(self.service.known_keys, set())

    def test_dispose_is_idempotent_and_clears_session_identity(self):
        stale = browser_stack("stale")
        self.install_stack(stale)

        self.service._dispose_browser()
        self.service._dispose_browser()

        stale.context.close.assert_called_once_with()
        stale.browser.close.assert_called_once_with()
        stale.playwright.stop.assert_called_once_with()
        self.assert_disposed()

    def test_dispose_continues_after_close_errors_and_stays_idempotent(self):
        stale = browser_stack("already-closed")
        self.install_stack(stale)
        stale.context.close.side_effect = RuntimeError("fixture context already closed")
        stale.browser.close.side_effect = RuntimeError("fixture browser disconnected")
        stale.playwright.stop.side_effect = RuntimeError("fixture driver stopped")

        self.service._dispose_browser()
        self.service._dispose_browser()

        stale.context.close.assert_called_once_with()
        stale.browser.close.assert_called_once_with()
        stale.playwright.stop.assert_called_once_with()
        self.assert_disposed()

    def test_relogin_always_replaces_context_and_clears_old_identity(self):
        stale, first, second = (browser_stack(label) for label in ("stale", "first", "second"))
        self.install_stack(stale)
        with patch("playwright.sync_api.sync_playwright", side_effect=[first.entry, second.entry]):
            self.service._login()
            self.assertIs(self.service.context, first.context)
            self.assertFalse(self.service.anchor)
            self.assertEqual(self.service.known_keys, set())
            self.service.anchor = "fixture-first-login-anchor"
            self.service.known_keys = {"fixture-first-login-key"}

            self.service._login()

        self.assertIs(self.service.context, second.context)
        self.assertIs(self.service.page, second.page)
        self.assertFalse(self.service.anchor)
        self.assertEqual(self.service.known_keys, set())
        stale.context.close.assert_called_once_with()
        first.context.close.assert_called_once_with()
        second.context.close.assert_not_called()
        stale.page.goto.assert_not_called()
        for fresh in (first, second):
            fresh.page.goto.assert_called_once()
            fresh.page.add_locator_handler.assert_called_once()
            self.assertEqual(fresh.page.goto.call_args.args[0], BASE + "/api_keys")
            self.assertNotIn("storage_state", fresh.browser.new_context.call_args.kwargs)

    def test_ensure_reuses_a_live_browser_without_resetting_identity(self):
        live = browser_stack("live")
        self.install_stack(live)
        with patch("playwright.sync_api.sync_playwright") as start:
            self.service._ensure_browser()

        start.assert_not_called()
        self.assertIs(self.service.page, live.page)
        self.assertEqual(self.service.anchor, "fixture-account-anchor")
        self.assertEqual(self.service.known_keys, {"fixture-known-key"})
        live.context.close.assert_not_called()

    def test_ensure_rebuilds_after_window_close_or_browser_disconnect(self):
        for closed_page, connected_browser in ((True, True), (False, False)):
            with self.subTest(page_closed=closed_page, browser_connected=connected_browser):
                stale, fresh = browser_stack("stale"), browser_stack("fresh")
                self.install_stack(stale)
                stale.page.is_closed.return_value = closed_page
                stale.browser.is_connected.return_value = connected_browser
                with patch("playwright.sync_api.sync_playwright", return_value=fresh.entry):
                    self.service._ensure_browser()

                self.assertIs(self.service.page, fresh.page)
                self.assertIs(self.service.context, fresh.context)
                self.assertFalse(self.service.anchor)
                self.assertEqual(self.service.known_keys, set())
                stale.context.close.assert_called_once_with()
                stale.browser.close.assert_called_once_with()
                stale.playwright.stop.assert_called_once_with()
                fresh.context.new_page.assert_called_once_with()

    def test_worker_login_creates_only_one_session_then_disposes_on_shutdown(self):
        fresh = browser_stack("worker-login")
        future = Future()
        self.service.queue.put(("login", {}, future))
        self.service.queue.put(None)
        with patch("playwright.sync_api.sync_playwright", return_value=fresh.entry), \
                patch.object(self.service, "_ensure_browser", wraps=self.service._ensure_browser) as ensure:
            self.service._worker()

        self.assertIn("message", future.result())
        ensure.assert_called_once_with()
        fresh.playwright.chromium.launch.assert_called_once()
        fresh.context.close.assert_called_once_with()
        self.assert_disposed()

    def test_worker_preserves_account_mismatch_exception_type(self):
        failure = AccountMismatch("fixture account continuity mismatch")
        future = Future()
        self.service.queue.put(("collect", {}, future))
        self.service.queue.put(None)
        with patch.object(self.service, "_ensure_browser"), \
                patch.object(self.service, "_collect", side_effect=failure):
            self.service._worker()

        self.assertIs(future.exception(), failure)
        with self.assertRaises(AccountMismatch):
            future.result()
        self.assert_disposed()

    def test_worker_non_login_action_still_ensures_browser(self):
        fresh = browser_stack("worker-selftest")
        future = Future()
        self.service.queue.put(("selftest", {}, future))
        self.service.queue.put(None)
        with patch("playwright.sync_api.sync_playwright", return_value=fresh.entry), \
                patch.object(self.service, "_ensure_browser", wraps=self.service._ensure_browser) as ensure:
            self.service._worker()

        self.assertEqual(future.result(), {"browser_started": True, "version": "fixture-browser-version"})
        ensure.assert_called_once_with()
        fresh.page.goto.assert_called_once_with("about:blank")
        self.assert_disposed()

    def test_worker_admits_read_only_inspection_without_permit(self):
        future = Future()
        expected = {'preflight_passed': True, 'name': 'Synthetic',
                    'tracking_id': '00000000-0000-0000-0000-000000000001', 'key_count': 2}
        args = {key: expected[key] for key in ('name', 'tracking_id')}
        self.service.queue.put(('inspect_revoke', args, future))
        self.service.queue.put(None)
        with patch.object(self.service, '_ensure_browser') as ensure, \
                patch.object(self.service, '_inspect_revoke', return_value=expected) as inspect:
            self.service._worker()
        ensure.assert_called_once_with()
        inspect.assert_called_once_with(**args)
        self.assertEqual(future.result(), expected)

    def test_inspection_worker_session_and_untyped_failures_are_sanitized(self):
        for stage, code in (('session', 'KEY_SESSION_UNAVAILABLE'), ('action', 'PREPARATION_FAILED')):
            with self.subTest(stage=stage):
                future = Future()
                self.service.queue.put(('inspect_revoke', {'name': 'Synthetic', 'tracking_id': 'synthetic'}, future))
                self.service.queue.put(None)
                with patch.object(self.service, '_ensure_browser',
                                  side_effect=RuntimeError('private SDK session text') if stage == 'session' else None), \
                        patch.object(self.service, '_inspect_revoke', side_effect=GuardError('private SDK action text')):
                    self.service._worker()
                self.assertIsInstance(future.exception(), KeyInspectionError)
                self.assertEqual(future.exception().reason_code, code)
                self.assertNotIn('private SDK', str(future.exception()))

    def test_identity_mismatch_is_distinct_for_unrelated_or_missing_anchors(self):
        initial = [{"name": "Fixture A", "tracking_id": "fixture-key-a"},
                   {"name": "Fixture B", "tracking_id": "fixture-key-b"}]
        original = key_fingerprint(initial)
        cases = (
            ([{"name": "Fixture other", "tracking_id": "fixture-other-key"}], {"fixture-key-b"}),
            ([], {"fixture-key-b"}),
            (initial, set()),
        )
        for keys, known_keys in cases:
            with self.subTest(keys=keys, known_keys=known_keys):
                with self.assertRaises(AccountMismatch):
                    check_identity(keys, original, known_keys)

        self.assertEqual(check_identity(initial[1:], original, {"fixture-key-b"}), original)
        self.assertEqual(check_identity(initial, "", set()), key_fingerprint(initial))


class BrowserLoginGuardChromiumTests(unittest.TestCase):
    """Use real DOM/action behavior, with every request fulfilled or aborted."""
    @classmethod
    def setUpClass(cls):
        runtime = Path(__file__).resolve().parents[1] / 'browser-runtime'
        if not runtime.is_dir():
            runtime = Path(__file__).resolve().parents[2] / 'browser-runtime'
        cls.environment = patch.dict(os.environ)
        cls.environment.start()
        if runtime.is_dir():
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(runtime)
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True, executable_path=cls.pw.chromium.executable_path)
        except Exception:
            cls.pw.stop()
            cls.environment.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.environment.stop()

    def setUp(self):
        self.document = ''
        self.navigations = []
        self.blocked_requests = []
        self.context = self.browser.new_context()
        self.context.route('**/*', self.route_request)
        self.page = self.context.new_page()
        self.page.set_default_timeout(3000)
        self.service = object.__new__(BrowserService)
        self.service.page = self.page
        self.service._login_waiting = True
        self.service.anchor = 'synthetic-original-identity'
        self.service.known_keys = {'synthetic-original-key'}

    def tearDown(self):
        self.context.close()

    def route_request(self, route):
        if route.request.is_navigation_request() and route.request.resource_type == 'document':
            self.navigations.append(route.request.url)
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=self.document)
        else:
            self.blocked_requests.append(route.request.resource_type)
            route.abort()

    def load(self, path, heading='<h1>登录</h1>'):
        self.document = ('<!doctype html><meta charset="utf-8">' + heading +
            '<form><input id="phone" autocomplete="tel"><input id="code" autocomplete="one-time-code" '
            'placeholder="请输入验证码"><button>登录</button></form>'
            '<script>window.fixtureMarker = "original-login-document";</script>')
        self.page.goto(BASE + path, wait_until='domcontentloaded')
        self.page.locator('#phone').fill('synthetic-phone-entry')
        self.page.locator('#code').fill('synthetic-partial-code')

    def assert_login_is_preserved(self):
        original_url = self.page.url
        original_navigations = list(self.navigations)
        started = time.monotonic()
        with patch.object(self.page, 'goto', wraps=self.page.goto) as goto, \
                patch.object(self.service, '_keys') as keys:
            for _ in range(2):
                with self.assertRaisesRegex(GuardError, '完成登录.*当前登录页面已保留'):
                    self.service._collect('2026-10-29')
            goto.assert_not_called()
            keys.assert_not_called()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.page.url, original_url)
        self.assertEqual(self.navigations, original_navigations)
        self.assertEqual(self.page.locator('#phone').input_value(), 'synthetic-phone-entry')
        self.assertEqual(self.page.locator('#code').input_value(), 'synthetic-partial-code')
        self.assertEqual(self.page.evaluate('window.fixtureMarker'), 'original-login-document')
        self.assertTrue(self.service._login_waiting)
        self.assertEqual(self.service.anchor, 'synthetic-original-identity')
        self.assertEqual(self.service.known_keys, {'synthetic-original-key'})

    def test_collect_on_login_url_returns_immediately_without_navigating_or_clearing_code(self):
        self.load('/sign_in')
        self.assert_login_is_preserved()

    def test_login_url_is_rejected_even_with_an_api_keys_heading(self):
        self.load('/sign_in', '<h1>API keys</h1>')
        self.assert_login_is_preserved()

    def test_unfinished_code_form_on_api_keys_route_is_preserved(self):
        self.load('/api_keys', '<h1>完成验证码登录</h1>')
        self.assert_login_is_preserved()

    def test_hidden_or_duplicate_api_keys_heading_does_not_end_login_wait(self):
        for heading in ('<h1 hidden>API keys</h1>', '<h1>API keys</h1><h1>API keys</h1>'):
            with self.subTest(heading=heading):
                self.load('/api_keys', heading)
                self.assert_login_is_preserved()

    def test_visible_api_keys_heading_on_correct_route_allows_collection(self):
        self.document = '<!doctype html><meta charset="utf-8"><h1>API keys</h1>'
        self.page.goto(BASE + '/api_keys', wait_until='domcontentloaded')
        with patch.object(self.page, 'goto', wraps=self.page.goto) as goto:
            self.service._check_login_ready()
            goto.assert_not_called()
        self.assertFalse(self.service._login_waiting)
        with patch.object(self.service, '_keys', return_value=[]) as keys:
            # Empty synthetic keys stop the later workflow, after the login
            # boundary has allowed collection to enter the actual keys step.
            with self.assertRaisesRegex(GuardError, '没有可绑定的 Key'):
                self.service._collect('2026-10-29')
            keys.assert_called_once_with()

    def test_usage_stage_metadata_has_only_structural_state_and_no_page_values(self):
        try:
            from .test_usage_page import usage_document
        except ImportError:
            from test_usage_page import usage_document
        self.page.set_content(usage_document(unfiltered=True) +
            '<div hidden>sk-synthetic-sensitive-value</div>'
            '<input type="password" value="synthetic-password-value">')
        captured = []
        self.service.diagnostics = SimpleNamespace(emit=lambda event, **fields: captured.append((event, fields)))
        select_usage_date_range(self.page, '2026-10-29', '2026-10-30', timeout_ms=2500,
                               on_stage=self.service._stage)
        self.assertTrue(captured)
        for event, fields in captured:
            self.assertEqual(event, 'browser_stage')
            self.assertEqual(set(fields), {'stage', 'state', 'count', 'elapsed'})
            self.assertIsInstance(fields['state'], str)
            self.assertIsInstance(fields['count'], int)
            self.assertIsInstance(fields['elapsed'], (int, float))
        serialized = json.dumps(captured, ensure_ascii=False)
        for forbidden in ('sk-synthetic-sensitive-value', 'synthetic-password-value',
                          '2026-10-29', '2026-10-30', '<input', 'fixtureMarker'):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
