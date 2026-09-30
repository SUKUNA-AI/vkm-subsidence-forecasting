"""Serial local vision requests with immutable raw responses and resumable hashes."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import time
import urllib.request
from pathlib import Path

CELL = ('Прочитай только одну ячейку на изображении буквально. Не исправляй орфографию, '
        'не заменяй запятую точкой, сохрани видимые буквы и знаки. Не дополняй обрезанный текст. '
        'Верни JSON {"text":"видимый текст или пустая строка","status":"READABLE|EMPTY|UNREADABLE|TRUNCATED"}.')
TILE = ('Прочитай подписи, числа, оси и легенду на этой части научного рисунка буквально. '
        'Не угадывай рудник, координаты или отсутствующий текст. Верни JSON с labels '
        '[{"text":"...","bbox":[x0,y0,x1,y1],"status":"READABLE|UNREADABLE|TRUNCATED"}], '
        'legend, axes, uncertainty. bbox в пикселях именно переданного изображения. '
        'Не трассируй кривые графиков, не вычисляй параметры. Отметь неоднозначное соответствие подписи объекту.')
CLASSIFY = ('Классифицируй исходный научный рисунок. Не извлекай подробные числа. Верни только JSON '
            '{"type":"PLAN|MAP|SECTION|STRATIGRAPHIC_COLUMN|TABLE|GRAPH|PHOTO|SCHEME|OTHER|UNKNOWN",'
            '"mine_attribution":"SKRU1|SKRU2|SKRU3|VKM_REGIONAL|OTHER|UNKNOWN",'
            '"visible_attribution_text":"только видимая подпись или пусто",'
            '"geometry_usefulness":"HIGH|MEDIUM|LOW|NONE|UNKNOWN",'
            '"coordinate_grid":"VISIBLE|ABSENT|UNKNOWN","scale":"VISIBLE|ABSENT|UNKNOWN",'
            '"uncertainty":"краткое ограничение"}. Не считай близость в поиске доказательством рудника.')
PROMPTS={'CELL':CELL,'LABEL':CELL,'TILE':TILE,'CLASSIFY':CLASSIFY}


def parse_answer(text):
    try:
        return json.loads(text[text.index('{'):text.rindex('}')+1])
    except (ValueError,TypeError,json.JSONDecodeError):
        return None


def parse_glm_text(text):
    """Remove a single complete output fence; preserve literal text and punctuation."""
    lines = text.strip().splitlines()
    if len(lines) >= 2 and lines[0] in ('```', '```markdown', '```text') and lines[-1] == '```':
        return '\n'.join(lines[1:-1]).strip()
    return text.strip()


def main(a):
    work = Path(a.work)
    lock_path = Path(__file__).resolve().parents[2] / 'work/gpu.lock'
    if not lock_path.exists():
        raise RuntimeError('GPU lock must be acquired before local inference')
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    if lock.get('owner') != a.lock_owner:
        raise RuntimeError('GPU lock belongs to a different owner')
    jobs = json.loads((work/'jobs_v2.json').read_text(encoding='utf-8'))
    keys = a.figures.split(',') if a.figures else []
    job_keys = set(json.loads(Path(a.job_keys).read_text(encoding='utf-8'))['job_keys']) if a.job_keys else None
    selected = [j for j in jobs if (not keys or j['figure_key'] in keys)
                and (job_keys is None or j['key'] in job_keys)
                and (a.kind == 'ALL' or j['kind'] == a.kind)]
    if a.limit:
        selected = selected[:a.limit]
    prompt_hashes = {k:hashlib.sha256(v.encode()).hexdigest() for k,v in PROMPTS.items()}
    stats = {'selected':len(selected),'complete':0,'cached':0,'failed':0,'reader':a.reader}
    for n,j in enumerate(selected,1):
        prompt = 'Text Recognition:' if a.reader == 'glm' else PROMPTS[j['kind']]
        request_prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        out = work/'raw'/a.reader/(j['key']+'.json')
        if out.exists():
            old = json.loads(out.read_text(encoding='utf-8'))
            if (old.get('input_sha256')==j['sha256'] and old.get('prompt_sha256')==prompt_hashes[j['kind']]
                    and old.get('request_prompt_sha256')==request_prompt_hash and old.get('error') is None):
                stats['cached']+=1
                continue
            raise ValueError('existing response has incompatible inputs: '+j['key'])
        data=(work/j['file']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=j['sha256']:
            raise ValueError('input hash mismatch: '+j['key'])
        body={'model':a.model,'messages':[{'role':'user','content':[
            {'type':'text','text':prompt},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(data).decode()}}]}],
            'temperature':0,'top_p':1 if a.reader=='qwen' else 1e-5,'seed':0,
            'max_tokens':128 if j['kind'] in ('CELL','LABEL') else 384 if j['kind']=='CLASSIFY' else 1536}
        if a.reader=='qwen':
            body.update({'cache_prompt':False,'chat_template_kwargs':{'enable_thinking':False}})
        else:
            body.update({'top_k':1,'repetition_penalty':1.1})
        started=time.time()
        rec={'key':j['key'],'figure_key':j['figure_key'],'reader':a.reader,'model':a.model,
             'input_sha256':j['sha256'],'prompt_sha256':prompt_hashes[j['kind']],
             'request_prompt_sha256':request_prompt_hash,
             'request_sha256':hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest(),
             'locator':j['locator'],'box_image_px':j['box_image_px'], 'sampling':{k:body[k] for k in ['temperature','top_p','seed','max_tokens']},
             'verification_status':'AUTO_EXTRACTED_UNREVIEWED','error':None}
        try:
            req=urllib.request.Request(a.url.rstrip('/')+'/v1/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=240) as response:
                result=json.load(response)
            rec['response']=result
            choice=result['choices'][0]
            rec['content']=choice['message'].get('content','')
            rec['finish_reason']=choice.get('finish_reason')
            rec['parsed']=parse_answer(rec['content']) if a.reader=='qwen' else {'text':rec['content'],'status':'AUTO_EXTRACTED_UNREVIEWED'}
            if rec['finish_reason']=='length':
                rec['read_status']='TRUNCATED_AT_TOKEN_LIMIT'
            elif rec['parsed'] is None:
                rec['read_status']='INVALID_STRUCTURED_ANSWER'
            else:
                rec['read_status']='PARSED'
            stats['complete']+=1
        except Exception as exc:
            rec['error']=type(exc).__name__+': '+str(exc)
            stats['failed']+=1
        rec['wall_s']=round(time.time()-started,3)
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(rec,ensure_ascii=False,indent=1),encoding='utf-8')
        print(a.reader,n,'/',len(selected),j['key'],rec.get('read_status',rec['error']),rec['wall_s'],flush=True)
        if stats['failed']>=3:
            raise RuntimeError('three local request failures; stop for diagnosis')
    (work/(a.reader+'_run_receipt.json')).write_text(json.dumps(stats,indent=2),encoding='utf-8')


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--work',required=True)
    ap.add_argument('--url',required=True)
    ap.add_argument('--reader',choices=['qwen','glm'],required=True)
    ap.add_argument('--model',required=True)
    ap.add_argument('--figures',default='')
    ap.add_argument('--kind',choices=['CELL','LABEL','TILE','CLASSIFY','ALL'],default='ALL')
    ap.add_argument('--limit',type=int,default=0)
    ap.add_argument('--job-keys',default='',help='Private JSON selection with a job_keys array')
    ap.add_argument('--lock-owner',default='GPT-FIG-V2')
    main(ap.parse_args())
