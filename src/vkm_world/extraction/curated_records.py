"""Lossless manual source records within WORLD_PARAMETERS/curated.

These are source extracts, not a PhysicalWorld or a new evidence stream. The
ordinary passport, observation and mine-history builders consume the same input.
The typed projection never changes the original record or upgrades its status.
Quotes remain a separate PRIVATE field, so PUBLIC's existing quote guard applies.
"""
from __future__ import annotations

import json
import re
import csv
from collections import Counter
from pathlib import Path

INPUT = "WORLD_PARAMETERS/curated/source_records.jsonl"
IDENTITIES_INPUT = "SOURCES/source_identifier_aliases.csv"
TYPES = {"EVIDENCE", "OBSERVATION_POINT", "OBJECT_GEOMETRY", "MINING_HISTORY",
         "SERIES_IDENTITY", "INFORMATION_AVAILABILITY", "CONFLICT"}
PRODUCER = "CURATED_SOURCE_RECORD"


def load(canon: Path) -> list[dict]:
    path = canon / INPUT
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = []
    for entry in rows:
        row = entry.get("source_record")
        if (entry.get("schema") != "vkm.curated_source_record/1" or entry.get("record_type") not in TYPES
                or not isinstance(row, dict) or not row.get("record_id")
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in row.items())
                or "quote" in row or not isinstance(entry.get("quote", ""), str)
                or not isinstance(entry.get("source_table"), str) or not entry["source_table"]
                or not isinstance(entry.get("source_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["source_sha256"])
                or not isinstance(entry.get("source_namespace", "SOURCE_REGISTER"), str)
                or not isinstance(entry.get("consumer_origin", ""), str)):
            raise ValueError("invalid curated source record")
        ids.append(row["record_id"])
    duplicates = [rid for rid, count in Counter(ids).items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate curated record IDs: {duplicates[:5]}")
    return rows


def load_source_identities(canon: Path) -> dict:
    """Existing SOURCES aliases resolve a namespaced identifier to one registered file.

    The migration envelope's source_sha256 hashes a derived CSV. It is never a
    substitute for the registered publication's sha256.
    """
    path = canon / IDENTITIES_INPUT
    if not path.is_file():
        return {}
    out = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            namespace, original = row.get("source_namespace", ""), row.get("source_id", "")
            canonical, digest = row.get("vkm_src_id", ""), row.get("sha256", "")
            if (not namespace or not original or not re.fullmatch(r"VKM-SRC-\d{3}", canonical)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or (namespace == "SOURCE_REGISTER" and original != canonical)):
                raise ValueError("invalid namespaced source alias")
            key = namespace, original
            if key in out:
                raise ValueError(f"ambiguous source alias: {namespace}:{original}")
            out[key] = canonical, digest
    return out


def resolve_source(entry: dict, identities: dict | None) -> dict:
    original = entry["source_record"].get("source_id", "")
    # The imported VKM-SRC identifiers are from SOURCE_REGISTER, not the old
    # SRC01..SRC11 namespace. A different namespace requires an explicit label.
    namespace = entry.get("source_namespace", "SOURCE_REGISTER") if original else ""
    if original:
        try:
            canonical, digest = (identities or {})[namespace, original]
        except KeyError as exc:
            raise ValueError(f"unresolved source identity: {namespace}:{original}") from exc
    else:
        canonical, digest = "", ""
    return {"source_id": canonical, "source_sha256": digest,
            "original_source_id": original, "original_source_namespace": namespace,
            "resolved_source_ref": f"SOURCE_REGISTER:{canonical}@sha256:{digest}" if canonical else "",
            "source_resolution": "EXACT_NAMESPACED_ALIAS" if canonical else "NO_SINGLE_SOURCE_ID"}


def provenance(entry: dict, identities: dict | None = None) -> dict:
    return {"source_table": entry["source_table"], "source_table_sha256": entry["source_sha256"],
            "source_record_json": json.dumps(entry["source_record"], ensure_ascii=False, sort_keys=True),
            "record_type": entry["record_type"], **resolve_source(entry, identities)}


def passport_rows(entries: list[dict], identities: dict | None = None) -> list[dict]:
    out = []
    for entry in entries:
        r, typ = entry["source_record"], entry["record_type"]
        module = {"OBJECT_GEOMETRY": "C1", "OBSERVATION_POINT": "OBS", "MINING_HISTORY": "C4"}.get(typ, "OTHER")
        out.append(dict(producer=PRODUCER, producer_record_id=r["record_id"], module=module,
                        parameter=r.get("topic") or r.get("event") or typ, kind=typ,
                        target=r.get("object") or r.get("benchmark_id") or r.get("label", ""),
                        material_class="", material_as_printed="", scale=r.get("scale", "UNKNOWN"),
                        branch="-", site_norm=r.get("site_norm", entry.get("site_norm", "UNKNOWN")), site_group="GENERAL_OR_UNSTATED",
                        site_basis=r.get("site_basis", ""), content_kind=typ,
                        value_as_printed=r.get("value", r.get("value_mm", "")),
                        unit_as_printed=r.get("unit", "mm" if typ == "OBSERVATION_POINT" else ""),
                        si_min="", si_max="", si_unit="", conversion="NOT_REINTERPRETED",
                        conditions=r.get("scope", ""), method=r.get("method", ""), n_samples="",
                        origin="MIGRATED_SOURCE_RECORD", cited_ref="",
                        page_id=r.get("page_id", ""), locator=r.get("locator", r.get("figure", "")),
                        quote=entry.get("quote", ""), status=r.get("status", ""),
                        verification="MIGRATED_UNCHANGED", notes=r.get("notes", r.get("note", "")),
                        time_as_printed=r.get("epoch", r.get("valid_time", r.get("time_basis", ""))),
                        equation_as_printed="", symbol="", **provenance(entry, identities)))
    return out


def point_rows(entries: list[dict], identities: dict | None = None) -> list[dict]:
    out = []
    for entry in entries:
        if entry["record_type"] != "OBSERVATION_POINT":
            continue
        r = entry["source_record"]
        out.append(dict(point_id=r["record_id"], obs_id=("WOB-DIG-" + r["digitization_ref"]) if r["digitization_ref"] else "",
                        x_local_m=r["x_sh1"], y_local_m=r["y_sh1"], xy_error_m=r["err_position_m"],
                        xy_status=r.get("status", ""), series_id=r["digitization_ref"],
                        page_id=r["page_id"], epoch_or_date=r["epoch"], line_id=r["line_id"],
                        benchmark_id=r["benchmark_id"], x_value=r["dist_from_rp45_m"], x_unit="m",
                        y_value=r["value_mm"], y_unit="mm", sign_convention=r["quantity"],
                        point_kind=r["series_nature"], status=r["status"],
                        independence_group=r["independence_group"], copy_role="REEXTRACTION_SAME_PUBLISHED_SERIES",
                        halfwidth_mm=r["halfwidth_mm"], zero_epoch=r["zero_epoch"],
                        zero_line_offset_mm=r["zero_line_offset_mm"], recheck_mm=r["recheck_mm"],
                        recheck_diff_mm=r["recheck_diff_mm"], recheck_verdict=r["recheck_verdict"],
                        qc_flag=r["qc_flag"], available_from=r["available_from"], **provenance(entry, identities)))
    return out


def conflict_rows(entries: list[dict], identities: dict | None = None) -> list[dict]:
    return [dict(pc_id="", conflict_id=r.get("conflict_id", r["record_id"]), origin=entry["source_table"], kind=r["kind"],
                 topic=r.get("description_ru", r.get("topic", "")), parameter="", target="", site_group="",
                 values=r.get("values", " | ".join(filter(None, [r.get("value_a"), r.get("value_b")]))),
                 sources=r.get("sources", ";".join(filter(None, [r.get("source_a"), r.get("source_b")]))),
                 resolution_status=r.get("status", r.get("resolution", "")),
                 evidence_ids=r["record_id"], **provenance(entry, identities))
            for entry in entries if entry["record_type"] == "CONFLICT" for r in [entry["source_record"]]]


def object_rows(entries: list[dict], identities: dict | None = None) -> list[dict]:
    return [dict(wo_id="", object_id=r["record_id"], kind=r["feature_kind"], mine="", ident=r["record_id"],
                 name=r["label"], level="", entity_type=r["feature_kind"], parent_id="",
                 mine_attribution="", site_scope="", origin="MIGRATED_SOURCE_RECORD", resolution="SOURCE_GEOMETRY",
                 n_mentions_extraction=0, names_as_printed=r["label"], pages=r["page_id"], source_ids=r["source_id"],
                 status=r["status"], review_basis="", review_page_id="", merged_ids="",
                 geometry_wkt=r["wkt_sh1"], coordinate_frame=r["frame"],
                 **provenance(entry, identities))
            for entry in entries if entry["record_type"] == "OBJECT_GEOMETRY" for r in [entry["source_record"]]]


def csv_columns(rows: list[dict], base: list[str] | None = None) -> list[str]:
    """Stable union: heterogeneous manual extensions must never be silently dropped."""
    columns = list(base or (list(rows[0]) if rows else ["empty"]))
    seen = set(columns)
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    return columns
