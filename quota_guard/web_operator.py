"""Stage-bound local model operations on the existing isolated console page.

The host prepares only generic pages. Every target control and confirmation is
chosen by the model. A request route checks read scope and the final mutation;
it never repairs a model-selected target or creates a replacement request.
"""
from __future__ import annotations

from decimal import Decimal
import json
import re
import time
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import Error as PlaywrightError

from .core import GuardError
from .platform_http import BASE, COST_PATH, CostReadError, cost_url, normalize_cost_scope, MAX_BODY_BYTES
from .platform_keys import EDIT_URL, DeleteTarget
from .operator_contract import MODEL_TOOLS, validate_action, public_ready
from .operator_runtime import OperatorError

STAGE_SECONDS = 180
MAX_ACTIONS = 20
_READ_API_PATHS = frozenset({
    '/api/v0/users/get_api_keys', '/auth-api/v0/users/current',
    '/api/v0/users/get_user_summary', '/api/v0/client/settings',
})
# Observed session validation, not a management mutation. All other POSTs are
# denied while the operator owns the page (including create/edit/logout).
_SESSION_CHECK_PATH = '/auth-api/v0/users/auth_token/check_device'
_STATIC_TYPES = frozenset({'script', 'stylesheet', 'image', 'font', 'media', 'manifest'})
_REASONS = {
    'AI_OPERATOR_BLOCKED': '本地 AI 未能完成当前页面操作，管理已暂停；请查看诊断记录。',
    'AI_OPERATOR_TIMEOUT': '本地 AI 页面操作超过本轮时限，已停止继续操作。',
    'AI_OPERATOR_ACTION_LIMIT': '本地 AI 页面操作达到步骤上限，已停止继续操作。',
    'AI_OPERATOR_SCOPE_REJECTED': '网页请求的目标或范围不符合本次任务，已阻止发送。',
    'AI_OPERATOR_WRITE_REJECTED': '网页写入请求未通过目标与一次性许可校验，已阻止发送。',
    'AI_OPERATOR_STALE_REFERENCE': '页面控件已变化，需要重新观察。',
    'AI_OPERATOR_PAGE_CHANGED': '页面离开本次阶段或登录失效，请重新核验登录。',
    'AI_OPERATOR_ACTION_FAILED': '网页动作未完成，需要重新观察；尚未据此确认任何删除。',
    'AI_OPERATOR_UNSUPPORTED': '本地 AI 请求了不支持的操作，已停止。',
    'AI_OPERATOR_RESPONSE_INVALID': '网页费用响应未通过格式检查，本次费用未应用。',
    'AI_OPERATOR_RESULT_UNKNOWN': '删除请求可能已经发送，结果待核实；禁止自动重试。',
}


class WebOperatorError(GuardError):
    def __init__(self, reason_code, *, may_have_submitted=False):
        self.reason_code = reason_code if reason_code in _REASONS else 'AI_OPERATOR_ACTION_FAILED'
        self.may_have_submitted = bool(may_have_submitted)
        super().__init__(_REASONS[self.reason_code])


def _remaining(deadline, cap=10):
    left = deadline - time.monotonic()
    if left <= 0:
        raise WebOperatorError('AI_OPERATOR_TIMEOUT')
    return min(left, cap)


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = value
        return result
    def constant(_):raise ValueError('non-finite number')
    return json.loads(raw, parse_float=Decimal, object_pairs_hook=pairs, parse_constant=constant)


def _public_value(value):
    value = re.sub(r'(?i)sk-[a-z0-9_*.-]+', '[KEY REDACTED]', str(value))
    return re.sub(r'(?i)Bearer\s+[^\s,;"<>]+', '[AUTH REDACTED]', value)


def _same_origin(url):
    try:
        p = urlsplit(url)
        return p.scheme == 'https' and p.netloc == 'platform.deepseek.com' and not p.fragment
    except (TypeError, ValueError):
        return False


def _query(url):
    return {key: tuple(sorted(values)) for key, values in parse_qs(
        urlsplit(url).query, keep_blank_values=True, strict_parsing=True).items()}


def same_cost_request(actual, expected):
    try:
        return (_same_origin(actual) and urlsplit(actual).path == COST_PATH
                and _query(actual) == _query(expected))
    except (ValueError, TypeError):
        return False


