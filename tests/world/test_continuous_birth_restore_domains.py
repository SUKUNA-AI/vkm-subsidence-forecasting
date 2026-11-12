"""Reject invalid real-domain checkpoints without mutating accepted memory."""
import numpy as np
import pytest
from vkm_world.representations.world0 import continuous_birth as C

def model(mod=C):
    return mod.ContinuousModel(dict(K=np.array([[3.0, 0.1], [0.1, 2.0]]), M=np.array([2.0, 3.0]), dM=np.array([0.2, 0.4]), Q=np.eye(2), Wk=np.array([1.0, 2.0]), r0=np.array([0.1, -0.2]), incoming_body=np.array([-0.3, -0.5]), support=np.zeros(1), cell_ids=np.array([0]), fill_cells=np.array([0]), fillcapacity=np.array([0.3]), incoming_mass=np.array(0.6)))

def good_data():
    y = np.array([-0.003, 0.002, 0.03, -0.02, 0.001, 0.002])
    return dict(time=np.array(0.5), state=y, times=np.array([0.0, 0.25, 0.5]), refs=np.array([[0.0, 0.0], [-0.001, 0.001], y[:2]]), identity=np.array('test-real-history'))

def write(path, data, bad_checksum=False):
    d = {k: np.asarray(v).copy() for k, v in data.items()}
    d['checksum'] = np.array('wrong' if bad_checksum else C.payload(d))
    np.savez_compressed(path, **d)

def unchanged_on_reject(path, match=None):
    m = model()
    m.commit(0.1, np.array([-0.002, 0.001]))
    ts, rr = (m.times, m.refs)
    before = (ts.tobytes(), rr.tobytes())
    with pytest.raises(ValueError, match=match):
        m.restore(path, 'test-real-history')
    assert m.times is ts and m.refs is rr and ((ts.tobytes(), rr.tobytes()) == before)

@pytest.mark.parametrize('field', ['time', 'state', 'times', 'refs'])
@pytest.mark.parametrize('imaginary', [0.0, 1e-05])
def test_valid_checksum_complex_rejected_before_coercion(tmp_path, field, imaginary):
    d = good_data()
    d[field] = d[field].astype(complex) + 1j * imaginary
    path = tmp_path / 'complex.npz'
    write(path, d)
    with np.load(path) as z:
        loaded = {k: z[k] for k in z.files if k != 'checksum'}
        assert C.payload(loaded) == str(z['checksum'])
    unchanged_on_reject(path, 'numeric dtype/domain')

@pytest.mark.parametrize('field', ['time', 'state', 'times', 'refs'])
@pytest.mark.parametrize('dtype', ['U30', bool])
def test_valid_checksum_non_numeric_rejected(tmp_path, field, dtype):
    d = good_data()
    d[field] = d[field].astype(dtype)
    p = tmp_path / 'typed.npz'
    write(p, d)
    unchanged_on_reject(p, 'numeric dtype/domain')

@pytest.mark.parametrize('field', ['time', 'state', 'times', 'refs'])
def test_object_arrays_rejected_without_pickle(tmp_path, field):
    d = good_data()
    d[field] = d[field].astype(object)
    p = tmp_path / 'object.npz'
    write(p, d)
    unchanged_on_reject(p, 'Object arrays cannot be loaded')

@pytest.mark.parametrize('field', ['time', 'state', 'times', 'refs'])
@pytest.mark.parametrize('value', [np.nan, np.inf, -np.inf])
def test_valid_checksum_nonfinite_rejected(tmp_path, field, value):
    d = good_data()
    d[field] = d[field].copy()
    d[field].flat[-1] = value
    p = tmp_path / 'finite.npz'
    write(p, d)
    unchanged_on_reject(p, 'finite/domain')

@pytest.mark.parametrize('field,value', [('time', np.array([0.5])), ('time', np.array([[0.5]])), ('state', np.zeros((2, 3))), ('times', np.array([])), ('times', np.array(0.5)), ('times', np.zeros((1, 3))), ('refs', np.zeros(6)), ('refs', np.zeros((2, 2)))])
def test_valid_checksum_shape_rejected(tmp_path, field, value):
    d = good_data()
    d[field] = value
    p = tmp_path / 'shape.npz'
    write(p, d)
    unchanged_on_reject(p, 'shape/domain')

@pytest.mark.parametrize('times,time', [(np.array([0.0, 0.5, 0.25]), 0.25), (np.array([0.0, 0.25, 0.25]), 0.25), (np.array([0, 2, 1], dtype=np.uint64), 1), (np.array([0.0, 0.25, 0.5]), 0.75), (np.array([0.0]), 0.0), (np.array([0.0, 0.5, 1.1]), 1.1), (np.array([0.1, 0.25, 0.5]), 0.5)])
def test_valid_checksum_temporal_domain_rejected(tmp_path, times, time):
    d = good_data()
    d['times'] = times
    d['time'] = np.array(time)
    d['refs'] = np.zeros((len(times), 2))
    p = tmp_path / 'time.npz'
    write(p, d)
    unchanged_on_reject(p, 'checkpoint domain')

def test_checksum_failure_is_distinct_from_domain(tmp_path):
    p = tmp_path / 'checksum.npz'
    write(p, good_data(), True)
    unchanged_on_reject(p, 'identity/checksum')
    d = good_data()
    d['state'] = d['state'].astype(complex)
    write(p, d)
    unchanged_on_reject(p, 'numeric dtype/domain')

def test_reference_consistency_guard_and_schema(tmp_path):
    d = good_data()
    d['refs'][-1, 0] += 0.001
    p = tmp_path / 'reference.npz'
    write(p, d)
    unchanged_on_reject(p, 'reference state')
    d = good_data()
    del d['time']
    write(p, d)
    unchanged_on_reject(p, 'schema')
