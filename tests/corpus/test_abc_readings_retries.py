"""Scientific limits and immutable retry provenance, without model execution."""
import pytest

from benchmarks.abc_completion_v1.close_retries import close
from benchmarks.abc_completion_v1.readings import acceptance_for
from benchmarks.abc_completion_v1.retry_glm import repetition_flag, subregions, translated_transform


def test_partial_reading_is_not_a_conditionally_known_number():
    decision={'acceptance':'CONDITIONAL','value':'UNKNOWN','status':'SOURCE_TRUNCATED',
              'verification_method':'EYE_SOURCE_REVIEW'}
    assert acceptance_for({},decision)[:2]==('UNRESOLVED','UNKNOWN')


def test_verified_blank_does_not_become_zero():
    result=acceptance_for({'verification_status':'NOT_A_CELL'},None)
    assert result[0:2]==('ACCEPTED','UNKNOWN')


def test_split_transform_preserves_rotated_source_coordinates():
    matrix=[0,2,-3,0,100,200]
    moved=translated_transform(matrix,10,20)
    assert moved==[0,2,-3,0,40,220]
    for x,y in [(0,0),(13,27)]:
        assert moved[0]*x+moved[2]*y+moved[4]==matrix[0]*(x+10)+matrix[2]*(y+20)+matrix[4]
        assert moved[1]*x+moved[3]*y+moved[5]==matrix[1]*(x+10)+matrix[3]*(y+20)+matrix[5]


def test_split_covers_edges_and_overlaps():
    boxes=subregions(641,639)
    assert boxes[0][:2]==[0,0] and boxes[-1][2:]==[641,639]
    assert boxes[0][2]>boxes[1][0] and boxes[0][3]>boxes[2][1]


def test_zero_runs_not_misclassified_as_text_repetition():
    assert not repetition_flag(('0000\n'*20))
    assert repetition_flag(('a fabricated long repeated label\n'*12))


def test_retry_completion_stays_unreviewed_and_not_evidence():
    batch={'records':[{'job_key':'j','status':'FULL_OUTPUT_COMPLETE','attempts':[{'file':'a'}]}]}
    result=close(batch,{'records':[]})
    assert result['records'][0]['resolution']['status']=='OCR_RETRY_COMPLETE_UNREVIEWED'
    assert result['records'][0]['admitted_to_evidence'] is False


def test_cannot_close_source_review_without_matching_crop():
    batch={'records':[{'job_key':'j','status':'SOURCE_REVIEW_REQUIRED',
                       'original_input_sha256':'original','attempts':[]}]}
    with pytest.raises(ValueError,match='exactly one'):
        close(batch,{'records':[]})
    decision={'job_key':'j','input_sha256':'different','status':'VERIFIED_NO_TEXT_IN_TILE',
              'verification_method':'eye','reason':'linework'}
    with pytest.raises(ValueError,match='crop mismatch'):
        close(batch,{'records':[decision]})
