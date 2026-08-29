"""Small dependency-light metric helpers for the first reproducible models."""

from __future__ import annotations

from typing import Iterable

import numpy as np


def regression_metrics(
    y_true: Iterable[float],
    y_pred: Iterable[float],
    *,
    sample_weight: Iterable[float] | None = None,
) -> dict[str, float | int]:
    truth = np.asarray(list(y_true), dtype=float)
    prediction = np.asarray(list(y_pred), dtype=float)
    if truth.shape != prediction.shape:
        raise ValueError("y_true and y_pred must have the same shape")
    valid = np.isfinite(truth) & np.isfinite(prediction)
    if not valid.any():
        raise ValueError("No finite regression pairs")
    truth = truth[valid]
    prediction = prediction[valid]
    if sample_weight is None:
        weight = np.ones_like(truth)
    else:
        all_weights = np.asarray(list(sample_weight), dtype=float)
        if all_weights.shape != valid.shape:
            raise ValueError("sample_weight must have the same shape as y_true")
        weight = all_weights[valid]
        if (~np.isfinite(weight) | (weight < 0)).any() or weight.sum() <= 0:
            raise ValueError("sample_weight must be finite, non-negative, and have positive mass")
    weight = weight / weight.sum()
    error = prediction - truth
    mae = float(np.sum(weight * np.abs(error)))
    rmse = float(np.sqrt(np.sum(weight * error**2)))
    bias = float(np.sum(weight * error))
    truth_mean = float(np.sum(weight * truth))
    denominator = float(np.sum(weight * (truth - truth_mean) ** 2))
    r2 = float(1.0 - np.sum(weight * error**2) / denominator) if denominator > 0 else float("nan")
    return {"n": int(len(truth)), "mae": mae, "rmse": rmse, "bias": bias, "r2": r2}


def binary_classification_metrics(
    y_true: Iterable[int],
    probability: Iterable[float],
    *,
    threshold: float = 0.5,
) -> dict[str, float | int]:
    truth = np.asarray(list(y_true), dtype=float)
    score = np.asarray(list(probability), dtype=float)
    if truth.shape != score.shape:
        raise ValueError("y_true and probability must have the same shape")
    valid = np.isfinite(truth) & np.isfinite(score)
    truth = truth[valid].astype(int)
    score = score[valid]
    if not set(np.unique(truth)).issubset({0, 1}):
        raise ValueError("Binary targets must contain only 0 and 1")
    if ((score < 0) | (score > 1)).any():
        raise ValueError("Probabilities must lie in [0, 1]")
    predicted = score >= threshold
    positive = truth == 1
    tp = int(np.sum(predicted & positive))
    fp = int(np.sum(predicted & ~positive))
    fn = int(np.sum(~predicted & positive))
    tn = int(np.sum(~predicted & ~positive))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "n": int(len(truth)),
        "positive": int(positive.sum()),
        "negative": int((~positive).sum()),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "average_precision": float(average_precision(truth, score)),
    }


def average_precision(y_true: np.ndarray, score: np.ndarray) -> float:
    positives = int(np.sum(y_true == 1))
    if positives == 0:
        return float("nan")
    order = np.argsort(-score, kind="mergesort")
    sorted_truth = y_true[order]
    cumulative_positive = np.cumsum(sorted_truth == 1)
    precision_at_rank = cumulative_positive / np.arange(1, len(sorted_truth) + 1)
    return float(np.sum(precision_at_rank[sorted_truth == 1]) / positives)
