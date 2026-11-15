import copy,hashlib,json,sys,tempfile,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import request_gate as gate
from vkm_worker_core import WorkerError
from shared_budget import BudgetError

P={'model_id':'fixture/model','provider_only':['fixture/provider'],'reasoning_effort':'high','max_output_tokens':64}
E={'context_length':64000,'max_completion_tokens':128,'pricing':{'prompt':'0.000001','completion':'0.000002'}}
SECRET='FAKE_PRIVATE_REASONING_KEY_SENTINEL'
def payload():
    return {'body_text':json.dumps({'model':P['model_id'],'provider':{'only':P['provider_only'],'allow_fallbacks':False,'require_parameters':True},'reasoning':{'enabled':True,'effort':'high','exclude':True},'max_tokens':64,'messages':[{'role':'user','content':SECRET}]}),'kind':'primary','url':'https://openrouter.ai/api/v1/chat/completions'}
def mutate(field,value):
    p=payload();b=json.loads(p['body_text']);b[field]=value;p['body_text']=json.dumps(b);return p
class FakeBudget:
    def __init__(self,error=None):self.calls=[];self.error=error;self.durable=False
    def call(self,method,*args,**kw):
        self.calls.append((method,args,kw))
        if method=='before_attempt':
            if self.error:raise self.error
            self.durable=True;return {'state':'POSSIBLY_SENT','remaining_cap_usd':'0.1'}
        if method=='after_attempt':return {'state':'OPEN','session_cap_usd':'0.1','attempts':[{'state':'KNOWN','cost_usd':'0.002'}]}
        if method=='finish':return {'state':'SETTLED'}
        if method=='abort':return {'state':'UNKNOWN'}
        raise AssertionError('diagnostics must not add budget RPC')
def setup(td,budget=None,policy=None):
    b=budget or FakeBudget();g=gate.HookGate(b,policy or gate.WirePolicy(P,E),Path(td));g.dispatch('/ready',{'protocol':2,'plugin_sha256':g.plugin_sha256});return g,b
def diagnostics(td):return sorted(Path(td).glob('denied-*.json'))
def one(td):
    files=diagnostics(td);assert len(files)==1, 'exactly one persisted denial required'
    raw=files[0].read_text(encoding='utf-8');assert SECRET not in raw
    d=json.loads(raw);assert d['schema']=='vkm.opencode.denied-telemetry/1';assert d['request_admitted'] is False;assert d['content_saved'] is False;assert d['accounting_changed'] is False
    assert d['stage'] in {'HOOK_HANDLER','WIRE_POLICY','BUDGET_BEFORE','TOOL_SCOPE','DISPATCH','DISPATCH_AFTER','UNCLASSIFIED'}
    return d
def http(g,path,p,auth=True):
    h={'Content-Type':'application/json'}
    if auth:h['Authorization']='Bearer '+g.token
    return urllib.request.urlopen(urllib.request.Request(g.url+path,data=json.dumps(p).encode(),headers=h),timeout=5)
