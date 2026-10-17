"""Scientific admission must not turn unavailable QC/frames into a good RMSE."""
import pytest
from scripts.audit_skru1_surface import exclusion_reasons, stats, validation_metrics


def comparable(**updates):
    row=dict(independent_of_fit=True,quality_pass=True,same_vertical_frame=True,
             comparable_surface=True,epoch_comparable=True,product_version_identified=True,
             prediction_m=3.0,reference_m=1.0)
    row.update(updates)
    return row


def test_missing_quality_is_not_a_pass_and_empty_is_not_zero_error():
    result=validation_metrics([comparable(quality_pass=None)])
    assert result["n"] == 0
    assert result["rmse_m"] is None
    assert result["status"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("field", ["independent_of_fit","same_vertical_frame","comparable_surface",
                                   "epoch_comparable","product_version_identified"])
def test_fit_datum_surface_epoch_and_version_are_required(field):
    assert exclusion_reasons(comparable(**{field:None}))
    assert validation_metrics([comparable(**{field:False})])["n"] == 0


def test_metrics_do_not_select_on_residual():
    result=validation_metrics([comparable(), comparable(prediction_m=-2,reference_m=1)])
    assert result["n"] == 2
    assert result["bias_m"] == -0.5
    assert result["mae_m"] == 2.5
    assert result["rmse_m"] == pytest.approx((6.5)**0.5)


def test_nonfinite_metrics_rejected():
    with pytest.raises(ValueError):
        stats([float("nan")])
