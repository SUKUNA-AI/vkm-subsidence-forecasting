"""Lossless storage, nonzero event memory and rejected-trial restoration guards."""
import json
from zipfile import ZipFile, ZIP_STORED

import numpy as np
import pytest

from test_r3_history import make
from vkm_world.representations.world0.checkpoint_storage import write_raw_checkpoint


def born_state():
    model = make(water=True, events=True)
    state, _ = model.initial()
    for time in (2., 3.):
        state, _ = model.advance(state, time)
        state, _ = model.apply_events(state)
    return model, state


def test_raw_and_compressed_preserve_all_state_and_continue_identically(tmp_path):
    model, state = born_state()
    compressed, raw = tmp_path/'compressed.npz', tmp_path/'raw.npz'
    model.checkpoint(compressed, state)
    write_raw_checkpoint(model, raw, state)
    with ZipFile(raw) as archive:
        assert all(member.compress_type == ZIP_STORED for member in archive.infolist())
    with np.load(compressed, allow_pickle=False) as first, np.load(raw, allow_pickle=False) as second:
        assert first.files == second.files
        for name in first.files:
            assert first[name].dtype == second[name].dtype
            assert first[name].shape == second[name].shape
            assert first[name].tobytes() == second[name].tobytes()
    expected, _ = model.advance(state, 4.)
    actual, _ = model.advance(model.reopen(raw), 4.)
    a, b = model.fields(expected), model.fields(actual)
    assert a.keys() == b.keys()
    for name in a:
        np.testing.assert_array_equal(a[name], b[name])
    for old, new in zip(expected.materials, actual.materials, strict=True):
        for key in old.memory:
            np.testing.assert_array_equal(old.memory[key], new.memory[key])


def test_raw_rejected_trial_is_not_accepted_restart(tmp_path):
    model, state = born_state()
    path = tmp_path/'rejected.npz'
    write_raw_checkpoint(model, path, state, accepted=False)
    sidecar = json.loads((tmp_path/'rejected.npz.binding.json').read_text(encoding='utf-8'))
    assert sidecar['accepted'] is False
    with pytest.raises(ValueError):
        model.reopen(path)


def test_raw_restart_rejects_modified_bytes(tmp_path):
    model, state = born_state()
    path = tmp_path/'state.npz'
    write_raw_checkpoint(model, path, state)
    with path.open('ab') as stream:
        stream.write(b'corruption')
    with pytest.raises(ValueError):
        model.reopen(path)
