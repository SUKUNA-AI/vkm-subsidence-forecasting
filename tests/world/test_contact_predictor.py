"""Regression tests for review findings R2-01..R2-03 (17.10.2026) and partial observations.

All coordinates are synthetic; expected values are computed analytically here,
not by calling the helper under test twice.
"""
import numpy as np
import pytest
from shapely.geometry import LineString, Polygon

from vkm_world.geometry_build.constrained_geology import joint_predict
from vkm_world.geometry_build.contact_predictor import (
    ContactPredictor, PredictorSpec, SupportSet, cross_validate, cv_blocks, geometry_digest)
from vkm_world.geometry_build.tin_section import compare_continuous, project_onto_trace, section_through_tin
from vkm_world.geometry_build.linked_geology import plan_triangles

CORNERS = np.array([[0., 0.], [2., 0.], [0., 2.], [2., 2.]])


def bump(xy):
    """Non-flat relief: 100 m at the corners, 120 m at (1, 1)."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    x, y = xy[:, 0]/2, xy[:, 1]/2
    return 100+320*x*(1-x)*y*(1-y)


def corner_supports():
    return SupportSet.from_depths(["a", "b", "c", "d"], CORNERS, bump(CORNERS), [[200, 230]]*4, ["T", "B"])


def test_r2_01_review_control_case_surface_vs_level_operator():
    s = corner_supports()
    surface = ContactPredictor(PredictorSpec("idw2", "SURFACE"), surface=bump).predict(s, [[1, 1]])
    level = ContactPredictor(PredictorSpec("idw2", "LEVEL")).predict(s, [[1, 1]])
    np.testing.assert_allclose(surface["levels"][0], [-80, -110])   # H(q) - sum w d
    np.testing.assert_allclose(level["levels"][0], [-100, -130])    # sum w (H_i - d_i)


def test_r2_01_cv_scores_the_published_operator():
    # Held centre well: collar on the relief (120 m), same depths. The SURFACE
    # build predicts it exactly; the LEVEL operator misses by -20 m (synthetic).
    xy = np.vstack([CORNERS, [[1, 1]]])
    s = SupportSet.from_depths(list("abcde"), xy, bump(xy), [[200, 230]]*5, ["T", "B"])
    for parameterization, expected in (("SURFACE", 0.0), ("LEVEL", -20.0)):
        factory = lambda m, train, p=parameterization: ContactPredictor(PredictorSpec(m, p), surface=bump)
        cv = cross_validate(factory, s, ["idw2"])
        row = next(r for r in cv["records"] if r["fold"] == "leave_one_physical_borehole_out" and r["well"] == "e")
        np.testing.assert_allclose(row["level_error_m"], [expected, expected], atol=1e-9)
        np.testing.assert_allclose(row["thickness_error_m"], [0.0], atol=1e-9)


def test_datum_offset_and_flat_relief():
    s = SupportSet.from_depths(list("abcd"), CORNERS, np.full(4, 55.0), [[10, 30]]*4, ["T", "B"])
    flat = lambda xy: np.full(len(np.asarray(xy).reshape(-1, 2)), 50.0)
    # Collars sampled 5 m above the operator are reconciled at the supports.
    r = ContactPredictor(PredictorSpec("idw2", "SURFACE", site_offset_m=5.0), surface=flat).predict(s, [[1, 1], [0, 0]])
    np.testing.assert_allclose(r["levels"], [[45, 25], [45, 25]])
    r2 = ContactPredictor(PredictorSpec("idw2", "SURFACE", site_offset_m=0.0), surface=flat).predict(s, [[1, 1], [0, 0]])
    np.testing.assert_allclose(r2["levels"][1], [45, 25])  # direct support reproduces its own level
    np.testing.assert_allclose(r2["levels"][0], [45, 25])  # 50 + (55-10-50)


def test_dsm_sampling_difference_is_reconciled_only_at_supports():
    collars = bump(CORNERS)+np.array([0.3, -0.2, 0.1, 0.0])  # e.g. bilinear vs TIN sampling
    s = SupportSet.from_depths(list("abcd"), CORNERS, collars, [[200, 230]]*4, ["T", "B"])
    r = ContactPredictor(PredictorSpec("idw2", "SURFACE"), surface=bump).predict(s, np.vstack([CORNERS, [[1, 1]]]))
    np.testing.assert_allclose(r["levels"][:4], (collars[:, None]-[200, 230]))
    w = np.full(4, .25)
    np.testing.assert_allclose(r["levels"][4], 120+w@((collars-100)[:, None]-[200, 230]))


def test_complete_columns_match_joint_predict_exactly():
    rng = np.random.default_rng(3)
    xy = rng.uniform(0, 10, (7, 2))
    depth = np.sort(rng.uniform(0, 300, (7, 4)), axis=1)
    s = SupportSet.from_depths(list("abcdefg"), xy, np.full(7, 100.), depth, list("PQRS"))
    q = rng.uniform(1, 9, (20, 2))
    for method in ("nearest", "idw2", "linear"):
        r = ContactPredictor(PredictorSpec(method, "LEVEL")).predict(s, q)
        expected, masks, _ = joint_predict(xy, 100-depth, q, method)
        np.testing.assert_allclose(r["levels"], expected, equal_nan=True)


def test_partial_reading_is_used_without_filling_the_gap():
    depth = [[10, 50, 90], [10, 50, 90], [10, 50, 90], [20, None, None]]
    xy = np.array([[0, 0], [4, 0], [0, 4], [1, 1]], float)
    s = SupportSet.from_depths(list("abcd"), xy, np.zeros(4), depth, list("XYZ"))
    with pytest.raises(ValueError, match="partial"):
        ContactPredictor(PredictorSpec("idw2", "LEVEL")).predict(s, [[1, 1]])
    r = ContactPredictor(PredictorSpec("idw2", "LEVEL", allow_partial=True)).predict(s, [[1, 1], [2, 1]])
    # Direct support: own reading reproduced; missing contacts inherit the local
    # thicknesses (40, 40 m) of the other supports, not their mean levels.
    np.testing.assert_allclose(r["levels"][0], [-20, -60, -100])
    assert np.all(np.diff(r["levels"], axis=1) <= 0)
    # Away from d the shallow TKT reading pulls X down by the weighted mean only
    w = np.array([np.linalg.norm(p-[2, 1])**-2 for p in xy]); w /= w.sum()
    np.testing.assert_allclose(r["levels"][1, 0], -(w[:3].sum()*10+w[3]*20))


def test_not_reached_interval_acts_only_when_violated_and_order_is_projected():
    # Support c did not reach contact Y: depth(Y) >= 120 there.
    lower = np.array([[10, 50], [10, 50], [10, np.nan], [10, 50]], float)
    upper = np.array([[10, 50], [10, 50], [10, np.nan], [10, 50]], float)
    lower[2, 1] = 120.0
    xy = np.array([[0, 0], [2, 0], [1, 2], [1, 4]], float)
    ids = list("abcd")
    s = SupportSet(ids, ids, xy, np.zeros(4), lower, upper, ["X", "Y"])
    r = ContactPredictor(PredictorSpec("idw2", "LEVEL", allow_partial=True)).predict(s, [[1, 2], [1, .01]])
    assert r["levels"][0, 1] <= -120+1e-6      # at c the bound is honoured
    assert r["levels"][1, 1] > -120             # near a/b the reading 50 m dominates
    loose = lower.copy(); loose[2, 1] = 20.0      # satisfied interval contributes nothing
    s2 = SupportSet(ids, ids, xy, np.zeros(4), loose, upper, ["X", "Y"])
    r2 = ContactPredictor(PredictorSpec("idw2", "LEVEL", allow_partial=True)).predict(s2, [[1, 1]])
    np.testing.assert_allclose(r2["levels"][0, 1], -50)


def test_zero_thickness_and_undetermined_contact():
    s = SupportSet.from_depths(list("abc"), [[0, 0], [1, 0], [0, 1]], np.zeros(3), [[5, 5, None]]*3, list("XYZ"))
    r = ContactPredictor(PredictorSpec("idw2", "LEVEL", allow_partial=True)).predict(s, [[.3, .3]])
    np.testing.assert_allclose(r["levels"][0, :2], [-5, -5])
    assert np.isnan(r["levels"][0, 2])  # no level reading anywhere: UNKNOWN, no regulariser


def test_thickness_reading_couples_contacts():
    s = SupportSet.from_depths(list("abc"), [[0, 0], [1, 0], [0, 1]], np.zeros(3), [[5, None]]*3, list("XY"),
                               thickness=[{"row": 0, "top": 0, "bottom": 1, "lower": 7.0, "upper": 7.0}])
    r = ContactPredictor(PredictorSpec("idw2", "LEVEL", allow_partial=True)).predict(s, [[.3, .3]])
    np.testing.assert_allclose(r["levels"][0], [-5, -12], atol=1e-8)


def test_cv_withholds_copies_of_one_physical_object():
    xy = np.array([[0, 0], [4, 0], [0, 4], [4, 4], [2, 2], [2.1, 2]], float)
    s = SupportSet.from_depths(list("abcdef"), xy, np.zeros(6), [[10, 40]]*6, ["T", "B"],
                               groups=["a", "b", "c", "d", "e", "e"])
    cv = cross_validate(lambda m, t: ContactPredictor(PredictorSpec(m, "LEVEL")), s, ["linear"])
    loo = [r for r in cv["records"] if r["fold"] == "leave_one_physical_borehole_out" and r["label"] == "e"]
    assert {r["well"] for r in loo} == {"e", "f"} and all(r["training_wells"] == 4 for r in loo)


def test_r2_02_interior_block_predicts_and_extrapolation_follows_policy():
    xy = np.array([[x, y] for x in range(4) for y in range(3)], float)
    s = SupportSet.from_depths([str(i) for i in range(12)], xy, np.zeros(12), [[10, 40]]*12, ["T", "B"])
    # Median-X halves (old design): no interpolation is possible by construction.
    old = ["west" if x <= np.median(xy[:, 0]) else "east" for x in xy[:, 0]]
    cv = cross_validate(lambda m, t: ContactPredictor(PredictorSpec(m, "LEVEL")), s, ["linear"], block_size_m=1, groups_for_blocks=old)
    halves = [r for r in cv["records"] if r["fold"] == "spatial_block"]
    assert all(r["position"] == "EXTRAPOLATION" and r["predicted_contacts"] == 0 for r in halves)
    # Fixed 1x1 blocks: interior wells are interpolation tests and are predicted.
    cv2 = cross_validate(lambda m, t: ContactPredictor(PredictorSpec(m, "LEVEL", extrapolate=(m != "linear"))), s,
                         ["linear", "idw2"], block_size_m=1.0)
    s2 = cv2["summary"]
    assert s2["spatial_block/INTERPOLATION/linear"]["predicted"] == s2["spatial_block/INTERPOLATION/linear"]["attempted"] > 0
    assert s2["spatial_block/EXTRAPOLATION/linear"]["predicted"] == 0
    assert s2["spatial_block/EXTRAPOLATION/idw2"]["predicted"] == s2["spatial_block/EXTRAPOLATION/idw2"]["attempted"]


def test_blocks_depend_on_coordinates_only():
    assert cv_blocks([[0, 0], [1500, 10], [3100, 2900]], 1500) == ["B0_0", "B1_0", "B2_1"]


def test_r2_03_exact_p1_section_differs_from_continuous_idw():
    # Review control: IDW at (0.5, 0.5) = 1.428571, P1 triangle = 2.5.
    nodes = np.array([[0, 0], [2, 0], [0, 2]], float)
    values = np.array([0., 10., 0.])
    sec = section_through_tin(nodes, [[0, 1, 2]], values, [[0.5, 0.5], [0.5, 0.5+1e-9]])
    np.testing.assert_allclose(sec["values"][0, 0], 2.5)
    w = np.linalg.norm(nodes-[.5, .5], axis=1)**-2; w /= w.sum()
    assert abs(w@values-1.4285714285714284) < 1e-12
    cmp = compare_continuous(sec, np.array([[w@values]]*len(sec["values"])))
    assert cmp["max_abs_m_on_samples"] > 1.0 and "not a supremum" in cmp["scope"]


def test_section_keeps_every_edge_crossing_and_turning_point():
    nodes = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float)
    tris = np.array([[0, 1, 2], [0, 2, 3]])
    z = np.array([0., 0., 10., 0.])  # ridge only at node 2: P1 kink on the diagonal
    sec = section_through_tin(nodes, tris, z, [[0, .5], [1, .5], [1, .9]])
    xs = sec["xy"]
    assert any(np.allclose(p, [.5, .5]) for p in xs)          # diagonal crossing kept
    assert any(np.allclose(p, [1, .5]) for p in xs)           # turning vertex kept once
    i = int(np.argmin(np.linalg.norm(xs-[.5, .5], axis=1)))
    np.testing.assert_allclose(sec["values"][i, 0], 5.0)      # exact P1 on the diagonal
    j = int(np.argmin(np.linalg.norm(xs-[1, .5], axis=1)))
    np.testing.assert_allclose(sec["values"][j, 0], 5.0)
    np.testing.assert_allclose(sec["chainage_m"][-1], 1.4)
    outside = section_through_tin(nodes, tris, z, [[2, 2], [3, 3]])
    assert np.isnan(outside["values"]).all()


def test_projection_reports_offset_distance():
    p = project_onto_trace([[1, 1], [5, 0]], [[0, 0], [4, 0]])
    np.testing.assert_allclose(p["distance_m"], [1, 1]); np.testing.assert_allclose(p["chainage_m"], [1, 4])


def test_geometry_digest_changes_with_contacts():
    n = np.zeros((3, 2)); t = [[0, 1, 2]]
    a = geometry_digest(n, t, [[1, 0]]*3)
    assert a == geometry_digest(n, t, [[1, 0]]*3) and a != geometry_digest(n, t, [[1, -1e-6]]*3)


def test_review_triangulation_regression_no_hanging_or_false_boundary():
    polygon = Polygon([(0, 0), (2, 0), (2, .2), (1, .5), (2, .8), (2, 2), (0, 2)])
    t = np.asarray(plan_triangles(polygon, 1.0))
    xy, idx = np.unique(t.reshape(-1, 2), axis=0, return_inverse=True)
    c = idx.reshape(-1, 3)
    edges = np.sort(np.concatenate([c[:, [0, 1]], c[:, [1, 2]], c[:, [2, 0]]]), axis=1)
    edges, counts = np.unique(edges, axis=0, return_counts=True)
    for a, b in edges[counts == 1]:
        assert LineString(xy[[a, b]]).difference(polygon.boundary.buffer(1e-9)).length <= 1e-7
    for a, b in edges:
        v = xy[b]-xy[a]
        for i, p in enumerate(xy):
            if i in (a, b):
                continue
            s = (p-xy[a])@v/(v@v)
            assert not (1e-8 < s < 1-1e-8 and np.linalg.norm(p-xy[a]-s*v) < 1e-9)
    assert abs(sum(Polygon(x).area for x in t)-polygon.area) < 1e-9


def test_section_marks_gap_across_mesh_notch_and_snaps_boundary_roundoff():
    nodes = np.array([[0, 0], [1, 0], [1, 1], [0, 1], [2, 1], [2, 0], [3, 0], [3, 1]], float)
    tri = np.array([[0, 1, 2], [0, 2, 3], [5, 6, 7], [5, 7, 4]])
    sec = section_through_tin(nodes, tri, np.arange(8.0), [[0.0, 0.5], [3.0, 0.5]])
    assert sec["gaps"] == 1
    i = int(np.flatnonzero(sec["gap_after"])[0])
    np.testing.assert_allclose(sec["xy"][i], [1, .5]); np.testing.assert_allclose(sec["xy"][i+1], [2, .5])
    edge = section_through_tin(nodes[:4], tri[:2], np.arange(4.0), [[0, 1e-13], [1, -1e-13]])
    assert np.isfinite(edge["values"]).all()


def test_two_sided_interval_reaches_the_bounded_least_squares_optimum():
    # Auditor case: the violated side switches during an active-set iteration.
    from scipy.optimize import minimize
    from vkm_world.geometry_build.contact_predictor import _solve_node
    rng = np.random.default_rng(512)
    checked = 0
    for _ in range(25):
        n, k = 6, 4
        lo = np.full((n, k), -np.inf); hi = np.full((n, k), np.inf)
        for i in range(n):
            z = -np.sort(rng.uniform(0, 100, k))
            lo[i, 0] = hi[i, 0] = z[0]
            for c in range(1, k):
                u = rng.random()
                if u < .4:
                    lo[i, c] = hi[i, c] = z[c]
                elif u < .8:
                    lo[i, c] = z[c]-rng.uniform(0, 10); hi[i, c] = lo[i, c]+rng.uniform(0, 8)
        th = [{"row": int(rng.integers(n)), "top": 1, "bottom": 2, "lower": float(rng.uniform(0, 20)), "upper": None}]
        w = rng.uniform(.05, 1, n); w /= w.sum()
        z, _ = _solve_node(w, lo, hi, th, k)
        if not np.isfinite(z).all():
            continue  # an undetermined contact is NaN by design (no regulariser)
        checked += 1

        point = np.isfinite(lo) & np.isfinite(hi) & (np.abs(hi-lo) <= 1e-9)

        def objective(v):
            # Increment form: first point level and gaps between consecutive point readings,
            # plus squared distance to interval readings and the soft thickness bound.
            zz = np.r_[v[0], v[0]-np.cumsum(v[1:])]
            f = 0.0
            for i in range(n):
                obs = np.flatnonzero(point[i])
                f += w[i]*(zz[obs[0]]-lo[i, obs[0]])**2
                for ca, cb in zip(obs[:-1], obs[1:]):
                    f += w[i]*((zz[ca]-zz[cb])-(lo[i, ca]-lo[i, cb]))**2
                for c in np.flatnonzero(~point[i]):
                    f += w[i]*(max(lo[i, c]-zz[c], 0)+max(zz[c]-hi[i, c], 0))**2
            tt = zz[1]-zz[2]
            return f+w[th[0]["row"]]*max(th[0]["lower"]-tt, 0)**2
        start = np.r_[z[0], -np.diff(z)]
        ref = minimize(objective, start, bounds=[(None, None)]+[(0, None)]*(k-1), method="L-BFGS-B",
                       options={"ftol": 1e-15, "gtol": 1e-12, "maxiter": 5000})
        assert objective(start) <= ref.fun*(1+1e-6)+1e-9
    assert checked >= 10
