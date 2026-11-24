"""Opt-in offline product acceptance: real 4B + Chromium + GUI + receipts.

Only a newly created synthetic directory is accepted. The browser uses a dead
proxy plus request routing to a loopback fake platform. No platform credential
or live HTTP fallback is available. Scoring truth stays in the parent harness;
the product sees normal page and read-only API observations.
"""
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

from . import __version__
from .core import DataStore, Person, TZ
from .operator_fixture import OfflinePlatform, OfflineContext


def _check(value, reason):
    if not value:raise AssertionError(reason)


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n',encoding='utf-8')


def _offline_worker(connection, *, config):
    """Spawned under the unchanged production browser Job and IPC protocol."""
    import quota_guard.browser_service as service_module
    from .browser_process import _browser_worker
    from .operator_runtime import OperatorRuntime, OperatorError
    from .web_operator import AIConsoleCostReader
    from playwright.sync_api import sync_playwright,BrowserContext,Route
    def offline_request(context):
        transport=getattr(context,'_offline_request',None)
        if transport is None:raise RuntimeError('OFFLINE_UNREGISTERED_CONTEXT')
        return transport
    BrowserContext.request=property(offline_request)
    def offline_fetch(route,**kwargs):
        if kwargs.get('max_redirects')!=0 or kwargs.get('max_retries')!=0:
            raise AssertionError('OFFLINE_UNBOUNDED_TRANSPORT')
        if set(kwargs)-{'max_redirects','max_retries','timeout'}:
            raise AssertionError('OFFLINE_REQUEST_OVERRIDE')
        original=route.request
        transport=offline_request(original.frame.page.context)
        return transport.fetch(original.url,method=original.method,body=original.post_data_buffer,source='browser')
    Route.fetch=offline_fetch
    # Reserve an owned local port: Chromium can never reach an unrelated proxy.
    proxy_socket=socket.socket();proxy_socket.bind(('127.0.0.1',0));proxy_socket.listen(8)

    original_connect = socket.socket.connect
    def loopback_only(sock,address):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1','localhost'):
            raise OSError('OFFLINE_NON_LOOPBACK_DENIED')
        return original_connect(sock,address)
    socket.socket.connect=loopback_only
    original_connect_ex=socket.socket.connect_ex
    def loopback_connect_ex(sock,address):
        if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1','localhost'):
            raise OSError('OFFLINE_NON_LOOPBACK_DENIED')
        return original_connect_ex(sock,address)
    socket.socket.connect_ex=loopback_connect_ex

    def trace(event):
        if not config.get('trace'):
            return
        with Path(config['trace']).open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event,ensure_ascii=False,default=str)+'\n')

    from .revocation_permit import DeletePermit
    original_consume=DeletePermit.consume_for_click
    def traced_consume(permit):
        value=original_consume(permit)
        trace({'kind':'permit_consumed','at':time.monotonic()})
        return value
    if config.get('trace'):
        DeletePermit.consume_for_click=traced_consume

    class DisabledRuntime:
        def start(self,**unused):pass
        def close(self):pass
        def decide(self,*args,**kwargs):
            trace({'kind':'no_model_block','at':time.monotonic()})
            raise OperatorError('AI_RUNTIME_CLOSED')

    class TracedRuntime(OperatorRuntime):
        def decide(self, task, observation, history, *, deadline):
            trace({'kind':'inference_start','phase':task['kind'],'at':time.monotonic()})
            if config.get('no_model'):raise OperatorError('AI_RUNTIME_CLOSED')
            started=time.monotonic()
            result=super().decide(task,observation,history,deadline=deadline)
            # Synthetic public controls only, never enabled in normal startup.
            trace({'kind':'model_action','task':task,'action':result,
                   'observation':observation,'elapsed':time.monotonic()-started})
            return result

    class OfflineBrowserService(service_module.BrowserService):
        def _ensure_browser(self):
            if self.context and self.browser and self.page and self.browser.is_connected() and not self.page.is_closed():return
            self._dispose_browser()
            self.pw=sync_playwright().start()
            self.browser=self.pw.chromium.launch(executable_path=config['chromium'],headless=True,
                proxy={'server':'http://127.0.0.1:'+str(proxy_socket.getsockname()[1])},
                args=['--disable-background-networking','--disable-component-update','--no-first-run'],timeout=30000)
            actual=self.browser.new_context(locale='zh-CN',timezone_id='Asia/Shanghai',service_workers='block')
            proxy=OfflineContext(actual,config['origin'])
            actual._offline_request=proxy.request
            self.context=actual
            actual.route('**/*',proxy.route_bottom)
            actual.route_web_socket('**/*',lambda ws: ws.close())
            self.page=actual.new_page();self.page.set_default_timeout(12000)
        def _get_operator_runtime(self,*,deadline):
            if getattr(self,'_operator_runtime',None) is None:
                runtime_type=TracedRuntime if config.get('trace') else OperatorRuntime
                self._operator_runtime=(DisabledRuntime() if config.get('no_model') else
                    runtime_type(assets=config['assets'],state_dir=config['model_state'],emit=self._log))
            self._operator_runtime.start(deadline=deadline)
            return self._operator_runtime
        def _create_cost_reader(self,*,deadline):
            # Real Playwright page/context identity; only management GETs use
            # the service's loopback request proxy.
            return AIConsoleCostReader(self.page.context,page=self.page,
                runtime=self._get_operator_runtime(deadline=deadline),emit=self._log,stage=self._stage,deadline=deadline)
    service_module.BrowserService=OfflineBrowserService
    try:_browser_worker(connection)
    finally:proxy_socket.close()


