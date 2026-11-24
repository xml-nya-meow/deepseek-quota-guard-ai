from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from quota_guard.core import DataStore, GuardError
from quota_guard.demo_workspace import DemoApp, create_example
from quota_guard.operator_fixture import OfflinePlatform
from quota_guard.workspace import portable_home
import main


class DesktopExampleTests(TestCase):
    def test_example_is_separate_and_starts_with_a_historical_fee(self):
        with TemporaryDirectory() as tmp:
            home = (Path(tmp) / 'sample').resolve()
            platform = OfflinePlatform(seed=7719, names=('Cedar', 'Maple'), cost='1.05')
            store = create_example(home, platform)
            cedar, maple, empty = store.read_evaluation()
            self.assertEqual(str(cedar['spent']), '0.80')
            self.assertEqual(cedar['tracking_id'], platform.target_id)
            self.assertEqual(maple['status'], 'skipped')
            self.assertEqual(empty['status'], 'unassigned')
            self.assertFalse(store.unresolved_actions())
            self.assertIsNone(platform.server)
            app = DemoApp.__new__(DemoApp)
            app.home_folder = home
            self.assertEqual(app._ledger_folder(store.folder), store.folder)
            with self.assertRaises(GuardError):
                app._ledger_folder(home.parent / 'external-ledger')
            self.assertFalse((home.parent / 'external-ledger').exists())

    def test_example_creates_only_the_loopback_worker_also_after_relogin(self):
        app = DemoApp.__new__(DemoApp)
        app.diagnostics_dir = Path('diagnostics')
        app._worker_config = {'origin': 'http://127.0.0.1:12345'}
        with patch('quota_guard.browser_process.IsolatedBrowserService') as service:
            app._new_browser()
            app._new_browser()
        self.assertEqual(service.call_count, 2)
        for call in service.call_args_list:
            target = call.kwargs['_worker_target']
            self.assertEqual(target.func.__name__, '_offline_worker')
            self.assertIs(target.keywords['config'], app._worker_config)

    def test_launcher_opens_only_explicit_demo_and_reuses_open_instance(self):
        child = Mock()
        child.poll.return_value = None
        with patch('main.subprocess.Popen', return_value=child) as spawn, patch('main.messagebox.showinfo'):
            launch = main.demo_launcher(Mock())
            launch()
            launch()
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][-1], '--demo')
        self.assertNotIn('--data-dir', spawn.call_args.args[0])

    def test_demo_rejects_existing_data_dir_before_opening_gui(self):
        with patch('sys.argv', ['main', '--demo', '--data-dir', 'live']), patch('sys.stderr'), patch('main.tk.Tk') as gui:
            with self.assertRaises(SystemExit):
                main.main()
            gui.assert_not_called()

    def test_fresh_portable_folder_and_existing_root_ledger(self):
        with TemporaryDirectory() as tmp:
            binary_folder = Path(tmp)
            self.assertEqual(portable_home(binary_folder), binary_folder / '用户数据')
            pointer = binary_folder / '当前账本.json'
            pointer.write_text('{', encoding='utf-8')
            self.assertEqual(portable_home(binary_folder), binary_folder)
            pointer.unlink()
            incomplete = binary_folder / '.initializing-list'
            incomplete.touch()
            self.assertEqual(portable_home(binary_folder), binary_folder)
            incomplete.unlink()
            (binary_folder / DataStore.DATABASE).write_bytes(b'fixture-legacy-database')
            self.assertEqual(portable_home(binary_folder), binary_folder)
