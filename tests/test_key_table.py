"""Synthetic key-table DOM and parser checks; all external requests are blocked."""
import html
import os
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

from quota_guard.browser_service import BrowserService, parse_tracking_field, key_fingerprint
from quota_guard.core import GuardError


TARGET = 'a0000000-0000-0000-0000-000000000001'
SURVIVOR = 'b0000000-0000-0000-0000-000000000002'
HEADERS = ['名称', 'Tracking ID', 'Key', '创建日期', '最新使用日期', '']


def key_row(name='Fixture target', tracking=TARGET, headers=None, field=None):
    headers = HEADERS if headers is None else headers
    if field is None:
        field = (f'<div role="button" aria-label="点击复制: {tracking}">'
                 f'<span class="ds-button__content">{tracking[:11]} … {tracking[-11:]}</span></div>')
    values = {'名称': html.escape(name), 'Tracking ID': field,
              'Key': '<div role="button" aria-label="点击复制: sk-fixture-only">sk-fixture-only</div>',
              '创建日期': 'fixture date', '最新使用日期': 'fixture date', '': ''}
    return '<tr>' + ''.join('<td>' + values.get(header, 'extra') + '</td>' for header in headers) + '</tr>'


def document(rows, headers=None, attributes='', extra='', script=''):
    headers = HEADERS if headers is None else headers
    return ('<!doctype html><meta charset="utf-8"><h1>API keys</h1>' + extra +
            f'<table id="keys" {attributes}><thead><tr>' +
            ''.join('<th>' + html.escape(header) + '</th>' for header in headers) +
            '</tr></thead><tbody>' + rows + '</tbody></table><script>' + script + '</script>')


class TrackingFieldTests(unittest.TestCase):
    def field(self, text='', labels=(), titles=()):
        return {'text': text, 'copy_labels': list(labels), 'titles': list(titles)}

    def test_copy_label_recovers_only_matching_abbreviation(self):
        display = TARGET[:11] + ' … ' + TARGET[-11:]
        self.assertEqual(parse_tracking_field(self.field(display, ['点击复制: ' + TARGET])), TARGET)
        with self.assertRaises(GuardError):
            parse_tracking_field(self.field(display, ['点击复制: ' + SURVIVOR]))

    def test_full_text_title_and_edge_whitespace(self):
        self.assertEqual(parse_tracking_field(self.field(' \n' + TARGET + '\n ')), TARGET)
        shortened = TARGET[:11] + ' … ' + TARGET[-11:]
        self.assertEqual(parse_tracking_field(self.field(shortened, titles=[' ' + TARGET + ' '])), TARGET)
        self.assertEqual(parse_tracking_field(self.field(TARGET.upper())), TARGET.upper())

    def test_rejects_conflicting_complete_sources(self):
        for field in (self.field(TARGET, titles=[SURVIVOR]),
                      self.field('', ['点击复制: ' + TARGET, '点击复制: ' + SURVIVOR])):
            with self.subTest(field_count=len(field['copy_labels'])):
                with self.assertRaises(GuardError):
                    parse_tracking_field(field)

    def test_invalid_or_secret_values_are_not_echoed(self):
        for value in ('-' * 36, TARGET.replace('-', '', 1), 'sk-fixture-only',
                      'SK-fixture-only', TARGET[:10] + '\u200b' + TARGET[10:],
                      TARGET[:10] + ' ' + TARGET[10:], TARGET[:11] + ' … ' + TARGET[-11:], ''):
            with self.subTest(kind='empty' if not value else 'invalid'):
                with self.assertRaises(GuardError) as caught:
                    parse_tracking_field(self.field(value))
                if value:
                    self.assertNotIn(value, str(caught.exception))


class KeyTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(__file__).resolve().parents[1] / 'browser-runtime'
        if not runtime.is_dir():
            runtime = Path(__file__).resolve().parents[2] / 'browser-runtime'
        if runtime.is_dir():
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(runtime)
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True, executable_path=cls.pw.chromium.executable_path)
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route('**/*', lambda route: route.abort())
        self.html = document(key_row())
        self.context.route('https://platform.deepseek.com/api_keys', lambda route: route.fulfill(
            status=200, content_type='text/html; charset=utf-8', body=self.html))
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.service = object.__new__(BrowserService)
        self.service.page = self.page
        self.service.key_table_timeout = .7
        self.service.key_table_stable_seconds = .05
        self.service.anchor = None
        self.service.known_keys = set()

    def tearDown(self):
        self.context.close()

    def test_real_copy_label_and_reordered_columns_ignore_secret_key(self):
        headers = ['Key', '创建日期', '名称', 'Selection', 'Tracking ID', '', '最新使用日期']
        self.html = document(key_row(headers=headers), headers,
                             extra='<table><thead><tr><th>Other table</th></tr></thead></table>')
        self.assertEqual(self.service._keys(), [{'name': 'Fixture target', 'tracking_id': TARGET}])

    def test_full_dom_text_with_css_ellipsis_remains_valid(self):
        field = f'<span style="display:block;width:30px;overflow:hidden;text-overflow:ellipsis">{TARGET}</span>'
        self.html = document(key_row(field=field))
        self.assertEqual(self.service._keys()[0]['tracking_id'], TARGET)

    def test_row_lookup_after_page_reorders_uses_live_id_and_rejects_identity_change(self):
        self.html = document(key_row('Same', TARGET) + key_row('Same', SURVIVOR))
        self.service._keys()
        self.page.evaluate('''() => {
            const body = document.querySelector('tbody');
            body.append(body.firstElementChild);
        }''')
        row, _, _ = self.service._key_row('Same', TARGET)
        self.assertEqual(row.evaluate('el => el.sectionRowIndex'), 1)
        self.page.evaluate("document.querySelector('tbody tr').firstElementChild.textContent = 'Renamed'")
        with self.assertRaises(GuardError):
            self.service._key_row('Same', TARGET)

    def test_missing_duplicate_or_ambiguous_table_headers_refuse(self):
        for headers in (['名称', 'Key', '创建日期', '最新使用日期', ''],
                        HEADERS + ['Tracking ID']):
            self.html = document(key_row(headers=headers), headers)
            with self.assertRaises(GuardError):
                self.service._keys()
        self.html = document(key_row()) + document(key_row('Second table', SURVIVOR)).replace('<h1>API keys</h1>', '')
        with self.assertRaises(GuardError):
            self.service._keys()

    def test_merged_headers_and_blank_display_refuse(self):
        for markup in (document(key_row()).replace('<th>名称</th>', '<th colspan="2">名称</th>'),
                       document(key_row(field=f'<span title="{TARGET}"> </span>'))):
            self.html = markup
            with self.assertRaises(GuardError):
                self.service._keys()

    def test_duplicate_ids_and_malformed_rows_refuse(self):
        for rows in (key_row() + key_row('Duplicate', TARGET.upper()), '<tr><td>Incomplete</td></tr>'):
            self.html = document(rows)
            with self.assertRaises(GuardError):
                self.service._keys()
            self.assertIsNone(self.service._last_key_snapshot)

    def test_loading_partial_list_cannot_confirm_target_absent(self):
        target_html = key_row()
        import json
        self.html = document(key_row('Fixture survivor', SURVIVOR), attributes='aria-busy="true"', script=(
            'setTimeout(() => { document.querySelector("tbody").insertAdjacentHTML("beforeend", ' +
            json.dumps(target_html) + '); document.querySelector("table").setAttribute("aria-busy", "false"); }, 250);'))
        result = self.service._verify_revoke('Fixture target', TARGET, {TARGET, SURVIVOR})
        self.assertEqual(result, {'absent': False, 'present': True})

    def test_stability_restarts_when_another_row_arrives(self):
        import json
        self.service.key_table_stable_seconds = .25
        self.html = document(key_row(), script=(
            'setTimeout(() => document.querySelector("tbody").insertAdjacentHTML("beforeend", ' +
            json.dumps(key_row('Fixture survivor', SURVIVOR)) + '), 150);'))
        self.assertEqual(len(self.service._keys()), 2)

    def test_loading_or_unexplained_empty_never_means_deleted(self):
        for markup in (document(key_row('Fixture survivor', SURVIVOR), attributes='aria-busy="true"'), document('')):
            self.html = markup
            with self.assertRaises(GuardError):
                self.service._verify_revoke('Fixture target', TARGET, {TARGET, SURVIVOR})

    def test_explicit_empty_is_readable_but_cannot_prove_account(self):
        self.html = document('<tr><td colspan="6">暂无数据</td></tr>')
        self.assertEqual(self.service._keys(), [])
        with self.assertRaises(GuardError):
            self.service._verify_revoke('Fixture target', TARGET, {TARGET, SURVIVOR})

    def test_pagination_virtual_rows_and_excess_count_refuse(self):
        for markup in (document(key_row(), extra='<nav aria-label="Pagination">Next</nav>'),
                       document(key_row().replace('<tr>', '<tr aria-rowindex="2">')),
                       document(key_row() * 201)):
            self.html = markup
            with self.assertRaises(GuardError):
                self.service._keys()

    def test_failed_list_fetch_refuses_complete_looking_rows(self):
        self.context.route('https://platform.deepseek.com/fixture-list', lambda route: route.fulfill(status=500))
        self.html = document(key_row(), script='fetch("/fixture-list");')
        with self.assertRaises(GuardError):
            self.service._keys()

    def test_prepare_uses_same_parser_for_abbreviated_identity(self):
        self.service.anchor = key_fingerprint([{'name': 'Fixture target', 'tracking_id': TARGET}])
        self.assertIn('message', self.service._prepare_revoke('Fixture target', TARGET))


if __name__ == '__main__':
    unittest.main()