def _forward_once(route, *, timeout_ms):
    """Forward this browser-originated request once, never follow a redirect.

    A redirect can bypass Playwright's subsequent route callbacks. Therefore
    the approved original request uses fetch with redirects/retries disabled,
    and only a non-redirect response is returned to the renderer. There is no
    new request body, alternate target or fallback send on this path.
    """
    response = None
    try:
        response = route.fetch(max_redirects=0,max_retries=0,timeout=timeout_ms)
        if response.url != route.request.url or 300 <= response.status < 400:
            raise WebOperatorError('AI_OPERATOR_SCOPE_REJECTED')
        # APIResponse.body is decoded. Do not retain wire compression/length
        # headers while fulfilling those decoded bytes to the browser.
        headers = {k:v for k,v in response.headers.items()
                   if k.lower() not in {'content-encoding','content-length','transfer-encoding'}}
        route.fulfill(status=response.status,headers=headers,body=response.body())
    finally:
        if response is not None:
            response.dispose()


def _install_write_boundary(context):
    """Keep delayed page retries blocked even after a model stage finishes.

    Installed only after interactive login. A new login disposes this context.
    The active stage forwards approved original requests itself. The remaining
    lower route grants no fee or write requests, including delayed retries.
    """
    if getattr(context, '_operator_write_boundary', None) is not None:
        return
    def boundary(route):
        request = route.request
        method = request.method.upper()
        path = urlsplit(request.url).path
        try:
            if _same_origin(request.url) and method in ('GET', 'HEAD'):
                if (path in _READ_API_PATHS or
                        request.resource_type in _STATIC_TYPES and not path.startswith(('/api/','/auth-api/')) or
                        request.is_navigation_request() and path in ('/api_keys','/usage')):
                    return _forward_once(route,timeout_ms=20000)
            if (_same_origin(request.url) and method == 'POST'
                    and path == _SESSION_CHECK_PATH):
                return _forward_once(route,timeout_ms=20000)
        except Exception:
            pass
        try:route.abort('blockedbyclient')
        except Exception:pass
    context.route('**/*', boundary)
    # WebSockets and service workers must not bypass HTTP request authorization.
    if hasattr(context, 'route_web_socket'):
        context.route_web_socket('**/*', lambda socket: socket.close(code=1008, reason='Operation boundary'))
    context._operator_write_boundary = boundary


