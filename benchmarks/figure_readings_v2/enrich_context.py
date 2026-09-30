"""Local full-page context and untouched DOCX parts; all outputs remain WORK/PRIVATE."""
from __future__ import annotations
import argparse,csv,hashlib,json,re,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
import pymupdf as fitz
from prepare import digest,write_json

def enrich(work,resources):
    repo=Path(__file__).resolve().parents[2]
    registry={r['resource_id']:r for r in csv.DictReader((resources/'00_registry/SOURCE_REGISTER.csv').open(encoding='utf-8-sig'))}
    inventory=json.loads((work/'inventory.json').read_text(encoding='utf8'))
    captions={}
    for path in sorted(work.glob('inventory_search_raw*.json')):
        for q in json.loads(path.read_text(encoding='utf8')):
            for hit in q['result'].get('items',[]):
                captions[hit['envelope']['object_id']]=hit['record'].get('caption') or hit['record'].get('title_or_caption')
    documents={};pages={};parts={};checked={}
    for item in inventory:
        sid=item['source_id'];reg=registry.get(sid)
        if not reg:continue
        path=resources/reg['canonical_path']
        if sid not in checked:
            checked[sid]=digest(path)
            if checked[sid]!=reg['sha256']:raise ValueError('SOURCE_HASH_MISMATCH:'+sid)
        item.setdefault('document',reg['canonical_path'])
        for field in ('document_date','figure_number','represented_date','orientation','scale','coordinate_grid'):
            item.setdefault(field,'UNKNOWN')
        for field in ('landmarks','shown_entities','conflicts'):
            item.setdefault(field,[])
        item.setdefault('temporal_information','UNKNOWN');item.setdefault('geometry_usefulness','UNREVIEWED')
        item.setdefault('quality','UNREVIEWED');item.setdefault('verification_status',item.get('review_status','AUTO_EXTRACTED_UNREVIEWED'))
        if isinstance(item.get('source_object_id'),str):
            item['caption']=captions.get(item['source_object_id']) or item.get('caption')
            match=re.search(r'(?:Рис\.?|Рисунок)\s*([\d.]+[а-яa-z]?)',item.get('caption') or '',re.I)
            if match:item['figure_number']=match.group(1)
        if path.suffix.lower()=='.docx' and sid not in parts:
            objects=[];relations=[];drawings=[]
            with zipfile.ZipFile(path) as package:
                for name in sorted(package.namelist()):
                    if name.endswith('.rels'):
                        for rel in ET.fromstring(package.read(name)):
                            relations.append({'part':name,**rel.attrib})
                    if name.startswith(('word/media/','word/embeddings/')) and not name.endswith('/'):
                        content=package.read(name);sha=hashlib.sha256(content).hexdigest()
                        dest=work/'docx_parts'/sid/(sha+Path(name).suffix)
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        if not dest.exists():dest.write_bytes(content)
                        objects.append({'part':name,'sha256':sha,'bytes':len(content),'file':dest.relative_to(work).as_posix(),
                                         'kind':'OLE_EMBEDDED' if '/embeddings/' in name else 'ORIGINAL_MEDIA','executed':False})
                    if name.startswith('word/') and name.endswith('.xml'):
                        content=package.read(name)
                        for ordinal,node in enumerate(ET.fromstring(content).iter()):
                            if node.tag.rsplit('}',1)[-1] in ('drawing','pict','object'):
                                serial=ET.tostring(node,encoding='utf8')
                                target=work/'docx_parts'/sid/(f'drawing_{len(drawings):05}.xml')
                                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(serial)
                                drawings.append({'part':name,'xml_node_ordinal':ordinal,'file':target.relative_to(work).as_posix(),
                                                 'sha256':hashlib.sha256(serial).hexdigest()})
            manifest={'source_id':sid,'source_sha256':checked[sid],'objects':objects,'relationships':relations,'drawings':drawings,
                      'page_context':'DOCX_LAYOUT_DEPENDENT; original part and paragraph locators used; page number UNKNOWN',
                      'verification':'AUTO_EXTRACTED_UNREVIEWED'}
            dest=work/'docx_parts'/sid/'manifest.json';write_json(dest,manifest);parts[sid]=dest.relative_to(work).as_posix()
        if sid in parts:item['docx_package_manifest']=parts[sid]
        page_id=item.get('page_id')
        if path.suffix.lower()!='.pdf' or not page_id:continue
        if page_id not in pages:
            if sid not in documents:documents[sid]=fitz.open(path)
            index=int(page_id.rsplit(':p',1)[-1]);page=documents[sid][index-1];scale=150/72
            pix=page.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False)
            dest=work/'pages'/(page_id.replace(':','_')+'.png');dest.parent.mkdir(exist_ok=True);pix.save(dest)
            pages[page_id]={'source_id':sid,'source_sha256':checked[sid],'page_id':page_id,'file':dest.relative_to(work).as_posix(),
                           'sha256':digest(dest),'dpi':150,'purpose':'WHOLE_PAGE_CONTEXT_ONLY',
                           'pixel_to_rotated_page_pt':[1/scale,0,0,1/scale,pix.x/scale,pix.y/scale],
                           'page_rotation':page.rotation,'width':pix.width,'height':pix.height}
        item['whole_page']=pages[page_id]
    for doc in documents.values():doc.close()
    write_json(work/'inventory.json',inventory)
    write_json(work/'context_manifest.json',{'schema':'vkm.figure_context/1','pages':pages,'docx_packages':parts,'checked_sources':checked,
          'inventory_sha256':digest(work/'inventory.json'),'status':'AUTO_EXTRACTED_UNREVIEWED'})
    print('context pages',len(pages),'DOCX packages',len(parts))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--work',required=True);ap.add_argument('--resources',required=True)
    a=ap.parse_args();enrich(Path(a.work),Path(a.resources))
