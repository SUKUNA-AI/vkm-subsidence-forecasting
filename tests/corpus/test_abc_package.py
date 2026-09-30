import pytest

from benchmarks.abc_completion_v1.package import (
    CONTEXT_FIELDS, INTERPRETATION_FIELDS, dataset_summary, queue_closure, split_review_case,
)


def test_queue_requires_every_original_case_once():
    queue=[{'id':'AR-1'},{'id':'AR-2'}]
    decision={'case_id':'AR-1','reason':'source gap'}
    with pytest.raises(ValueError,match='unprocessed'):
        queue_closure(queue,{'A':[decision]})
    with pytest.raises(ValueError,match='duplicate'):
        queue_closure(queue,{'A':[decision],'B':[decision]})
    result=queue_closure(queue,{'A':[decision],'B':[{'case_id':'AR-2','reason':'legend verified'}]})
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
