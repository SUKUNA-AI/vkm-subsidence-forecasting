#!/usr/bin/env python3
"""Observation catalogue for checking the forecast (09.10.2026): real observations of all VKM sites with strict mine
attribution, SKRU-1 flagged separately.

Inputs (PRIVATE ``11_evidence_vnext/canonical/``):
- ``WORLD_OBSERVATIONS/figure_series_review.csv`` — figures checked by eye on 08.10 (YES / PARTIAL kept);
- ``WORLD_OBSERVATIONS/digitized/<source>/<figure>/`` — figures digitized on 09.10 (``series.csv``,
  ``calibration.json`` with the axis reference points, ``meta.json``, overlay), each series a DERIVATION;
- ``MONITORING_LIFECYCLE/monitoring_observation_catalog.csv`` — Phase-1 monitoring catalogue (datasets only);
- ``WORLD_EXTRACTION/<run>/accepted.jsonl`` — observation values printed in the text (page extraction);
- PUBLIC ``evidence/monitoring/musikhin_vkm_src002_p15_profiles.csv`` — the Musikhin digitization (Phase 1);
- ``--nav`` (NAV page export): ``available_from`` of each source = the last day of its publication period
  (``works_availability.available_latest_day``), the conservative date for the rule ``available_from ≤ t0``.

Outputs (``WORLD_OBSERVATIONS/``): ``world_observation_catalog.csv`` (one row per observation item),
``digitized_series_points.csv`` (all points of the digitized series, long format), ``build_receipt.json``.
A digitized value is never called a measurement: status DERIVATION; a number printed in the text is
PUBLISHED_VALUE; neither is a raw survey record.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> python scripts/build_world_observations.py --nav <db> --runs high_full double
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = "world_observations_v1"
MUSIKHIN = ROOT / "evidence" / "monitoring" / "musikhin_vkm_src002_p15_profiles.csv"
OBS_CODES = {"SUBSIDENCE", "SUBSIDENCE_RATE", "SUBSIDENCE_MAX", "TILT", "CURVATURE", "HORIZONTAL_STRAIN",
             "HORIZONTAL_DISPLACEMENT", "CONVERGENCE", "CONVERGENCE_RATE", "PILLAR_STRAIN", "INSAR_LOS"}
NOT_OBSERVATION = ("MODEL_RESULT", "BENCHMARK_POSITION_AS_PUBLISHED")    # digitized, kept out of the catalogue
NATURE_MODALITY = {"FIELD_SURFACE_SUBSIDENCE": "LEVELLING", "FIELD_INSAR": "INSAR", "FIELD_CONVERGENCE": "CONVERGENCE",
                   "FIELD_OTHER": "OTHER"}
MON_MODALITY = {"insar": "INSAR", "levelling": "LEVELLING", "profile_line": "LEVELLING", "convergence": "CONVERGENCE",
                "underground_station": "CONVERGENCE", "gnss": "GNSS", "hydrostatic_levelling": "LEVELLING"}


def modality_of(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"insar|интерферометр|радар|los|ps-", t):
        return "INSAR"
    if re.search(r"конвергенц|смещени\w* контур|контур\w* выработ|деформир\w* мкц|поперечн\w* деформир", t):
        return "CONVERGENCE"
    if re.search(r"gnss|gps", t):
        return "GNSS"
    if re.search(r"оседан|нивелир|репер|профильн|мульд|сдвижен", t):
        return "LEVELLING"
    return "OTHER"


def fig_no(label: str | None) -> str:
    """«Рис. 4.3.4», «Рисунок 4.3.4 –», «рисунок 2.» → «4.3.4», «2»."""
    m = re.search(r"(\d+(?:\.\d+)*)", label or "")
    return m.group(1) if m else ""


def skru1_flag(site_norm: str) -> str:
    s = (site_norm or "").upper()
    if s == "SKRU1":
        return "YES"
    if s in ("SKRU1_OR_SKRU2_UNATTRIBUTED", "SKRU1_SKRU2_PILLAR"):
        return "SKRU1_OR_SKRU2"
    return "NO"


def _rows(p: Path) -> list[dict]:
    return list(csv.DictReader(open(p, encoding="utf-8"))) if p.is_file() else []


def availability(nav: Path | None) -> dict[str, tuple[str, str]]:
    if not nav or not nav.is_file():
        return {}
    import duckdb
    c = duckdb.connect(str(nav), read_only=True)
    out = {}
    for sid, day, basis in c.execute(
            """select ws.source_id, wa.available_latest_day, wa.available_from_basis from work_sources ws
               join works_availability wa on wa.work_id = ws.work_id where ws.is_primary order by ws.source_id""").fetchall():
        if day is not None:
            out.setdefault(sid, (day.isoformat(), basis or ""))
    return out


def base(**kw) -> dict:
    keys = ["obs_id", "item_kind", "modality", "site_as_printed", "site_norm", "skru1_flag", "georef", "line_or_benchmark",
            "epochs", "time_start", "time_end", "quantity", "unit", "value_min", "value_max", "n_points",
            "available_from", "available_from_basis", "source_id", "page_id", "figure_label", "figure_id",
            "calibration_ref", "calibration_check", "status", "primary_data_origin", "applicability",
            "usable_for_validation", "producer", "review_status", "notes"]
    r = {k: "" for k in keys}
    r.update({k: ("" if v is None else v) for k, v in kw.items()})
    return r


def _years(text: str) -> tuple[str, str]:
    ys = sorted(int(y) for y in re.findall(r"(?<!\d)(19[5-9]\d|20[0-3]\d)(?!\d)", text or ""))
    return (str(ys[0]), str(ys[-1])) if ys else ("", "")


def georef_points(canon: Path) -> dict[str, dict]:
    """Benchmarks placed on the SKRU-1 mine-field plan (``WORLD_OBSERVATIONS/georef/*.geojson``, local system
    SKRU1_LOCAL_SH1: X east, Y north, metres, origin shaft No 1, CRS UNKNOWN): digitized benchmark id → position."""
    out = {}
    for p in sorted((canon / "WORLD_OBSERVATIONS" / "georef").glob("*.geojson")):
        for f in json.loads(p.read_text(encoding="utf-8")).get("features", []):
            pr, geom = f.get("properties") or {}, f.get("geometry") or {}
            key = pr.get("digitized_benchmark_id") or pr.get("benchmark_id")
            if key and geom.get("type") == "Point":
                out[key] = {"x": geom["coordinates"][0], "y": geom["coordinates"][1], "error_m": pr.get("error_m"),
                            "status": pr.get("status") or "", "method": pr.get("method") or ""}
    return out


def build(canon: Path, runs: list[str], nav: Path | None) -> dict[str, bytes]:
    avail = availability(nav)
    geo = georef_points(canon)
    obs, points = [], []
    excluded = Counter()

    def av(sid):
        return avail.get(sid, ("", "UNKNOWN"))

    # 1. digitized series (09.10)
    dig = canon / "WORLD_OBSERVATIONS" / "digitized"
    for meta_p in sorted(dig.glob("*/*/meta.json")):
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        fig_dir = meta_p.parent
        rel = fig_dir.relative_to(canon).as_posix()
        series = _rows(fig_dir / "series.csv")
        by_series: dict[str, list[dict]] = {}
        for s in series:
            by_series.setdefault(s["series_id"], []).append(s)
        sid = meta.get("source_id") or fig_dir.parent.name
        for ser_id, pts in sorted(by_series.items()):
            first = pts[0]
            nature = first.get("series_nature") or "OBSERVATION_AS_PUBLISHED"
            if nature in NOT_OBSERVATION:
                excluded[nature] += len(pts)
                continue
            ys = [float(p["y_value"]) for p in pts if p.get("y_value") not in (None, "")]
            obs.append(base(obs_id=f"WOB-DIG-{ser_id}", item_kind="DIGITIZED_SERIES",
                            modality=modality_of(" ".join(str(meta.get(k, "")) for k in ("quantity", "caption",
                                                                                           "site_as_printed"))),
                            site_as_printed=meta.get("site_as_printed"), site_norm=meta.get("site_norm"),
                            skru1_flag=skru1_flag(meta.get("site_norm")),
                            line_or_benchmark=";".join(meta.get("line_ids") or []) or first.get("line_id"),
                            epochs=first.get("epoch_or_date") or "", time_start=first.get("epoch_or_date") or "",
                            time_end=first.get("epoch_or_date") or "", quantity=meta.get("quantity"),
                            unit=first.get("y_unit"), value_min=f"{min(ys):.6g}" if ys else "",
                            value_max=f"{max(ys):.6g}" if ys else "", n_points=len(pts),
                            available_from=av(sid)[0] or meta.get("available_from", ""),
                            available_from_basis=av(sid)[1] if av(sid)[0] else meta.get("available_from_precision", ""),
                            source_id=sid, page_id=meta.get("page_id"), figure_label=meta.get("figure_label"),
                            figure_id=meta.get("figure_id_layer", ""), calibration_ref=f"{rel}/calibration.json",
                            calibration_check="AXIS_POINTS_IN_CALIBRATION_JSON", status="DERIVATION",
                            primary_data_origin=meta.get("primary_data_origin", ""),
                            applicability=meta.get("applicability", ""),
                            usable_for_validation="YES" if meta.get("site_norm") not in ("UNKNOWN", "") else "PARTIAL",
                            producer=meta.get("digitizer", "Claude subagent 09.10.2026"),
                            review_status=meta.get("review_status", "AUTO_EXTRACTED_UNREVIEWED"),
                            notes=(("среднее, вычисленное автором (DERIVATION автора); " if nature == "AUTHOR_DERIVED_MEAN"
                                    else "") + "; ".join(meta.get("notes", []) if isinstance(meta.get("notes"), list)
                                                         else [str(meta.get("notes", ""))]))[:500]))
            n_geo = sum(1 for p in pts if p.get("benchmark_id") in geo)
            obs[-1]["georef"] = f"{n_geo}/{len(pts)} точек на плане СКРУ-1 (SKRU1_LOCAL_SH1)" if n_geo else ""
            for p in pts:
                g = geo.get(p.get("benchmark_id") or "", {})
                points.append({"point_id": f"WOP-{len(points) + 1:06d}", "obs_id": f"WOB-DIG-{ser_id}",
                               "x_local_m": g.get("x", ""), "y_local_m": g.get("y", ""),
                               "xy_error_m": g.get("error_m", "") if g.get("error_m") is not None else "",
                               "xy_status": g.get("status", ""), "series_id": ser_id, "source_id": sid,
                               "page_id": meta.get("page_id"), "epoch_or_date": p.get("epoch_or_date", ""),
                               "line_id": p.get("line_id", ""), "benchmark_id": p.get("benchmark_id", ""),
                               "x_value": p.get("x_value", ""), "x_unit": p.get("x_unit", ""),
                               "y_value": p.get("y_value", ""), "y_unit": p.get("y_unit", ""),
                               "sign_convention": p.get("sign_convention", ""), "point_kind": p.get("point_kind", ""),
                               "status": "DERIVATION"})
    digitized_pages = {(o["page_id"], fig_no(o["figure_label"])) for o in obs}

    # 2. figures reviewed on 08.10 (YES / PARTIAL) not digitized again
    for r in _rows(canon / "WORLD_OBSERVATIONS" / "figure_series_review.csv"):
        if r["usable_for_validation"] not in ("YES", "PARTIAL"):
            continue
        t0, t1 = _years(r["time_support"] + " " + r["epochs"])
        obs.append(base(obs_id=f"WOB-FIG-{r['figure_id']}", item_kind="REVIEWED_FIGURE_SERIES",
                        modality=NATURE_MODALITY.get(r["nature"], modality_of(r["quantity"])),
                        site_as_printed=r["site_as_printed"], site_norm=r["site_norm"],
                        skru1_flag=skru1_flag(r["site_norm"]), epochs=r["epochs"], time_start=t0, time_end=t1,
                        quantity=r["quantity"], unit=r["unit"], n_points="", available_from=av(r["source_id"])[0],
                        available_from_basis=av(r["source_id"])[1], source_id=r["source_id"], page_id=r["page_id"],
                        figure_label=r["figure_label"], figure_id=r["figure_id"],
                        calibration_ref="NAV figure_series (auto, vector route)", calibration_check=r["calibration_check"],
                        status="DERIVATION", applicability=r["reason"], usable_for_validation=r["usable_for_validation"],
                        producer=r["reviewed_by"], review_status="AUTO_EXTRACTED_UNREVIEWED",
                        notes=((" also digitized 09.10 (see DIGITIZED_SERIES) |" if (r["page_id"], fig_no(r["figure_label"]))
                                in digitized_pages else "") + r["notes"])[:500]))

    # 3. Phase-1 monitoring catalogue: datasets with values in the corpus
    for r in _rows(canon / "MONITORING_LIFECYCLE" / "monitoring_observation_catalog.csv"):
        if r["item_kind"] not in ("DATASET", "DATASET_METADATA"):
            continue
        mod = MON_MODALITY.get(r["schema_modality"])
        if mod is None:
            continue
        t0, t1 = _years(r["time_support"])
        page = re.match(r"\s*p(\d{1,4})", r["locator"])
        obs.append(base(obs_id=f"WOB-MON-{r['cat_id']}", item_kind="MONITORING_CATALOGUE_DATASET", modality=mod,
                        site_as_printed=r["site_attribution_strict"], site_norm=r["scope"],
                        skru1_flag=skru1_flag(r["scope"]), time_start=t0, time_end=t1, epochs=r["time_support"][:200],
                        quantity=r["observable"][:200], unit="", available_from=av(r["source_id"])[0],
                        available_from_basis=av(r["source_id"])[1], source_id=r["source_id"],
                        page_id=f"{r['source_id']}:p{int(page.group(1)):04d}" if page else "",
                        figure_label=r["locator"][:120], status=r["status"], applicability=r["usable_for_skru1"],
                        usable_for_validation="PARTIAL" if r["values_in_corpus"] not in ("", "NO", "none") else "NO",
                        producer="PHASE1_CATALOGUE", review_status="PHASE1_REVIEWED",
                        notes=(r["values_summary"] or "")[:500]))

    # 4. Musikhin digitization (PUBLIC, Phase 1)
    mus = _rows(MUSIKHIN)
    for line in sorted({r["line"] for r in mus}):
        rs = [r for r in mus if r["line"] == line]
        vals = [float(r["value_mm"]) for r in rs if r["value_mm"]]
        obs.append(base(obs_id=f"WOB-MUS-P15-L{line}", item_kind="PUBLIC_DIGITIZATION_PHASE1", modality="LEVELLING",
                        site_as_printed=rs[0]["scope"], site_norm="SKRU1_OR_SKRU2_UNATTRIBUTED",
                        skru1_flag="SKRU1_OR_SKRU2", line_or_benchmark=f"линия {line}",
                        time_start=rs[0]["interval_start"], time_end=rs[0]["interval_end"], unit="мм",
                        value_min=f"{min(vals):.6g}" if vals else "", value_max=f"{max(vals):.6g}" if vals else "",
                        n_points=len(rs), available_from=av("VKM-SRC-002")[0], available_from_basis=av("VKM-SRC-002")[1],
                        source_id="VKM-SRC-002", page_id="VKM-SRC-002:p0015", status="DERIVATION",
                        calibration_ref="evidence/monitoring/musikhin_vkm_src002_p15_manifest.json",
                        applicability=rs[0]["evidence_note"], usable_for_validation="PARTIAL",
                        producer="scripts/build_evidence_from_legacy.py (Phase 1)", review_status="PHASE1_REVIEWED",
                        notes=f"{sum(1 for r in rs if not r['value_mm'])} точек без значения (occlusion)"))

    # 5. values printed in the text (page extraction)
    for run in runs:
        p = canon / "WORLD_EXTRACTION" / run / "accepted.jsonl"
        if not p.is_file():
            continue
        for r in (json.loads(x) for x in open(p, encoding="utf-8")):
            if not (r.get("kind") == "OBSERVATION" or r.get("parameter_code") in OBS_CODES):
                continue
            if r.get("scale") not in ("FIELD", "UNKNOWN"):
                continue                                         # model results are not observations
            sid = r["page_id"].split(":")[0]
            t0, t1 = _years(r.get("time_as_printed") or "")
            obs.append(base(obs_id=f"WOB-TXT-{r['record_id']}", item_kind="TEXT_VALUE",
                            modality=modality_of(" ".join(x or "" for x in (r.get("parameter"), r.get("method"),
                                                                           r.get("quote")))),
                            site_as_printed=r.get("site_as_printed"), site_norm=r.get("site_norm"),
                            skru1_flag=skru1_flag(r.get("site_norm")), line_or_benchmark=r.get("entity_name") or "",
                            epochs=r.get("time_as_printed"), time_start=t0, time_end=t1,
                            quantity=f"{r.get('parameter') or ''} [{r.get('parameter_code')}]",
                            unit=r.get("unit_as_printed"), value_min=r.get("value_min"), value_max=r.get("value_max"),
                            available_from=av(sid)[0], available_from_basis=av(sid)[1], source_id=sid,
                            page_id=r["page_id"], figure_label=r.get("locator"), status="PUBLISHED_VALUE",
                            applicability=r.get("conditions") or "",
                            usable_for_validation="PARTIAL" if r.get("site_norm") not in ("UNKNOWN", None) else "NO",
                            producer=f"page extraction {run}", review_status="AUTO_EXTRACTED_UNREVIEWED",
                            notes=(r.get("notes") or "")[:300]))
    obs.sort(key=lambda o: ({"YES": 0, "SKRU1_OR_SKRU2": 1, "NO": 2}[o["skru1_flag"]], o["item_kind"], o["obs_id"]))
    files = {"world_observation_catalog.csv": _csv(obs), "digitized_series_points.csv": _csv(points)}
    receipt = {"rule_version": RULE_VERSION, "runs": runs,
               "outputs": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
               "counts": {"items": len(obs), "by_kind": dict(Counter(o["item_kind"] for o in obs).most_common()),
                          "by_modality": dict(Counter(o["modality"] for o in obs).most_common()),
                          "by_skru1_flag": dict(Counter(o["skru1_flag"] for o in obs).most_common()),
                          "by_site": dict(Counter(o["site_norm"] for o in obs).most_common()),
                          "digitized_points": len(points),
                          "digitized_points_georeferenced": sum(1 for x in points if x.get("x_local_m") != ""),
                          "digitized_points_excluded_not_observation": dict(excluded),
                          "without_available_from": sum(1 for o in obs if not o["available_from"])},
               "nav_used": bool(avail)}
    files["build_receipt.json"] = (json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    return files


def _csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    if not rows:
        return b"empty\n"
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", nargs="*", default=["high_full", "double"])
    ap.add_argument("--nav", type=Path)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT", file=sys.stderr)
        return 2
    canon = Path(res) / "11_evidence_vnext" / "canonical"
    files = build(canon, a.runs, a.nav)
    out = canon / "WORLD_OBSERVATIONS"
    if a.check:
        bad = [k for k, v in files.items() if not (out / k).is_file() or (out / k).read_bytes() != v]
        print(json.dumps({"check": "FAIL" if bad else "PASS", "differs": bad}))
        return 1 if bad else 0
    for k, v in files.items():
        (out / k).write_bytes(v)
    print(files["build_receipt.json"].decode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
