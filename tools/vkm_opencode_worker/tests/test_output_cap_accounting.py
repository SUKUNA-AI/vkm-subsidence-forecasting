"""Synthetic offline fixtures; never an approval of a paid host."""
import base64,copy,hashlib,json,sys,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import request_gate as g
import vkm_worker_core as c
from image_admission import RULES
class OutputCapTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.r=Path(self.tmp.name);self.proof=self.r/'synthetic-proof.json';self.proof.write_text('SYNTHETIC_INCLUSIVE_CAP_DERIVATION_FIXTURE')
  self.patch=patch.object(g,'INCLUSIVE_CAP_PROOF_SHA256',c.sha256_file(self.proof));self.patch.start()
  self.p={'model_id':'google/gemini-4-hydrogen','provider_only':['google-ai-studio/flex'],'reasoning_effort':'high','max_output_tokens':64}
  self.e={'tag':'google-ai-studio/flex','context_length':1000000,'max_completion_tokens':100000,'pricing':{'prompt':'.00000075','completion':'.00000375','internal_reasoning':'.00000375','input_cache_write_1h':'.000008'}}
  self.policy={'schema':'vkm.opencode.output-cap-accounting/1','status':'APPROVED_BY_MAIN_CODEX','model_id':self.p['model_id'],'provider_only':self.p['provider_only'],'inclusive_output_cap':True,'cap_fields':['max_tokens','max_completion_tokens'],'pricing_condition':'FRESH_COMPLETION_EQUALS_INTERNAL_REASONING','proof':{'path':str(self.proof),'sha256':c.sha256_file(self.proof)}}
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def rec(self,policy=None):
  p=self.r/'policy.json';p.write_text(json.dumps(policy or self.policy));return {'path':str(p),'sha256':c.sha256_file(p)}
 def payload(self):return {'kind':'primary','url':'https://openrouter.ai/api/v1/chat/completions','body_text':json.dumps({'model':self.p['model_id'],'provider':{'only':self.p['provider_only'],'allow_fallbacks':False,'require_parameters':True},'reasoning':{'enabled':True,'effort':'high','exclude':True},'max_completion_tokens':64,'messages':[{'role':'user','content':'SYNTHETIC ONLY'}]})}
 def test_no_optin_keeps_sum(self):
  e=g.WirePolicy(self.p,self.e).inspect(self.payload())[2];self.assertEqual(Decimal(e['output_rate_bound']),Decimal('.0000075'));self.assertEqual(e['output_rate_accounting']['method'],'SUM_UNVERIFIED_OUTPUT_CLASS_QUOTAS')
 def test_optin_max_preserves_cachemax_margin_cap_high(self):
  pins,cost,e=g.WirePolicy(self.p,self.e,output_rate_policy=self.rec()).inspect(self.payload());self.assertEqual(e['output_rate_bound'],'0.00000375');self.assertEqual(e['input_rate_bound'],'0.000008');self.assertEqual(e['max_tokens'],64);self.assertEqual(pins['reasoning']['effort'],'high');self.assertEqual(cost,(Decimal(e['input_bound'])*Decimal('.000008')+Decimal(64)*Decimal('.00000375'))*Decimal('1.25'))
 def test_all_nonnegative_partitions(self):
  rate=Decimal(g.WirePolicy(self.p,self.e,output_rate_policy=self.rec()).inspect(self.payload())[2]['output_rate_bound'])
  for visible in range(65):
   for reasoning in range(65-visible):self.assertLessEqual(Decimal(visible)*Decimal('.00000375')+Decimal(reasoning)*Decimal('.00000375'),Decimal(64)*rate)
  self.assertGreater(Decimal(128)*rate,Decimal(64)*rate) # joint-cap hypothesis is necessary
 def test_malformed_optins_fail_closed(self):
  cases=[{'status':'READY_FOR_REVIEW'},{'inclusive_output_cap':1},{'cap_fields':['max_output_tokens']},{'pricing_condition':'IGNORE_REASONING'},{'proof':{'path':str(self.proof),'sha256':'1'*64}},{'extra':True}]
  for update in cases:
   with self.subTest(fields=list(update)),self.assertRaises(c.WorkerError):g.WirePolicy(self.p,self.e,output_rate_policy=self.rec({**self.policy,**update}))
 def test_other_model_provider_or_endpoint_never_optin(self):
  for model,prov,tag in [('other/model',['google-ai-studio/flex'],'google-ai-studio/flex'),(self.p['model_id'],['google-vertex/global'],'google-ai-studio/flex'),(self.p['model_id'],self.p['provider_only'],'other/tag')]:
   with self.subTest(model=model,tag=tag),self.assertRaises(c.WorkerError):g.WirePolicy({**self.p,'model_id':model,'provider_only':prov},{**self.e,'tag':tag},output_rate_policy=self.rec())
 def test_unequal_missing_and_invalid_rates_fail_closed(self):
  for pricing in [{**self.e['pricing'],'internal_reasoning':'.00000376'},{k:v for k,v in self.e['pricing'].items() if k!='internal_reasoning'},{**self.e['pricing'],'completion':'NaN'},{**self.e['pricing'],'internal_reasoning':True}]:
   with self.subTest(pricing=pricing),self.assertRaises(c.WorkerError):g.WirePolicy(self.p,{**self.e,'pricing':pricing},output_rate_policy=self.rec()).inspect(self.payload())
 def test_unproved_cap_field_denied(self):
  x=self.payload();body=json.loads(x['body_text']);body['max_output_tokens']=body.pop('max_completion_tokens');x['body_text']=json.dumps(body)
  with self.assertRaises(c.WorkerError):g.WirePolicy(self.p,self.e,output_rate_policy=self.rec()).inspect(x)
 def test_proof_bytes_changed_denied(self):
  rec=self.rec();self.proof.write_text('changed')
  with self.assertRaises(c.WorkerError):g.WirePolicy(self.p,self.e,output_rate_policy=rec)
