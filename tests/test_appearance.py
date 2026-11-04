"""Appearance stays local and cannot modify a ledger or the Windows theme."""
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch

from quota_guard import appearance


class AppearancePreferencesTests(unittest.TestCase):
    def test_default_follows_system_without_creating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(appearance.load_theme_mode(Path(folder)), ('system', None))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_modes_roundtrip_without_changing_ledger_or_other_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            protected = {'界面偏好.json': b'{"schema":1,"poll_interval_seconds":20}',
                         '管理设置.json': b'ledger settings sentinel',
                         '人员额度.xlsx': b'workbook sentinel'}
            for name, content in protected.items():
                (home / name).write_bytes(content)
            for mode in appearance.THEME_MODES:
                self.assertEqual(appearance.save_theme_mode(home, mode), mode)
                self.assertEqual(appearance.load_theme_mode(home), (mode, None))
            self.assertEqual(protected, {name: (home / name).read_bytes() for name in protected})

    def test_invalid_modes_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            for mode in ('Dark', '', 'auto', ' dark ', None, False, 0, [], {}):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    appearance.save_theme_mode(home, mode)
            self.assertEqual(list(home.iterdir()), [])

    def test_corrupt_or_unknown_preferences_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            path = home / appearance.APPEARANCE_FILENAME
            for text in ('broken', '[]', '{"schema":true,"theme_mode":"dark"}',
                         '{"schema":2,"theme_mode":"dark"}',
                         '{"schema":1,"theme_mode":"unknown"}',
                         '{"schema":1,"theme_mode":"dark","unexpected":1}'):
                with self.subTest(text=text):
                    path.write_text(text, encoding='utf-8')
                    mode, notice = appearance.load_theme_mode(home)
                    self.assertEqual(mode, 'system')
                    self.assertIn('原文件保留', notice)
                    self.assertEqual(path.read_text(encoding='utf-8'), text)

    def test_failed_save_keeps_the_previous_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            appearance.save_theme_mode(home, 'dark')
            with patch('quota_guard.core.os.replace', side_effect=OSError('fixture')):
                with self.assertRaises(OSError):
                    appearance.save_theme_mode(home, 'light')
            self.assertEqual(appearance.load_theme_mode(home), ('dark', None))
            self.assertEqual([p.name for p in home.iterdir()], [appearance.APPEARANCE_FILENAME])


class SystemAppearanceTests(unittest.TestCase):
    @staticmethod
    def registry():
        registry = MagicMock()
        registry.HKEY_CURRENT_USER = 'current-user'
        registry.KEY_READ = 0x20019
        registry.REG_DWORD = 4
        return registry

    def test_only_reads_the_windows_apps_setting(self):
        registry = self.registry()
        with patch.object(appearance, 'winreg', registry):
            registry.QueryValueEx.return_value = (0, registry.REG_DWORD)
            self.assertEqual(appearance.read_system_theme(), 'dark')
            registry.OpenKey.assert_called_once_with(registry.HKEY_CURRENT_USER,
                appearance._WINDOWS_THEME_KEY, 0, registry.KEY_READ)
            registry.QueryValueEx.assert_called_once_with(
                registry.OpenKey.return_value.__enter__.return_value, 'AppsUseLightTheme')
            registry.OpenKey.return_value.__exit__.assert_called_once()
            registry.SetValueEx.assert_not_called()
            registry.CreateKey.assert_not_called()

    def test_unavailable_or_invalid_registry_falls_back_to_light(self):
        registry = self.registry()
        with patch.object(appearance, 'winreg', registry):
            for pair in [(1, registry.REG_DWORD), ('0', 1), (None, 0), (True, registry.REG_DWORD),
                         (-1, registry.REG_DWORD)]:
                with self.subTest(pair=pair):
                    registry.QueryValueEx.return_value = pair
                    self.assertEqual(appearance.read_system_theme(), 'light')
            registry.OpenKey.side_effect = OSError('fixture unavailable')
            self.assertEqual(appearance.read_system_theme(), 'light')
        with patch.object(appearance, 'winreg', None):
            self.assertEqual(appearance.read_system_theme(), 'light')

    def test_manual_modes_do_not_read_windows_and_system_resolves_fresh(self):
        with patch.object(appearance, 'read_system_theme', side_effect=['dark', 'light']) as read:
            self.assertEqual(appearance.resolve_theme('dark'), 'dark')
            self.assertEqual(appearance.resolve_theme('light'), 'light')
            read.assert_not_called()
            self.assertEqual(appearance.resolve_theme('system'), 'dark')
            self.assertEqual(appearance.resolve_theme('system'), 'light')
        with self.assertRaises(ValueError):
            appearance.resolve_theme('unknown')


