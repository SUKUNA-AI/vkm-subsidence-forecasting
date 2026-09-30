"""DPF result extraction (PyDPF-Core 0.16, in-process server in the Entry context: no licence checkout).

Runs inside a job process (``python -m vkm_ansys.dpf_job <job_dir>``, spec ``in/dpf_spec.json``) or as the post-step
of a MAPDL job; the MCP server itself never loads DPF. Kinds:

* ``extract`` — nodal or elemental values of U/UX/UY/UZ, S, EPEL, EPPL, EPCR (and their equivalents) on a selection
  (all, named selection, box, top surface, node list) for chosen result sets → ``out/dpf_<quantity>.csv`` and
  ``out/dpf_summary.json``;
* ``subsidence`` — the model trough on the top surface: w(x, y, t) = -(uz(t) - uz(t_ref)), profiles along lines, the
  maximum in time (status DERIVATION of a MODEL_RESULT) → ``out/subsidence_*.csv`` and ``out/results.json``;
* ``s08`` — ladder step S08: DPF nodal displacements of an S02 and an S04 job against the decks' ``*VGET`` tables and
  the ``PRNSOL`` listing;
* ``operators`` — names of the DPF operators with their licence property.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from vkm_ansys.errors import ToolFailure

SPEC_NAME = "dpf_spec.json"
QUANTITIES: dict[str, tuple[str, int | None]] = {
    "U": ("displacement", None), "UX": ("displacement", 0), "UY": ("displacement", 1), "UZ": ("displacement", 2),
    "S": ("stress", None), "SEQV": ("stress_von_mises", None),
    "EPEL": ("elastic_strain", None), "EPPL": ("plastic_strain", None), "EPCR": ("creep_strain", None),
    "EPCR_EQV": ("creep_strain_eqv", None), "RF": ("reaction_force", None),
}
COMPONENTS = {3: ["x", "y", "z"], 6: ["xx", "yy", "zz", "xy", "yz", "xz"], 1: ["value"]}
MAX_ROWS = 5_000_000


def start_server(env: Mapping[str, str] | None = None):
    """In-process DPF server of the local Ansys installation in the Entry context (licence checkouts refused)."""
    from ansys.dpf import core as dpf

    from vkm_ansys.detect import find_root
    root, _ver, how = find_root(os.environ if env is None else env)
    if root is None:
        raise ToolFailure("APP_UNAVAILABLE", f"Ansys installation not found: {how}")
    server = dpf.start_local_server(ansys_path=str(root), config=dpf.AvailableServerConfigs.InProcessServer,
                                    context=dpf.AvailableServerContexts.entry, as_global=True)
    ctx = str(getattr(server.context, "licensing_context_type", server.context)).split(".")[-1].lower()
    return dpf, server, {"server": type(server).__name__, "version": str(server.version), "context": ctx}


# ------------------------------------------------------------------------------------------------ selection
def _node_coords(mesh) -> tuple[list[int], Any]:
    import numpy as np
    ids = list(mesh.nodes.scoping.ids)
    xyz = np.asarray(mesh.nodes.coordinates_field.data).reshape(-1, 3)
    return ids, xyz


def select_nodes(model, selection: Mapping[str, Any] | None, tol: float = 1e-6) -> tuple[list[int] | None, str]:
    """Node ids of a selection (``None`` = all nodes) and a description."""
    import numpy as np
    selection = dict(selection or {})
    if not selection or selection.get("all"):
        return None, "all nodes"
    mesh = model.metadata.meshed_region
    ids, xyz = _node_coords(mesh)
    if "named_selection" in selection:
        name = str(selection["named_selection"])
        available = list(model.metadata.available_named_selections)
        match = next((n for n in available if n.upper() == name.upper()), None)
        if match is None:
            raise ToolFailure("NOT_FOUND", f"named selection {name!r} is not in the result file",
                              details={"available": available[:100]})
        return list(model.metadata.named_selection(match).ids), f"named selection {match}"
    if "surface" in selection:
        if selection["surface"] != "top":
            raise ToolFailure("INVALID_ARGUMENT", "surface: only 'top' (nodes at the maximum z) is supported")
        zmax = float(xyz[:, 2].max())
        span = float(xyz[:, 2].max() - xyz[:, 2].min()) or 1.0
        mask = xyz[:, 2] >= zmax - tol * span
        return [i for i, m in zip(ids, mask) if m], f"top surface z = {zmax:g}"
    if "box" in selection:
        b = [float(v) for v in selection["box"]]
        if len(b) != 6:
            raise ToolFailure("INVALID_ARGUMENT", "box = [x0, x1, y0, y1, z0, z1]")
        mask = ((xyz[:, 0] >= b[0]) & (xyz[:, 0] <= b[1]) & (xyz[:, 1] >= b[2]) & (xyz[:, 1] <= b[3])
                & (xyz[:, 2] >= b[4]) & (xyz[:, 2] <= b[5]))
        return [i for i, m in zip(ids, np.asarray(mask)) if m], f"box {b}"
    if "nodes" in selection:
        wanted = {int(n) for n in selection["nodes"]}
        return [i for i in ids if i in wanted], f"{len(wanted)} listed nodes"
    raise ToolFailure("INVALID_ARGUMENT", "selection: all | named_selection | surface: top | box | nodes")


def select_sets(model, times: Any) -> list[int]:
    """Result set ids (1-based) for ``"all"``, ``"last"`` or a list of times (nearest set within 1e-9 relative)."""
    tf = model.metadata.time_freq_support
    values = [float(v) for v in tf.time_frequencies.data]
    n = len(values)
    if times in (None, "all"):
        return list(range(1, n + 1))
    if times == "last":
        return [n]
    if isinstance(times, list) and times:
        out = []
        for t in times:
            k = min(range(n), key=lambda i: abs(values[i] - float(t)))
            if abs(values[k] - float(t)) > 1e-9 * max(1.0, abs(float(t))):
                raise ToolFailure("NOT_FOUND", f"no result set at time {t}", details={"times": values[:200]})
            out.append(k + 1)
        return out
    raise ToolFailure("INVALID_ARGUMENT", "times: 'all', 'last' or a list of times")


# ------------------------------------------------------------------------------------------------ extraction
def _result_fields(dpf, model, quantity: str, location: str, set_ids: list[int], node_ids: list[int] | None):
    name, comp = QUANTITIES[quantity]
    op_cls = getattr(dpf.operators.result, name, None)
    if op_cls is None:
        raise ToolFailure("NOT_FOUND", f"DPF has no result operator {name}")
    op = op_cls(data_sources=model.metadata.data_sources)
    op.inputs.time_scoping.connect(set_ids)
    if node_ids is not None and location == "nodal":
        op.inputs.mesh_scoping.connect(dpf.Scoping(ids=node_ids, location=dpf.locations.nodal))
    if location == "nodal" and name not in ("displacement", "reaction_force"):
        op.inputs.requested_location.connect(dpf.locations.nodal)
    elif location == "elemental":
        op.inputs.requested_location.connect(dpf.locations.elemental)
    fc = op.eval()
    return fc, comp


def extract(model, dpf, quantity: str, location: str, selection: Mapping[str, Any] | None, times: Any,
            out_csv: Path) -> dict[str, Any]:
    import numpy as np
    quantity = quantity.upper()
    if quantity not in QUANTITIES:
        raise ToolFailure("INVALID_ARGUMENT", f"quantity: one of {sorted(QUANTITIES)}")
    if location not in ("nodal", "elemental"):
        raise ToolFailure("INVALID_ARGUMENT", "location: nodal | elemental")
    node_ids, sel_text = select_nodes(model, selection)
    set_ids = select_sets(model, times)
    tvals = [float(v) for v in model.metadata.time_freq_support.time_frequencies.data]
    fc, comp = _result_fields(dpf, model, quantity, location, set_ids, node_ids)
    mesh = model.metadata.meshed_region
    ids_all, xyz = _node_coords(mesh)
    pos = {nid: k for k, nid in enumerate(ids_all)}
    rows = 0
    per_set = []
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        header_done = False
        for k, sid in enumerate(set_ids):
            field = fc[k]
            data = np.asarray(field.data)
            data = data.reshape(len(field.scoping.ids), -1) if data.size else data.reshape(0, 1)
            if comp is not None:
                data = data[:, comp:comp + 1]
            names = COMPONENTS.get(data.shape[1], [f"c{i}" for i in range(data.shape[1])])
            if comp is not None:
                names = [["x", "y", "z"][comp]]
            if not header_done:
                ent = "node_id" if location == "nodal" else "element_id"
                coords = ["x", "y", "z"] if location == "nodal" else []
                w.writerow(["set", "time", ent, *coords, *[f"{quantity.lower()}_{n}" for n in names]])
                header_done = True
            ent_ids = list(field.scoping.ids)
            if node_ids is not None and location == "elemental":
                pass                                          # elemental: selection applies to nodes only
            order = sorted(range(len(ent_ids)), key=lambda i: ent_ids[i])
            for i in order:
                eid = ent_ids[i]
                coord = list(xyz[pos[eid]]) if location == "nodal" and eid in pos else []
                w.writerow([sid, repr(tvals[sid - 1]), eid, *[repr(float(c)) for c in coord],
                            *[repr(float(v)) for v in data[i]]])
                rows += 1
                if rows > MAX_ROWS:
                    raise ToolFailure("PAYLOAD_TOO_LARGE", f"more than {MAX_ROWS} rows; narrow the selection")
            col = data[:, 0] if data.size else np.zeros(0)
            per_set.append({"set": sid, "time": tvals[sid - 1], "entities": len(ent_ids),
                            "min": float(col.min()) if col.size else None,
                            "max": float(col.max()) if col.size else None,
                            "abs_max": float(abs(col).max()) if col.size else None})
    return {"quantity": quantity, "location": location, "selection": sel_text, "sets": per_set, "rows": rows,
            "file": f"out/{out_csv.name}", "unit_system": str(model.metadata.result_info.unit_system_name)}


def subsidence(model, dpf, reference: str | float, times: Any, profiles: list[list[float]],
               out: Path) -> dict[str, Any]:
    """Top-surface trough w(x, y, t) = -(uz(t) - uz(t_ref)) (positive = downward), profiles and maxima."""
    import numpy as np
    node_ids, sel_text = select_nodes(model, {"surface": "top"})
    set_ids = select_sets(model, times)
    tvals = [float(v) for v in model.metadata.time_freq_support.time_frequencies.data]
    if reference == "first":
        ref_set = 1
    elif reference == "none":
        ref_set = None
    else:
        ref_set = select_sets(model, [float(reference)])[0]
    need = sorted(set(set_ids) | ({ref_set} if ref_set else set()))
    fc, _ = _result_fields(dpf, model, "UZ", "nodal", need, node_ids)
    by_set = {}
    mesh = model.metadata.meshed_region
    ids_all, xyz = _node_coords(mesh)
    pos = {nid: k for k, nid in enumerate(ids_all)}
    for k, sid in enumerate(need):
        f = fc[k]
        uz = np.asarray(f.data).reshape(len(f.scoping.ids), -1)[:, 2]
        by_set[sid] = dict(zip(list(f.scoping.ids), uz))
    nodes = sorted(node_ids)
    xy = np.array([[xyz[pos[n]][0], xyz[pos[n]][1]] for n in nodes])
    ref = np.array([by_set[ref_set][n] for n in nodes]) if ref_set else np.zeros(len(nodes))
    series = []
    with open(out / "subsidence_field.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["set", "time", "node_id", "x", "y", "w_m"])
        for sid in set_ids:
            w = -(np.array([by_set[sid][n] for n in nodes]) - ref)
            for n, (x, y), wv in zip(nodes, xy, w):
                wr.writerow([sid, repr(tvals[sid - 1]), n, repr(float(x)), repr(float(y)), repr(float(wv))])
            k = int(np.argmax(w))
            series.append({"set": sid, "time": tvals[sid - 1], "w_max_m": float(w[k]),
                           "x_at_max": float(xy[k, 0]), "y_at_max": float(xy[k, 1]), "w_min_m": float(w.min())})
    prof_out = []
    if profiles:
        from scipy.interpolate import griddata
        with open(out / "subsidence_profiles.csv", "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(["profile", "set", "time", "s_m", "x", "y", "w_m"])
            for pi, prof in enumerate(profiles):
                if len(prof) != 5 or int(prof[4]) < 2:
                    raise ToolFailure("INVALID_ARGUMENT", "profiles: [[x0, y0, x1, y1, n >= 2], ...]")
                x0, y0, x1, y1, n = float(prof[0]), float(prof[1]), float(prof[2]), float(prof[3]), int(prof[4])
                pts = np.array([[x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * i / (n - 1)] for i in range(n)])
                s = np.hypot(pts[:, 0] - x0, pts[:, 1] - y0)
                for sid in set_ids:
                    w = -(np.array([by_set[sid][n_] for n_ in nodes]) - ref)
                    wp = griddata(xy, w, pts, method="linear")
                    for si, (px, py), wv in zip(s, pts, wp):
                        wr.writerow([pi, sid, repr(tvals[sid - 1]), repr(float(si)), repr(float(px)),
                                     repr(float(py)), "" if not np.isfinite(wv) else repr(float(wv))])
                prof_out.append({"profile": pi, "from": [x0, y0], "to": [x1, y1], "points": n})
    growth = [b["w_max_m"] - a["w_max_m"] for a, b in zip(series, series[1:])]
    return {"selection": sel_text, "surface_nodes": len(nodes), "reference_set": ref_set,
            "reference_time": tvals[ref_set - 1] if ref_set else None, "series": series,
            "w_max_final_m": series[-1]["w_max_m"] if series else None,
            "monotonic_nondecreasing": all(g >= 0 for g in growth), "profiles": prof_out,
            "files": ["out/subsidence_field.csv"] + (["out/subsidence_profiles.csv"] if profiles else []),
            "status": "DERIVATION", "result_status": "MODEL_RESULT"}


def compare_with_deck(model, dpf, work: Path) -> dict[str, Any]:
    """S08: DPF nodal U (last set) against the deck's *VGET table and PRNSOL listing in ``work``."""
    import numpy as np

    from vkm_ansys.mapdl_out import parse_prnsol, parse_table
    fc = model.results.displacement.on_last_time_freq.eval()
    f = fc[0]
    dpf_u = {int(n): np.asarray(v, dtype=float) for n, v in zip(f.scoping.ids, np.asarray(f.data))}
    vget = {int(r["id"]): np.array([r["ux"], r["uy"], r["uz"]]) for r in parse_table(
        (work / "vkm_nodes_u.txt").read_text(encoding="utf-8", errors="replace"), ["id", "ux", "uy", "uz"])}
    prn = parse_prnsol((work / "vkm_prnsol_u.txt").read_text(encoding="utf-8", errors="replace"))
    common = sorted(set(dpf_u) & set(vget))
    scale = max((float(np.abs(v).max()) for v in vget.values()), default=0.0) or 1.0
    diff = max((float(np.abs(dpf_u[n] - vget[n]).max()) for n in common), default=math.inf)
    prn_common = sorted(set(dpf_u) & set(prn))
    prn_diff = max((abs(float(dpf_u[n][2]) - prn[n].get("uz", math.nan)) for n in prn_common), default=math.inf)
    return {"nodes_dpf": len(dpf_u), "nodes_vget": len(vget), "nodes_prnsol": len(prn),
            "same_node_set": set(dpf_u) == set(vget) == set(prn), "u_scale_m": scale,
            "u_max_abs_diff": diff, "u_max_rel_diff": diff / scale,
            "prnsol_uz_max_abs_diff": prn_diff, "prnsol_uz_max_rel_diff": prn_diff / scale}


