"""Offline viewer for the desktop fee review, using disposable synthetic ledgers."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import tkinter as tk
from tkinter import ttk
from uuid import NAMESPACE_URL, uuid5

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quota_guard.appearance import apply_theme, resolve_theme
from quota_guard.core import DataStore, Person, TZ
from quota_guard.fee_review import FeeReviewData, FeeReviewFrame


SCENES = ('首次读取', '第二次读取 · 个人跨线', '公共人均额度为零')


def example_views(directory: Path) -> tuple[FeeReviewData, ...]:
    now = datetime.now(TZ)
    end_date = now.date().isoformat()
    people = [
        Person('研读组', Decimal('1')),
        Person('设计组', Decimal('12')),
        Person('测试组', Decimal('8')),
        Person('临时项目', Decimal('0')),
        Person('预留项目', Decimal('-1')),
        Person('', Decimal('-1')),
    ]
    keys = [
        {'name': p.name, 'tracking_id': str(uuid5(NAMESPACE_URL, 'quota-preview/' + p.name))}
        for p in people if p.name and p.amount >= 0
    ]
    periods = {}
    for index, key in enumerate(keys):
        created = now - timedelta(days=7 + index * 3)
        periods[key['tracking_id']] = {
            'created_at': int(created.timestamp()),
            'start_date': created.date().isoformat(),
            'end_date': end_date,
        }

    def create_ledger(name, budget):
        ledger = DataStore(directory / name)
        ledger.initialize(len(people), budget, end_date, fee_basis='key_lifetime')
        ledger.save_people(people, allow_unassigned=True)
        ledger.bind_keys(keys, 'local-demo')
        return ledger

    def read(ledger, source, values, observed):
        totals = {key['tracking_id']: Decimal(value) for key, value in zip(keys, values)
                  if value is not None}
        snapshot = ledger.apply_snapshot_result(
            totals, source, end_date, end_date, fee_basis='key_lifetime', key_periods=periods)
        config = ledger.load()
        rows = tuple({**row, **periods.get(row.get('tracking_id'), {})}
                     for row in snapshot.rows)
        missing = snapshot.coverage_summary['active_missing_count']
        return FeeReviewData(
            rows=rows, total_budget=config.total_budget, people_count=config.people_count,
            share=config.share, basis_text='各 Key 自创建日起累计（UTC+8）',
            account_text='已核验 · 本地示例账户', observed_text=observed,
            identity_current=True, fees_current=True,
            coverage_text=f'本次取得 {len(totals)} 枚完整费用，{missing} 枚待补全。',
        )

    ledger = create_ledger('personal', '60')
    first = read(ledger, 'first', ('0.80', '4.20', '8.50', '0'), '示例第 1 次读取')
    second = read(ledger, 'second', ('1.05', '4.20', None, '0'), '示例第 2 次读取')
    public = read(create_ledger('public', '5'), 'public', ('0.80', '4.20', None, '0'),
                  '示例读取 · 总预算 5 元')
    return first, second, public


def create_viewer(root: tk.Tk, views):
    root.title('DeepSeek 额度管理器 · 费用核对预览')
    width = min(1040, max(320, root.winfo_screenwidth() - 50))
    height = min(680, max(320, root.winfo_screenheight() - 80))
    root.geometry(f'{width}x{height}')
    root.minsize(min(width, 760), min(height, 510))
    style = ttk.Style(root)
    if 'vista' in style.theme_names():
        style.theme_use('vista')
    root.option_add('*Font', ('Microsoft YaHei UI', 10))
    style.configure('Treeview', rowheight=31, font=('Microsoft YaHei UI', 10))
    style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'))
    style.configure('Muted.TLabel', foreground='#526174')
    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)
    header = ttk.Frame(root, padding=(16, 12, 16, 8))
    header.grid(row=0, column=0, sticky='ew')
    ttk.Label(header, text='离线示例', font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
    scene = tk.StringVar(root, value=SCENES[1])
    selector = ttk.Combobox(header, textvariable=scene, values=SCENES, state='readonly',
                            width=24, takefocus=True)
    selector.pack(side='left', padx=(12, 0))
    ttk.Label(header, text='合成账本  ·  只读预览').pack(side='right')
    panel = FeeReviewFrame(root)
    panel.grid(row=1, column=0, sticky='nsew', padx=10, pady=(0, 10))
    apply_theme(root, resolve_theme('system'))

    def show(index):
        selector.current(index)
        panel.show(views[index])
        visible = panel.tree.get_children()
        if visible and not panel.tree.selection():
            panel.tree.selection_set(visible[0])
            panel.tree.focus(visible[0])

    selector.bind('<<ComboboxSelected>>', lambda _event: show(selector.current()))
    show(1)
    return show


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Render all three examples and exit')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='quota-review-preview-') as temp:
        views = example_views(Path(temp))
    root = tk.Tk()
    if args.check:
        root.withdraw()
    show = create_viewer(root, views)
    if args.check:
        for index in range(len(views)):
            show(index)
            root.update_idletasks()
        root.destroy()
    else:
        root.mainloop()


if __name__ == '__main__':
    main()
