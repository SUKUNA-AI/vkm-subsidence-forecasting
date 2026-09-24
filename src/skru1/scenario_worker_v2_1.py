"""Model-worker input boundary. Does not implement or execute an estimator."""
from __future__ import annotations
from dataclasses import dataclass
import pandas as pd

from .scenario_adapter_v2_1 import ModelDataBundle, make_dataset, estimator_matrix
from .scenario_boundary_v2_1 import model_worker_scope
from .splits import ManifestDataset


@dataclass(frozen=True)
class OriginInputs:
    origin: ManifestDataset
    history: pd.DataFrame  # this point only, cutoff already applied


def origin_inputs(bundle: ModelDataBundle, sample_id: str) -> OriginInputs:
    frame = bundle.frames.loc[bundle.frames.sample_id.eq(sample_id)].copy()
    if len(frame) != 1:
        raise ValueError("Unknown/nonunique origin")
    dataset = make_dataset(frame, "prediction", bundle.provenance["split_sha256"])
    estimator_matrix(dataset)
    return OriginInputs(dataset, bundle.history.for_origin(frame.iloc[0]))


def run_origin_worker(root, bundle: ModelDataBundle, sample_id: str, operation):
    """Operation receives no target store; file boundary remains active throughout."""
    if not isinstance(bundle, ModelDataBundle):
        raise TypeError("Worker requires ModelDataBundle")
    with model_worker_scope(root):
        return operation(origin_inputs(bundle, sample_id))
