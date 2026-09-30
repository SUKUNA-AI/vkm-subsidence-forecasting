"""Conservative geometry candidates; reader attribution remains unreviewed."""
from __future__ import annotations
import argparse,json,subprocess,sys
from pathlib import Path

GEOMETRY_TYPES={'PLAN','MAP','SECTION','STRATIGRAPHIC_COLUMN','STRAT_COLUMN','SCHEME_DRAWING','SCHEME'}

def select(work):
    inv=json.loads((work/'inventory.json').read_text(encoding='utf8'))
    selected=[];decisions=[]
    for item in inv:
        path=work/'raw/qwen'/(item['key']+'_classify.json')
        response=json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
        classification=response.get('parsed') or {}
        kind=classification.get('type',item['type'])
        # Never turn an unreviewed classifier's mine label into scientific scope.
        accepted=kind in GEOMETRY_TYPES
        if accepted:selected.append(item['key'])
        decisions.append({'figure_key':item['key'],'classified_type':kind,'selected':accepted,
                          'classification_file':path.relative_to(work).as_posix() if path.exists() else None,
                          'status':'AUTO_EXTRACTED_UNREVIEWED'})
    return selected,decisions

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--work',required=True);ap.add_argument('--reader',choices=['qwen','glm'],required=True)
    ap.add_argument('--url',required=True);ap.add_argument('--model',required=True)
    args=ap.parse_args();work=Path(args.work)
    keys,decisions=select(work)
    jobs=json.loads((work/'jobs_v2.json').read_text(encoding='utf8'))
    (work/'geometry_read_selection.json').write_text(json.dumps({'rule':'Geometry type candidates including analogues; scope is not promoted',
          'selected_figures':len(keys),'selected_tile_jobs':sum(j['kind']=='TILE' and j['figure_key'] in keys for j in jobs),
          'decisions':decisions},ensure_ascii=False,indent=2),encoding='utf8')
    subprocess.run([sys.executable,str(Path(__file__).with_name('read_local.py')),'--work',str(work),'--reader',args.reader,
          '--url',args.url,'--model',args.model,'--kind','TILE','--figures',','.join(keys)],check=True)
