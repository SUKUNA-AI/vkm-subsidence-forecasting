"""Portable actual SharedSession + pinned PUBLIC Delegate tests on tempfile ledgers.

No host paths, real ledger, credentials, provider HTTP, DB or OpenCode process.
The public backend20 compatibility is tested here; current40/C3/native binding
has a separate native qualification and is not inferred from these fixtures.
"""
import hashlib,json,sys,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from shared_budget import SharedSession
from request_gate import HookGate
class Noop:
 def shutdown(self):pass
 def server_close(self):pass
 def join(self,timeout):pass
class DirectBudget:
 def __init__(self,session):self.session=session
 def call(self,method,*args,**kwargs):return getattr(self.session,method)(*args,**kwargs)
class SharedBudgetPortableTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='vkm-ledger-cpu-');self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  public=Path(__file__).resolve().parents[3];client=public/'.agents/skills/openrouter-delegate/scripts/openrouter_call.py';policy=client.parents[1]/'references/DELEGATION_POLICY_V2.json'
  self.assertTrue(client.is_file() and policy.is_file(),'PUBLIC Delegate ledger backend is required')
  sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
  self.record={'client_path':str(client),'client_sha256':sha(client),'policy_path':str(policy),'policy_sha256':sha(policy),'requested_reasoning':{'enabled':True,'effort':'high','exclude':True}}
  key=hashlib.sha256(b'SYNTHETIC_UNIT_LEDGER_NOT_API_KEY').hexdigest();self.ledger=self.root/'synthetic-ledger.json'
  self.ledger.write_text(json.dumps({'budget_usd':'20','key_id':key,'spent_usd':'0','reservations':{},'reservation_leases':{},'reservation_count':0,'blocked':False,'owner_local_cap_only':False}),encoding='utf-8')
  self.host={'ledger_path':str(self.ledger),'total_budget_usd':'20','key_fingerprint':key,'key_remaining_usd':'20','diagnostic_reserve_usd':'.025'}
  network=patch('socket.socket.connect',side_effect=AssertionError('portable ledger fixture must not connect'));network.start();self.addCleanup(network.stop)
  self.session=SharedSession.begin(self.host,self.record,'cheap_code','.10','SYNTHETIC-CPU-SESSION',30,self.root/'session.json');self.addCleanup(self.cleanup_session)
  model,provider,_,_=self.session.backend.profile_spec('cheap_code')
  self.body=json.dumps({'model':model,'provider':{'only':[provider],'allow_fallbacks':False},'reasoning':self.record['requested_reasoning'],'max_completion_tokens':32}).encode()
  self.pins={'model':model,'provider':{'only':[provider],'allow_fallbacks':False},'reasoning':self.record['requested_reasoning'],'request_sha256':hashlib.sha256(self.body).hexdigest(),'kind':'main','retry_count':0}
 def cleanup_session(self):
  if self.session._owned:self.session.abort('SYNTHETIC_TEST_CLEANUP')
 def before(self,name='http-1'):self.session.before_attempt(name,'.05',self.pins,request_body=self.body)
 def known(self,name='http-1',generation='gen-synthetic-cpu-1'):
  self.before(name);self.session.after_attempt(name,generation,'.002','a'*64,evidence_kind='response_usage')
 def gate_close(self,cleanup=True,inflight=None):
  gate=HookGate.__new__(HookGate);gate.server=Noop();gate.thread=Noop();gate.inflight=inflight;gate.budget=DirectBudget(self.session);return gate.close(clean=cleanup)
 def snapshot(self):return json.loads(self.ledger.read_text(encoding='utf-8'))
 def test_failed_job_all_known_cleanup_proof_settles_only_known_cost(self):
  self.known();job={'lifecycle':'EXITED','returncode':7,'tree_cleanup_proven':True}
  result=self.gate_close(cleanup=job['tree_cleanup_proven'] is True);after=self.snapshot()
  self.assertEqual(result['state'],'SETTLED');self.assertEqual(Decimal(after['spent_usd']),Decimal('.002'));self.assertFalse(after['reservations']);self.assertEqual(after['reservation_count'],1);self.assertEqual(job['returncode'],7)
 def test_known_cost_without_cleanup_proof_remains_unknown(self):
  self.known();result=self.gate_close(cleanup=False);after=self.snapshot()
  self.assertEqual(result['state'],'UNKNOWN_HOLD');self.assertTrue(after['blocked']);self.assertTrue(after['reservations']);self.assertEqual(after['spent_usd'],'0')
 def test_actual_inflight_attempt_is_not_forgiven(self):
  self.known();self.before('http-2');result=self.gate_close(cleanup=True,inflight='http-2');after=self.snapshot()
  self.assertEqual(result['state'],'UNKNOWN_HOLD');self.assertTrue(after['reservations']);self.assertTrue(after['blocked'])
 def test_finish_rechecks_possibly_sent_even_if_caller_reports_noinflight(self):
  self.before();result=self.gate_close(cleanup=True,inflight=None)
  self.assertEqual(result['state'],'UNKNOWN_HOLD');self.assertTrue(self.snapshot()['blocked'])
 def test_explicit_noinflight_false_rejects_even_all_known(self):
  self.known();result=self.session.finish(no_inflight=False)
  self.assertEqual(result['state'],'UNKNOWN_HOLD');self.assertTrue(self.snapshot()['reservations'])
 def test_explicit_known_reconcile_preserves_global_block_and_lifetime(self):
  self.before();self.session.abort('SYNTHETIC_UNKNOWN');evidence=[{'attempt_id':'http-1','generation_id':'gen-synthetic-cpu-recovered','cost_usd':'.002','evidence_sha256':'b'*64,'evidence_kind':'generation_record','request_sha256':self.pins['request_sha256']}]
  result=SharedSession.reconcile_pending(self.host,self.record,self.root/'session.json',evidence,True);after=self.snapshot()
  self.assertEqual(result['state'],'SETTLED');self.assertEqual(Decimal(after['spent_usd']),Decimal('.002'));self.assertTrue(after['blocked']);self.assertFalse(after['reservations']);self.assertEqual(after['reservation_count'],1)
if __name__=='__main__':unittest.main()
