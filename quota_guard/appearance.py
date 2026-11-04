"""Local application appearance; observing the OS never changes its settings."""
from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from .core import DataStore

try:
    import winreg
except ImportError:  # The UI is Windows-only, but preference helpers are portable.
    winreg = None


THEME_MODES = ('system', 'light', 'dark')
APPEARANCE_FILENAME = '外观偏好.json'
_WINDOWS_THEME_KEY = r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'

PALETTES = {
    'light': {
        'background': '#f3f4f6', 'panel': '#ffffff', 'input': '#ffffff',
        'text': '#17212f', 'muted': '#526174', 'warning': '#87510b',
        'danger': '#a52323', 'disabled': '#677384', 'disabled_background': '#e6e9ee',
        'border': '#abb5c3', 'button': '#ffffff', 'hover': '#e2eaf4',
        'selection': '#1769a7', 'selection_text': '#ffffff', 'focus': '#1769a7',
    },
    'dark': {
        'background': '#171b22', 'panel': '#202630', 'input': '#111820',
        'text': '#eef2f7', 'muted': '#b5c1d0', 'warning': '#f5c16c',
        'danger': '#ff9292', 'disabled': '#a0acbc', 'disabled_background': '#29313d',
        'border': '#566477', 'button': '#2c3542', 'hover': '#3a485b',
        'selection': '#255f9f', 'selection_text': '#ffffff', 'focus': '#82b8ff',
    },
}


def validate_theme_mode(mode) -> str:
    if not isinstance(mode, str) or mode not in THEME_MODES:
        raise ValueError('外观只能选择跟随系统、浅色或深色。')
    return mode


def load_theme_mode(home: Path) -> tuple[str, str | None]:
    try:
        data = json.loads((Path(home) / APPEARANCE_FILENAME).read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or set(data) != {'schema', 'theme_mode'}
                or type(data['schema']) is not int or data['schema'] != 1):
            raise ValueError('unsupported appearance preferences')
        return validate_theme_mode(data['theme_mode']), None
    except FileNotFoundError:
        return 'system', None
    except (OSError, UnicodeError, ValueError, TypeError):
        return 'system', '外观偏好无法读取，暂跟随系统；原文件保留，可重新选择外观。'


def save_theme_mode(home: Path, mode) -> str:
    mode = validate_theme_mode(mode)
    DataStore._write_json(Path(home) / APPEARANCE_FILENAME,
                         {'schema': 1, 'theme_mode': mode})
    return mode


def read_system_theme() -> str:
    """Read Windows' app theme, with a light fallback on unavailable settings."""
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_THEME_KEY,
                                0, winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            if kind == winreg.REG_DWORD and type(value) is int and value == 0:
                return 'dark'
        except (OSError, TypeError, ValueError):
            pass
    return 'light'


def resolve_theme(mode: str) -> str:
    mode = validate_theme_mode(mode)
    return read_system_theme() if mode == 'system' else mode


