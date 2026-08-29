# Mathematical methodology v3

## Spatial reconstruction
Plan polygons are extracted from red vector linework in Figure 13. The segmentation mask uses hue/channel separation rather than grayscale, preventing the settlement heatmap from becoming geometry. The local affine bounds are calibrated against published local-coordinate anchors.

## Settlement map
The Figure 22 color scale is digitized into a reference-year field. Source evidence supports the year 2022, but not a precise date. Exact published `Disp_min`, `Disp_mean`, and `Disp_max` values condition non-overlapping local support cells by a monotone rank-power transform.

## Measurement simulation
Raw leveling equations are generated as observed height differences. Adjustment solves `A h = l` by weighted least squares with independent benchmark-height constraints. Hidden truth is not passed into the solver. GNSS common mode is estimated from stable reference points. InSAR observations are stored relative to the first acquisition.

## Derived surveying quantities
- Settlement: eta = H_previous - H_current.
- Tilt: difference of settlements divided by profile interval length.
- Curvature: difference of adjacent tilts divided by distance between interval midpoints.
- Horizontal strain: interval-length change divided by initial interval length.
Uncertainties are propagated by first-order variance rules.