class _Driver:
    def __init__(self,store,registry,config):
        from .ui import App
        from .browser_process import IsolatedBrowserService
        self.root=tk.Tk();self.root.withdraw();self.errors=[]
        self.root.report_callback_exception=lambda *args:self.errors.append(str(args[1]))
        self.transport=None
        try:
            self.app=App(self.root,store.folder,responsibility_directory=registry)
            self.until(lambda:self.app.config is not None,20)
            self.transport=IsolatedBrowserService(store.folder/'diagnostics',_worker_target=partial(_offline_worker,config=config))
            self.app.browser=self.transport
        except BaseException:
            if self.transport is not None:self.transport.close()
            if hasattr(self,'app'):self.app.close()
            try:self.root.destroy()
            except tk.TclError:pass
            raise

    def until(self,predicate,timeout=240):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.root.update()
            _check(not self.errors,'TK_CALLBACK_ERROR: '+repr(self.errors))
            if predicate():return
            time.sleep(.01)
        raise AssertionError('OFFLINE_WAIT_TIMEOUT: '+self.app.status_var.get())

    def idle(self,timeout=600):
        self.until(lambda:not self.app.busy and not self.app.pending_login and not self.app.interrupting,timeout)

    def login_and_read(self):
        self.app.login();self.idle(60)
        self.app.collect();self.idle()

    def close(self):
        try:
            self.app.close()
            deadline=time.monotonic()+30
            while not self.app.window_destroyed and time.monotonic()<deadline:
                self.root.update();time.sleep(.01)
            _check(self.app.window_destroyed,'WINDOW_CLEANUP_FAILED')
            _check(not self.transport.cleanup_failed,'BROWSER_JOB_CLEANUP_FAILED')
        finally:
            self.transport.close()
            try:self.root.destroy()
            except tk.TclError:pass


