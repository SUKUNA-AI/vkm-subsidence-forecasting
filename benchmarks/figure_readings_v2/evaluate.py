"""Gold accuracy, literal/tolerant views, and reader disagreement are distinct."""
from __future__ import annotations
import argparse,csv,hashlib,json,math,re,unicodedata
from pathlib import Path
try:
    from .read_local import parse_glm_text
except ImportError:  # direct script invocation
    from read_local import parse_glm_text

OFFSETS={'F023_015':1,'F023_016':1,'F023_017':3,'F023_018':3,'F023_019':3}

def normalized(text):
    # A secondary typographic comparison only: never overwrite a literal value.
    text=unicodedata.normalize('NFC',text).strip()
    text=re.sub(r'\s+',' ',text)
    return text.replace('Крll','КрII').replace('КрIl','КрII').replace('КрlI','КрII')

def wilson(k,n):
    if not n:return None
    z=1.959963984540054;p=k/n;d=1+z*z/n
    center=(p+z*z/(2*n))/d
    radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [center-radius,center+radius]

def evaluate(work,gold,readers):
    jobs=json.loads((work/'jobs_v2.json').read_text(encoding='utf8'))
    by_cell={(j['figure_key'],j['row'],j['column']):j for j in jobs if j['kind']=='CELL'}
    compared=[];excluded=[];output={}
    for key,offset in OFFSETS.items():
        rows=list(csv.DictReader((gold/(key+'_claude.csv')).open(encoding='utf8')))
        columns=[c for c in rows[0] if c not in ('status',)]
        for r,row in enumerate(rows):
            for c,name in enumerate(columns):
                expected=row[name]
                if '[обрезано]' in expected or row.get('status','').startswith('LOW_CONFIDENCE'):
                    excluded.append({'figure_key':key,'row':r+offset,'column':c,'reason':'SOURCE_TRUNCATED_GOLD'})
                    continue
                job=by_cell.get((key,r+offset,c))
                item={'figure_key':key,'row':r+offset,'column':c,'gold_column':name,'gold':expected,'readers':{}}
                for reader in readers:
                    path=work/'raw'/reader/(job['key']+'.json') if job else None
                    rec=json.loads(path.read_text(encoding='utf8')) if path and path.exists() else {}
                    parsed=rec.get('parsed') or {}
                    text=parsed.get('text','')
                    if reader=='glm':
                        text=parse_glm_text(text)
                    status=parsed.get('status','MISSING')
                    readable=bool(rec) and not rec.get('error') and rec.get('finish_reason')!='length' and status not in ('UNREADABLE','TRUNCATED','MISSING')
                    item['readers'][reader]={'text':text,'status':status,'readable':readable,
                          'exact':readable and text.strip()==expected.strip(),
                          'typographic':readable and normalized(text)==normalized(expected),
                          'raw_file':str(path.relative_to(work)) if path and path.exists() else None}
                compared.append(item)
    for reader in readers:
        rs=[x['readers'][reader] for x in compared];n=len(rs)
        exact=sum(x['exact'] for x in rs);typo=sum(x['typographic'] for x in rs);readable=sum(x['readable'] for x in rs)
        output[reader]={'gold_cells':n,'readable':readable,'coverage':readable/n if n else None,
                       'literal_accuracy':exact/n if n else None,'literal_correct':exact,
                       'literal_wilson95':wilson(exact,n),'typographic_accuracy':typo/n if n else None,
                       'definition':'Accuracy against independently eye-reviewed published image transcription, not field validation'}
    if len(readers)==2:
        a,b=readers;rs=[x for x in compared if x['readers'][a]['readable'] and x['readers'][b]['readable']]
        output['agreement']={'joint_readable_cells':len(rs),'exact':sum(x['readers'][a]['text'].strip()==x['readers'][b]['text'].strip() for x in rs)/len(rs) if rs else None,
                             'definition':'Reader agreement; not accuracy'}
    result={'schema':'vkm.figure_reading_accuracy/2','metrics':output,'excluded_truncated_gold_cells':len(excluded),
            'gold_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in gold.glob('*_claude.csv')},
            'jobs_sha256':hashlib.sha256((work/'jobs_v2.json').read_bytes()).hexdigest(),
            'alignment':{'method':'source grid visually checked; fixed data-row offsets','row_offsets':OFFSETS},
            'status':'LITERAL_EXTRACTION_ONLY; NOT_MINE_OBSERVATIONS',
            'format_parser':'GLM: remove one complete Markdown/text fence only; preserve letters, decimal punctuation and internal whitespace',
            'cells':compared,'excluded':excluded}
    (work/'accuracy.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return result

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--work',required=True);ap.add_argument('--gold',required=True);ap.add_argument('--readers',default='qwen,glm')
    a=ap.parse_args();r=evaluate(Path(a.work),Path(a.gold),a.readers.split(','));print(json.dumps(r['metrics'],indent=2))