def list_operators(dpf, name_filter: str | None, limit: int) -> dict[str, Any]:
    names = sorted(dpf.dpf_operator.available_operator_names())
    flt = (name_filter or "").lower()
    rows = []
    for n in names:
        if flt and flt not in n.lower():
            try:
                props = dpf.Operator.operator_specification(n).properties
            except Exception:  # noqa: BLE001
                continue
            if flt not in str(props.get("scripting_name", "")).lower() and flt not in str(
                    props.get("user_name", "")).lower():
                continue
        try:
            props = dpf.Operator.operator_specification(n).properties
        except Exception:  # noqa: BLE001
            props = {}
        rows.append({"name": n, "scripting_name": props.get("scripting_name"), "user_name": props.get("user_name"),
                     "category": props.get("category"), "plugin": props.get("plugin"),
                     "license": props.get("license") or "none"})
        if len(rows) >= limit:
            break
    return {"total_operators": len(names), "matched": len(rows), "operators": rows}


# ------------------------------------------------------------------------------------------------ job entry
def _resolve_rst(path_text: str, env: Mapping[str, str]) -> Path:
    from vkm_ansys.context import validate_sim_root
    root, reason = validate_sim_root(env)
    if root is None:
        raise ToolFailure("SIM_ROOT_UNAVAILABLE", f"simulation root unavailable: {reason}")
    p = Path(path_text)
    p = (root / p) if not p.is_absolute() else p
    p = p.resolve()
    if not p.is_relative_to(root):
        raise ToolFailure("PATH_OUTSIDE_ROOT", "result files are read only under VKM_SIM_ROOT")
    if not p.is_file():
        raise ToolFailure("RESULT_NOT_FOUND", f"result file not found: {p.name}")
    return p


