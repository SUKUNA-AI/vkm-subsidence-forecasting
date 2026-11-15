"""Captured request policy and authenticated local accounting control tests."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from request_gate import WirePolicy, HookGate, BudgetThread
from vkm_worker_core import WorkerError

P = {'model_id':'fixture/model','provider_only':['fixture/provider'], 'reasoning_effort':'high', 'max_output_tokens':64}
E = {'context_length':64000,'max_completion_tokens':128,'pricing':{'prompt':'0.000001','completion':'0.000002'}}
def payload():
    return {'body_text':json.dumps({'model':P['model_id'],'provider':{'only':P['provider_only'],'allow_fallbacks':False,'require_parameters':True},
            'reasoning':{'enabled':True,'effort':'high','exclude':True},'max_tokens':64,'messages':[{'role':'user','content':'Синтетика'}]},ensure_ascii=False),
            'kind':'primary','url':'https://openrouter.ai/api/v1/chat/completions'}
def mutate(p, field, value):
    body=json.loads(p['body_text']);body[field]=value;p['body_text']=json.dumps(body);return p

class PolicyTests(unittest.TestCase):
    def test_owner_revoked_kimi_api(self):
        p={**P,'model_id':'moonshotai/kimi-k3.1-pro'}
        with self.assertRaisesRegex(WorkerError,'revoked'):
            WirePolicy(p,E).inspect(mutate(payload(),'model',p['model_id']))
    def test_captured_bytes_hash(self):
        p=payload();pins, cost, evidence=WirePolicy(P,E).inspect(p)
        self.assertEqual(pins['request_sha256'],hashlib.sha256(p['body_text'].encode()).hexdigest())
        self.assertEqual(evidence['request_bytes'],len(p['body_text'].encode()));self.assertGreater(cost,0)
    def test_wrong_model(self):
        with self.assertRaises(WorkerError):WirePolicy(P,E).inspect(mutate(payload(),'model','other/model'))
    def test_no_fallback(self):
        p=payload();b=json.loads(p['body_text']);b['provider']['allow_fallbacks']=True;p['body_text']=json.dumps(b)
        with self.assertRaises(WorkerError):WirePolicy(P,E).inspect(p)
    def test_reasoning_exact(self):
        with self.assertRaises(WorkerError):WirePolicy(P,E).inspect(mutate(payload(),'reasoning',{'effort':'max'}))
    def test_output_bound(self):
        for x in [None,0,True,65]:
            with self.subTest(x=x),self.assertRaises(WorkerError):WirePolicy(P,E).inspect(mutate(payload(),'max_tokens',x))
    def test_unknown_tariff(self):
        ep=copy.deepcopy(E);ep['pricing']['new_charge']='1'
        with self.assertRaises(WorkerError):WirePolicy(P,ep).inspect(payload())
    def test_unqualified_image_denied(self):
        p=mutate(payload(),'messages',[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:foo'}}]}])
        with self.assertRaises(WorkerError):WirePolicy(P,E).inspect(p)
    def test_wrong_url_and_hidden_kind(self):
        for field,value in [('url','https://evil.invalid/v1/chat/completions'),('kind','retry')]:
            p=payload();p[field]=value
            with self.subTest(field=field),self.assertRaises(WorkerError):WirePolicy(P,E).inspect(p)
    def test_duplicate_key_rejected(self):
        p=payload();p['body_text']='{"model":"one","model":"two"}'
        with self.assertRaises(WorkerError):WirePolicy(P,E).inspect(p)
    def test_maintenance_same_pins(self):
        for kind in ['primary','title','compaction','generate']:
            p=payload();p['kind']=kind;self.assertEqual(WirePolicy(P,E).inspect(p)[0]['model'],P['model_id'])

class FakeSession:
    def __init__(self):self.calls=[]
    def before_attempt(self,*a,**kw):self.calls.append(('before',a,kw));return {'state':'POSSIBLY_SENT'}
    def after_attempt(self,*a,**kw):self.calls.append(('after',a,kw));return {'state':'KNOWN'}
    def finish(self,**kw):self.calls.append(('finish',kw));return {'known':True}
    def abort(self,*a):self.calls.append(('abort',a));return {'unknown':True}

class ControlTests(unittest.TestCase):
    def test_transport_ids_survive_parse_failure_without_settlement(self):
        with tempfile.TemporaryDirectory() as td:
            s=FakeSession();g=HookGate(BudgetThread(lambda:s),WirePolicy(P,E),Path(td))
            try:
                g.dispatch('/ready',{'protocol':2,'plugin_sha256':g.plugin_sha256});g.dispatch('/before',payload())
                evidence={'attempt_id':'http-1','http_status':200,'response_sha256':'a'*64,'content_type':'text/event-stream','headers':{'x-request-id':'request-fixture','x-generation-id':'gen-fixture'}}
                g.dispatch('/response-metadata',evidence)
                self.assertEqual(json.loads((Path(td)/'http-1.transport.json').read_bytes())['headers'],evidence['headers'])
                self.assertEqual([x[0] for x in s.calls],['before'])
                for bad in [{**evidence,'headers':{'authorization':'Bearer-secret'}},{**evidence,'headers':{'x-request-id':'unsafe\nvalue'}},{**evidence,'attempt_id':'other'}]:
                    with self.assertRaises(WorkerError):g.dispatch('/response-metadata',bad)
            finally:self.assertTrue(g.close(clean=False)['unknown'])
    def test_http_error_preserved_without_zero_settlement(self):
        with tempfile.TemporaryDirectory() as td:
            s=FakeSession();g=HookGate(BudgetThread(lambda:s),WirePolicy(P,E),Path(td))
            g.dispatch('/ready',{'protocol':2,'plugin_sha256':g.plugin_sha256});a=g.dispatch('/before',payload())
            g.dispatch('/unresolved',{'attempt_id':a['attempt_id'],'http_status':400,'response_sha256':'c'*64,'reason':'HTTP_ERROR','generation_id':None,'error_code':'400','error_message':'sanitized fixture'})
            self.assertTrue(g.close(clean=False)['unknown']);self.assertEqual([x[0] for x in s.calls],['before','abort'])
            self.assertTrue((Path(td)/'http-1.unresolved.json').is_file())
    def test_authenticated_control_and_complete_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            s=FakeSession();g=HookGate(BudgetThread(lambda:s),WirePolicy(P,E),Path(td))
            try:
                g.dispatch('/ready',{'protocol':2,'plugin_sha256':g.plugin_sha256})
                req=urllib.request.Request(g.url+'/before',data=json.dumps(payload()).encode(),headers={'Authorization':'Bearer '+g.token})
                admitted=json.loads(urllib.request.urlopen(req).read())
                self.assertTrue(admitted['permit'])
                with self.assertRaises(WorkerError):g.dispatch('/before',payload())
                g.dispatch('/after',{'attempt_id':admitted['attempt_id'],'generation_id':'gen-fixture','cost':'0.00001','response_sha256':'a'*64,'http_status':200})
            finally:r=g.close(clean=True)
            self.assertTrue(r['known']);self.assertEqual([x[0] for x in s.calls],['before','after','finish'])
            self.assertEqual(s.calls[0][2]['request_body'],payload()['body_text'].encode())
    def test_unknown_response_keeps_hold(self):
        with tempfile.TemporaryDirectory() as td:
            s=FakeSession();g=HookGate(BudgetThread(lambda:s),WirePolicy(P,E),Path(td))
            g.dispatch('/ready',{'protocol':2,'plugin_sha256':g.plugin_sha256});g.dispatch('/before',payload())
            with self.assertRaises(WorkerError):g.dispatch('/after',{'attempt_id':'wrong','generation_id':'x','cost':'0','response_sha256':'b'*64,'http_status':200})
            self.assertTrue(g.close(clean=False)['unknown'])
    def test_missing_auth_denies_before_admission(self):
        with tempfile.TemporaryDirectory() as td:
            s=FakeSession();g=HookGate(BudgetThread(lambda:s),WirePolicy(P,E),Path(td))
            try:
                with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(urllib.request.Request(g.url+'/before',data=b'{}'))
                self.assertFalse(s.calls)
            finally:g.close(clean=True)

if __name__=='__main__':unittest.main()
