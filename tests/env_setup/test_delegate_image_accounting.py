"""In-memory SYNTHETIC approval fixtures only; no root approval or live I/O."""
import base64
import copy
import hashlib
import importlib.util
from datetime import datetime, timezone, timedelta
from pathlib import Path
import io
import pytest
from PIL import Image, PngImagePlugin
import test_openrouter_delegate_v2 as fixtures

d = fixtures.d


def decoder():
    import PIL
    from PIL import Image, PngImagePlugin, JpegImagePlugin
    return {'version':PIL.__version__, 'files':{Path(m.__file__).name:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in [PIL, Image, PngImagePlugin, JpegImagePlugin, Image.core]}}


def proof_for(job, meta, status='APPROVED_BY_MAIN_CODEX'):
    ep = d.select_endpoint(meta,job['profile'])[2]
    return {'schema':'vkm.delegate.image-rule/1','status':status,'task_id':job['task_id'],'input_sha256':d.digest(job),
        'model_id':'google/gemini-4-hydrogen','provider_only':['google-ai-studio/flex'],'rule':'GEMINI4_MAX2240',
        'checked_at':d.now(),'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
        'sources':[{'sha256':hashlib.sha256(base64.b64decode(x['data_base64'])).hexdigest(),'mime':x['mime'],'width':x['width'],'height':x['height'],'bbox':x['bbox']}for x in job['images']],
        'total_pixels':sum(x['width']*x['height'] for x in job['images']), 'decoder':decoder(),
        'primary_docs':[{'url':u,'sha256':'a'*64}for u in ['https://aistudio.google.com/docs/media-resolution','https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-hydrogen']],
        'qualification':{'algorithm_sha256':'7898955b61f16c83b15713d67dc6d95dea4603c407f2cd1cf30c78ea1c2cb08a','native_receipt_sha256':'b'*64},
        'supported_parameters':ep['supported_parameters'].copy()}


@pytest.fixture
def inputs():
    j=fixtures.job('vision_documents','visual_analysis');j['images']=[fixtures.image(710,415),fixtures.image(139,30)]
    j['reasoning_effort']='high';j['max_tokens']=16384
    m=fixtures.metadata();p=proof_for(j,m)
    return j,m,p


def prepare(j,m,p):
    return d.prepare(j,m,image_rule_proof=p,approved_image_rule_sha256=d.digest(p))


def test_bound_keeps_full_wire_and_separates_requested_effective(inputs):
    j,m,p=inputs;r,c,e=prepare(j,m,p)
    assert r['reasoning']=={'effort':'high','enabled':True}
    assert r['max_tokens']==16384
    for wire,raw in zip(r['messages'][0]['content'][1:],j['images']):
        assert wire['image_url']['url'].split(',',1)[1]==raw['data_base64']
    a=e['accounting'];b=a['image_rule']
    assert a['estimated_input_token_bound']==b['text_and_schema_bytes']+4096+2*2496
    assert b['effective_media_resolution']=='UNCONFIRMED_MAX_ALLOCATION_USED'
    assert c < d.Decimal('.30')


