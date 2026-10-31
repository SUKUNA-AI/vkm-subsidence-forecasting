"""Finite-growth positive links: numerical contracts, not field validation."""
import hashlib

import numpy as np
import pytest

from vkm_world.geometry_build import gp_ensemble as ge
from vkm_world.geometry_build.positive_field_law import (
    LEGACY_LAW, MEDIAN_SCALE_RULE, PositiveFieldLaw, SQUAREPLUS_LAW,
    inverse_squareplus, inverse_squareplus_torch, squareplus, squareplus_torch,
)

PINS = {"synthetic_input": hashlib.sha256(b"synthetic inputs, not observations").hexdigest()}


def test_inverse_positive_monotone_and_linear_growth_bound():
    y = np.r_[-1e200, -1e100, -1e10, np.linspace(-100, 100, 1001), 1e10, 1e100, 1e200]
    d = squareplus(y, 14.)
    assert np.all(d > 0) and np.all(np.diff(d) > 0)
    np.testing.assert_allclose(inverse_squareplus(d, 14.), y, rtol=4e-15, atol=2e-14)
    assert np.all(d <= 14. * (1. + np.abs(y)) * (1 + 2e-15))
    assert squareplus(0., 14.) == 14.


def test_units_and_inverse_near_positive_zero():
    depths_m = np.array([1e-250, 1e-100, .001, 1.2, 14., 68., 1e100, 1e250])
    y = inverse_squareplus(depths_m, 14.)
    np.testing.assert_allclose(squareplus(y, 14.), depths_m, rtol=3e-15)
    for unit_factor in (.01, 100., 1000.):
        np.testing.assert_allclose(inverse_squareplus(depths_m * unit_factor, 14. * unit_factor), y, rtol=4e-15)
        np.testing.assert_allclose(squareplus(y, 14. * unit_factor), depths_m * unit_factor, rtol=4e-15)


def test_negative_extreme_avoids_cancellation_and_squaring_overflow():
    y = np.array([-np.finfo(float).max, -1e308, -1e200, -1e20])
    d = squareplus(y, 14.)
    assert np.isfinite(d).all() and (d > 0).all()
    np.testing.assert_allclose(inverse_squareplus(d, 14.), y, rtol=4e-15)


@pytest.mark.parametrize("bad", [0., -1., np.nan, np.inf])
def test_invalid_depths_or_scale_fail_closed(bad):
    with pytest.raises(ValueError):
        inverse_squareplus([bad], 14.)
    with pytest.raises(ValueError):
        squareplus([1.], bad)


def test_unrepresentable_result_fails_without_floor_or_clip():
    with pytest.raises(FloatingPointError):
        squareplus([np.finfo(float).max], 14.)
    with pytest.raises(FloatingPointError):
        inverse_squareplus([np.nextafter(0., 1.)], 14.)
    with pytest.raises(ValueError):
        squareplus([np.inf], 14.)
    with pytest.raises(ValueError):
        squareplus(np.array([1 + 2j]), 14.)
    with pytest.raises(ValueError):
        inverse_squareplus(np.array([1 + 2j]), 14.)


def test_data_derived_scale_identity_and_units():
    d = np.array([1.2, 2., 14., 20., 68.])
    law = PositiveFieldLaw.from_corrected_top(d, input_sha256=PINS)
    assert law.law_id == SQUAREPLUS_LAW and law.top_scale_m == 14.
    assert law.scale_rule == MEDIAN_SCALE_RULE
    assert law.describe()["status"] == "MODEL_CHOICE"
    assert law.describe()["physical_upper_bound_m"] is None
    assert law.describe()["censoring_convergence"] == "NOT_ESTABLISHED_BY_LINK"
    cm = PositiveFieldLaw.from_corrected_top(d * 100, input_sha256=PINS)
    np.testing.assert_allclose(cm.encode_top(d * 100), law.encode_top(d), atol=1e-15)
    settings = dict(seed=31, batch=17, parameter_chunk=128, censoring_sweeps=30,
                    input_sha256=PINS, module_sha256="b" * 64, rng="numpy.PCG64", precision="float64")
    identity = law.generation_identity(**settings)
    assert identity["positive_field_law"]["scale_input_sha256"] == PINS
    assert len(identity["law_module_sha256"]) == 64
    assert identity != law.generation_identity(**{**settings, "batch": 128})
    assert identity != law.generation_identity(**{**settings, "parameter_chunk": 17})
    with pytest.raises(ValueError):
        law.generation_identity(**{**settings, "input_sha256": {}})
    with pytest.raises(ValueError):
        PositiveFieldLaw.from_corrected_top([0., 1., 2., 3.], input_sha256=PINS)


def test_even_scale_median_does_not_overflow():
    d = np.array([1e308, 1.1e308, 1.2e308, 1.3e308])
    assert np.isfinite(PositiveFieldLaw.from_corrected_top(d, input_sha256=PINS).top_scale_m)


def _readings():
    xy = np.array([[0., 0.], [100., 0.], [0., 100.], [100., 100.], [50., 200.], [200., 50.]])
    top = np.array([1.2, 5., 14., 23., 31., 68.])
    intervals = np.array([[10 + i * 2 + f for f in range(5)] for i in range(6)], float)
    intervals[2, 2] = 0.
    depth = np.column_stack([top, top[:, None] + np.cumsum(intervals, axis=1)])
    return ge.LayeredReadings([str(i) for i in range(6)], xy, depth, list("ABCDEF"), [(2, 2, 3)])