def _verify_initial_review(app, store, platform, amount):
    """Check the visible selected row, durable ledger and independent backend."""
    review = app.fee_review
    _check(app.review_tabs.tab(app.review_tabs.select(), 'text') == '费用核对', 'REVIEW_TAB_NOT_SELECTED')
    _check(review.data.identity_current and review.data.fees_current, 'REVIEW_EVIDENCE_NOT_CURRENT')
    item = next(i for i in review.tree.get_children() if review.tree.item(i, 'values')[0] == 'Cedar')
    review.tree.selection_set(item)
    review.tree.event_generate('<<TreeviewSelect>>')
    app.root.update()
    values = review.tree.item(item, 'values')
    row = next(r for r in store.read_evaluation() if r['name'] == 'Cedar')
    _check(review.target_id.get() == row['tracking_id'] == platform.target_id, 'REVIEW_ID_MISMATCH')
    _check(row['spent'] == Decimal(values[1]) == Decimal(amount), 'DISPLAYED_OR_SAVED_COST_MISMATCH')
    _check(row['complete'] and row['amount'] == Decimal('1.00') and values[-1] == '额度内',
           'FIRST_READ_NOT_BELOW_LIMIT')
    _check(review.data.counts() == (1, 1, 0, 0), 'REVIEW_ROSTER_MISMATCH')
    _check(not app.observing and not app.automatic and store.current_snapshot_id(), 'REVIEW_ALREADY_ACTIVE')
    truth = platform.snapshot()
    reads = [e for e in truth['events'] if e['kind'] == 'cost_read']
    _check(len(reads) == 1 and reads[0]['amount'] == amount and
           reads[0]['ids'] == [platform.target_id], 'FIRST_READ_SCOPE_OR_AMOUNT')
    _check(truth['keys'] == truth['initial_keys'] and
           not any(e['kind'] == 'post_arrival' for e in truth['events']), 'WRITE_BEFORE_START')
    return {'tab':'费用核对', 'values':values, 'tracking_id':review.target_id.get(),
            'counts':review.data.counts(), 'account':review.data.account_text,
            'period':review.data.basis_text, 'snapshot_id':store.current_snapshot_id(),
            'saved_amount':str(row['spent']), 'management_active':False}


def _unknown_checkpoint(app, platform):
    record = app.store.latest_action_record(platform.target_id)
    _check(record and record['status'] == 'unknown_result' and record.get('operation_id'),
           'LOST_RESPONSE_NOT_UNKNOWN')
    _check(not app.automatic and not app.observing and app.pending_start is None,
           'UNKNOWN_DID_NOT_STOP_AUTOMATIC_MANAGEMENT')
    obligations = app.responsibility_gate.registry.open_obligations()
    _check(len(obligations) == 1 and obligations[0]['phase'] == 'possible_send' and
           obligations[0]['receipt']['operation_id'] == record['operation_id'],
           'UNKNOWN_RESPONSIBILITY_LOST')
    truth = platform.snapshot()
    posts = [event for event in truth['events'] if event['kind'] == 'post_arrival']
    _check(len(posts) == 1 and posts[0]['source'] == 'browser', 'UNKNOWN_POST_COUNT')
    expected_keys = [key for key in truth['initial_keys'] if key['tracking_id'] != platform.target_id]
    _check(truth['keys'] == expected_keys, 'UNKNOWN_TARGET_OR_CONTROL_MISMATCH')
    return record, len(posts)


