"""Materialize verified readings separately from immutable raw extraction."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8',newline='\n')


def acceptance_for(cell, decision):
    if decision is not None:
        acceptance=decision['acceptance']
        # A verified fragment does not establish the complete scientific value.
        if acceptance=='CONDITIONAL' and decision['value']=='UNKNOWN':
            acceptance='UNRESOLVED'
        return acceptance,decision['value'],decision['status'],decision['verification_method']
    status=cell['verification_status']
    if status in ('VERIFIED_BY_EYE','VERIFIED_BY_EYE_EXISTING_GOLD'):
        return 'ACCEPTED',cell.get('literal_text'),'VERIFIED_PRIOR_READING','INHERITED_HASHED_EYE_REVIEW'
    if status=='NOT_A_CELL':
        return 'ACCEPTED','UNKNOWN','SOURCE_UI_FRAGMENT_NOT_A_VALUE','INHERITED_HASHED_EYE_REVIEW'
    return 'UNRESOLVED','UNKNOWN',status,'NO_COMPLETED_SOURCE_VERIFICATION'


def main(args):
    repo=Path.cwd().resolve(); old=(repo/args.input).resolve(); out=(repo/args.out).resolve()
    if not out.is_relative_to(repo/'work'):raise ValueError('accepted literal data must remain PRIVATE')
    manual=out/'sol_value_decisions.json'; reviewed=json.loads(manual.read_text(encoding='utf-8'))
    decisions={row['crop']:row for row in reviewed['records']}
    if len(decisions)!=len(reviewed['records']):raise ValueError('duplicate reviewed crop')
    for row in reviewed['records']:
        context=repo/row['context_crop']
        if sha(context)!=row['context_sha256']:raise ValueError('review context changed')
    queue=json.loads((old/'ASTRA_REVIEW_REQUIRED.json').read_text(encoding='utf-8'))['items']
    queue_values=[case for case in queue if case['uncertainty']!='SEMANTIC_GEOMETRY_REVIEW']
    if {case['id'] for case in queue_values}!={row['case_id'] for row in reviewed['records']}:
        raise ValueError('not all original value cases have a Sol decision')
    records=[];used=set();inputs={manual.relative_to(repo).as_posix():sha(manual)}
    for path in sorted((old/'figures').glob('*.json')):
        fig=json.loads(path.read_text(encoding='utf-8'))
        inputs[path.relative_to(repo).as_posix()]=sha(path)
        source=repo/fig['source_image']
        if sha(source)!=fig['image_sha256']:raise ValueError('source image changed')
        for cell in fig['cells_and_labels']:
            crop=old/cell['crop_file']
            if sha(crop)!=cell['crop_sha256']:raise ValueError('source crop changed')
            key=crop.relative_to(repo).as_posix();decision=decisions.get(key)
            if decision:used.add(decision['case_id'])
            accepted,value,status,method=acceptance_for(cell,decision)
            if decision and decision['crop_sha256']!=cell['crop_sha256']:raise ValueError('review used wrong crop')
            record={'record_id':'A-'+cell['job_key'],'acceptance':accepted,'value':value,
                'literal_visible_text':decision['literal_visible_text'] if decision else cell.get('literal_text'),
                'reading_status':status,'source_id':fig['source_id'],'figure':fig['key'],
                'source_document_sha256':fig['source_sha256'],
                'page_id':fig.get('page_id','UNKNOWN'),'locator':cell['locator'],'row':cell.get('row'),'column':cell.get('column'),
                'verification_method':method,'uncertainty':list(decision.get('uncertainty',[])) if decision else [],
                'epistemic_status':'DERIVATION','physical_unit':'UNKNOWN','admitted_to_evidence':False,
                'source_image':{'path':fig['source_image'],'sha256':fig['image_sha256']},
                'crop':{'path':key,'sha256':cell['crop_sha256'],'crop_to_image':cell['crop_to_image']},
                'prior_record':{'path':path.relative_to(repo).as_posix(),'sha256':inputs[path.relative_to(repo).as_posix()]},
                'source_frame':{'id':fig['key'],'units':'px','coordinate_reference':'SOURCE_PIXELS_Y_DOWN'},
                'sol_interpretation':decision['sol_interpretation'] if decision else 'Verified literal reading; physical interpretation is separate',
                'reason':decision['reason'] if decision else 'Existing source-linked visual verification retained',
                'original_reader_outputs_unchanged':True}
            if decision:
                record['sol_decision_ref']={'path':manual.relative_to(repo).as_posix(),'sha256':sha(manual),'case_id':decision['case_id']}
                record['expanded_context']={'path':decision['context_crop'],'sha256':decision['context_sha256']}
                record['source_fragment_acceptance']=decision['acceptance']
            record['value_resolution']='UNRESOLVED_VALUE' if accepted=='UNRESOLVED' else ('VERIFIED_NO_VALUE' if value=='UNKNOWN' else 'VERIFIED_READING')
            if value=='UNKNOWN':record['uncertainty'].append(record['reason'])
            records.append(record)
    if used!={row['case_id'] for row in reviewed['records']}:raise ValueError('unmatched Sol value decision')
    result={'schema':'vkm.sol_abc_acceptance/1','dataset':'A_ACCEPTED','records':records,
            'acceptance_counts':dict(Counter(r['acceptance'] for r in records)),
            'scope':'Source readings and explicit source limits; no automatic evidence admission','frozen_inputs_unchanged':True}
    write(out/'A_ACCEPTED.json',result)
    write(out/'resolved_review_cases.json',{'schema':'vkm.sol_review_decisions/1','records':reviewed['records']})
    write(out/'receipt.json',{'schema':'vkm.sol_readings_receipt/1','inputs':inputs,
        'outputs':{p.relative_to(repo).as_posix():sha(p) for p in [out/'A_ACCEPTED.json',out/'resolved_review_cases.json']},
        'record_count':len(records),'original_value_queue':len(queue_values),'value_cases_processed':len(used),
        'acceptance_counts':result['acceptance_counts'],'command':'python -B benchmarks/abc_completion_v1/readings.py'})
    print(json.dumps({'records':len(records),'processed_queue':len(used),'acceptance':result['acceptance_counts']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',default='work/figure_readings_2026-09-29/v2')
    parser.add_argument('--out',default='work/abc_completion_2026-09-30/readings')
    main(parser.parse_args())
