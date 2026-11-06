"""Exercise UI callbacks against the real ledger with an offline browser double."""
from concurrent.futures import Future
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from quota_guard.core import AccountMismatch, DataStore, Person, TZ, GuardError
from isolated_app import App
from quota_guard.ui import COLLECTION_MAX_AGE_SECONDS
from quota_guard.browser_service import check_identity, key_fingerprint
from quota_guard.workspace import current_ledger


def ready_with_inventory(app, permit):
    """Synthetic prepared HTTP/DOM evidence, separate from the later UI grant."""
    permit.set_prepared_inventory(permit.target_tracking_id,
                                  [key['tracking_id'] for key in app.last_keys])
    permit.ready.set()


class OfflineBrowser:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def submit(self, action, **kwargs):
        self.calls.append((action, kwargs))
        future = Future()
        future.set_result(self.results[action])
        return future

    def close(self):
        pass

    def interrupt(self):
        future = Future(); future.set_result(None)
        return future


class ControlledBrowser(OfflineBrowser):
    def __init__(self, *, identity_result=None):
        super().__init__({})
        self.futures = []
        self.identity_result = identity_result

    def submit(self, action, **kwargs):
        self.calls.append((action, kwargs))
        future = Future()
        if action == 'read_keys' and self.identity_result is not None:
            # The identity read completes normally; only outstanding actions
            # enter the queue controlled by each test.
            future.set_result(self.identity_result)
            return future
        self.futures.append(future)
        return future

    def interrupt(self):
        for future in self.futures:
            if not future.done():
                future.set_exception(GuardError('操作已中断；撤销结果未知，禁止自动重试。'))
        if hasattr(self, 'cleanup_future'):
            return self.cleanup_future
        return super().interrupt()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.start = datetime.now(TZ).date().isoformat()
        self.store = DataStore(self.folder)
        self.store.initialize(14, '1500', self.start)
        self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(80))])
        self.keys = [{'name': '测试甲', 'tracking_id': 'test-key-a'},
                     {'name': '测试乙', 'tracking_id': 'test-key-b'}]
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.folder)
        self.patches = [patch('quota_guard.ui.messagebox.showinfo'),
                        patch('quota_guard.ui.messagebox.showerror'),
                        patch('quota_guard.ui.messagebox.askyesno', return_value=True)]
        self.mocks = [p.start() for p in self.patches]
        self.pump()

    def tearDown(self):
        self.app.stop(quiet=True)
        if self.app.target_lock is not None:
            self.app.target_lock.close()
        self.app.folder_lock.close()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def pump(self):
        end = time.monotonic() + .2
        while time.monotonic() < end:
            self.root.update()
            time.sleep(.01)

    def valid_collection(self, **overrides):
        result = {'keys': self.keys, 'account_id': key_fingerprint(self.keys),
                'totals': {'test-key-a': Decimal(0), 'test-key-b': Decimal(5)},
                'source_id': 'valid-fixture', 'start_date': self.start, 'end_date': self.start,
                'collection_started_wall': time.time(),
                'collection_started_monotonic': time.monotonic()}
        result.update(overrides)
        return result

    def empty_collection(self, **overrides):
        result = self.valid_collection(totals=None, source_id='', message='暂无记录',
                                       reason_code='NO_USAGE_DATA', retryable=True)
        result.update(overrides)
        return result

    def identity_result(self, **overrides):
        result = {'keys': self.keys, 'account_id': key_fingerprint(self.keys)}
        result.update(overrides)
        return result

    def prime_verified_session(self):
        """Establish a current read-only identity check before a focused action test."""
        self.app.browser = OfflineBrowser({'collect': self.empty_collection()})
        self.app.collect()
        self.pump()
        self.assertTrue(self.app.session_ready)
        self.assertTrue(self.app._collection_is_current())

    def test_empty_then_revoke_result_is_logged_and_restorable(self):
        browser = OfflineBrowser({
            'collect': self.empty_collection(message='没有费用记录'),
            'prepare_revoke': {'message': '已定位'},
            'verify_revoke': {'absent': True},
        })
        self.app.browser = browser
        self.app.collect()
        self.pump()
        self.assertEqual(self.app.rows[0]['status'], 'triggered')
        self.assertIsNone(self.app.rows[1]['spent'])
        self.app.tree.selection_set('0')
        self.app.prepare_revoke()
        self.pump()
        self.app.verify_revoke()
        self.pump()
        states = [e['status'] for e in self.store.events() if e['kind'] == 'action']
        self.assertEqual(states, ['absent_verified', 'prepared', 'pending'])
        self.assertEqual(self.app.rows[0]['status'], 'retired')
        self.assertEqual(self.store.active_tracking_ids(), {'test-key-b'})
        self.mocks[1].assert_not_called()

    def test_changed_workbook_during_collection_is_not_applied(self):
        browser = OfflineBrowser({'collect': self.valid_collection(
            totals={'test-key-a': Decimal(100)}, source_id='fake')})
        self.app.browser = browser
        self.app.collect()
        self.store.save_people([Person('测试甲', Decimal(-1))])
        self.pump()
        self.assertEqual(self.store.active_tracking_ids(), set())
        self.mocks[1].assert_not_called()
        self.assertIn('表格发生变化', self.app.status_var.get())
        self.assertFalse(self.app.busy)

    def test_empty_report_keeps_one_observer_without_inventing_zero(self):
        self.store.save_people([Person('测试甲', Decimal(100)), Person('测试乙', Decimal(80))])
        self.app.browser = OfflineBrowser({'collect': self.valid_collection()})
        self.app.collect(); self.pump()
        self.app.start(); self.pump()
        self.app.browser.results['collect'] = self.empty_collection()
        self.app.collect()
        self.pump()
        first_timer = self.app.timer
        self.assertTrue(self.app.observing)
        self.assertIsNotNone(first_timer)
        self.app.collect()
        self.pump()
        self.assertNotEqual(self.app.timer, first_timer)
        self.assertNotIn(first_timer, self.root.tk.call('after', 'info'))
        self.assertEqual(self.app.rows[1]['spent'], Decimal(5))

    def test_automatic_zero_budget_is_logged_and_skipped_people_untouched(self):
        self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(-1))])
        browser = OfflineBrowser({'collect': self.valid_collection(), 'revoke': {'absent': True}})
        self.app.browser = browser
        self.app.collect(); self.pump()
        self.app.start()
        self.pump(); self.pump()
        calls = [(action, args.get('name')) for action, args in browser.calls]
        self.assertEqual(calls, [('collect', None), ('revoke', '测试甲')])
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual(self.app.rows[1]['status'], 'skipped')

    def test_unconfirmed_action_blocks_automatic_retry(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.store.record_action('测试甲', 'test-key-a', 'requested', 'synthetic interrupted attempt')
        self.app.browser = OfflineBrowser({'collect': self.valid_collection()})
        self.app.collect(); self.pump()
        self.app.start(); self.pump()
        self.assertFalse(self.app.observing)
        self.assertEqual([action for action, _ in self.app.browser.calls], ['collect'])
        self.assertEqual(self.app.rows[0]['status'], 'recovery_pending')

    def test_delete_permit_follows_durable_requested_record(self):
        browser = OfflineBrowser({'collect': self.valid_collection()})
        original = browser.submit
        observed = []
        def submit(action, **kwargs):
            if action != 'revoke':
                return original(action, **kwargs)
            future = Future()
            permit = kwargs['permit']
            def worker():
                ready_with_inventory(self.app, permit)
                if not permit.decided.wait(3):
                    future.set_exception(RuntimeError('No permit'))
                    return
                observed.append((permit.allowed, self.store.latest_action('test-key-a')))
                future.set_result({'absent': True})
            threading.Thread(target=worker, daemon=True).start()
            return future
        browser.submit = submit
        self.app.browser = browser
        self.app.collect(); self.pump()
        self.app.start()
        self.pump(); self.pump(); self.pump()
        self.assertEqual(observed, [(True, 'requested')])
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')

    def test_next_person_queue_error_does_not_rewrite_finished_person(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.store.record_action('测试甲', 'test-key-a', 'absent_verified', 'synthetic completed')
        self.app.automatic = self.app.observing = True
        with patch.object(self.app, '_process_next', side_effect=OSError('synthetic disk error')):
            self.app._schedule_processing()
            self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertFalse(self.app.observing)

    def test_display_failure_does_not_change_confirmed_absence_to_unknown(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.app.refresh_rows()
        self.app.tree.selection_set('0')
        self.app.browser = OfflineBrowser({'verify_revoke': {'absent': True}})
        with patch.object(self.app, 'refresh_rows', side_effect=OSError('synthetic display failure')):
            self.app.verify_revoke()
            self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')

    def test_close_invalidates_old_read_result_before_destroying(self):
        self.app.browser = OfflineBrowser({'collect': self.valid_collection(
            totals={'test-key-b': Decimal(5)}, source_id='close-result')})
        self.app.collect()
        with patch.object(self.root, 'destroy'):
            self.app.close()
            self.pump(); self.pump()
        self.assertIsNone(self.store.evaluate()[1]['spent'])
        self.assertFalse(self.app.busy)

    def test_identity_continues_with_survivor_but_rejects_other_account(self):
        original = key_fingerprint(self.keys)
        survivor = self.keys[1:]
        self.assertEqual(check_identity(survivor, original, {'test-key-b'}), original)
        with self.assertRaises(GuardError):
            check_identity([{'name': '测试乙', 'tracking_id': 'foreign-key'}], original, {'test-key-b'})
        with self.assertRaises(GuardError):
            check_identity(survivor, original, set())

    def test_relogin_discards_old_collection_and_coalesces_pending_requests(self):
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.collect()
        for _ in range(4):
            self.app.login()
        self.assertFalse(self.app.automatic)
        self.assertEqual([a for a, _ in browser.calls], ['collect'])
        browser.futures[0].set_result(self.valid_collection(
            totals={'test-key-b': Decimal(99)}, source_id='stale'))
        self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['collect', 'login'])
        self.assertEqual(self.store.active_tracking_ids(), set())
        self.assertFalse(any(e['kind'] == 'snapshot' for e in self.store.events()))
        browser.futures[1].set_result({})
        self.pump()
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.observing)
        self.mocks[1].assert_not_called()

    def test_superseded_login_failure_does_not_cancel_new_login(self):
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.login(); self.pump()
        self.app.login(); self.app.login()
        browser.futures[0].set_exception(GuardError('obsolete login failed'))
        self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['login', 'login'])
        browser.futures[1].set_result({})
        self.pump()
        self.mocks[1].assert_not_called()
        self.assertIn('新页面', self.app.status_var.get())

    def test_relogin_discards_preparation_completion_without_rewriting_action(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.prime_verified_session()
        self.app.tree.selection_set('0')
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.prepare_revoke()
        self.app.login()
        browser.futures[0].set_result({})
        self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'pending')
        self.assertEqual([a for a, _ in browser.calls], ['prepare_revoke', 'login'])
        browser.futures[1].set_result({}); self.pump()

    def test_relogin_waits_for_approved_revocation_and_persists_its_result(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.prime_verified_session()
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        permit = browser.calls[0][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        self.assertEqual(self.store.latest_action('test-key-a'), 'requested')
        self.app.login()
        self.assertEqual([a for a, _ in browser.calls], ['revoke'])
        browser.futures[0].set_result({'absent': True})
        self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual(self.store.active_tracking_ids(), {'test-key-b'})
        self.assertEqual([a for a, _ in browser.calls], ['revoke', 'login'])
        self.assertFalse(self.app.automatic)
        browser.futures[1].set_result({}); self.pump()

    def test_date_change_waits_for_verify_then_carries_final_status_and_restores_selection(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.app.refresh_rows(); self.app.tree.selection_set('0')
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.verify_revoke()
        date = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
        with patch('quota_guard.ui.ask_text', return_value=date):
            self.app.change_start_date()
        self.assertEqual(self.app.folder, self.folder)
        browser.futures[0].set_result({'absent': True})
        self.pump(); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual(self.app.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual(self.app.config.start_date, date)
        self.assertEqual(current_ledger(self.folder), self.app.folder)
        self.assertEqual(self.app.folder.parent, self.folder / '账本')
        self.assertEqual([a for a, _ in browser.calls], ['verify_revoke'])
        self.assertIs(self.app.browser, browser)
        self.assertFalse(self.app.observing)

    def test_verification_result_is_nonmodal_and_saved_before_relogin(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.app.refresh_rows(); self.app.tree.selection_set('0')
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.verify_revoke()
        browser.futures[0].set_result({'absent': True})
        self.pump()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.mocks[0].assert_not_called()
        self.app.login(); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['verify_revoke', 'login'])
        browser.futures[1].set_result({}); self.pump()

    def test_switch_pointer_failure_preserves_original_selection_and_lock(self):
        destination = self.folder / 'candidate'
        self.store.create_successor(destination)
        from quota_guard.instance import FolderLock
        with patch('quota_guard.ui.save_current_ledger', side_effect=OSError('pointer failure')):
            with self.assertRaises(OSError):
                self.app._switch_folder(destination)
        self.assertEqual(self.app.folder, self.folder)
        with self.assertRaises(GuardError):
            FolderLock(self.folder)
        released = FolderLock(destination); released.close()

    def test_close_cancels_queued_login_but_keeps_pending_verification_result(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.app.refresh_rows(); self.app.tree.selection_set('0')
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.verify_revoke(); self.app.login()
        with patch.object(self.root, 'destroy'):
            browser.futures[0].set_result({'absent': True})
            self.app.close()
            self.pump(); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual([a for a, _ in browser.calls], ['verify_revoke'])

    def test_template_cannot_overwrite_active_workbook(self):
        original = self.store.workbook_path.read_bytes()
        with patch('quota_guard.ui.filedialog.asksaveasfilename', return_value=str(self.store.workbook_path)), \
                patch('quota_guard.ui.write_personnel_template') as copy:
            self.app.save_template()
        copy.assert_not_called()
        self.assertEqual(self.store.workbook_path.read_bytes(), original)

    def test_template_saved_from_bundled_runtime_keeps_active_table_unchanged(self):
        original = self.store.workbook_path.read_bytes()
        runtime = self.folder / 'fixture-runtime'
        (runtime / 'templates').mkdir(parents=True)
        template = runtime / 'templates' / '人员额度模板.xlsx'
        template.write_bytes(original)
        output = self.folder / 'downloaded-template.xlsx'
        with patch('quota_guard.ui.sys._MEIPASS', str(runtime), create=True), \
                patch('quota_guard.ui.filedialog.asksaveasfilename', return_value=str(output)):
            self.app.save_template()
        from quota_guard.excel_io import read_people_workbook
        self.assertEqual(read_people_workbook(output), [Person('', Decimal(-1)) for _ in range(14)])
        self.assertEqual(template.read_bytes(), original)
        self.assertEqual(self.store.workbook_path.read_bytes(), original)

    def test_relogin_clears_unused_initial_binding_but_preserves_real_billing(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.login(); self.pump()
        self.assertEqual(self.store.load().account_id, '')
        self.assertEqual(self.store.active_bindings(), [])
        browser.futures[0].set_result({}); self.pump()
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.store.apply_snapshot({'test-key-b': Decimal(7)}, 'existing-bill', self.start, self.start)
        self.app.login(); self.pump()
        self.assertEqual(self.store.load().account_id, key_fingerprint(self.keys))
        self.assertEqual(self.store.evaluate()[1]['spent'], Decimal(7))
        browser.futures[1].set_result({}); self.pump()

    def test_account_mismatch_creates_separate_unbound_ledger_after_confirmation(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.store.apply_snapshot({'test-key-b': Decimal(7)}, 'existing-bill', self.start, self.start)
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.collect()
        browser.futures[0].set_exception(AccountMismatch('fixture account mismatch'))
        self.pump(); self.pump()
        self.assertEqual(self.app.folder, self.folder)
        self.assertFalse(self.app.busy)
        self.mocks[2].assert_not_called()
        self.assertIn('账户与当前账本不一致', self.app.status_var.get())
        self.app.buttons['account_ledger'].invoke(); self.pump()
        self.assertNotEqual(self.app.folder, self.folder)
        self.assertEqual(self.app.store.active_bindings(), [])
        self.assertEqual(self.app.store.load().account_id, '')
        self.assertEqual(self.store.evaluate()[1]['spent'], Decimal(7))
        self.assertEqual(self.store.load().account_id, key_fingerprint(self.keys))
        self.assertEqual(current_ledger(self.folder), self.app.folder)
        self.assertEqual([a for a, _ in browser.calls], ['collect', 'login'])
        browser.futures[1].set_result({}); self.pump()

    def test_person_import_previews_counts_and_preserves_source_workbook(self):
        from openpyxl import Workbook
        input_path = self.folder / 'import-fixture.xlsx'
        workbook = Workbook(); sheet = workbook.active
        sheet.append(['API名/人员名', '个人金额（元）'])
        sheet.append(['导入甲', 0]); sheet.append(['导入乙', '12.50'])
        sheet.append(['导入丙', -1]); workbook.save(input_path); workbook.close()
        original = input_path.read_bytes()
        with patch('quota_guard.ui.filedialog.askopenfilename', return_value=str(input_path)):
            self.app.import_people()
        self.assertEqual(input_path.read_bytes(), original)
        self.assertEqual(self.store.read_people(), [Person('导入甲', Decimal(0)),
            Person('导入乙', Decimal('12.50')), Person('导入丙', Decimal(-1))])
        self.assertIn('零额度 1', self.mocks[2].call_args.args[1])
        self.assertFalse(self.app.observing)

    def test_person_import_refuses_external_change_during_confirmation(self):
        from openpyxl import Workbook
        input_path = self.folder / 'import-fixture.xlsx'
        workbook = Workbook(); workbook.active.append(['API名/人员名', '个人金额（元）'])
        workbook.active.append(['导入甲', 80]); workbook.save(input_path); workbook.close()
        def concurrent_save(*_args, **_kwargs):
            self.store.save_people([Person('外部修改', Decimal(25))])
            return True
        with patch('quota_guard.ui.filedialog.askopenfilename', return_value=str(input_path)), \
                patch('quota_guard.ui.messagebox.askyesno', side_effect=concurrent_save):
            self.app.import_people()
        self.assertEqual(self.store.read_people(), [Person('外部修改', Decimal(25))])
        self.mocks[1].assert_called_once()

    def test_start_before_login_does_not_navigate_or_enable_management(self):
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.start()
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertEqual(browser.calls, [])
        self.assertIn('完成登录', self.app.status_var.get())
        self.mocks[2].assert_not_called()

    def test_open_login_is_pending_until_keys_are_verified(self):
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.login(); self.pump()
        browser.futures[0].set_result({}); self.pump()
        self.assertFalse(self.app.session_ready)
        self.assertIn('待登录', self.app.buttons['login'].cget('text'))
        self.assertIn('浏览器已打开', self.app.status_var.get())
        self.app.start()
        self.assertEqual([a for a, _ in browser.calls], ['login'])
        self.app.collect()
        browser.futures[1].set_result(self.valid_collection()); self.pump()
        self.assertTrue(self.app.session_ready)
        self.assertIn('已核验', self.app.buttons['login'].cget('text'))
        self.assertIn('成功', self.app.buttons['collect'].cget('text'))

    def test_empty_initial_fees_allow_zero_quota_without_inventing_positive_fees(self):
        self.app.browser = OfflineBrowser({'collect': self.empty_collection(), 'revoke': {'absent': True}})
        self.app.collect(); self.pump()
        self.assertTrue(self.app.session_ready)
        self.assertIsNotNone(self.app.collection_context)
        self.assertIn('暂无明细，费用未知', self.app.buttons['collect'].cget('text'))
        self.app.start(); self.pump(); self.pump()
        self.assertTrue(self.app.observing)
        self.assertEqual([a for a, _ in self.app.browser.calls], ['collect', 'revoke'])
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertIsNone(self.app.rows[1]['spent'])
        self.assertIn('费用尚未就绪', self.mocks[2].call_args.args[1])

    def test_start_uses_current_valid_collection_and_stop_has_persistent_label(self):
        self.store.save_people([Person('测试甲', Decimal(100)), Person('测试乙', Decimal(80))])
        self.app.browser = OfflineBrowser({'collect': self.valid_collection()})
        self.app.collect(); self.pump()
        self.app.start(); self.pump()
        self.assertTrue(self.app.observing)
        self.assertEqual([a for a, _ in self.app.browser.calls], ['collect'])
        self.assertEqual(self.app.buttons['start'].cget('text'), '全自动管理：已开启')
        self.app.stop()
        self.assertFalse(self.app.observing)
        self.assertIn('已停止', self.app.buttons['stop'].cget('text'))

    def test_changed_workbook_invalidates_successful_collection_before_start(self):
        self.app.browser = OfflineBrowser({'collect': self.valid_collection()})
        self.app.collect(); self.pump()
        self.store.save_people([Person('测试甲', Decimal(25))])
        self.app.start()
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.collection_context)
        self.assertIn('待重新读取', self.app.buttons['collect'].cget('text'))
        self.assertEqual([a for a, _ in self.app.browser.calls], ['collect'])

    def test_read_failure_releases_buttons_without_modal_and_can_retry(self):
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.collect()
        browser.futures[0].set_exception(GuardError('fixture network failure')); self.pump()
        self.assertFalse(self.app.busy)
        self.assertEqual(str(self.app.buttons['collect'].cget('state')), 'normal')
        self.assertIn('失败', self.app.buttons['collect'].cget('text'))
        self.assertIn('fixture network failure', self.app.status_var.get())
        self.assertIn('重试', self.app.status_var.get())
        self.mocks[1].assert_not_called()
        self.app.collect()
        browser.futures[1].set_result(self.valid_collection()); self.pump()
        self.assertIn('成功', self.app.buttons['collect'].cget('text'))

    def test_important_failure_persists_result_then_releases_busy_before_dialog(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.app.refresh_rows(); self.app.tree.selection_set('0')
        browser = ControlledBrowser(); self.app.browser = browser
        observed = []
        def show_error(*_args, **_kwargs):
            observed.append((self.app.busy, self.store.latest_action('test-key-a')))
        with patch('quota_guard.ui.messagebox.showerror', side_effect=show_error):
            self.app.verify_revoke()
            browser.futures[0].set_exception(GuardError('fixture verification failure'))
            self.pump()
        self.assertEqual(observed, [(False, 'observation_failed')])

    def test_slow_pending_task_shows_stage_without_becoming_idle(self):
        browser = ControlledBrowser(); self.app.browser = browser
        browser.progress = {'stage': '等待费用响应', 'started_at': time.monotonic()}
        self.app.collect()
        time.sleep(.11)
        with patch('quota_guard.ui.time.monotonic', return_value=time.monotonic() + 31):
            self.root.update()
        self.assertTrue(self.app.busy)
        self.assertIn('等待费用响应', self.app.progress_var.get())
        self.assertIn('仍在处理', self.app.progress_var.get())
        self.assertEqual(str(self.app.buttons['collect'].cget('state')), 'disabled')
        browser.futures[0].set_result(self.valid_collection()); self.pump()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.progress_var.get(), '当前操作：空闲')

    def test_stop_during_approved_revoke_retains_result_and_stays_stopped(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.prime_verified_session()
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        permit = browser.calls[0][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        self.app.stop()
        self.assertIn('已停止', self.app.buttons['stop'].cget('text'))
        browser.futures[0].set_result({'absent': True}); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertFalse(self.app.automatic)
        self.assertIn('自动管理已停止', self.app.status_var.get())

    def test_interrupt_discards_read_clears_pending_changes_and_waits_for_cleanup(self):
        browser = ControlledBrowser(); browser.cleanup_future = Future()
        self.app.browser = browser
        self.app.collect()
        self.app.login()
        self.app.pending_change = lambda: self.fail('Interrupted change must not run')
        self.app.interrupt_operation()
        self.assertTrue(self.app.interrupting)
        self.assertFalse(self.app.pending_login)
        self.assertIsNone(self.app.pending_change)
        self.app.login(); self.pump()
        self.assertFalse(self.app.busy)
        self.assertTrue(self.app.interrupting)
        self.assertEqual(str(self.app.buttons['login'].cget('state')), 'disabled')
        self.assertEqual([a for a, _ in browser.calls], ['collect'])
        self.assertEqual(self.store.active_tracking_ids(), set())
        browser.cleanup_future.set_result(None); self.pump()
        self.assertFalse(self.app.interrupting)
        self.assertFalse(self.app.session_ready)
        self.assertEqual(self.app.last_keys, [])
        self.assertIsNone(self.app.collection_context)
        self.assertEqual(str(self.app.buttons['login'].cget('state')), 'normal')
        self.assertIn('清理已完成', self.app.status_var.get())
        self.assertIn('重新登录', self.app.status_var.get())
        self.mocks[1].assert_not_called()

    def test_interrupt_records_approved_revoke_unknown_before_cleanup_finishes(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.store.apply_snapshot({'test-key-b': Decimal(7)}, 'before-interrupt', self.start, self.start)
        self.prime_verified_session()
        browser = ControlledBrowser(); browser.cleanup_future = Future()
        self.app.browser = browser
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        permit = browser.calls[0][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        self.app.interrupt_operation(); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'unknown_result')
        self.assertEqual(self.store.evaluate()[1]['spent'], Decimal(7))
        self.assertFalse(self.app.observing)
        self.assertTrue(self.app.interrupting)
        self.assertIsNone(self.app.delete_permit)
        browser.cleanup_future.set_result(None); self.pump()
        self.assertIn('结果不明', self.app.status_var.get())
        self.mocks[1].assert_not_called()

    def test_close_interrupts_pending_browser_instead_of_waiting_for_busy(self):
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.collect()
        self.assertTrue(self.app.busy)
        with patch.object(self.root, 'destroy') as destroy:
            self.app.close()
            self.assertTrue(browser.futures[0].done())
            self.pump(); self.pump()
            destroy.assert_called_once()
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.session_ready)
        self.assertEqual(self.store.active_tracking_ids(), set())

    def test_cleanup_failure_keeps_login_disabled_until_retry_completes(self):
        browser = ControlledBrowser(); self.app.browser = browser
        browser.cleanup_future = Future()
        browser.cleanup_future.set_exception(GuardError('fixture cleanup failure'))
        self.app.interrupt_operation(); self.pump()
        self.assertTrue(self.app.cleanup_failed)
        self.assertEqual(str(self.app.buttons['login'].cget('state')), 'disabled')
        self.assertEqual(str(self.app.buttons['interrupt'].cget('state')), 'normal')
        self.app.login()
        self.assertEqual(browser.calls, [])
        browser.cleanup_future = Future(); browser.cleanup_future.set_result(None)
        self.app.interrupt_operation(); self.pump()
        self.assertFalse(self.app.cleanup_failed)
        self.assertEqual(str(self.app.buttons['login'].cget('state')), 'normal')

    def test_diagnostic_events_contain_structure_without_raw_error_or_people(self):
        browser = ControlledBrowser(); self.app.browser = browser
        captured = []
        with patch.object(self.app.diagnostics, 'emit', side_effect=lambda event, **fields: captured.append((event, fields))):
            self.app.buttons['collect'].invoke()
            browser.futures[0].set_exception(GuardError('raw sensitive value sk-do-not-record'))
            self.pump()
        events = {event for event, fields in captured}
        self.assertTrue({'button_started', 'button_returned', 'browser_action_started', 'browser_action_failed'} <= events)
        serialized = str(captured)
        for value in ('raw sensitive value', 'sk-do-not-record', '测试甲', 'test-key-a'):
            self.assertNotIn(value, serialized)

    def test_external_workbook_change_invalidates_visible_success_and_stops_management(self):
        self.app.browser = OfflineBrowser({'collect': self.valid_collection()})
        self.app.collect(); self.pump()
        self.app.observing = self.app.automatic = True
        self.store.save_people([Person('测试甲', Decimal(50))])
        self.app._watch_workbook()
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.validated_hash)
        self.assertIsNone(self.app.collection_context)
        self.assertIn('待重新检查', self.app.buttons['check'].cget('text'))
        self.assertIn('待重新读取', self.app.buttons['collect'].cget('text'))

    def test_empty_fees_only_process_zero_even_with_prior_positive_trigger(self):
        previous = self.valid_collection()
        previous['totals']['test-key-b'] = Decimal(100)
        browser = OfflineBrowser({'collect': previous, 'revoke': {'absent': True}})
        self.app.browser = browser
        self.app.collect(); self.pump()
        browser.results['collect'] = self.empty_collection()
        self.app.collect(); self.pump()
        self.app.start(); self.pump(); self.pump()
        self.assertEqual([args.get('name') for action, args in browser.calls if action == 'revoke'], ['测试甲'])
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertIsNone(self.store.latest_action('test-key-b'))
        self.assertIn('仅零额度可处理', self.app.status_var.get())

    def test_diagnostic_viewer_can_open_during_pending_work(self):
        self.app.browser = ControlledBrowser()
        self.app.collect()
        self.assertEqual(str(self.app.buttons['diagnostics'].cget('state')), 'normal')
        with patch('quota_guard.ui.os.startfile') as startfile:
            self.app.buttons['diagnostics'].invoke()
        startfile.assert_not_called()
        self.assertTrue(self.app._diagnostics_window.winfo_exists())
        self.assertEqual(self.app._diagnostics_window.title(), '诊断日志与本地 AI 助手')
        self.assertIn('本地 AI', self.app.buttons['diagnostics'].cget('text'))
        self.assertTrue(self.app.busy)
        self.assertIn('已打开', self.app.buttons['diagnostics'].cget('text'))

    def test_closed_browser_with_confirmed_cleanup_can_be_replaced(self):
        old = ControlledBrowser(); old.closed = True; old.cleanup_failed = False
        fresh = ControlledBrowser(); self.app.browser = old
        with patch('quota_guard.browser_process.IsolatedBrowserService', return_value=fresh) as create:
            self.app.login(); self.pump()
        create.assert_called_once_with(diagnostics_dir=self.app.diagnostics_dir)
        self.assertIs(self.app.browser, fresh)
        self.assertEqual(old.calls, [])
        self.assertEqual([a for a, _ in fresh.calls], ['login'])
        fresh.futures[0].set_result({}); self.pump()

    def test_closed_browser_cleanup_failure_must_be_cleared_before_replacement(self):
        old = ControlledBrowser(); old.closed = True; old.cleanup_failed = True
        self.app.browser = old
        with patch('quota_guard.browser_process.IsolatedBrowserService') as create:
            self.app.login(); self.pump()
            create.assert_not_called()
            self.assertIs(self.app.browser, old)
            self.assertTrue(self.app.cleanup_failed)
        old.cleanup_failed = False
        self.app.interrupt_operation(); self.pump()
        fresh = ControlledBrowser()
        with patch('quota_guard.browser_process.IsolatedBrowserService', return_value=fresh):
            self.app.login(); self.pump()
        self.assertIs(self.app.browser, fresh)
        fresh.futures[0].set_result({}); self.pump()

    def test_close_failure_preserves_window_and_folder_lock_for_cleanup_retry(self):
        from quota_guard.instance import FolderLock
        browser = ControlledBrowser(); self.app.browser = browser
        with patch.object(browser, 'close', side_effect=GuardError('fixture close cleanup failed')), \
                patch.object(self.root, 'destroy') as destroy:
            self.app.close(); self.pump(); self.pump()
            destroy.assert_not_called()
        self.assertFalse(self.app.closing)
        self.assertTrue(self.app.cleanup_failed)
        self.assertIn('关闭未完成', self.app.status_var.get())
        with self.assertRaises(GuardError):
            FolderLock(self.folder)
        self.app.interrupt_operation(); self.pump()
        self.assertFalse(self.app.cleanup_failed)
        with patch.object(self.root, 'destroy') as destroy:
            self.app.close(); self.pump(); self.pump()
            destroy.assert_called_once()

    def test_failed_interrupt_is_reported_while_original_future_is_still_busy(self):
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.collect()
        failed_cleanup = Future(); failed_cleanup.set_exception(GuardError('fixture retained process'))
        with patch.object(browser, 'interrupt', return_value=failed_cleanup):
            self.app.interrupt_operation(); self.pump()
        self.assertTrue(self.app.busy)
        self.assertFalse(self.app.interrupting)
        self.assertTrue(self.app.cleanup_failed)
        self.assertIn('中断清理未完成', self.app.status_var.get())
        self.assertEqual(str(self.app.buttons['interrupt'].cget('state')), 'normal')
        browser.futures[0].set_exception(GuardError('eventual action failure')); self.pump()

    def test_revoke_unknown_audit_failure_survives_interrupt_and_close_until_local_retry(self):
        from quota_guard.instance import FolderLock
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.prime_verified_session()
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        permit = browser.calls[0][1]['permit']; ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        with patch.object(self.app.store, 'record_action', side_effect=OSError('fixture full disk')), \
                patch.object(self.root, 'destroy') as destroy:
            self.app.close(); self.pump(); self.pump()
            destroy.assert_not_called()
            self.assertTrue(self.app.audit_failed)
            self.assertEqual(len(self.app.audit_pending), 1)
            self.assertIn('尚未完整保存', self.app.status_var.get())
            self.assertIn('尚未完整保存', self.app.audit_var.get())
            self.assertFalse(self.app.closing)
            self.assertEqual(self.store.latest_action('test-key-a'), 'requested')
            self.app.login(); self.app.start()
            self.app._queue_change(lambda: self.fail('pending audit must block change'), 'change')
            self.assertFalse(self.app.pending_login)
            self.assertIsNone(self.app.pending_change)
            self.assertEqual(str(self.app.buttons['save_audit'].cget('state')), 'normal')
            self.app.retry_audit()
            self.assertTrue(self.app.audit_failed)
        with self.assertRaises(GuardError):
            FolderLock(self.folder)
        self.app.buttons['save_audit'].invoke(); self.pump()
        self.assertFalse(self.app.audit_failed)
        self.assertEqual(self.app.audit_pending, [])
        self.assertEqual(self.store.latest_action('test-key-a'), 'unknown_result')
        self.assertEqual([a for a, _ in browser.calls], ['revoke'])
        with patch.object(self.root, 'destroy') as destroy:
            self.app.close(); self.pump(); self.pump()
            destroy.assert_called_once()

    def test_observed_absence_is_retained_until_failed_audit_save_can_be_retried(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.app.refresh_rows(); self.app.tree.selection_set('0')
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.verify_revoke()
        with patch.object(self.app.store, 'record_action', side_effect=OSError('fixture full disk')):
            browser.futures[0].set_result({'absent': True}); self.pump()
        self.assertTrue(self.app.audit_failed)
        self.assertEqual(self.app.audit_pending[0]['result'], {'absent': True})
        pending = self.store.latest_action_record('test-key-a')
        self.assertEqual(pending['status'], 'pending')
        self.assertIn('只读', pending['detail'])
        self.app.interrupt_operation(); self.pump()
        self.assertTrue(self.app.audit_failed)
        self.assertIn('尚未完整保存', self.app.status_var.get())
        self.app.retry_audit(); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual(self.store.latest_action_record('test-key-a')['operation_id'], pending['operation_id'])
        self.assertFalse(self.app.audit_failed)
        self.assertEqual([a for a, _ in browser.calls], ['verify_revoke'])

    def test_display_retry_does_not_write_confirmed_absence_a_second_time(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.app.refresh_rows(); self.app.tree.selection_set('0')
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.verify_revoke()
        with patch.object(self.app, 'refresh_rows', side_effect=OSError('fixture display error')):
            browser.futures[0].set_result({'absent': True}); self.pump()
        self.assertTrue(self.app.audit_failed)
        self.app.retry_audit(); self.pump()
        actions = [event for event in self.store.events() if event['kind'] == 'action']
        self.assertEqual([event['status'] for event in actions], ['absent_verified', 'pending'])
        self.assertEqual(actions[0]['operation_id'], actions[1]['operation_id'])
        self.assertEqual([a for a, _ in browser.calls], ['verify_revoke'])
        self.assertFalse(self.app.audit_failed)

    def test_worker_collection_delayed_before_ui_delivery_is_not_applied(self):
        browser = ControlledBrowser()
        self.app.browser = browser
        result = self.valid_collection()
        # The worker's read predates delivery; the UI must not refresh its clocks.
        result['collection_started_wall'] -= COLLECTION_MAX_AGE_SECONDS + 1
        result['collection_started_monotonic'] -= COLLECTION_MAX_AGE_SECONDS + 1
        self.app.collect()
        browser.futures[0].set_result(result)
        self.pump()
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.session_ready)
        self.assertIsNone(self.app.collection_context)
        self.assertEqual(self.store.active_tracking_ids(), set())
        self.assertFalse(any(event['kind'] == 'snapshot' for event in self.store.events()))
        self.assertIn('读取已过期', self.app.status_var.get())
        self.mocks[1].assert_not_called()

    def test_removed_person_keeps_recovery_row_and_can_be_verified_present(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        evidence = self.store.action_evidence('测试甲', 'test-key-a')
        self.store.record_action('测试甲', 'test-key-a', 'requested', 'fixture approved',
                                 operation_id='removed-person-operation', evidence=evidence)
        self.store.save_people([Person('测试乙', Decimal(80))])
        self.app.refresh_rows()
        self.app._update_buttons()
        index = next(i for i, row in enumerate(self.app.rows) if row.get('tracking_id') == 'test-key-a')
        row = self.app.rows[index]
        self.assertEqual(row['status'], 'recovery_pending')
        self.assertEqual(row['name'], '测试甲')
        self.assertIsNone(row['amount'])
        self.assertEqual(str(self.app.buttons['verify'].cget('state')), 'normal')
        self.app.tree.selection_set(str(index))
        browser = OfflineBrowser({'verify_revoke': {'absent': False, 'present': True}})
        self.app.browser = browser
        self.app.buttons['verify'].invoke()
        self.pump()
        record = self.store.latest_action_record('test-key-a')
        self.assertEqual(record['status'], 'present_verified')
        self.assertEqual(record['operation_id'], 'removed-person-operation')
        self.assertEqual(record['evidence'], evidence)
        self.assertEqual(len(self.store.unresolved_actions()), 1)
        self.assertIn('test-key-a', self.store.active_tracking_ids())
        self.assertTrue(any(row.get('status') == 'present_unresolved' for row in self.app.rows))
        self.assertEqual([action for action, _ in browser.calls], ['verify_revoke'])

    def test_definite_preparation_failure_records_not_submitted_without_approval(self):
        from quota_guard.revocation_permit import RevocationNotSubmitted
        self.prime_verified_session()
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        permit = browser.calls[0][1]['permit']
        browser.futures[0].set_exception(RevocationNotSubmitted())
        self.pump()
        self.assertFalse(permit.allowed)
        self.assertFalse(permit.ready.is_set())
        self.assertEqual(self.store.latest_action('test-key-a'), 'not_submitted')
        self.assertEqual(self.store.unresolved_actions(), [])
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.audit_failed)

    def test_presence_observation_does_not_allow_restart_of_unknown_revoke(self):
        self.prime_verified_session()
        self.store.record_action('测试甲', 'test-key-a', 'unknown_result', 'fixture old uncertainty',
                                 operation_id='uncertain-original')
        self.app.refresh_rows()
        self.app.tree.selection_set('0')
        browser = OfflineBrowser({'verify_revoke': {'absent': False, 'present': True}})
        self.app.browser = browser
        self.app.verify_revoke(); self.pump()
        self.assertEqual(self.store.latest_action_record('test-key-a')['operation_id'], 'uncertain-original')
        self.assertEqual(len(self.store.unresolved_actions()), 1)
        self.app.start(); self.pump()
        self.assertFalse(self.app.automatic)
        self.assertEqual([action for action, _ in browser.calls], ['verify_revoke'])

        failed_read = ControlledBrowser()
        self.app.browser = failed_read
        self.app.tree.selection_set('0')
        self.app.verify_revoke()
        failed_read.futures[0].set_exception(GuardError('fixture second read failed'))
        self.pump()
        observed = self.store.latest_action_record('test-key-a')
        self.assertEqual(observed['operation_id'], 'uncertain-original')
        self.assertEqual(observed['status'], 'observation_failed')
        self.assertEqual(len(self.store.unresolved_actions()), 1)
        self.assertFalse(self.app.audit_failed)
        self.assertFalse(self.app.automatic)

    def test_not_submitted_export_retry_only_saves_locally_once(self):
        from quota_guard.revocation_permit import RevocationNotSubmitted
        self.prime_verified_session()
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        original_export = self.app.store.export_logs
        failed_once = False
        def export_with_one_failure():
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise GuardError('fixture export failure')
            return original_export()
        with patch.object(self.app.store, 'export_logs', side_effect=export_with_one_failure) as export:
            browser.futures[0].set_exception(RevocationNotSubmitted())
            self.pump()
            self.assertTrue(self.app.audit_failed)
            before = len([event for event in self.store.events() if event.get('status') == 'not_submitted'])
            self.assertEqual(before, 1)
            self.app.retry_audit(); self.pump()
            self.assertGreaterEqual(export.call_count, 2)
        self.assertFalse(self.app.audit_failed)
        self.assertEqual(len([event for event in self.store.events() if event.get('status') == 'not_submitted']), before)
        self.assertEqual([action for action, _ in browser.calls], ['revoke'])
        self.assertFalse(self.app.automatic)

    def _assert_changed_authorization_evidence_denies_permit(self, changes):
        self.prime_verified_session()
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        permit = browser.calls[0][1]['permit']
        original = self.app.store.action_evidence
        def changed_evidence(*args, **kwargs):
            result = original(*args, **kwargs)
            result.update(changes)
            return result
        with patch.object(self.app.store, 'action_evidence', side_effect=changed_evidence) as capture:
            ready_with_inventory(self.app, permit)
            self.pump()
        capture.assert_called_once_with('测试甲', 'test-key-a')
        self.assertTrue(permit.decided.is_set())
        self.assertFalse(permit.allowed)
        self.assertEqual(self.store.latest_action('test-key-a'), 'pending')
        self.assertFalse(any(event.get('status') == 'requested' for event in self.store.events()))
        browser.futures[0].set_result({'cancelled': True})
        self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'cancelled')
        self.assertEqual([action for action, _ in browser.calls], ['revoke'])

    def test_changed_evidence_workbook_hash_cannot_approve_revocation(self):
        self._assert_changed_authorization_evidence_denies_permit({'workbook_hash': '0' * 64})

    def test_changed_evidence_trigger_status_cannot_approve_revocation(self):
        self._assert_changed_authorization_evidence_denies_permit({'status': 'ok', 'reasons': []})

    def test_requested_record_finishing_after_collection_expiry_does_not_grant_permit(self):
        self.prime_verified_session()
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        permit = browser.calls[0][1]['permit']
        original = self.app.store.record_action
        persisted = []
        def slow_save(name, tracking_id, status, detail, **kwargs):
            result = original(name, tracking_id, status, detail, **kwargs)
            if status == 'requested':
                persisted.append(self.store.latest_action_record(tracking_id))
                # Simulate time spent persisting after the first freshness check.
                self.app.collection_observed_wall -= COLLECTION_MAX_AGE_SECONDS + 1
                self.app.collection_observed_monotonic -= COLLECTION_MAX_AGE_SECONDS + 1
            return result
        with patch.object(self.app.store, 'record_action', side_effect=slow_save):
            ready_with_inventory(self.app, permit)
            self.pump()
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]['status'], 'requested')
        self.assertEqual(persisted[0]['evidence']['status'], 'triggered')
        self.assertTrue(permit.decided.is_set())
        self.assertFalse(permit.allowed)
        self.assertEqual(self.store.latest_action('test-key-a'), 'requested')
        browser.futures[0].set_result({'cancelled': True})
        self.pump()
        cancelled = self.store.latest_action_record('test-key-a')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(cancelled['operation_id'], persisted[0]['operation_id'])
        self.assertEqual([action for action, _ in browser.calls], ['revoke'])

    def test_rejected_structure_cannot_open_manual_revoke_from_old_high_fees(self):
        self.store.save_people([Person('测试甲', Decimal(10)), Person('测试乙', Decimal(80))])
        self.app.browser = OfflineBrowser({'collect': self.valid_collection(
            totals={'test-key-a': Decimal('10.2'), 'test-key-b': Decimal(5)})})
        self.app.collect(); self.pump()
        self.app.browser = OfflineBrowser({'collect': self.empty_collection(
            reason_code='CHARGE_TYPE_UNSUPPORTED', retryable=False)})
        self.app.collect(); self.pump()
        index = next(i for i, row in enumerate(self.app.rows) if row['tracking_id'] == 'test-key-a')
        self.assertTrue(self.app.rows[index]['complete'])  # Historical display only.
        self.app.tree.selection_set(str(index))
        self.app.prepare_revoke(); self.pump()
        self.assertEqual([a for a, _ in self.app.browser.calls], ['collect'])
        self.assertIsNone(self.store.latest_action('test-key-a'))

    def test_decreasing_current_fees_invalidate_old_high_snapshot_authorization(self):
        self.store.save_people([Person('测试甲', Decimal(10)), Person('测试乙', Decimal(80))])
        self.app.browser = OfflineBrowser({'collect': self.valid_collection(
            totals={'test-key-a': Decimal('10.2'), 'test-key-b': Decimal(5)})})
        self.app.collect(); self.pump()
        self.app.browser = OfflineBrowser({'collect': self.valid_collection(source_id='correction',
            totals={'test-key-a': Decimal('9.8'), 'test-key-b': Decimal(5)})})
        self.app.collect(); self.pump()
        self.assertIsNone(self.app.collection_context)
        self.assertTrue(self.app.session_ready)  # Local fee rejection does not prove login expired.
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.app.action_states['collect'], '累计费用下降')
        self.app.start(); self.pump()
        self.assertFalse(self.app.observing)
        self.assertEqual([a for a, _ in self.app.browser.calls], ['collect'])
        self.assertIsNone(self.store.latest_action('test-key-a'))

    def test_new_readonly_verification_survives_table_removed_while_in_flight(self):
        self.prime_verified_session()
        browser = ControlledBrowser()
        self.app.browser = browser
        self.app.tree.selection_set('0')
        self.app.verify_revoke()
        pending = self.store.latest_action_record('test-key-a')
        self.assertEqual(pending['status'], 'pending')
        self.assertIn('只读', pending['detail'])
        self.store.workbook_path.unlink()
        browser.futures[0].set_result({'absent': False, 'present': True})
        self.pump()
        result = self.store.latest_action_record('test-key-a')
        self.assertEqual(result['operation_id'], pending['operation_id'])
        self.assertEqual(result['status'], 'present_verified')
        self.assertFalse(self.app.audit_failed)
        self.assertTrue(self.app.recovery_mode)
        self.assertEqual([a for a, _ in browser.calls], ['verify_revoke'])

    def test_target_conflict_stops_collection_before_binding_or_snapshot(self):
        from quota_guard.target_lock import TargetLock
        with TargetLock({'test-key-b'}):
            self.app.browser = OfflineBrowser({'collect': self.valid_collection()})
            self.app.collect(); self.pump()
        self.assertFalse(self.app.session_ready)
        self.assertFalse(self.store.active_tracking_ids())
        self.assertFalse(any(e['kind'] == 'snapshot' for e in self.store.events()))
        self.assertIsNone(self.app.collection_context)

    def test_partial_latest_fees_cannot_revoke_positive_historical_trigger(self):
        browser = OfflineBrowser({'collect': self.valid_collection(
            totals={'test-key-a': Decimal(0), 'test-key-b': Decimal(100)}), 'revoke': {'absent': True}})
        self.app.browser = browser
        self.app.collect(); self.pump()
        browser.results['collect'] = self.valid_collection(totals={'test-key-a': Decimal(0)},
                                                          source_id='partial-current')
        self.app.collect(); self.pump()
        positive = next(row for row in self.app.rows if row['name'] == '测试乙')
        self.assertEqual(positive['status'], 'triggered')
        self.assertFalse(positive['complete'])
        self.assertTrue(positive['spent_is_lower_bound'])
        self.app.start(); self.pump(); self.pump()
        self.assertEqual([args['name'] for action, args in browser.calls if action == 'revoke'], ['测试甲'])
        self.assertIsNone(self.store.latest_action('test-key-b'))

    def test_single_delete_uses_durable_permit_and_only_selected_target(self):
        self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal('0.2'))])
        self.app.browser = OfflineBrowser({'collect': self.valid_collection(
            totals={'test-key-a': Decimal(0), 'test-key-b': Decimal('0.2')})})
        self.app.collect(); self.pump()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('1')
        self.app.revoke_selected_once(); self.pump()
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertIsNotNone(self.app.single_revoke_id)
        self.assertEqual(str(self.app.buttons['stop'].cget('state')), 'normal')
        self.assertEqual(browser.calls[0][0], 'read_keys')
        permit = browser.calls[1][1]['permit']
        self.assertEqual(browser.calls[1][1]['tracking_id'], 'test-key-b')
        self.assertTrue(browser.calls[1][1]['target_lock_names'])
        self.assertEqual(self.store.latest_action('test-key-b'), 'pending')
        pending = self.store.latest_action_record('test-key-b')
        self.assertEqual(pending['intent'], 'manual_force')
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        self.assertEqual(self.store.latest_action('test-key-b'), 'requested')
        browser.futures[0].set_result({'absent': True}); self.pump(); self.pump()
        self.assertEqual(self.store.latest_action('test-key-b'), 'absent_verified')
        self.assertIsNone(self.store.latest_action('test-key-a'))
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke'])
        self.assertIsNone(self.app.timer)
        self.assertIsNone(self.app.single_revoke_id)
        self.assertIn('强制删除', self.app.buttons['revoke'].cget('text'))
        self.assertIn('已确认不存在', self.app.buttons['revoke'].cget('text'))
        self.assertIn('未启动周期读取', self.app.status_var.get())
        completed = self.store.latest_action_record('test-key-b')
        self.assertEqual(completed['intent'], 'manual_force')
        self.assertEqual(completed['operation_id'], pending['operation_id'])

    def assert_force_allowed(self, quota, *, totals=None, stale=False, collect=True):
        """Quota evidence is retained as fact, never converted into permission."""
        self.store.save_people([Person('测试甲', Decimal(100)), Person('测试乙', quota)])
        self.app.refresh_rows()
        if collect:
            self.app.browser = OfflineBrowser({'collect': (
                self.empty_collection() if totals is None else self.valid_collection(totals=totals))})
            self.app.collect(); self.pump()
        if stale:
            self.app.collection_observed_monotonic -= COLLECTION_MAX_AGE_SECONDS + 1
        before = next(row for row in self.store.evaluate() if row['name'] == '测试乙')
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('1')
        self.app.revoke_selected_once(); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke'])
        permit = browser.calls[1][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        event = self.store.latest_action_record('test-key-b')
        self.assertEqual(event['status'], 'requested')
        self.assertEqual(event['intent'], 'manual_force')
        self.assertEqual(Decimal(event['evidence']['amount']), quota)
        self.assertEqual(event['evidence']['spent'],
                         None if before['spent'] is None else str(before['spent']))
        self.assertEqual(event['evidence']['status'], before['status'])
        self.assertFalse(self.app._collection_is_current())
        browser.futures[0].set_result({'absent': True}); self.pump()
        self.assertEqual(self.store.latest_action('test-key-b'), 'absent_verified')
        self.assertIsNone(self.store.latest_action('test-key-a'))
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.timer)

    def test_single_delete_allows_under_budget_without_faking_trigger(self):
        self.assert_force_allowed(Decimal(80), totals={'test-key-b': Decimal(5)})

    def test_single_delete_binds_skipped_target_and_ignores_skip_quota(self):
        self.assert_force_allowed(Decimal(-1), collect=False)

    def test_single_delete_allows_unknown_cost_without_reading_fees(self):
        self.assert_force_allowed(Decimal(80), collect=False)

    def test_single_delete_allows_expired_fee_evidence_after_fresh_identity_read(self):
        self.assert_force_allowed(Decimal(80), totals={'test-key-b': Decimal(5)}, stale=True)

    def test_automatic_delete_still_rejects_under_budget_skipped_and_unknown(self):
        for quota, totals in ((Decimal(80), {'test-key-b': Decimal(5)}),
                              (Decimal(-1), {'test-key-b': Decimal(100)}),
                              (Decimal(80), None)):
            with self.subTest(quota=quota, totals=totals):
                self.store.save_people([Person('测试甲', Decimal(1000)), Person('测试乙', quota)])
                self.app.browser = OfflineBrowser({'collect': (
                    self.empty_collection() if totals is None else
                    self.valid_collection(totals=totals, source_id=str(quota)))})
                self.app.collect(); self.pump()
                row = next(row for row in self.app.rows if row['name'] == '测试乙')
                with self.assertRaises(GuardError):
                    self.app._current_revoke_target(row)
                self.assertIsNone(self.store.latest_action('test-key-b'))

    def test_automatic_delete_still_rejects_expired_fee_evidence(self):
        self.prime_verified_session()
        self.app.collection_observed_monotonic -= COLLECTION_MAX_AGE_SECONDS + 1
        with self.assertRaises(GuardError):
            self.app._current_revoke_target(self.app.rows[0])

    def test_single_delete_revalidates_workbook_after_target_confirmation(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('0')
        def change_workbook(*args, **kwargs):
            self.store.save_people([Person('测试甲', Decimal(-1)), Person('测试乙', Decimal(80))])
            return True
        with patch('quota_guard.ui.messagebox.askyesno', side_effect=change_workbook):
            self.app.revoke_selected_once(); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.assertIsNone(self.store.latest_action('test-key-a'))
        self.assertIsNone(self.app.single_revoke_id)

    def test_single_delete_rejects_fresh_list_replacing_bound_tracking_id(self):
        self.prime_verified_session()
        changed = [{'name': '测试甲', 'tracking_id': 'replacement-key'}, self.keys[1]]
        browser = ControlledBrowser(identity_result=self.identity_result(keys=changed))
        self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.assertIsNone(self.store.latest_action('test-key-a'))
        self.assertIsNone(self.store.latest_action('replacement-key'))
        self.assertEqual(self.store.evaluate()[0]['tracking_id'], 'test-key-a')

    def test_single_delete_rejects_ambiguous_fresh_target_name(self):
        self.prime_verified_session()
        ambiguous = self.keys + [{'name': '测试甲', 'tracking_id': 'duplicate-name-key'}]
        browser = ControlledBrowser(identity_result=self.identity_result(keys=ambiguous))
        self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.assertIsNone(self.store.latest_action('test-key-a'))

    def test_single_delete_rejects_fresh_identity_from_other_account(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result(account_id='other-account'))
        self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.assertIsNone(self.store.latest_action('test-key-a'))
        self.assertEqual(self.store.load().account_id, key_fingerprint(self.keys))

    def test_single_delete_stop_during_identity_read_cannot_grant_intent(self):
        self.prime_verified_session()
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.app.stop(quiet=True)
        browser.futures[0].set_result(self.identity_result()); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.assertIsNone(self.store.latest_action('test-key-a'))

    def test_single_delete_workbook_change_during_identity_read_cannot_grant_intent(self):
        self.prime_verified_session()
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once()
        self.store.save_people([Person('测试甲', Decimal(-1)), Person('测试乙', Decimal(80))])
        browser.futures[0].set_result(self.identity_result()); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.assertIsNone(self.store.latest_action('test-key-a'))

    def test_single_delete_identity_read_failure_never_creates_pending_action(self):
        self.prime_verified_session()
        browser = ControlledBrowser(); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once()
        browser.futures[0].set_exception(GuardError('fixture identity read failed')); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.assertIsNone(self.store.latest_action('test-key-a'))
        self.assertIsNone(self.app.single_revoke_id)

    def test_single_delete_cancel_target_confirmation_never_creates_intent(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('0')
        with patch('quota_guard.ui.messagebox.askyesno', return_value=False):
            self.app.revoke_selected_once(); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys'])
        self.assertIsNone(self.store.latest_action('test-key-a'))
        self.assertIsNone(self.app.single_revoke_id)

    def test_single_delete_rejects_unresolved_other_target(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result())
        self.store.record_action('测试乙', 'test-key-b', 'requested', 'other uncertain operation')
        self.app.browser = browser
        self.app.tree.selection_set('0')
        self.app.revoke_selected_once()
        self.assertEqual(browser.calls, [])
        self.assertIsNone(self.store.latest_action('test-key-a'))

    def test_single_delete_stop_before_permit_never_records_requested(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        permit = browser.calls[1][1]['permit']
        self.app.stop()
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.decided.is_set())
        self.assertFalse(permit.allowed)
        browser.futures[0].set_result({'cancelled': True}); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'cancelled')
        self.assertFalse(any(e['status'] == 'requested' for e in self.store.events()
                             if e['kind'] == 'action'))
        self.assertIsNone(self.app.single_revoke_id)
        self.assertIsNone(self.app.timer)
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke'])

    def test_single_delete_stop_after_permit_keeps_result_without_polling(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        permit = browser.calls[1][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        self.app.stop()
        browser.futures[0].set_result({'absent': True}); self.pump(); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.timer)
        self.assertIsNone(self.app.single_revoke_id)
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke'])

    def test_single_delete_respects_public_quota_trigger(self):
        self.store.save_people([Person('测试甲', Decimal(1000)), Person('测试乙', Decimal(-1))])
        self.app.browser = OfflineBrowser({'collect': self.valid_collection(
            totals={'test-key-a': Decimal(107), 'test-key-b': Decimal(0)})})
        self.app.collect(); self.pump()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        permit = browser.calls[1][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        browser.futures[0].set_result({'absent': True}); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertIsNone(self.store.latest_action('test-key-b'))
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke'])

    def test_single_delete_interrupt_preserves_unknown_and_clears_single_permission(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result()); browser.cleanup_future = Future()
        self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        permit = browser.calls[1][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        self.app.interrupt_operation(); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'unknown_result')
        self.assertIsNone(self.app.single_revoke_id)
        self.assertIsNone(self.app.delete_permit)
        self.assertIsNone(self.app.timer)
        self.assertTrue(self.app.interrupting)
        browser.cleanup_future.set_result(None); self.pump()
        self.assertFalse(self.app.interrupting)
        self.assertFalse(self.app.session_ready)
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke'])

    def test_single_delete_preparation_failure_is_not_submitted_and_not_retried(self):
        from quota_guard.revocation_permit import RevocationNotSubmitted
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        failure = RevocationNotSubmitted()
        browser.futures[0].set_exception(failure); self.pump(); self.pump()
        event = self.store.latest_action_record('test-key-a')
        self.assertEqual(event['status'], 'not_submitted')
        self.assertEqual(event['detail'], str(failure))
        self.assertEqual(self.store.unresolved_actions(), [])
        self.assertIn('准备失败，未提交', self.app.buttons['revoke'].cget('text'))
        self.assertIsNone(self.app.single_revoke_id)
        self.assertIsNone(self.app.timer)
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke'])

    def test_single_delete_unknown_then_present_does_not_authorize_retry(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        permit = browser.calls[1][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        browser.futures[0].set_result({'absent': False}); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'unknown_result')
        self.assertIn('结果未知', self.app.buttons['revoke'].cget('text'))
        self.app.tree.selection_set('0'); self.app.verify_revoke()
        browser.futures[1].set_result({'absent': False, 'present': True}); self.pump()
        self.assertIn('列表存在不代表 Key 可调用', self.app.status_var.get())
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        self.assertTrue(self.store.unresolved_actions())
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke', 'verify_revoke'])
        self.assertFalse(self.app.automatic)

    def test_single_delete_login_waits_for_recorded_outcome(self):
        self.prime_verified_session()
        browser = ControlledBrowser(identity_result=self.identity_result()); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        permit = browser.calls[1][1]['permit']
        ready_with_inventory(self.app, permit); self.pump()
        self.assertTrue(permit.allowed)
        self.app.login(); self.pump()
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke'])
        self.assertIsNone(self.app.single_revoke_id)
        browser.futures[0].set_result({'absent': True}); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual([a for a, _ in browser.calls], ['read_keys', 'revoke', 'login'])
        self.assertFalse(self.app.automatic)
        self.assertIsNone(self.app.timer)

    def test_old_scheduled_callback_cannot_join_a_later_run(self):
        self.app.automatic = self.app.observing = True
        self.app._schedule_processing()
        self.app.stop(quiet=True)
        self.app.automatic = self.app.observing = True
        with patch.object(self.app, '_process_next') as process:
            self.pump()
        process.assert_not_called()

    def test_start_confirmation_cannot_override_stop_login_or_workbook_change(self):
        # Keep login last: its intentionally queued login remains pending until
        # teardown, proving confirmation cannot start management ahead of it.
        for mutation in ('stop', 'workbook', 'login'):
            with self.subTest(mutation=mutation):
                self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(80))])
                self.prime_verified_session()
                browser = ControlledBrowser(); self.app.browser = browser
                def during_confirmation(*args, **kwargs):
                    if mutation == 'stop':
                        self.app.stop()
                    elif mutation == 'login':
                        self.app.login()
                    else:
                        self.store.save_people([Person('测试甲', Decimal(-1)), Person('测试乙', Decimal(80))])
                    return True
                with patch('quota_guard.ui.messagebox.askyesno', side_effect=during_confirmation), \
                        patch.object(self.app, '_schedule_processing') as schedule:
                    self.app.start()
                schedule.assert_not_called()
                self.assertFalse(self.app.observing)
                self.assertFalse(self.app.automatic)
                self.assertEqual(browser.calls, [])
                self.assertIsNone(self.app.timer)
                self.assertIsNone(self.store.latest_action('test-key-a'))
                if mutation == 'login':
                    self.assertTrue(self.app.pending_login)


if __name__ == '__main__':
    unittest.main()