def _verify_from_report(driver, platform, record):
    app = driver.app
    app.review_tabs.select(app.reports_tab)
    report = app.activity_report
    report.tree.selection_set(str(record['id']))
    report.tree.event_generate('<<TreeviewSelect>>')
    app.root.update()
    operation = record['operation_id']
    _check(operation in report.reference.get() and platform.target_id in report.reference.get(),
           'UNKNOWN_NOT_VISIBLE_IN_REPORT')
    report.locate.invoke()
    row = app.selected()
    _check(row and row.get('tracking_id') == platform.target_id and row.get('operation_id') == operation,
           'RECOVERY_ORIGINAL_ROW_NOT_SELECTED')
    # Exclude the login page's list query. Verification must make a fresh GET.
    mark = len(platform.snapshot()['events'])
    app.buttons['verify'].invoke()
    driver.idle(60)
    truth = platform.snapshot()
    verification = truth['events'][mark:]
    _check(not any(event['kind'] in ('post_arrival', 'cost_read') for event in verification),
           'RECOVERY_NOT_READ_ONLY')
    receipts = [event for event in verification if event['kind'] == 'identity_read' and
                event['source'] == 'browser' and platform.target_id not in event['ids'] and
                platform.control_id in event['ids']]
    _check(receipts, 'RECOVERY_FRESH_LIST_OR_CONTROL_MISSING')
    verified = app.store.latest_action_record(platform.target_id)
    _check(verified['operation_id'] == operation and verified['status'] == 'absent_verified',
           'RECOVERY_OPERATION_OR_RESULT_CHANGED')
    return {'verified_operation_id': verified['operation_id'],
            'post_count_after_verification': sum(e['kind'] == 'post_arrival' for e in truth['events']),
            'fresh_list_receipts': len(receipts), 'control_id': platform.control_id,
            'target_id': platform.target_id}


def _verify_portable_outcome(store, platform, truth, costs, trace, action, first, recovery=None):
    stamp = int(datetime.fromisoformat(platform.today).replace(tzinfo=TZ).timestamp())
    _check(all(e['source']=='browser' and e['start']==[str(stamp)] and e['end']==[str(stamp+86400)]
               for e in costs), 'WRONG_DATE_OR_COST_SOURCE')
    actions = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
    phases = [e['task']['kind'] for e in actions if e['kind']=='model_action']
    _check('READ' in phases, 'NO_ACTUAL_READ_MODEL_ACTION')
    if action == 'review':
        _check(len(costs)==1 and 'DELETE' not in phases, 'READ_ONLY_ACTION_COUNT')
        return {'model_actions':phases}
    _check('DELETE' in phases and len(costs)>=2 and costs[0]['amount']==first
           and all(e['amount']==platform.cost for e in costs[1:]), 'CROSSING_NOT_OBSERVED')
    events = truth['events']
    writes = [e for e in events if e['kind']=='delete_applied']
    _check(len(writes)==1 and writes[0]['target_id']==platform.target_id
           and costs[1]['monotonic'] < writes[0]['monotonic'], 'WRONG_DELETION_OR_ORDER')
    if recovery is None:
        receipts = sum(e['kind']=='identity_read' and e['source']=='api-context'
                       and e['monotonic']>writes[0]['monotonic'] and platform.target_id not in e['ids']
                       and platform.control_id in e['ids'] for e in events)
        _check(receipts, 'MISSING_INDEPENDENT_LIST_CONFIRMATION')
        receipt_source = 'api-context'
    else:
        receipts = recovery['fresh_list_receipts']
        receipt_source = 'reloaded browser list during read-only verification'
    row = next(r for r in store.read_evaluation() if r['name']=='Cedar')
    _check(row['spent']==Decimal(platform.cost), 'CROSSING_NOT_PERSISTED')
    return {'model_actions':phases,'crossed_amount':str(row['spent']),
            'independent_absence_receipts':receipts, 'absence_receipt_source':receipt_source}