def run_extract(job_dir: Path, spec: Mapping[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run one DPF spec for ``job_dir`` (``out/`` receives the files); returns the summary (also out/dpf_summary.json)."""
    env = dict(os.environ if env is None else env)
    job_dir = Path(job_dir)
    out = job_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    dpf, _server, info = start_server(env)
    kind = spec.get("mode", "extract")
    summary: dict[str, Any] = {"schema": "vkm-ansys.dpf_summary/1", "mode": kind, "dpf": info,
                               "result_status": "MODEL_RESULT"}
    if kind == "operators":
        summary.update(list_operators(dpf, spec.get("filter"), int(spec.get("limit", 100))))
    elif kind == "s08":
        from vkm_ansys import ladder
        res: dict[str, Any] = {"dpf": info}
        for key in ("s02", "s04"):
            src = _resolve_rst(spec[f"{key}_rst"], env)
            model = dpf.Model(str(src))
            res[key] = compare_with_deck(model, dpf, src.parent)
            res[key]["job"] = spec.get(f"{key}_job")
        summary["post"] = ladder.write_results(job_dir, "S08", res)
        summary["s08"] = {k: v for k, v in res.items() if k != "dpf"}
    else:
        rst = Path(spec["result_file"]) if spec.get("result_file_abs") else _resolve_rst(spec["result_file"], env)
        model = dpf.Model(str(rst))
        if kind == "subsidence":
            res = subsidence(model, dpf, spec.get("reference", "first"), spec.get("times", "all"),
                             spec.get("profiles") or [], out)
            (out / "results.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            summary["subsidence"] = {k: v for k, v in res.items() if k != "series"} | {
                "series_head": res["series"][:5], "series_tail": res["series"][-5:]}
        else:
            summary["fields"] = []
            for q in spec.get("quantities", ["U"]):
                summary["fields"].append(extract(model, dpf, q, spec.get("location", "nodal"),
                                                 spec.get("selection"), spec.get("times", "all"),
                                                 out / f"dpf_{q.lower()}.csv"))
            summary["time_sets"] = len(model.metadata.time_freq_support.time_frequencies.data)
    summary["duration_s"] = round(time.monotonic() - t0, 3)
    (out / "dpf_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True,
                                                     default=str) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m vkm_ansys.dpf_job <job_dir>", file=sys.stderr)
        return 2
    job_dir = Path(args[0])
    spec = json.loads((job_dir / "in" / SPEC_NAME).read_text(encoding="utf-8"))
    try:
        run_extract(job_dir, spec)
    except ToolFailure as exc:
        (job_dir / "out").mkdir(parents=True, exist_ok=True)
        (job_dir / "out" / "dpf_error.json").write_text(json.dumps(exc.as_dict(), indent=1) + "\n",
                                                        encoding="utf-8")
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
