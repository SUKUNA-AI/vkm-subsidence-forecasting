"""Prepare source-preserving figure/cell/tile jobs; never invent a coordinate system."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from pathlib import Path

import pymupdf as fitz
import numpy as np
from PIL import Image


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=list), encoding='utf-8')


def serial(value):
    if isinstance(value, (fitz.Point, fitz.Rect, fitz.Quad, fitz.Matrix)):
        return list(value)
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serial(v) for v in value]
    return value


def clusters(indices):
    groups = []
    for v in indices:
        if not groups or v - groups[-1][-1] > 2:
            groups.append([int(v)])
        else:
            groups[-1].append(int(v))
    return [int(round(np.mean(g))) for g in groups]


def table_grid(im, include_black=False):
    import cv2
    rgb = np.asarray(im.convert('RGB'))
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Excel grid is grey and achromatic; black text is deliberately excluded.
    achromatic = rgb.max(axis=2).astype(int) - rgb.min(axis=2).astype(int) < 12
    mask = (((grey >= 0) if include_black else (grey > 140)) & (grey < 245) & achromatic).astype('uint8') * 255
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, max(15, im.width // 5)), 'uint8'))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((max(15, im.height // 3), 1), 'uint8'))
    ys = clusters(np.where((horizontal > 0).sum(axis=1) > im.width * .45)[0])
    xs = clusters(np.where((vertical > 0).sum(axis=0) > im.height * .35)[0])
    if not xs or xs[0] > 3:
        xs.insert(0, 0)
    if not xs or im.width - xs[-1] > 3:
        xs.append(im.width - 1)
    if not ys or ys[0] > 3:
        ys.insert(0, 0)
    if not ys or im.height - ys[-1] > 3:
        ys.append(im.height - 1)
    return xs, ys


def make_crop(work, image, parent, box, key, kind, extra=None, scale=3):
    crop = image.crop(box).resize(((box[2]-box[0])*scale, (box[3]-box[1])*scale), Image.Resampling.LANCZOS)
    dst = work / 'crops' / (key + '.png')
    dst.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dst)
    rec = {'key': key, 'figure_key': parent['key'], 'source_id': parent['source_id'],
           'file': dst.relative_to(work).as_posix(), 'sha256': digest(dst), 'kind': kind,
           'source_image_sha256': parent['image_sha256'], 'box_image_px': list(box),
           'crop_to_image': [1/scale, 0, 0, 1/scale, box[0], box[1]],
           'image_to_page': parent.get('image_to_page'), 'locator': parent['locator'],
           'verification_status': 'AUTO_EXTRACTED_UNREVIEWED', **(extra or {})}
    return rec


def prepare(args):
    work = Path(args.work).resolve()
    repo = Path(__file__).resolve().parents[2]
    base = work.parent
    resources = Path(args.resources).resolve()
    with open(resources / '00_registry/SOURCE_REGISTER.csv', encoding='utf-8-sig') as f:
        register = {r['resource_id']: r for r in csv.DictReader(f)}
    jobs_v1 = json.loads((base / 'jobs.json').read_text(encoding='utf-8'))
    raw = []
    for path in sorted(work.glob('inventory_search_raw*.json')):
        raw.extend(json.loads(path.read_text(encoding='utf-8')))
    candidates = {}
    search_receipt = []
    for query in raw:
        new = 0
        for item in query['result'].get('items') or []:
            env = item['envelope']
            if env['object_id'] not in candidates:
                candidates[env['object_id']] = item
                new += 1
        search_receipt.append({'query': query['query'], 'hits': len(query['result'].get('items') or []), 'new': new})
    # Reuse all original figure objects from v1; original bytes are retained, not screenshots of the report.
    inventory = []
    checked = {}
    for j in ([] if args.append else jobs_v1):
        image_path = base / j['sent_file']
        if not image_path.exists():
            continue
        rec = {'key': j['key'], 'source_id': j['source_id'], 'document': j.get('container'),
               'caption': j.get('caption'), 'locator': j['locator'], 'source_object_id': j['locator'].get('object_ids'),
               'source_sha256': register.get(j['source_id'], {}).get('sha256', 'UNKNOWN'),
               'source_image': image_path.relative_to(repo).as_posix(), 'image_sha256': digest(image_path),
               'native_format': j.get('original_format'), 'width': j['width'], 'height': j['height'],
               'type': 'UNKNOWN', 'review_status': 'AUTO_EXTRACTED_UNREVIEWED', 'represented_date': 'UNKNOWN',
               'mine_attribution': 'UNREVIEWED', 'scope': register.get(j['source_id'], {}).get('evidence_scope', 'UNKNOWN'),
               'coordinate_grid': 'UNKNOWN', 'scale': 'UNKNOWN', 'orientation': 'UNKNOWN', 'landmarks': [],
               'shown_entities': [], 'temporal_information': 'UNKNOWN', 'quality': 'UNREVIEWED',
               'geometry_usefulness': 'UNREVIEWED', 'conflicts': [], 'image_to_page': None}
        v1class = base / 'raw/qwen/CLASSIFY' / (j['key']+'.json')
        if v1class.exists():
            rec['type'] = json.loads(v1class.read_text(encoding='utf-8')).get('type', 'UNKNOWN')
        if j.get('original_file') and (base / j['original_file']).exists():
            rec['original_object'] = (base / j['original_file']).relative_to(repo).as_posix()
            rec['original_sha256'] = digest(base / j['original_file'])
        inventory.append(rec)
    if args.append:
        inventory = json.loads((work/'inventory.json').read_text(encoding='utf-8'))
    old_keys = {x['key'] for x in inventory} if args.append else set()
    old_ids = {x['source_object_id'] for x in inventory if isinstance(x.get('source_object_id'),str)}
    page_documents = {}
    for oid, item in sorted(candidates.items()):
        if oid in old_ids:
            continue
        env, hit = item['envelope'], item['record']
        sid = env['source_id']
        if not sid in register or not (env.get('geometry') or {}).get('bbox'):
            continue
        page_id = env['page_id']
        if ':p' not in page_id:
            continue  # DOCX uses original parts already inventoried above.
        src = resources / register[sid]['canonical_path']
        if src.suffix.lower() != '.pdf':
            continue  # retained separately as format-specific discovery candidates.
        if sid not in checked:
            checked[sid] = digest(src)
            if checked[sid] != register[sid]['sha256']:
                raise ValueError(sid + ': source hash mismatch')
            page_documents[sid] = fitz.open(src)
        doc = page_documents[sid]
        page = doc[env['page_index']-1]
        key = 'I' + hashlib.sha256(oid.encode()).hexdigest()[:16]
        clip = fitz.Rect(env['geometry']['bbox']) & page.rect
        if clip.is_empty:
            continue
        imgfile = work / 'renders' / (key+'.png')
        imgfile.parent.mkdir(parents=True, exist_ok=True)
        # 600 dpi, capped only by a declared pixel budget; actual DPI is recorded.
        scale = min(args.dpi / 72, (args.max_pixels / max(1, clip.width*clip.height))**.5)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        pix.save(imgfile)
        native_clip = clip * page.derotation_matrix
        words = page.get_text('words', clip=native_clip)
        paths = [d for d in page.get_drawings(extended=True)
                 if d.get('type') in ('clip', 'group') or not (fitz.Rect(d['rect']) & native_clip).is_empty]
        embedded = []
        for xref, smask, *_ in page.get_images(full=True):
            placements = page.get_image_rects(xref, transform=True)
            if not any(not (rect & native_clip).is_empty for rect, _ in placements):
                continue
            obj = doc.extract_image(xref)
            blob_hash = hashlib.sha256(obj['image']).hexdigest()
            obj_path = work/'embedded'/(blob_hash+'.'+obj['ext'])
            obj_path.parent.mkdir(parents=True, exist_ok=True)
            if not obj_path.exists():
                obj_path.write_bytes(obj['image'])
            masks = []
            if smask:
                mask = doc.extract_image(smask)
                mask_hash = hashlib.sha256(mask['image']).hexdigest()
                mask_path = work/'embedded'/(mask_hash+'.'+mask['ext'])
                if not mask_path.exists():
                    mask_path.write_bytes(mask['image'])
                masks.append({'xref':smask,'file':mask_path.relative_to(work).as_posix(),'sha256':mask_hash})
            embedded.append({'xref':xref,'file':obj_path.relative_to(work).as_posix(),'sha256':blob_hash,
                             'placements':serial(placements),'masks':masks})
        # Retain all drawing state (clips/groups) for downstream visible-path extraction.
        native = {'source_id':sid, 'key':key, 'width':pix.width, 'height':pix.height,
                  'source_sha256': checked[sid], 'object_id': oid, 'page_id': page_id,
                  'bbox_page_pt': list(clip), 'bbox_native_pt': list(native_clip),
                  'page_rotation_matrix': list(page.rotation_matrix),
                  'page_derotation_matrix': list(page.derotation_matrix),
                  'rotation': page.rotation, 'page_rect': list(page.rect),
                  'words': serial(words), 'drawings': serial(paths),
                  'image_xobjects': embedded,
                  'coordinate_system': 'PDF_UNROTATED_PT', 'review_status': 'AUTO_EXTRACTED_UNREVIEWED'}
        write_json(work/'native'/ (key+'.json'), native)
        rec = {'key': key, 'source_id': sid, 'document': src.name, 'page_id': page_id,
               'figure_number': 'UNKNOWN', 'caption': hit.get('caption') or hit.get('title_or_caption'), 'document_date': 'UNKNOWN',
               'represented_date': 'UNKNOWN', 'type': env['object_kind'], 'mine_attribution': 'UNREVIEWED',
               'scope': env.get('source_scope'), 'raster_vector_mixed': 'MIXED' if paths and page.get_images() else 'VECTOR' if paths else 'RASTER',
               'coordinate_grid': 'UNKNOWN', 'scale': 'UNKNOWN', 'orientation': 'UNKNOWN', 'landmarks': [],
               'shown_entities': [], 'temporal_information': 'UNKNOWN', 'quality': env.get('quality_flags', []),
               'geometry_usefulness': 'UNREVIEWED', 'verification_status': 'AUTO_EXTRACTED_UNREVIEWED', 'conflicts': [],
               'source_object_id': oid, 'source_sha256': checked[sid], 'source_image': imgfile.relative_to(repo).as_posix(),
               'image_sha256': digest(imgfile), 'width': pix.width, 'height': pix.height,
               'native_object': (work/'native'/(key+'.json')).relative_to(repo).as_posix(),
               'locator': {'page_id': page_id, 'figure_id': oid, 'bbox_pt_tl': list(clip),
                           'renderer': 'PyMuPDF', 'version': fitz.VersionBind, 'effective_dpi': scale*72},
               'image_to_page': [1/scale, 0, 0, 1/scale, pix.x/scale, pix.y/scale], 'rotation': page.rotation}
        inventory.append(rec)
        print('native', key, sid, pix.width, pix.height, len(words), len(paths), flush=True)
    jobs = json.loads((work/'jobs_v2.json').read_text(encoding='utf-8')) if args.append else []
    grids = json.loads((work/'grids.json').read_text(encoding='utf-8')) if args.append else {}
    for rec in inventory:
        if rec['key'] in old_keys:
            continue
        im = Image.open(repo/rec['source_image']).convert('RGB')
        key = rec['key']
        if rec['type'] == 'TABLE_SCREENSHOT' and key not in ('F023_007', 'F023_031'):
            black = key == 'F023_016'
            xs, ys = table_grid(im, include_black=black)
            grids[key] = {'x': xs, 'y': ys, 'method': 'BLACK_RULED_GRID' if black else 'ACHROMATIC_EXCEL_GRID', 'verification': 'AUTO_EXTRACTED_UNREVIEWED'}
            if len(xs) >= 3 and len(ys) >= 3:
                for r, (y0,y1) in enumerate(zip(ys,ys[1:])):
                    if y1-y0 < 8:
                        continue
                    for c, (x0,x1) in enumerate(zip(xs,xs[1:])):
                        if x1-x0 < 12:
                            continue
                        box = (x0+1,y0+1,x1,y1)
                        grid_key = key + ('_black' if black else '')
                        jobs.append(make_crop(work, im, rec, box, f'{grid_key}_r{r:03d}_c{c:02d}', 'CELL',
                                              {'row': r, 'column': c, 'edge_truncated': y1 >= im.height-2},scale=3))
        # Native text is preserved as exact objects; model tiles serve raster/mixed semantics.
        if key.startswith('I') and rec.get('native_object'):
            native = json.loads((repo/rec['native_object']).read_text(encoding='utf-8'))
            if len(native['words']) >= 25 and not native['image_xobjects']:
                continue
        side, step = args.tile_side, int(args.tile_side*(1-args.overlap))
        for y in range(0, max(1, im.height-side+step), step):
            for x in range(0, max(1, im.width-side+step), step):
                box = (x,y,min(x+side,im.width),min(y+side,im.height))
                jobs.append(make_crop(work,im,rec,box,f'{key}_t{x:05d}_{y:05d}','TILE',scale=2 if im.width<1500 else 1))
    write_json(work/'inventory.json', inventory)
    write_json(work/'jobs_v2.json', jobs)
    write_json(work/'grids.json', grids)
    receipt_name = 'prepare_append_receipt.json' if args.append else 'prepare_receipt.json'
    write_json(work/receipt_name, {'schema':'vkm.figure_preparation/1','images':len(inventory),
            'append_only_new_inputs':args.append,'new_images':len(inventory)-len(old_keys),
            'jobs':len(jobs),'cells':sum(j['kind']=='CELL' for j in jobs),'searches':search_receipt,
            'checked_sources':checked,'outputs':{n:digest(work/n) for n in ('inventory.json','jobs_v2.json','grids.json')},
            'methods':{'native':'PyMuPDF extended drawing state + words + XObjects','tile_overlap':args.overlap,
                       'tile_side':args.tile_side,'raster_upscale':'Lanczos; no new source information'},
            'status':'AUTO_EXTRACTED_UNREVIEWED'})
    for doc in page_documents.values():
        doc.close()
    print('prepared', len(inventory), 'figures',len(jobs),'jobs',flush=True)


if __name__ == '__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--work',required=True)
    ap.add_argument('--resources',required=True)
    ap.add_argument('--dpi',type=int,default=600)
    ap.add_argument('--max-pixels',type=int,default=30000000)
    ap.add_argument('--tile-side',type=int,default=640)
    ap.add_argument('--overlap',type=float,default=.2)
    ap.add_argument('--append',action='store_true',help='Append new search candidates; preserve existing crops/jobs/raw')
    prepare(ap.parse_args())
