"""Full desktop example backed by an ephemeral ledger and a loopback console."""
from functools import partial
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import tkinter as tk
from tkinter import messagebox
from decimal import Decimal

from .core import DataStore, GuardError, Person
from .ui import App


class DemoApp(App):
    is_demo = True

    def __init__(self, root, home, store, worker_config):
        self._worker_config = worker_config
        super().__init__(root, store.folder, home_folder=home,
                         responsibility_directory=home / 'registry')

    def _ledger_folder(self, folder):
        target = super()._ledger_folder(folder)
        if not target.is_relative_to(self.home_folder):
            raise GuardError('示例窗口只打开本次示例目录中的账本。要选择自己的账本，请回到主窗口。')
        return target

    def _new_browser(self):
        # Remain bound to the same synthetic platform after relogin or interruption.
        from .browser_process import IsolatedBrowserService
        from .operator_acceptance import _offline_worker
        return IsolatedBrowserService(diagnostics_dir=self.diagnostics_dir,
            _worker_target=partial(_offline_worker, config=self._worker_config))


def create_example(home, platform):
    from .browser_service import key_fingerprint
    from .responsibility_gate import ResponsibilityGate
    store = DataStore(home / '示例账本')
    store.initialize(3, '30', platform.today, fee_basis='key_lifetime')
    store.save_people([Person('Cedar', Decimal('1.00')), Person('Maple', Decimal('-1')),
                       Person('', Decimal('-1'))])
    keys = [{'name': key['name'], 'tracking_id': key['tracking_id']} for key in platform.keys]
    store.bind_keys(keys, key_fingerprint(keys))
    key = next(key for key in platform.keys if key['tracking_id'] == platform.target_id)
    period = {'created_at': key['created_at'], 'start_date': platform.today, 'end_date': platform.today}
    store.apply_snapshot_result({platform.target_id: Decimal('0.80')}, 'demo-opening-balance',
        platform.today, platform.today, fee_basis='key_lifetime', key_periods={platform.target_id: period})
    ResponsibilityGate(home / 'registry').initialize([store])
    return store


def example_chromium():
    if getattr(sys, 'frozen', False):
        browser_dir = Path(sys._MEIPASS) / 'browser-runtime'
    else:
        browser_dir = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH',
                                        Path(__file__).resolve().parents[1] / 'browser-runtime'))
    matches = list(browser_dir.glob('chromium-*/chrome-win64/chrome.exe'))
    if len(matches) != 1:
        raise GuardError('未找到配套 Chromium。请将完整程序包解压到新文件夹后打开。')
    return matches[0]


def run_demo():
    from .operator_fixture import OfflinePlatform
    from .operator_runtime import assets_directory
    root = tk.Tk()
    root.withdraw()
    platform = None
    try:
        with TemporaryDirectory(prefix='deepseek-example-', ignore_cleanup_errors=True) as temp:
            home = Path(temp).resolve()
            chromium = example_chromium()
            platform = OfflinePlatform(seed=7719, names=('Cedar', 'Maple'), cost='1.05').start()
            store = create_example(home, platform)
            config = {'origin': platform.origin, 'assets': str(assets_directory()),
                      'chromium': str(chromium), 'model_state': str(home / 'model-state')}
            app = DemoApp(root, home, store, config)
            root.deiconify()
            root.mainloop()
            return 0 if app.window_destroyed else 1
    except (OSError, GuardError) as exc:
        message = str(exc) if isinstance(exc, GuardError) else '请检查程序资源和临时目录是否可用，然后重新打开示例。'
        messagebox.showerror('示例账本未能打开', message, parent=root)
        return 1
    finally:
        if platform is not None:
            platform.close()
        try:
            root.destroy()
        except tk.TclError:
            pass
