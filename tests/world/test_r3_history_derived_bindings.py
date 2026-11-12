"""Additional05 controls: water transpose and event calendar are physical operators."""
import pytest
from test_r3_history_independent import specimen,event
from vkm_world.representations.world0.constitutive_history import StepFailure

def test_hydraulic_transpose_mutation_cannot_reuse_identity():
    model=specimen(water=True);state,_=model.initial()
    model.water.Bt.data*=1.1
    with pytest.raises((ValueError,StepFailure)):
        model.advance(state,.5)

def test_derived_event_times_cannot_shift_source_date():
    model=specimen(rows=[event('excavate','cut',.2)]);state,_=model.initial()
    model.event_times[0]=2.
    with pytest.raises((ValueError,StepFailure)):
        model.advance(state,1.5)