def _one_case(base,case,assets,chromium):
    from .responsibility_gate import ResponsibilityGate,action_history
    from .responsibility_registry import ResponsibilityRegistry
    folder=base/case['name'];folder.mkdir()
    action=case.get('action','automatic')
    portable=action in ('review','crossing')
    names=('Cedar','Maple') if portable else ('Synthetic target','Independent control')
    first=case.get('initial_cost',case.get('cost','1.25'))
    platform=OfflinePlatform(seed=case['seed'],variant=case.get('variant','direct'),cost=first,
        age_days=case.get('age_days',0),fault=case.get('fault','none'),similar_ids=case.get('similar_ids',False),
        names=names).start()
    registry=folder/'registry'
    people=[] if portable else [Person('',Decimal(-1))]*12
    people.extend([Person(names[0],Decimal(case.get('quota','1'))),Person(names[1],Decimal(-1))])
    store=DataStore(folder/'ledger');store.initialize(len(people),'10' if portable else '1500',
        datetime.now(TZ).date().isoformat(),fee_basis='key_lifetime')
    store.save_people(people)
    ResponsibilityGate(registry).initialize([store])
    trace=folder/'model-trace.jsonl'
    config={'origin':platform.origin,'assets':str(assets),'chromium':str(chromium),
            'trace':str(trace),'model_state':str(folder/'model-state'),'no_model':case.get('no_model',False)}
    result={'name':case['name'],'passed':False,'planned':True,'executed':True,'started_at':datetime.now(TZ).isoformat()}
    driver=None;started=time.monotonic();dialogs=[]
    try:
        with ExitStack() as stack:
            for name in ('showerror','showinfo','showwarning'):
                stack.enter_context(patch('quota_guard.ui.messagebox.'+name,side_effect=lambda *a,**k:dialogs.append(list(a))))
            stack.enter_context(patch('quota_guard.ui.messagebox.askyesno',return_value=True))
            driver=_Driver(store,registry,config);app=driver.app
            if case.get('interrupt'):
                app.login();driver.idle(60);app.collect()
                driver.until(lambda:trace.exists() and 'inference_start' in trace.read_text(encoding='utf-8'),120)
                observed=time.monotonic()
                driver.until(lambda:time.monotonic()-observed>=.5,2)
                interrupt_started=time.monotonic();app.interrupt_operation();driver.idle(30)
                result['interrupt_seconds']=round(time.monotonic()-interrupt_started,3)
                _check(not driver.transport.cleanup_failed,'INTERRUPT_CLEANUP_FAILED')
                _check(not any(e['kind']=='post_arrival' for e in platform.snapshot()['events']),'INTERRUPT_SENT_WRITE')
                _check(not app.collection_has_fees,'INTERRUPTED_READ_APPLIED')
            driver.login_and_read()
            result['read_state']=app.action_states.get('collect');result['read_status']=app.status_var.get()
            if case.get('no_model'):
                _check(not app.collection_has_fees,'NO_MODEL_FEE_APPLIED')
            else:
                _check(app.collection_has_fees and app.session_ready,'READ_NOT_APPLIED: '+app.status_var.get())
                if portable:
                    result['initial_review']=_verify_initial_review(app,store,platform,first)
                if action in ('automatic','crossing'):
                    if action=='crossing':
                        with platform.lock:
                            platform.cost=case['cost']
                        app.fee_review.start_button.invoke()
                    else:
                        app.start()
                    _check(app.observing,'AUTO_START_FAILED: '+app.status_var.get())
                    if Decimal(case.get('cost','1.25'))<Decimal(case.get('quota','1')):
                        # Actual second automatic read, not just successful start.
                        driver.until(lambda:sum(e['kind']=='cost_read' for e in platform.snapshot()['events'])>=2,240)
                        driver.idle();app.stop(quiet=True)
                    else:
                        driver.until(lambda:bool(store.latest_action_record(platform.target_id)) and
                            store.latest_action_record(platform.target_id).get('status') in ('absent_verified','unknown_result','prepare_failed','not_submitted','failed'),300)
                        driver.idle()
                        if case.get('fault')=='response_drop':
                            before, post_count = _unknown_checkpoint(app, platform)
                            result['unknown_operation']=before['operation_id']
                            recovery = result['recovery'] = {'kind':'same_window',
                                'before_operation_id':before['operation_id'], 'post_count_before_close':post_count}
                            if case.get('reopen'):
                                period_id = app.config.period_id
                                driver.close()
                                driver = None
                                recovery.update(kind='desktop_window_reopen', first_window_closed=True)
                                driver = _Driver(store,registry,config)
                                app = driver.app
                                reopened, count = _unknown_checkpoint(app, platform)
                                _check(app.config.period_id == period_id and reopened == before,
                                       'RECOVERY_ORIGINAL_LEDGER_OR_RECORD_CHANGED')
                                recovery.update(reopened_operation_id=reopened['operation_id'],
                                                ledger_period_id=period_id, post_count_after_reopen=count)
                                app.login()
                                driver.idle(60)
                                _check(app.action_states.get('login') == '待登录', 'RECOVERY_LOGIN_FAILED')
                            recovery.update(_verify_from_report(driver, platform, before))
                        else:
                            app.stop(quiet=True)
                elif action!='review':
                    raise AssertionError('UNSUPPORTED_ACCEPTANCE_ACTION')
            result['status']=app.status_var.get()
            result['history']=action_history(store)
            result['open_obligations']=ResponsibilityRegistry(registry).open_obligations()
            truth=platform.snapshot();posts=[e for e in truth['events'] if e['kind']=='post_arrival']
            costs=[e for e in truth['events'] if e['kind']=='cost_read']
            expected=action!='review' and not case.get('no_model') and Decimal(case.get('cost','1.25'))>=Decimal(case.get('quota','1'))
            _check(len(posts)==int(expected),'POST_COUNT_MISMATCH')
            _check(all(e['ids']==[platform.target_id] for e in costs),'READ_SCOPE_MISMATCH')
            unchanged=[x for x in truth['initial_keys'] if x['tracking_id']!=platform.target_id]
            _check(all(x in truth['keys'] for x in unchanged),'CONTROL_CHANGED')
            if expected:
                record=store.latest_action_record(platform.target_id)
                _check(record['status']=='absent_verified','NOT_INDEPENDENTLY_SETTLED: '+str(record.get('status')))
                _check(not result['open_obligations'],'RESPONSIBILITY_UNSETTLED')
                actions=result['history']
                _check(len({a.get('operation_id') for a in actions})==1,'OPERATION_CHANGED')
                states=[a['status'] for a in actions]
                _check(states.count('requested')==1 and states[-1]=='absent_verified','EVENT_ORDER_INVALID')
                consumed=[json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()
                          if json.loads(line).get('kind')=='permit_consumed']
                _check(len(consumed)==1,'PERMIT_COUNT_MISMATCH')
                result['permit_consumptions']=len(consumed)
            else:
                _check(truth['keys']==truth['initial_keys'],'UNAUTHORIZED_CHANGE')
            if case.get('no_model'):_check(not costs,'NO_MODEL_COST_QUERY')
            if portable:
                result['verified_outcome']=_verify_portable_outcome(
                    store,platform,truth,costs,trace,action,first,result.get('recovery'))
            result.update(passed=True,actual_post_count=len(posts),actual_cost_queries=len(costs))
    except Exception as exc:
        result['error_type']=type(exc).__name__;result['error']=str(exc)
    finally:
        if driver:
            try:driver.close();result['cleanup_passed']=True
            except Exception as exc:result['cleanup_passed']=False;result['cleanup_error']=str(exc);result['passed']=False
        result['elapsed_seconds']=round(time.monotonic()-started,3);result['dialogs']=dialogs
        truth=platform.snapshot();platform.close()
        result['actual_post_count']=sum(e['kind']=='post_arrival' for e in truth['events'])
        result['actual_cost_queries']=sum(e['kind']=='cost_read' for e in truth['events'])
        result['history']=action_history(store)
        result['open_obligations']=ResponsibilityRegistry(registry).open_obligations()
        _write(folder/'backend-truth.json',truth)
        _write(folder/'result.json',result)
    return result


