"""Local startup failures are bounded and never attempt platform traffic."""
import errno
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quota_guard.browser_process import _worker_failure
from quota_guard.browser_service import BrowserService
from quota_guard.browser_startup import BrowserStartupError, check_browser_loopback


class LoopbackProbeTests(unittest.TestCase):
    def setUp(self):
        self.listener = Mock(name='private-listener')
        self.client = Mock(name='private-client')
        self.accepted = Mock(name='accepted')
        self.listener.getsockname.return_value = ('127.0.0.1', 43210)
        self.listener.accept.return_value = (self.accepted, ('127.0.0.1', 43211))
        sockets = patch('quota_guard.browser_startup.socket.socket',
                        side_effect=[self.listener, self.client])
        self.socket_factory = sockets.start()
        self.addCleanup(sockets.stop)
        pair = patch('quota_guard.browser_startup.socket.socketpair',
                     side_effect=AssertionError('The socketpair fallback must never be called'))
        pair.start()
        self.addCleanup(pair.stop)

    def test_success_uses_only_own_loopback_listener_and_closes_every_socket(self):
        with patch('quota_guard.browser_startup.time.monotonic', side_effect=[10, 10.1, 10.4, 11.2]):
            check_browser_loopback()
        self.listener.bind.assert_called_once_with(('127.0.0.1', 0))
        self.listener.listen.assert_called_once_with(1)
        self.client.connect.assert_called_once_with(('127.0.0.1', 43210))
        self.listener.accept.assert_called_once_with()
        self.assertAlmostEqual(self.listener.settimeout.call_args_list[0].args[0], 1.9)
        self.assertAlmostEqual(self.client.settimeout.call_args.args[0], 1.6)
        self.assertAlmostEqual(self.listener.settimeout.call_args_list[1].args[0], .8)
        for sock in (self.listener, self.client, self.accepted):
            sock.close.assert_called_once_with()

    def test_connect_error_is_fixed_safe_message_and_never_waits_in_accept(self):
        error = OSError(errno.ENETUNREACH, 'private network detail')
        error.winerror = 10051
        self.client.connect.side_effect = error
        with self.assertRaises(BrowserStartupError) as caught:
            check_browser_loopback()
        self.assertEqual(caught.exception.error_code, 10051)
        self.assertEqual(caught.exception.reason_code, 'BROWSER_LOOPBACK_UNAVAILABLE')
        self.assertNotIn('private', str(caught.exception))
        self.assertIn('本次未访问平台、未执行删除', str(caught.exception))
        self.listener.accept.assert_not_called()
        self.listener.close.assert_called_once_with()
        self.client.close.assert_called_once_with()

    def test_accept_timeout_closes_client_and_listener_and_reports_timeout(self):
        self.listener.accept.side_effect = TimeoutError('untrusted detail')
        with self.assertRaises(BrowserStartupError) as caught:
            check_browser_loopback()
        self.assertEqual(caught.exception.error_code, 'TIMEOUT')
        self.assertNotIn('untrusted', str(caught.exception))
        self.listener.close.assert_called_once_with()
        self.client.close.assert_called_once_with()

    def test_connect_consuming_budget_prevents_unbounded_accept(self):
        with patch('quota_guard.browser_startup.time.monotonic', side_effect=[10, 10.1, 11, 12.01]), \
                self.assertRaises(BrowserStartupError) as caught:
            check_browser_loopback()
        self.assertEqual(caught.exception.error_code, 'TIMEOUT')
        self.client.connect.assert_called_once()
        self.listener.accept.assert_not_called()
        self.listener.close.assert_called_once_with()
        self.client.close.assert_called_once_with()

    def test_socket_creation_failure_has_no_raw_exception_or_unknown_code(self):
        self.socket_factory.side_effect = OSError(987654, 'private exception detail')
        with self.assertRaises(BrowserStartupError) as caught:
            check_browser_loopback()
        self.assertEqual(caught.exception.error_code, 'UNKNOWN')
        self.assertNotIn('private', str(caught.exception))
        self.listener.bind.assert_not_called()

    def test_failure_code_accepts_only_allowlisted_numeric_errors_or_timeout(self):
        for value in (True, False, '10051', 987654, None, 'private detail', []):
            with self.subTest(value=value):
                self.assertEqual(BrowserStartupError(value).error_code, 'UNKNOWN')


class BrowserStartupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(BrowserService)
        self.service.pw = self.service.context = None
        self.service.page = self.service.browser = None
        self.service.anchor = None
        self.service.known_keys = set()
        self.service._stage = Mock()
        self.service._log = Mock()
        self.page = Mock()
        self.context = Mock()
        self.context.new_page.return_value = self.page
        self.browser = Mock()
        self.browser.new_context.return_value = self.context
        self.driver = Mock()
        self.driver.chromium.launch.return_value = self.browser
        self.entry = SimpleNamespace(start=Mock(return_value=self.driver))
        network = patch('socket.socket', side_effect=AssertionError('No real sockets in this test'))
        network.start()
        self.addCleanup(network.stop)

    def test_preflight_failure_prevents_driver_launch_and_keeps_references_empty(self):
        with patch('quota_guard.browser_service.check_browser_loopback',
                   side_effect=BrowserStartupError(10051)), \
                patch('playwright.sync_api.sync_playwright') as driver_entry:
            with self.assertRaises(BrowserStartupError):
                self.service._ensure_browser()
        driver_entry.assert_not_called()
        self.assertIsNone(self.service.pw)
        self.assertIsNone(self.service.browser)
        self.assertIsNone(self.service.context)
        self.assertIsNone(self.service.page)
        self.service._log.assert_called_once_with(
            'browser_startup_failed', phase='loopback',
            reason_code='BROWSER_LOOPBACK_UNAVAILABLE', error_code=10051)

    def test_startup_stages_precede_driver_and_chromium_and_retry_is_possible(self):
        with patch('quota_guard.browser_service.check_browser_loopback',
                   side_effect=[BrowserStartupError(10051), None]), \
                patch('playwright.sync_api.sync_playwright', return_value=self.entry), \
                patch('quota_guard.browser_service.install_optional_notice_handler'):
            with self.assertRaises(BrowserStartupError):
                self.service._ensure_browser()
            def start():
                self.assertEqual(self.service._stage.call_args.args[0], '正在初始化浏览器驱动')
                return self.driver
            self.entry.start.side_effect = start
            self.service._ensure_browser()
        self.driver.chromium.launch.assert_called_once_with(headless=False, timeout=30000)
        self.assertIs(self.service.page, self.page)
        self.page.goto.assert_not_called()
        self.assertEqual([call.args[0] for call in self.service._stage.call_args_list], [
            '正在检查本机浏览器通信', '正在检查本机浏览器通信', '正在初始化浏览器驱动', '正在启动独立浏览器'])

    def test_existing_live_session_does_not_run_startup_probe_again(self):
        self.service.browser = self.browser
        self.service.context = self.context
        self.service.page = self.page
        self.browser.is_connected.return_value = True
        self.page.is_closed.return_value = False
        with patch('quota_guard.browser_service.check_browser_loopback') as probe:
            self.service._ensure_browser()
        probe.assert_not_called()
        self.service._stage.assert_not_called()

    def test_fixed_login_error_survives_worker_boundary_without_raw_network_text(self):
        result = _worker_failure(BrowserStartupError(10051), 'login', None)
        self.assertEqual(result['error_type'], 'GuardError')
        self.assertIn('本机回环通信', result['message'])
        self.assertIn('重试', result['message'])
        self.assertNotIn('10051', result['message'])


if __name__ == '__main__':
    unittest.main()
