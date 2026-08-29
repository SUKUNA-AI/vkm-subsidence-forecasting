"""Leakage-safe data and evaluation utilities for the SKRU-1 project."""

from .data_contracts import CanonicalBundle, FeatureContract, load_canonical_bundle
from .splits import ManifestDataset, SealedTestError, UnsafeSplitError, load_split_dataset

__all__ = [
    "CanonicalBundle",
    "FeatureContract",
    "ManifestDataset",
    "SealedTestError",
    "UnsafeSplitError",
    "load_canonical_bundle",
    "load_split_dataset",
]
