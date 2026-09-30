import pytest

from benchmarks.abc_completion_v1.package import (
    CONTEXT_FIELDS, INTERPRETATION_FIELDS, dataset_summary, queue_closure, split_review_case, topology_summary,
    checked_coverage_finalization, sha, write,
)
from benchmarks.abc_completion_v1.public_receipt import aggregates


def test_queue_requires_every_original_case_once():
    queue=[{'id':'AR-1'},{'id':'AR-2'}]
    decision={'case_id':'AR-1','reason':'source gap','verification_method':'source image'}
    with pytest.raises(ValueError,match='unprocessed'):
        queue_closure(queue,{'A':[decision]})
    with pytest.raises(ValueError,match='duplicate'):
        queue_closure(queue,{'A':[decision],'B':[decision]})
    result=queue_closure(queue,{'A':[decision],'B':[{'case_id':'AR-2','reason':'legend verified','verification_method':'legend and source image'}]})
    assert result['processed_cases']==2


def test_source_first_context_hides_sol_and_extracted_answers():
    case={field:[] for field in CONTEXT_FIELDS+INTERPRETATION_FIELDS}
    case.update(case_id='case1',priority='P1',alternatives=['x','y'],
                normal_methods_attempted=['expanded context','native lookup'],
                why_normal_methods_failed='source labels overlap',sol_interpretation='x',
                extracted_value_or_geometry={'value':'x'})
    context,interpretation=split_review_case(case)
    assert 'sol_interpretation' not in context and 'extracted_value_or_geometry' not in context
    assert interpretation['sol_interpretation']=='x'


def test_cannot_defer_unattempted_work_to_independent_reviewer():
    case={field:[] for field in CONTEXT_FIELDS+INTERPRETATION_FIELDS}
    case.update(case_id='case1',priority='P0',alternatives=['x','y'])
    with pytest.raises(ValueError,match='ordinary methods'):
        split_review_case(case)


def test_reading_acceptance_cannot_silently_become_evidence():
    record={'record_id':'a','acceptance':'ACCEPTED','source_id':'source',
            'verification_method':'eye','admitted_to_evidence':True}
    with pytest.raises(ValueError,match='admit evidence'):
        dataset_summary({'records':[record]})


def test_explicit_unknown_remains_in_unresolved_partition():
    record={'record_id':'a','acceptance':'UNRESOLVED','source_id':'source',
            'verification_method':'eye','value':'UNKNOWN'}
    assert dataset_summary({'records':[record]})['partitions']['UNRESOLVED']==['a']


def test_public_receipt_drops_source_literals_and_internal_record_ids():
    completion={'datasets':{'A':{'record_count':1,'counts':{'ACCEPTED':1},
                'partitions':{'ACCEPTED':['private-record-id']},'records':[{'value':'private-value'}]}},
                **{name:{} for name in ('original_queue','glm_retry_closure','coverage','astra_review','frozen_integrity')},
                'inputs':{'private-source-path':'hash'}}
    result=aggregates(completion)
    assert 'private-value' not in str(result) and 'private-record-id' not in str(result)
    assert 'inputs' not in result


def test_topology_cannot_be_closed_by_counts_without_qgis_acceptance():
    topology={'status_counts':{'REPAIRED_GRAPHIC_CANDIDATE':1,'UNRESOLVED_GRAPHIC_TOPOLOGY':2},
              'original_invalid':3,'repaired_layers':1,'raw_frozen_unchanged':True,
              'makevalid_calls':0,'new_coordinates_or_nonzero_edges':0,'semantic_geometry_accepted':False}
    qgis={'status':'PASS','repaired_candidates':1,
          'records':[{'status':'PASS','scientific_acceptance':False}]}
    assert topology_summary(topology,qgis)['strict_qgis_passed']==1
    qgis['records'][0]['status']='FAIL'
    with pytest.raises(ValueError,match='strict QGIS'):
        topology_summary(topology,qgis)
    qgis['records'][0]['status']='PASS'
    topology['new_coordinates_or_nonzero_edges']=1
    with pytest.raises(ValueError,match='boundary'):
        topology_summary(topology,qgis)


def coverage_fixture(repo):
    root=repo/'work/run'
    directory=root/'coverage'
    paths={'candidates':directory/'candidates.json','coverage':directory/'coverage.json',
           'inventory':repo/'work/figure_readings_2026-09-29/v2/inventory.json',
           'visual_reviews':directory/'visual_role_reviews.json'}
    for name,path in paths.items():
        value=([{'candidate_id':'TEST-1'}] if name=='candidates' else
               [{'source_id':'TEST-SOURCE','pages_checked':2}] if name=='coverage' else [])
        write(path,value)
    target=directory/'candidate_dispositions.json'
    write(target,[{'candidate_id':'TEST-1'}])
    code=repo/'benchmarks/abc_completion_v1/coverage.py'
    code.parent.mkdir(parents=True)
    code.write_text('# synthetic code\n',encoding='utf8')
    receipt={'inputs':{name:sha(path) for name,path in paths.items()},
             'outputs':{target.name:sha(target)},'code_sha256':sha(code),
             'sources':1,'pages_screened':2,'candidate_dispositions':1,'visually_role_checked_candidates':0}
    write(directory/'screening_finalization_receipt.json',receipt)
    return root,receipt


def test_stale_coverage_receipt_cannot_certify_changed_inputs(tmp_path):
    root,_=coverage_fixture(tmp_path)
    assert checked_coverage_finalization(tmp_path,root)['pages_screened']==2
    write(root/'coverage/candidates.json',[{'candidate_id':'OTHER'}])
    with pytest.raises(ValueError,match='input changed'):
        checked_coverage_finalization(tmp_path,root)


def test_hash_bound_but_incomplete_dispositions_are_rejected(tmp_path):
    root,receipt=coverage_fixture(tmp_path)
    target=root/'coverage/candidate_dispositions.json'
    write(target,[{'candidate_id':'TEST-1'},{'candidate_id':'TEST-1'}])
    receipt['outputs'][target.name]=sha(target)
    write(root/'coverage/screening_finalization_receipt.json',receipt)
    with pytest.raises(ValueError,match='exactly once'):
        checked_coverage_finalization(tmp_path,root)
