"""Regression for independent review's lifecycle failures."""
import copy
import pickle
from dataclasses import FrozenInstanceError
import numpy as np
import pytest
from vkm_world.representations.world0.dynamic_schur import DynamicCondensation, CondensedState

CONTEXT={x:"synthetic" for x in ("geometry","materials","topology","boundary","partition","laws")}

def test_direct_state_constructor_and_deepcopy_remain_immutable():
    a=np.array([1.]); b=np.array([2.])
    state=CondensedState(0.,a,b,"a"*64)
    a[0]=20; b[0]=30
    assert state.retained[0]==1 and state.hidden[0]==2
    for candidate in (state,copy.copy(state),copy.deepcopy(state),pickle.loads(pickle.dumps(state))):
        for field in (candidate.retained,candidate.hidden):
            with pytest.raises(ValueError):field.flags.writeable=True
            with pytest.raises(ValueError):field[0]=99
        with pytest.raises((AttributeError,FrozenInstanceError)):candidate.hidden=np.array([99.])

def test_operator_reassignment_external_alias_and_binding_changes_cannot_mutate():
    c=np.eye(2);k=np.array([[2.,-.5],[-.5,3.]])
    context=copy.deepcopy(CONTEXT)
    op=DynamicCondensation(c,k,[0],context)
    signature=op.binding["signature"]
    state=op.initial_state([1,2]); expected=op.reconstruct(op.trial_step(state,[0,0],.3))
    c[0,0]=99;k[0,0]=99;context["laws"]="changed"
    op.binding["components"]["laws"]="changed"
    for name in ("c","k","r","i","_c","_k","_binding_json"):
        with pytest.raises(AttributeError):setattr(op,name,np.zeros(2))
    for model in (op,copy.deepcopy(op),pickle.loads(pickle.dumps(op))):
        for arr in (model.k,model.c,model.r,model.i):
            with pytest.raises(ValueError):arr.flags.writeable=True
        assert model.binding["signature"]==signature
        np.testing.assert_array_equal(model.reconstruct(model.trial_step(state,[0,0],.3)),expected)
