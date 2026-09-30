"""Close extraction failures through recorded source review, without editing raw OCR."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from benchmarks.abc_completion_v1.retry_glm import digest, save


def close(batch, decisions):
    required={row['job_key'] for row in batch['records'] if row['status']=='SOURCE_REVIEW_REQUIRED'}
    reviewed={row['job_key']:row for row in decisions['records']}
    if len(reviewed)!=len(decisions['records']) or set(reviewed)!=required:
        raise ValueError('every remaining retry requires exactly one source decision')
    records=[]
    for item in batch['records']:
        decision=reviewed.get(item['job_key'])
        if decision:
            if decision['input_sha256']!=item['original_input_sha256']:
                raise ValueError('source decision crop mismatch')
            if decision['status'] not in ('VERIFIED_NO_TEXT_IN_TILE','SOURCE_LABELS_PARTLY_UNREADABLE'):
                raise ValueError('unsupported terminal source decision')
            if not decision['verification_method'] or not decision['reason']:
                raise ValueError('source adjudication needs a method and reason')
        records.append({**item,'resolution':decision or {'status':'OCR_RETRY_COMPLETE_UNREVIEWED'},
                        'truncation_case_closed':True,'admitted_to_evidence':False})
    return {'schema':'vkm.sol_glm_retry_closure/1','records':records,
            'truncation_cases_closed':len(records),'resolution_counts':dict(Counter(
                row['resolution']['status'] for row in records)),
            'reader_attempt_count':sum(len(row['attempts']) for row in records),
            'raw_ocr_unchanged':True,'ocr_completion_is_not_verification':True}


def main(args):
    repo=Path.cwd().resolve(); out=(repo/args.out).resolve()
    if not out.is_relative_to(repo/'work'):raise ValueError('literal review must remain PRIVATE')
    batch=out/'receipt.json'; decisions=out/'sol_source_decisions.json'
    result=close(json.loads(batch.read_text(encoding='utf-8')),
                 json.loads(decisions.read_text(encoding='utf-8')))
    result['inputs']={path.relative_to(repo).as_posix():digest(path) for path in (batch,decisions)}
    for row in result['records']:
        for ref in row['attempts']:
            if digest(repo/ref['file'])!=ref['sha256']:raise ValueError('immutable attempt changed')
        decision=row['resolution']
        for name in ('source_image','crop','native_object'):
            ref=decision.get(name)
            if ref and digest(repo/ref['path'])!=ref['sha256']:raise ValueError('review context changed')
    save(out/'closure.json',result)
    print(json.dumps({key:result[key] for key in ('truncation_cases_closed','resolution_counts','reader_attempt_count')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',default='work/abc_completion_2026-09-30/retries')
    main(parser.parse_args())
