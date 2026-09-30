"""Assemble source-linked PRIVATE readings without promoting machine output to evidence."""
from __future__ import annotations
import argparse, collections, csv, json, math
from pathlib import Path
import pymupdf as fitz
from prepare import digest, write_json
from read_local import parse_glm_text
from evaluate import wilson

def answer(work, reader, job):
    path=work/'raw'/reader/(job['key']+'.json')
    if not path.exists():return None
    value=json.loads(path.read_text(encoding='utf8'))
    if value.get('input_sha256')!=job['sha256']:raise ValueError('READER_INPUT_MISMATCH:'+job['key'])
    if value.get('error'):return {'status':'READER_ERROR','raw_file':path.relative_to(work).as_posix()}
    parsed=value.get('parsed') or {}
    text=parse_glm_text(value.get('content','')) if reader=='glm' else parsed.get('text')
    return {'text':text,'parsed':parsed,'read_status':value.get('read_status'),'finish_reason':value.get('finish_reason'),
            'status':'AUTO_EXTRACTED_UNREVIEWED','raw_file':path.relative_to(work).as_posix(),'raw_sha256':digest(path)}

def assemble(work: Path):
    work=work.resolve();repo=Path(__file__).resolve().parents[2]
    inventory=json.loads((work/'inventory.json').read_text(encoding='utf8'))
    jobs=json.loads((work/'jobs_v2.json').read_text(encoding='utf8'));byfigure=collections.defaultdict(list)
    for job in jobs:byfigure[job['figure_key']].append(job)
    manuals={};manual_files={};extra=[]
    for directory in ('review_root','review_root_other','review_sys'):
        path=work/directory/'review.json'
        if not path.exists():continue
        value=json.loads(path.read_text(encoding='utf8'));manual_files[path.relative_to(work).as_posix()]=digest(path)
        for raw in value.get('records',value.get('units',[])):
            unit=dict(raw);key=unit.get('job_key',unit.get('key'));unit['manual_file']=path.relative_to(work).as_posix()
            unit['literal_text']=unit.get('literal_text',unit.get('literal'))
            unit['read_status']=unit.get('read_status',unit.get('status',unit.get('verification_status')))
            unit['reviewer']=unit.get('reviewer',value.get('reviewer','UNKNOWN'))
            manuals[key]=unit
            if directory!='review_root':extra.append(unit)
    accuracy=json.loads((work/'accuracy.json').read_text(encoding='utf8'))
    gold={(r['figure_key'],r['row'],r['column']):r for r in accuracy['cells']}
    gold_excluded={(r['figure_key'],r['row'],r['column']) for r in accuracy['excluded']}
    queue=[];output_files=[];kind_counts=collections.Counter();reader_counts={r:collections.Counter() for r in ('qwen','glm')}
    extra_comparisons=[];final_counts=collections.Counter();classification_counts=collections.Counter()
    def enqueue(item,job,question,options,impact,reason):
        crop={'file':job['file'],'sha256':job['sha256'],'bbox_source_image_px':job['box_image_px'],'crop_to_image':job['crop_to_image']} if job else {
            'file':item['source_image'],'sha256':item['image_sha256'],'bbox_source_image_px':[0,0,item['width'],item['height']],
            'crop_to_image':[1,0,0,1,0,0]}
        queue.append({'id':'AR-'+str(len(queue)+1).zfill(5),'source_id':item['source_id'],'figure_key':item['key'],
            'locator':job['locator'] if job else item['locator'],'source_sha256':item['source_sha256'],
            'source_image':{'file':item['source_image'],'sha256':item['image_sha256']},'whole_page':item.get('whole_page'),
            'crop':crop,'question':question,'options':options,'uncertainty':reason,'impact_on_geometry_or_worldspec':impact,
            'status':'ASTRA_REVIEW_REQUIRED','admitted_to_evidence':False})
    for item in inventory:
        assert digest(repo/item['source_image'])==item['image_sha256']
        records=[];labels=[];fragments=[];classes=[]
        for job in byfigure[item['key']]:
            if digest(work/job['file'])!=job['sha256']:raise ValueError('CROP_HASH_MISMATCH:'+job['key'])
            kind_counts[job['kind']]+=1
            readers={r:answer(work,r,job) for r in ('qwen','glm')}
            for r,value in readers.items():reader_counts[r][job['kind']+':'+(value.get('read_status') or value.get('status','UNKNOWN')) if value else job['kind']+':MISSING']+=1
            if job['kind']=='CLASSIFY':
                classification=(readers['qwen'] or {}).get('parsed',{});classes.append(classification)
                if classification:classification_counts[str(classification.get('type','UNKNOWN'))]+=1
                continue
            base={'job_key':job['key'],'crop_file':job['file'],'crop_sha256':job['sha256'],'source_image_sha256':item['image_sha256'],
                  'bbox_image_px':job['box_image_px'],'crop_to_image':job['crop_to_image'],'image_to_page':job.get('image_to_page'),
                  'locator':job['locator'],'readers':readers,'epistemic_status':'DERIVATION','physical_units':'UNKNOWN',
                  'admitted_to_evidence':False,'verification_status':'AUTO_EXTRACTED_UNREVIEWED'}
            if job['kind'] in ('CELL','LABEL'):
                cell=(item['key'],job.get('row'),job.get('column'));manual=manuals.get(job['key']);reference=gold.get(cell)
                literal=(readers['glm'] or {}).get('text');status='AUTO_EXTRACTED_UNREVIEWED';provenance='GLM_OCR'
                if cell in gold_excluded:literal=None;status='SOURCE_TRUNCATED';provenance='EXISTING_GOLD_EXCLUSION'
                elif reference:
                    literal=reference['gold'];status='VERIFIED_BY_EYE_EXISTING_GOLD';provenance='INHERITED_PRIOR_EYE_REVIEW'
                    base['gold_reference']={'file':item['key']+'_claude.csv','sha256':accuracy['gold_hashes'][item['key']+'_claude.csv'],
                                            'column':reference['gold_column'],'not_new_independent_accuracy':True}
                if manual:
                    if manual.get('input_sha256',manual.get('crop_sha256'))!=job['sha256']:raise ValueError('MANUAL_HASH_MISMATCH:'+job['key'])
                    literal=manual['literal_text'];status=manual['read_status'];provenance=manual['reviewer'];base['manual_review']=manual
                    if status in ('VERIFIED_BY_ROOT_EYE','VERIFIED_BY_EYE') or status.startswith('READABLE') or status=='BLANK_VISIBLE_CELL':status='VERIFIED_BY_EYE'
                base.update(row=job.get('row'),column=job.get('column'),literal_text=literal,verification_status=status,chosen_provenance=provenance)
                values=[v.get('text') for v in readers.values() if v and v.get('text') is not None]
                base['reader_disagreement']=len(values)==2 and values[0].strip()!=values[1].strip()
                records.append(base);final_counts[status]+=1
                if status not in ('VERIFIED_BY_EYE','VERIFIED_BY_EYE_EXISTING_GOLD','NOT_A_CELL'):
                    enqueue(item,job,'Какой текст полностью виден? Отделить обрезание источника и интерфейс от значения; не восстанавливать скрытый хвост.',
                            list(dict.fromkeys([v for v in [literal,*values] if v is not None])),
                            'Не использовать непроверенное число, ID или единицу в привязке, временной истории и WorldSpec.',status)
                elif base['reader_disagreement'] and not (manual or reference):
                    enqueue(item,job,'Проверить буквальное чтение по пикселям.',values,'Блокирует использование значения в геометрии.','READER_DISAGREEMENT')
                if job['key'] in {u.get('job_key',u.get('key')) for u in extra}:
                    unit=manuals[job['key']];eligible=unit['read_status'].startswith('READABLE') or unit['read_status']=='BLANK_VISIBLE_CELL'
                    glm=readers['glm'];prediction=glm.get('text') if glm else None
                    extra_comparisons.append({'job_key':job['key'],'eligible_literal':eligible,'manual_status':unit['read_status'],
                        'manual_text':unit['literal_text'],'glm_text':prediction,'exact':eligible and prediction is not None and prediction.strip()==(unit['literal_text'] or '').strip(),
                        'covered':glm is not None and glm.get('finish_reason')!='length','sampling':'MIXED_CONDITIONAL_SOURCE_SELECTION'})
            else:
                for r,value in readers.items():
                    if value:fragments.append({**base,'reader':r,'literal_text':value.get('text') if r=='glm' else None,
                                              'parsed_unreviewed':value.get('parsed'),'raw_file':value['raw_file']})
                parsed=(readers['qwen'] or {}).get('parsed',{})
                for raw_label in parsed.get('labels',[]) if isinstance(parsed.get('labels'),list) else []:
                    if not isinstance(raw_label,dict):continue
                    box=raw_label.get('bbox');valid=isinstance(box,list) and len(box)==4 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in box)
                    if valid:
                        a,b,c,d,e,f=job['crop_to_image'];rect=[a*box[0]+c*box[1]+e,b*box[0]+d*box[1]+f,a*box[2]+c*box[3]+e,b*box[2]+d*box[3]+f]
                        valid=rect[0]<=rect[2] and rect[1]<=rect[3] and 0<=rect[0]<=rect[2]<=item['width'] and 0<=rect[1]<=rect[3]<=item['height']
                    labels.append({'text':raw_label.get('text'),'bbox_image_px':rect if valid else None,'bbox_model_original':box,
                        'center_image_px':[(rect[0]+rect[2])/2,(rect[1]+rect[3])/2] if valid else None,
                        'bbox_status':'MODEL_PROPOSED_UNREVIEWED' if valid else 'INVALID_MODEL_BBOX','source_job':job['key'],'locator':job['locator'],
                        'verification_status':'AUTO_EXTRACTED_UNREVIEWED','physical_units':'UNKNOWN','admitted_to_evidence':False})
        if item.get('native_object'):
            native=json.loads((repo/item['native_object']).read_text(encoding='utf8'));rotation=fitz.Matrix(*native['page_rotation_matrix']);inverse=~fitz.Matrix(*item['image_to_page'])
            for word in native['words']:
                rect=list(fitz.Rect(word[:4])*rotation*inverse)
                labels.append({'text':word[4],'bbox_image_px':rect,'center_image_px':[(rect[0]+rect[2])/2,(rect[1]+rect[3])/2],
                    'bbox_native_unrotated_pt':word[:4],'bbox_status':'NATIVE_TEXT_OBJECT','origin':'PDF_TEXT_LAYER',
                    'locator':item['locator'],'verification_status':'AUTO_EXTRACTED_UNREVIEWED','physical_units':'UNKNOWN','admitted_to_evidence':False})
        figure={**item,'classifier_outputs_unreviewed':classes,'cells_and_labels':records,'text_fragments':fragments,
                'science_boundary':'Literal reading and graphic candidates only; not mine observations, evidence or WorldSpec.'}
        for directory,value in (('figures',figure),('labels',{'figure_key':item['key'],'labels':labels,'unstructured_ocr_fragments':fragments})):
            path=work/directory/(item['key']+'.json');write_json(path,value);output_files.append(path)
        if any(j['kind']=='CELL' for j in byfigure[item['key']]):
            path=work/'tables'/(item['key']+'.csv');path.parent.mkdir(exist_ok=True)
            with path.open('w',encoding='utf8',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=['job_key','row','column','literal_text','verification_status','chosen_provenance','crop_sha256','physical_units','admitted_to_evidence'])
                writer.writeheader();writer.writerows({k:r.get(k) for k in writer.fieldnames} for r in records)
            output_files.append(path)
        if any(c.get('type') in ('PLAN','MAP','SECTION','STRATIGRAPHIC_COLUMN','SCHEME') for c in classes):
            enqueue(item,None,'Проверить напечатанную атрибуцию рудника, семантику границ, подпись/полигон, единицы осей и представленную дату. Видимого номера недостаточно для скрытого GIS-ID.',
                    ['Соответствие явно напечатано','Только графический кандидат','UNKNOWN / нечитаемо'],
                    'Блокирует превращение графических линий в workings, GCP, picks или хронологию WorldSpec.','SEMANTIC_GEOMETRY_REVIEW')
    write_json(work/'ASTRA_REVIEW_REQUIRED.json',{'schema':'vkm.astra_review_queue/1','review_model':'Separate future strong cloud vision review',
        'no_new_model_invoked':True,'items':queue})
    eligible=[r for r in extra_comparisons if r['eligible_literal']];correct=sum(r['exact'] for r in eligible);n=len(eligible)
    extra_metrics={'reviewed_units':len(extra_comparisons),'eligible_literal_units':n,'rejected_or_partial_units':len(extra_comparisons)-n,
                  'glm_literal_correct':correct,'glm_literal_accuracy':correct/n if n else None,'literal_error_wilson95':wilson(n-correct,n),
                  'scope':'126 complete candidate-cell population from two non-gold screenshots +24 seeded native text crops. Not representative of corpus maps.',
                  'sampling_is_corpus_random':False,'table_99_percent_target_met':False,'map_97_percent_target':'NOT_ESTABLISHED'}
    write_json(work/'independent_extra_review_accuracy.json',{'metrics':extra_metrics,'comparisons':extra_comparisons,'manual_files':manual_files})
    receipt={'schema':'vkm.figure_readings_v2/2','date':'2026-09-30','inventory_objects':len(inventory),'source_ids':sorted({i['source_id'] for i in inventory}),
       'native_pdf_objects':sum(bool(i.get('native_object')) for i in inventory),'djvu_figures':sum(i.get('native_format')=='DJVU' for i in inventory),
       'whole_page_contexts':len({i['whole_page']['page_id'] for i in inventory if i.get('whole_page')}),
       'job_counts':dict(kind_counts),'active_reader_counts':{k:dict(v) for k,v in reader_counts.items()},'final_literal_status_counts':dict(final_counts),
       'classification_counts_unreviewed':dict(classification_counts),'gold_accuracy':accuracy['metrics'],'extra_review':extra_metrics,
       'manual_review_counts':{'root_gold_purposive':sum(u.get('manual_file')=='review_root/review.json' for u in manuals.values()),'other_source_units':len(extra_comparisons)},'astra_queue_count':len(queue),
       'input_hashes':{n:digest(work/n) for n in ('inventory.json','jobs_v2.json','accuracy.json')},'manual_review_hashes':manual_files,
       'output_manifest_hash':'PENDING','method':'Native PDF objects, unchanged DOCX parts, addressed DjVu decode, literal GLM bulk OCR; source pixels retained.',
       'current_owner_reader_decision':'GLM bulk only; Qwen historical answers retained, no further Qwen run.',
       'coverage_limit':'Addressed corpus navigation and local inventory; no proof that all relevant figures have been found. Semantic and physical registration remains gated.',
       'epistemic_status':'DERIVATION','verification_status':'MIXED_PER_RECORD; NO_EVIDENCE_PROMOTION',
       'goals':{'automatic_table_99_percent':'NOT_ACHIEVED','map_labels_97_percent':'NOT_ESTABLISHED','physical_crs':'UNKNOWN'}}
    write_json(work/'assembled_outputs_manifest.json',{'files':{p.relative_to(work).as_posix():digest(p) for p in output_files},
               'queue_sha256':digest(work/'ASTRA_REVIEW_REQUIRED.json'),'extra_review_sha256':digest(work/'independent_extra_review_accuracy.json')})
    receipt['output_manifest_hash']=digest(work/'assembled_outputs_manifest.json');write_json(work/'receipt_final.json',receipt)
    print(json.dumps({k:receipt[k] for k in ('inventory_objects','job_counts','astra_queue_count','extra_review')},ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--work',required=True);a=p.parse_args();assemble(Path(a.work))
