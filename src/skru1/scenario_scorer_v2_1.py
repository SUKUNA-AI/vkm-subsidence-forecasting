"""Scorer-side truth loading only. No model object, fitting or scoring is executed."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import pandas as pd

from .scenario_boundary_v2_1 import verified_manifest, verified_payload


@dataclass(frozen=True)
class EvaluatorTruthStore:
    _truth: pd.DataFrame

    def join_predictions(self, predictions: pd.DataFrame) -> pd.DataFrame:
        """Exact scorer join; metrics are intentionally outside this release."""
        if tuple(predictions.columns) != ("sample_id", "predicted_rate_mm_y"):
            raise ValueError("Prediction boundary accepts only origin ID and prediction")
        if predictions.sample_id.duplicated().any():
            raise ValueError("Duplicate predicted origins")
        if not predictions.sample_id.isin(self._truth.sample_id).all():
            raise ValueError("Unknown predicted origin")
        return predictions.merge(self._truth, on="sample_id", validate="one_to_one", sort=False)


def load_evaluator_truth(root: str | Path) -> EvaluatorTruthStore:
    root = Path(root).resolve()
    manifest = verified_manifest(root)
    path = verified_payload(root, manifest, "evaluator/next_planned_truth.csv.gz")
    truth = pd.read_csv(path)
    if truth.sample_id.duplicated().any():
        raise ValueError("Duplicate evaluator origins")
    return EvaluatorTruthStore(truth)


def load_campaign_plan_for_qa(root: str | Path) -> pd.DataFrame:
    """Authorized synthetic QA only, select no latent or observation error columns."""
    root = Path(root).resolve()
    manifest = verified_manifest(root)
    path = verified_payload(root, manifest, "evaluator/campaign_truth.csv.gz")
    return pd.read_csv(path, usecols=["entity_point_id", "campaign_id", "date", "targeted", "observed"])