class DenialRepro(unittest.TestCase):
    def test_valid_lifecycle_preserves_durable_before_and_no_denials(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=setup(td)
            try:
                a=g.dispatch('/before',payload());self.assertTrue(a['permit']);self.assertTrue(b.durable)
                g.dispatch('/after',{'attempt_id':a['attempt_id'],'generation_id':'gen-fixture','cost':'0.002','response_sha256':'a'*64,'http_status':200})
                self.assertEqual(diagnostics(td),[])
            finally:self.assertEqual(g.close(clean=True)['state'],'SETTLED')
            self.assertEqual([x[0] for x in b.calls],['before_attempt','after_attempt','finish'])
    def test_direct_context_denial_persisted_and_no_budget_admission(self):
        with tempfile.TemporaryDirectory() as td:
            ep=copy.deepcopy(E);ep['context_length']=1;g,b=setup(td,policy=gate.WirePolicy(P,ep));p=payload()
            try:
                with self.assertRaises(WorkerError):g.dispatch('/before',p)
                d=one(td);self.assertEqual(d['reason_code'],'ENDPOINT_CONTEXT_LIMIT');self.assertEqual(d['stage'],'WIRE_POLICY');self.assertEqual(d['body_sha256'],hashlib.sha256(p['body_text'].encode()).hexdigest());self.assertEqual(d['body_utf8_bytes'],len(p['body_text'].encode()));self.assertFalse(b.calls)
            finally:g.close(clean=True)
    def test_http_wrong_provider_one_record_403_no_permit(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=setup(td)
            try:
                with self.assertRaises(urllib.error.HTTPError) as cm:http(g,'/before',mutate('provider',{'only':['wrong'],'allow_fallbacks':False,'require_parameters':True}))
                self.assertEqual(cm.exception.code,403);self.assertEqual(one(td)['reason_code'],'PROVIDER_FALLBACK_PIN_MISMATCH');self.assertFalse(b.calls);self.assertEqual(g.denied,1)
            finally:g.close(clean=True)
    def test_auth_denial_records_no_header_token_or_control_body(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=setup(td)
            try:
                with self.assertRaises(urllib.error.HTTPError):http(g,'/before',payload(),auth=False)
                d=one(td);self.assertEqual(d['reason_code'],'HOOK_AUTHENTICATION_FAILED');self.assertEqual(d['stage'],'HOOK_HANDLER');self.assertFalse(b.calls)
                raw=diagnostics(td)[0].read_text(encoding='utf-8');self.assertNotIn(g.token,raw)
            finally:g.close(clean=True)
    def test_budget_typed_denial_retains_estimate_and_no_inflight(self):
        with tempfile.TemporaryDirectory() as td:
            b=FakeBudget(BudgetError('SESSION_BUDGET_EXHAUSTED'));g,_=setup(td,budget=b);p=payload();_,upper,_=g.policy.inspect(p)
            try:
                with self.assertRaises(BudgetError):g.dispatch('/before',p)
                d=one(td);self.assertEqual(d['reason_code'],'SESSION_BUDGET_EXHAUSTED');self.assertEqual(d['stage'],'BUDGET_BEFORE');self.assertEqual(d['metrics']['estimated_upper_usd'],str(upper));self.assertIsNone(g.inflight);self.assertFalse(b.durable)
                self.assertEqual([x[0] for x in b.calls],['before_attempt'])
            finally:g.close(clean=True)
    def test_unknown_worker_message_is_unclassified_not_leaked(self):
        class BadPolicy:
            def inspect(self,p):raise WorkerError(SECRET)
        with tempfile.TemporaryDirectory() as td:
            g,b=setup(td,policy=BadPolicy())
            try:
                with self.assertRaises(WorkerError):g.dispatch('/before',payload())
                self.assertEqual(one(td)['reason_code'],'UNCLASSIFIED');self.assertFalse(b.calls)
            finally:g.close(clean=True)
    def test_foreign_exception_code_not_trusted(self):
        class Foreign(Exception):code='SESSION_BUDGET_EXHAUSTED'
        with tempfile.TemporaryDirectory() as td:
            g,b=setup(td,budget=FakeBudget(Foreign(SECRET)))
            try:
                with self.assertRaises(Foreign):g.dispatch('/before',payload())
                self.assertEqual(one(td)['reason_code'],'UNCLASSIFIED')
            finally:g.close(clean=True)
    def test_diagnostic_write_failure_keeps_original_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=setup(td);original=gate.write_json
            def fail_denied(path,*a,**kw):
                if Path(path).name.startswith('denied-'):raise OSError(SECRET)
                return original(path,*a,**kw)
            try:
                with mock.patch.object(gate,'write_json',side_effect=fail_denied):
                    with self.assertRaises(WorkerError):g.dispatch('/before',mutate('model','wrong'))
                self.assertFalse(b.calls);self.assertIsNone(g.inflight)
            finally:g.close(clean=True)
    def test_unresolved_attempt_stays_unknown_after_denied_after(self):
        with tempfile.TemporaryDirectory() as td:
            g,b=setup(td);a=g.dispatch('/before',payload())
            try:
                with self.assertRaises(WorkerError):g.dispatch('/after',{'attempt_id':'wrong','generation_id':'gen-fixture','cost':'0','response_sha256':'a'*64,'http_status':200})
                one(td);self.assertEqual(g.inflight,a['attempt_id'])
            finally:self.assertEqual(g.close(clean=False)['state'],'UNKNOWN')
            self.assertEqual([x[0] for x in b.calls],['before_attempt','abort'])
if __name__=='__main__':unittest.main()