@pytest.mark.parametrize('change', ['missing','malformed','unapproved','stale','future','long_expiry','task','input','rule','model','provider','sources','source_sha','source_bool','pixel_underclaim','dimension','bbox','decoder_version','decoder_file','docs','qualification','params','extra'])
def test_proof_fail_closed(inputs,change):
    j,m,p=inputs
    if change=='missing':p=None
    elif change=='malformed':p=[]
    elif change=='unapproved':p['status']='READY_FOR_REVIEW'
    elif change=='stale':p['checked_at']='2020-01-01T00:00:00Z'
    elif change=='future':p['checked_at']=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
    elif change=='long_expiry':p['expires_at']=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    elif change=='task':p['task_id']='changed'
    elif change=='input':p['input_sha256']='0'*64
    elif change=='rule':p['rule']='GEMINI_DEFAULT1120'
    elif change=='model':p['model_id']='other'
    elif change=='provider':p['provider_only']=['google-ai-studio']
    elif change=='sources':p['sources']=p['sources'][:1]
    elif change=='source_sha':p['sources'][0]['sha256']='0'*64
    elif change=='source_bool':p['sources'][0]['bbox'][0]=False
    elif change=='pixel_underclaim':p['total_pixels']-=1
    elif change=='dimension':p['sources'][0]['width']-=1
    elif change=='bbox':p['sources'][0]['bbox'][0]=1
    elif change=='decoder_version':p['decoder']['version']='old'
    elif change=='decoder_file':p['decoder']['files'][next(iter(p['decoder']['files']))]='0'*64
    elif change=='docs':p['primary_docs']=[]
    elif change=='qualification':p['qualification']['algorithm_sha256']='0'*64
    elif change=='params':p['supported_parameters'].append('unknown')
    elif change=='extra':p['claimed_tokens']=1
    with pytest.raises(d.DelegateError):d.prepare(j,m,image_rule_proof=p,approved_image_rule_sha256=d.digest(p))


def test_wrong_approved_sha_and_one_sided_optin(inputs):
    j,m,p=inputs
    for proof,sha in [(p,'0'*64),(p,None),(None,d.digest(p))]:
        with pytest.raises(d.DelegateError):d.prepare(j,m,image_rule_proof=proof,approved_image_rule_sha256=sha)


@pytest.mark.parametrize('change',['profile','endpoint_provider','parameter_new','price_new','price_nan','context_missing','context_short','output_cap','modality'])
def test_endpoint_guards_preserved(inputs,change):
    j,m,p=inputs;ep=d.select_endpoint(m,j['profile'])[2]
    if change=='profile':j['profile']='cheap_code';p['input_sha256']=d.digest(j)
    elif change=='endpoint_provider':ep['provider_name']='other'
    elif change=='parameter_new':ep['supported_parameters'].append('unknown');p['supported_parameters']=ep['supported_parameters'].copy()
    elif change=='price_new':ep['pricing']['new_charge']='1'
    elif change=='price_nan':ep['pricing']['prompt']='NaN'
    elif change=='context_missing':del ep['context_length']
    elif change=='context_short':ep['context_length']=100
    elif change=='output_cap':ep['max_completion_tokens']=100
    elif change=='modality':next(x for x in m if x['model']==p['model_id'])['metadata']['data']['architecture']['input_modalities']=['text']
    with pytest.raises(d.DelegateError):prepare(j,m,p)


@pytest.mark.parametrize('kind',['trailing','bad_crc','bad_pixels','fake_dimensions','animation','secret_metadata'])
def test_hostile_container_and_source_changes(inputs,kind):
    j,m,p=inputs;x=j['images'][0];raw=base64.b64decode(x['data_base64'])
    if kind=='trailing':raw+=b'hostile trailer'
    elif kind=='bad_crc':raw=raw[:29]+bytes([raw[29]^1])+raw[30:]
    elif kind=='bad_pixels':raw=raw[:-25]+b'bad'+raw[-12:]
    elif kind=='fake_dimensions':x['width']-=1;x['bbox'][2]-=1
    elif kind=='animation':
        b=io.BytesIO();Image.new('RGB',(710,415),'white').save(b,format='PNG',save_all=True,append_images=[Image.new('RGB',(710,415),'black')],duration=100);raw=b.getvalue()
    elif kind=='secret_metadata':
        b=io.BytesIO();meta=PngImagePlugin.PngInfo();meta.add_text('note','OPENROUTER_API_KEY=sk-or-v1-'+'a'*64);Image.new('RGB',(710,415)).save(b,format='PNG',pnginfo=meta);raw=b.getvalue()
    x['data_base64']=base64.b64encode(raw).decode();p=proof_for(j,m)
    with pytest.raises(d.DelegateError):prepare(j,m,p)


