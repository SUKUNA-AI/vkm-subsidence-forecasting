"""Versioned GLM retries; never overwrite the frozen first-pass OCR."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import time
import urllib.request

from PIL import Image


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x' if exclusive else 'w', encoding='utf-8', newline='\n') as file:
        json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        file.write('\n')


def repetition_flag(text):
    lines=[line.strip() for line in text.splitlines() if len(line.strip()) >= 12]
    counts=Counter(lines)
    return bool(lines and max(counts.values()) >= 8 and len(set(lines))/len(lines) < .6)


def subregions(width, height, overlap=32):
    mx,my=width//2,height//2
    return [[0,0,min(width,mx+overlap),min(height,my+overlap)],
            [max(0,mx-overlap),0,width,min(height,my+overlap)],
            [0,max(0,my-overlap),min(width,mx+overlap),height],
            [max(0,mx-overlap),max(0,my-overlap),width,height]]


def translated_transform(matrix, x, y):
    a,b,c,d,e,f=matrix
    return [a,b,c,d,e+a*x+c*y,f+b*x+d*y]


def request(job, image, target, url, tokens, parent_raw, transform):
    prompt='Text Recognition:'
    body={'model':'glm-ocr','messages':[{'role':'user','content':[
        {'type':'text','text':prompt},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(image.read_bytes()).decode()}}]}],
        'temperature':0,'top_p':1e-5,'top_k':1,'repetition_penalty':1.1,'seed':0,'max_tokens':tokens}
    request_hash=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    if target.exists():
        result=json.loads(target.read_text(encoding='utf-8'))
        if result['request_sha256'] != request_hash:
            raise ValueError('immutable retry request differs')
        return result
    req=urllib.request.Request(url.rstrip('/')+'/v1/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'})
    start=time.monotonic()
    with urllib.request.urlopen(req,timeout=300) as response:
        raw=json.load(response)
    choice=raw['choices'][0]; text=choice['message'].get('content','')
    result={'schema':'vkm.glm_retry/1','job_key':job['key'],'figure_key':job['figure_key'],
        'source_id':job['source_id'],'input_sha256':digest(image),'parent_raw_sha256':digest(parent_raw),
        'request_sha256':request_hash,'max_tokens':tokens,'finish_reason':choice.get('finish_reason'),
        'content':text,'response':raw,'crop_to_image':transform,'locator':job['locator'],
        'repetition_flag':repetition_flag(text),'verification':'AUTO_EXTRACTED_UNREVIEWED',
        'elapsed_s':round(time.monotonic()-start,3)}
    save(target,result,exclusive=True)
    return result


def main(args):
    repo=Path.cwd().resolve(); work=(repo/args.work).resolve(); out=(repo/args.out).resolve()
    if not out.is_relative_to(repo/'work'):
        raise ValueError('retry literals must stay in ignored work')
    lock=json.loads((repo/'work/gpu.lock').read_text(encoding='utf-8'))
    if lock['owner'] != 'SOL-ABC': raise ValueError('GPU lock owner mismatch')
    jobs={job['key']:job for job in json.loads((work/'jobs_v2.json').read_text(encoding='utf-8'))}
    selected=[]
    for path in sorted((work/'raw/glm').glob('*.json')):
        old=json.loads(path.read_text(encoding='utf-8'))
        if old.get('finish_reason')=='length': selected.append((jobs[old['key']],path))
    records=[]
    for index,(job,parent) in enumerate(selected,1):
        image=work/job['file']
        if digest(image) != job['sha256']: raise ValueError('frozen crop hash mismatch')
        full=out/'attempts'/(job['key']+'__full4096.json')
        result=request(job,image,full,args.url,4096,parent,job['crop_to_image'])
        item={'job_key':job['key'],'figure_key':job['figure_key'],'parent_raw_sha256':digest(parent),
              'original_input_sha256':job['sha256'],'attempts':[{'file':full.relative_to(repo).as_posix(),'sha256':digest(full)}]}
        if result['finish_reason']=='length' or result['repetition_flag']:
            im=Image.open(image)
            children=[]
            for part,box in enumerate(subregions(*im.size)):
                crop=out/'crops'/(job['key']+f'__part{part}.png');crop.parent.mkdir(parents=True,exist_ok=True)
                if not crop.exists(): im.crop(box).save(crop)
                path=out/'attempts'/(job['key']+f'__part{part}_2048.json')
                child=request(job,crop,path,args.url,2048,parent,translated_transform(job['crop_to_image'],box[0],box[1]))
                ref={'file':path.relative_to(repo).as_posix(),'sha256':digest(path),'crop':crop.relative_to(repo).as_posix(),
                     'crop_sha256':digest(crop),'box_in_parent_tile':box,'finish_reason':child['finish_reason'],
                     'repetition_flag':child['repetition_flag']}
                children.append(ref);item['attempts'].append(ref)
            item['status']='SPLIT_OUTPUTS_COMPLETE' if all(c['finish_reason']!='length' and not c['repetition_flag'] for c in children) else 'SOURCE_REVIEW_REQUIRED'
        else: item['status']='FULL_OUTPUT_COMPLETE'
        item['verification']='AUTO_EXTRACTED_UNREVIEWED';records.append(item)
        save(out/'receipt.json',{'schema':'vkm.glm_retry_batch/1','selected':len(selected),'records':records,
             'status_counts':dict(Counter(r['status'] for r in records)),'frozen_inputs_unchanged':True,
             'reader':'GLM-OCR','new_qwen_calls':0,'command':'python -B benchmarks/abc_completion_v1/retry_glm.py'})
        print(f"{index}/{len(selected)} {job['key']} {item['status']}",flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',default='work/figure_readings_2026-09-29/v2')
    parser.add_argument('--out',default='work/abc_completion_2026-09-30/retries')
    parser.add_argument('--url',default='http://127.0.0.1:8093')
    main(parser.parse_args())
