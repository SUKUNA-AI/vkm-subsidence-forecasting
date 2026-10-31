"""Uncertainty ensemble of the engineering geology model v6_2 (conditional Gaussian, GPU).

Stages (PREREGISTRATION.json fixes every rule before scoring):

* ``cv``: leave-one-physical-borehole-out and fixed 1.5 km block folds; covariance parameters
  and the covariance type are taken ONLY from the training supports of each fold; coverage of
  P10-P90 / P05-P95, CRPS and |P50 error| for withheld readings, interpolation and
  extrapolation separately, variants G (collar = DSM) and P90 (plan-placement collar noise);
* ``grid``: the covariance type per field chosen by the same rule on all supports, then
  >= 5000 conditional realizations of 6 contacts on the 19 128 v6_2 nodes, bands on sections
  and along profile line 12, one-sided checks, comparison with the v6_2 surfaces.

Realizations are synthetic, never observations; calibration is a consistency check of the
uncertainty model, not field validation. Real inputs and outputs stay in ignored ``work/``
or in PRIVATE; this script contains no machine paths.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import inspect
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from vkm_world.geometry_build import gp_ensemble as ge  # noqa: E402
from vkm_world.geometry_build.column_display import TINSurface  # noqa: E402

CONTACTS = ["TKT", "SMT", "PKS", "SKZ", "SZ", "PDKS"]
FIELDS = ["TOP_LOGDEPTH", "H_TKT_SMT", "H_SMT_PKS", "H_PKS_SKZ", "H_SKZ_SZ", "H_SZ_PDKS"]
MASTER_SEED = 20261023
LINE12 = ((2680.0, -960.0), (3710.0, -960.0))
KR2_OFFSETS_M = (7.5, 16.0)
KR2_THICKNESS_M = 5.97
# Historical recipe retained for diagnosis, not an accepted local geological law.
KR2_RECIPE_STATUS = "MODEL_CHOICE; pooled offsets and fixed mean thickness; local applicability unresolved"
TOL_M = 0.01  # float32 rounding of levels at exactly honoured supports


def generation_settings(args):
    """Resolved sampling settings; defaults omitted from CLI still affect draw identity."""
    return {
        "master_seed": MASTER_SEED,
        "seed_streams": "SeedSequence spawn keys: plan=(0,), CV=(1,), grid=(2,); ordered child streams",
        "draw_field_support_chunk": inspect.signature(ge.draw_field).parameters["chunk"].default,
        "draw_field_censoring_sweeps": 30,
        "simulate_on_nodes_realization_batch": args.batch,
        "realization_counts": {"G": args.realizations_g, "P90": args.realizations_p90, "P200": args.realizations_p200},
        "cv_draws": args.cv_draws,
        "grid_backend": "torch", "grid_dtype": "float32", "device": args.device,
        "ordering": "input support/node order; appended plan supports; variant/field order as in pinned code",
        "reproducibility_contract": "Batch/chunk changes may preserve the sampling law while changing fixed-seed draw paths. "
                                    "A seed alone does not identify a realization.",
        "world_id_scope": "variant:Rindex identifies a row only within this generation identity and hashed output; "
                          "not a persistent world ID across generation settings"}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    def clean(o):
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, float)):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.ndarray):
            return clean(o.tolist())
        return o
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(clean(value), ensure_ascii=False, indent=1, allow_nan=False) + "\n", encoding="utf-8")


def decode(spec):
    return np.frombuffer(base64.b64decode(spec["data"]), dtype="<" + spec["dtype"]).reshape(spec["shape"])


# --------------------------------------------------------------------------- inputs

def load_inputs(args):
    model = args.model_dir
    mapped = {r["physical_id"]: r for r in read(model / "mapped_boreholes.json")}
    qa = read(model / "FULL_CONTEXT_PARTIAL" / "qa.json")
    ids = list(qa["physical_groups"])
    rows = [mapped[i] for i in ids]
    fields = np.load(model / "FULL_CONTEXT_PARTIAL" / "contact_fields.npz")
    if str(fields["geometry_version"]) != args.geometry_version:
        raise ValueError("geometry version differs from the accepted v6_2 model")
    obs = read(args.observations)
    absent, checks = [], []
    index = {pid: i for i, pid in enumerate(ids)}
    for r in obs["rows"]:
        if r["physical_id"] not in index:
            continue
        if r["kind"] == "absent" and r.get("use_in_model") is True:
            absent.append((index[r["physical_id"]], CONTACTS.index(r["top_contact"]), CONTACTS.index(r["bottom_contact"])))
        if r["kind"] == "not_reached" and str(r.get("model_decision", "")).startswith("CHECK ONLY"):
            checks.append({"row": index[r["physical_id"]], "contact": CONTACTS.index(r["contact"]),
                           "deeper_than_m": float(r["lower_m"] if r.get("lower_m") is not None else r["value_m"]),
                           "physical_id": r["physical_id"], "contact_name": r["contact"]})
    depth = np.array([[np.nan if v is None else float(v) for v in r["depths_m"]] for r in rows])
    readings = ge.LayeredReadings(ids, [[r["x"], r["y"]] for r in rows], depth, CONTACTS, absent, checks)
    collar = np.array([r["terrain_height_m"] for r in rows], float)
    scene_text = args.previous_scene.read_text(encoding="utf-8")
    scene = json.loads(scene_text.split("=", 1)[1].strip().removesuffix(";"))
    del scene_text
    surface = TINSurface(np.column_stack([decode(scene["terrain_xy"]).reshape(-1, 2), decode(scene["terrain_raw_z"]).ravel()]),
                         decode(scene["terrain_triangles"]).reshape(-1, 3))
    # v6_2 collar = terrain_height_m (bilinear raster); the SURFACE operator samples the TIN, so the
    # build's own collar sits base_shift = collar - S(x) above the TIN at every support (<= 0.4 m).
    base_shift = collar - surface.sample(readings.xy)
    return {"ids": ids, "rows": rows, "readings": readings, "collar": collar, "surface": surface, "fields": fields,
            "base_shift": base_shift,
            "qa": qa, "absent": absent, "checks": checks}


class PlanSampler:
    """Plan registration (PREREGISTRATION amendment A1): position jitter of whole boreholes.

    Every realization moves each support uniformly inside a disc of ``radius_m``; depths below
    the DSM are kept (SURFACE parameterization). The only vertical term is the lake collar of
    BH-SOL-74 (true collar 0..9.06 m above the DSM water surface). ENGINEERING_ASSUMPTION.
    """

    WATER = {"BH-SOL-74": (0.0, 9.06)}

    def __init__(self, xy, ids, radius_m, seed, pool=32):
        self.xy, self.ids, self.radius_m = np.asarray(xy, float), list(ids), radius_m
        rng = np.random.default_rng(seed)
        self.candidates = self.xy[:, None, :] + self._disc(rng, (len(ids), pool))

    def _disc(self, rng, shape):
        r = self.radius_m * np.sqrt(rng.random(shape))
        t = 2 * np.pi * rng.random(shape)
        return np.stack([r * np.cos(t), r * np.sin(t)], axis=-1)

    def positions(self, rng, draws):
        """Continuous jitter for cross-validation: (draws, n, 2)."""
        return self.xy[None] + self._disc(rng, (draws, len(self.ids)))

    def pool_choice(self, rng, draws):
        """Candidate index per realization and support for the grid: (draws, n)."""
        return rng.integers(0, self.candidates.shape[1], (draws, len(self.ids)))

    def vertical(self, rng, draws):
        out = np.zeros((draws, len(self.ids)))
        for pid, (lo, hi) in self.WATER.items():
            if pid in self.ids:
                out[:, self.ids.index(pid)] = rng.uniform(lo, hi, draws)
        return out

    def describe(self):
        return {"kind": "POSITION_JITTER_UNIFORM_DISC", "radius_m": self.radius_m, "grid_pool_per_support": int(self.candidates.shape[1]),
                "vertical_terms": {k: {"rule": "U(%g, %g) m above the DSM water surface" % v} for k, v in self.WATER.items()},
                "depths_below_dsm": "unchanged (SURFACE parameterization)"}


def make_samplers(data, seeds):
    xy, ids = data["readings"].xy, data["ids"]
    return {"P90": PlanSampler(xy, ids, 90.0, seeds["P90"]), "P200": PlanSampler(xy, ids, 200.0, seeds["P200"])}


# --------------------------------------------------------------------------- cross-validation

def folds_from_v62(cv, ids):
    loo = [("leave_one_physical_borehole_out", pid, [ids.index(pid)]) for pid in ids]
    blocks = {}
    for r in cv["records"]:
        if r["fold"] == "spatial_block":
            blocks.setdefault(r["label"], set()).add(r["well"])
    block_folds = [("spatial_block", b, sorted(ids.index(w) for w in wells)) for b, wells in sorted(blocks.items())]
    return loo + block_folds


def position(train_xy, point):
    from shapely.geometry import MultiPoint, Point

    if len(train_xy) < 3:
        return "EXTRAPOLATION"
    hull = MultiPoint([tuple(p) for p in train_xy]).convex_hull
    return "INTERPOLATION" if hull.buffer(1e-6).covers(Point(point)) else "EXTRAPOLATION"


def selected_kinds(readings, train_rows, grid):
    kinds, evidence = [], []
    for f in range(readings.n_fields):
        rows, value, censored = readings.field_readings(f, train_rows)
        best, ev = ge.select_covariance_type(readings.xy[rows[~censored]], value[~censored], grid=grid)
        kinds.append(best)
        evidence.append(ev)
    return kinds, evidence


def run_cv(args, data, grid, samplers):
    readings, ids = data["readings"], data["ids"]
    cv62 = read(args.v62_cv)
    idw = {(r["fold"], r["label"], r["well"]): r for r in cv62["records"] if r["method"] == "idw2"}
    folds = folds_from_v62(cv62, ids)
    if args.max_folds:
        folds = folds[:args.max_folds]
    configs = ["SELECTED"] + list(ge.COVARIANCE_TYPES)
    variants = ["G", "P90"]
    seq = np.random.SeedSequence(MASTER_SEED, spawn_key=(1,))
    children = seq.spawn(len(folds) * len(variants) * len(configs))
    records, fold_meta = [], []
    t0 = time.perf_counter()
    c = 0
    for kind, label, held in folds:
        train = np.array([i for i in range(len(ids)) if i not in held])
        sel, evidence = selected_kinds(readings, train, grid)
        fold_meta.append({"fold": kind, "label": label, "held": [ids[i] for i in held], "selected_types": dict(zip(FIELDS, sel)),
                          "log_evidence": dict(zip(FIELDS, evidence))})
        for variant in variants:
            for config in configs:
                rng = np.random.default_rng(children[c]); c += 1
                if variant != "G" and config != "SELECTED":
                    continue  # amendment A1: post-hoc type comparison for G only
                kinds = sel if config == "SELECTED" else [config] * readings.n_fields
                shifts = np.broadcast_to(data["base_shift"], (args.cv_draws, len(ids))).copy()
                pos_xy = None
                if variant != "G":
                    shifts += samplers[variant].vertical(rng, args.cv_draws)
                    pos_xy = samplers[variant].positions(rng, args.cv_draws)
                pred = ge.layered_predictive(rng, readings, train, readings.xy[held], kinds, args.cv_draws, grid,
                                             train_shift=shifts, sweeps=30, train_xy=pos_xy,
                                             target_xy=None if pos_xy is None else pos_xy[:, held])
                for j, i in enumerate(held):
                    pos = position(readings.xy[train], readings.xy[i])
                    base = {"fold": kind, "label": label, "well": ids[i], "variant": variant, "config": config, "position": pos,
                            "types": kinds}
                    e_i = shifts[:, i]
                    point = np.isfinite(readings.depth[i]) & ~readings.filled[i]
                    ref = idw.get((kind, label, ids[i]))
                    for k in range(readings.n_fields):
                        if point[k]:
                            s = ge.score_draws(pred["depth"][:, j, k] + e_i, readings.depth[i, k])
                            err = None if ref is None or ref["level_error_m"][k] is None else abs(ref["level_error_m"][k])
                            records.append({**base, "quantity": "depth", "item": CONTACTS[k], **s, "idw2_abs_error": err,
                                            "idw2_position": None if ref is None else ref["position"]})
                    for k in range(readings.n_fields - 1):
                        if point[k] and point[k + 1]:
                            s = ge.score_draws(pred["thickness"][:, j, k], readings.depth[i, k + 1] - readings.depth[i, k])
                            err = None if ref is None or ref["thickness_error_m"][k] is None else abs(ref["thickness_error_m"][k])
                            records.append({**base, "quantity": "thickness", "item": CONTACTS[k] + "_" + CONTACTS[k + 1], **s,
                                            "idw2_abs_error": err})
                        if readings.zero_intervals[i, k]:
                            p0 = float(np.mean(pred["thickness"][:, j, k] <= 0.0))
                            records.append({**base, "quantity": "absence", "item": CONTACTS[k] + "_" + CONTACTS[k + 1],
                                            "p_zero": p0, "brier": (p0 - 1.0) ** 2})
                    for chk in readings.checks:
                        if chk["row"] == i:
                            dd = pred["depth"][:, j, chk["contact"]] + e_i
                            records.append({**base, "quantity": "not_reached_check", "item": chk["contact_name"],
                                            "bound_m": chk["deeper_than_m"], "p_satisfied": float(np.mean(dd >= chk["deeper_than_m"] - TOL_M))})
        print("fold", kind, label, "%.0fs" % (time.perf_counter() - t0), file=sys.stderr, flush=True)
    return records, fold_meta, time.perf_counter() - t0


def summarize_cv(records):
    out = {}
    keys = sorted({(r["fold"], r["position"], r["quantity"], r["variant"], r["config"]) for r in records
                   if r["quantity"] in ("depth", "thickness")})
    for fold, pos, qty, var, cfg in keys:
        rows = [r for r in records if (r["fold"], r["position"], r["quantity"], r["variant"], r["config"]) == (fold, pos, qty, var, cfg)]
        items = sorted({r["item"] for r in rows}, key=lambda x: (CONTACTS + [a + "_" + b for a, b in zip(CONTACTS, CONTACTS[1:])]).index(x))
        def block(rs):
            n = len(rs)
            k80, k90 = sum(r["in80"] for r in rs), sum(r["in90"] for r in rs)
            idw = [r["idw2_abs_error"] for r in rs if r.get("idw2_abs_error") is not None]
            gp_same = [r["abs_error_p50"] for r in rs if r.get("idw2_abs_error") is not None]
            return {"n_readings": n, "n_wells": len({r["well"] for r in rs}),
                    "coverage80": k80 / n if n else None, "coverage80_wilson95": ge.wilson_interval(k80, n),
                    "verdict80": ge.coverage_verdict(k80, n, 0.8),
                    "coverage90": k90 / n if n else None, "coverage90_wilson95": ge.wilson_interval(k90, n),
                    "verdict90": ge.coverage_verdict(k90, n, 0.9),
                    "crps_mean_m": float(np.mean([r["crps"] for r in rs])) if n else None,
                    "mae_p50_m": float(np.mean([r["abs_error_p50"] for r in rs])) if n else None,
                    "mean_width80_m": float(np.mean([r["p90"] - r["p10"] for r in rs])) if n else None,
                    "same_readings_mae_p50_m": float(np.mean(gp_same)) if gp_same else None,
                    "same_readings_mae_idw2_m": float(np.mean(idw)) if idw else None, "n_same_readings": len(idw)}
        out["/".join((fold, pos, qty, var, cfg))] = {"pooled": block(rows), "by_item": {it: block([r for r in rows if r["item"] == it]) for it in items}}
    extra = {"absence": [r for r in records if r["quantity"] == "absence"],
             "not_reached_checks": [r for r in records if r["quantity"] == "not_reached_check" and r["config"] == "SELECTED"]}
    return out, extra


def loo_residual_correlation(records):
    """Correlation of standardized LOO thickness residuals between intervals (diagnostic of independence)."""
    rows = [r for r in records if r["fold"] == "leave_one_physical_borehole_out" and r["variant"] == "G" and r["config"] == "SELECTED"
            and r["quantity"] in ("depth", "thickness")]
    keys = ["TKT"] + [a + "_" + b for a, b in zip(CONTACTS, CONTACTS[1:])]
    table = {}
    for r in rows:
        if r["item"] in keys:
            scale = max(r["p90"] - r["p10"], 1e-6) / 2.5631
            table.setdefault(r["well"], {})[r["item"]] = (r["observed"] - r["p50"]) / scale
    out = {}
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            pairs = [(v[keys[a]], v[keys[b]]) for v in table.values() if keys[a] in v and keys[b] in v]
            if len(pairs) >= 5:
                x = np.array(pairs)
                out[keys[a] + "~" + keys[b]] = {"n": len(pairs), "pearson": float(np.corrcoef(x.T)[0, 1])}
    return out


# --------------------------------------------------------------------------- grid ensemble

def trace_points(trace_xy, step_m):
    pts, ch = [], []
    total = 0.0
    for a, b in zip(trace_xy[:-1], trace_xy[1:]):
        a, b = np.asarray(a, float), np.asarray(b, float)
        L = float(np.linalg.norm(b - a))
        n = max(int(np.ceil(L / step_m)), 1)
        for t in np.linspace(0, 1, n, endpoint=False):
            pts.append(a + t * (b - a)); ch.append(total + t * L)
        total += L
    pts.append(np.asarray(trace_xy[-1], float)); ch.append(total)
    return np.array(pts), np.array(ch)


def run_grid(args, data, grid, samplers, selection, out):
    import torch

    readings, ids = data["readings"], data["ids"]
    f = data["fields"]
    nodes, tri, z62, mask = f["xy"], f["triangles"], f["contacts"], f["support_mask"]
    N, K = len(nodes), readings.n_fields
    dev = torch.device(args.device)
    if dev.type != "cuda":
        raise ValueError("the grid stage needs a CUDA device")
    if dev.index is None:
        dev = torch.device("cuda", torch.cuda.current_device())
    torch.cuda.set_device(dev)  # every later default-device CUDA call targets the selected GPU
    torch.cuda.set_per_process_memory_fraction(
        min(1.0, args.gpu_memory_gb * 2**30 / torch.cuda.get_device_properties(dev).total_memory), device=dev)
    data_idx = ge.data_node_index(nodes, readings.xy)
    s_nodes = data["surface"].sample(nodes)
    if not np.isfinite(s_nodes).all():
        raise ValueError("DSM operator undefined at some grid nodes")
    s_check = float(np.max(np.abs(s_nodes[data_idx] - data["collar"])))
    sections = read(args.sections)["sections"]
    traces = {}
    for name in ("I-I", "II-II", "III-III"):
        pts, ch = trace_points(sections[name]["trace_xy"], args.section_step_m)
        traces[name] = (pts, ch)
    for name, dy in (("PL12", 0.0), ("PL12_north50", 50.0), ("PL12_south50", -50.0)):
        traces[name] = trace_points([(LINE12[0][0], LINE12[0][1] + dy), (LINE12[1][0], LINE12[1][1] + dy)], args.section_step_m)
    ops = {k: ge.tin_sampling_operator(nodes, tri, v[0]) for k, v in traces.items()}
    for k, (idx, w) in ops.items():
        if np.isnan(w).any():
            raise ValueError("trace %s leaves the v6_2 grid" % k)
    plan = {"G": args.realizations_g, "P90": args.realizations_p90, "P200": args.realizations_p200}
    seq = np.random.SeedSequence(MASTER_SEED, spawn_key=(2,))
    seeds = {v: s for v, s in zip(plan, seq.spawn(len(plan)))}
    receipts = {}
    probs = [0.05, 0.10, 0.50, 0.90, 0.95]
    for variant, R in plan.items():
        if R <= 0:
            continue
        child = seeds[variant].spawn(K + 1)
        rng_plan = np.random.default_rng(child[K])
        n_sup = len(ids)
        if variant == "G":
            ext_nodes, idx_all = nodes, np.broadcast_to(data_idx, (R, n_sup))
            shifts, positions = np.broadcast_to(data["base_shift"], (R, n_sup)).copy(), None
        else:
            sampler = samplers[variant]
            pool = sampler.candidates.reshape(-1, 2)
            ext_nodes = np.vstack([nodes, pool])
            choice = sampler.pool_choice(rng_plan, R)
            idx_all = N + np.arange(n_sup)[None, :] * sampler.candidates.shape[1] + choice
            positions = ext_nodes[idx_all]
            shifts = data["base_shift"][None, :] + sampler.vertical(rng_plan, R)
        torch.cuda.reset_peak_memory_stats(dev)
        start, stop = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        start.record()
        wall = time.perf_counter()
        latent = []
        chol_log, field_info = [], []
        for fi in range(K):
            rng = np.random.default_rng(child[fi])
            rows, value, censored = readings.field_readings(fi)
            y = np.broadcast_to(value, (R, len(rows))).copy()
            floored = 0
            if fi == 0:
                y, floored = ge.top_latent(readings.depth[rows, 0][None, :], shifts[:, rows])
            xy = readings.xy[rows] if positions is None else positions[:, rows]
            _, params, y_used = ge.draw_field(rng, xy, selection[fi], grid, y, censored, sweeps=30)
            torch_seed = int(child[fi].generate_state(1)[0])
            backend = ge.make_backend("torch", seed=torch_seed, device=args.device, dtype="float32")
            log = []
            # only supports with a reading of this field condition it
            field = ge.simulate_on_nodes(backend, ext_nodes, idx_all[:, rows], selection[fi], grid, params, y_used, batch=args.batch, log=log)
            latent.append(field.cpu())  # offload: GPU holds one field at a time (memory cap)
            del field
            torch.cuda.empty_cache()
            chol_log.extend(dict(e, field=FIELDS[fi]) for e in log)
            field_info.append({"field": FIELDS[fi], "type": selection[fi], "readings": int(len(rows)), "censored": int(censored.sum()),
                               "top_floor_hits": floored, "torch_seed": torch_seed,
                               "range_m_draws": np.quantile(np.asarray(grid.ranges_m)[params["a_idx"]], [0.05, 0.5, 0.95]),
                               "nugget_draws": np.quantile(np.asarray(grid.nugget_ratios)[params["v_idx"]], [0.05, 0.5, 0.95]),
                               "sigma_draws": np.quantile(np.sqrt(params["sigma2"]), [0.05, 0.5, 0.95]),
                               "mean_draws": np.quantile(params["mean"], [0.05, 0.5, 0.95])})
            print(variant, FIELDS[fi], "done %.1fs" % (time.perf_counter() - wall), file=sys.stderr, flush=True)
        # contacts: depth below the DSM reference, then levels
        S = torch.as_tensor(s_nodes, dtype=torch.float32, device=args.device)
        depth = torch.exp(latent[0].to(args.device))
        sub = args.subset_check if 0 < args.subset_check < R else 0
        pzero_sub = []
        result = {}
        thick_q, pzero = [], []
        honour = 0.0
        ordered_min = np.inf
        sec = {k: {} for k in traces}
        stats = {}
        sample_keep = min(args.keep_realizations, R)
        kept = np.empty((sample_keep, K, N), np.float32)
        support_depth = np.empty((R, len(ids), K), np.float32)
        grid_kr2 = {}
        for k in range(K):
            if k > 0:
                h = torch.clamp(latent[k].to(args.device), min=0.0)
                ordered_min = min(ordered_min, float(h.min()))
                pzero.append(backend.to_numpy((h[:, :N] <= 0).float().mean(dim=0)))
                if sub:
                    pzero_sub.append(backend.to_numpy((h[:sub, :N] <= 0).float().mean(dim=0)))
                thick_q.append(ge.sorted_quantiles(backend, h[:, :N], [0.1, 0.5, 0.9]))
                if CONTACTS[k] == "PDKS":
                    # H_SZ_PDKS is the exact parent interval used for these draws.
                    # All grid nodes/all draws are checked, not marginal bands
                    # or the first saved subset. Nothing is repaired or selected.
                    unknown = (~torch.isfinite(h[:, :N]) | ~torch.isfinite(depth[:, :N])
                               | ~torch.isfinite(S)[None, :])
                    unknown_counts = backend.to_numpy(unknown.sum(dim=1)).astype(int)
                    for offset in KR2_OFFSETS_M:
                        bad = h[:, :N] < offset + KR2_THICKNESS_M - TOL_M
                        counts = backend.to_numpy(bad.sum(dim=1)).astype(int)
                        valid_worlds = (counts == 0) & (unknown_counts == 0)
                        grid_kr2["offset_%g" % offset] = {
                            "invalid_locations_per_world": counts,
                            "unknown_locations_per_world": unknown_counts,
                            "finite_worlds": unknown_counts == 0, "valid_worlds": valid_worlds,
                            "unknown_world_ids": ["%s:R%06d" % (variant, i) for i in np.flatnonzero(unknown_counts)],
                            "invalid_world_ids": ["%s:R%06d" % (variant, i) for i in np.flatnonzero(~valid_worlds)],
                            "valid_world_ids": ["%s:R%06d" % (variant, i) for i in np.flatnonzero(valid_worlds)],
                            "required_parent_thickness_m": offset + KR2_THICKNESS_M,
                            "violation": "floor_below_parent", "tolerance_m": TOL_M}
                depth = depth + h
                del h
            support_depth[:, :, k] = backend.to_numpy(backend.take_rows(depth, idx_all))
            level = S[None, :] - depth[:, :N]
            q = ge.sorted_quantiles(backend, level, probs)
            stats[CONTACTS[k]] = {"mean": backend.to_numpy(level.mean(dim=0)), "sd": backend.to_numpy(level.std(dim=0)), "q": q}
            if sub:
                stats[CONTACTS[k]]["q_sub"] = ge.sorted_quantiles(backend, level[:sub], probs)
            kept[:, k, :] = backend.to_numpy(level[:sample_keep])
            for name, (idx, w) in ops.items():
                it, wt = backend.index(idx), torch.as_tensor(w, dtype=torch.float32, device=args.device)
                vals = (level[:, it] * wt[None]).sum(dim=2)
                sec[name][CONTACTS[k]] = ge.sorted_quantiles(backend, vals, probs)
                if name.startswith("PL12") and CONTACTS[k] in ("SZ", "PDKS"):
                    sec[name].setdefault("_raw", {})[CONTACTS[k]] = backend.to_numpy(vals)
                del vals
            del level
        stop.record(); torch.cuda.synchronize(dev)
        gpu_ms = start.elapsed_time(stop)
        peak = torch.cuda.max_memory_allocated(dev) / 2**30
        # honouring of point readings (depth below the true collar = shift + reference depth)
        point = np.isfinite(readings.depth) & ~readings.filled
        true_depth = support_depth + shifts[:, :, None].astype(np.float32)
        honour = float(np.max(np.abs(true_depth - readings.depth[None].astype(np.float32))[:, point]))
        checks = []
        for chk in readings.checks:
            d = true_depth[:, chk["row"], chk["contact"]]
            checks.append({"physical_id": chk["physical_id"], "contact": chk["contact_name"], "not_reached_until_m": chk["deeper_than_m"],
                           "violation_fraction": float(np.mean(d < chk["deeper_than_m"] - TOL_M)),
                           "equality_fraction": float(np.mean(np.abs(d - chk["deeper_than_m"]) <= TOL_M)),
                           "model_depth_p10_p50_p90_m": np.quantile(d, [0.1, 0.5, 0.9])})
        # KrII along profile line 12: scenario ends kept separate
        kr2 = {}
        for name in ("PL12", "PL12_north50", "PL12_south50"):
            zsz, zpd = sec[name]["_raw"]["SZ"], sec[name]["_raw"]["PDKS"]
            entry = {}
            for u in KR2_OFFSETS_M:
                roof = zsz - u
                admissibility = ge.seam_envelope_admissibility(roof, roof - KR2_THICKNESS_M, zsz, zpd, TOL_M)
                entry["offset_%g" % u] = {"roof_q": np.quantile(roof, probs, axis=0),
                                          "conflict_fraction": np.mean(roof - KR2_THICKNESS_M < zpd, axis=0),
                                          "envelope_worlds_checked": int(len(zsz)),
                                          "envelope_worlds_invalid": int((~admissibility["valid_worlds"]).sum()),
                                          "envelope_tolerance_m": TOL_M,
                                          "mechanical_admission": "NOT_ADMITTED; section envelope check alone is insufficient"}
            kr2[name] = entry
            del sec[name]["_raw"]
        # comparison with v6_2 surfaces
        comparison = {}
        for k, name in enumerate(CONTACTS):
            q = stats[name]["q"]
            for m in ("INTERPOLATION", "EXTRAPOLATION", "DIRECT_CONSTRAINT"):
                sel = mask == m
                z = z62[sel, k]
                comparison.setdefault(name, {})[m] = {
                    "nodes": int(sel.sum()), "tolerance_m": TOL_M,
                    "inside_p10_p90": float(np.mean((z >= q[1, sel] - TOL_M) & (z <= q[3, sel] + TOL_M))),
                    "inside_p05_p95": float(np.mean((z >= q[0, sel] - TOL_M) & (z <= q[4, sel] + TOL_M))),
                    "median_v62_minus_p50_m": float(np.median(z - q[2, sel])),
                    "median_width80_m": float(np.median(q[3, sel] - q[1, sel]))}
        np.savez_compressed(out / ("ensemble_%s.npz" % variant), xy=nodes, triangles=tri, geometry_version=np.array(str(f["geometry_version"])),
                            contacts=np.array(CONTACTS), probs=np.array(probs), support_mask=mask, dsm_reference=s_nodes.astype(np.float32),
                            v62_contacts=z62.astype(np.float32),
                            **{"%s_%s" % (c, s): stats[c][s].astype(np.float32) for c in CONTACTS for s in ("mean", "sd")},
                            **{"%s_q" % c: stats[c]["q"].astype(np.float32) for c in CONTACTS},
                            p_zero=np.array(pzero, np.float32), thickness_q10_q50_q90=np.array(thick_q, np.float32),
                            realizations=R, subset_size=sub,
                            **({"%s_q_subset" % c: stats[c]["q_sub"].astype(np.float32) for c in CONTACTS} if sub else {}),
                            **({"p_zero_subset": np.array(pzero_sub, np.float32)} if sub else {}))
        np.savez_compressed(out / ("realizations_subset_%s.npz" % variant), levels=kept, contacts=np.array(CONTACTS),
                            xy=nodes, triangles=tri, geometry_version=np.array(str(f["geometry_version"])),
                            realization_ids=np.array(["%s:R%06d" % (variant, i) for i in range(sample_keep)]),
                            sampling_path=np.array("simulate_on_nodes: joint prior + residual conditioning"),
                            mechanical_admission=np.array("NOT_ADMITTED; productive seams/material state require separate validation"),
                            note=np.array("first %d synthetic realizations of %d; SYNTHETIC, not observations" % (sample_keep, R)))
        save(out / ("productive_seam_pre_admission_%s.json" % variant), {
            "schema": "vkm.productive_seam_pre_admission/1", "parent": ["SZ", "PDKS"],
            "worlds_checked": R, "locations_per_world": N, "scenarios": grid_kr2,
            "recipe_status": KR2_RECIPE_STATUS, "selection_policy": "all original draws retained",
            "checked_scope": "all grid nodes; containment extends through each triangle only when parent/seam "
                             "surfaces share the same P1 interpolant and connectivity",
            "unknown_policy": "nonfinite parent thickness/depth or DSM refuses the entire world; no implicit validity",
            "mechanical_admission": "NOT_ADMITTED; interpretation/material partition/history remain prerequisites"})
        save(out / ("sections_%s.json" % variant), {
            "variant": variant, "realizations": R, "probs": probs, "geometry_version": str(f["geometry_version"]),
            "status": "SYNTHETIC ENSEMBLE BANDS (DERIVATION); not observations",
            "sections": {name: {"chainage_m": traces[name][1], "xy": traces[name][0], "dsm_m": data["surface"].sample(traces[name][0]),
                                "v62": {c: (z62[ops[name][0], CONTACTS.index(c)] * ops[name][1]).sum(axis=1) for c in CONTACTS},
                                "bands": sec[name]} for name in traces},
            "kr2_line12": kr2, "kr2_rule": "roof = SZ roof - offset, offset scenario ends 7.5 and 16 m (L04, A_corpus seam_scenarios); thickness 5.97 m; conflict = floor below PDKS roof"})
        receipts[variant] = {"realizations": R, "gpu_ms": gpu_ms, "wall_s": time.perf_counter() - wall, "peak_gpu_memory_gib": peak,
                             "seed_spawn_key": list(seeds[variant].spawn_key), "fields": field_info, "cholesky": chol_log,
                             "honour_max_abs_m": honour, "base_shift_m": data["base_shift"], "thickness_min_m": ordered_min, "dsm_operator_at_supports_max_diff_m": s_check,
                             "not_reached_checks": checks, "v62_comparison": comparison,
                             "collar_rule": "none (collar = DSM operator)" if variant == "G" else samplers[variant].describe()}
        del latent, depth, S
        torch.cuda.empty_cache()
        print(variant, "grid done gpu %.1fs peak %.2f GiB" % (gpu_ms / 1000, peak), file=sys.stderr, flush=True)
    return receipts


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--model-dir", type=Path, required=True, help="extracted engineering_v6_2 directory")
    p.add_argument("--observations", type=Path, required=True)
    p.add_argument("--previous-scene", type=Path, required=True)
    p.add_argument("--sections", type=Path, required=True, help="graphics/data/sections_xyz.json")
    p.add_argument("--v62-cv", type=Path, required=True, help="FULL_CONTEXT_PARTIAL/cross_validation.json of v6_2")
    p.add_argument("--prereg", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--stage", choices=["cv", "grid", "all"], default="all")
    p.add_argument("--geometry-version", default="887a7f3361cb528adf931cd62934c008e98dcf80d4ceeb2f853047e967ec9dc7")
    p.add_argument("--cv-draws", type=int, default=4000)
    p.add_argument("--max-folds", type=int, default=0, help="smoke runs only")
    p.add_argument("--realizations-g", type=int, default=5000)
    p.add_argument("--realizations-p90", type=int, default=5000)
    p.add_argument("--realizations-p200", type=int, default=2000)
    p.add_argument("--keep-realizations", type=int, default=50)
    p.add_argument("--subset-check", type=int, default=5000, help="also store quantiles of the first N realizations (Monte Carlo convergence)")
    p.add_argument("--section-step-m", type=float, default=10.0)
    p.add_argument("--batch", type=int, default=1000)
    p.add_argument("--device", default="cuda")
    p.add_argument("--gpu-memory-gb", type=float, default=10.0)
    args = p.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to((REPO / "work").resolve()):
        raise ValueError("real outputs belong in ignored work/")
    out.mkdir(parents=True, exist_ok=True)
    inputs = {"model_fields": args.model_dir / "FULL_CONTEXT_PARTIAL" / "contact_fields.npz",
              "model_qa": args.model_dir / "FULL_CONTEXT_PARTIAL" / "qa.json", "mapped_boreholes": args.model_dir / "mapped_boreholes.json",
              "observations": args.observations, "previous_scene": args.previous_scene, "sections": args.sections,
              "v62_cv": args.v62_cv, "prereg": args.prereg, "script": Path(__file__),
              "module": REPO / "src/vkm_world/geometry_build/gp_ensemble.py",
              "tin_surface": REPO / "src/vkm_world/geometry_build/column_display.py"}
    pins = {k: sha(v) for k, v in inputs.items()}
    grid = ge.ParameterGrid()
    data = load_inputs(args)
    seq = np.random.SeedSequence(MASTER_SEED, spawn_key=(0,))
    s90, s200 = seq.spawn(2)
    samplers = make_samplers(data, {"P90": s90, "P200": s200})
    receipt_path = out / ("receipt_%s.json" % args.stage)
    receipt = {"schema": "vkm.receipt/1", "command": [Path(a).name if i == 0 else a for i, a in enumerate(sys.argv)],
               "stage": args.stage, "inputs_sha256": pins, "grid": grid.describe(), "master_seed": MASTER_SEED,
               "generation_settings": generation_settings(args),
               "versions": {"python": platform.python_version(), "numpy": np.__version__}, "status": "SYNTHETIC ENSEMBLE; not observations; not field validation"}
    import scipy
    receipt["versions"]["scipy"] = scipy.__version__
    if args.stage in ("cv", "all"):
        records, meta, seconds = run_cv(args, data, grid, samplers)
        summary, extra = summarize_cv(records)
        save(out / "cv_records.json", {"records": records})
        save(out / "cv_summary.json", {"summary": summary, "absence": extra["absence"], "not_reached_checks": extra["not_reached_checks"],
                                       "fold_selection": meta, "loo_residual_correlation_G": loo_residual_correlation(records),
                                       "draws_per_fold": args.cv_draws, "status": "consistency check of the uncertainty model; not field validation"})
        receipt["cv_seconds_cpu"] = seconds
    if args.stage in ("grid", "all"):
        import torch
        selection, evidence = selected_kinds(data["readings"], np.arange(len(data["ids"])), grid)
        save(out / "selection_full_data.json", {"rule": "PREREGISTRATION.json type_selection_rule", "selected": dict(zip(FIELDS, selection)),
                                                "log_evidence": dict(zip(FIELDS, evidence)),
                                                "fixed_before_grid_under_prereg_sha256": pins["prereg"]})
        receipt["versions"].update({"torch": torch.__version__, "cuda": torch.version.cuda, "gpu": torch.cuda.get_device_name(torch.device(args.device)),
                                     "device": args.device})
        receipt["grid"] = run_grid(args, data, grid, samplers, selection, out)
        receipt["collar_samplers"] = {k: v.describe() for k, v in samplers.items()}
    # Resolve versions after backend discovery. This identity pins configuration,
    # not byte equality across hardware/library implementations. Output hashes
    # identify the actual generated arrays; row IDs are meaningful with both.
    receipt["generation_identity"] = {
        "stage": args.stage, "inputs_sha256": pins, "parameter_grid": grid.describe(),
        "settings": receipt["generation_settings"], "versions": dict(receipt["versions"])}
    receipt["generation_identity_sha256"] = hashlib.sha256(
        json.dumps(receipt["generation_identity"], sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    receipt["outputs_sha256"] = {pth.name: sha(pth) for pth in sorted(out.iterdir()) if pth.is_file() and not pth.name.startswith("receipt")}
    save(receipt_path, receipt)
    if pins != {k: sha(v) for k, v in inputs.items()}:
        raise RuntimeError("an input changed during the run")


if __name__ == "__main__":
    main()
