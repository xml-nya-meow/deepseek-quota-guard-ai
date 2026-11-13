"""Offline actual Chromium/HTTP tests; scripted model only tests boundaries.

The separate product acceptance command runs the actual packaged 4B model.
Nothing here can reach a live platform: every request ends at loopback routes.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright, Route

from quota_guard.core import TZ
from quota_guard.operator_fixture import OfflinePlatform, OfflineContext
from quota_guard.platform_keys import DeleteTarget
from quota_guard.revocation_permit import DeletePermit
from quota_guard.web_operator import (WebOperator, WebOperatorError, OperatorRequestGate,
                                      same_cost_request, BASE, EDIT_URL)
from quota_guard.web_operator import _forward_once
from quota_guard.platform_http import cost_url


class ScriptedPublicOperator:
    """Test double. Production imports no decision logic from this fixture."""
    def __init__(self):self.observations=[];self.actions=[]
    def decide(self,task,observation,history,*,deadline):
        self.observations.append(observation)
        nodes=observation.get('nodes',[])
        if observation.get('dialog'):
            action={'tool':'handle_dialog','arguments':{'ref':observation['dialog']['ref'],'action':'accept'}}
        else:
            node=None;mode='click';value=None
            if task['kind']=='READ':
                for label,field in [('Start date','start_date'),('End date','end_date')]:
                    candidate=next((n for n in nodes if n['text']==label),None)
                    if candidate and candidate['value']!=task['date_range'][field]:
                        node=candidate;mode='type';value=task['date_range'][field];break
                if node is None:
                    node=next((n for n in nodes if n['text']=='Read cost' and
                               task['target_ids'][0] in n['context']),None)
            else:
                node=next((n for n in nodes if n['text'] in ('Confirm deletion','Confirm permanent deletion','Continue')),None)
                if node is None:
                    node=next((n for n in nodes if n['text']=='Delete' and task['target_id'] in n['context']),None)
                if node is None:
                    node=next((n for n in nodes if n['text']=='Actions' and task['target_id'] in n['context']),None)
            if node:
                args={'ref':node['ref'],'action':mode}
                if value is not None:args['value']=value
                action={'tool':'act_on_node','arguments':args}
            else:action={'tool':'poll_action','arguments':{}}
        self.actions.append(action)
        return action


class OperatorBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime=Path(__file__).resolve().parents[1]/'browser-runtime'
        if not runtime.is_dir():runtime=runtime.parent.parent/'browser-runtime'
        if runtime.is_dir():os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime)
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch(headless=True,executable_path=cls.pw.chromium.executable_path)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.platform=OfflinePlatform(age_days=1).start()
        self.context=self.browser.new_context(service_workers='block')
        self.proxy=OfflineContext(self.context,self.platform.origin)
        self.context.route('**/*',self.proxy.route_bottom)
        def fetch(route,**options):
            self.assertEqual(options['max_redirects'],0)
            self.assertEqual(options['max_retries'],0)
            request=route.request
            return self.proxy.request.fetch(request.url,method=request.method,
                body=request.post_data_buffer,source='browser')
        self.fetch_patch=patch.object(Route,'fetch',fetch)
        self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)
        self.page=self.context.new_page()
        self.page.set_default_timeout(2000)
        self.runtime=ScriptedPublicOperator()
        self.events=[]
        self.operator=WebOperator(self.page,self.runtime,emit=lambda event,**fields:self.events.append((event,fields)))

    def tearDown(self):self.context.close();self.platform.close()

    def target(self):
        row=next(k for k in self.platform.keys if k['tracking_id']==self.platform.target_id)
        return DeleteTarget(row['tracking_id'],row['name'],row['sensitive_id'],row['created_at'])

    def permit(self):
        permit=DeletePermit(target_tracking_id=self.platform.target_id)
        def approve():
            if permit.ready.wait(15):permit.decide(True)
        thread=threading.Thread(target=approve,daemon=True);thread.start()
        self.addCleanup(thread.join,1)
        return permit

    def writes(self):return [e for e in self.platform.events if e['kind']=='post_arrival']

    def test_exact_date_cost_requires_actual_model_actions_and_hides_secret_column(self):
        first=(datetime.now(TZ)-timedelta(days=1)).date().isoformat()
        url,payload=self.operator.read_cost(first,self.platform.today,[self.platform.target_id],deadline=time.monotonic()+15)
        self.assertTrue(same_cost_request(url,cost_url(first,self.platform.today,tracking_ids=[self.platform.target_id])))
        reads=[e for e in self.platform.events if e['kind']=='cost_read']
        self.assertEqual(len(reads),1);self.assertEqual(reads[0]['ids'],[self.platform.target_id])
        self.assertEqual(reads[0]['source'],'browser')
        self.assertEqual(self.writes(),[])
        observation=json.dumps(self.runtime.observations)
        self.assertNotIn('sk-synthetic',observation)
        self.assertNotIn('offline-fixture',observation)
        self.assertIn(self.platform.control_id,observation)  # no target filtering
        self.assertIn(self.platform.target_id,observation)
        self.assertNotIn(self.platform.target_id,json.dumps(self.events))
        self.assertTrue(any(a['arguments'].get('action')=='type' for a in self.runtime.actions))

    def test_model_delete_handles_menu_and_two_confirmations(self):
        for variant in ('menu','confirm2'):
            with self.subTest(variant=variant):
                self.platform.variant=variant
                self.platform.keys=json.loads(json.dumps(self.platform.initial))
                self.platform.events=[]
                permit=self.permit()
                result=self.operator.delete(self.target(),permit,[k['tracking_id'] for k in self.platform.keys],deadline=time.monotonic()+15)
                self.assertEqual(result,{'submitted':True})
                self.assertEqual(len(self.writes()),1)
                self.assertEqual(self.writes()[0]['source'],'browser')
                self.assertNotIn(self.platform.target_id,[k['tracking_id'] for k in self.platform.keys])
                self.assertIn(self.platform.control_id,[k['tracking_id'] for k in self.platform.keys])

    def test_native_confirm_needs_separate_model_action(self):
        self.platform.variant='native_confirm'
        self.operator.delete(self.target(),self.permit(),[k['tracking_id'] for k in self.platform.keys],deadline=time.monotonic()+15)
        self.assertEqual(len(self.writes()),1)
        self.assertTrue(any(a['tool']=='handle_dialog' for a in self.runtime.actions))
        self.assertTrue(any(o.get('dialog') for o in self.runtime.observations))

    def test_no_model_actions_cannot_read_or_delete(self):
        class NoModel:
            def decide(self,*_a,**_k):return {'tool':'report_blocker','arguments':{'reason':'unavailable'}}
        operator=WebOperator(self.page,NoModel())
        with self.assertRaises(WebOperatorError):
            operator.delete(self.target(),self.permit(),[k['tracking_id'] for k in self.platform.keys],deadline=time.monotonic()+5)
        self.assertFalse(self.writes())
        with self.assertRaises(WebOperatorError):
            operator.read_cost(self.platform.today,self.platform.today,[self.platform.target_id],deadline=time.monotonic()+5)
        self.assertFalse([e for e in self.platform.events if e['kind']=='cost_read'])

    def test_response_drop_is_unknown_not_not_submitted(self):
        self.platform.fault='response_drop'
        with self.assertRaises(WebOperatorError) as caught:
            self.operator.delete(self.target(),self.permit(),[k['tracking_id'] for k in self.platform.keys],deadline=time.monotonic()+15)
        self.assertTrue(caught.exception.may_have_submitted)
        self.assertEqual(caught.exception.reason_code,'AI_OPERATOR_RESULT_UNKNOWN')
        self.assertEqual(len(self.writes()),1)

    def test_wrong_target_and_unrelated_post_never_reach_server(self):
        self.page.goto(BASE+'/api_keys');self.page.wait_for_selector('tbody tr')
        permit=self.permit()
        with OperatorRequestGate(self.page,prepared=self.target(),permit=permit,inventory=(),deadline=time.monotonic()+10) as gate:
            self.page.evaluate("""url=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'delete',tracking_id:'wrong'})}).catch(()=>{})""",EDIT_URL)
            self.page.wait_for_timeout(50)
            self.assertEqual(gate.violation,'AI_OPERATOR_WRITE_REJECTED')
        self.assertFalse(permit.ready.is_set());self.assertFalse(self.writes())
        # Stage teardown must not reopen writes for delayed page timers/retries.
        self.page.evaluate("""url=>fetch(url,{method:'POST',body:'{}'}).catch(()=>{})""",EDIT_URL)
        self.page.wait_for_timeout(50)
        self.assertFalse(self.writes())

    def test_current_reference_rejects_changed_row(self):
        self.page.goto(BASE+'/api_keys');self.page.wait_for_selector('tbody tr')
        observation=self.operator._observe()
        node=next(n for n in observation['nodes'] if n['text']=='Delete' and self.platform.target_id in n['context'])
        self.page.evaluate("""id=>{let row=[...document.querySelectorAll('tr')].find(r=>r.textContent.includes(id));row.cells[1].textContent='changed'}""",self.platform.target_id)
        with self.assertRaises(WebOperatorError) as caught:
            self.operator._action({'tool':'act_on_node','arguments':{'ref':node['ref'],'action':'click'}},observation,time.monotonic()+5)
        self.assertEqual(caught.exception.reason_code,'AI_OPERATOR_STALE_REFERENCE')
        self.assertFalse(self.writes())

    def test_input_feedback_reports_no_change_and_actual_readback(self):
        self.page.goto(BASE+'/usage');self.page.wait_for_selector('tbody tr')
        observation=self.operator._observe()
        node=next(n for n in observation['nodes'] if n['text']=='Start date')
        action={'tool':'act_on_node','arguments':{'ref':node['ref'],'action':'type','value':node['value']}}
        result,_=self.operator._action(action,observation,time.monotonic()+5)
        self.assertEqual(result,{'status':'no_change','value_before':node['value'],
                                 'value_after':node['value'],'changed':False})
        observation=self.operator._observe()
        node=next(n for n in observation['nodes'] if n['text']=='Start date')
        action['arguments'].update(ref=node['ref'],value='2026-01-01')
        result,_=self.operator._action(action,observation,time.monotonic()+5)
        self.assertEqual(result['status'],'completed');self.assertTrue(result['changed'])
        self.assertEqual(result['value_after'],'2026-01-01')

    def test_delayed_duplicate_post_cannot_escape_completed_stage(self):
        original=self.platform.handle
        def handle(method,path,body=b'',source='browser'):
            status,media,data=original(method,path,body,source)
            if method=='GET' and path=='/api_keys':
                script=b'''<script>const actualFetch=window.fetch;window.fetch=(url,options)=>{
                  const pending=actualFetch(url,options);if(String(url).includes('edit_api_keys'))
                  setTimeout(()=>actualFetch(url,options).catch(()=>{}),300);return pending};</script>'''
                data+=script
            return status,media,data
        self.platform.handle=handle
        self.operator.delete(self.target(),self.permit(),[k['tracking_id'] for k in self.platform.keys],deadline=time.monotonic()+10)
        self.page.wait_for_timeout(500)
        self.assertEqual(len(self.writes()),1)

    def test_redirect_like_write_response_is_unknown(self):
        original=self.platform.handle
        def handle(method,path,body=b'',source='browser'):
            status,media,data=original(method,path,body,source)
            if method=='POST':return 302,media,data
            return status,media,data
        self.platform.handle=handle
        with self.assertRaises(WebOperatorError) as caught:
            self.operator.delete(self.target(),self.permit(),[k['tracking_id'] for k in self.platform.keys],deadline=time.monotonic()+10)
        self.assertTrue(caught.exception.may_have_submitted)
        self.assertEqual(caught.exception.reason_code,'AI_OPERATOR_RESULT_UNKNOWN')
        self.assertEqual(len(self.writes()),1)

    def test_bootstrap_exact_cost_does_not_replace_model_action(self):
        expected=cost_url(self.platform.today,self.platform.today,tracking_ids=[self.platform.target_id])
        original=self.platform.handle
        def handle(method,path,body=b'',source='browser'):
            status,media,data=original(method,path,body,source)
            if method=='GET' and path=='/usage':
                data+=('<script>fetch('+json.dumps(expected)+').catch(()=>{})</script>').encode()
            return status,media,data
        self.platform.handle=handle
        class NoModel:
            def decide(self,*_a,**_k):return {'tool':'report_blocker','arguments':{'reason':'no action'}}
        with self.assertRaises(WebOperatorError):
            WebOperator(self.page,NoModel()).read_cost(self.platform.today,self.platform.today,
                [self.platform.target_id],deadline=time.monotonic()+5)
        self.assertFalse([e for e in self.platform.events if e['kind']=='cost_read'])

    def test_poll_only_delayed_cost_cannot_be_authorized(self):
        expected=cost_url(self.platform.today,self.platform.today,tracking_ids=[self.platform.target_id])
        original=self.platform.handle
        def handle(method,path,body=b'',source='browser'):
            status,media,data=original(method,path,body,source)
            if method=='GET' and path=='/usage':
                data+=('<script>setTimeout(()=>fetch('+json.dumps(expected)+').catch(()=>{}),150)</script>').encode()
            return status,media,data
        self.platform.handle=handle
        class PollOnly:
            def decide(self,*_a,**_k):return {'tool':'poll_action','arguments':{}}
        with self.assertRaises(WebOperatorError):
            WebOperator(self.page,PollOnly()).read_cost(self.platform.today,self.platform.today,
                [self.platform.target_id],deadline=time.monotonic()+1)
        self.assertFalse([e for e in self.platform.events if e['kind']=='cost_read'])

    def test_onload_exact_delete_does_not_obtain_permit_or_send(self):
        target=self.target();permit=self.permit()
        body={'action':'delete','name':None,'tracking_id':target.tracking_id,
              'created_at':target.created_at,'redacted_key':target.sensitive_id}
        original=self.platform.handle
        def handle(method,path,payload=b'',source='browser'):
            status,media,data=original(method,path,payload,source)
            if method=='GET' and path=='/api_keys':
                data+=('<script>fetch('+json.dumps(EDIT_URL)+', {method:"POST",headers:{"Content-Type":"application/json"},body:'+json.dumps(json.dumps(body))+'}).catch(()=>{})</script>').encode()
            return status,media,data
        self.platform.handle=handle
        with self.assertRaises(WebOperatorError):
            self.operator.delete(target,permit,[k['tracking_id'] for k in self.platform.keys],deadline=time.monotonic()+5)
        self.assertFalse(permit.ready.is_set());self.assertFalse(self.writes())

    def test_public_readiness_wait_and_background_egress_rejection_are_nonfatal(self):
        original=self.platform.handle
        def handle(method,path,body=b'',source='browser'):
            status,media,data=original(method,path,body,source)
            if method=='GET' and path=='/usage':
                data=data.replace(b"main.setAttribute('aria-busy','false')",b"setTimeout(()=>main.setAttribute('aria-busy','false'),400)")
                data+=b'<script>fetch("https://telemetry.invalid/list",{method:"POST",body:"synthetic"}).catch(()=>{})</script>'
            return status,media,data
        self.platform.handle=handle
        self.operator.read_cost(self.platform.today,self.platform.today,[self.platform.target_id],deadline=time.monotonic()+5)
        self.assertTrue(self.runtime.observations)
        self.assertTrue(all(o['readiness']['busy'] is False for o in self.runtime.observations))
        self.assertTrue(any(e=='operator_request_rejected' for e,_ in self.events))
        self.assertEqual(len([e for e in self.platform.events if e['kind']=='cost_read']),1)

    def test_real_fetch_307_and_308_locations_do_not_replay_post(self):
        self.fetch_patch.stop()  # Exercise actual Playwright transport on owned loopback only.
        for status in (307,308):
            with self.subTest(status=status):
                arrivals=[]
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self,*unused):pass
                    def do_GET(self):
                        data=b'<script>fetch("/first",{method:"POST",body:"owned"}).catch(()=>{})</script>'
                        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                    def do_POST(self):
                        self.rfile.read(int(self.headers.get('Content-Length','0')));arrivals.append(self.path)
                        self.send_response(status if self.path=='/first' else 200)
                        if self.path=='/first':self.send_header('Location','/replay')
                        self.send_header('Content-Length','0');self.end_headers()
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                context=self.browser.new_context()
                def forward(route):
                    if route.request.method!='POST':return route.fallback()
                    try:_forward_once(route,timeout_ms=2000)
                    except WebOperatorError:route.abort('blockedbyclient')
                context.route('**/*',forward)
                page=context.new_page()
                try:
                    page.goto('http://127.0.0.1:'+str(server.server_port)+'/')
                    page.wait_for_timeout(200)
                    self.assertEqual(arrivals,['/first'])
                finally:
                    context.close();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
