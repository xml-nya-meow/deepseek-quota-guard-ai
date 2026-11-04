"""Offline browser checks: synthetic DOM only, every network request is blocked."""
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from quota_guard.browser_service import BrowserService, DeletePermit, DELETE_WARNING, TRASH_PATH, key_fingerprint
from quota_guard.core import GuardError


TARGET = "00000000-0000-0000-0000-000000000001"
DECOY = "00000000-0000-0000-0000-000000000002"
SURVIVOR = "00000000-0000-0000-0000-000000000003"
KEYS = [{"name": "Fixture target", "tracking_id": TARGET},
        {"name": "Fixture target", "tracking_id": DECOY},
        {"name": "Fixture survivor", "tracking_id": SURVIVOR}]


class RevocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(__file__).resolve().parents[1] / "browser-runtime"
        if not runtime.is_dir():
            runtime = Path(__file__).resolve().parents[2] / "browser-runtime"
        if runtime.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime)
        cls.pw = sync_playwright().start()
        try:
            # The portable bundle contains full Chromium, not a separate
            # headless-shell download. Run that exact bundled executable.
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
        self.context.route("**/*", lambda route: route.abort())
        self.context.route("https://platform.deepseek.com/api_keys", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body="<h1>API keys</h1><table><thead><tr><th>名称</th><th>Tracking ID</th><th>Key</th><th>创建日期</th><th>最新使用日期</th><th></th></tr></thead><tbody></tbody></table>"))
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.page.goto("https://platform.deepseek.com/api_keys")
        self.page.evaluate("""({keys, trash, warning}) => {
            window.deleted = []; window.finalClicks = 0; window.replaceConfirmation = false;
            window.openDialog = (id) => {
                document.querySelector('[role=dialog]')?.remove();
                const dialog = document.createElement('div'); dialog.setAttribute('role', 'dialog');
                dialog.dataset.target = id;
                const text = document.createElement('p'); text.textContent = warning; dialog.append(text);
                const cancel = document.createElement('button'); cancel.textContent = '取消';
                cancel.onclick = () => dialog.remove(); dialog.append(cancel);
                const confirm = document.createElement('button'); confirm.textContent = '删除';
                confirm.onclick = () => {
                    window.finalClicks++; window.deleted.push(id);
                    document.querySelector(`tr[data-id="${id}"]`).remove(); dialog.remove();
                };
                dialog.append(confirm); document.body.append(dialog);
            };
            keys.forEach(key => {
                const row = document.createElement('tr'); row.dataset.id = key.tracking_id;
                row.dataset.name = key.name;
                [key.name, '', 'fixture-masked-key', 'fixture', 'fixture'].forEach(value => {
                    const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
                });
                const copy = document.createElement('div'); copy.setAttribute('role', 'button');
                copy.setAttribute('aria-label', '点击复制: ' + key.tracking_id);
                const abbreviated = document.createElement('span'); abbreviated.className = 'ds-button__content';
                abbreviated.textContent = key.tracking_id.slice(0, 11) + ' … ' + key.tracking_id.slice(-11);
                copy.append(abbreviated); row.cells[1].append(copy);
                const actions = document.createElement('td');
                const unrelated = document.createElement('button'); unrelated.textContent = 'Other action';
                actions.append(unrelated);
                const button = document.createElement('button');
                const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                svg.setAttribute('width', '16'); svg.setAttribute('height', '16');
                const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                path.setAttribute('d', trash); svg.append(path); button.append(svg);
                button.onclick = () => {
                    openDialog(key.tracking_id);
                    if (window.replaceConfirmation) setTimeout(() => openDialog(keys[1].tracking_id), 50);
                };
                actions.append(button); row.append(actions); document.querySelector('tbody').append(row);
            });
        }""", {"keys": KEYS, "trash": TRASH_PATH, "warning": DELETE_WARNING})
        # Preserve the synthetic document across reloads, but exercise the real
        # header/Tracking ID parser and row matching during every operation.
        self.service = object.__new__(BrowserService)
        self.service.page = self.page
        self.service.anchor = key_fingerprint(KEYS)
        self.service.known_keys = {k["tracking_id"] for k in KEYS}
        self.service.key_table_timeout = 1
        self.service.key_table_stable_seconds = .05
        self.navigation = patch.object(self.page, 'goto', return_value=None)
        self.navigation.start()
        self.threads = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(timeout=2)
        self.navigation.stop()
        self.context.close()

    def permit(self, allowed=True, delay=0):
        permit = DeletePermit()
        def authorize():
            if permit.ready.wait(2):
                if delay:
                    time.sleep(delay)
                permit.decide(allowed)
        thread = threading.Thread(target=authorize, daemon=True)
        self.threads.append(thread)
        thread.start()
        return permit

    def revoke(self, permit=None):
        return self.service._revoke_dom_legacy("Fixture target", TARGET, permit or self.permit())

    def install_current_platform_actions(self):
        # Read the independently captured public control structure, not the
        # production signature constant, so a mistaken adapter literal fails.
        actions = (Path(__file__).parent / "fixtures" / "platform_actions_20261031.html").read_text(encoding="utf-8")
        self.page.evaluate("""({target, actions}) => {
            const row = document.querySelector(`tr[data-id="${target}"]`);
            row.cells[row.cells.length - 1].innerHTML = actions;
            window.fixtureDeleteOpens = 0; window.fixtureOtherClicks = 0;
            for (const control of row.cells[row.cells.length - 1].querySelectorAll('[role="button"]')) {
                control.onclick = control.dataset.fixture === 'delete'
                    ? () => { window.fixtureDeleteOpens++; openDialog(target); }
                    : () => { window.fixtureOtherClicks++; };
            }
        }""", {'target': TARGET, 'actions': actions})

    def current_delete_control(self):
        return self.page.locator(f'tr[data-id="{TARGET}"] [data-fixture="delete"]')

    def assert_current_control_refused(self):
        permit = DeletePermit()
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertFalse(permit.ready.is_set())
        self.assertEqual(self.page.evaluate('window.fixtureDeleteOpens'), 0)
        self.assertEqual(self.page.evaluate('window.fixtureOtherClicks'), 0)
        self.assertEqual(self.page.evaluate('window.finalClicks'), 0)
        self.assertEqual(self.page.evaluate('window.deleted'), [])
        self.assertEqual(self.page.locator('[role="dialog"]').count(), 0)

    def test_exact_tracking_id_selects_target_among_duplicate_names(self):
        self.assertTrue(self.revoke()["absent"])
        self.assertEqual(self.page.evaluate("window.deleted"), [TARGET])
        self.assertEqual({k["tracking_id"] for k in self.service._keys()}, {DECOY, SURVIVOR})

    def test_current_platform_div_control_deletes_only_exact_target(self):
        self.install_current_platform_actions()
        self.assertTrue(self.revoke()["absent"])
        self.assertEqual(self.page.evaluate('window.deleted'), [TARGET])
        self.assertEqual(self.page.evaluate('window.finalClicks'), 1)
        self.assertEqual(self.page.evaluate('window.fixtureDeleteOpens'), 1)
        self.assertEqual(self.page.evaluate('window.fixtureOtherClicks'), 0)
        self.assertEqual({k['tracking_id'] for k in self.service._keys()}, {DECOY, SURVIVOR})

    def test_current_platform_unknown_path_refuses_before_dialog(self):
        self.install_current_platform_actions()
        self.current_delete_control().locator('path').evaluate("path => path.setAttribute('d', 'M1 1L3 3')")
        self.assert_current_control_refused()

    def test_current_platform_wrong_viewbox_refuses_before_dialog(self):
        self.install_current_platform_actions()
        self.current_delete_control().locator('svg').evaluate("svg => svg.setAttribute('viewBox', '0 0 24 24')")
        self.assert_current_control_refused()

    def test_current_platform_additional_svg_or_path_refuses_before_dialog(self):
        for selector in ('svg', 'path'):
            with self.subTest(additional=selector):
                self.install_current_platform_actions()
                self.current_delete_control().locator(selector).evaluate('node => node.after(node.cloneNode(true))')
                self.assert_current_control_refused()

    def test_current_platform_other_svg_shapes_refuse_before_dialog(self):
        for tag in ('use', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line', 'text', 'image'):
            with self.subTest(shape=tag):
                self.install_current_platform_actions()
                self.current_delete_control().locator('svg').evaluate("""(svg, tag) => {
                    svg.append(document.createElementNS('http://www.w3.org/2000/svg', tag));
                }""", tag)
                self.assert_current_control_refused()

    def test_current_platform_nested_controls_refuse_before_dialog(self):
        for tag in ('button', 'div'):
            with self.subTest(nested=tag):
                self.install_current_platform_actions()
                self.current_delete_control().evaluate("""(node, tag) => {
                    const nested = document.createElement(tag);
                    if (tag === 'div') nested.setAttribute('role', 'button');
                    node.append(nested);
                }""", tag)
                self.assert_current_control_refused()

    def test_current_platform_two_delete_candidates_refuse_before_dialog(self):
        self.install_current_platform_actions()
        self.current_delete_control().evaluate('node => node.after(node.cloneNode(true))')
        self.assert_current_control_refused()

    def test_current_platform_disabled_controls_refuse_before_dialog(self):
        mutations = {
            'aria-disabled': "node => node.setAttribute('aria-disabled', 'true')",
            'disabled-ancestor': "node => node.parentElement.setAttribute('aria-disabled', 'true')",
            'inert': "node => node.setAttribute('inert', '')",
            'native-disabled': """node => {
                const button = document.createElement('button');
                button.dataset.fixture = 'delete'; button.innerHTML = node.innerHTML;
                button.onclick = node.onclick; button.disabled = true; node.replaceWith(button);
            }""",
        }
        for condition, mutation in mutations.items():
            with self.subTest(disabled=condition):
                self.install_current_platform_actions()
                self.current_delete_control().evaluate(mutation)
                self.assert_current_control_refused()
                # The actions cell persists when the next fixture is installed.
                self.page.locator(f'tr[data-id="{TARGET}"] td:last-child').evaluate(
                    "cell => cell.removeAttribute('aria-disabled')")

    def test_current_platform_hidden_controls_refuse_before_dialog(self):
        mutations = {
            'display-none': "node => node.style.display = 'none'",
            'visibility-hidden': "node => node.style.visibility = 'hidden'",
            'hidden-attribute': "node => node.hidden = true",
        }
        for condition, mutation in mutations.items():
            with self.subTest(hidden=condition):
                self.install_current_platform_actions()
                self.current_delete_control().evaluate(mutation)
                self.assert_current_control_refused()

    def test_current_platform_noninteractive_node_refuses_before_dialog(self):
        self.install_current_platform_actions()
        self.current_delete_control().evaluate("node => node.removeAttribute('role')")
        self.assert_current_control_refused()

    def test_current_platform_path_changed_after_guard_cannot_open_dialog(self):
        self.install_current_platform_actions()
        evaluate = self.page.evaluate
        def replace_after_guard(expression, arg=None):
            result = evaluate(expression, arg)
            if 'window.__quotaGuardInteraction = state' in expression:
                self.current_delete_control().locator('path').evaluate(
                    "path => path.setAttribute('d', 'M1 1L3 3')")
            return result
        with patch.object(self.page, 'evaluate', side_effect=replace_after_guard):
            self.assert_current_control_refused()
        self.assertTrue(self.page.evaluate('window.__quotaGuardInteraction === undefined'))

    def test_current_platform_path_changed_during_permit_cannot_final_click(self):
        self.install_current_platform_actions()
        permit = DeletePermit()
        permit.decide(True)
        def replace_while_waiting(_timeout):
            self.current_delete_control().locator('path').evaluate(
                "path => path.setAttribute('d', 'M1 1L3 3')")
            return True
        permit.decided.wait = replace_while_waiting
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertTrue(permit.ready.is_set())
        self.assertEqual(self.page.evaluate('window.fixtureDeleteOpens'), 1)
        self.assertEqual(self.page.evaluate('window.fixtureOtherClicks'), 0)
        self.assertEqual(self.page.evaluate('window.finalClicks'), 0)
        self.assertEqual(self.page.evaluate('window.deleted'), [])
        self.assertTrue(self.page.evaluate('window.__quotaGuardInteraction === undefined'))

    def test_missing_trash_icon_refuses_before_permit_or_confirmation(self):
        self.page.locator(f'tr[data-id="{TARGET}"] path').evaluate("path => path.remove()")
        permit = DeletePermit()
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertFalse(permit.ready.is_set())
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)

    def test_denied_permit_cancels_without_deletion(self):
        self.assertTrue(self.revoke(self.permit(False))["cancelled"])
        self.assertEqual(self.page.evaluate("window.deleted"), [])
        self.assertEqual(self.page.get_by_role("button", name="删除", exact=True).count(), 0)

    def test_repeat_target_is_never_clicked_again(self):
        permit = self.permit()
        self.revoke(permit)
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertEqual(self.page.evaluate("window.finalClicks"), 1)

    def test_confirmation_timeout_does_not_repeat_consequential_click(self):
        # Model a successful server-side effect followed by a stuck dialog. The
        # adapter must report uncertainty rather than click the button again.
        self.page.evaluate("""() => {
            const original = window.openDialog;
            window.openDialog = id => {
                original(id);
                const dialog = document.querySelector('[role=dialog]');
                dialog.querySelectorAll('button')[1].onclick = () => {
                    window.finalClicks++; window.deleted.push(id);
                    document.querySelector(`tr[data-id="${id}"]`).remove();
                };
            };
        }""")
        with self.assertRaises(Exception):
            self.revoke()
        self.assertEqual(self.page.evaluate("window.deleted"), [TARGET])
        self.assertEqual(self.page.evaluate("window.finalClicks"), 1)

    def test_unrecognized_confirmation_warning_refuses_click(self):
        self.page.evaluate("""() => {
            const original = window.openDialog;
            window.openDialog = id => {
                original(id);
                document.querySelector('[role=dialog] p').textContent = 'Changed fixture warning';
            };
        }""")
        permit = DeletePermit()
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertFalse(permit.ready.is_set())
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)

    def test_wrong_name_or_changed_key_set_refuses_without_click(self):
        with self.assertRaises(GuardError):
            self.service._revoke_dom_legacy("Wrong fixture", TARGET, DeletePermit())
        self.page.locator(f'tr[data-id="{DECOY}"]').evaluate("row => row.remove()")
        with self.assertRaises(GuardError):
            self.revoke(DeletePermit())
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)

    def test_persisted_known_keys_verify_absence_after_browser_restart(self):
        self.page.locator(f'tr[data-id="{TARGET}"]').evaluate("row => row.remove()")
        self.service.known_keys = set()
        self.service.anchor = None
        result = self.service._verify_revoke("Fixture target", TARGET, {TARGET, SURVIVOR})
        self.assertTrue(result["absent"])

    def test_no_surviving_known_key_cannot_confirm_absence(self):
        self.page.locator(f'tr[data-id="{TARGET}"]').evaluate("row => row.remove()")
        with self.assertRaises(GuardError):
            self.service._verify_revoke("Fixture target", TARGET, {TARGET})

    def test_replaced_confirmation_cannot_delete_another_key(self):
        self.page.evaluate("window.replaceConfirmation = true")
        with self.assertRaises(Exception):
            self.revoke(self.permit(delay=.25))
        self.assertEqual(self.page.evaluate("window.deleted"), [])
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)

    def test_trusted_user_interaction_interrupts_and_unlocks_document(self):
        permit = DeletePermit()
        permit.decide(True)
        # Run a trusted browser input on the same Playwright thread at the exact
        # authorization wait boundary; no cross-thread browser calls are used.
        def interrupted_wait(_timeout):
            self.page.mouse.click(10, 10)
            return True
        permit.decided.wait = interrupted_wait
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)
        self.assertTrue(self.page.evaluate("window.__quotaGuardInteraction === undefined"))

    def replace_target_identity(self):
        self.page.evaluate("""({target, replacement}) => {
            const row = document.querySelector(`tr[data-id="${target}"]`);
            const copy = row.cells[1].querySelector('[role="button"]');
            copy.setAttribute('aria-label', '点击复制: ' + replacement);
            copy.textContent = replacement.slice(0, 11) + ' … ' + replacement.slice(-11);
            row.querySelector('button:last-child').onclick = () => openDialog(replacement);
        }""", {'target': TARGET, 'replacement': DECOY})

    def test_reused_row_before_opening_dialog_cannot_change_target(self):
        evaluate = self.page.evaluate
        def replace_after_guard(expression, arg=None):
            result = evaluate(expression, arg)
            if 'window.__quotaGuardInteraction = state' in expression:
                self.replace_target_identity()
            return result
        with patch.object(self.page, 'evaluate', side_effect=replace_after_guard):
            with self.assertRaises(GuardError):
                self.revoke(DeletePermit())
        self.assertEqual(self.page.evaluate('window.finalClicks'), 0)
        self.assertEqual(self.page.get_by_role('button', name='删除', exact=True).count(), 0)

    def test_reused_row_during_permit_cannot_reach_final_click(self):
        permit = DeletePermit()
        permit.decide(True)
        def replace_while_waiting(_timeout):
            self.replace_target_identity()
            return True
        permit.decided.wait = replace_while_waiting
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertEqual(self.page.evaluate('window.finalClicks'), 0)


if __name__ == "__main__":
    unittest.main()
