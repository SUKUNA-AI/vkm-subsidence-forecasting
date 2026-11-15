"""Offline image-bound contracts; synthetic proofs do not approve a live host."""
import base64
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from image_admission import ImageAdmission,dimensions,RULES
from request_gate import WirePolicy
from vkm_worker_core import WorkerError

def png(w=56,h=28):
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\0\0\0'*w)*h))+chunk(b'IEND',b'')

class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        proof=self.root/'synthetic-proof.json';proof.write_text('LOCAL_UNIT_FIXTURE_NOT_MAIN_APPROVAL')
        self.proof={'path':str(proof),'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}
        self.data=png();self.mime='image/png'
        self.profile={'model_id':'anthropic/claude-opus-5.5','provider_only':['google-vertex/global'],'reasoning_effort':'high','max_output_tokens':64}
        self.endpoint={'context_length':1000000,'max_completion_tokens':128000,'pricing':{'prompt':'0.000004','completion':'0.00002','input_cache_write_1h':'0.000008'}}
    def tearDown(self):self.temp.cleanup()
    def policy(self,**updates):
        p={'schema':'vkm.opencode.image-admission/1','status':'APPROVED_BY_MAIN_CODEX','task_id':'LOCAL-UNIT-IMAGE','model_id':self.profile['model_id'],'provider_only':self.profile['provider_only'],'rule':RULES[self.profile['model_id']][1],
           'sources':[{'path':'source.png','sha256':hashlib.sha256(self.data).hexdigest(),'mime':self.mime,'width':56,'height':28}],
           'proofs':[self.proof],'decoder':{'version':'UNIT_FIXTURE_NO_DECODER_STARTED','files':[self.proof]}}
        p.update(updates);return p
    def body(self):
        return {'model':self.profile['model_id'],'provider':{'only':self.profile['provider_only'],'allow_fallbacks':False,'require_parameters':True},'reasoning':{'enabled':True,'effort':'high','exclude':True},'max_completion_tokens':64,
                'messages':[{'role':'user','content':[{'type':'text','text':'Synthetic image'},self.part()]}]}
    def part(self):return {'type':'image_url','image_url':{'url':'data:'+self.mime+';base64,'+base64.b64encode(self.data).decode()}}
    def inspect(self,body=None,policy=None):
        body=body or self.body();im=ImageAdmission(policy or self.policy(),self.profile)
        payload={'body_text':json.dumps(body),'kind':'primary','url':'https://openrouter.ai/api/v1/chat/completions'}
        return WirePolicy(self.profile,self.endpoint,image_policy=im).inspect(payload)
    def test_png_header_and_full_sha(self):
        self.assertEqual(dimensions(self.data,self.mime),(56,28))
        pins,cost,e=self.inspect();self.assertEqual(e['images']['image_count'],1)
        self.assertEqual(e['images']['images'][0]['visual_tokens_upper'],4784)
        self.assertEqual(e['input_rate_bound'],'0.000008');self.assertGreater(cost,0)
        self.assertEqual(pins['reasoning'],{'enabled':True,'effort':'high','exclude':True})
    def test_tiny_image_still_reserves_native_max_and_overhead(self):
        self.data=png(1,1);p=self.policy();p['sources'][0].update(width=1,height=1)
        e=self.inspect(policy=p)[2]['images']['images'][0]
        self.assertEqual(e['visual_tokens_upper'],4784);self.assertEqual(e['protocol_guard_tokens'],256)
    def test_hydrogen_uses_max_resolution_allocation_not_pixels(self):
        self.profile.update(model_id='google/gemini-4-hydrogen',provider_only=['google-ai-studio/flex'])
        e=self.inspect()[2]['images']['images'][0];self.assertEqual(e['visual_tokens_upper'],2240)
    def test_repeated_images_count_every_occurrence(self):
        b=self.body();b['messages'][0]['content'].append(self.part());e=self.inspect(b)[2]
        self.assertEqual(e['images']['image_count'],2)
        self.assertEqual(len(e['images']['images']),2)
    def test_flat_image_tariff_is_added(self):
        before=self.inspect()[1];self.endpoint['pricing']['image']='0.01';after=self.inspect()[1]
        self.assertEqual(after-before,Decimal('0.0125'))
    def test_text_only_guard_remains_closed(self):
        with self.assertRaises(WorkerError):
            WirePolicy(self.profile,self.endpoint).inspect({'body_text':json.dumps(self.body()),'kind':'primary','url':'https://openrouter.ai/api/v1/chat/completions'})
    def test_wrong_sha_denied(self):
        self.data=png(57,28)
        with self.assertRaises(WorkerError):self.inspect(policy=self.policy(sources=[{'path':'source.png','sha256':'a'*64,'mime':self.mime,'width':57,'height':28}]))
    def test_metadata_rebind_denied(self):
        for key,value in [('width',1),('height',1),('mime','image/jpeg')]:
            p=self.policy();p['sources'][0][key]=value
            with self.subTest(key=key),self.assertRaises(WorkerError):self.inspect(policy=p)
    def test_remote_uri_and_options_denied(self):
        for value in [{'url':'https://example.invalid/p.png'},{'url':self.part()['image_url']['url'],'detail':'low'},{'url':self.part()['image_url']['url'],'unknown':True}]:
            b=self.body();b['messages'][0]['content'][1]['image_url']=value
            with self.subTest(value=set(value)),self.assertRaises(WorkerError):self.inspect(b)
    def test_malformed_base64_and_crc_denied(self):
        b=self.body();b['messages'][0]['content'][1]['image_url']['url']='data:image/png;base64,???'
        with self.assertRaises(WorkerError):self.inspect(b)
        data=bytearray(self.data);data[29]^=1
        with self.assertRaises(WorkerError):dimensions(bytes(data),'image/png')
    def test_pending_approval_and_task_mismatch_denied(self):
        with self.assertRaises(WorkerError):self.inspect(policy=self.policy(status='READY_FOR_REVIEW'))
        with self.assertRaises(WorkerError):ImageAdmission(self.policy(),self.profile,task_id='OTHER-TASK')
    def test_model_provider_rule_rebind_denied(self):
        for key,value in [('model_id','other/model'),('provider_only',['other/provider']),('rule','cheap-pixels')]:
            with self.subTest(key=key),self.assertRaises(WorkerError):self.inspect(policy=self.policy(**{key:value}))
    def test_source_path_escape_and_unpriced_types_denied(self):
        p=self.policy();p['sources'][0]['path']='../outside.png'
        with self.assertRaises(WorkerError):self.inspect(policy=p)
        for part in [{'type':'input_audio','input_audio':{}},{'type':'file','file':{}},{'type':'image_url','image_url':{'url':'data:image/gif;base64,AAAA'}}]:
            b=self.body();b['messages'][0]['content'][1]=part
            with self.subTest(part=part['type']),self.assertRaises(WorkerError):self.inspect(b)
    def test_context_limit_and_output_generation_denied(self):
        self.endpoint['context_length']=100
        with self.assertRaises(WorkerError):self.inspect()
        self.endpoint['context_length']=1000000
        for key,value in [('modalities',['text','image']),('image_config',{}),('web_search_options',{})]:
            b=self.body();b[key]=value
            with self.subTest(key=key),self.assertRaises(WorkerError):self.inspect(b)

if __name__=='__main__':unittest.main()
