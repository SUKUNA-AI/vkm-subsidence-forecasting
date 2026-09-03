from __future__ import annotations

import numpy as np
import pytest
from scipy import integrate, stats

from skru1.gate_c1_probabilistic import quantile_grid_crps, student_t_nll, student_t_quantiles


def test_student_t_quantiles_nll_and_crps_are_finite_and_ordered() -> None:
    truth = np.asarray([-1.0, 0.5, 2.0])
    loc = np.asarray([0.0, 0.0, 1.0])
    scale = np.asarray([1.0, 2.0, 0.5])
    df = np.asarray([3.0, 5.0, 10.0])
    quantiles = student_t_quantiles(loc, scale, df, np.arange(0.01, 1.0, 0.01))
    assert quantiles.shape == (3, 99)
    assert (np.diff(quantiles, axis=1) > 0).all()
    assert np.isfinite(student_t_nll(truth, loc, scale, df)).all()
    assert np.isfinite(quantile_grid_crps(truth, loc, scale, df)).all()


def test_student_t_crps_grid_matches_independent_cdf_integration() -> None:
    truth = np.asarray([0.35])
    loc = np.asarray([0.1])
    scale = np.asarray([1.2])
    df = np.asarray([4.5])
    approximate = float(quantile_grid_crps(truth, loc, scale, df)[0])
    distribution = stats.t(df=df[0], loc=loc[0], scale=scale[0])
    lower = distribution.ppf(1.0e-7)
    upper = distribution.ppf(1.0 - 1.0e-7)
    independent, _ = integrate.quad(
        lambda value: (distribution.cdf(value) - float(value >= truth[0])) ** 2,
        lower,
        upper,
        points=[truth[0]],
        epsabs=1.0e-8,
    )
    assert approximate == pytest.approx(independent, abs=0.03)


torch = pytest.importorskip("torch", reason="Gate C1 adapter tests run authoritatively in gate_c_torch")

from skru1.gate_c1_models import (  # noqa: E402
    CausalConv1d,
    PackedRecurrentRegressor,
    create_sequence_model,
    model_parameter_count,
)
from torch.nn.utils.rnn import pack_padded_sequence  # noqa: E402


def _inputs(input_size: int = 8):
    torch.manual_seed(42117)
    x = torch.randn(3, 16, input_size)
    lengths = torch.tensor([3, 8, 16])
    padding = torch.ones(3, 16)
    observation = torch.zeros(3, 16)
    missing = torch.zeros(3, 16)
    for index, length in enumerate(lengths.tolist()):
        padding[index, -length:] = 0
        observation[index, -length:] = 1
    return x, lengths, padding, observation, missing


@pytest.mark.parametrize(
    "model_id,parameters",
    [
        ("C01_compact_gru", {"hidden_size": 16, "layers": 1, "dropout": 0.0, "weight_decay": 0.0001}),
        ("C02_compact_lstm", {"hidden_size": 16, "layers": 1, "dropout": 0.0, "weight_decay": 0.0001}),
        ("C03_causal_tcn", {"channels": [16, 16], "kernel_size": 2, "dropout": 0.0, "weight_decay": 0.0001}),
        ("C04_probabilistic_gru_student_t", {"hidden_size": 16, "layers": 1, "dropout": 0.0, "weight_decay": 0.0001}),
    ],
)
def test_padding_values_do_not_change_prediction(model_id: str, parameters: dict) -> None:
    x, lengths, padding, observation, missing = _inputs()
    model = create_sequence_model(model_id, parameters, input_size=x.shape[-1]).eval()
    masked = x.clone()
    masked[padding.bool()] = 0
    altered = masked.clone()
    altered[padding.bool()] = 9999
    with torch.no_grad():
        first = model(masked, lengths, padding, observation, missing).point
        second = model(altered, lengths, padding, observation, missing).point
    assert torch.allclose(first, second, atol=1.0e-7, rtol=0)
    assert model_parameter_count(model) <= 100000


def test_causal_conv_does_not_use_future_token_positions() -> None:
    layer = CausalConv1d(2, 3, kernel_size=3, dilation=2).eval()
    original = torch.randn(1, 2, 16)
    altered = original.clone()
    altered[:, :, 10:] = 1000
    with torch.no_grad():
        first = layer(original)
        second = layer(altered)
    assert torch.allclose(first[:, :, :10], second[:, :, :10], atol=1.0e-7, rtol=0)


@pytest.mark.parametrize("cell", ["gru", "lstm"])
def test_dense_cuda_ready_recurrent_path_matches_packed_reference(cell: str) -> None:
    x, lengths, _, _, _ = _inputs()
    model = PackedRecurrentRegressor(
        input_size=x.shape[-1], hidden_size=12, layers=1, dropout=0.0, cell=cell
    ).eval()
    right_padded = model.chronological_right_padded(x, lengths)
    packed = pack_padded_sequence(
        right_padded, lengths.cpu(), batch_first=True, enforce_sorted=False
    )
    with torch.inference_mode():
        _, hidden = model.recurrent(packed)
        if isinstance(hidden, tuple):
            hidden = hidden[0]
        expected = hidden[-1]
        actual = model.encode(x, lengths)
    assert torch.allclose(actual, expected, atol=1.0e-6, rtol=1.0e-6)


def test_recurrent_padding_path_has_no_python_row_loop_or_host_list_sync() -> None:
    import inspect

    source = inspect.getsource(PackedRecurrentRegressor.chronological_right_padded)
    assert ".cpu().tolist()" not in source
    assert "for row_index" not in source
    assert ".gather(" in source


def test_student_t_head_constraints() -> None:
    x, lengths, padding, observation, missing = _inputs()
    model = create_sequence_model(
        "C04_probabilistic_gru_student_t",
        {"hidden_size": 16, "layers": 1, "dropout": 0.0, "weight_decay": 0.0001},
        input_size=x.shape[-1],
    ).eval()
    with torch.no_grad():
        output = model(x, lengths, padding, observation, missing)
    assert torch.isfinite(output.loc).all()
    assert (output.scale > 0).all()
    assert (output.df > 2.01).all()