def test_latent_exact_conditioning_inverse_and_censored_absence():
    r = _readings()
    law = PositiveFieldLaw.from_corrected_top(r.depth[:, 0], input_sha256=PINS)
    grid = ge.ParameterGrid(ranges_m=(100.,), nugget_ratios=(.1,), jitter=1e-4)
    out = ge.layered_predictive(np.random.default_rng(31), r, np.arange(6), r.xy,
                               ["spherical"] * 6, 6, grid, sweeps=3, positive_law=law)
    np.testing.assert_allclose(out["depth"], np.broadcast_to(r.depth, out["depth"].shape), atol=2e-5)
    assert (out["depth"] > 0).all() and (out["thickness"] >= 0).all()
    assert (out["thickness"][:, 2, 2] == 0).all()


def test_new_top_collar_shift_no_floor_legacy_replay_unchanged():
    depths = np.array([1., 2., 3., 4.])
    shifts = np.array([.5, .1, -.1, .4])
    law = PositiveFieldLaw.from_corrected_top(depths - shifts, input_sha256=PINS)
    y, floors = ge.top_latent(depths, shifts, law)
    assert floors == 0
    np.testing.assert_allclose(law.decode_top(y), depths - shifts)
    with pytest.raises(ValueError):
        ge.top_latent([1.], [2.], law)
    old, count = ge.top_latent([1.], [2.])
    assert count == 1 and old[0] == np.log(.1)
    r = _readings()
    kwargs = dict(readings=r, train_rows=np.arange(6), targets=r.xy, kinds=["spherical"] * 6,
                  draws=4, grid=ge.ParameterGrid(ranges_m=(100.,), nugget_ratios=(.1,)), sweeps=2)
    a = ge.layered_predictive(np.random.default_rng(88), **kwargs)
    b = ge.layered_predictive(np.random.default_rng(88), **kwargs, positive_law=PositiveFieldLaw())
    np.testing.assert_array_equal(a["depth"], b["depth"])
    assert PositiveFieldLaw().law_id == LEGACY_LAW


@pytest.mark.parametrize("apply_shift", [False, True])
def test_explicit_legacy_matches_none_below_historical_floor(apply_shift):
    r = _readings()
    depth = r.depth.copy()
    depth[0] -= 1.15  # positive TOP0.05m, below the historical0.1m shift-floor
    r = ge.LayeredReadings(r.ids, r.xy, depth, r.contacts, r.absent)
    kwargs = dict(readings=r, train_rows=np.arange(6), targets=r.xy, kinds=["spherical"] * 6,
                  draws=3, grid=ge.ParameterGrid(ranges_m=(100.,), nugget_ratios=(.1,)), sweeps=2)
    if apply_shift:
        kwargs["train_shift"] = np.zeros((3, 6))
    a = ge.layered_predictive(np.random.default_rng(89), **kwargs)
    b = ge.layered_predictive(np.random.default_rng(89), **kwargs, positive_law=PositiveFieldLaw())
    np.testing.assert_array_equal(a["depth"], b["depth"])
    # Legacy floor applies only through top_latent's collar-shift path; the
    # unshifted old field_readings log path never applied this floor.
    np.testing.assert_allclose(a["depth"][:, 0, 0], .1 if apply_shift else .05, atol=2e-6)
    assert a["fields"][0]["top_floor_hits"] == (3 if apply_shift else 0)


def test_new_law_second_moment_support_gate():
    r = _readings().subset([0, 1, 2])
    law = PositiveFieldLaw(SQUAREPLUS_LAW, 14., "explicit_synthetic_scale_MODEL_CHOICE")
    with pytest.raises(ValueError, match="nondegenerate exact"):
        ge.layered_predictive(np.random.default_rng(5), r, np.arange(3), r.xy,
                             ["spherical"] * 6, 2, positive_law=law)


def test_relative_only_support_mask_honours_five_gaps_without_absolute_top():
    r = _readings()
    mask = np.ones((6, 6), bool)
    mask[0, 0] = False  # A relative-only support, no source-ID special case.
    law = PositiveFieldLaw.from_corrected_top(r.depth[mask[0], 0], input_sha256=PINS)
    out = ge.layered_predictive(np.random.default_rng(77), r, np.arange(6), r.xy,
                               ["spherical"] * 6, 6,
                               ge.ParameterGrid(ranges_m=(100.,), nugget_ratios=(.1,)),
                               sweeps=3, positive_law=law, field_reading_mask=mask)
    assert out["fields"][0]["readings"] == 5
    assert all(f["readings"] == 6 for f in out["fields"][1:])
    np.testing.assert_allclose(np.diff(out["depth"][:, 0, :], axis=1),
                               np.broadcast_to(np.diff(r.depth[0]), (6, 5)), atol=2e-5)
    assert not np.allclose(out["depth"][:, 0, 0], r.depth[0, 0])
    wrong_scale = PositiveFieldLaw.from_corrected_top(r.depth[:, 0], input_sha256=PINS)
    with pytest.raises(ValueError, match="training TOP supports"):
        ge.layered_predictive(np.random.default_rng(77), r, np.arange(6), r.xy,
                             ["spherical"] * 6, 2, positive_law=wrong_scale, field_reading_mask=mask)


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_torch_fixed_values_cpu_cuda_forward_inverse(device):
    torch = pytest.importorskip("torch")
    if device == "cuda" and not torch.cuda.is_available():
        pytest.skip("CUDA unavailable; never reported as CUDA pass")
    y = np.array([-1e200, -100., -1., 0., 1., 100., 1e200])
    t = torch.tensor(y, dtype=torch.float64, device=device)
    d = squareplus_torch(t, 14.)
    assert d.device.type == device and d.dtype == torch.float64
    np.testing.assert_allclose(d.cpu().numpy(), squareplus(y, 14.), rtol=4e-15)
    np.testing.assert_allclose(inverse_squareplus_torch(d, 14.).cpu().numpy(), y, rtol=4e-15)
