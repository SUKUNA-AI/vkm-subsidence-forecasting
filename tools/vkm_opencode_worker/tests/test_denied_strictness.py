"""Public-safe wrapper of independent native80 methods; no host/private paths."""
from pathlib import Path
from decimal import Decimal
import hashlib,json,sys,tempfile,unittest,urllib.error,urllib.request
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import request_gate as gate
import denied_telemetry as dt
from vkm_worker_core import WorkerError
from shared_budget import BudgetError
import test_denied_repro as base
SECRET='SYNTHETIC_QA_PRIVATE_PATH_KEY_REASONING_SENTINEL'
class Poison:
 def __str__(self):raise AssertionError('untrusted __str__ invoked')
 def __repr__(self):raise AssertionError('untrusted __repr__ invoked')

class Tests(unittest.TestCase):
 def test_exact_exception_types_not_subclasses(self):
  class W(WorkerError):pass
  class B(BudgetError):pass
  class F(Exception):code='SESSION_BUDGET_EXHAUSTED'
  with tempfile.TemporaryDirectory() as td:
   g,b=base.setup(td)
   try:
    for exc in [W('request model differs from selected model'),B('SESSION_BUDGET_EXHAUSTED'),F(SECRET),WorkerError(Poison()),WorkerError('request model differs from selected model',SECRET),BudgetError(Poison()),BudgetError('SESSION_BUDGET_EXHAUSTED '+SECRET)]:
     self.assertEqual(g._denial_reason(exc),'UNCLASSIFIED')
    self.assertEqual(g._denial_reason(BudgetError('HOOK_AUTHENTICATION_FAILED')),'UNCLASSIFIED')
    self.assertEqual(g._denial_reason(WorkerError('request model differs from selected model')),'MODEL_PIN_MISMATCH')
   finally:g.close(clean=True)
 def test_untrusted_code_properties_and_stage_strings(self):
  class String(str):pass
  self.assertEqual(dt.budget_reason(String('SESSION_BUDGET_EXHAUSTED')),'UNCLASSIFIED')
  for kw in [{'stage':String('DISPATCH'),'reason_code':'UNCLASSIFIED'},{'stage':'DISPATCH','reason_code':String('UNCLASSIFIED')},{'stage':SECRET,'reason_code':'UNCLASSIFIED'},{'stage':'DISPATCH','reason_code':SECRET}]:
   with self.subTest(kw=list(kw)):
    with self.assertRaises(ValueError):dt.build_record(**kw)
 def test_numeric_metric_bounds_and_no_coercion(self):
  invalid=[{'input_bound':True},{'input_bound':-1},{'input_bound':2**63},{'input_bound':1.0},{'messages_count':Poison()},{'unknown':0},{'estimated_upper_usd':True},{'estimated_upper_usd':float('nan')},{'estimated_upper_usd':float('inf')},{'estimated_upper_usd':Decimal('NaN')},{'estimated_upper_usd':Decimal('Infinity')},{'estimated_upper_usd':Decimal('-1')},{'estimated_upper_usd':Poison()},{'session_remaining_usd':SECRET}]
  for i,m in enumerate(invalid):
   with self.subTest(i=i):
    with self.assertRaises(ValueError):dt.build_record(stage='BUDGET_BEFORE',reason_code='UNCLASSIFIED',metrics=m)
  r=dt.build_record(stage='BUDGET_BEFORE',reason_code='UNCLASSIFIED',metrics={'input_bound':2**63-1,'estimated_upper_usd':Decimal('0.00000001')})
  json.dumps(r,allow_nan=False);self.assertLess(len(json.dumps(r)),800)
 def test_huge_decimal_never_expands_before_rejection(self):
  def fail_format(*a,**k):raise AssertionError('unbounded fixed expansion attempted')
  # These decimals are cheap to construct; no huge string/allocation is requested.
  with mock.patch.object(dt,'format',side_effect=fail_format,create=True):
   for value in [Decimal('1e-1000000000'),Decimal('0e-1000000000')]:
    with self.subTest(exponent=value.as_tuple().exponent):
     with self.assertRaises(ValueError):dt.money_text(value)
 def test_handler_privacy_exact_one_direct_and_http(self):
  class BadPolicy:
   def inspect(self,p):raise WorkerError(SECRET)
  with tempfile.TemporaryDirectory() as td:
   g,b=base.setup(td,policy=BadPolicy())
   try:
    with self.assertRaises(urllib.error.HTTPError) as cm:base.http(g,'/before',base.payload())
    self.assertEqual(cm.exception.code,403)
    self.assertEqual(cm.exception.read(),b'{"error":"ADMISSION_DENIED"}')
    d=base.one(td);self.assertEqual(d['reason_code'],'UNCLASSIFIED')
    self.assertEqual(g.denied,1);self.assertEqual(g.denials,['UNCLASSIFIED']);self.assertEqual(b.calls,[])
    self.assertNotIn(SECRET,json.dumps(g.denials));self.assertNotIn(g.token,json.dumps(g.denials))
   finally:g.close(clean=True)
 def test_handler_invalid_json_and_auth_no_body_evidence(self):
  with tempfile.TemporaryDirectory() as td:
   g,b=base.setup(td)
   try:
    for auth,raw in [(True,b'{"private":"'+SECRET.encode()+b'"'),(False,json.dumps(base.payload()).encode())]:
     headers={'Authorization':'Bearer '+g.token} if auth else {}
     req=urllib.request.Request(g.url+'/before',data=raw,headers=headers)
     with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req,timeout=5)
     self.assertEqual(cm.exception.code,403)
    files=base.diagnostics(td);self.assertEqual(len(files),2)
    for p in files:
     text=p.read_text(encoding='utf-8');r=json.loads(text);self.assertNotIn(SECRET,text);self.assertNotIn(g.token,text);self.assertIsNone(r['body_sha256']);self.assertIsNone(r['body_utf8_bytes'])
    self.assertEqual(b.calls,[]);self.assertEqual(g.denied,2)
   finally:g.close(clean=True)
 def test_foreign_exception_class_name_and_str_never_saved(self):
  def poison_str(self):raise AssertionError('foreign exception str invoked')
  Foreign=type(SECRET,(Exception,),{'code':'SESSION_BUDGET_EXHAUSTED','__str__':poison_str})
  with tempfile.TemporaryDirectory() as td:
   g,b=base.setup(td,budget=base.FakeBudget(Foreign(SECRET)))
   try:
    with self.assertRaises(urllib.error.HTTPError) as cm:base.http(g,'/before',base.payload())
    self.assertEqual(cm.exception.code,403);self.assertEqual(g.denials,['UNCLASSIFIED'])
    self.assertEqual(base.one(td)['reason_code'],'UNCLASSIFIED');self.assertIsNone(g.inflight)
    self.assertEqual([c[0] for c in b.calls],['before_attempt'])
   finally:g.close(clean=True)
 def test_diagnostic_serializer_failure_still_http_denied(self):
  with tempfile.TemporaryDirectory() as td:
   g,b=base.setup(td)
   try:
    with mock.patch.object(dt,'build_record',side_effect=ValueError(SECRET)):
     with self.assertRaises(urllib.error.HTTPError) as cm:base.http(g,'/before',base.mutate('model','wrong'))
     self.assertEqual(cm.exception.code,403)
    self.assertEqual(b.calls,[]);self.assertIsNone(g.inflight);self.assertEqual(base.diagnostics(td),[])
    self.assertEqual(g.denials,['MODEL_PIN_MISMATCH'])
   finally:g.close(clean=True)
 def test_diagnostic_io_failure_still_single_denial_no_extra_rpc(self):
  with tempfile.TemporaryDirectory() as td:
   g,b=base.setup(td);original=gate.write_json
   def fail(path,*a,**kw):
    if Path(path).name.startswith('denied-'):raise OSError(SECRET)
    return original(path,*a,**kw)
   try:
    with mock.patch.object(gate,'write_json',side_effect=fail):
     with self.assertRaises(urllib.error.HTTPError) as cm:base.http(g,'/before',base.mutate('model','wrong'))
     self.assertEqual(cm.exception.code,403)
    self.assertEqual(b.calls,[]);self.assertIsNone(g.inflight);self.assertEqual(g.denied,1);self.assertEqual(g.denials,['MODEL_PIN_MISMATCH'])
   finally:g.close(clean=True)
