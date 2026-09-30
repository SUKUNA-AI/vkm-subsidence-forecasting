"""Whole-figure classification crops; preserve the exact pixel transform and prior jobs."""
import argparse,json
from pathlib import Path
from PIL import Image
from prepare import digest,write_json

def build(work):
    repo=Path(__file__).resolve().parents[2]
    inventory=json.loads((work/'inventory.json').read_text(encoding='utf8'))
    jobs=json.loads((work/'jobs_v2.json').read_text(encoding='utf8'));existing={j['key'] for j in jobs};added=0
    before=digest(work/'jobs_v2.json')
    for item in inventory:
        key=item['key']+'_classify'
        if key in existing:continue
        source=repo/item['source_image'];im=Image.open(source).convert('RGB')
        assert digest(source)==item['image_sha256']
        im.thumbnail((1536,1536),Image.Resampling.LANCZOS)
        out=work/'whole'/(item['key']+'.png');out.parent.mkdir(exist_ok=True);im.save(out)
        jobs.append({'key':key,'figure_key':item['key'],'kind':'CLASSIFY','source_id':item['source_id'],
                     'file':out.relative_to(work).as_posix(),'sha256':digest(out),'source_image_sha256':item['image_sha256'],
                     'box_image_px':[0,0,item['width'],item['height']],
                     'crop_to_image':[item['width']/im.width,0,0,item['height']/im.height,0,0],
                     'image_to_page':item.get('image_to_page'),'locator':item['locator'],'verification_status':'AUTO_EXTRACTED_UNREVIEWED'})
        added+=1
    write_json(work/'jobs_v2.json',jobs)
    write_json(work/'classification_append_receipt.json',{'added_jobs':added,'jobs_sha256_before':before,'jobs_sha256_after':digest(work/'jobs_v2.json')})
    print('added classifications',added)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--work',required=True);a=ap.parse_args();build(Path(a.work))
