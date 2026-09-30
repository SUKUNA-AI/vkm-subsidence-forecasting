"""Validate accepted datasets and prepare a source-first independent review package.

All literal readings, coordinates and reviewer interpretations stay in ignored work.
Only aggregate, source-free receipts may be copied to the public documentation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8',newline='\n')


def queue_closure(queue, decisions):
    expected={row['id'] for row in queue}
    seen={}; records=[]
    for dataset,rows in decisions.items():
        for row in rows:
            identifier=row.get('case_id',row.get('id'))
            if identifier not in expected:raise ValueError(f'unknown original queue case: {identifier}')
            if identifier in seen:raise ValueError(f'duplicate original queue case: {identifier}')
            if not row.get('reason'):raise ValueError(f'case has no reason: {identifier}')
            seen[identifier]=dataset
            records.append({'case_id':identifier,'dataset':dataset,'decision':row})
    missing=expected-set(seen)
    if missing:raise ValueError(f'unprocessed queue cases: {sorted(missing)}')
    return {'schema':'vkm.sol_queue_closure/1','original_cases':len(queue),'processed_cases':len(seen),
            'by_dataset':dict(Counter(seen.values())),'records':records}


def dataset_summary(dataset):
    records=dataset['records']; identifiers=[row['record_id'] for row in records]
    if len(identifiers)!=len(set(identifiers)):raise ValueError('duplicate accepted record id')
    groups={status:[] for status in ('ACCEPTED','CONDITIONAL','UNRESOLVED')}
    for row in records:
        status=row['acceptance']
        if status not in groups:raise ValueError('unrecognised acceptance status')
        if not row.get('source_id'):raise ValueError('record lacks source identity')
        if not row.get('verification_method'):raise ValueError('record lacks verification method')
        if row.get('admitted_to_evidence',False):raise ValueError('this package cannot silently admit evidence')
        groups[status].append(row['record_id'])
    return {'record_count':len(records),'counts':{status:len(ids) for status,ids in groups.items()},
            'partitions':groups}


CONTEXT_FIELDS=('case_id','priority','source_id','page_id','figure','problem_type',
                'downstream_impact','full_image','exact_crop','neighboring_crops',
                'related_feature_ids','related_gcp_ids','related_zone_ids')
INTERPRETATION_FIELDS=('extracted_value_or_geometry','sol_interpretation','alternatives',
                       'confidence','why_normal_methods_failed','normal_methods_attempted')


def split_review_case(case):
    for field in CONTEXT_FIELDS+INTERPRETATION_FIELDS:
        if field not in case:raise ValueError(f'review case lacks {field}')
    if case['priority'] not in ('P0','P1','P2','P3'):raise ValueError('invalid review priority')
    if not case['why_normal_methods_failed'] or not case['normal_methods_attempted']:
        raise ValueError('ordinary methods must be exhausted before independent review')
    if not case['alternatives']:raise ValueError('independent review requires explicit alternatives')
    return ({field:case[field] for field in CONTEXT_FIELDS},
            {'case_id':case['case_id'],**{field:case[field] for field in INTERPRETATION_FIELDS}})


def checked_reference(repo,ref):
    if not ref.get('path') or not ref.get('sha256'):raise ValueError('source reference must have path and SHA-256')
    path=(repo/ref['path']).resolve()
    if not path.is_relative_to(repo/'work'):raise ValueError('review artifacts must remain in PRIVATE work')
    if sha(path)!=ref['sha256']:raise ValueError('review source/crop changed')


def prepare_astra(repo,out,cases):
    ids=[case['case_id'] for case in cases]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate independent review case')
    index=[];manifest=[]
    for case in sorted(cases,key=lambda row:(row['priority'],row['case_id'])):
        context,interpretation=split_review_case(case)
        identifier=case['case_id']
        if any(ch not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for ch in identifier):
            raise ValueError('unsafe review case filename')
        for ref in [case['full_image'],case['exact_crop'],*case['neighboring_crops']]:checked_reference(repo,ref)
        source_file=out/'source_context'/f'{identifier}.json'
        decision_file=out/'sol_decisions'/f'{identifier}.json'
        write(source_file,context);write(decision_file,interpretation)
        entry={'case_id':identifier,'priority':case['priority'],'source_id':case['source_id'],
               'page_id':case['page_id'],'source_context':{'path':source_file.relative_to(repo).as_posix(),'sha256':sha(source_file)}}
        index.append(entry)
        manifest.append({**case,**entry,'sol_decision':{'path':decision_file.relative_to(repo).as_posix(),'sha256':sha(decision_file)}})
    write(out/'source_first_index.json',{'schema':'vkm.astra_source_first/1','cases':index,
          'protocol':['Open source context and images without Sol decisions.',
                      'Record independent interpretation and unresolved alternatives.',
                      'Open the separate Sol decision and compare.',
                      'Record PASS, CORRECT or UNRESOLVED; do not rewrite the frozen extraction.']})
    write(out/'manifest.json',{'schema':'vkm.astra_review_package/1','case_count':len(cases),
          'priority_counts':dict(Counter(row['priority'] for row in cases)),'cases':manifest,
          'entry_point':'source_first_index.json','sol_decisions_hidden_in_entry_point':True})


def main(args):
    repo=Path.cwd().resolve();root=(repo/args.work).resolve();out=root/'accepted'
    if not root.is_relative_to(repo/'work'):raise ValueError('PRIVATE package must remain in ignored work')
    paths={'A':root/'readings/A_ACCEPTED.json','B':root/'geometry/B_ACCEPTED.json','C':root/'geology/C_ACCEPTED.json'}
    summaries={name:dataset_summary(load(path)) for name,path in paths.items()}
    for name,path in paths.items():
        dataset=load(path); write(out/f'{name}_ACCEPTED.json',dataset)
        for status in ('ACCEPTED','CONDITIONAL','UNRESOLVED'):
            write(out/name/f'{status}.json',{'schema':'vkm.sol_abc_partition/1','dataset':name,
                  'acceptance':status,'records':[row for row in dataset['records'] if row['acceptance']==status],
                  'parent_sha256':sha(path)})
    decisions={}
    for name,directory in [('A','readings'),('B','geometry'),('C','geology')]:
        decisions[name]=load(root/directory/'resolved_review_cases.json')['records']
    queue=load(repo/'work/figure_readings_2026-09-29/v2/ASTRA_REVIEW_REQUIRED.json')['items']
    closed=queue_closure(queue,decisions);write(root/'queue_closure.json',closed)
    retry=load(root/'retries/closure.json')
    if retry['truncation_cases_closed']!=55:raise ValueError('not all 55 original GLM truncations closed')
    coverage=load(root/'coverage/receipt.json')
    if coverage['sources']!=271 or coverage['pages_screened']!=27135:raise ValueError('coverage does not match current snapshot')
    frozen=load(root/'frozen_integrity.json')
    if frozen['status']!='PASS' or not frozen['checked_file_count']:
        raise ValueError('frozen byte verification must pass before acceptance packaging')
    cases=[]
    for directory in ('geometry','geology'):
        path=root/directory/'astra_residual_cases.json'
        if path.exists():cases.extend(load(path)['cases'])
    prepare_astra(repo,root/'ASTRA_REVIEW_PACKAGE',cases)
    inputs={path.relative_to(repo).as_posix():sha(path) for path in paths.values()}
    for path in [root/'queue_closure.json',root/'retries/closure.json',root/'coverage/receipt.json',root/'frozen_integrity.json']:
        inputs[path.relative_to(repo).as_posix()]=sha(path)
    receipt={'schema':'vkm.sol_abc_completion_receipt/1','datasets':summaries,
        'original_queue':{'cases':len(queue),'processed':closed['processed_cases'],'by_dataset':closed['by_dataset']},
        'glm_retry_closure':{key:retry[key] for key in ('truncation_cases_closed','resolution_counts','reader_attempt_count')},
        'coverage':{key:coverage[key] for key in ('sources','pages_screened','source_sha_counts','status_counts','exhaustive_figure_recall_proven')},
        'astra_review':{'case_count':len(cases),'priorities':dict(Counter(row['priority'] for row in cases))},
        'frozen_integrity':{'status':frozen['status'],'checked_file_count':frozen['checked_file_count']},
        'inputs':inputs,'raw_frozen_unchanged':True,'admitted_to_evidence':False,
        'command':'python -B -m benchmarks.abc_completion_v1.package'}
    write(root/'completion_receipt.json',receipt)
    print(json.dumps({key:receipt[key] for key in ('original_queue','glm_retry_closure','astra_review')},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',default='work/abc_completion_2026-09-30')
    main(parser.parse_args())
