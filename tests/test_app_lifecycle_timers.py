"""Offline timer ownership and real-Tk close/retry regression tests.

Each App has a synthetic ledger and an explicitly injected registry.  A second
Tk interpreter keeps processing after the first root is destroyed.  This tests
application-owned after callbacks, not every native ttk ThemeChanged warning.
"""
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
import tempfile
from threading import Event
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, GuardError, TZ
from quota_guard.responsibility_gate import ResponsibilityGate
from quota_guard.ui import App


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'
TIMER_FIELDS = ('workbook_timer', 'theme_timer', 'initial_load_timer')


class CloseBrowser:
    def __init__(self):
        self.interrupt_future = Future()
        self.interrupt_future.set_result(None)
        self.interrupt_error = None
        self.close_error = None
        self.close_entered = Event()
        self.close_release = Event()
        self.close_release.set()
        self.closes = 0

    def interrupt(self):
        if self.interrupt_error:
            raise self.interrupt_error
        return self.interrupt_future

    def close(self):
        self.closes += 1
        self.close_entered.set()
        if not self.close_release.wait(5):
            raise AssertionError('synthetic close release not received')
        if self.close_error:
            raise self.close_error


class AppLifecycleTimerTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='app-timers-', dir=TEMP_ROOT)
        self.base = Path(self.temporary.name)
        self.apps = []
        self.errors = []
        self.patchers = [patch('quota_guard.ui.messagebox.showinfo'),
                         patch('quota_guard.ui.messagebox.showerror'),
                         patch('quota_guard.ui.messagebox.askyesno', return_value=True)]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        # Assertions inspect product cleanup before this fixture-only cleanup.
        for app in reversed(self.apps):
            if app.browser is not None:
                app.browser.close_release.set()
            for timer in app.root.tk.call('after', 'info'):
                app.root.tk.call('after', 'cancel', timer)
            if app.target_lock is not None:
                app.target_lock.close()
            app.folder_lock.close()
            if not app.window_destroyed:
                app.root.destroy()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def make_app(self, *, loaded=True):
        folder = self.base / f'app-{len(self.apps)}'
        store = DataStore(folder)
        store.initialize(2, '100', datetime.now(TZ).date().isoformat())
        directory = folder / 'synthetic-control' / 'responsibility'
        ResponsibilityGate(directory).initialize([store])
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda *args: self.errors.append(('python', repr(args)))
        # A direct Tcl command survives root widget destruction and therefore
        # captures dangling after scripts during the second interpreter's pump.
        root.tk.createcommand('capture_lifecycle_error',
                              lambda message: self.errors.append(('tcl', str(message))))
        root.tk.eval('proc bgerror {message} {capture_lifecycle_error $message}')
        app = App(root, folder, responsibility_directory=directory)
        self.apps.append(app)
        if loaded:
            self.until(lambda: app.config is not None)
        return app

    def until(self, predicate, *, timeout=4):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for app in self.apps:
                if not app.window_destroyed:
                    app.root.update()
            if predicate():
                return
            time.sleep(.002)
        self.fail('Synthetic Tk lifecycle did not reach expected state')

    def commands(self, app):
        return {str(timer): str(app.root.tk.call('after', 'info', timer)[0])
                for timer in app.root.tk.call('after', 'info')}

    def assert_watchers_once(self, app, *, initial=False):
        commands = self.commands(app)
        expected = {'workbook_timer': '_watch_workbook', 'theme_timer': '_watch_theme'}
        if initial:
            expected['initial_load_timer'] = '_initial_load'
        else:
            self.assertIsNone(app.initial_load_timer)
        for attribute, suffix in expected.items():
            self.assertIn(getattr(app, attribute), commands)
            self.assertEqual(sum(command.endswith(suffix) for command in commands.values()), 1)

    def assert_paused(self, app):
        self.assertTrue(all(getattr(app, field) is None for field in TIMER_FIELDS))
        self.assertFalse(any(command.endswith(('_watch_workbook', '_watch_theme', '_initial_load'))
                             for command in self.commands(app).values()))

    def assert_no_invalid_callbacks(self):
        self.assertFalse([message for kind, message in self.errors
                          if kind == 'python' or 'invalid command name' in message], self.errors)

    def test_repeated_resume_and_direct_watch_do_not_duplicate_lifecycle_timers(self):
        app = self.make_app(loaded=False)
        original = {field: getattr(app, field) for field in TIMER_FIELDS}
        for _ in range(3):
            app._resume_lifecycle_timers()
        self.assertEqual({field: getattr(app, field) for field in TIMER_FIELDS}, original)
        self.assert_watchers_once(app, initial=True)
        self.until(lambda: app.config is not None)
        app._watch_workbook()
        app._watch_theme()
        app._resume_lifecycle_timers()
        self.assert_watchers_once(app)
        app.close()
        self.until(lambda: app.window_destroyed)
        self.assert_paused(app)
        self.assert_no_invalid_callbacks()

    def test_due_close_cancels_own_timers_and_leaves_second_tk_timer_running(self):
        first = self.make_app(loaded=False)
        second = self.make_app(loaded=False)
        first._pause_lifecycle_timers()
        # All callbacks become due together. Close was queued first; its
        # cancellation must precede root destruction in the same event loop.
        first.root.after(0, first.close)
        first._arm_lifecycle_timer('workbook_timer', 0, first._watch_workbook)
        first._arm_lifecycle_timer('theme_timer', 0, first._watch_theme)
        first._arm_lifecycle_timer('initial_load_timer', 0, first._initial_load)
        poll_calls = []
        first.timer = first.root.after(0, lambda: poll_calls.append('unexpected'))
        other_calls = []
        second.root.after(0, lambda: other_calls.append('other interpreter'))
        self.until(lambda: first.window_destroyed and bool(other_calls))
        self.assertIsNone(first.config, 'Closing before initial load must not read the ledger')
        self.assertEqual(poll_calls, [])
        self.assertIsNone(first.timer)
        self.assert_paused(first)
        # Advance the surviving interpreter after the first Tk root is gone.
        second.root.after(30, lambda: other_calls.append('after first destroy'))
        self.until(lambda: len(other_calls) == 2 and second.config is not None)
        self.assert_watchers_once(second)
        self.assert_no_invalid_callbacks()
        second.close()
        self.until(lambda: second.window_destroyed)

    def test_close_interrupt_exception_restores_initial_load_and_single_watchers(self):
        app = self.make_app(loaded=False)
        browser = CloseBrowser()
        browser.interrupt_error = GuardError('synthetic interrupt refused')
        app.browser = browser
        app.close()
        self.assertFalse(app.closing)
        self.assertFalse(app.window_destroyed)
        self.assert_watchers_once(app, initial=True)
        app._resume_lifecycle_timers()
        self.assert_watchers_once(app, initial=True)
        self.until(lambda: app.config is not None)
        self.assert_watchers_once(app)
        browser.interrupt_error = None
        app.close()
        self.until(lambda: app.window_destroyed)
        self.assertEqual(browser.closes, 1)
        self.assert_paused(app)
        self.assert_no_invalid_callbacks()

    def test_async_interrupt_failure_restores_watchers_without_restarting_management(self):
        app = self.make_app()
        browser = CloseBrowser()
        browser.interrupt_future = Future()
        browser.interrupt_future.set_exception(GuardError('synthetic cleanup failure'))
        app.browser = browser
        app.close()
        self.assert_paused(app)
        self.until(lambda: not app.closing)
        self.assert_watchers_once(app)
        self.assertFalse(app.automatic)
        self.assertFalse(app.observing)
        self.assertEqual(browser.closes, 0)
        browser.interrupt_future = Future()
        browser.interrupt_future.set_result(None)
        app.close()
        self.until(lambda: app.window_destroyed)
        self.assert_no_invalid_callbacks()

    def test_browser_close_failure_restores_one_watch_chain_then_retry_closes(self):
        app = self.make_app()
        browser = CloseBrowser()
        browser.close_error = GuardError('synthetic browser close failed')
        app.browser = browser
        app.close()
        self.until(lambda: not app.closing)
        self.assertTrue(app.cleanup_failed)
        self.assert_watchers_once(app)
        for _ in range(3):
            app._resume_lifecycle_timers()
        self.assert_watchers_once(app)
        browser.close_error = None
        app.close()
        self.until(lambda: app.window_destroyed)
        self.assertEqual(browser.closes, 2)
        self.assert_paused(app)
        self.assert_no_invalid_callbacks()

    def test_lock_close_failure_retains_window_and_restores_watchers(self):
        app = self.make_app()
        with patch.object(app.folder_lock, 'close', side_effect=GuardError('synthetic lock failure')):
            app.close()
            self.until(lambda: not app.closing)
        self.assertFalse(app.window_destroyed)
        self.assert_watchers_once(app)
        app.close()
        self.until(lambda: app.window_destroyed)
        self.assert_paused(app)
        self.assert_no_invalid_callbacks()

    def test_close_wait_callbacks_survive_pause_until_browser_cleanup_finishes(self):
        app = self.make_app()
        browser = CloseBrowser()
        browser.interrupt_future = Future()
        browser.close_release.clear()
        app.browser = browser
        app.close()
        self.assert_paused(app)
        self.until(lambda: any(command.endswith('wait_interrupt') for command in self.commands(app).values()))
        self.assertFalse(app.window_destroyed)
        browser.interrupt_future.set_result(None)
        self.until(browser.close_entered.is_set)
        self.assertTrue(any(command.endswith('wait_close') for command in self.commands(app).values()))
        self.assert_paused(app)
        self.assertFalse(app.window_destroyed)
        browser.close_release.set()
        self.until(lambda: app.window_destroyed)
        self.assertEqual(browser.closes, 1)
        self.assert_paused(app)
        self.assert_no_invalid_callbacks()


if __name__ == '__main__':
    unittest.main()
