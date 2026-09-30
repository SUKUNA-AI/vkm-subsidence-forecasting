"""CPU-only screening of every registered source; outputs belong in ignored work/.

Canonical text is searched locally and is never printed. Screening is an auditable
candidate-discovery operation, not human reading or an estimate of figure recall.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from pathlib import Path


SNAPSHOT = "snap-20260929T175107Z-574daaac"
RULES = {
    "MINE_PLAN": r"(?:план|схем\w*|контур\w*)[^.\n]{0,100}(?:шахт\w*\s+пол|рудничн\w*\s+пол|горн\w*\s+отвод|панел|блок|горн\w*\s+работ|выработ)|(?:mine|mining)\s+(?:plan|layout|map)",
    "GEOLOGICAL_SECTION": r"геологическ\w*\s+(?:разрез|колон|профил)|стратиграфическ\w*\s+(?:колон|разрез)|(?:geological|stratigraphic)\s+(?:section|column|profile)",
    "BOREHOLE": r"(?:разрез|колон\w*|профил\w*|отбив\w*)[^.\n]{0,80}(?:скважин|скв\.)|(?:borehole|well)\s+(?:log|section|profile)",
    "SPATIAL_MONITORING": r"(?:расположени\w*|схем\w*|план\w*)[^.\n]{0,100}(?:репер|профильн\w*\s+лини|наблюдательн\w*\s+станц|скважин|ствол)|(?:profile|survey)\s+line|(?:map|location)[^.\n]{0,50}(?:mine|borehole)",
    "GEOLOGY_MAP": r"(?:геологическ\w*|тектоническ\w*)\s+(?:карт|схем)|(?:geological|tectonic)\s+map",
    "DEPTH_THICKNESS": r"(?:глубин|мощност|thickness|depth)[^.\n]{0,60}(?:пласт|сло|скважин|seam|strat|borehole)",
}
COMPILED = {k: re.compile(v, re.I) for k, v in RULES.items()}
CAPTION = re.compile(r"(?:^|\n)\s*(?:рис(?:унок)?\.?|fig(?:ure)?\.?)\s*[\dIVX]+[^\n]{0,400}", re.I)
SITE = re.compile(r"(?:СКРУ\s*[-–—]?\s*1|Соликамск\w*\s+(?:рудник|калийн)|SKRU\s*[-–—]?\s*1)", re.I)
LEXICAL_GEOLOGY = re.compile(r"геолог|стратиграф|скважин|пласт|borehole|stratigraph", re.I)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def relevance(text: str) -> list[str]:
    return sorted(k for k, pattern in COMPILED.items() if pattern.search(text or ""))


def captions(text: str) -> list[str]:
    return [m.group(0).strip() for m in CAPTION.finditer(text or "")]


def assert_private_output(out: Path, repo: Path) -> None:
    # Real corpus text, coordinates and graphic products must never reach PUBLIC.
    if not out.resolve().is_relative_to((repo / "work").resolve()):
        raise ValueError("coverage outputs must be below ignored work/")


def safe_source(root: Path, logical: str) -> Path:
    path = (root / logical).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("source escapes resources root")
    return path


def known_inventory(path: Path) -> tuple[set[str], set[str], dict[str, int]]:
    ids, pages, counts = set(), set(), Counter()
    for item in json.loads(path.read_text(encoding="utf-8")):
        raw_id = item.get("source_object_id")
        ids.update([raw_id] if isinstance(raw_id, str) else (raw_id or []))
        loc = item.get("locator") or {}
        p = item.get("page_id") or loc.get("page_id")
        if p:
            pages.add(p)
        counts[item["source_id"]] += 1
    return ids, pages, dict(counts)


def box_area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def inventory_matches(candidate: dict, inventory: list[dict]) -> list[dict]:
    """ID disagreement alone must not manufacture a new graphic object."""
    matches = []
    for old in inventory:
        if old["source_id"] != candidate["source_id"]:
            continue
        loc = old.get("locator") or {}
        # Inventory versions use both scalar figure_id and plural object_ids.
        # Combine them rather than letting a stale alias hide an exact match.
        ids = set()
        for value in (old.get("source_object_id"), loc.get("object_ids"), loc.get("figure_id")):
            if isinstance(value, str):
                ids.add(value)
            elif value:
                ids.update(value)
        if (candidate.get("source_sha256") and old.get("source_sha256")
                and candidate["source_sha256"] != old["source_sha256"]):
            continue
        reasons = []
        if candidate["figure_id"] in ids:
            reasons.append("EXACT_OBJECT_ID")
        page = old.get("page_id") or loc.get("page_id")
        old_box = loc.get("bbox_pt_tl")
        box = candidate.get("bbox_page")
        if page == candidate["page_id"] and old_box and box and all(v is not None for v in (*box, *old_box)):
            intersection = [max(box[0], old_box[0]), max(box[1], old_box[1]), min(box[2], old_box[2]), min(box[3], old_box[3])]
            if box_area(box) and box_area(intersection) / box_area(box) >= 0.95:
                reasons.append("SOURCE_PAGE_REGION_ALREADY_COVERED")
        existing_shas = {old.get("image_sha256"), old.get("original_sha256")}
        for art in candidate.get("artifacts", []):
            if art.get("sha256") and art["sha256"] in existing_shas:
                reasons.append("EXACT_IMAGE_BYTES")
        if reasons:
            matches.append({"inventory_key": old["key"], "basis": sorted(set(reasons))})
    return matches


def candidate_group(candidate: dict) -> str:
    """Group same-page caption fragments without merging their geometry."""
    label = candidate.get("figure_label") or ""
    match = re.search(r"\d+(?:[.]\d+)*", label)
    suffix = match.group(0) if match else candidate["figure_id"]
    return "GROUP-" + hashlib.sha256(
        f"{candidate['source_id']}|{candidate.get('page_id')}|{suffix}".encode()
    ).hexdigest()[:16]


def screening_disposition(candidate: dict, scope: str) -> str:
    """Discovery disposition is not a source-verified scientific assertion."""
    if candidate.get("inventory_matches"):
        return "EXISTING_INVENTORY_REGION"
    if candidate["association_basis"] != "FIGURE_CAPTION":
        return "PAGE_CONTEXT_ONLY_NO_OBJECT_ASSOCIATION"
    if scope == "GENERAL_METHOD":
        return "GENERAL_METHOD_DISCOVERY_ONLY"
    if scope.startswith("OTHER") or scope == "NON_VKM_ANALOG":
        return "OTHER_SITE_OR_ANALOG_DISCOVERY_ONLY"
    # Mixed SKRU1 register scope and a page mentioning SKRU1 do not prove
    # that every object on that page belongs to the mine.
    return "REGIONAL_OR_MIXED_SCOPE_DISCOVERY_ONLY"


def finalize_existing(out: Path, repo: Path, inventory_path: Path, reviews_path: Path) -> dict:
    """Append review dispositions; never overwrite the initial screening receipt."""
    assert_private_output(out, repo)
    candidates_path, coverage_path = out / "candidates.json", out / "coverage.json"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    rows = json.loads(coverage_path.read_text(encoding="utf-8"))
    validate_coverage(rows, {r["source_id"] for r in rows})
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
    review_ids = [r["candidate_id"] for r in reviews]
    if len(review_ids) != len(set(review_ids)):
        raise ValueError("duplicate visual review candidate")
    lookup = {r["candidate_id"]: r for r in reviews}
    candidate_ids = {c["candidate_id"] for c in candidates}
    if not set(lookup).issubset(candidate_ids):
        raise ValueError("visual review must refer to an existing candidate")
    scopes = {r["source_id"]: r["scope_from_register"] for r in rows}
    dispositions = []
    for c in candidates:
        matched = inventory_matches(c, inventory)
        scope = scopes[c["source_id"]]
        rec = {"candidate_id": c["candidate_id"], "source_id": c["source_id"],
               "page_id": c["page_id"], "source_sha256": c["source_sha256"],
               "figure_group_id": candidate_group(c), "inventory_matches": matched,
               "screening_disposition": screening_disposition({**c, "inventory_matches": matched}, scope),
               "scope_from_register": scope, "scientific_data_acceptance": "NOT_ACCEPTED_BY_SCREENING",
               "astra_review_required": False, "human_review": "NOT_REVIEWED",
               "reason": "Local discovery rules do not verify figure semantics, values, attribution or exhaustive recall."}
        if c["candidate_id"] in lookup:
            r = lookup[c["candidate_id"]]
            for k in ("full_image_path", "crop_path"):
                p = (out / r[k]).resolve()
                if not p.is_relative_to(out.resolve()) or digest(p) != r[k.replace("path", "sha256")]:
                    raise ValueError("review image provenance does not match")
            rec.update({"human_review": "SOL_VISUAL_ROLE_CHECK", "visual_review": r,
                        "screening_disposition": r["screening_disposition"], "reason": r["reason"]})
        dispositions.append(rec)
    target = out / "candidate_dispositions.json"
    write_json(target, dispositions)
    result = {"status": "PASS_SCREENING_WITH_EXPLICIT_DISPOSITIONS", "sources": len(rows),
              "pages_screened": sum(r["pages_checked"] for r in rows), "candidate_dispositions": len(dispositions),
              "visually_role_checked_candidates": len(reviews),
              "new_explicit_candidates_after_dedup": sum(c["association_basis"] == "FIGURE_CAPTION" and not inventory_matches(c, inventory) for c in candidates),
              "disposition_counts": dict(Counter(r["screening_disposition"] for r in dispositions)),
              "human_full_read": False, "exhaustive_figure_recall_proven": False,
              "astra_cases_created": 0, "initial_screening_receipt_preserved": True,
              "inputs": {"candidates": digest(candidates_path), "coverage": digest(coverage_path),
                         "inventory": digest(inventory_path), "visual_reviews": digest(reviews_path)},
              "code_sha256": digest(Path(__file__)), "outputs": {target.name: digest(target)}}
    write_json(out / "screening_finalization_receipt.json", result)
    return result


def materialize_priority(candidates, rows, page_audit, resources, out, fitz, limit):
    """Context and exact crops for a bounded high-value human review, no model call."""
    row_map = {r["source_id"]: r for r in rows}
    pages = {p["page_id"]: p for p in page_audit}
    selected = []
    for c in candidates:
        if c["association_basis"] != "FIGURE_CAPTION" or c.get("inventory_matches") or not c.get("page_index") or Path(c["source_path"]).suffix.lower() != ".pdf":
            continue
        scope = row_map[c["source_id"]]["scope_from_register"]
        site_page = pages.get(c["page_id"], {}).get("site_mention", False)
        if site_page or scope == "SKRU1":
            c["selection_priority"] = 0
        elif "VKM" in scope and not scope.startswith("OTHER") and any(t in c["tags"] for t in ("GEOLOGICAL_SECTION", "GEOLOGY_MAP", "BOREHOLE")):
            c["selection_priority"] = 1
        elif "VKM" in scope and not scope.startswith("OTHER") and "MINE_PLAN" in c["tags"]:
            c["selection_priority"] = 2
        else:
            continue
        selected.append(c)
    counts, manifests = Counter(), []
    for c in sorted(selected, key=lambda c: (c["selection_priority"], c["known_v2_page"], c["source_id"], c["page_index"], c["candidate_id"])):
        if len(manifests) >= limit:
            break
        if counts[c["source_id"]] >= 4 and c["selection_priority"]:
            continue
        src = safe_source(resources, c["source_path"])
        if digest(src) != c["source_sha256"]:
            raise ValueError("source changed before priority rendering")
        with fitz.open(src) as doc:
            page = doc[c["page_index"] - 1]
            box = c["bbox_page"]
            clip = fitz.Rect(box) & page.rect
            if clip.is_empty:
                continue
            full = out / "priority_images" / (c["page_id"].replace(":", "_") + "_full.png")
            crop = out / "priority_images" / (c["candidate_id"] + "_crop.png")
            full.parent.mkdir(parents=True, exist_ok=True)
            if not full.exists():
                page.get_pixmap(matrix=fitz.Matrix(120 / 72, 120 / 72), alpha=False).save(full)
            scale = min(240 / 72, (8_000_000 / max(1, clip.width * clip.height)) ** 0.5)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
            pix.save(crop)
            native = out / "priority_native" / (c["candidate_id"] + ".json")
            write_json(native, {"source_id": c["source_id"], "page_id": c["page_id"], "source_sha256": c["source_sha256"],
                                "bbox_pt_tl": list(clip), "words": page.get_text("words", clip=clip * page.derotation_matrix),
                                "vector_paths": len(page.get_drawings()), "verification_status": "AUTO_EXTRACTED_UNREVIEWED"})
            manifests.append({**c, "scope_from_register": row_map[c["source_id"]]["scope_from_register"],
                              "site_mention_on_page_is_not_attribution": pages.get(c["page_id"], {}).get("site_mention", False),
                              "full_image_path": full.relative_to(out).as_posix(), "full_image_sha256": digest(full),
                              "crop_path": crop.relative_to(out).as_posix(), "crop_sha256": digest(crop),
                              "native_words_path": native.relative_to(out).as_posix(), "native_words_sha256": digest(native),
                              "crop_size_px": [pix.width, pix.height], "image_to_page": [1 / scale, 0, 0, 1 / scale, clip.x0, clip.y0],
                              "render_dpi": 72 * scale, "human_review": "PENDING"})
            counts[c["source_id"]] += 1
    write_json(out / "priority_new_objects.json", manifests)
    return manifests


def read_rows(connection, sql: str) -> list[dict]:
    result = connection.execute(sql)
    names = [c[0] for c in result.description]
    return [dict(zip(names, values)) for values in result.fetchall()]


def source_status(row: dict) -> str:
    if row["canonical_rollup"] == "SKIPPED_BY_REGISTER":
        return "EXCLUDED_BY_REGISTER"
    if row["source_sha_status"] != "MATCH":
        return "UNRESOLVED_SOURCE_INTEGRITY"
    if not row["canonical_pages"]:
        return "UNRESOLVED_CANONICAL_PAGES"
    if row["canonical_rollup"] != "COMPLETE" or row["textless_pages"] or row.get("native_errors") or "NATIVE_CANONICAL_PAGECOUNT_DIFFERENCE" in row.get("unresolved_reasons", []):
        return "SCREENED_WITH_GAPS"
    return "SCREENED_AUTOMATICALLY"


def validate_coverage(rows: list[dict], registered: set[str]) -> None:
    ids = [r["source_id"] for r in rows]
    if len(ids) != len(set(ids)) or set(ids) != registered:
        raise ValueError("coverage must contain exactly one row per registered source")
    for row in rows:
        if row["human_full_read"] or row["exhaustive_figure_recall_proven"]:
            raise ValueError("automatic screening cannot assert full human reading or recall")
        if row["source_sha_status"] != "MATCH" and row["status"].startswith("SCREENED"):
            raise ValueError("unverified source bytes cannot have screened status")
        if row["pages_checked"] != row["canonical_pages"]:
            raise ValueError("all existing canonical page records must be screened")


def screen(args) -> dict:
    import duckdb
    import pymupdf as fitz

    repo = Path(__file__).resolve().parents[2]
    out, resources = Path(args.out), Path(args.resources)
    assert_private_output(out, repo)
    out.mkdir(parents=True, exist_ok=True)
    register_path = resources / "00_registry/SOURCE_REGISTER.csv"
    registry = list(csv.DictReader(register_path.open(encoding="utf-8-sig")))
    con = duckdb.connect(str(args.nav), read_only=True)
    summaries = {r["source_id"]: r for r in read_rows(con, "select * from source_status_summary")}
    pages_by_source = defaultdict(list)
    for row in read_rows(con, "select source_id,page_id,page_index,normalized_text,text_sha256,char_count,page_status,primary_text_origin,quality_flags,width_pt,height_pt from pages order by source_id,page_index"):
        pages_by_source[row["source_id"]].append(row)
    figures_by_source = defaultdict(list)
    for row in read_rows(con, "select object_id,source_id,page_id,caption,figure_label,layout_class,bbox_x0,bbox_y0,bbox_x1,bbox_y1,image_artifact_id,embedded_image_artifact_id,vector_artifacts,review_status from figures order by source_id,page_id,object_id"):
        figures_by_source[row["source_id"]].append(row)
    artifacts = {r["artifact_id"]: r for r in read_rows(con, "select artifact_id,storage_relpath,materialization,artifact_kind from artifacts")}
    known_ids, known_pages, inventory_counts = known_inventory(Path(args.inventory))
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    artifact_root = Path(args.artifacts) if args.artifacts else None
    rows, candidates, page_audit, structural = [], [], [], []
    for index, registered in enumerate(registry, 1):
        sid = registered["resource_id"]
        source = safe_source(resources, registered["canonical_path"])
        observed = digest(source) if source.is_file() else None
        summary = summaries.get(sid, {})
        page_rows = pages_by_source[sid]
        by_page = {p["page_id"]: p for p in page_rows}
        row = {"source_id": sid, "canonical_path": registered["canonical_path"],
               "registered_sha256": registered["sha256"], "observed_sha256": observed,
               "source_sha_status": "MATCH" if observed == registered["sha256"] else ("MISSING" if observed is None else "MISMATCH"),
               "format": source.suffix.lower().lstrip("."), "available": source.is_file(),
               "canonical_rollup": summary.get("source_rollup", "UNKNOWN"),
               "canonical_expected_pages": summary.get("page_count"), "canonical_pages": len(page_rows),
               "pages_checked": len(page_rows), "page_ranges_checked": compact_ranges([p["page_index"] for p in page_rows]),
               "textless_pages": sum(not (p["normalized_text"] or "").strip() for p in page_rows),
               "canonical_figures_screened": len(figures_by_source[sid]), "existing_v2_objects": inventory_counts.get(sid, 0),
               "scope_from_register": registered.get("evidence_scope", "UNKNOWN"),
               "methods": ["REGISTER_SHA256", "CANONICAL_PAGE_TEXT_LOCAL_RULE_SCREEN", "CANONICAL_FIGURE_CAPTION_LOCAL_RULE_SCREEN"],
               "human_full_read": False, "exhaustive_figure_recall_proven": False,
               "native_page_count": None, "native_graphic_page_checks": 0, "native_errors": [], "unresolved_reasons": []}
        source_candidates = []
        for page in page_rows:
            text = page["normalized_text"] or ""
            hits = relevance(text)
            relevant_captions = [{"caption": caption, "tags": relevance(caption)} for caption in captions(text) if relevance(caption)]
            page_audit.append({"source_id": sid, "page_id": page["page_id"], "page_index": page["page_index"],
                               "text_sha256": page["text_sha256"], "chars": page["char_count"],
                               "status": page["page_status"], "quality_flags": page["quality_flags"],
                               "machine_tags": hits, "site_mention": bool(SITE.search(text)),
                               "relevant_captions": relevant_captions,
                               "screening_method": "LOCAL_TEXT_AND_CAPTION_RULES", "human_review": "NOT_REVIEWED"})
        for fig in figures_by_source[sid]:
            page = by_page.get(fig["page_id"], {})
            tags = relevance(fig["caption"] or "")
            # A whole page mention does not establish this figure's meaning.
            page_tags = relevance(page.get("normalized_text") or "")
            if not tags and not page_tags:
                continue
            explicit = bool(tags)
            if not explicit and not ("MINE_PLAN" in page_tags or "GEOLOGICAL_SECTION" in page_tags or "BOREHOLE" in page_tags):
                continue
            rec = {"candidate_id": "COV-" + hashlib.sha256(fig["object_id"].encode()).hexdigest()[:16],
                   "source_id": sid, "page_id": fig["page_id"], "page_index": page.get("page_index"),
                   "figure_id": fig["object_id"], "figure_label": fig["figure_label"], "caption": fig["caption"],
                   "source_path": registered["canonical_path"], "source_sha256": observed,
                   "bbox_page": [fig[k] for k in ("bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1")],
                   "layout_class": fig["layout_class"], "tags": tags or page_tags,
                   "association_basis": "FIGURE_CAPTION" if explicit else "PAGE_CONTEXT_ONLY_NOT_SEMANTIC_ASSOCIATION",
                   "relevance_status": "RELEVANT_CANDIDATE" if explicit else "CONTEXT_CANDIDATE_UNRESOLVED",
                   "review_status": "AUTO_EXTRACTED_UNREVIEWED", "known_v2_id": fig["object_id"] in known_ids,
                   "known_v2_page": fig["page_id"] in known_pages, "artifacts": []}
            for aid in [fig["image_artifact_id"], fig["embedded_image_artifact_id"], *[v["artifact_id"] for v in (fig["vector_artifacts"] or [])]]:
                if not aid or aid not in artifacts:
                    continue
                art = artifacts[aid]
                ap = artifact_root / art["storage_relpath"] if artifact_root else None
                rec["artifacts"].append({**art, "available_locally": bool(ap and ap.is_file()),
                                         "sha256": digest(ap) if ap and ap.is_file() else None})
            source_candidates.append(rec)
        native_check(source, row, source_candidates, page_rows, structural, registered, out, fitz, args)
        for c in source_candidates:
            c["inventory_matches"] = inventory_matches(c, inventory)
        row["graphic_candidates"] = len(source_candidates)
        row["relevance_tags"] = sorted({t for c in source_candidates for t in c["tags"]})
        row["new_explicit_candidates"] = sum(c["association_basis"] == "FIGURE_CAPTION" and not c["inventory_matches"] for c in source_candidates)
        if row["canonical_rollup"] == "SKIPPED_BY_REGISTER":
            row["unresolved_reasons"].append("REGISTER_EXCLUDED_RETIRED_OR_SUPERSEDED_NOT_REIMPORTED")
        if row["textless_pages"]:
            row["unresolved_reasons"].append("TEXTLESS_CANONICAL_PAGES_REQUIRE_VISUAL_OR_NATIVE_CHECK")
        if row["canonical_rollup"] not in ("COMPLETE", "SKIPPED_BY_REGISTER"):
            row["unresolved_reasons"].append("CANONICAL_EXTRACTION_PARTIAL")
        if row["native_errors"]:
            row["unresolved_reasons"].append("NATIVE_INSPECTION_ERROR")
        row["status"] = source_status(row)
        rows.append(row)
        candidates.extend(source_candidates)
        if index % 20 == 0 or index == len(registry):
            print(f"screened {index}/{len(registry)} sources; {len(candidates)} graphic candidates", flush=True)
    con.close()
    validate_coverage(rows, {r["resource_id"] for r in registry})
    candidates.sort(key=lambda c: (c["source_id"], c.get("page_id") or "", c["candidate_id"]))
    materialized = materialize_priority(candidates, rows, page_audit, resources, out, fitz, args.materialize_limit)
    write_json(out / "coverage.json", rows)
    write_json(out / "candidates.json", candidates)
    write_json(out / "page_screening.json", page_audit)
    write_json(out / "native_structural_checks.json", structural)
    new = [c for c in candidates if c["association_basis"] == "FIGURE_CAPTION" and not c["inventory_matches"]]
    write_json(out / "new_relevant_candidates.json", new)
    keys = sorted({k for row in rows for k in row})
    with (out / "coverage.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in r.items()})
    outputs = {p.name: digest(p) for p in sorted(out.glob("*.json")) if p.name != "receipt.json"}
    outputs["coverage.csv"] = digest(out / "coverage.csv")
    receipt = {"status": "PASS_EXPLICIT_SCREENING_ALL_REGISTERED_SOURCES", "snapshot": SNAPSHOT,
               "sources": len(rows), "pages_screened": len(page_audit), "canonical_figures_screened": sum(r["canonical_figures_screened"] for r in rows),
               "candidates": len(candidates), "new_explicit_candidates": len(new), "status_counts": dict(Counter(r["status"] for r in rows)),
               "priority_materialized_objects": len(materialized),
               "human_full_read": False, "exhaustive_figure_recall_proven": False, "gpu_runs": 0,
               "source_sha_counts": dict(Counter(r["source_sha_status"] for r in rows)),
               "register_sha256": digest(register_path), "nav_sha256": digest(Path(args.nav)),
               "inventory_sha256": digest(Path(args.inventory)), "code_sha256": digest(Path(__file__)), "outputs": outputs}
    write_json(out / "receipt.json", receipt)
    return receipt


def compact_ranges(values: list[int]) -> list[list[int]]:
    ranges = []
    for value in sorted(set(v for v in values if v is not None)):
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ranges


def native_check(source, row, candidates, pages, structural, registered, out, fitz, args):
    """Inspect native containers and candidate page geometry without OCR inference."""
    sid = registered["resource_id"]
    if row["source_sha_status"] != "MATCH" or row["canonical_rollup"] == "SKIPPED_BY_REGISTER":
        return
    try:
        if source.suffix.lower() == ".pdf":
            with fitz.open(source) as doc:
                row["native_page_count"] = len(doc)
                row["methods"].append("NATIVE_PDF_PAGECOUNT_AND_CANDIDATE_STRUCTURE")
                page_ids = {p["page_index"]: p["page_id"] for p in pages}
                selected = {c["page_index"] for c in candidates if c.get("page_index")}
                selected |= {p["page_index"] for p in pages if not (p["normalized_text"] or "").strip()}
                for pn in sorted(selected):
                    if not 1 <= pn <= len(doc):
                        row["native_errors"].append({"page_index": pn, "error": "PAGE_INDEX_OUT_OF_RANGE"})
                        continue
                    page = doc[pn - 1]
                    drawings = page.get_drawings()
                    images = page.get_images(full=True)
                    native = page.get_text()
                    relevant = [{"caption": c, "tags": relevance(c)} for c in captions(native) if relevance(c)]
                    structural.append({"source_id": sid, "page_id": page_ids.get(pn), "page_index": pn,
                                       "native_text_chars": len(native), "native_text_sha256": hashlib.sha256(native.encode()).hexdigest(),
                                       "vector_paths": len(drawings), "embedded_images": len(images), "relevant_native_captions": relevant,
                                       "method": "NATIVE_CONTAINER_NO_MODEL", "source_sha256": row["observed_sha256"]})
                    row["native_graphic_page_checks"] += 1
        elif source.suffix.lower() in (".docx", ".epub"):
            row["methods"].append("NATIVE_ZIP_XML_MEDIA_AND_CAPTION_SCREEN")
            with zipfile.ZipFile(source) as archive:
                for part in sorted(archive.namelist()):
                    if not ((source.suffix.lower() == ".docx" and part.startswith("word/") and part.endswith(".xml")) or (source.suffix.lower() == ".epub" and part.endswith((".xhtml", ".html")))):
                        continue
                    blob = archive.read(part)
                    try:
                        root = ET.fromstring(blob)
                    except ET.ParseError:
                        continue
                    text = "\n".join(t for t in root.itertext() if t.strip())
                    tags = relevance(text)
                    if tags:
                        structural.append({"source_id": sid, "page_id": None, "native_part": part,
                                           "native_part_sha256": hashlib.sha256(blob).hexdigest(), "tags": tags,
                                           "native_text_chars": len(text), "relevant_native_captions": [c for c in captions(text) if relevance(c)],
                                           "pagination": "UNKNOWN_NATIVE_PART_LOCATOR", "method": "NATIVE_XML_NO_MODEL"})
                row["native_embedded_media"] = sum(p.startswith("word/media/") for p in archive.namelist())
        elif source.suffix.lower() == ".djvu":
            result = subprocess.run(["djvused", str(source), "-e", "n"], capture_output=True, text=True, timeout=30)
            if result.returncode:
                row["native_errors"].append({"error": "DJVUSED_PAGECOUNT_FAILED", "exit_code": result.returncode})
            else:
                row["native_page_count"] = int(result.stdout.strip())
                row["methods"].append("DJVULIBRE_NATIVE_PAGECOUNT")
        if row["native_page_count"] is not None and row["native_page_count"] != row["canonical_expected_pages"]:
            row["unresolved_reasons"].append("NATIVE_CANONICAL_PAGECOUNT_DIFFERENCE")
    except Exception as exc:  # recorded without source text or paths in stdout
        row["native_errors"].append({"error": type(exc).__name__})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--resources", default=None)
    p.add_argument("--nav", required=True)
    p.add_argument("--artifacts")
    p.add_argument("--inventory", default="work/figure_readings_2026-09-29/v2/inventory.json")
    p.add_argument("--out", default="work/abc_completion_2026-09-30/coverage")
    p.add_argument("--materialize-limit", type=int, default=48)
    p.add_argument("--finalize-from-existing", action="store_true")
    p.add_argument("--visual-reviews")
    args = p.parse_args()
    if args.finalize_from_existing:
        if not args.visual_reviews:
            p.error("finalization requires --visual-reviews")
        result = finalize_existing(Path(args.out), Path(__file__).resolve().parents[2], Path(args.inventory), Path(args.visual_reviews))
        print(json.dumps(result, ensure_ascii=False))
        return
    if not args.resources:
        import os
        args.resources = os.environ.get("VKM_RESOURCES_ROOT")
    if not args.resources:
        p.error("provide --resources or VKM_RESOURCES_ROOT")
    result = screen(args)
    print(json.dumps({k: result[k] for k in ("status", "sources", "pages_screened", "candidates", "new_explicit_candidates", "status_counts")}))


if __name__ == "__main__":
    main()