def run_portable_operator_acceptance(destination,*,assets=None,chromium=None):
    cases=[{'name':'read-only-fee-review','seed':7719,'action':'review','initial_cost':'0.80','quota':'1.00'},
           {'name':'positive-quota-crossing','seed':7719,'action':'crossing','initial_cost':'0.80',
            'cost':'1.05','quota':'1.00'},
           {'name':'response-drop-restart','seed':7719,'action':'crossing','initial_cost':'0.80',
            'cost':'1.05','quota':'1.00','fault':'response_drop','reopen':True}]
    return run_operator_acceptance(destination,assets=assets,chromium=chromium,cases=cases,continue_after_failure=True)


def run_operator_acceptance(destination,*,assets=None,chromium=None,cases=None,continue_after_failure=False):
    from .operator_runtime import assets_directory,verify_assets
    path=Path(destination).absolute()
    if not path.name.startswith('operator-acceptance-'):raise ValueError('NEW_OPERATOR_ACCEPTANCE_DIRECTORY_REQUIRED')
    for ancestor in (path,*path.parents):
        if ancestor.is_symlink() or getattr(ancestor,'is_junction',lambda:False)():raise ValueError('LINK_REJECTED')
    path.parent.resolve(strict=True);path.mkdir(exist_ok=False)
    assets=Path(assets) if assets is not None else assets_directory()
    if chromium is None:
        bundle=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))/'browser-runtime'
        matches=list(bundle.glob('chromium-*/chrome-win64/chrome.exe'))
        if len(matches)!=1:raise ValueError('BUNDLED_CHROMIUM_REQUIRED')
        chromium=matches[0]
    report={'version':__version__,'real_platform':'not_performed','real_requests':0,'local_model':'Qwen3-4B-Instruct-2507 Q4_K_M',
            'passed':False,'cases':[],'scope':'synthetic production GUI/browser/ledger integration'}
    report['asset_verification']=verify_assets(assets)
    code=Path(__file__).resolve().parent
    if not getattr(sys,'frozen',False):
        frozen=path/'frozen-source';frozen.mkdir()
        report['source_hashes']={}
        for source in sorted(code.glob('*.py')):
            raw=source.read_bytes();(frozen/source.name).write_bytes(raw)
            report['source_hashes'][source.name]=hashlib.sha256(raw).hexdigest()
    else:
        with Path(sys.executable).open('rb') as stream:report['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    plan=cases or [{'name':'no-model-ablation','seed':7700,'no_model':True},
        {'name':'positive-direct','seed':7701},
        {'name':'positive-menu','seed':7702,'variant':'menu'},
        {'name':'positive-confirm2','seed':7703,'variant':'confirm2'},
        {'name':'positive-native-confirm','seed':7704,'variant':'native_confirm'},
        {'name':'below-threshold','seed':7705,'cost':'0.25'},
        {'name':'historical-range','seed':7706,'age_days':2},
        {'name':'response-drop-recovery','seed':7707,'fault':'response_drop'},
        {'name':'interrupt-and-relogin','seed':7708,'interrupt':True}]
    report['planned']=len(plan);report['plan']=plan;_write(path/'plan.json',report)
    temp=path/'temp';temp.mkdir()
    old_temp=tempfile.tempdir
    try:
        tempfile.tempdir=str(temp)
        with patch.dict(os.environ,{'TEMP':str(temp),'TMP':str(temp),'TMPDIR':str(temp)}):
            for case in plan:
                result=_one_case(path,case,assets,chromium);report['cases'].append(result)
                report['executed']=len(report['cases']);_write(path/'summary.json',report)
                if not result['passed'] and not continue_after_failure:
                    report['stop_reason']='case_failed';break
        report['passed']=len(report['cases'])==len(plan) and all(c['passed'] for c in report['cases'])
        if 'source_hashes' in report:
            report['source_unchanged']=all(hashlib.sha256((code/name).read_bytes()).hexdigest()==digest
                                            for name,digest in report['source_hashes'].items())
            if not report['source_unchanged']:report['passed']=False;report['stop_reason']='source_changed_during_run'
    finally:
        tempfile.tempdir=old_temp
        report['unexecuted']=len(plan)-len(report['cases']);_write(path/'summary.json',report)
    return report
