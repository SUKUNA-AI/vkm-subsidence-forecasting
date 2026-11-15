"""Explicit Kimi paid-API stop; no subscription fallback or real HTTP."""
import importlib.util
import os
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('delegate_kimi_policy',ROOT/'.agents/skills/openrouter-delegate/scripts/openrouter_call.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)

def job(profile='engineering_research'):
    return {'task_id':'synthetic-subscription-guard','synthetic':True,'profile':profile,'task_kind':'code_implementation','prompt':'Return a synthetic function candidate.','max_tokens':512}

def metadata():
    model,tag,provider,_=d.PROFILES['engineering_research'];params=['reasoning','reasoning_effort','max_tokens','response_format','structured_outputs']
    return [{'model':model,'http_status':200,'checked_at':d.now(),'metadata':{'data':{'id':model,'architecture':{'input_modalities':['text']},'endpoints':[
      {'model_id':model,'provider_name':provider,'tag':tag,'quantization':'mxfp4','status':0,'context_length':1000000,'max_completion_tokens':100000,
       'supported_parameters':params,'pricing':{'prompt':'0.0000001','completion':'0.0000005'}}]}},
      'model_capabilities':{'checked_at':d.now(),'catalogue_sha256':'a'*64,'model':{'id':model,'canonical_slug':d.CANONICAL_MODEL_PINS[model],
        'reasoning':{'supported_efforts':['high','low','none','max'],'default_effort':'max','mandatory':False},'supported_parameters':params}}}]

@pytest.mark.parametrize('transport_kind',['none','trap'])
def test_kimi_paid_stop_precedes_credentials_prepare_and_network(tmp_path,monkeypatch,transport_kind):
    original=os.environ
    class NoCredentialRead(dict):
        def get(self,key,default=None):
            if key=='OPENROUTER_API_KEY':raise AssertionError('credential read before policy stop')
            return original.get(key,default)
    monkeypatch.setattr(d.os,'environ',NoCredentialRead())
    class Trap(d.Transport):
        def request(self,*args,**kwargs):raise AssertionError('network before policy stop')
    def prepare(*args,**kwargs):raise AssertionError('prepare before policy stop')
    monkeypatch.setattr(d,'prepare',prepare)
    transport=None if transport_kind=='none' else Trap()
    with pytest.raises(d.DelegateError,match='KIMI_SUBSCRIPTION_ONLY_API_DISABLED'):
        d.run(job(),None,tmp_path/'never',ledger=tmp_path/'ledger.json',budget='40',transport=transport)
    assert not (tmp_path/'never').exists() and not (tmp_path/'ledger.json').exists()

def test_kimi_offline_check_mock_and_metadata_get_remain_available(tmp_path):
    request,_,_=d.prepare(job(),metadata())
    assert request['model']=='moonshotai/kimi-k3.1-pro' and request['provider']['only']==['decart/mxfp4']
    assert request['reasoning']=={'enabled':True,'effort':'high'}
    receipt=d.run(job(),metadata(),tmp_path/'mock',mock=True)
    assert receipt['verification']=='MOCK_PASS' and receipt['mode']=='mock'
    class MetadataTransport:
        def __init__(self):self.calls=[]
        def request(self,method,path,body=None):
            self.calls.append((method,path));assert method=='GET'
            return metadata()[0]['metadata']
    transport=MetadataTransport();rows=d.discover(transport,['engineering_research'])
    assert len(rows)==1 and transport.calls==[('GET','models/moonshotai/kimi-k3.1-pro/endpoints')]

@pytest.mark.parametrize('profile',['vision_documents','cheap_code','critical_review'])
def test_other_profiles_pass_paid_policy_gate(profile,tmp_path,monkeypatch):
    class AfterGate(Exception):pass
    def prepare(*args,**kwargs):raise AfterGate()
    monkeypatch.setattr(d,'prepare',prepare)
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False)
    with pytest.raises(AfterGate):d.run(job(profile),None,tmp_path/'never')
    assert not (tmp_path/'never').exists()