def test_full_text_schema_context_is_counted(inputs):
    j,m,p=inputs;_,_,e=prepare(j,m,p)
    old=e['accounting']['estimated_input_token_bound']
    j['prompt']+=' Х'*1000;p=proof_for(j,m)
    _,_,e=prepare(j,m,p)
    assert e['accounting']['estimated_input_token_bound']==old+len((' Х'*1000).encode())
    ep=d.select_endpoint(m,j['profile'])[2];ep['pricing']['input_cache_write']='0.0000009'
    _,c,e=prepare(j,m,p)
    assert e['accounting']['input_rate_bound']=='9E-7'
    ep['pricing']['prompt']='1'
    with pytest.raises(d.DelegateError,match='CALL_COST_CAP'):prepare(j,m,p)


def test_legacy_unchanged_without_optin(inputs):
    j,m,p=inputs;d.select_endpoint(m,j['profile'])[2]['context_length']=2000000;_,_,e=d.prepare(j,m)
    assert e['accounting']['image_method_status'].startswith('LEGACY')
    assert e['accounting']['estimated_input_token_bound']>p['total_pixels']*4


def test_invalid_optin_before_key_env_ledger_network(inputs,tmp_path,monkeypatch):
    j,m,p=inputs;p['status']='READY_FOR_REVIEW'
    def forbidden(*args,**kwargs):raise AssertionError('credential/ledger/network read forbidden')
    monkeypatch.setattr(d.os.environ,'get',forbidden);monkeypatch.setattr(d,'reserve',forbidden);monkeypatch.setattr(d.Transport,'request',forbidden)
    with pytest.raises(d.DelegateError,match='IMAGE_RULE_APPROVAL_REQUIRED'):
        d.run(j,m,tmp_path/'absent',image_rule_proof=p,approved_image_rule_sha256=d.digest(p))
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('kind',['good','trailer','fake_header'])
def test_jpeg_exact_bytes_and_container(inputs,kind):
    j,m,p=inputs;b=io.BytesIO();Image.new('RGB',(710,415),'white').save(b,format='JPEG');raw=b.getvalue()
    if kind=='trailer':raw+=b'trailer'
    if kind=='fake_header':raw=b'not-jpeg'+raw[8:]
    j['images'][0]['mime']='image/jpeg';j['images'][0]['data_base64']=base64.b64encode(raw).decode();p=proof_for(j,m)
    if kind=='good':
        r,_,e=prepare(j,m,p);assert e['accounting']['image_rule']['images'][0]['mime']=='image/jpeg'
        assert r['messages'][0]['content'][1]['image_url']['url'].endswith(j['images'][0]['data_base64'])
    else:
        with pytest.raises(d.DelegateError):prepare(j,m,p)


@pytest.mark.parametrize('cap',[False,0,100,'100'])
def test_optional_prompt_cap(inputs,cap):
    j,m,p=inputs;d.select_endpoint(m,j['profile'])[2]['max_prompt_tokens']=cap
    with pytest.raises(d.DelegateError,match='INPUT_LIMIT'):prepare(j,m,p)


def test_ready_cli_cannot_become_approval(inputs,tmp_path,monkeypatch,capsys):
    j,m,p=inputs;p['status']='READY_FOR_REVIEW'
    for name,value in [('job',j),('metadata',m),('proof',p)]:d.write_json(tmp_path/(name+'.json'),value)
    original_get=d.os.environ.get
    def guarded_get(name,*a,**kw):
        if name=='OPENROUTER_API_KEY':raise AssertionError('No key read')
        return original_get(name,*a,**kw)
    def forbidden(*a,**kw):raise AssertionError('No network')
    monkeypatch.setattr(d.os.environ,'get',guarded_get);monkeypatch.setattr(d.Transport,'request',forbidden)
    code=d.main(['check','--job',str(tmp_path/'job.json'),'--metadata',str(tmp_path/'metadata.json'),'--image-rule-proof',str(tmp_path/'proof.json'),'--approved-image-rule-sha256',d.digest(p)])
    assert code==2 and 'IMAGE_RULE_APPROVAL_REQUIRED' in capsys.readouterr().out
