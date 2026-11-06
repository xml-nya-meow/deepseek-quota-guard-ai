"""Offline preference persistence and deterministic automatic polling scheduling."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from quota_guard.preferences import (DEFAULT_POLL_INTERVAL_SECONDS, PREFERENCES_FILENAME,
                                     load_poll_interval, save_poll_interval, validate_poll_interval)
from quota_guard.ui import App, COLLECTION_MAX_AGE_SECONDS, TZ, collection_is_fresh
from quota_guard.scheduler import CandidateQueue


class Value:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Timers:
    def __init__(self):
        self.pending = {}
        self.sequence = 0

    def after(self, delay, callback):
        self.sequence += 1
        key = f'after-{self.sequence}'
        self.pending[key] = (delay, callback)
        return key

    def after_cancel(self, key):
        self.pending.pop(key, None)

    def fire(self, key):
        self.pending.pop(key)[1]()


class PreferenceTests(unittest.TestCase):
    def test_default_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(load_poll_interval(Path(folder)), (10, None))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_interval_validation_requires_an_integer_in_range(self):
        for value, expected in [(1, 1), (' 10 ', 10), ('3600', 3600)]:
            self.assertEqual(validate_poll_interval(value), expected)
        for value in (True, False, 0, -1, 3601, 10.0, '1.5', '1e1', '', 'one', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '1～3600'):
                validate_poll_interval(value)

    def test_roundtrip_is_in_home_and_preserves_ledger_files(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            ledger = home / '账本'; ledger.mkdir()
            settings = ledger / '管理设置.json'
            settings.write_bytes(b'{"total_budget":"1500"}')
            workbook = ledger / '人员额度.xlsx'; workbook.write_bytes(b'fixture workbook')
            before = {path: path.read_bytes() for path in (settings, workbook)}
            save_poll_interval(home, '25')
            self.assertEqual(load_poll_interval(home), (25, None))
            self.assertTrue((home / PREFERENCES_FILENAME).is_file())
            self.assertFalse((ledger / PREFERENCES_FILENAME).exists())
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_corrupt_preferences_fall_back_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / PREFERENCES_FILENAME
            for content in ('broken', '{"schema":1,"poll_interval_seconds":true}',
                            '{"schema":1,"poll_interval_seconds":0}',
                            '{"schema":2,"poll_interval_seconds":10}'):
                with self.subTest(content=content):
                    path.write_text(content, encoding='utf-8')
                    interval, notice = load_poll_interval(Path(folder))
                    self.assertEqual(interval, DEFAULT_POLL_INTERVAL_SECONDS)
                    self.assertIn('原文件保留', notice)
                    self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_failed_atomic_save_preserves_old_preference_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            save_poll_interval(home, 20)
            with patch('quota_guard.core.os.replace', side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError):
                    save_poll_interval(home, 30)
            self.assertEqual(load_poll_interval(home), (20, None))
            self.assertEqual([p.name for p in home.iterdir()], [PREFERENCES_FILENAME])


class PollSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App.__new__(App)
        app = self.app
        app.home_folder = Path(self.temp.name)
        app.root = Timers()
        app.timer = None
        app.poll_timer_generation = 0
        app.poll_wait_started = None
        app.poll_interval_seconds = 10
        app.poll_interval_var = Value('10')
        app.poll_feedback_var = Value()
        app.status_var = Value()
        app.management_generation = 1
        app.candidate_queue = CandidateQueue()
        app.queue_summary_var = Value()
        app.automatic = app.observing = True
        app.busy = app.closing = app.recovery_mode = app.interrupting = False
        app.cleanup_failed = app.audit_failed = app.pending_login = False
        app.pending_change = app.single_revoke_id = app.delete_permit = app.pending_start = None
        app.start_refresh_required = False
        app.action_states = {}
        app.collect = Mock()
        app._update_buttons = Mock()
        self.clock = patch('quota_guard.ui.time.monotonic', return_value=100)
        self.now = self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def test_default_wait_is_ten_seconds_after_completion(self):
        self.app._schedule_collection_poll()
        timer = self.app.timer
        self.assertEqual(self.app.root.pending[timer][0], 10000)
        self.app.collect.assert_not_called()
        self.app.root.fire(timer)
        self.app.collect.assert_called_once_with()
        self.assertIsNone(self.app.timer)

    def test_change_replaces_timer_and_keeps_original_completion_time(self):
        self.app._schedule_collection_poll()
        old = self.app.timer
        stale_callback = self.app.root.pending[old][1]
        self.now.return_value = 104
        self.app.poll_interval_var.set('20')
        self.app.apply_poll_interval()
        new = self.app.timer
        self.assertNotEqual(old, new)
        self.assertEqual(set(self.app.root.pending), {new})
        self.assertEqual(self.app.root.pending[new][0], 16000)
        stale_callback()
        self.app.collect.assert_not_called()
        self.assertEqual(self.app.timer, new)
        self.assertEqual(load_poll_interval(self.app.home_folder), (20, None))

    def test_shorter_elapsed_wait_schedules_once_without_inline_reentry(self):
        self.app._schedule_collection_poll()
        self.now.return_value = 105
        self.app.poll_interval_var.set('1'); self.app.apply_poll_interval()
        timer = self.app.timer
        self.assertEqual(self.app.root.pending[timer][0], 0)
        self.app.collect.assert_not_called()
        self.app.root.fire(timer)
        self.app.collect.assert_called_once()
        self.assertEqual(self.app.root.pending, {})

    def test_change_during_inflight_waits_for_completion_without_concurrency(self):
        self.app.busy = True
        self.app.poll_interval_var.set('3'); self.app.apply_poll_interval()
        self.assertIsNone(self.app.timer)
        self.assertEqual(self.app.root.pending, {})
        self.app.collect.assert_not_called()
        self.now.return_value = 140
        self.app.busy = False
        self.app._schedule_collection_poll()
        self.assertEqual(self.app.poll_wait_started, 140)
        self.assertEqual(self.app.root.pending[self.app.timer][0], 3000)

    def test_setting_while_stopped_does_not_start_management_or_change_freshness(self):
        self.app.automatic = self.app.observing = False
        self.app.poll_interval_var.set('3600'); self.app.apply_poll_interval()
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertEqual(self.app.root.pending, {})
        self.assertEqual(COLLECTION_MAX_AGE_SECONDS, 300)
        observed = datetime(2026, 11, 1, 12, tzinfo=TZ).timestamp()
        for age, expected in [(15, True), (299, True), (300, False)]:
            self.assertEqual(collection_is_fresh(observed, 100, '2026-11-01',
                wall_now=observed + age, monotonic_now=100 + age), expected)

    def test_invalid_value_or_failed_save_preserves_interval_and_existing_timer(self):
        self.app._schedule_collection_poll()
        original = self.app.timer
        self.app.poll_interval_var.set('0'); self.app.apply_poll_interval()
        self.assertIn('1～3600', self.app.poll_feedback_var.get())
        self.app.poll_interval_var.set('30')
        with patch('quota_guard.ui.save_poll_interval', side_effect=OSError('fixture')):
            self.app.apply_poll_interval()
        self.assertEqual(self.app.poll_interval_seconds, 10)
        self.assertEqual(self.app.timer, original)
        self.assertEqual(set(self.app.root.pending), {original})

    def test_stop_and_later_run_cannot_reuse_old_poll_callback(self):
        self.app._schedule_collection_poll()
        stale = self.app.root.pending[self.app.timer][1]
        self.app.stop(quiet=True)
        self.assertEqual(self.app.root.pending, {})
        self.app.automatic = self.app.observing = True
        stale()
        self.app.collect.assert_not_called()
        self.assertIsNone(self.app.timer)

    def test_callback_cannot_enter_collection_while_busy_or_closing(self):
        for flag in ('busy', 'closing'):
            with self.subTest(flag=flag):
                self.app._schedule_collection_poll()
                timer = self.app.timer
                setattr(self.app, flag, True)
                self.app.root.fire(timer)
                self.app.collect.assert_not_called()
                self.assertIsNone(self.app.timer)
                setattr(self.app, flag, False)


if __name__ == '__main__':
    unittest.main()
