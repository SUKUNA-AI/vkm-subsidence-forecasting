#!/usr/bin/env python3
"""Build public-safe evidence tables from frozen legacy artifacts (read from git objects, no checkout).

Targets
-------
``monitoring``  Musikhin digitization, VKM-SRC-002 PDF p.15 (decision D-07) ->
                ``evidence/monitoring/musikhin_vkm_src002_p15_profiles.csv``,
                ``evidence/monitoring/musikhin_modality_comparison.csv``,
                ``evidence/monitoring/musikhin_vkm_src002_p15_manifest.json``.
                Inputs: the canonical digitization of scenario constraints v2 (atlas readings + the independent
                detailed re-read of line 1 levelling 2011-2016), the frozen manual digitization (printed labels) and
                the per-series point tables, all at the legacy commit, each verified by sha256.
``sources``     ``evidence/sources/legacy_source_id_map.csv``: legacy ids SRC01..SRC11/SUP01 of
                ``configs/{source_manifest,supplementary_source_manifest}.csv`` -> PRIVATE ``VKM-SRC`` ids, joined by
                sha256 with ``$VKM_RESOURCES_ROOT/00_registry/SOURCE_REGISTER.csv``. No PRIVATE paths are written.

The legacy files were removed from the working tree in the reset; they are read with ``git cat-file`` from the
pinned commit (branch ``legacy``, tag ``legacy/final``). Outputs are deterministic (fixed row order, LF, no
timestamps). ``--check`` rebuilds in memory and compares with the committed files (exit 1 on any difference).

Usage::

    python scripts/build_evidence_from_legacy.py monitoring [--check]
    VKM_RESOURCES_ROOT=<private checkout> python scripts/build_evidence_from_legacy.py sources [--check]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_COMMIT = "d54025d4c47b79b864076a33a4ecf877174ec922"   # branch legacy, tag legacy/final
REF_HELP = "run 'git fetch origin legacy:legacy' (and 'git fetch --unshallow' in a shallow clone)"

# ------------------------------------------------------------------------------------------------ monitoring
SOURCE_ID, LEGACY_SOURCE_ID = "VKM-SRC-002", "SUP01"
SOURCE_SHA256 = "4d20f03c0c3a7bb7039c405e2aff2780a30d9f6e52e99fe5da50fa4c9ccbe947"
LOCATOR = "PDF p.15"
SCOPE = "VKM_Solikamsk (SKRU-1 or SKRU-2 not proven)"
EVIDENCE_NOTE = "discovery-only; not a time series"
LINE_ORDER = ("1", "5", "17", "6")                    # order of the four charts on the page
PDF_XOBJECTS = {"1": "/Image157", "5": "/Image159", "17": "/Image161", "6": "/Image163"}
METHODS = {"leveling": "levelling", "radar": "insar"}  # legacy label -> Modality value of vkm_world
METHOD_ORDER = ("levelling", "insar")
OUT_DIR = "evidence/monitoring"
PROFILES_CSV = f"{OUT_DIR}/musikhin_vkm_src002_p15_profiles.csv"
COMPARISON_CSV = f"{OUT_DIR}/musikhin_modality_comparison.csv"
MANIFEST_JSON = f"{OUT_DIR}/musikhin_vkm_src002_p15_manifest.json"

A = "artifacts/reconstruction"
MON_INPUTS = {   # key: (legacy path, sha256, role)
    "canonical": (f"{A}/scenario_constraints_v2/canonical_digitization.csv",
                  "f33c22a8b6af4a8def6aeffc696cedc73434fb704fe174907a453c11f224abe0",
                  "accepted readings (values, reading status, reading bounds, raster coordinates)"),
    "comparison": (f"{A}/scenario_constraints_v2/modality_comparison.csv",
                   "a35539cb7c4e1f112d771ac5fae8faaacbee3ac2697dd9a7572f031cd87fde7e",
                   "legacy modality comparison, recomputed and compared"),
    "constraints_manifest": (f"{A}/scenario_constraints_v2/manifest.json",
                             "96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef",
                             "closure link: pins canonical_digitization.csv and modality_comparison.csv"),
    "digitization": ("data/published_figure_digitization_v1/musikhin_profiles_digitization.csv",
                     "2fc8ac5cd0a40eff9c230e641fe5a3c1a7e1522d41dd989d9abf7eda4af02761",
                     "frozen manual digitization (printed x labels, cross-check of values)"),
    "digitization_manifest": ("data/published_figure_digitization_v1/extraction_manifest.json",
                              "e8ce3f949feb747a55b99340a7491cb29e8f80546e8235e09e3dfc4727096278",
                              "provenance of the digitization and native rasters"),
    "line1_points": (f"{A}/musikhin_line1_2011_2016_v1/points.csv",
                     "257f643f300a58fce6699cd9938292ae63f0958857b2a996f58b1db13d8274cb",
                     "independent detailed re-read of line 1 levelling 2011-2016 (cross-check)"),
    "line1_manifest": (f"{A}/musikhin_line1_2011_2016_v1/manifest.json",
                       "5081dc8ce463119774cb4a6e3ffdd121a1a38e9b3bd0cee75e0382dbb6ce9f06", "provenance"),
    "profiles_manifest": (f"{A}/musikhin_profiles_2011_2016_v1/manifest.json",
                          "7740a92603762c9fb7db3c264db95c257bcf28d635aea54a2775676956e2bac4", "provenance"),
    "atlas_1": (f"{A}/musikhin_profiles_2011_2016_v1/profile_1/points.csv",
                "aa60e9e03de1ef5d4caa1a78962efb5287e2984cf17c3da5d0054fba1bb3d109", "atlas readings (cross-check)"),
    "atlas_5": (f"{A}/musikhin_profiles_2011_2016_v1/profile_5/points.csv",
                "63553b4642da045cbdf9c324574e96728bff5becae7c8f9005dbde9068c89f95", "atlas readings (cross-check)"),
    "atlas_17": (f"{A}/musikhin_profiles_2011_2016_v1/profile_17/points.csv",
                 "425aa933d91e523941b576464ffb82cbb5cba462b9d0d5462940a801a191eac4", "atlas readings (cross-check)"),
    "atlas_6": (f"{A}/musikhin_profiles_2011_2016_v1/profile_6/points.csv",
                "cffccc80e392dde55522139528602ee024b86cf40fa59b8c19f91bc49f74bf27", "atlas readings (cross-check)"),
}
# The frozen digitization normalised a few x labels (trailing zeros). Checked visually on the native rasters of
# VKM-SRC-002 p.15 (26.09.2026); the categorical label is written exactly as printed. key: (line, position).
PRINTED_LABEL_CORRECTIONS = {
    ("1", 10): ("1.0", "1"),
    **{("5", pos): (legacy, printed) for pos, legacy, printed in (
        (2, "0.10", "0.1"), (4, "0.20", "0.2"), (6, "0.30", "0.3"), (8, "0.40", "0.4"), (10, "0.50", "0.5"),
        (12, "0.60", "0.6"), (14, "0.70", "0.7"), (16, "0.80", "0.8"), (18, "0.90", "0.9"), (20, "1.00", "1"),
        (22, "1.10", "1.1"), (24, "1.20", "1.2"))},
}
LINE1_STATUS = {"clear_marker_center": "digitized_marker_or_visible_curve",
                "partially_occluded_approximation": "partially_occluded_approximation",
                "unresolved_occlusion": "unresolved_occlusion"}
READING_ORIGIN = {"accepted_atlas": "atlas_digitization",
                  "accepted_independent_detailed_reread": "independent_detailed_reread"}
PROFILE_COLUMNS = ("id", "source_id", "locator", "line", "method", "interval_start", "interval_end",
                   "x_label_as_printed", "value_mm", "digitization_status", "digitization_uncertainty_mm", "status",
                   "scope", "evidence_note", "position_index", "reading_origin", "raster_x_px", "raster_y_px",
                   "legacy_point_id")
COMPARISON_COLUMNS = ("id", "source_id", "locator", "line", "interval_start", "interval_end", "n_paired_positions",
                      "insar_minus_levelling_mean_mm", "insar_minus_levelling_median_mm", "rms_disagreement_mm",
                      "max_abs_disagreement_mm", "pearson_correlation", "fraction_within_reading_bounds", "status",
                      "scope", "evidence_note")
COMPARISON_NOTE = ("discovery-only; plotted modality disagreement at shared printed slots (no interpolation); "
                   "not a levelling error, not an InSAR accuracy estimate, not independent validation")
LIMITS = [
    "Manual digitization of four charts on slides (not peer reviewed); discovery-only until the primary "
    "publication of the profile lines is found.",
    "Mine not proven: the slides concern the Solikamsk area (SKRU-1 and/or SKRU-2).",
    "Each value is the displacement over one interval labelled by years (2011-2016 or 2015-2016); exact campaign "
    "dates and the common datum are unknown. Not a time series; intervals must not be subtracted from each other.",
    "The x axis is categorical (printed km labels, repeated or missing labels kept as printed); no georeference, "
    "no metric gradients, no mapping to benchmark or synthetic point ids.",
    "digitization_uncertainty_mm is a conservative chart-reading half-width, not a measurement sigma or a "
    "confidence interval.",
    "Levelling and InSAR are different modalities with different observation operators; neither is ground truth "
    "for the other.",
]


# ------------------------------------------------------------------------------------------------ sources
SRC_MANIFESTS = {"configs/source_manifest.csv": "8c9de321be4871e63f8300b2a38eaaea546e02cc24ad0ee1f0b20637e3eb5ceb",
                 "configs/supplementary_source_manifest.csv":
                     "9ed4885c5e4607fc6445ef4c01c35ab3a1ca70a81dc5301b9aee4a54f0a251ef"}
SRC_MAP_CSV = "evidence/sources/legacy_source_id_map.csv"
SRC_COLUMNS = ("legacy_source_id", "legacy_manifest", "legacy_file_name", "legacy_authority_class",
               "legacy_site_scope", "vkm_src_id", "sha256", "size_bytes", "register_source_class",
               "register_evidence_scope", "match_method", "note")
SRC_NOTES = {
    "SUP01": "source of evidence/monitoring/musikhin_* (PDF p.15); register: discovery-only",
    "SRC11": "legacy manifest itself records a corrected site attribution; use the register scope",
}


class BuildError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob(path: str, commit: str = LEGACY_COMMIT) -> bytes:
    done = subprocess.run(["git", "-C", str(ROOT), "cat-file", "blob", f"{commit}:{path}"], capture_output=True)
    if done.returncode != 0:
        raise BuildError(f"cannot read {commit[:7]}:{path} from git objects ({REF_HELP})")
    return done.stdout


def verified_inputs(spec: dict[str, tuple[str, str, str]]) -> dict[str, bytes]:
    out = {}
    for key, (path, want, _role) in spec.items():
        data = git_blob(path)
        if sha256(data) != want:
            raise BuildError(f"{path}: sha256 {sha256(data)} != pinned {want}")
        out[key] = data
    return out


def read_csv(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))


def csv_bytes(columns: tuple[str, ...], rows: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def fnum(text: str) -> float | None:
    return None if text is None or text.strip() == "" else float(text)


def fmt_tenth(value: float | None) -> str:
    """Values were read to 0.1 mm; refuse silent rounding of anything finer."""
    if value is None:
        return ""
    if abs(value * 10 - round(value * 10)) > 1e-6:
        raise BuildError(f"value {value!r} is not on the 0.1 mm reading grid")
    text = f"{round(value * 10) / 10:.1f}"
    return "0.0" if text == "-0.0" else text


def fmt_px(value: float | None) -> str:
    if value is None:
        return ""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def fmt6(value: float) -> str:
    text = f"{value:.6f}"
    return "0.000000" if text == "-0.000000" else text


def same(a: float | None, b: float | None, tol: float = 1e-9) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= tol)


def check_constraints_manifest(blobs: dict[str, bytes]) -> None:
    manifest = json.loads(blobs["constraints_manifest"])
    outputs = {o["path"]: o["sha256"] for o in manifest["outputs"]}
    inputs = {i["path"]: i["sha256"] for i in manifest["inputs"]}
    for key, name in (("canonical", "canonical_digitization.csv"), ("comparison", "modality_comparison.csv")):
        if outputs.get(name) != MON_INPUTS[key][1]:
            raise BuildError(f"constraints v2 manifest does not pin {name} with the expected sha256")
    for key in ("line1_points", "atlas_1", "atlas_5", "atlas_17", "atlas_6", "line1_manifest", "profiles_manifest"):
        path, want, _ = MON_INPUTS[key]
        if inputs.get(path) != want:
            raise BuildError(f"constraints v2 manifest does not pin {path} with the expected sha256")


def build_profiles(blobs: dict[str, bytes]) -> tuple[list[dict], dict]:
    canonical = read_csv(blobs["canonical"])
    digit = {(r["profile_label"], r["method"], r["observation_interval_label"], int(r["marker_index"])): r
             for r in read_csv(blobs["digitization"])}
    atlas = {r["point_id"]: r for key in ("atlas_1", "atlas_5", "atlas_17", "atlas_6") for r in read_csv(blobs[key])}
    line1 = {int(r["figure_order"]): r for r in read_csv(blobs["line1_points"])}
    if len(canonical) != 258 or len(digit) != 258 or len(atlas) != 258 or len(line1) != 14:
        raise BuildError("unexpected row counts in the frozen inputs")
    applied, rows = set(), []
    for c in canonical:
        line, legacy_method, interval = c["profile_label"], c["method"], c["observation_interval_label"]
        pos = int(c["figure_order"])
        if c["source_sha256"] != SOURCE_SHA256 or c["pdf_page"] != "15" or c["origin_class"] != "DIGITIZED":
            raise BuildError(f"{c['point_id']}: unexpected source/page/origin")
        if c["eligible_as_t1_time_series"] != "False" or c["mapped_synthetic_point_id"]:
            raise BuildError(f"{c['point_id']}: legacy row claims time-series use or a synthetic point mapping")
        value, bound = fnum(c["signed_displacement_mm"]), fnum(c["digitization_envelope_half_width_mm"])
        status = c["reading_status"]
        # cross-checks against the tables the canonical digitization was assembled from
        if c["reading_priority"] == "accepted_atlas":
            a = atlas[c["point_id"]]
            ok = (same(value, fnum(a["signed_displacement_mm"])) and same(bound, fnum(a["digitization_envelope_half_width_mm"]))
                  and status == a["reading_status"])
        elif c["reading_priority"] == "accepted_independent_detailed_reread":
            r = line1[pos]
            ok = ((line, legacy_method, interval) == ("1", "leveling", "2011-2016")
                  and same(value, fnum(r["signed_subsidence_mm"])) and same(bound, fnum(r["reading_envelope_half_width_mm"]))
                  and status == LINE1_STATUS[r["readability"]])
        else:
            raise BuildError(f"{c['point_id']}: unknown reading_priority {c['reading_priority']!r}")
        if not ok:
            raise BuildError(f"{c['point_id']}: canonical reading differs from its input table")
        d = digit[(line, legacy_method, interval, pos)]
        if c["reading_priority"] == "accepted_atlas" and not same(value, fnum(d["displacement_negative_down_mm"])):
            raise BuildError(f"{c['point_id']}: atlas value differs from the frozen digitization")
        label = d["distance_label_as_printed_km"]
        if (line, pos) in PRINTED_LABEL_CORRECTIONS:
            legacy_label, printed = PRINTED_LABEL_CORRECTIONS[(line, pos)]
            if label != legacy_label:
                raise BuildError(f"line {line} slot {pos}: expected legacy label {legacy_label!r}, got {label!r}")
            label = printed
            applied.add((line, pos))
        if (value is None) != (status == "unresolved_occlusion") or (value is not None and bound is None):
            raise BuildError(f"{c['point_id']}: value/status/bound inconsistent")
        start, end = interval.split("-")
        rows.append({
            "source_id": SOURCE_ID, "locator": LOCATOR, "line": line, "method": METHODS[legacy_method],
            "interval_start": start, "interval_end": end, "x_label_as_printed": label, "value_mm": fmt_tenth(value),
            "digitization_status": status, "digitization_uncertainty_mm": fmt_tenth(bound) if value is not None else "",
            "status": "FACT" if value is not None else "UNKNOWN", "scope": SCOPE, "evidence_note": EVIDENCE_NOTE,
            "position_index": pos, "reading_origin": READING_ORIGIN[c["reading_priority"]],
            "raster_x_px": fmt_px(fnum(c["source_pixel_x"])), "raster_y_px": fmt_px(fnum(c["source_pixel_y"])),
            "legacy_point_id": c["point_id"],
        })
    if applied != set(PRINTED_LABEL_CORRECTIONS):
        raise BuildError(f"label corrections not applied: {sorted(set(PRINTED_LABEL_CORRECTIONS) - applied)}")
    rows.sort(key=lambda r: (LINE_ORDER.index(r["line"]), METHOD_ORDER.index(r["method"]), r["interval_start"],
                             r["position_index"]))
    counters: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["line"], r["method"])
        counters[key] = counters.get(key, 0) + 1
        r["id"] = f"OBS-MUS-P15-{r['line']}-{r['method'].upper()}-{counters[key]:03d}"
    # printed x labels are a property of the chart: identical for every series of a line
    for line in LINE_ORDER:
        by_series: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            if r["line"] == line:
                by_series.setdefault((r["method"], r["interval_start"]), []).append(r["x_label_as_printed"])
        if len({tuple(v) for v in by_series.values()}) != 1:
            raise BuildError(f"line {line}: x labels differ between series")
    stats = {
        "rows": len(rows), "values": sum(r["value_mm"] != "" for r in rows),
        "digitization_status": dict(sorted(_count(r["digitization_status"] for r in rows).items())),
        "status": dict(sorted(_count(r["status"] for r in rows).items())),
        "reading_origin": dict(sorted(_count(r["reading_origin"] for r in rows).items())),
        "series": [{"line": ln, "method": m, "interval": f"{s}-{e}", "positions": n, "values": v}
                   for (ln, m, s, e), (n, v) in _series(rows).items()],
    }
    return rows, stats


def _count(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def _series(rows: list[dict]) -> dict[tuple[str, str, str, str], tuple[int, int]]:
    out: dict[tuple[str, str, str, str], tuple[int, int]] = {}
    for r in rows:
        key = (r["line"], r["method"], r["interval_start"], r["interval_end"])
        n, v = out.get(key, (0, 0))
        out[key] = (n + 1, v + (r["value_mm"] != ""))
    return out


def build_comparison(rows: list[dict], blobs: dict[str, bytes]) -> list[dict]:
    legacy = {(r["profile_label"], r["interval"]): r for r in read_csv(blobs["comparison"])}
    out = []
    for line in LINE_ORDER:
        for interval in sorted({(r["interval_start"], r["interval_end"]) for r in rows if r["line"] == line}):
            series = {m: {r["position_index"]: r for r in rows
                          if r["line"] == line and r["method"] == m and (r["interval_start"], r["interval_end"]) == interval}
                      for m in METHOD_ORDER}
            pairs = [(series["levelling"][p], series["insar"][p]) for p in sorted(series["levelling"])
                     if p in series["insar"] and series["levelling"][p]["value_mm"] and series["insar"][p]["value_mm"]]
            lev = [float(a["value_mm"]) for a, _ in pairs]
            ins = [float(b["value_mm"]) for _, b in pairs]
            d = [b - a for a, b in zip(lev, ins)]
            u = [float(a["digitization_uncertainty_mm"]) + float(b["digitization_uncertainty_mm"]) for a, b in pairs]
            rec = {"n": len(d), "mean": statistics.fmean(d), "median": statistics.median(d),
                   "rms": math.sqrt(statistics.fmean(x * x for x in d)), "max_abs": max(abs(x) for x in d),
                   "corr": statistics.correlation(lev, ins),
                   "overlap": sum(abs(x) <= w for x, w in zip(d, u)) / len(d)}
            old = legacy[(line, f"{interval[0]}-{interval[1]}")]
            olds = {"n": int(old["paired_readable"]), "mean": float(old["signed_radar_minus_leveling_mean_mm"]),
                    "median": float(old["median_mm"]), "rms": float(old["rms_disagreement_mm"]),
                    "max_abs": float(old["max_abs_mm"]), "corr": float(old["correlation"]),
                    "overlap": float(old["fraction_reading_intervals_overlap"])}
            bad = [k for k in rec if not same(float(rec[k]), olds[k], 1e-6)]
            if bad:
                raise BuildError(f"line {line} {interval}: recomputed {bad} differ from the legacy comparison")
            out.append({"id": f"OBS-MUS-P15-{line}-CMP-{interval[0]}_{interval[1]}", "source_id": SOURCE_ID,
                        "locator": LOCATOR, "line": line, "interval_start": interval[0], "interval_end": interval[1],
                        "n_paired_positions": rec["n"], "insar_minus_levelling_mean_mm": fmt6(rec["mean"]),
                        "insar_minus_levelling_median_mm": fmt6(rec["median"]), "rms_disagreement_mm": fmt6(rec["rms"]),
                        "max_abs_disagreement_mm": fmt6(rec["max_abs"]), "pearson_correlation": fmt6(rec["corr"]),
                        "fraction_within_reading_bounds": fmt6(rec["overlap"]), "status": "DERIVATION",
                        "scope": SCOPE, "evidence_note": COMPARISON_NOTE})
    if len(out) != len(legacy):
        raise BuildError("comparison row count differs from the legacy table")
    return out


def build_monitoring() -> dict[str, bytes]:
    blobs = verified_inputs(MON_INPUTS)
    check_constraints_manifest(blobs)
    rows, stats = build_profiles(blobs)
    comparison = build_comparison(rows, blobs)
    profiles_b, comparison_b = csv_bytes(PROFILE_COLUMNS, rows), csv_bytes(COMPARISON_COLUMNS, comparison)
    manifest = {
        "artifact_id": "OBS-MUS-P15",
        "decision": "D-07, docs/governance/PHASE1_DESIGN_DECISIONS_RU.md",
        "generator": "scripts/build_evidence_from_legacy.py",
        "command": "python scripts/build_evidence_from_legacy.py monitoring",
        "source": {"source_id": SOURCE_ID, "legacy_source_id": LEGACY_SOURCE_ID, "sha256": SOURCE_SHA256,
                   "locator": LOCATOR, "pdf_xobjects_by_line": PDF_XOBJECTS, "register_scope": "VKM_Solikamsk"},
        "legacy_commit": LEGACY_COMMIT,
        "inputs": [{"key": k, "path": p, "sha256": s, "role": r} for k, (p, s, r) in MON_INPUTS.items()],
        "outputs": [{"path": PROFILES_CSV, "sha256": sha256(profiles_b), "rows": len(rows)},
                    {"path": COMPARISON_CSV, "sha256": sha256(comparison_b), "rows": len(comparison)}],
        "counts": stats,
        "printed_label_corrections": [{"line": ln, "position_index": pos, "legacy_label": a, "as_printed": b}
                                      for (ln, pos), (a, b) in sorted(PRINTED_LABEL_CORRECTIONS.items(),
                                                                      key=lambda kv: (LINE_ORDER.index(kv[0][0]), kv[0][1]))],
        "mapping": {"method": METHODS, "status": "FACT if a value was read, UNKNOWN for unresolved occlusions",
                    "id": "OBS-MUS-P15-<line>-<METHOD>-<nnn>, nnn over (interval_start, position_index)",
                    "dropped_legacy_fields": ["eligible_as_t1_time_series", "mapped_synthetic_point_id",
                                              "distance_value_from_label_km", "source_file", "input_table*"]},
        "checks": {"inputs_sha256_pinned": True, "constraints_v2_manifest_pins_inputs": True,
                   "canonical_equals_atlas_or_detailed_reread": True, "atlas_equals_frozen_digitization": True,
                   "unresolved_positions_not_filled": True, "x_labels_identical_across_series_of_a_line": True,
                   "modality_comparison_recomputed_equals_legacy_1e-6": True},
        "limits": LIMITS,
    }
    manifest_b = (json.dumps(manifest, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    return {PROFILES_CSV: profiles_b, COMPARISON_CSV: comparison_b, MANIFEST_JSON: manifest_b}


def build_sources() -> dict[str, bytes]:
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        raise BuildError("set VKM_RESOURCES_ROOT to the PRIVATE resources checkout (SOURCE_REGISTER.csv is read)")
    register = read_csv((Path(res) / "00_registry" / "SOURCE_REGISTER.csv").read_bytes())
    by_sha: dict[str, list[dict]] = {}
    for r in register:
        by_sha.setdefault(r["sha256"].strip().lower(), []).append(r)
    rows = []
    for manifest, want in SRC_MANIFESTS.items():
        data = git_blob(manifest)
        if sha256(data) != want:
            raise BuildError(f"{manifest}: sha256 differs from the pinned legacy manifest")
        for m in read_csv(data):
            hits = by_sha.get(m["sha256"].strip().lower(), [])
            if len(hits) != 1:
                raise BuildError(f"{m['source_id']}: {len(hits)} register rows with its sha256 (expected exactly 1)")
            reg = hits[0]
            if reg["size_bytes"] != m["size_bytes"]:
                raise BuildError(f"{m['source_id']}: size differs from the register")
            rows.append({"legacy_source_id": m["source_id"], "legacy_manifest": f"{manifest}@{LEGACY_COMMIT[:7]}",
                         "legacy_file_name": m["name"], "legacy_authority_class": m["authority_class"],
                         "legacy_site_scope": m["site_scope"], "vkm_src_id": reg["resource_id"],
                         "sha256": m["sha256"].strip().lower(), "size_bytes": m["size_bytes"],
                         "register_source_class": reg["source_class"], "register_evidence_scope": reg["evidence_scope"],
                         "match_method": "sha256_exact+size", "note": SRC_NOTES.get(m["source_id"], "")})
    rows.sort(key=lambda r: (r["legacy_source_id"].startswith("SUP"), r["legacy_source_id"]))
    if len({r["vkm_src_id"] for r in rows}) != len(rows):
        raise BuildError("two legacy ids map to the same VKM-SRC id")
    return {SRC_MAP_CSV: csv_bytes(SRC_COLUMNS, rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", choices=("monitoring", "sources"))
    parser.add_argument("--check", action="store_true", help="compare with committed files instead of writing")
    args = parser.parse_args(argv)
    try:
        outputs = build_monitoring() if args.target == "monitoring" else build_sources()
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    differing = []
    for rel, data in outputs.items():
        path = ROOT / rel
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                differing.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"{rel}  sha256={sha256(data)}")
    if args.check:
        print("OK: committed files equal the rebuild" if not differing else f"DIFFERENT: {differing}")
        return 1 if differing else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
