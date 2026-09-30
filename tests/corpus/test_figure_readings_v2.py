"""Synthetic regressions for literal text, clipping eligibility and pixel provenance."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'benchmarks/figure_readings_v2'

def module(name):
    spec=importlib.util.spec_from_file_location(name,BASE/(name+'.py'))
    out=importlib.util.module_from_spec(spec)
    sys.modules[name]=out
    spec.loader.exec_module(out)
    return out

reader=module('read_local')

def test_ocr_format_parser_preserves_critical_punctuation_and_glyphs():
    assert reader.parse_glm_text('```markdown\n\n```')==''
    assert reader.parse_glm_text('```text\n00100,020 AА\n```')=='00100,020 AА'
    assert reader.parse_glm_text('```markdown\n00100,020')=='```markdown\n00100,020'
    assert reader.parse_answer('prefix {"text":"00100,020","status":"READABLE"} suffix')['text']=='00100,020'
    assert reader.parse_answer('not json') is None

def test_unreadable_is_an_accuracy_failure_not_dropped_denominator(tmp_path):
    evaluator=module('evaluate')
    evaluator.OFFSETS={'SYNTHETIC':1}
    gold=tmp_path/'gold';gold.mkdir()
    (gold/'SYNTHETIC_claude.csv').write_text('value,status\n"00100,020",VERIFIED\n"cut[обрезано]",VERIFIED\n',encoding='utf8')
    work=tmp_path/'work';(work/'raw/qwen').mkdir(parents=True)
    jobs=[{'figure_key':'SYNTHETIC','row':1,'column':0,'kind':'CELL','key':'unit'}]
    (work/'jobs_v2.json').write_text(json.dumps(jobs),encoding='utf8')
    (work/'raw/qwen/unit.json').write_text(json.dumps({'parsed':{'text':'','status':'UNREADABLE'},'finish_reason':'stop'}),encoding='utf8')
    result=evaluator.evaluate(work,gold,['qwen'])
    assert result['metrics']['qwen']['gold_cells']==1
    assert result['metrics']['qwen']['coverage']==0
    assert result['metrics']['qwen']['literal_accuracy']==0
    assert result['excluded_truncated_gold_cells']==1

def test_crop_transform_roundtrip_and_black_zero_grid(tmp_path):
    pytest.importorskip('cv2');pytest.importorskip('pymupdf')
    from PIL import Image,ImageDraw
    preparation=module('prepare')
    im=Image.new('RGB',(100,60),'white');draw=ImageDraw.Draw(im)
    for x in [0,50,99]:draw.line((x,0,x,59),fill='black')
    for y in [0,30,59]:draw.line((0,y,99,y),fill='black')
    xs,ys=preparation.table_grid(im,include_black=True)
    assert 50 in xs and 30 in ys
    assert preparation.table_grid(im,include_black=False)[0]==[0,99]
    parent={'key':'SYNTHETIC','source_id':'SYNTHETIC','image_sha256':'x','locator':{'page_id':'SYNTHETIC-P1'},'image_to_page':[.5,0,0,.5,100,200]}
    crop=preparation.make_crop(tmp_path,im,parent,[10,20,40,50],'unit','CELL',scale=3)
    assert Image.open(tmp_path/crop['file']).size==(90,90)
    sx=45*crop['crop_to_image'][0]+crop['crop_to_image'][4]
    sy=45*crop['crop_to_image'][3]+crop['crop_to_image'][5]
    assert (sx,sy)==(25,35)
    assert sx*.5+100==112.5
    assert preparation.digest(tmp_path/crop['file'])==crop['sha256']


def test_assembly_preserves_ambiguity_and_never_promotes_native_or_model_reading(tmp_path,monkeypatch):
    pytest.importorskip('pymupdf')
    from PIL import Image
    preparation=module('prepare');assembly=module('assemble')
    work=tmp_path/'work';work.mkdir();source=tmp_path/'synthetic.png';Image.new('RGB',(10,10),'white').save(source)
    crop=work/'crop.png';Image.new('RGB',(5,5),'white').save(crop)
    image_sha=preparation.digest(source);crop_sha=preparation.digest(crop)
    item={'key':'SYNTHETIC','source_id':'SYNTHETIC','source_sha256':'a'*64,'source_image':str(source),
          'image_sha256':image_sha,'width':10,'height':10,'locator':{'object_id':'SYNTHETIC-OBJ'}}
    job={'key':'SYNTHETIC-CELL','figure_key':'SYNTHETIC','kind':'CELL','row':1,'column':0,'file':'crop.png',
         'sha256':crop_sha,'box_image_px':[0,0,5,5],'crop_to_image':[1,0,0,1,0,0],'locator':item['locator']}
    preparation.write_json(work/'inventory.json',[item]);preparation.write_json(work/'jobs_v2.json',[job])
    preparation.write_json(work/'accuracy.json',{'cells':[],'excluded':[],'gold_hashes':{},'metrics':{}})
    preparation.write_json(work/'review_root/review.json',{'records':[{'job_key':job['key'],'literal_text':'AА',
         'verification_status':'VISIBLE_GLYPHS_REVIEWED_ENCODING_AMBIGUOUS','input_sha256':crop_sha,'reviewer':'SYNTHETIC_REVIEWER','uncertainty':['encoding']}]})
    raw=work/'raw/glm/SYNTHETIC-CELL.json'
    preparation.write_json(raw,{'input_sha256':crop_sha,'content':'AA','parsed':{'text':'AA'},'read_status':'PARSED','finish_reason':'stop'})
    raw_sha=preparation.digest(raw);assembly.assemble(work)
    figure=json.loads((work/'figures/SYNTHETIC.json').read_text(encoding='utf8'))
    result=figure['cells_and_labels'][0]
    assert result['verification_status']=='VISIBLE_GLYPHS_REVIEWED_ENCODING_AMBIGUOUS'
    assert result['literal_text']=='AА' and result['physical_units']=='UNKNOWN'
    assert result['admitted_to_evidence'] is False and preparation.digest(raw)==raw_sha
    queue=json.loads((work/'ASTRA_REVIEW_REQUIRED.json').read_text(encoding='utf8'))['items']
    assert len(queue)==1 and queue[0]['status']=='ASTRA_REVIEW_REQUIRED'
    assert queue[0]['crop']['sha256']==crop_sha and queue[0]['source_image']['sha256']==image_sha
