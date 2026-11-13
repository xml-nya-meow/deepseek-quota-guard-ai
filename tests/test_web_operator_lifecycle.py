"""An unconfirmed model shutdown must survive login cleanup and block reuse."""
import time
import unittest
from unittest.mock import Mock, patch

from quota_guard.browser_service import BrowserService
from quota_guard.operator_runtime import OperatorError
from test_browser_session import browser_stack


class OperatorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.service=object.__new__(BrowserService)
        self.old=browser_stack('owned-old')
        self.service.context=self.old.context
        self.service.browser=self.old.browser
        self.service.page=self.old.page
        self.service.pw=self.old.playwright
        self.service.anchor='synthetic-anchor'
        self.service.known_keys={'synthetic-id'}
        self.runtime=Mock(spec=['close','start'])
        self.service._operator_runtime=self.runtime
        self.reader=Mock(spec=['clear'])
        self.service._cost_reader=self.reader
        self.service._cost_reader_identity='synthetic-reader'
        self.loopback=patch('quota_guard.browser_service.check_browser_loopback')
        self.loopback.start();self.addCleanup(self.loopback.stop)

    def assert_browser_cleaned(self):
        self.old.context.close.assert_called_once_with()
        self.old.browser.close.assert_called_once_with()
        self.old.playwright.stop.assert_called_once_with()
        for field in ('context','browser','page','pw','anchor'):
            self.assertIsNone(getattr(self.service,field))
        self.assertEqual(self.service.known_keys,set())
        self.assertIsNone(self.service._cost_reader_identity)

    def assert_pending(self, action):
        with self.assertRaises(OperatorError) as caught:action()
        self.assertEqual(caught.exception.reason_code,'AI_CLEANUP_PENDING')
        self.assertNotIn('synthetic-private',str(caught.exception))
        self.assertTrue(self.service._operator_cleanup_pending)

    def test_close_failure_keeps_original_runtime_and_cleans_all_browser_layers(self):
        self.runtime.close.side_effect=RuntimeError('Bearer synthetic-private')
        self.assert_pending(self.service._dispose_browser)
        self.assertIs(self.service._operator_runtime,self.runtime)
        self.assertIsNone(self.service._cost_reader)
        self.reader.clear.assert_called_once_with()
        self.assert_browser_cleaned()
        with patch('quota_guard.browser_service.OperatorRuntime') as construct:
            self.assert_pending(lambda:self.service._get_operator_runtime(deadline=time.monotonic()+5))
        construct.assert_not_called();self.runtime.start.assert_not_called()

    def test_successful_retry_releases_original_reference_only_after_close(self):
        self.runtime.close.side_effect=[RuntimeError('synthetic-private'),None]
        self.assert_pending(self.service._dispose_browser)
        self.assertIs(self.service._operator_runtime,self.runtime)
        self.service._dispose_browser()
        self.assertIsNone(self.service._operator_runtime)
        self.assertFalse(self.service._operator_cleanup_pending)
        self.service._dispose_browser()
        self.assertEqual(self.runtime.close.call_count,2)
        self.assert_browser_cleaned()

    def test_failed_relogin_does_not_launch_replacement_and_can_retry_cleanup(self):
        self.runtime.close.side_effect=[RuntimeError('synthetic-private'),None]
        fresh=browser_stack('replacement-after-confirmed-exit')
        with patch('playwright.sync_api.sync_playwright',return_value=fresh.entry) as launch, \
                patch('quota_guard.browser_service.OperatorRuntime') as construct:
            self.assert_pending(self.service._login)
            launch.assert_not_called();construct.assert_not_called()
            self.assertIs(self.service._operator_runtime,self.runtime)
            self.service._login()
            launch.assert_called_once_with()
            construct.assert_not_called()  # model stays lazy until a business operation
        self.assertEqual(self.runtime.close.call_count,2)
        self.assertIsNone(self.service._operator_runtime)
        self.assertIs(self.service.context,fresh.context)

    def test_reader_failure_does_not_skip_runtime_or_browser_cleanup(self):
        self.reader.clear.side_effect=[RuntimeError('synthetic-private'),None]
        self.assert_pending(self.service._dispose_browser)
        self.assertIs(self.service._cost_reader,self.reader)
        self.runtime.close.assert_called_once_with()
        self.assertIsNone(self.service._operator_runtime)
        self.assert_browser_cleaned()
        self.assert_pending(lambda:self.service._get_operator_runtime(deadline=time.monotonic()+5))
        self.service._dispose_browser()
        self.assertIsNone(self.service._cost_reader)
        self.assertFalse(self.service._operator_cleanup_pending)
        self.assertEqual(self.reader.clear.call_count,2)

    def test_reader_and_runtime_failures_are_independently_retried(self):
        self.reader.clear.side_effect=[RuntimeError('reader'),RuntimeError('reader'),None]
        self.runtime.close.side_effect=[RuntimeError('model'),None]
        self.old.context.close.side_effect=RuntimeError('already closed')
        self.assert_pending(self.service._dispose_browser)
        self.assertIs(self.service._operator_runtime,self.runtime)
        self.assertIs(self.service._cost_reader,self.reader)
        self.assert_browser_cleaned()
        self.assert_pending(self.service._dispose_browser)
        self.assertIsNone(self.service._operator_runtime)
        self.assertIs(self.service._cost_reader,self.reader)
        self.service._dispose_browser()
        self.assertFalse(self.service._operator_cleanup_pending)
        self.assertEqual(self.runtime.close.call_count,2)
        self.assertEqual(self.reader.clear.call_count,3)

    def test_pending_cleanup_prevents_live_session_fast_path(self):
        self.service._operator_cleanup_pending=True
        self.runtime.close.side_effect=RuntimeError('synthetic-private')
        with patch('playwright.sync_api.sync_playwright') as launch:
            self.assert_pending(self.service._ensure_browser)
        launch.assert_not_called()
        self.assert_browser_cleaned()
        self.assertIs(self.service._operator_runtime,self.runtime)


if __name__=='__main__':unittest.main()