class OperatorRequestGate:
    """One stage, one origin, one fixed read scope or one once-only mutation."""
    def __init__(self, page, *, expected_cost=None, prepared=None, permit=None,
                 inventory=(), deadline=None, emit=None):
        self.page = page
        self.expected_cost = expected_cost
        self.prepared = prepared
        self.permit = permit
        self.inventory = tuple(inventory)
        self.deadline = deadline or time.monotonic() + STAGE_SECONDS
        self.emit = emit or (lambda *_a, **_k: None)
        self.sent = False
        self.attempted = False
        self.violation = None
        self.last_rejection = None
        self.response = None
        self.response_error = None
        self.write_completed = False
        self.model_action_started = False
        self._write_request = None
        self._read_requests = set()
        self._callback = self.route
        self._response_callback = self.capture_response
        self._popup_callback = self.close_popup
        self._failed_callback = self.request_failed
        self._download_callback = lambda download: download.cancel()

    def _reject(self, route, reason, *, fatal=True, empty=False):
        self.last_rejection = reason
        if fatal:self.violation = reason
        self.emit('operator_request_rejected', reason_code=reason, phase='request_boundary')
        if empty:
            # Page bootstrap may ask for account-wide costs. Never send that
            # request; an explicit local rejection keeps target controls usable.
            route.fulfill(status=403, content_type='application/json', body='{"code":403,"msg":"scope required"}')
        else:
            route.abort('blockedbyclient')

    def route(self, route):
        request = route.request
        try:
            _remaining(self.deadline)
            if not _same_origin(request.url):
                # Background analytics/fonts are never sent, but their absence
                # is not itself proof that the business page is unusable.
                # Navigation/popups still invalidate the active operation.
                return self._reject(route, 'AI_OPERATOR_SCOPE_REJECTED',
                                    fatal=request.is_navigation_request())
            try:
                if request.frame.page is not self.page:
                    return self._reject(route, 'AI_OPERATOR_SCOPE_REJECTED')
            except Exception:
                return self._reject(route, 'AI_OPERATOR_SCOPE_REJECTED')
            path = urlsplit(request.url).path
            method = request.method.upper()
            if path in (COST_PATH, '/api/v0/usage/by_api_key/amount'):
                if method != 'GET':
                    return self._reject(route, 'AI_OPERATOR_SCOPE_REJECTED')
                expected = self.expected_cost
                if path != COST_PATH:
                    # Token amount is not necessary for fee authority.
                    return self._reject(route, 'AI_OPERATOR_SCOPE_REJECTED', fatal=False, empty=True)
                if expected and self.model_action_started and same_cost_request(request.url, expected):
                    self._read_requests.add(request)
                    return _forward_once(route,timeout_ms=int(_remaining(self.deadline,20)*1000))
                supplied = parse_qs(urlsplit(request.url).query).get('api_key_tracking_id', [])
                allowed = parse_qs(urlsplit(expected or '').query).get('api_key_tracking_id', [])
                wrong_target = bool(supplied and set(supplied) - set(allowed))
                return self._reject(route, 'AI_OPERATOR_SCOPE_REJECTED', fatal=wrong_target, empty=True)
            if method in ('GET', 'HEAD'):
                if path.startswith(('/api/', '/auth-api/')) and path not in _READ_API_PATHS:
                    return self._reject(route, 'AI_OPERATOR_SCOPE_REJECTED')
                if request.is_navigation_request() and path not in ('/api_keys', '/usage'):
                    return self._reject(route, 'AI_OPERATOR_PAGE_CHANGED')
                if (not request.is_navigation_request() and path not in _READ_API_PATHS
                        and request.resource_type not in _STATIC_TYPES):
                    return self._reject(route, 'AI_OPERATOR_SCOPE_REJECTED')
                return _forward_once(route,timeout_ms=int(_remaining(self.deadline,20)*1000))
            if method == 'POST' and path == _SESSION_CHECK_PATH:
                return _forward_once(route,timeout_ms=int(_remaining(self.deadline,20)*1000))
            if method != 'POST' or request.url != EDIT_URL or self.prepared is None or self.permit is None:
                return self._reject(route, 'AI_OPERATOR_WRITE_REJECTED')
            if not self.model_action_started:
                return self._reject(route, 'AI_OPERATOR_WRITE_REJECTED')
            if self.attempted or self.sent or self.page.url != BASE + '/api_keys':
                return self._reject(route, 'AI_OPERATOR_WRITE_REJECTED')
            if not isinstance(self.prepared, DeleteTarget):
                return self._reject(route, 'AI_OPERATOR_WRITE_REJECTED')
            body = request.post_data
            if not isinstance(body, str) or len(body.encode('utf-8')) > 4096:
                return self._reject(route, 'AI_OPERATOR_WRITE_REJECTED')
            actual = _strict_json(body)
            expected_body = {'action': 'delete', 'name': None,
                             'redacted_key': self.prepared.sensitive_id,
                             'created_at': self.prepared.created_at,
                             'tracking_id': self.prepared.tracking_id}
            if actual != expected_body or type(actual.get('created_at')) is not int:
                return self._reject(route, 'AI_OPERATOR_WRITE_REJECTED')
            self.attempted = True
            self.permit.set_prepared_inventory(self.prepared.tracking_id, self.inventory)
            self.permit.ready.set()
            self.emit('operator_submission_waiting', phase='permit_ready')
            if not self.permit.decided.wait(_remaining(self.deadline, 20)) or not self.permit.allowed:
                return self._reject(route, 'AI_OPERATOR_WRITE_REJECTED')
            # No model inference, page navigation or target replacement occurs
            # between the existing short-lived permit and the actual request.
            self.permit.consume_for_click()
            self.sent = True  # conservative if transport subsequently fails
            self._write_request = request
            _forward_once(route,timeout_ms=int(_remaining(self.deadline,20)*1000))
            self.emit('operator_submission_dispatched', phase='may_submit', count=1)
        except Exception:
            if self.sent:
                self.violation = 'AI_OPERATOR_RESULT_UNKNOWN'
            else:
                self.violation = 'AI_OPERATOR_WRITE_REJECTED' if self.prepared else 'AI_OPERATOR_SCOPE_REJECTED'
            try:route.abort('blockedbyclient')
            except Exception:pass

    def capture_response(self, response):
        if self.sent and response.request is self._write_request:
            # A response status alone does not establish successful deletion.
            # Independent inventory verification remains the only result proof.
            try:
                if response.url != EDIT_URL or response.status != 200:
                    raise ValueError('write status')
                if response.headers.get('content-type','').partition(';')[0].strip().lower() != 'application/json':
                    raise ValueError('write media')
                raw = response.body()  # waits for the full body, not just headers
                if len(raw) > MAX_BODY_BYTES:raise ValueError('write size')
                result = _strict_json(raw)
                if not isinstance(result,dict) or type(result.get('code')) is not int or result['code'] != 0:
                    raise ValueError('write envelope')
                data = result.get('data')
                if (not isinstance(data,dict) or type(data.get('biz_code')) is not int
                        or data['biz_code'] != 0 or 'biz_data' not in data
                        or data['biz_data'] is not None and not isinstance(data['biz_data'],dict)):
                    raise ValueError('write business')
                self.write_completed = True
            except Exception:
                self.violation = 'AI_OPERATOR_RESULT_UNKNOWN'
            return
        if (not self.expected_cost or response.request not in self._read_requests
                or not same_cost_request(response.url, self.expected_cost)):return
        try:
            if response.status in (401, 403):
                self.response_error = 'AUTH_REQUIRED';return
            if response.status != 200:
                self.response_error = 'HTTP_ERROR';return
            if response.headers.get('content-type', '').partition(';')[0].strip().lower() != 'application/json':
                raise ValueError('media type')
            raw = response.body()
            if len(raw) > MAX_BODY_BYTES:raise ValueError('body limit')
            self.response = (self.expected_cost, _strict_json(raw))
            self.emit('operator_cost_response_observed', phase='READ', bytes=len(raw), http_status=200)
        except Exception:
            self.response_error = 'RESPONSE_NOT_JSON'

    def request_failed(self, request):
        if request is self._write_request:
            self.violation = 'AI_OPERATOR_RESULT_UNKNOWN'

    def close_popup(self, page):
        if page is not self.page:
            self.violation = 'AI_OPERATOR_PAGE_CHANGED'
            try:page.close()
            except Exception:pass

    def __enter__(self):
        _install_write_boundary(self.page.context)
        self.page.context.route('**/*', self._callback)
        self.page.on('response', self._response_callback)
        self.page.on('requestfailed', self._failed_callback)
        self.page.on('download', self._download_callback)
        self.page.context.on('page', self._popup_callback)
        return self

    def __exit__(self, *_):
        self.page.context.unroute('**/*', self._callback)
        self.page.remove_listener('response', self._response_callback)
        self.page.remove_listener('requestfailed', self._failed_callback)
        self.page.remove_listener('download', self._download_callback)
        self.page.context.remove_listener('page', self._popup_callback)


