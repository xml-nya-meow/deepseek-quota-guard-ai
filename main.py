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
    parser.add_argument('--smoke', action='store_true', help='Open then close UI without network or data mutation')
    parser.add_argument('--browser-smoke', action='store_true', help='Check bundled Chromium using about:blank only')
    parser.add_argument('--ai-smoke', action='store_true', help='Run one offline teaching case using the bundled model')
    args = parser.parse_args()
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent / 'data'
    home = (args.data_dir or base).resolve()
    folder = home
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
    try:
        from quota_guard.ui import App
        from quota_guard.workspace import current_ledger
        folder = current_ledger(home)
        app = App(root, folder, home_folder=home)
        if args.smoke:
            root.after(1500, app.close)
        root.mainloop()
    except Exception as exc:
        from quota_guard.core import GuardError
        detail = str(exc) if isinstance(exc, GuardError) else '请确认文件夹完整、目录可写且没有其他实例；保留数据文件，请勿清空统计。'
        messagebox.showerror('启动未完成', detail)
        root.destroy()
        raise

if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    raise SystemExit(main())
