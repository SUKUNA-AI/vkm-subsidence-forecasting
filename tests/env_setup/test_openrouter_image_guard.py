"""Synthetic binary/text boundary tests. No private pixels, keys or HTTP."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import zlib

from PIL import Image, PngImagePlugin
import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('delegate_image_guard',ROOT/'.agents/skills/openrouter-delegate/scripts/openrouter_call.py')
d=importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def chunk(kind,value):
    return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value)&0xffffffff)


def innocent_pattern_png():
    # The pixel bytes happen to encode to an AWS-like token; this is not a key.
    marker='AKIAabcdefghijklmnop'
    rgb=base64.b64decode(marker)
    for padding in range(3):
        raw=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',5,1,8,2,0,0,0))+
             chunk(b'tEXt',b'note\0'+b'X'*padding)+chunk(b'IDAT',zlib.compress(b'\0'+rgb,0))+chunk(b'IEND',b''))
        if marker in base64.b64encode(raw).decode():
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                assert image.tobytes()==rgb
            return raw
    raise AssertionError('fixture alignment')


def encoded_image(raw,mime='image/png'):
    with Image.open(io.BytesIO(raw)) as image:
        width,height=image.size
    return {'mime':mime,'data_base64':base64.b64encode(raw).decode(),'width':width,'height':height,'bbox':[0,0,width,height]}


def job(raw=None,mime='image/png'):
    if raw is None:
        buf=io.BytesIO();Image.new('RGB',(8,4),'white').save(buf,format='PNG');raw=buf.getvalue()
    return {'task_id':'synthetic-image-guard','task_kind':'visual_analysis','synthetic':True,'profile':'vision_documents',
            'reasoning_effort':'high','prompt':'Read synthetic printed shapes only.','max_tokens':1024,'images':[encoded_image(raw,mime)]}


def real_job(raw):
    result=job(raw);result['synthetic']=False
    result['input_package']={'schema':'vkm.delegate-artifact-package/2','authorization_scope':'VKM-MULTIAGENT-DELEGATION-V2',
      'selection_rationale':'Synthetic fixture representing a real-image package contract, not a scientific source claim.',
      'artifacts':[{'artifact_id':'fixture','kind':'SOURCE','source_id':'VKM-SRC-011','work_id':'VKM-WRK-011',
        'logical_name':'synthetic/image.png','full_sha256':hashlib.sha256(raw).hexdigest(),'fragments':[],
        'images':[{'image_index':0,'sha256':hashlib.sha256(raw).hexdigest(),'locator':{'kind':'CROP','value':'synthetic whole-image fixture'}}]}]}
    return result


def png_metadata(value,kind='text',key='Description'):
    info=PngImagePlugin.PngInfo()
    if kind=='itxt':info.add_itxt(key,value,zip=True)
    else:info.add_text(key,value,zip=kind=='ztxt')
    out=io.BytesIO();Image.new('RGB',(8,4),'white').save(out,format='PNG',pnginfo=info);return out.getvalue()


def exif_image(value,tag=0x010e,*,kind='JPEG',nested=False):
    exif=Image.Exif()
    if nested:exif[0x8769]={tag:value}
    else:exif[tag]=value
    out=io.BytesIO();Image.new('RGB',(8,4),'white').save(out,format=kind,exif=exif);return out.getvalue()


def test_valid_png_binary_encoding_is_not_plaintext_credential():
    raw=innocent_pattern_png()
    assert hashlib.sha256(raw).hexdigest()=='2ab415da4e44b8dc222187599819a195ba99debe37486af64e83caf25eed961d'
    _,pixels=d.validate_job(job(raw))
    assert pixels==5


@pytest.mark.parametrize('kind',['text','ztxt','itxt'])
@pytest.mark.parametrize('text',['password=synthetic-secret','api_key: synthetic-value','postgresql://user:synthetic@localhost/db','sk-or-v1-synthetic-not-a-real-key','AKIAabcdefghijklmnop'])
def test_png_text_metadata_credentials_rejected(kind,text):
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(job(png_metadata(text,kind)))


def test_png_sensitive_key_with_plain_value_rejected():
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(job(png_metadata('synthetic-value',key='password')))


@pytest.mark.parametrize('kind,mime',[('JPEG','image/jpeg'),('PNG','image/png')])
def test_exif_description_credentials_rejected(kind,mime):
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(job(exif_image('password=synthetic-secret',kind=kind),mime))


@pytest.mark.parametrize('value',[b'ASCII\0\0\0password=synthetic-secret',b'UNICODE\0'+('password=synthetic-secret').encode('utf-16-be')])
def test_nested_exif_user_comment_credentials_rejected(value):
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(job(exif_image(value,tag=0x9286,nested=True),'image/jpeg'))


def test_utf16_exif_xpcomment_credentials_rejected():
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(job(exif_image(('password=synthetic-secret').encode('utf-16-le'),tag=0x9c9c),'image/jpeg'))


def test_exact_session_secret_in_image_metadata_rejected():
    secret='synthetic-exact-session-secret-no-format'
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(job(png_metadata(secret)),secret)


@pytest.mark.parametrize('text',['password=synthetic-secret','{"password":"synthetic-secret"}','Authorization: Bearer synthetic-token',
    '{"api_key": "synthetic-token"}','mysql://user:synthetic@localhost/db','mongodb://user:synthetic@localhost/db',
    'ghp_'+'a'*24,'-----BEGIN PRIVATE KEY-----','AKIAabcdefghijklmnop'])
def test_plaintext_credentials_still_rejected(text):
    value=job(innocent_pattern_png());value['prompt']=text
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(value)


def test_nested_schema_text_is_scanned_even_if_named_data_base64():
    value=job(innocent_pattern_png());value['schema']={'type':'object','properties':{'data_base64':{'type':'string','description':'password=synthetic-secret'}},'required':['data_base64'],'additionalProperties':False}
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(value)


def test_fragment_nested_json_credential_is_scanned():
    value=real_job(innocent_pattern_png());text='{"nested":{"password":"synthetic-secret"}}';h=hashlib.sha256(text.encode()).hexdigest()
    value['input_package']['artifacts'].append({'artifact_id':'text','kind':'PUBLIC_TEXT','source_id':None,'work_id':None,
      'logical_name':'synthetic/nested.json','full_sha256':h,'fragments':[{'text':text,'sha256':h,'locator':{'kind':'FULL_FILE','value':'synthetic/nested.json'}}],'images':[]})
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(value)


def test_binary_skip_does_not_skip_image_provenance_hash():
    value=real_job(innocent_pattern_png());value['input_package']['artifacts'][0]['images'][0]['sha256']='f'*64
    with pytest.raises(d.DelegateError,match='IMAGE_PROVENANCE_MISMATCH'):
        d.validate_job(value)


@pytest.mark.parametrize('mutation',['mime','size','base64','truncated','crc'])
def test_malformed_or_spoofed_image_still_rejected(mutation):
    value=job(innocent_pattern_png())
    if mutation=='mime':value['images'][0]['mime']='image/jpeg'
    elif mutation=='size':value['images'][0]['width']=6
    elif mutation=='base64':value['images'][0]['data_base64']='not valid base64!'
    else:
        raw=base64.b64decode(value['images'][0]['data_base64'])
        raw=raw[:-15] if mutation=='truncated' else raw[:29]+bytes([raw[29]^1])+raw[30:]
        value['images'][0]['data_base64']=base64.b64encode(raw).decode()
    with pytest.raises(d.DelegateError,match='INVALID_IMAGE'):
        d.validate_job(value)


def test_ordinary_metadata_and_large_v2_image_still_allowed():
    out=io.BytesIO();info=PngImagePlugin.PngInfo();info.add_itxt('Description','Synthetic terrain label, 20 cm; no credentials.')
    Image.new('RGB',(2050,1),'white').save(out,format='PNG',pnginfo=info)
    assert d.validate_job(job(out.getvalue()))[1]==2050


def test_legacy_image_dimension_cap_retained():
    value=job();value.pop('task_kind');value.pop('reasoning_effort');value['images'][0]['width']=2049
    with pytest.raises(d.DelegateError,match='INVALID_IMAGE'):
        d.validate_job(value)


def test_binary_payload_and_job_never_rewritten():
    value=real_job(innocent_pattern_png());before=d.encoded(value)
    d.validate_job(value)
    assert d.encoded(value)==before


@pytest.mark.parametrize('text',[r'{"pass\u0077ord":"synthetic-value"}',r'{"nested":[{"value":"sk\u002dor\u002dv1\u002dsynthetic"}]}'])
def test_json_escaped_credentials_in_text_are_not_a_bypass(text):
    value=job();value['prompt']=text
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):d.validate_job(value)


def test_json_nested_credentials_in_png_text_rejected():
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(job(png_metadata(r'{"pass\u0077ord":"synthetic-value"}')))


def test_jpeg_comment_credentials_rejected():
    out=io.BytesIO();Image.new('RGB',(8,4),'white').save(out,format='JPEG')
    content=b'password=synthetic-value';raw=out.getvalue()
    raw=raw[:2]+b'\xff\xfe'+struct.pack('>H',len(content)+2)+content+raw[2:]
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):d.validate_job(job(raw,'image/jpeg'))


@pytest.mark.parametrize('mime',[None,[],{},True])
def test_nontext_mime_rejected_without_typeerror(mime):
    value=job();value['images'][0]['mime']=mime
    with pytest.raises(d.DelegateError,match='INVALID_IMAGE'):d.validate_job(value)


def high_metadata():
    model,tag,provider,_=d.PROFILES['vision_documents']
    params=['reasoning','reasoning_effort','max_tokens','response_format','structured_outputs']
    return [{'model':model,'checked_at':d.now(),'http_status':200,
      'metadata':{'data':{'id':model,'architecture':{'input_modalities':['text','image']},'endpoints':[
        {'model_id':model,'provider_name':provider,'tag':tag,'status':0,'service_tier':'flex',
         'context_length':1000000,'max_completion_tokens':100000,'supported_parameters':params,
         'pricing':{'prompt':'0.0000001','completion':'0.0000005','image':'0'}}]}},
      'model_capabilities':{'checked_at':d.now(),'catalogue_sha256':'a'*64,'model':{
        'id':model,'canonical_slug':d.CANONICAL_MODEL_PINS[model],'supported_parameters':params,
        'reasoning':{'supported_efforts':['high','medium','low'],'default_effort':'medium','mandatory':True}}}}]


@pytest.mark.parametrize('case',['escaped_password_list_prompt','escaped_password_dict_fragment','api_key_enum_schema'])
def test_credential_containers_fail_before_request_construction(case):
    credential='synthetic-other-service-credential-no-key-prefix'
    value=job()
    if case=='escaped_password_list_prompt':
        value['prompt']=r'{"pass\u0077ord":["'+credential+r'"]}'
    elif case=='escaped_password_dict_fragment':
        value=real_job(innocent_pattern_png())
        text=r'{"pass\u0077ord":{"primary":"'+credential+r'"}}';h=hashlib.sha256(text.encode()).hexdigest()
        value['input_package']['artifacts'].append({'artifact_id':'config','kind':'PUBLIC_TEXT','source_id':None,'work_id':None,
          'logical_name':'synthetic/config.json','full_sha256':h,'fragments':[{'text':text,'sha256':h,'locator':{'kind':'FULL_FILE','value':'synthetic/config.json'}}],'images':[]})
    else:
        value['schema']={'type':'object','properties':{'api_key':{'type':'string','enum':[credential]}},'required':['api_key'],'additionalProperties':False}
    before=d.encoded(value)
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):d.prepare(value,high_metadata())
    assert d.encoded(value)==before


@pytest.mark.parametrize('key',['password','api_key','authorization'])
@pytest.mark.parametrize('payload',[["opaque-synthetic-value"],{"primary":"opaque-synthetic-value"},[{"a":[{"b":"opaque-synthetic-value"}]}],0,False])
def test_sensitive_metadata_context_survives_all_container_wrappers(key,payload):
    escaped={'password':'pass\\u0077ord','api_key':'api_\\u006bey','authorization':'authoriz\\u0061tion'}[key]
    text=json.dumps({key:payload}).replace('"'+key+'"','"'+escaped+'"')
    with pytest.raises(d.DelegateError,match='FORBIDDEN_INPUT'):
        d.validate_job(job(png_metadata(text)))


@pytest.mark.parametrize('empty',[None,'',[],{}])
def test_empty_credential_containers_have_no_credential_payload(empty):
    d.guard_input_text({'password':empty})
