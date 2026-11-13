"""Portable entry point. No system configuration changes."""
import argparse
import json
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--smoke', action='store_true', help='Open then close UI using isolated local test data')
    modes.add_argument('--browser-smoke', action='store_true', help='Check bundled Chromium using about:blank only')
    modes.add_argument('--ai-smoke', action='store_true', help='Run one offline teaching case using the bundled model')
    modes.add_argument('--offline-acceptance', action='store_true', help='Check local workflows with synthetic transport in a NEW offline-acceptance-* directory')
    modes.add_argument('--operator-acceptance', action='store_true', help='Run bundled 4B/browser offline acceptance in an explicitly new isolated directory')
    args = parser.parse_args()
    if (args.offline_acceptance or args.operator_acceptance or args.smoke) and args.data_dir is None:
        parser.error('This test mode requires an explicit --data-dir.')
    if args.operator_acceptance:
        try:
            from quota_guard.operator_acceptance import run_operator_acceptance
            result = run_operator_acceptance(args.data_dir)
        except Exception:
            return 1  # Never enter a real session or GUI after an offline failure.
        return 0 if (isinstance(result, dict) and result.get('passed') is True
                     and type(result.get('real_requests')) is int
                     and result['real_requests'] == 0) else 1
    if args.offline_acceptance:
        from quota_guard.offline_acceptance import run_offline_acceptance
        try:
            result = run_offline_acceptance(args.data_dir)
        except Exception:
            return 1  # Never fall through to the real application or an error dialog.
        return 0 if result['passed'] is True else 1
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent / 'data'
    home = (args.data_dir or base).resolve()
    folder = home
    if args.smoke:
        try:
            # Ordinary startup retains its normal behavior. The test must never
            # reopen an existing ledger or default per-user responsibility scope.
            from quota_guard.offline_acceptance import _new_directory
            home = folder = _new_directory(args.data_dir)
        except Exception:
            return 1
    if args.ai_smoke:
        from quota_guard.local_ai_assistant import EXAMPLES, topics_for
        from quota_guard.local_ai_runtime import rank_topics
        case = EXAMPLES[0]
        topics = tuple(topics_for(case))
        result = {'model_ran': False}
        try:
            selected = rank_topics(case.model_json(), topics)
            if not selected or not set(selected) <= set(topics):
                raise ValueError('invalid ranking')
            result = {'model_ran': True, 'topics': list(selected)}
        except Exception:
            pass  # Never serialize raw native output or exception text.
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'ai-smoke.json').write_text(json.dumps(result), encoding='utf-8')
        return 0 if result['model_ran'] else 1
    if args.browser_smoke:
        from quota_guard.browser_process import IsolatedBrowserService as BrowserService
        browser = None
        result = {'browser_started': False, 'reason_code': 'BROWSER_SMOKE_FAILED'}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            browser = BrowserService(diagnostics_dir=folder / '临时诊断日志')
            result = browser.submit('selftest').result(timeout=45)
        except Exception:
            # Windowed self-tests must exit without an exception dialog.
            # SDK exceptions can contain credentials and must not be serialized.
            result = {'browser_started': False, 'reason_code': 'BROWSER_SMOKE_FAILED'}
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    result = {'browser_started': False, 'reason_code': 'BROWSER_SMOKE_CLEANUP_FAILED'}
        try:
            (folder / 'browser-smoke.json').write_text(json.dumps(result), encoding='utf-8')
        except OSError:
            return 1
        return 0 if result.get('browser_started') is True else 1
    root = tk.Tk()
    callback_errors = []
    if args.smoke:
        root.withdraw()
        root.report_callback_exception = lambda *unused: callback_errors.append('TK_CALLBACK_FAILED')
    try:
        from quota_guard.ui import App
        from quota_guard.workspace import current_ledger
        folder = current_ledger(home)
        app = App(root, folder, home_folder=home,
                  responsibility_directory=home / 'registry' if args.smoke else None)
        if args.smoke:
            root.after(1500, app.close)
        root.mainloop()
        if args.smoke:
            result = {'passed': bool(app.window_destroyed and app.store is not None and not callback_errors
                      and all(getattr(app, name) is None for name in
                              ('workbook_timer', 'theme_timer', 'initial_load_timer'))),
                      'callback_errors': len(callback_errors)}
            (home / 'ui-smoke.json').write_text(json.dumps(result), encoding='utf-8')
            return 0 if result['passed'] else 1
    except Exception as exc:
        if args.smoke:
            try:
                root.destroy()
            except tk.TclError:
                pass
            return 1
        from quota_guard.core import GuardError
        detail = str(exc) if isinstance(exc, GuardError) else '请确认文件夹完整、目录可写且没有其他实例；保留数据文件，请勿清空统计。'
        messagebox.showerror('启动未完成', detail)
        root.destroy()
        raise

if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    raise SystemExit(main())
