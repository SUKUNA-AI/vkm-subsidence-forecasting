"""Relative displacement observation operators with explicit frame and reference.

Coordinates and displacements use metres. ENU axes are east, north, up.
These operators predict a measurement. They do not supply survey accuracy.
"""
from __future__ import annotations

from numbers import Integral

import numpy as np


def validate_state(displacements, *, frame_id: str, observation_frame_id: str, unit: str):
    frames = (frame_id, observation_frame_id)
    if any(not isinstance(frame, str) or not frame.strip() or frame.strip().upper() == "UNKNOWN"
           for frame in frames) or frame_id != observation_frame_id:
        raise ValueError("A verified common displacement frame is required")
    if unit != "m":
        raise ValueError("Displacements must be explicitly converted to metres")
    values = np.asarray(displacements, dtype=np.float64)
    if (values.ndim != 3 or values.shape[2] != 3 or values.shape[0] == 0
            or values.shape[1] == 0 or not np.isfinite(values).all()):
        raise ValueError("Expected finite time-by-node-by-XYZ displacement field")
    return values


def exact_epoch_index(times, requested_time):
    times = np.asarray(times, dtype=np.float64)
    if times.ndim != 1 or not np.isfinite(times).all() or not np.all(np.diff(times) > 0):
        raise ValueError("Times must be finite and strictly increasing")
    matches = np.flatnonzero(times == requested_time)
    if len(matches) != 1:
        raise ValueError("Requested physical epoch is absent; interpolation is not implicit")
    return int(matches[0])


def triangle_weights(xy, triangles, stations, *, tolerance=1e-9):
    xy = np.asarray(xy, dtype=np.float64)
    connectivity = np.asarray(triangles)
    stations = np.asarray(stations, dtype=np.float64)
    if (xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 3
            or stations.ndim != 2 or stations.shape[1] != 2):
        raise ValueError("Expected XY nodes and XY stations")
    if not np.isfinite(xy).all() or not np.isfinite(stations).all():
        raise ValueError("Node and station coordinates must be finite")
    if not np.isfinite(tolerance) or tolerance < 0:
        raise ValueError("The support tolerance must be finite and non-negative")
    if (connectivity.ndim != 2 or connectivity.shape[1] != 3
            or len(connectivity) == 0 or connectivity.dtype.kind not in "iuf"):
        raise ValueError("Triangle connectivity must contain nonempty integer indices")
    if (not np.isfinite(connectivity).all() or np.any(connectivity < 0)
            or np.any(connectivity >= len(xy))
            or np.any(connectivity != np.floor(connectivity))):
        raise ValueError("Invalid triangle connectivity")
    triangles = connectivity.astype(np.int64)
    vertices = xy[triangles]
    basis = np.stack((vertices[:, 1] - vertices[:, 0], vertices[:, 2] - vertices[:, 0]), axis=2)
    determinant = np.linalg.det(basis)
    if np.any(determinant <= 0):
        raise ValueError("Triangles must have positive signed area")
    inverse = np.linalg.inv(basis)
    weights = np.zeros((len(stations), len(xy)), dtype=np.float64)
    supports = []
    for index, station in enumerate(stations):
        uv = np.einsum("tij,tj->ti", inverse, station - vertices[:, 0])
        barycentric = np.column_stack((1 - uv.sum(axis=1), uv))
        inside = np.flatnonzero((barycentric >= -tolerance).all(axis=1))
        if len(inside) == 0:
            raise ValueError(f"Station {index} lies outside the supported mesh")
        chosen = int(inside[0])
        weights[index, triangles[chosen]] = barycentric[chosen]
        supports.append(chosen)
    return weights, np.asarray(supports, dtype=np.int64)


def relative_vectors(displacements, weights, time_index, zero_index, *, frame_id,
                     observation_frame_id, unit="m", reference_weights=None):
    values = validate_state(displacements, frame_id=frame_id,
                            observation_frame_id=observation_frame_id, unit=unit)
    weights = np.asarray(weights, dtype=np.float64)
    if weights.ndim != 2 or weights.shape[1] != values.shape[1] or not np.isfinite(weights).all():
        raise ValueError("Station support weights do not match the model nodes")
    if not np.allclose(weights.sum(axis=1), 1, rtol=0, atol=1e-12):
        raise ValueError("Every station support must preserve constant displacement")
    if any(isinstance(index, (bool, np.bool_)) or not isinstance(index, Integral)
           for index in (time_index, zero_index)):
        raise ValueError("Epoch indices must be integers")
    if not (0 <= zero_index < len(values) and 0 <= time_index < len(values)):
        raise ValueError("Requested epoch index is outside the state")
    delta = values[time_index] - values[zero_index]
    predicted = weights @ delta
    if reference_weights is not None:
        reference = np.asarray(reference_weights, dtype=np.float64)
        if reference.shape != (values.shape[1],) or not np.isfinite(reference).all():
            raise ValueError("Reference support does not match the model nodes")
        if not np.isclose(reference.sum(), 1, rtol=0, atol=1e-12):
            raise ValueError("Reference support must preserve constant displacement")
        predicted -= reference @ delta
    return predicted


def leveling(displacements, weights, time_index, zero_index, *, positive_down=False, **kwargs):
    vertical = relative_vectors(displacements, weights, time_index, zero_index, **kwargs)[:, 2]
    return -vertical if positive_down else vertical


def gnss(displacements, weights, time_index, zero_index, *, model_to_enu, **kwargs):
    rotation = np.asarray(model_to_enu, dtype=np.float64)
    if rotation.shape != (3, 3) or not np.isfinite(rotation).all():
        raise ValueError("An explicit finite model-to-ENU rotation is required")
    if not np.allclose(rotation @ rotation.T, np.eye(3), rtol=0, atol=1e-12) or not np.isclose(np.linalg.det(rotation), 1, rtol=0, atol=1e-12):
        raise ValueError("The ENU rotation must be orthonormal and right-handed")
    vector = relative_vectors(displacements, weights, time_index, zero_index, **kwargs)
    return vector @ rotation.T


def insar(displacements, weights, time_index, zero_index, *, model_to_enu,
          enu_toward_satellite, positive_toward_satellite, **kwargs):
    if not isinstance(positive_toward_satellite, (bool, np.bool_)):
        raise ValueError("The LOS sign convention must be explicitly boolean")
    vectors = gnss(displacements, weights, time_index, zero_index,
                   model_to_enu=model_to_enu, **kwargs)
    los = np.asarray(enu_toward_satellite, dtype=np.float64)
    if los.shape == (3,):
        los = np.broadcast_to(los, vectors.shape)
    if los.shape != vectors.shape or not np.isfinite(los).all():
        raise ValueError("LOS geometry must be defined at every station")
    if not np.allclose(np.linalg.norm(los, axis=1), 1, rtol=0, atol=1e-12):
        raise ValueError("LOS directions must already be unit vectors")
    projected = np.einsum("ij,ij->i", vectors, los)
    return projected if positive_toward_satellite else -projected


def unique_evidence_groups(records):
    """Group records by their resolved primary physical-series identity.

    Callers assign distinct IDs to independent measurements and share an ID
    across copies. Source files and locators retain provenance without creating
    additional independent observations.
    """
    groups = {}
    for record in records:
        key = record["physical_series_id"]
        groups.setdefault(key, []).append(record["record_id"])
    return groups
