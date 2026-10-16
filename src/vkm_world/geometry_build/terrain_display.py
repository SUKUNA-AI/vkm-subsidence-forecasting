"""Explicit display smoothing of a complete DSM raster.

The result is a visualization choice, never a new bare-earth measurement or a
vertical-datum conversion. Callers retain the unmodified source raster and record
the filter scale, edge mode, and displacement from the source.
"""
from __future__ import annotations

import math
import numpy as np
from scipy.ndimage import gaussian_filter


def smooth_raster_values(values, rows: int, cols: int, sigma_px: float):
    """Return float64 Gaussian-smoothed values in the input shape.

    ``values`` must be either a complete row-major raster or its flat row-major
    representation. There is no NoData filling, resampling, global height shift,
    or inferred terrain correction. Reflecting edges prevent zero padding from
    depressing the boundary. The kernel is truncated at four standard deviations.
    """
    if (isinstance(rows, bool) or isinstance(cols, bool)
            or not isinstance(rows, (int, np.integer))
            or not isinstance(cols, (int, np.integer)) or rows < 1 or cols < 1):
        raise ValueError("positive integer raster dimensions required")
    if not math.isfinite(sigma_px) or sigma_px <= 0:
        raise ValueError("positive finite smoothing sigma required")
    source = np.asarray(values, dtype=np.float64)
    if source.shape not in ((rows * cols,), (rows, cols)):
        raise ValueError("complete row-major raster or flat raster required")
    if not np.isfinite(source).all():
        raise ValueError("finite complete raster required; NoData is not filled")
    if np.ptp(source) == 0:
        return source.copy()
    result = gaussian_filter(source.reshape(rows, cols), sigma=float(sigma_px),
                             mode="reflect", truncate=4.0, output=np.float64)
    if not np.isfinite(result).all():
        raise ValueError("filter produced nonfinite raster values")
    return result.reshape(source.shape)
