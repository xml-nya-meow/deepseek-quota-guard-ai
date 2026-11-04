"""Synthetic usage date controls; all external network traffic is aborted."""
import json
import os
from pathlib import Path
import time
import unittest

from playwright.sync_api import sync_playwright

from quota_guard.core import GuardError
from quota_guard.usage_page import select_usage_date_range
from quota_guard.page_notices import install_optional_notice_handler


def usage_document(**options):
    return r'''<!doctype html><meta charset="utf-8">
<h1 onclick="state.headingClicks++; document.querySelector('#menu').hidden=true">用量信息</h1>
<button id="clear" onclick="clearFilters()">清除筛选条件</button>
<button id="keys">API Key 合成筛选</button>
<button id="time" onclick="openMenu()">时间维度 近 30 天</button>
<div id="menu" hidden><div id="custom" onmouseenter="customHover()" onclick="state.events.push('custom'); openPanels()">自定义</div></div>
<div id="calendar"></div>
<script>
const options = ''' + json.dumps(options) + r''';
window.state = {filter:'synthetic-key-filter', selected:[], events:[], headingClicks:0, month:9, year:2026, menuOpens:0, notices:0, noticeCloses:0};
const pad = n => String(n).padStart(2, '0');
if(options.unfiltered) {
    state.filter = null;
    document.querySelector('#clear').remove();
    document.querySelector('#keys').textContent = 'API Key 全部';
}
function clearFilters() {
    state.events.push('clear'); state.selected = [];
    if(!options.failedClear) {
        state.filter = null;
        document.querySelector('#keys').textContent = 'API Key 全部';
    }
    document.querySelector('#clear').remove();
}
function openMenu() {
    state.menuOpens++;
    document.querySelector('#menu').hidden=false;
    if(options.blockCustom) {
        const cover = document.createElement('div'); cover.id = 'unrecognized-overlay';
        cover.style = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.1)';
        document.body.append(cover);
    }
}
function customHover() {
    if(options.lateEmail && (state.notices === 0 || options.repeatEmail)) {
        showEmail(); return;
    }
    if(options.hoverCustom) {
        state.events.push('custom-hover'); openPanels();
        document.querySelector('#menu').hidden = true;
    }
}
function showEmail() {
    state.notices++;
    const overlay = document.createElement('div'); overlay.id = 'email';
    overlay.className = 'ds-modal'; overlay.setAttribute('aria-modal', 'true');
    overlay.style = 'position:fixed;inset:0;z-index:9999;background:white';
    overlay.innerHTML = `<div class="ds-modal-content" role="dialog">
      <div><div class="ds-modal-content__title">绑定邮箱</div></div>
      <p>我们会通过该邮箱为你推送 DeepSeek API 的功能动态、商业信息和产品通知。</p>
      <input placeholder="请输入邮箱"><input placeholder="请输入验证码">
      <button>发送验证码</button><button>绑定</button><button onclick="closeEmail()">稍后再填</button>
      </div>`;
    document.body.append(overlay);
}
function closeEmail() {
    state.noticeCloses++;
    document.querySelector('#email').remove();
    document.querySelector('#menu').hidden=true;
    document.querySelector('#calendar').replaceChildren();
}
function openPanels() {
    state.year = options.year || 2026;
    state.month = options.month || 9;
    if(options.delayedPanels) {
        document.querySelector('#menu').hidden = true;
        setTimeout(render, 70);
    } else render();
}
function moveMonth(delta) {
    state.events.push('move:' + delta);
    if(options.stuckNavigation) return;
    const next = new Date(Date.UTC(state.year, state.month - 1 + delta, 1));
    state.year = next.getUTCFullYear(); state.month = next.getUTCMonth() + 1;
    if(options.delayedNavigation) setTimeout(render, 30); else render();
}
function selected(iso) {
    state.events.push('date:' + iso); state.selected.push(iso);
    if(state.selected.length === 2) {
        const dates = state.selected.map(value => value.slice(5).replace('-', '/'));
        document.querySelector('#time').textContent = options.wrongLabel ? '时间维度 10/01 - 10/02' : '时间维度 ' + dates.join(' - ');
        if(options.filterAfterDates) {
            state.filter = 'synthetic-key-filter';
            document.querySelector('#keys').textContent = 'API Key 合成筛选';
        }
        if(!options.keepOpen) document.querySelector('#calendar').replaceChildren();
    }
}
function render() {
    const calendar = document.querySelector('#calendar'); calendar.replaceChildren();
    for(let offset = 0; offset < 2; offset++) {
        const current = new Date(Date.UTC(state.year, state.month - 1 + offset, 1));
        const year = current.getUTCFullYear(), month = current.getUTCMonth() + 1;
        const panel = document.createElement('section'); panel.className = 'ds-date-picker-panel';
        const title = document.createElement('div'); title.className = 'ds-date-picker-panel__title';
        title.textContent = options.invalidTitle ? 'Unknown month' : `${year}年${options.duplicateMonths ? state.month : month}月`;
        panel.append(title);
        for(const direction of ['prev', 'next']) {
            const button = document.createElement('div'); button.className = `ds-date-picker-panel__${direction}-month-button`;
            button.setAttribute('role', 'button'); button.textContent = direction;
            if(options.disabledNavigation) button.classList.add('ds-button--disabled');
            button.onclick = () => moveMonth(direction === 'prev' ? -1 : 1); panel.append(button);
        }
        // Adjacent-month dates must never be selected just because their number matches.
        const adjacent = document.createElement('div'); adjacent.className = 'ds-date-picker-panel__date';
        adjacent.innerHTML = '<span class="ds-date-picker-panel__date-inner">1</span>';
        adjacent.onclick = () => state.events.push('wrong-adjacent-month'); panel.append(adjacent);
        const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
        for(let day = 1; day <= days; day++) {
            const iso = `${year}-${pad(month)}-${pad(day)}`;
            const cell = document.createElement('div'); cell.className = 'ds-date-picker-panel__date ds-date-picker-panel__date--current-month';
            cell.innerHTML = `<span class="ds-date-picker-panel__date-inner">${day}</span>`;
            if(iso > '2026-10-30') cell.classList.add('ds-date-picker-panel__date--disabled');
            if(options.ariaDisabled && day === 29) cell.setAttribute('aria-disabled', 'true');
            cell.onclick = () => selected(iso); panel.append(cell);
            if(options.duplicateDate && day === 29) panel.append(cell.cloneNode(true));
        }
        calendar.append(panel);
    }
}
</script>'''