# This observer never uses the task target. Secret cells are removed by their
# column heading before text extraction; known credential patterns are scrubbed
# again as defense in depth. Full IDs come only from the public Tracking field.
_OBSERVE_JS = r"""() => {
 const visible=e=>e.isConnected&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const scrub=s=>(s||'').replace(/sk-[A-Za-z0-9_*.-]+/gi,'[KEY REDACTED]').replace(/Bearer\s+[^\s,;"<>]+/gi,'[AUTH REDACTED]');
 const secretCell=e=>{const td=e.closest('td,th');if(!td)return false;const t=td.closest('table');
   const h=t?.querySelectorAll('thead th')[td.cellIndex]?.textContent.trim().toLowerCase();return h==='key'||h==='secret'||h==='api key';};
 const text=e=>{if(!e)return '';if(secretCell(e))return '[KEY REDACTED]';const c=e.cloneNode(true);
   for(const n of c.querySelectorAll('script,style,input[type=password],input[type=hidden],[data-sensitive]'))n.remove();
   const originalRows=e.matches('tr')?[e]:Array.from(e.querySelectorAll('tr'));
   const clonedRows=c.matches('tr')?[c]:Array.from(c.querySelectorAll('tr'));
   originalRows.forEach((r,i)=>Array.from(r.cells||[]).forEach((td,j)=>{if(secretCell(td)&&clonedRows[i]?.cells[j])clonedRows[i].cells[j].textContent='[KEY REDACTED]';}));
   const walker=document.createTreeWalker(c,NodeFilter.SHOW_TEXT);const parts=[];let current;
   while(current=walker.nextNode()){const value=current.textContent.trim();if(value)parts.push(value)}
   return scrub(parts.join(' | ')).replace(/\s+/g,' ').trim();};
 const modal=Array.from(document.querySelectorAll('dialog[open],[role=dialog][aria-modal=true]')).find(visible);
 const controls=Array.from(document.querySelectorAll('button,a[href],input:not([type=hidden]):not([type=password]),select,[role=button],[role=option],[role=menuitem],[tabindex="0"]'));
 const nodes=[];window.__quotaOperatorNodes=[];
 for(const el of controls){if(!visible(el)||secretCell(el)||(modal&&!modal.contains(el)))continue;
   const tag=el.tagName.toLowerCase();const type=(el.getAttribute('type')||'').toLowerCase();
   if(tag==='input'&&!['','text','search','date','datetime-local','number','checkbox'].includes(type))continue;
   const holder=el.closest('dialog,[role=dialog]')||el.closest('tr,[data-key-row],article,section,li,.key-row')||el.parentElement;
   let context=text(holder);const row=el.closest('tr'),table=row?.closest('table');
   if(row&&table){const heads=Array.from(table.querySelectorAll('thead th'));const idx=heads.findIndex(h=>h.textContent.trim().toLowerCase()==='tracking id');
     const field=row.cells[idx];if(field){const values=[field.getAttribute('title'),...Array.from(field.querySelectorAll('[title],[aria-label]')).flatMap(x=>[x.getAttribute('title'),x.getAttribute('aria-label')])];
       const ids=values.join(' ').match(/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/gi)||[];context+=' '+Array.from(new Set(ids)).join(' ');}}
   const label=scrub(el.getAttribute('aria-label')||text(el)||el.getAttribute('placeholder')||el.getAttribute('title')||'');
   window.__quotaOperatorNodes.push(el);
   nodes.push({tag,role:el.getAttribute('role')||(tag==='a'?'link':tag==='select'?'combobox':tag==='input'?'textbox':'button'),
     text:label,context,input_type:type,disabled:!!el.disabled||el.getAttribute('aria-disabled')==='true',
     value:tag==='input'||tag==='select'?scrub(el.value):null,
     options:tag==='select'?Array.from(el.options).map(o=>({value:scrub(o.value),text:scrub(o.text)})):null});}
 const main=document.querySelector('main,[role=main]')||document.body;
 return {nodes,page_text:text(modal||main),readiness:{document_state:document.readyState,main_present:!!main,
  busy:!!document.querySelector('[aria-busy=true],[role=progressbar]'),content_ready:!!main&&main.children.length>0}};
}"""


