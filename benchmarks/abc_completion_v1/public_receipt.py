"""Publish only allowlisted aggregates; never source readings or coordinates."""
from __future__ import annotations

import argparse
from pathlib import Path

from benchmarks.abc_completion_v1.package import load, sha, write


def aggregates(completion):
    return {'schema':'vkm.sol_abc_public_receipt/1','date':'2026-09-30',
        'snapshot':'snap-20260929T175107Z-574daaac',
        'datasets':{name:{'record_count':summary['record_count'],'counts':summary['counts']}
                    for name,summary in completion['datasets'].items()},
        'original_queue':completion['original_queue'],
        'glm_retry_closure':completion['glm_retry_closure'],
        'coverage':completion['coverage'],'astra_review':completion['astra_review'],
        'topology':completion.get('topology',{}),
        'frozen_integrity':completion['frozen_integrity'],
        'admitted_to_evidence':False,'raw_frozen_unchanged':True,
        'private_scientific_data_published':False,'new_qwen_calls':0,
        'core_edge_changed':False,'solver_ml_cad_runs':0}


def main(args):
    repo=Path.cwd().resolve();root=(repo/args.work).resolve();out=(repo/args.out).resolve()
    if not root.is_relative_to(repo/'work'):raise ValueError('full provenance must be read from PRIVATE work')
    if out.parent!=repo/'docs/corpus_platform/receipts':raise ValueError('publish only into public receipts')
    result=aggregates(load(root/'completion_receipt.json'))
    output_files={
        'completion':root/'completion_receipt.json','A_ACCEPTED':root/'accepted/A_ACCEPTED.json',
        'B_ACCEPTED':root/'accepted/B_ACCEPTED.json','C_ACCEPTED':root/'accepted/C_ACCEPTED.json',
        'queue_closure':root/'queue_closure.json','astra_manifest':root/'ASTRA_REVIEW_PACKAGE/manifest.json',
        'astra_source_first_index':root/'ASTRA_REVIEW_PACKAGE/source_first_index.json',
        'frozen_integrity':root/'frozen_integrity.json','GPU_release':root/'retries/gpu_release_receipt.json',
        'coverage_finalization':root/'coverage/screening_finalization_receipt.json',
        'topology_classification':root/'geometry/topology/classification.json',
        'topology_receipt':root/'geometry/topology/receipt.json',
        'topology_qgis_acceptance':root/'geometry/topology/qgis_acceptance.json'}
    result['private_output_hashes']={name:sha(path) for name,path in output_files.items()}
    result['code_hashes']={path.relative_to(repo).as_posix():sha(path)
                          for path in sorted((repo/'benchmarks/abc_completion_v1').glob('*.py'))}
    verification=load(root/'verification_summary.json')
    if verification['status'] not in ('PASS','PASS_WITH_DISCLOSED_LIMITATIONS'):
        raise ValueError('complete QA before publishing the final public receipt')
    result['verification']=verification
    write(out,result)
    print('Published source-free A/B/C aggregate receipt')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',default='work/abc_completion_2026-09-30')
    parser.add_argument('--out',default='docs/corpus_platform/receipts/abc_sol_completion_2026-09-30.json')
    main(parser.parse_args())