class UsagePageTests(unittest.TestCase):
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
        self.page = self.context.new_page()
        self.page.set_default_timeout(500)

    def tearDown(self):
        self.context.close()

    def select(self, start='2026-10-29', end='2026-10-30', timeout_ms=2500, **options):
        self.page.goto('about:blank')
        self.page.set_content(usage_document(**options))
        return select_usage_date_range(self.page, start, end, timeout_ms=timeout_ms)

    def state(self):
        return self.page.evaluate('state')

    def test_clears_key_filter_before_selecting_and_closes_menu(self):
        self.assertEqual(self.select(), ('2026-10-29', '2026-10-30'))
        state = self.state()
        self.assertIsNone(state['filter'])
        self.assertEqual(state['events'], ['clear', 'custom', 'date:2026-10-29', 'date:2026-10-30'])
        self.assertEqual(state['headingClicks'], 1)
        self.assertEqual(self.page.locator('#clear').count(), 0)
        self.assertEqual(self.page.get_by_role('button', name='API Key 全部', exact=True).count(), 1)
        self.assertEqual(self.page.locator('.ds-date-picker-panel:visible').count(), 0)
        self.assertFalse(self.page.locator('#menu').is_visible())

    def test_cross_year_range_uses_complete_month_titles(self):
        self.select('2025-12-31', '2026-10-30', timeout_ms=6000)
        state = self.state()
        self.assertEqual(state['selected'], ['2025-12-31', '2026-10-30'])
        self.assertIn('move:-1', state['events'])
        self.assertIn('move:1', state['events'])

    def test_same_day_clicks_twice(self):
        self.select('2026-10-29', '2026-10-29')
        self.assertEqual(self.state()['selected'], ['2026-10-29', '2026-10-29'])

    def test_day_one_does_not_match_eleven_or_adjacent_month(self):
        self.select('2026-09-01', '2026-10-01')
        self.assertEqual(self.state()['selected'], ['2026-09-01', '2026-10-01'])
        self.assertNotIn('wrong-adjacent-month', self.state()['events'])

    def test_navigation_waits_for_updated_month(self):
        self.select('2026-08-29', '2026-10-30', delayedNavigation=True)
        self.assertEqual(self.state()['selected'], ['2026-08-29', '2026-10-30'])

    def test_disabled_dates_and_month_controls_refuse_click(self):
        with self.assertRaisesRegex(GuardError, '不可用'):
            self.select('2026-10-29', '2026-10-31')
        self.assertEqual(self.state()['selected'], ['2026-10-29'])
        with self.assertRaisesRegex(GuardError, '不可用'):
            self.select(ariaDisabled=True)
        self.assertEqual(self.state()['selected'], [])
        with self.assertRaisesRegex(GuardError, '不可用'):
            self.select('2026-08-29', disabledNavigation=True)
        self.assertNotIn('move:-1', self.state()['events'])

    def test_ambiguous_dates_or_months_and_invalid_titles_fail(self):
        for options in ({'duplicateDate': True}, {'duplicateMonths': True}, {'invalidTitle': True}):
            with self.subTest(options=options), self.assertRaises(GuardError):
                self.select(**options)
            self.assertEqual(self.state()['selected'], [])

    def test_range_label_must_acknowledge_selected_endpoints(self):
        with self.assertRaisesRegex(GuardError, '不一致'):
            self.select(wrongLabel=True)
        self.assertEqual(self.state()['headingClicks'], 0)

    def test_incomplete_selection_and_stuck_navigation_time_out(self):
        for options in ({'keepOpen': True}, {'stuckNavigation': True}):
            with self.subTest(options=options), self.assertRaises(GuardError):
                self.select('2026-08-29', timeout_ms=700, **options)
            self.assertEqual(self.state()['headingClicks'], 0)

    def test_bad_input_does_not_change_filters(self):
        for start, end in (('20261029', '2026-10-30'), ('2026-02-30', '2026-10-30'),
                           ('2026-10-30', '2026-10-29'), (None, '2026-10-30')):
            with self.subTest(start=start), self.assertRaises(GuardError):
                self.select(start, end)
            self.assertEqual(self.state()['events'], [])

    def test_default_unfiltered_page_needs_no_clear_button(self):
        self.select(unfiltered=True)
        self.assertEqual(self.state()['events'], ['custom', 'date:2026-10-29', 'date:2026-10-30'])
        self.assertIsNone(self.state()['filter'])

    def test_missing_clear_control_does_not_prove_all_keys_selected(self):
        self.page.set_content(usage_document())
        self.page.locator('#clear').evaluate('(node) => node.remove()')
        with self.assertRaises(GuardError):
            select_usage_date_range(self.page, '2026-10-29', '2026-10-30', timeout_ms=100)
        self.assertEqual(self.state()['events'], [])

    def test_clear_must_reach_all_keys_state(self):
        with self.assertRaises(GuardError):
            self.select(failedClear=True, timeout_ms=300)
        self.assertEqual(self.state()['events'], ['clear'])

    def test_dates_must_not_leave_a_key_filter_active(self):
        with self.assertRaises(GuardError):
            self.select(filterAfterDates=True, timeout_ms=700)
        self.assertEqual(self.state()['selected'], ['2026-10-29', '2026-10-30'])

    def test_late_optional_notice_handler_collapses_menu_and_reopens_once(self):
        self.page.set_content(usage_document(lateEmail=True))
        install_optional_notice_handler(self.page)
        stages = []
        select_usage_date_range(self.page, '2026-10-29', '2026-10-30', timeout_ms=4500,
            on_stage=lambda stage, **metadata: stages.append((stage, metadata)))
        state = self.state()
        self.assertEqual(state['noticeCloses'], 1)
        self.assertEqual(state['menuOpens'], 2)
        self.assertEqual(state['selected'], ['2026-10-29', '2026-10-30'])
        self.assertEqual(state['events'].count('custom'), 1)
        self.assertIn('menu_collapsed', [metadata['state'] for _, metadata in stages])
        self.assertEqual(stages[-1][1]['state'], 'range_confirmed')
        for _, metadata in stages:
            self.assertEqual(set(metadata), {'state', 'count', 'elapsed'})

    def test_hover_opened_calendar_does_not_repeat_hidden_custom_click(self):
        self.select(hoverCustom=True, timeout_ms=3500)
        state = self.state()
        self.assertEqual(state['menuOpens'], 1)
        self.assertEqual(state['events'].count('custom-hover'), 1)
        self.assertEqual(state['events'].count('custom'), 0)
        self.assertEqual(state['selected'], ['2026-10-29', '2026-10-30'])

    def test_truly_obstructed_custom_menu_fails_without_reopening(self):
        with self.assertRaisesRegex(GuardError, '遮挡'):
            self.select(blockCustom=True, timeout_ms=2500)
        state = self.state()
        self.assertEqual(state['menuOpens'], 1)
        self.assertEqual(state['selected'], [])
        self.assertEqual(state['events'], ['clear'])

    def test_repeated_menu_collapse_does_not_retry_a_second_time(self):
        self.page.set_content(usage_document(lateEmail=True, repeatEmail=True))
        install_optional_notice_handler(self.page)
        started = time.monotonic()
        with self.assertRaisesRegex(GuardError, '菜单已收起'):
            select_usage_date_range(self.page, '2026-10-29', '2026-10-30', timeout_ms=4500)
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertEqual(self.state()['menuOpens'], 2)
        self.assertEqual(self.state()['noticeCloses'], 2)
        self.assertEqual(self.state()['selected'], [])

    def test_recovery_does_not_reset_overall_deadline(self):
        self.page.set_content(usage_document(lateEmail=True, repeatEmail=True))
        install_optional_notice_handler(self.page)
        started = time.monotonic()
        with self.assertRaises(GuardError):
            select_usage_date_range(self.page, '2026-10-29', '2026-10-30', timeout_ms=1500)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertLessEqual(self.state()['menuOpens'], 2)

    def test_hover_calendar_supports_same_day_and_same_day_number_across_months(self):
        for start, end in (('2026-10-29', '2026-10-29'), ('2026-09-29', '2026-10-29')):
            with self.subTest(start=start, end=end):
                self.select(start, end, hoverCustom=True, timeout_ms=3500)
                self.assertEqual(self.state()['selected'], [start, end])
                self.assertEqual(self.state()['menuOpens'], 1)

    def test_calendar_render_after_menu_closes_does_not_reopen(self):
        self.select(delayedPanels=True)
        self.assertEqual(self.state()['menuOpens'], 1)
        self.assertEqual(self.state()['events'].count('custom'), 1)
        self.assertEqual(self.state()['selected'], ['2026-10-29', '2026-10-30'])


if __name__ == '__main__':
    unittest.main()
