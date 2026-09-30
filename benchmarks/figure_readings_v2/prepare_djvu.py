"""Addressed DjVu figures: original file identity, decoded page, exact crop transforms."""
from __future__ import annotations
import argparse, csv, hashlib, json, subprocess
from pathlib import Path
from prepare import digest, make_crop, write_json
from vkm_corpus.artifacts.render import render_djvu_page, crop_box_px

def prepare(work: Path, resources: Path, dpi: int = 300):
    work=work.resolve();resources=resources.resolve()
    repo=Path(__file__).resolve().parents[2]
    register={r['resource_id']:r for r in csv.DictReader((resources/'00_registry/SOURCE_REGISTER.csv').open(encoding='utf-8-sig'))}
    inventory=json.loads((work/'inventory.json').read_text(encoding='utf8'))
    jobs=json.loads((work/'jobs_v2.json').read_text(encoding='utf8'))
    known={r['source_object_id'] for r in inventory if isinstance(r.get('source_object_id'),str)}
    candidates={}
    for path in sorted(work.glob('inventory_search_raw*.json')):
        for query in json.loads(path.read_text(encoding='utf8')):
            for hit in query['result'].get('items') or []:
                env=hit['envelope'];sid=env['source_id'];reg=register.get(sid)
                if reg and Path(reg['canonical_path']).suffix.lower()=='.djvu':candidates[env['object_id']]=hit
    checked={};pages={};added=[]
    before={n:digest(work/n) for n in ('inventory.json','jobs_v2.json')}
    for oid,hit in sorted(candidates.items()):
        if oid in known:continue
        env=hit['envelope'];sid=env['source_id'];reg=register[sid];src=resources/reg['canonical_path']
        bbox=(env.get('geometry') or {}).get('bbox')
        if not bbox:raise ValueError('MISSING_BBOX:'+oid)
        if sid not in checked:
            checked[sid]=digest(src)
            if checked[sid]!=reg['sha256']:raise ValueError('SOURCE_HASH_MISMATCH:'+sid)
        page_id=env['page_id'];index=env['page_index']
        if page_id not in pages:
            raster=render_djvu_page(src,index,dpi,'RGB')
            pagefile=work/'pages'/(page_id.replace(':','_')+'.png');pagefile.parent.mkdir(exist_ok=True)
            raster.image.save(pagefile)
            metadata=subprocess.run(['djvused',str(src),'-e',f'select {index}; size; print-txt; print-ant'],capture_output=True,timeout=60,check=True)
            nativefile=work/'native'/(page_id.replace(':','_')+'_djvu.txt');nativefile.parent.mkdir(exist_ok=True)
            nativefile.write_bytes(metadata.stdout)
            pages[page_id]=(raster,{'page_id':page_id,'file':pagefile.relative_to(work).as_posix(),'sha256':digest(pagefile),
                 'dpi':dpi,'width':raster.width,'height':raster.height,'purpose':'WHOLE_PAGE_CONTEXT',
                 'source_sha256':checked[sid],'native_metadata':nativefile.relative_to(work).as_posix(),
                 'native_metadata_sha256':digest(nativefile),'pixel_to_rotated_page_pt':[72/dpi,0,0,72/dpi,0,0]})
        raster,context=pages[page_id];box=crop_box_px(raster,tuple(bbox),pad_pt=0)
        if box[2]<=box[0] or box[3]<=box[1]:raise ValueError('EMPTY_BBOX:'+oid)
        image=raster.image.crop(box);key='I'+hashlib.sha256(oid.encode()).hexdigest()[:16]
        path=work/'renders'/(key+'.png');path.parent.mkdir(exist_ok=True);image.save(path)
        rec={'key':key,'source_id':sid,'document':reg['canonical_path'],'source_object_id':oid,'page_id':page_id,
             'source_sha256':checked[sid],'source_image':path.relative_to(repo).as_posix(),'image_sha256':digest(path),
             'width':image.width,'height':image.height,'caption':hit['record'].get('caption'),
             'type':'UNKNOWN','figure_number':'UNKNOWN','document_date':'UNKNOWN','represented_date':'UNKNOWN',
             'scope':env.get('source_scope'),'mine_attribution':'UNREVIEWED','raster_vector_mixed':'RASTER',
             'coordinate_grid':'UNKNOWN','scale':'UNKNOWN','orientation':'UNKNOWN','landmarks':[],'shown_entities':[],
             'quality':env.get('quality_flags',[]),'geometry_usefulness':'UNREVIEWED','temporal_information':'UNKNOWN','conflicts':[],
             'native_format':'DJVU','whole_page':context,'verification_status':'AUTO_EXTRACTED_UNREVIEWED',
             'locator':{'page_id':page_id,'figure_id':oid,'bbox_pt_tl':bbox,'renderer':'DjVuLibre ddjvu','effective_dpi':dpi},
             'image_to_page':[72/dpi,0,0,72/dpi,box[0]*72/dpi,box[1]*72/dpi]}
        inventory.append(rec);added.append(key)
        for y in range(0,max(1,image.height-640+512),512):
            for x in range(0,max(1,image.width-640+512),512):
                tile=(x,y,min(x+640,image.width),min(y+640,image.height))
                jobs.append(make_crop(work,image,rec,tile,f'{key}_t{x:05d}_{y:05d}','TILE',scale=2 if image.width<1500 else 1))
    write_json(work/'inventory.json',inventory);write_json(work/'jobs_v2.json',jobs)
    write_json(work/'djvu_prepare_receipt.json',{'schema':'vkm.djvu_preparation/1','added':added,'checked_sources':checked,
        'outputs_before':before,'outputs_after':{n:digest(work/n) for n in before},'dpi':dpi,'overlap':.2,
        'method':'DjVuLibre decode; original source bytes unchanged; native text/annotations retained; no vector claim'})
    print('DjVu figures added',len(added),'pages',len(pages))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--work',required=True);p.add_argument('--resources',required=True);p.add_argument('--dpi',type=int,default=300)
    a=p.parse_args();prepare(Path(a.work),Path(a.resources),a.dpi)
