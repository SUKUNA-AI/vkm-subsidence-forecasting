"""Synthetic HTTP diagnostic contracts; no real sockets, keys or live ledger."""
import importlib.util
import json
from pathlib import Path
import types

import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('delegate_http_evidence',ROOT/'.agents/skills/openrouter-delegate/scripts/openrouter_call.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)

def fake_http(monkeypatch,status=400,body=None,headers=None,read_error=False):
    if body is None:body={'error':{'code':400,'message':'Unsupported parameter reasoning_effort'},'id':'gen-synthetic-error','request_id':'body-request-1'}
    raw=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode()
    state={'requests':[],'read_limits':[],'closed':False}
    class Response:
        def __init__(self):self.status=status
        def getheader(self,name):return {k.lower():v for k,v in (headers or {}).items()}.get(name.lower())
        def read(self,limit):
            state['read_limits'].append(limit)
            if read_error:raise OSError('synthetic body read failure; do not persist its text')
            return raw[:limit]
    class Connection:
        def __init__(self,*args,**kwargs):self.sock=types.SimpleNamespace(settimeout=lambda _:None)
        def connect(self):pass
        def request(self,method,path,**kwargs):state['requests'].append((method,path))
        def getresponse(self):return Response()
        def close(self):state['closed']=True
    monkeypatch.setattr(d.http.client,'HTTPSConnection',Connection)
    return state,raw

def caught(monkeypatch,**kwargs):
    key=kwargs.pop('key','synthetic-session-key-no-format')
    state,raw=fake_http(monkeypatch,**kwargs)
    with pytest.raises(d.DelegateError) as error:d.Transport(key).request('POST','chat/completions',{'model':'synthetic'})
    return error.value,state,raw

def test_http400_retains_safe_cause_status_and_identifiers(monkeypatch):
    error,state,raw=caught(monkeypatch,headers={'X-Request-ID':'header-request-1','CF-Ray':'unit-ray-VIE','Content-Type':'application/json'})
    assert error.code=='HTTP_400' and str(error)=='HTTP_400'
    evidence=error.http_error_evidence
    assert evidence['http_status']==400 and evidence['error_code']==400
    assert evidence['error_message']=='Unsupported parameter reasoning_effort'
    assert evidence['request_id']=='body-request-1' and evidence['generation_id']=='gen-synthetic-error'
    assert evidence['headers']['x-request-id']=='header-request-1' and evidence['headers']['cf-ray']=='unit-ray-VIE'
    assert state['closed'] and state['requests']==[('POST','/api/v1/chat/completions')]
    assert 'raw' not in evidence and evidence['body_capture']['raw_body_persisted'] is False

@pytest.mark.parametrize('message',[
    'synthetic-session-key-no-format','sk-or-v1-synthetic-never-real',
    'password=synthetic-value','postgresql://user:synthetic-value@localhost/db',
    'AKIAabcdefghijklmnop',r'{"pass\u0077ord":["opaque-synthetic-value"]}',
    '{"reasoning_details":["PRIVATE_COT_MARKER"]}','<think>PRIVATE_COT_MARKER</think>',
    r'{"reason\u0069ng_details":["PRIVATE_COT_MARKER"]}',
    r'"{\"reason\\u0069ng\":[\"PRIVATE_COT_MARKER\"]}"',
    'reasoning: PRIVATE_COT_MARKER','<analysis class="private">PRIVATE_COT_MARKER</analysis>'
])
def test_secret_or_reasoning_in_error_message_never_persists(monkeypatch,message):
    body={'error':{'code':400,'message':message,'metadata':{'raw':'RAW_PRIVATE_BODY','reasoning_content':'PRIVATE_COT_MARKER'}},'choices':[{'message':{'content':'PRIVATE_CONTENT'}}]}
    error,_,_=caught(monkeypatch,body=body)
    evidence=error.http_error_evidence;serialized=json.dumps(evidence)
    assert evidence['error_message'] is None and evidence['message_omitted'] is True
    assert message not in serialized and 'PRIVATE_COT_MARKER' not in serialized
    assert 'RAW_PRIVATE_BODY' not in serialized and 'PRIVATE_CONTENT' not in serialized

def test_header_allowlist_and_secret_values(monkeypatch):
    headers={'Authorization':'Bearer synthetic-session-key-no-format','Set-Cookie':'PRIVATE_COOKIE','X-API-Key':'PRIVATE_KEY',
             'X-Request-ID':'sk-or-v1-synthetic-never-real','X-OpenRouter-Request-ID':'request-safe','Retry-After':'3'}
    error,_,_=caught(monkeypatch,headers=headers);value=error.http_error_evidence
    assert value['headers']=={'x-openrouter-request-id':'request-safe','retry-after':'3'}
    assert 'PRIVATE_' not in json.dumps(value) and 'sk-or-' not in json.dumps(value)

@pytest.mark.parametrize('body',[b'',b'<html>not json</html>',b'{"error":',b'\xff\xfe',b'['*2000+b']'*2000])
def test_malformed_error_body_preserves_http_status_headers(monkeypatch,body):
    error,_,_=caught(monkeypatch,status=503,body=body,headers={'X-Request-ID':'req-malformed'})
    assert error.code=='HTTP_503'
    value=error.http_error_evidence
    assert value['http_status']==503 and value['headers']['x-request-id']=='req-malformed'
    assert value['error_message'] is None and value['body_capture']['raw_body_persisted'] is False

def test_large_error_read_is_bounded_and_does_not_claim_full_body(monkeypatch):
    error,state,raw=caught(monkeypatch,status=413,body=b'x'*(200000),headers={'X-Request-ID':'req-large'})
    value=error.http_error_evidence
    assert state['read_limits']==[d.MAX_HTTP_ERROR_BODY_BYTES+1]
    assert value['body_capture']['truncated'] is True
    assert value['body_capture']['observed_prefix_bytes']==d.MAX_HTTP_ERROR_BODY_BYTES
    assert len(json.dumps(value).encode())<4096

def test_status_headers_retained_when_body_read_fails(monkeypatch):
    error,_,_=caught(monkeypatch,status=502,headers={'X-Request-ID':'req-read-fail'},read_error=True)
    value=error.http_error_evidence
    assert error.code=='HTTP_502' and value['headers']['x-request-id']=='req-read-fail'
    assert value['body_capture']['read_failed'] is True and 'synthetic body read failure' not in json.dumps(value)

def test_error_message_utf8_cap(monkeypatch):
    error,_,_=caught(monkeypatch,body={'error':{'message':'Ошибка '*1000,'code':'provider_error'}})
    value=error.http_error_evidence
    assert len(value['error_message'].encode('utf-8'))<=d.MAX_HTTP_ERROR_MESSAGE_BYTES
    assert value['message_truncated'] is True

def test_api_error_in_http200_retains_safe_evidence(monkeypatch):
    error,_,_=caught(monkeypatch,status=200,body={'error':{'code':'upstream_reject','message':'Provider rejected input'},'request_id':'req-api200'})
    assert error.code=='API_ERROR'
    assert error.http_error_evidence['http_status']==200 and error.http_error_evidence['error_code']=='upstream_reject'

def test_success_contract_and_model_content_unchanged(monkeypatch):
    body={'id':'gen-success','data':{'value':2}}
    state,_=fake_http(monkeypatch,status=200,body=body)
    assert d.Transport('synthetic-session-key').request('GET','unit')==body
    assert state['closed']

def run_error_receipt(tmp_path,monkeypatch,*,body,headers=None,key='sk-or-synthetic-http-test'):
    state,_=fake_http(monkeypatch,body=body,headers=headers)
    fixture=importlib.util.spec_from_file_location('http_metadata_fixture',ROOT/'tests/env_setup/test_openrouter_delegate_v2.py');module=importlib.util.module_from_spec(fixture);fixture.loader.exec_module(module)
    rows=module.metadata(); model=d.PROFILES['cheap_code'][0]
    class Offline(d.ReplayTransport):
        def request(self,method,path,body=None):
            if path=='models/'+model+'/endpoints':return next(row['metadata'] for row in rows if row['model']==model)
            if path=='models':return {'data':[row['model_capabilities']['model'] for row in rows]}
            if path=='key':return {'data':{'limit':40,'limit_remaining':40}}
            if method=='POST':return d.Transport(key).request(method,path,body)
            raise AssertionError('unexpected extra request')
    monkeypatch.setenv('OPENROUTER_API_KEY',key)
    job={'task_id':'synthetic-http-ledger','synthetic':True,'profile':'cheap_code','task_kind':'code_implementation','prompt':'Return synthetic code.','max_tokens':512}
    ledger=tmp_path/'scratch-ledger.json'
    result=d.run(job,rows,tmp_path/'run',ledger=ledger,budget='40',transport=Offline([]))
    assert result['verification']=='HTTP_400' and result['http_status']==400
    assert result['execution_cost_status']=='UNKNOWN' and result['cost_usd'] is None
    value=d.load_json(ledger)
    assert value['blocked'] is True and len(value['reservations'])==1 and value['spent_usd']=='0'
    assert len(state['requests'])==1 and 'http_error_evidence' in d.load_json(tmp_path/'run/receipt.json')
    return result,d.load_json(tmp_path/'run/receipt.json')


def test_zero_error_usage_does_not_settle_unknown_or_retry(tmp_path,monkeypatch):
    result,_=run_error_receipt(tmp_path,monkeypatch,body={'error':{'code':400,'message':'Provider rejected input'},'id':'gen-unit-zero-claim','usage':{'cost':0}})
    assert result['http_error_evidence']['generation_id']=='gen-unit-zero-claim'


@pytest.mark.parametrize('case',['prefix_key','prefix_nested_key','prefix_COT','allowed_header_key'])
def test_prefixed_escaped_diagnostic_never_persists_in_receipt(tmp_path,monkeypatch,case):
    key='sk-or-v1-SYNTHETIC_AUDIT_ONLY_0001';escaped=key.replace('-',r'\u002d');marker='SYNTHETIC_PRIVATE_COT_MARKER'
    body={'error':{'message':'Rejected'}};headers={}
    if case=='prefix_key':body['error']['message']='Provider diagnostic: '+escaped
    elif case=='prefix_nested_key':body['error']['message']='Provider diagnostic: '+json.dumps({'authorization':key}).replace('authorization',r'authoriz\u0061tion').replace(key,escaped)
    elif case=='prefix_COT':body['error']['message']=r'Provider diagnostic: {"reason\u0069ng_details":["'+marker+r'"]}'
    else:headers={'Content-Type':'application/json; credential='+escaped}
    result,persisted=run_error_receipt(tmp_path,monkeypatch,body=body,headers=headers,key=key)
    text=json.dumps(persisted,ensure_ascii=False)
    assert result==persisted
    assert key not in text and escaped.replace('\\','\\\\') not in text and marker not in text
    evidence=result['http_error_evidence']
    if case=='allowed_header_key':assert 'content-type' not in evidence['headers']
    else:assert evidence['error_message'] is None and evidence['message_omitted'] is True
