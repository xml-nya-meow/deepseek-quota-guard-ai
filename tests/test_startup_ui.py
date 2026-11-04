"""Startup failures and rejected force actions must be visible and retryable."""
from decimal import Decimal
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from quota_guard.core import GuardError, Person


class StartupUiTests(unittest.TestCase):
    # Reuse setup methods only. Importing/subclassing WorkflowTests directly
    # would cause its entire test collection to run again in this module.
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump

    def assert_no_delete_intent(self):
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))
        self.assertIsNone(self.app.delete_permit)
        self.assertIsNone(self.app.single_revoke_id)
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)

    def test_login_communication_failure_releases_ui_shows_once_and_allows_retry(self):
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.login()
        self.pump()
        self.assertEqual([action for action, _ in browser.calls], ['login'])
        self.assertTrue(self.app.busy)
        self.assertEqual(self.app.current_action, 'login')

        def error_after_release(*args, **kwargs):
            self.assertFalse(self.app.busy)
            self.assertIsNone(self.app.current_action)
            self.assertFalse(self.app.pending_login)
            self.assertFalse(self.app.buttons['login'].instate(['disabled']))

        self.mocks[1].side_effect = error_after_release
        message = '本机浏览器通信不可用：本地回环连接失败，请检查后重试。'
        browser.futures[0].set_exception(GuardError(message))
        self.pump()
        self.mocks[1].assert_called_once()
        self.assertIn(message, self.mocks[1].call_args.args[1])
        self.assertIn(message, self.app.status_var.get())
        self.assertEqual(self.app.action_states['login'], '失败')
        self.assertFalse(self.app.session_ready)
        self.assert_no_delete_intent()

        self.app.buttons['login'].invoke()
        self.pump()
        self.assertEqual([action for action, _ in browser.calls], ['login', 'login'])
        self.assertTrue(self.app.busy)
        self.assertFalse(self.app.pending_login)
        browser.futures[1].set_result({})
        self.pump()
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.current_action)
        self.assertFalse(self.app.pending_login)
        self.assertFalse(self.app.buttons['login'].instate(['disabled']))
        self.assertIn('待登录', self.app.buttons['login'].cget('text'))
        self.mocks[1].assert_called_once()
        self.assert_no_delete_intent()

    def test_force_without_selection_shows_reason_and_never_dispatches(self):
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.tree.selection_remove(*self.app.tree.selection())
        with patch.object(self.app, '_browser_action') as dispatch:
            self.app.revoke_selected_once()
        dispatch.assert_not_called()
        self.assertEqual(browser.calls, [])
        self.mocks[1].assert_called_once()
        self.assertIn('填写平台 Key 名称并选中', self.mocks[1].call_args.args[1])
        self.assertIn('没有发出删除请求', self.mocks[1].call_args.args[1])
        self.assertFalse(self.app.busy)
        self.assert_no_delete_intent()

    def test_force_with_unnamed_zero_row_shows_reason_and_never_dispatches(self):
        self.store.save_people([Person('', Decimal(0))], allow_unassigned=True)
        self.app.load_folder()
        self.app.tree.selection_set('0')
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        with patch.object(self.app, '_browser_action') as dispatch:
            self.app.revoke_selected_once()
        dispatch.assert_not_called()
        self.assertEqual(browser.calls, [])
        self.mocks[1].assert_called_once()
        self.assertIn('填写平台 Key 名称并选中', self.mocks[1].call_args.args[1])
        self.assert_no_delete_intent()

    def test_force_before_login_shows_reason_and_never_dispatches(self):
        self.assertIsNone(self.app.browser)
        self.app.tree.selection_set('0')
        with patch.object(self.app, '_browser_action') as dispatch:
            self.app.revoke_selected_once()
        dispatch.assert_not_called()
        self.mocks[1].assert_called_once()
        self.assertIn('请先点击登录', self.mocks[1].call_args.args[1])
        self.assertIn('没有发出删除请求', self.mocks[1].call_args.args[1])
        self.assertIsNone(self.app.browser)
        self.assertFalse(self.app.busy)
        self.assert_no_delete_intent()


if __name__ == '__main__':
    unittest.main()
