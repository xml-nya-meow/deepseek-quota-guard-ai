"""Portable integration checks use synthetic assets and never launch a model."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from types import ModuleType
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import build
import main


class OperatorPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'source'
        self.bundle = self.base / 'bundle'
        self.source.mkdir()
        self.bundle.mkdir()
        self.runtime = ModuleType('quota_guard.operator_runtime')
        self.runtime.verify_assets = Mock()
        self.patch = patch.dict('sys.modules', {'quota_guard.operator_runtime': self.runtime})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def manifest(self, files):
        data = json.dumps({'files': files}).encode()
        (self.source / 'asset-lock.json').write_bytes(data)
        return data

    def test_only_manifest_members_are_copied_and_both_trees_verified(self):
        files = ['engine/llama-server.exe', 'models/model.gguf', 'metadata/template.jinja',
                 'metadata/LICENSE']
        lock = self.manifest(files)
        for relative in files:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('synthetic:' + relative).encode())
        (self.source / 'model.gguf.part').write_bytes(b'unfinished download')
        (self.source / 'private-profile').mkdir()
        (self.source / 'private-profile' / 'Cookies').write_bytes(b'never package')
        self.assertEqual(build.copy_operator_assets(self.source, self.bundle), sha256(lock).hexdigest())
        target = self.bundle / 'operator-ai'
        self.assertEqual({p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()},
                         {'asset-lock.json', *files})
        self.assertEqual(self.runtime.verify_assets.call_args_list[0].args, (self.source.resolve(),))
        self.assertEqual(self.runtime.verify_assets.call_args_list[1].args, (target,))
        for relative in files:
            self.assertEqual((target / relative).read_bytes(), (self.source / relative).read_bytes())

    def test_invalid_asset_verification_stops_before_copy(self):
        self.runtime.verify_assets.side_effect = ValueError('fixture mismatch')
        with self.assertRaises(ValueError):
            build.copy_operator_assets(self.source, self.bundle)
        self.assertEqual(list(self.bundle.iterdir()), [])

    def test_traversal_is_refused_even_if_asset_verifier_is_replaced(self):
        self.manifest(['../../escape-file'])
        with self.assertRaises(RuntimeError):
            build.copy_operator_assets(self.source, self.bundle)
        self.assertFalse((self.base / 'escape-file').exists())

    def test_existing_operator_tree_is_never_overwritten(self):
        self.manifest([])
        target = self.bundle / 'operator-ai'
        target.mkdir()
        sentinel = target / 'keep'
        sentinel.write_bytes(b'old bundle')
        with self.assertRaises(FileExistsError):
            build.copy_operator_assets(self.source, self.bundle)
        self.assertEqual(sentinel.read_bytes(), b'old bundle')

    def test_offline_operator_reports_are_not_release_assets(self):
        for name in ('operator-acceptance.json', 'operator-smoke.json', '状态.db', '人员额度.xlsx'):
            with self.subTest(name=name):
                sample = self.bundle / name
                sample.write_bytes(b'synthetic local runtime data')
                with self.assertRaises(RuntimeError):
                    build.validate_bundle(self.bundle)
                sample.unlink()

    def test_build_output_cannot_be_inside_an_input_resource(self):
        ai_source = self.base / 'diagnostic-assets'
        ai_source.mkdir()
        (ai_source / 'asset-lock.json').write_bytes((build.ROOT / 'local_ai' / 'asset-lock.json').read_bytes())
        browser_source = self.base / 'browser-assets'
        browser_source.mkdir()
        for resource in (self.source, ai_source, browser_source):
            args = SimpleNamespace(ai_assets=ai_source, operator_assets=self.source,
                                   runtime=browser_source, output=resource / 'new-output', install_browser=False)
            with self.subTest(resource=resource), patch('build.platform.python_version', return_value='3.13.17'), \
                    patch('build.platform.machine', return_value='AMD64'), patch('build.sys.platform', 'win32'), \
                    patch('build.locked_dependencies', return_value={}), \
                    patch('quota_guard.local_ai_runtime.verify_assets'), \
                    patch('quota_guard.build_identity.source_build_identity', return_value={}), \
                    patch('build.subprocess.run') as process:
                with self.assertRaisesRegex(RuntimeError, 'separate directories'):
                    build.build(args)
                self.assertFalse(args.output.exists())
                process.assert_not_called()


class OperatorAcceptanceCliTests(unittest.TestCase):
    def test_explicit_path_and_exclusive_mode_are_required(self):
        for args in (['--operator-acceptance'], ['--operator-acceptance', '--ai-smoke']):
            with self.subTest(args=args), patch('sys.argv', ['main', *args]), patch('sys.stderr'):
                with self.assertRaises(SystemExit) as error:
                    main.main()
                self.assertEqual(error.exception.code, 2)

    def test_failed_acceptance_never_opens_normal_gui(self):
        runner = ModuleType('quota_guard.operator_acceptance')
        for result in ({'passed': False, 'real_requests': 0}, {'passed': True, 'real_requests': 1},
                       {'passed': True}, {'passed': True, 'real_requests': False}, {}, None, []):
            with self.subTest(result=result):
                runner.run_operator_acceptance = Mock(return_value=result)
                with patch.dict('sys.modules', {'quota_guard.operator_acceptance': runner}), \
                        patch('sys.argv', ['main', '--operator-acceptance', '--data-dir', 'fixture-new']), \
                        patch('main.tk.Tk') as gui, patch('main.messagebox.showerror') as dialog:
                    self.assertEqual(main.main(), 1)
                    gui.assert_not_called()
                    dialog.assert_not_called()

    def test_runner_success_and_exception_produce_explicit_exit_status(self):
        runner = ModuleType('quota_guard.operator_acceptance')
        runner.run_operator_acceptance = Mock(return_value={'passed': True, 'real_requests': 0})
        with patch.dict('sys.modules', {'quota_guard.operator_acceptance': runner}), \
                patch('sys.argv', ['main', '--operator-acceptance', '--data-dir', 'fixture-new']), \
                patch('main.tk.Tk') as gui:
            self.assertEqual(main.main(), 0)
            runner.run_operator_acceptance.assert_called_once_with(Path('fixture-new'))
            runner.run_operator_acceptance.side_effect = OSError('synthetic private detail')
            self.assertEqual(main.main(), 1)
            gui.assert_not_called()


if __name__ == '__main__':
    unittest.main()