def apply_theme(root: tk.Misc, theme: str) -> None:
    """Recolor this Tk application, preserving values, selections and operations.

    Native Windows dialogs are owned and painted by the OS. Custom Tk dialogs
    inherit these defaults; an extra call after creating one recolors explicit
    widget colors as well. No browser page or Windows preference is modified.
    """
    if theme not in PALETTES:
        raise ValueError('只能应用浅色或深色配色。')
    p = PALETTES[theme]
    style = ttk.Style(root)
    # Native vista controls ignore several supplied colors. Clam supports both
    # palettes consistently, including switching back from dark to light.
    if 'clam' in style.theme_names() and style.theme_use() != 'clam':
        style.theme_use('clam')
    style.configure('.', background=p['background'], foreground=p['text'],
                    fieldbackground=p['input'], bordercolor=p['border'],
                    lightcolor=p['border'], darkcolor=p['border'],
                    troughcolor=p['background'], selectbackground=p['selection'],
                    selectforeground=p['selection_text'],
                    font=('Microsoft YaHei UI', 10))
    style.configure('TFrame', background=p['background'])
    style.configure('TLabel', background=p['background'], foreground=p['text'])
    style.configure('Title.TLabel', font=('Microsoft YaHei UI', 19, 'bold'),
                    foreground=p['text'], background=p['background'])
    style.configure('Muted.TLabel', foreground=p['muted'], background=p['background'])
    style.configure('Warn.TLabel', foreground=p['warning'], background=p['background'])
    style.configure('TLabelframe', background=p['background'], bordercolor=p['border'])
    style.configure('TLabelframe.Label', background=p['background'], foreground=p['text'])

    style.configure('TButton', background=p['button'], foreground=p['text'],
                    bordercolor=p['border'], focuscolor=p['focus'], padding=(8, 5))
    style.map('TButton',
              background=[('disabled', p['disabled_background']), ('pressed', p['hover']),
                          ('active', p['hover'])],
              foreground=[('disabled', p['disabled'])],
              bordercolor=[('focus', p['focus']), ('active', p['focus'])])
    for name in ('TEntry', 'TCombobox', 'TSpinbox'):
        style.configure(name, foreground=p['text'], fieldbackground=p['input'],
                        background=p['button'], bordercolor=p['border'],
                        insertcolor=p['text'], arrowcolor=p['text'],
                        selectbackground=p['selection'], selectforeground=p['selection_text'])
        style.map(name,
                  fieldbackground=[('disabled', p['disabled_background']), ('readonly', p['input'])],
                  foreground=[('disabled', p['disabled']), ('readonly', p['text'])],
                  background=[('disabled', p['disabled_background']), ('active', p['hover'])],
                  arrowcolor=[('disabled', p['disabled'])],
                  bordercolor=[('focus', p['focus'])])
    for name in ('TRadiobutton', 'TCheckbutton'):
        style.configure(name, background=p['background'], foreground=p['text'],
                        indicatorbackground=p['input'], indicatorforeground=p['text'],
                        focuscolor=p['focus'])
        style.map(name, background=[('active', p['background'])],
                  foreground=[('disabled', p['disabled'])],
                  indicatorbackground=[('selected', p['selection']), ('disabled', p['disabled_background'])])
    style.configure('Treeview', background=p['panel'], fieldbackground=p['panel'],
                    foreground=p['text'], bordercolor=p['border'], rowheight=31,
                    font=('Microsoft YaHei UI', 10))
    style.map('Treeview', background=[('selected', p['selection'])],
              foreground=[('selected', p['selection_text'])])
    style.configure('Treeview.Heading', background=p['button'], foreground=p['text'],
                    bordercolor=p['border'], font=('Microsoft YaHei UI', 10, 'bold'))
    style.map('Treeview.Heading', background=[('active', p['hover'])])
    style.configure('TNotebook', background=p['background'], bordercolor=p['border'])
    style.configure('TNotebook.Tab', background=p['button'], foreground=p['muted'], padding=(10, 5))
    style.map('TNotebook.Tab', background=[('selected', p['panel']), ('active', p['hover'])],
              foreground=[('selected', p['text']), ('active', p['text'])])
    for name in ('Vertical.TScrollbar', 'Horizontal.TScrollbar'):
        style.configure(name, background=p['button'], troughcolor=p['background'],
                        arrowcolor=p['text'], bordercolor=p['border'])
        style.map(name, background=[('active', p['hover']), ('pressed', p['hover'])])
    style.configure('TSeparator', background=p['border'])

    # The combobox popdown is a classic Tk Listbox outside the ttk style tree.
    defaults = {
        '*Toplevel.background': p['background'], '*Frame.background': p['background'],
        '*Label.background': p['background'], '*Label.foreground': p['text'],
        '*Button.background': p['button'], '*Button.foreground': p['text'],
        '*Button.activeBackground': p['hover'], '*Button.activeForeground': p['text'],
        '*Entry.background': p['input'], '*Entry.foreground': p['text'],
        '*Entry.insertBackground': p['text'], '*Text.background': p['panel'],
        '*Text.foreground': p['text'], '*Text.insertBackground': p['text'],
        '*Listbox.background': p['input'], '*Listbox.foreground': p['text'],
        '*Listbox.selectBackground': p['selection'],
        '*Listbox.selectForeground': p['selection_text'],
    }
    for option, value in defaults.items():
        root.option_add(option, value)

    def recolor(widget):
        # ttk.Entry also inherits tkinter.Entry, but rejects classic color
        # options. Its colors are supplied by the style above instead.
        if isinstance(widget, ttk.Widget):
            if isinstance(widget, ttk.Treeview):
                for tag, color in [('triggered', p['danger']), ('unknown', p['warning']),
                                   ('skipped', p['muted'])]:
                    widget.tag_configure(tag, foreground=color)
            if isinstance(widget, ttk.Combobox):
                # Already-created popdowns have Tcl widgets without Python
                # wrappers, so winfo_children() cannot visit their Listbox.
                pending = list(widget.tk.splitlist(widget.tk.call('winfo', 'children', widget._w)))
                while pending:
                    path = pending.pop()
                    kind = widget.tk.call('winfo', 'class', path)
                    if kind == 'Listbox':
                        widget.tk.call(path, 'configure', '-background', p['input'],
                                       '-foreground', p['text'], '-selectbackground', p['selection'],
                                       '-selectforeground', p['selection_text'])
                    elif kind in ('Frame', 'Toplevel'):
                        widget.tk.call(path, 'configure', '-background', p['background'])
                    pending.extend(widget.tk.splitlist(widget.tk.call('winfo', 'children', path)))
        elif isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
            widget.configure(background=p['background'])
        elif isinstance(widget, tk.Text):
            widget.configure(background=p['panel'], foreground=p['text'],
                             insertbackground=p['text'], selectbackground=p['selection'],
                             selectforeground=p['selection_text'],
                             highlightbackground=p['border'], highlightcolor=p['focus'])
        elif isinstance(widget, (tk.Entry, tk.Listbox)):
            options = dict(background=p['input'], foreground=p['text'],
                           selectbackground=p['selection'], selectforeground=p['selection_text'])
            if isinstance(widget, tk.Entry):
                options.update(insertbackground=p['text'], disabledbackground=p['disabled_background'],
                               disabledforeground=p['disabled'], readonlybackground=p['input'])
            widget.configure(**options)
        elif isinstance(widget, (tk.Label, tk.Button)):
            widget.configure(background=p['background'], foreground=p['text'])
            if isinstance(widget, tk.Button):
                widget.configure(background=p['button'], activebackground=p['hover'],
                                 activeforeground=p['text'], disabledforeground=p['disabled'])
        for child in widget.winfo_children():
            recolor(child)

    recolor(root)