class NativeMediaConfigTests(unittest.TestCase):
 def valid_host(self,root,profile,selected):
  return {'schema':'vkm.opencode.host/2','platform':'windows-native','opencode':{'path':str(root/'opencode.exe'),'sha256':'1'*64,'version':'2.2.7'},'run_root':str(root/'runs'),'state_root':str(root/'state'),'protected_roots':[],'approved_plugins':[],'environment_names':[],'shell_commands':{},'profiles':{profile:selected},'mcp':{},'budget':{'authorized_total_usd':5,'baseline_usage_usd':0,'authorization_id':'offline-unit','key_fingerprint':'0'*64}}
 def config(self):return {'permissions':[{'action':'*','resource':'*','effect':'deny'}],'policies':[{'action':'tool.use','resource':'*','effect':'deny'}]}
 def test_text_only_has_no_media_side_effect(self):
  tree=Path('/tmp/vkm-media-test').resolve();host=self.valid_host(tree,'deepseek',{'model_id':'fixture/model','provider_only':['fixture/provider'],'max_output_tokens':512,'reasoning_effort':'high'})
  job={'profile':'deepseek','scenario':'code_investigation','read_paths':['main.py'],'edit_paths':[],'mcp':[],'max_steps':4,'mode':'review'}
  self.assertNotIn('media',c.build_config(job,host,tree,tree/'run'))
 def test_media_exact_shape_and_no_auto_transform(self):
  value=self.config();value['media']={'image':{'auto_resize':False,'max_width':3465,'max_height':2363,'max_base64_bytes':3000000}};c.validate_generated_config(value)
  for key,bad in [('auto_resize',True),('max_width',True),('max_height',0),('max_base64_bytes','3000000'),('extra',1)]:
   v=copy.deepcopy(value);v['media']['image'][key]=bad
   with self.subTest(key=key),self.assertRaises(c.WorkerError):c.validate_generated_config(v)
 def test_image_limits_derive_only_sha_bound_sources(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);source=r/'approved.png';source.write_bytes(b'SYNTHETIC LIMIT BYTECOUNT ONLY; NOT DECODER TEST')
   proof=r/'proof';proof.write_text('UNIT NOT LIVE');record={'path':str(proof),'sha256':c.sha256_file(proof)}
   p={'model_id':'google/gemini-4-hydrogen','provider_only':['google-ai-studio/flex'],'reasoning_effort':'high','max_output_tokens':512}
   policy={'schema':'vkm.opencode.image-admission/1','status':'APPROVED_BY_MAIN_CODEX','task_id':'UNIT-MEDIA','model_id':p['model_id'],'provider_only':p['provider_only'],'rule':RULES[p['model_id']][1],'sources':[{'path':'approved.png','sha256':c.sha256_file(source),'mime':'image/png','width':3465,'height':2363}],'proofs':[record],'decoder':{'version':'UNIT_NO_DECODER_STARTED','files':[record]}}
   f=r/'policy.json';f.write_text(json.dumps(policy));host=self.valid_host(r,'hydrogen',p);host['image_admission']={'hydrogen':{'path':str(f),'sha256':c.sha256_file(f)}}
   job={'task_id':'UNIT-MEDIA','profile':'hydrogen','scenario':'code_investigation','read_paths':['approved.png'],'edit_paths':[],'mcp':[],'max_steps':4,'mode':'review'}
   cfg=c.build_config(job,host,r,r/'run');self.assertEqual(cfg['media']['image'],{'auto_resize':False,'max_width':3465,'max_height':2363,'max_base64_bytes':4*((source.stat().st_size+2)//3)})
   planned=r/'not-created-worktree';self.assertFalse(planned.exists())
   self.assertEqual(c.build_config(job,host,planned,r/'run',source_root=r)['media'],cfg['media'])
   self.assertFalse(planned.exists())
   source.write_bytes(b'changed')
   with self.assertRaises(c.WorkerError):c.build_config(job,host,r,r/'run')
if __name__=='__main__':unittest.main()
