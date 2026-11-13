"""Explicit offline acceptance fixture. Never imported by normal GUI startup.

Real browser actions use the product's HTTPS URLs, which a bottom route maps
only to this owned loopback HTTP server. Context API POST is forbidden: every
write must originate in an actual browser action and pass the product gate.
"""
from datetime import datetime,timedelta
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import random
import threading
import time
from urllib.parse import urlsplit,parse_qs
import urllib.request
import urllib.error
from uuid import UUID

from .core import TZ

ORIGIN='https://platform.deepseek.com'
LIST='/api/v0/users/get_api_keys'
EDIT='/api/v0/users/edit_api_keys'
COST='/api/v0/usage/by_api_key/cost'

HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Offline acceptance</title>
<style>body{font:16px sans-serif;padding:24px}td,th{padding:12px;border-bottom:1px solid #ccc}button,input{padding:8px;margin:4px}dialog{min-width:430px}nav{margin:10px}</style></head>
<body><nav><a href="/usage">Usage</a> <a href="/api_keys">API keys</a></nav><h1 id="heading"></h1>
<main aria-busy="true" data-load-state="loading"><section id="dates"></section><table><thead><tr><th>Name</th><th>Tracking ID</th><th>Key</th><th>Created at</th><th>Last use</th><th>Actions</th></tr></thead><tbody></tbody></table>
<p id="status"></p></main><dialog id="confirmation"><h2>Delete API key</h2><p id="identity"></p><p>This is permanent.</p><button id="cancel">Cancel</button><button id="confirm">Confirm deletion</button></dialog>
<script>
const config=__CONFIG__;const headers={'Authorization':'Bearer offline-fixture','X-Client-Platform':'web','X-Device-Model':''};
const isUsage=location.pathname==='/usage';const main=document.querySelector('main'), tbody=document.querySelector('tbody'), modal=document.querySelector('dialog');let target=null,stage=0;
document.querySelector('h1').textContent=isUsage?'Usage by API key':'API keys';
const text=(tag,value)=>{let n=document.createElement(tag);n.textContent=value;return n};
const button=(label,callback)=>{let b=text('button',label);b.onclick=callback;return b};
if(isUsage){let section=document.querySelector('#dates');for(let label of ['Start date','End date']){let l=text('label',label);let i=document.createElement('input');i.type='text';i.setAttribute('aria-label',label);i.id=label==='Start date'?'start':'end';i.value=config.today;l.append(i);section.append(l)}}
async function readCost(key){const start=Math.floor(Date.parse(document.querySelector('#start').value+'T00:00:00+08:00')/1000),end=Math.floor(Date.parse(document.querySelector('#end').value+'T00:00:00+08:00')/1000)+86400;
 const q=new URLSearchParams({start:String(start),end:String(end),tz:'28800'});q.append('api_key_tracking_id',key.tracking_id);
 try{const r=await fetch('__COST__?'+q,{headers});document.querySelector('#status').textContent=JSON.stringify(await r.json())}catch(e){document.querySelector('#status').textContent='Read did not complete'}}
async function submitDelete(key){let payload={action:'delete',name:null,redacted_key:key.sensitive_id,created_at:key.created_at,tracking_id:key.tracking_id};
try{let r=await fetch('__EDIT__',{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!r.ok)throw Error();await r.json();modal.close();await load()}catch(e){document.querySelector('#status').textContent='Submission result unknown; do not retry'}}
function openDelete(key){target=key;stage=0;if(config.variant==='native_confirm'){if(confirm('Permanently delete '+key.name+' '+key.tracking_id+'?'))submitDelete(key);return}
document.querySelector('#identity').textContent=key.name+' '+key.tracking_id;document.querySelector('#confirm').textContent=config.variant==='confirm2'?'Continue':'Confirm deletion';modal.showModal()}
document.querySelector('#cancel').onclick=()=>modal.close();document.querySelector('#confirm').onclick=()=>{if(config.variant==='confirm2'&&stage===0){stage=1;document.querySelector('#confirm').textContent='Confirm permanent deletion';return}submitDelete(target)};
async function load(){main.setAttribute('aria-busy','true');main.dataset.loadState='loading';let r=await fetch('__LIST__',{headers});let data=await r.json();let keys=data.data.biz_data.api_keys;tbody.replaceChildren();
for(const key of keys){let tr=document.createElement('tr');tr.append(text('td',key.name));let identity=text('td',key.tracking_id);identity.title=key.tracking_id;tr.append(identity);tr.append(text('td',key.key_type==='DSH'?'Harness managed':key.sensitive_id));tr.append(text('td',new Date(key.created_at*1000).toISOString().slice(0,10)));tr.append(text('td','-'));let cell=document.createElement('td');
if(isUsage){cell.append(button('Read cost',()=>readCost(key)))}else if(key.key_type==='NORMAL'){if(config.variant==='menu'){let more=button('Actions',()=>{if(!cell.querySelector('[data-delete]')){let b=button('Delete',()=>openDelete(key));b.dataset.delete='true';cell.append(b)}});cell.append(more)}else cell.append(button('Delete',()=>openDelete(key)))}else{let b=button('Managed by Harness',()=>{});b.disabled=true;cell.append(b)}tr.append(cell);tbody.append(tr)}main.setAttribute('aria-busy','false');main.dataset.loadState='ready'}
load();</script></body></html>'''

def envelope(data):return {'code':0,'msg':'','data':{'biz_code':0,'biz_msg':'','biz_data':data}}

class OfflinePlatform:
    def __init__(self,seed=4100,variant='direct',*,cost='1.25',age_days=0,fault='none',similar_ids=False):
        self.seed,self.variant,self.cost,self.fault=seed,variant,cost,fault
        self.today=datetime.now(TZ).date().isoformat()
        created=int((datetime.now(TZ)-timedelta(days=age_days)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
        rng=random.Random(seed)
        ids=([str(UUID(int=seed*100+i)) for i in (1,2,3)] if similar_ids else
             [str(UUID(int=rng.getrandbits(128),version=4)) for _ in range(3)])
        self.target_id,self.control_id=ids[:2]
        self.keys=[{'name':'Synthetic target','tracking_id':self.target_id,'sensitive_id':'sk-synthetic*****aaaa','created_at':created,'last_use':None,'key_type':'NORMAL'},
                   {'name':'Independent control','tracking_id':self.control_id,'sensitive_id':'sk-synthetic*****bbbb','created_at':created,'last_use':None,'key_type':'NORMAL'},
                   {'name':'DeepSeek Harness','tracking_id':ids[2],'sensitive_id':'','created_at':created,'last_use':None,'key_type':'DSH'}]
        random.Random(seed).shuffle(self.keys);self.initial=json.loads(json.dumps(self.keys))
        self.events=[];self.lock=threading.RLock();self.server=None;self.thread=None

    def event(self,kind,**fields):
        with self.lock:self.events.append({'kind':kind,'monotonic':time.monotonic(),**fields})

    def handle(self,method,path,body=b'',source='browser'):
        parsed=urlsplit(path)
        if method=='GET' and parsed.path in ('/api_keys','/usage'):
            config=json.dumps({'today':self.today,'variant':self.variant})
            html=HTML.replace('__CONFIG__',config).replace('__COST__',COST).replace('__EDIT__',EDIT).replace('__LIST__',LIST)
            self.event('page',path=parsed.path,source=source)
            return 200,'text/html; charset=utf-8',html.encode()
        if method=='GET' and parsed.path==LIST:
            self.event('identity_read',source=source)
            return 200,'application/json',json.dumps(envelope({'api_keys':self.keys,'key_quota':{'total':{'used':len(self.keys)-1,'limit':300}}})).encode()
        if method=='GET' and parsed.path==COST:
            q=parse_qs(parsed.query);ids=q.get('api_key_tracking_id',[])
            self.event('cost_read',ids=ids,source=source,start=q.get('start'),end=q.get('end'))
            start=int(q['start'][0]);end=int(q['end'][0])
            series=[{'api_key':{'tracking_id':k},'model':'synthetic','buckets':[{'time':start,'cost':self.cost}]} for k in ids]
            data={'start':start,'end':end,'bucket':'1d','data':[{'currency':'CNY','series':series,'function_series':[]}]}
            return 200,'application/json',json.dumps(envelope(data)).encode()
        if method=='POST':
            self.event('post_arrival',path=parsed.path,source=source)
            try:payload=json.loads(body)
            except Exception:return 400,'application/json',b'{}'
            with self.lock:
                target=next((k for k in self.keys if k['tracking_id']==payload.get('tracking_id')),None)
                expected={'action':'delete','name':None,'redacted_key':target['sensitive_id'],'created_at':target['created_at'],'tracking_id':target['tracking_id']} if target else None
                if parsed.path!=EDIT or payload!=expected or target['key_type']!='NORMAL':return 400,'application/json',b'{}'
                self.event('write_received',target_id=target['tracking_id'])
                self.keys.remove(target);self.event('delete_applied',target_id=target['tracking_id'])
            if self.fault=='response_drop':return None,None,None
            return 200,'application/json',json.dumps(envelope({})).encode()
        return 404,'application/json',b'{}'

    def start(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*unused):pass
            def process(self):
                count=int(self.headers.get('Content-Length','0'))
                if not 0<=count<=1000000:self.send_error(413);return
                status,media,body=owner.handle(self.command,self.path,self.rfile.read(count),self.headers.get('X-Offline-Source','browser'))
                if status is None:self.close_connection=True;self.connection.shutdown(2);return
                self.send_response(status);self.send_header('Content-Type',media);self.send_header('Content-Length',str(len(body)));self.end_headers()
                try:self.wfile.write(body)
                except OSError:pass
            do_GET=process;do_POST=process
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();return self

    def close(self):
        if self.server:self.server.shutdown();self.server.server_close()
        if self.thread:self.thread.join(3)

    def snapshot(self):
        with self.lock:return json.loads(json.dumps({'initial_keys':self.initial,'keys':self.keys,'events':self.events}))

class OfflineResponse:
    def __init__(self,url,status,headers,body):
        self.url,self.status,self._body=url,status,body
        self.headers={k.lower():v for k,v in headers.items()}
    def body(self):return self._body
    def json(self):return json.loads(self._body)
    def dispose(self):pass

class LoopbackTransport:
    def __init__(self,origin):
        parsed=urlsplit(origin)
        if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.path:raise ValueError('Offline loopback origin required')
        self.origin=origin
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        self.http=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def fetch(self,url,*,method='GET',body=None,source='api-context'):
        if method not in ('GET','HEAD') and source!='browser':raise AssertionError('OFFLINE_SCRIPT_POST_FORBIDDEN')
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.netloc!='platform.deepseek.com' or parsed.fragment:raise ValueError('Offline unexpected origin')
        target=self.origin+parsed.path+('?' + parsed.query if parsed.query else '')
        req=urllib.request.Request(target,data=body,method=method,headers={'X-Offline-Source':source,'Content-Type':'application/json'})
        try:
            with self.http.open(req,timeout=5) as response:return OfflineResponse(url,response.status,dict(response.headers.items()),response.read())
        except urllib.error.HTTPError as exc:return OfflineResponse(url,exc.code,dict(exc.headers.items()),exc.read())
    def get(self,url,**unused):return self.fetch(url)
    def post(self,*args,**kwargs):raise AssertionError('OFFLINE_SCRIPT_POST_FORBIDDEN')

class OfflineContext:
    def __init__(self,context,origin):self._context=context;self.request=LoopbackTransport(origin)
    def __getattr__(self,name):return getattr(self._context,name)
    def route_bottom(self,route,request):
        try:
            response=self.request.fetch(request.url,method=request.method,body=request.post_data_buffer,source='browser')
            media=response.headers.get('content-type','application/json')
            route.fulfill(status=response.status,content_type=media,body=response.body())
        except Exception:route.abort('failed')