def contrast(first, second):
    def luminance(color):
        rgb = [int(color[n:n + 2], 16) / 255 for n in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in rgb]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    left, right = sorted((luminance(first), luminance(second)))
    return (right + 0.05) / (left + 0.05)


class PaletteTests(unittest.TestCase):
    def test_normal_and_status_text_has_readable_contrast(self):
        for theme, colors in appearance.PALETTES.items():
            for foreground in ('text', 'muted', 'warning', 'danger'):
                for background in ('background', 'panel'):
                    with self.subTest(theme=theme, fg=foreground, bg=background):
                        self.assertGreaterEqual(contrast(colors[foreground], colors[background]), 4.5)
            self.assertGreaterEqual(contrast(colors['selection_text'], colors['selection']), 4.5)


class TkAppearanceTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_dark_light_switch_preserves_content_selection_and_disabled_controls(self):
        root = self.root
        frame = ttk.Frame(root)
        entry = ttk.Entry(frame)
        entry.insert(0, '0.0000000001')
        combo = ttk.Combobox(frame, values=('跟随系统', '浅色', '深色'), state='readonly')
        combo.current(0)
        button = ttk.Button(frame, text='保存', state='disabled')
        text = tk.Text(frame, background='#f6f8fa')
        text.insert('1.0', '已有处理记录')
        text.configure(state='disabled')
        tree = ttk.Treeview(frame, columns=('amount',), show='headings')
        item = tree.insert('', 'end', values=('0.1',), tags=('triggered',))
        tree.selection_set(item)
        style = ttk.Style(root)
        for theme in ('dark', 'light', 'dark'):
            with self.subTest(theme=theme):
                appearance.apply_theme(root, theme)
                colors = appearance.PALETTES[theme]
                self.assertEqual(root.cget('background'), colors['background'])
                self.assertEqual(text.cget('background'), colors['panel'])
                self.assertEqual(text.cget('foreground'), colors['text'])
                self.assertEqual(text.cget('state'), 'disabled')
                self.assertEqual(text.get('1.0', 'end-1c'), '已有处理记录')
                self.assertEqual(entry.get(), '0.0000000001')
                self.assertEqual(combo.get(), '跟随系统')
                self.assertTrue(combo.instate(['readonly']))
                self.assertTrue(button.instate(['disabled']))
                self.assertEqual(tree.selection(), (item,))
                self.assertEqual(tree.item(item, 'values'), ('0.1',))
                # Windows Tcl can return a color object rather than a Python str.
                self.assertEqual(str(tree.tag_configure('triggered', 'foreground')), colors['danger'])
                self.assertEqual(str(style.lookup('TEntry', 'fieldbackground')), colors['input'])
                self.assertEqual(str(style.lookup('TCombobox', 'foreground', ('readonly',))), colors['text'])
                self.assertEqual(str(style.lookup('TButton', 'foreground', ('disabled',))), colors['disabled'])
                self.assertEqual(str(style.lookup('Treeview', 'foreground', ('selected',))), colors['selection_text'])

    def test_later_custom_dialog_inherits_palette_and_can_be_recolored(self):
        appearance.apply_theme(self.root, 'dark')
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        label = tk.Label(dialog, text='提示')
        entry = tk.Entry(dialog)
        entry.insert(0, '保留')
        self.assertEqual(label.cget('foreground'), appearance.PALETTES['dark']['text'])
        self.assertEqual(dialog.cget('background'), appearance.PALETTES['dark']['background'])
        appearance.apply_theme(self.root, 'light')
        self.assertEqual(label.cget('foreground'), appearance.PALETTES['light']['text'])
        self.assertEqual(entry.cget('background'), appearance.PALETTES['light']['input'])
        self.assertEqual(entry.get(), '保留')

    def test_existing_tcl_only_dropdown_is_recolored_without_python_wrappers(self):
        combo = ttk.Combobox(self.root, values=('浅色', '深色'), state='readonly')
        popup = str(combo) + '.fixture_popdown'
        frame = popup + '.frame'
        listing = frame + '.list'
        self.root.tk.call('toplevel', popup)
        self.root.tk.call('wm', 'withdraw', popup)
        self.root.tk.call('frame', frame)
        self.root.tk.call('listbox', listing)
        self.root.tk.call(listing, 'insert', 'end', '浅色', '深色')
        for theme in ('dark', 'light'):
            appearance.apply_theme(self.root, theme)
            colors = appearance.PALETTES[theme]
            self.assertEqual(self.root.tk.call(listing, 'cget', '-background'), colors['input'])
            self.assertEqual(self.root.tk.call(listing, 'cget', '-foreground'), colors['text'])
            self.assertEqual(self.root.tk.call(listing, 'get', 0), '浅色')


if __name__ == '__main__':
    unittest.main()