class WebOperator:
    def __init__(self, page, runtime, *, emit=None, stage=None):
        self.page = page
        self.runtime = runtime
        self.emit = emit or (lambda *_a, **_k: None)
        self.stage = stage or (lambda *_a, **_k: None)
        self.epoch = 0
        self.sequence = 0
        self.nodes = {}
        self.dialog = None
        self.dialog_ref = None
        self.last_action = {'status':'idle'}
        self.history = []
        self._dialog_callback = self._on_dialog
        self._navigation_callback = self._on_navigation

    def _on_navigation(self, frame):
        if frame == self.page.main_frame:
            self.epoch += 1
            self._clear_nodes()

    def _clear_nodes(self):
        for element, _, _ in self.nodes.values():
            try:element.dispose()
            except Exception:pass
        self.nodes.clear()

    def _on_dialog(self, dialog):
        self.dialog = dialog
        self.dialog_ref = f'dialog-{self.epoch}-{self.sequence}'

    def _observe(self):
        self.sequence += 1
        ref = f'obs-{self.epoch}-{self.sequence}'
        if self.dialog is not None:
            message = re.sub(r'(?i)sk-[a-z0-9_*.-]+', '[KEY REDACTED]', self.dialog.message)
            message = re.sub(r'(?i)Bearer\s+[^\s,;"<>]+', '[AUTH REDACTED]', message)
            return {'observation_ref':ref,'document_epoch':self.epoch,'url':self.page.url,
                    'page_text':'Native dialog is open.','nodes':[],
                    'dialog':{'ref':self.dialog_ref,'type':self.dialog.type,'message':message},
                    'action_state':self.last_action,'readiness':{'status':'native_dialog'},'observed_at':time.time()}
        self._clear_nodes()
        snapshot = self.page.evaluate(_OBSERVE_JS)
        # Retain exact public elements, never resolve a later selector by label.
        handles = self.page.evaluate_handle('() => window.__quotaOperatorNodes')
        properties = handles.get_properties()
        self.nodes = {}
        for i, node in enumerate(snapshot['nodes']):
            node_ref = f'{ref}-n{i+1}'
            node['ref'] = node_ref
            element = properties[str(i)].as_element()
            self.nodes[node_ref] = (element, self.epoch, dict(node))
        handles.dispose()
        return {**snapshot,'observation_ref':ref,'document_epoch':self.epoch,'url':self.page.url,
                'dialog':None,'action_state':self.last_action,'observed_at':time.time()}

    def _action(self, action, observation, deadline, *, activate=None):
        try:
            return self._perform_action(action,observation,deadline,activate=activate)
        except PlaywrightError:
            raise WebOperatorError('AI_OPERATOR_ACTION_FAILED') from None

    def _perform_action(self, action, observation, deadline, *, activate=None):
        activate = activate or (lambda: None)
        tool, args = action.get('tool'), action.get('arguments')
        if tool not in MODEL_TOOLS or not validate_action(tool,args,observation)[0]:
            raise WebOperatorError('AI_OPERATOR_UNSUPPORTED')
        _remaining(deadline)
        if tool == 'report_blocker':raise WebOperatorError('AI_OPERATOR_BLOCKED')
        if tool == 'poll_action':
            self.page.wait_for_timeout(min(100, int(_remaining(deadline)*1000)))
            return {'status':'completed'}, None
        if tool == 'handle_dialog':
            if self.dialog is None or args['ref'] != self.dialog_ref:
                raise WebOperatorError('AI_OPERATOR_STALE_REFERENCE')
            dialog = self.dialog
            if args['action'] == 'accept':
                activate()
                dialog.accept(args.get('text'))
            else:dialog.dismiss()
            self.dialog = self.dialog_ref = None
            return {'status':'completed'}, {'type':dialog.type}
        value = self.nodes.get(args['ref'])
        if value is None or value[1] != self.epoch or self.dialog is not None:
            raise WebOperatorError('AI_OPERATOR_STALE_REFERENCE')
        element, _, node = value
        if not element or not element.is_visible() or not element.is_enabled():
            raise WebOperatorError('AI_OPERATOR_STALE_REFERENCE')
        # Any public text/row replacement invalidates this reference. This
        # does not select another element or infer target identity for the AI.
        if not element.evaluate('(el)=>el.isConnected'):
            raise WebOperatorError('AI_OPERATOR_STALE_REFERENCE')
        fresh = self.page.evaluate(_OBSERVE_JS)
        index = element.evaluate('(el)=>window.__quotaOperatorNodes.indexOf(el)')
        if (index < 0 or index >= len(fresh['nodes'])
                or fresh['nodes'][index] != {k:v for k,v in node.items() if k != 'ref'}):
            raise WebOperatorError('AI_OPERATOR_STALE_REFERENCE')
        timeout = min(1500,int(_remaining(deadline)*1000))
        if args['action'] == 'click':
            # Queue one click, so native confirm() can be observed and accepted
            # only by a subsequent model action instead of blocking click().
            activate()
            element.evaluate('(el)=>{setTimeout(()=>{if(el.isConnected)el.click()},0)}')
        elif args['action'] == 'scroll':element.scroll_into_view_if_needed(timeout=timeout)
        elif args['action'] in ('type','select'):
            before = element.input_value(timeout=timeout)
            if before != args['value']:
                activate()
                if args['action'] == 'type':element.fill(args['value'],timeout=timeout)
                else:element.select_option(args['value'],timeout=timeout)
            after = element.input_value(timeout=timeout)
            result = {'status':'completed' if before != after else 'no_change',
                      'value_before':_public_value(before),'value_after':_public_value(after),
                      'changed':before != after}
            return result, {k:node.get(k) for k in ('text','tag','role','context','input_type','value')}
        else:raise WebOperatorError('AI_OPERATOR_UNSUPPORTED')
        return {'status':'completed'}, {k:node.get(k) for k in ('text','tag','role','context')}

    def _wait_ready(self, gate, deadline):
        """Wait only for public page readiness, never for a chosen target row."""
        ready_deadline = min(deadline, time.monotonic() + 30)
        while True:
            _remaining(ready_deadline)
            if gate.violation:
                raise WebOperatorError(gate.violation,may_have_submitted=gate.sent)
            observation = self._observe()
            if public_ready(observation) or gate.response is not None or gate.response_error:
                return observation
            self.stage('正在等待页面加载完成', phase='operator_readiness')
            self.page.wait_for_timeout(min(100,int(_remaining(ready_deadline)*1000)))

    def run(self, task, gate, *, path, deadline):
        self.page.on('dialog',self._dialog_callback)
        self.page.on('framenavigated',self._navigation_callback)
        self.history = []
        try:
            with gate:
                self.stage('正在准备费用页面' if task['kind']=='READ' else '正在准备 Key 管理页面')
                self.emit('operator_stage_navigation',phase=task['kind'],action='navigate')
                self.page.goto(BASE+path,wait_until='domcontentloaded',timeout=int(_remaining(deadline,15)*1000))
                if self.page.url != BASE+path:raise WebOperatorError('AI_OPERATOR_PAGE_CHANGED')
                for step in range(MAX_ACTIONS):
                    _remaining(deadline)
                    self.page.wait_for_timeout(50)
                    if gate.violation:raise WebOperatorError(gate.violation,may_have_submitted=gate.sent)
                    if gate.response is not None:return gate.response
                    if gate.response_error:
                        raise CostReadError('本次 AI 操作取得的费用响应不可用，请重新核验登录和网络。',
                                            reason_code=gate.response_error,retryable=gate.response_error!='AUTH_REQUIRED')
                    if gate.sent:
                        # Never infer again or repeat a click after possible
                        # dispatch. Pump only response/failed events until the
                        # bounded stage ends, then independent verification.
                        while not gate.write_completed:
                            _remaining(deadline)
                            self.page.wait_for_timeout(50)
                            if gate.violation:raise WebOperatorError(gate.violation,may_have_submitted=True)
                        return {'submitted':True}
                    observation = self._wait_ready(gate,deadline)
                    if gate.response is not None:return gate.response
                    if gate.response_error:
                        raise CostReadError('本次 AI 操作取得的费用响应不可用，请重新核验登录和网络。',
                                            reason_code=gate.response_error,retryable=gate.response_error!='AUTH_REQUIRED')
                    self.stage('本地 4B 正在操作读取' if task['kind']=='READ' else '本地 4B 正在操作删除确认')
                    action = self.runtime.decide(task,observation,self.history,deadline=deadline)
                    _remaining(deadline)
                    intent = action.get('arguments',{}).get('action',action.get('tool'))
                    intent = intent if intent in {'click','scroll','type','select','accept','dismiss',
                                                  'poll_action','report_blocker'} else 'invalid'
                    self.emit('operator_model_action',phase=task['kind'],action=intent,step=step)
                    try:
                        result, control = self._action(action,observation,deadline,
                            activate=lambda: setattr(gate,'model_action_started',True))
                    except WebOperatorError as exc:
                        if exc.reason_code not in ('AI_OPERATOR_STALE_REFERENCE','AI_OPERATOR_ACTION_FAILED'):raise
                        if gate.sent:
                            raise WebOperatorError('AI_OPERATOR_RESULT_UNKNOWN',may_have_submitted=True) from None
                        stale = exc.reason_code == 'AI_OPERATOR_STALE_REFERENCE'
                        result={'status':'failed','tool_error':{
                            'code':'STALE_REFERENCE' if stale else 'ACTION_FAILED',
                            'phase':'browser_action','side_effect':'not_started' if stale else 'not_inferred'}}
                        control=None
                    self.last_action = result
                    self.history.append({'action':action,'result':result,'phase':task['kind'],
                                         'observed_control':control,'poll_status':result.get('status')})
                raise WebOperatorError('AI_OPERATOR_ACTION_LIMIT',may_have_submitted=gate.sent)
        except WebOperatorError as exc:
            if gate.sent:raise WebOperatorError('AI_OPERATOR_RESULT_UNKNOWN',may_have_submitted=True) from None
            raise
        except OperatorError:
            if gate.sent:raise WebOperatorError('AI_OPERATOR_RESULT_UNKNOWN',may_have_submitted=True) from None
            raise
        except CostReadError:raise
        except Exception:
            raise WebOperatorError('AI_OPERATOR_RESULT_UNKNOWN' if gate.sent else 'AI_OPERATOR_ACTION_FAILED',may_have_submitted=gate.sent) from None
        finally:
            self.page.remove_listener('dialog',self._dialog_callback)
            self.page.remove_listener('framenavigated',self._navigation_callback)
            if self.dialog is not None:
                try:self.dialog.dismiss()
                except Exception:pass
            self.dialog = self.dialog_ref = None
            self._clear_nodes()

    def read_cost(self,start_date,end_date,tracking_ids,*,deadline=None):
        ids=normalize_cost_scope(tracking_ids)
        deadline=min(deadline or float('inf'),time.monotonic()+STAGE_SECONDS)
        url=cost_url(start_date,end_date,tracking_ids=ids)
        task={'kind':'READ','target_ids':list(ids),
              'date_range':{'start_date':start_date,'end_date':end_date,'timezone':'UTC+8'},
              'instruction':'Read costs only for the exact listed Tracking IDs and inclusive UTC+8 date range. '
                 'Use the page controls to choose the requested keys and dates, then apply or refresh. '
                 'Do not read other keys. Do not delete, create or rename anything.'}
        gate=OperatorRequestGate(self.page,expected_cost=url,deadline=deadline,emit=self.emit)
        return self.run(task,gate,path='/usage',deadline=deadline)

    def delete(self,prepared,permit,inventory,*,deadline=None):
        deadline=min(deadline or float('inf'),time.monotonic()+STAGE_SECONDS)
        task={'kind':'DELETE','target_id':prepared.tracking_id,
              'instruction':'The program authorized deletion of this exact ordinary key. Find the target by complete Tracking ID, '
                 'open its deletion operation and finish the shown confirmations. Preserve all other keys. '
                 'Do not rename or create anything. If identity cannot be established, report the blocker.'}
        gate=OperatorRequestGate(self.page,prepared=prepared,permit=permit,inventory=inventory,deadline=deadline,emit=self.emit)
        return self.run(task,gate,path='/api_keys',deadline=deadline)


class AIConsoleCostReader:
    """Existing fee-parser interface, but all cost GETs originate in AI UI actions."""
    def __init__(self,context,*,page,runtime,emit=None,stage=None,deadline=None):
        self.context=context;self.page=page;self.runtime=runtime
        self.emit=emit;self.stage=stage;self.deadline=deadline

    def clear(self):pass  # no management credentials or cached costs are held

    def prepare(self,page,*,tracking_ids=None):
        if page is not self.page or page.context is not self.context:
            raise CostReadError('AI 操作会话不匹配，请重新登录。',reason_code='AUTH_REQUIRED')
        normalize_cost_scope(tracking_ids)

    def read(self,start_date,end_date,*,tracking_ids=None):
        try:
            return WebOperator(self.page,self.runtime,emit=self.emit,stage=self.stage).read_cost(
                start_date,end_date,tracking_ids,deadline=self.deadline)
        except (WebOperatorError, OperatorError) as exc:
            raise CostReadError(str(exc),reason_code=exc.reason_code,retryable=True) from None
