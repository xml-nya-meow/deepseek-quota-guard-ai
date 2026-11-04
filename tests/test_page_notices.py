"""Synthetic optional reminder dialogs; all outside network traffic is blocked."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from quota_guard.core import GuardError
from quota_guard.page_notices import (
    dismiss_optional_notice, install_optional_notice_handler, raise_optional_notice_error,
)


def document(**options):
    return r'''<!doctype html><meta charset="utf-8">
<style>
.ds-modal { position:fixed; inset:0; background:#888a; display:grid; place-items:center; }
.ds-modal-content { background:white; padding:20px; }
[role=button] { display:inline-block; margin:8px; padding:8px; background:#ddd; cursor:pointer; }
.ds-modal-content__close { width:20px; height:20px; }
</style>
<button id="target" onclick="state.events.push('target')">读取表格</button>
<script>
const options = ''' + json.dumps(options) + r''';
window.state = {events:[]};
function showNotice(overrides={}) {
    const opts = {...options, ...overrides};
    const modal = document.createElement('div');
    modal.className = 'ds-modal'; modal.setAttribute('aria-modal','true');
    const dialog = document.createElement('div');
    dialog.className = 'ds-modal-content'; dialog.setAttribute('role','dialog'); modal.append(dialog);
    const header = document.createElement('div'); dialog.append(header);
    const title = document.createElement('div'); title.className = 'ds-modal-content__title';
    title.textContent = opts.title || '绑定邮箱'; header.append(title);
    function button(parent, label, event, className='') {
        const node = document.createElement('div'); node.setAttribute('role','button');
        node.textContent=label; node.className=className;
        node.onclick=() => {
            state.events.push(event);
            if(event==='later' || event==='close') {
                if(opts.changedAfterClick) { title.textContent='登录'; return; }
                if(!opts.stuck) modal.remove();
            }
        };
        parent.append(node); return node;
    }
    if(!opts.noClose) button(opts.misplacedClose ? dialog : header, '', 'close', 'ds-modal-content__close');
    const description=document.createElement('p');
    description.textContent=opts.description || '我们会通过该邮箱为你推送 DeepSeek API 的功能动态、商业信息和产品通知。';
    dialog.append(description);
    for(const [placeholder,value] of [['请输入邮箱',opts.email || ''],['请输入验证码',opts.code || '']]) {
        const input=document.createElement('input'); input.placeholder=placeholder; input.value=value; dialog.append(input);
    }
    if(opts.extraInput) {
        const extra=document.createElement('input'); extra.value=opts.extraInput; dialog.append(extra);
    }
    button(dialog,'发送验证码','send');
    if(!opts.noLater) {
        const later=button(dialog,'稍后再填','later');
        if(opts.disabledLater) later.setAttribute('aria-disabled','true');
        if(opts.hiddenLater) later.style.display='none';
        if(opts.duplicateLater) button(dialog,'稍后再填','duplicate-later');
    }
    button(dialog,'绑定','bind');
    document.body.append(modal);
    if(opts.obstructed) {
        const blocker=document.createElement('div'); blocker.style='position:fixed;inset:0;background:#eee';
        document.body.append(blocker);
    }
}
if(options.delayed) {
    document.querySelector('#target').disabled=true;
    setTimeout(() => {showNotice(); document.querySelector('#target').disabled=false;},120);
} else if(!options.noNotice) showNotice();
if(options.duplicate) showNotice();
if(options.otherDialog) {
    const other=document.createElement('div'); other.setAttribute('role','alertdialog');
    other.innerHTML='<button onclick="state.events.push(\'delete\')">删除</button>'; document.body.append(other);
}
</script>'''


class OptionalNoticeTests(unittest.TestCase):
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

    def load(self, **options):
        self.page.goto('about:blank')
        self.page.set_content(document(**options))

    def events(self):
        return self.page.evaluate('state.events')

    def test_prefers_later_and_never_sends_or_binds(self):
        self.load()
        self.assertTrue(dismiss_optional_notice(self.page))
        self.assertFalse(dismiss_optional_notice(self.page))
        self.assertEqual(self.events(), ['later'])
        self.assertEqual(self.page.locator('.ds-modal').count(), 0)

    def test_close_fallback_only_when_later_is_absent(self):
        self.load(noLater=True)
        self.assertTrue(dismiss_optional_notice(self.page))
        self.assertEqual(self.events(), ['close'])
        for options in ({'hiddenLater': True}, {'disabledLater': True}, {'duplicateLater': True}):
            with self.subTest(options=options):
                self.load(**options)
                with self.assertRaises(GuardError):
                    dismiss_optional_notice(self.page, timeout_ms=100)
                self.assertEqual(self.events(), [])

    def test_missing_or_unverified_close_is_never_replaced_by_bind(self):
        for options in ({'noClose': True}, {'misplacedClose': True}):
            with self.subTest(options=options):
                self.load(noLater=True, **options)
                with self.assertRaisesRegex(GuardError, '已知关闭按钮'):
                    dismiss_optional_notice(self.page)
                self.assertEqual(self.events(), [])

    def test_any_entered_input_is_preserved_without_exposing_value(self):
        for field in ('email', 'code', 'extraInput'):
            with self.subTest(field=field):
                self.load(**{field: 'private-fixture-value'})
                with self.assertRaisesRegex(GuardError, '已有输入') as caught:
                    dismiss_optional_notice(self.page)
                self.assertNotIn('private-fixture-value', str(caught.exception))
                self.assertEqual(self.events(), [])
                self.assertEqual(self.page.locator('.ds-modal').count(), 1)

    def test_unknown_login_deletion_and_other_email_dialogs_are_untouched(self):
        for options in ({'title': '登录'}, {'title': '确认删除'}, {'title': '确认'},
                        {'description': '请绑定邮箱以继续登录。'}, {'title': '绑定邮箱以登录'}):
            with self.subTest(options=options):
                self.load(**options)
                self.assertFalse(dismiss_optional_notice(self.page))
                self.assertEqual(self.events(), [])
                self.assertEqual(self.page.locator('.ds-modal').count(), 1)

    def test_outside_later_button_does_not_replace_missing_local_control(self):
        self.load(noLater=True, noClose=True)
        self.page.evaluate('''() => {
            const button=document.createElement('button'); button.textContent='稍后再填';
            button.onclick=() => state.events.push('outside'); document.body.append(button);
        }''')
        with self.assertRaises(GuardError):
            dismiss_optional_notice(self.page)
        self.assertEqual(self.events(), [])

    def test_ambiguous_or_concurrent_dialogs_refuse_all_clicks(self):
        for options in ({'duplicate': True}, {'otherDialog': True}):
            with self.subTest(options=options):
                self.load(**options)
                with self.assertRaisesRegex(GuardError, '其他弹窗|数量不唯一'):
                    dismiss_optional_notice(self.page)
                self.assertEqual(self.events(), [])

    def test_close_must_hide_modal_even_when_its_title_changes(self):
        for options in ({'stuck': True}, {'changedAfterClick': True}):
            with self.subTest(options=options):
                self.load(**options)
                with self.assertRaisesRegex(GuardError, '未能安全关闭'):
                    dismiss_optional_notice(self.page, timeout_ms=100)
                self.assertEqual(self.events(), ['later'])

    def test_obstructed_close_never_uses_force_or_javascript_click(self):
        self.load(obstructed=True)
        with self.assertRaisesRegex(GuardError, '未能安全关闭'):
            dismiss_optional_notice(self.page, timeout_ms=100)
        self.assertEqual(self.events(), [])

    def test_handler_closes_late_notice_before_original_click(self):
        install_optional_notice_handler(self.page)
        self.load(delayed=True)
        self.page.get_by_role('button', name='读取表格', exact=True).click(timeout=1800)
        self.assertEqual(self.events(), ['later', 'target'])
        raise_optional_notice_error(self.page)

    def test_handler_install_is_idempotent_and_survives_navigation(self):
        self.context.route('https://fixture.invalid/**', lambda route: route.fulfill(
            status=200, content_type='text/html; charset=utf-8', body=document()))
        with patch.object(self.page, 'add_locator_handler', wraps=self.page.add_locator_handler) as add:
            self.assertTrue(install_optional_notice_handler(self.page))
            for number in range(3):
                self.page.goto(f'https://fixture.invalid/page-{number}')
                self.assertFalse(install_optional_notice_handler(self.page))
                self.page.get_by_role('button', name='读取表格', exact=True).click(timeout=1500)
                self.assertEqual(self.events(), ['later', 'target'])
            self.assertEqual(add.call_count, 1)

    def test_explicit_dismiss_with_handler_does_not_reenter_and_restores_handler(self):
        install_optional_notice_handler(self.page)
        self.load()
        self.assertTrue(dismiss_optional_notice(self.page, timeout_ms=500))
        self.page.evaluate('showNotice()')
        self.page.get_by_role('button', name='读取表格', exact=True).click(timeout=1500)
        self.assertEqual(self.events(), ['later', 'later', 'target'])

    def test_handler_preserves_entered_values_blocks_original_action_and_reports_error(self):
        install_optional_notice_handler(self.page)
        self.load(email='private-fixture-value')
        with self.assertRaises(PlaywrightTimeoutError):
            self.page.get_by_role('button', name='读取表格', exact=True).click(timeout=350)
        self.assertEqual(self.events(), [])
        with patch.object(self.page, 'locator', side_effect=AssertionError('must not inspect DOM')):
            with self.assertRaisesRegex(GuardError, '已有输入'):
                raise_optional_notice_error(self.page)
            raise_optional_notice_error(self.page)

    def test_explicit_entry_consumes_pending_handler_error_without_retrying_click(self):
        install_optional_notice_handler(self.page)
        self.load(duplicate=True)
        with self.assertRaises(PlaywrightTimeoutError):
            self.page.get_by_role('button', name='读取表格', exact=True).click(timeout=350)
        with self.assertRaisesRegex(GuardError, '数量不唯一'):
            dismiss_optional_notice(self.page)
        self.assertEqual(self.events(), [])
        raise_optional_notice_error(self.page)

    def test_no_notice_and_unknown_notice_do_not_trigger_the_handler(self):
        install_optional_notice_handler(self.page)
        self.load(noNotice=True)
        self.page.get_by_role('button', name='读取表格', exact=True).click()
        self.assertEqual(self.events(), ['target'])
        self.load(title='确认删除')
        with self.assertRaises(PlaywrightTimeoutError):
            self.page.get_by_role('button', name='读取表格', exact=True).click(timeout=150)
        self.assertEqual(self.events(), [])
        raise_optional_notice_error(self.page)


if __name__ == '__main__':
    unittest.main()
