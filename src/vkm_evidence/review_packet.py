"""Bounded source-backed review material; producing a packet is not a review."""
from __future__ import annotations

from urllib.parse import quote

from vkm_corpus.api.canon import kind_of
from vkm_corpus.ids import grammar
from vkm_evidence.contracts import canonical_bytes, record_hash
from vkm_evidence.objects import canonical_locator, canonical_resolver, canonical_text
from vkm_evidence.validation import semantic_references, version_references


class ReviewPacketLimit(ValueError):
    """An exact record/ref list cannot be silently truncated to fit a packet."""


def _supports(record):
    for index, ref in enumerate(record.supports):
        yield f"supports/{index}", ref
    for index, binding in enumerate(getattr(record, "symbols", ())):
        for support_index, ref in enumerate(binding.supports):
            yield f"symbols/{index}/supports/{support_index}", ref


def _clip(value, limit):
    if value is None:
        return {"text": None, "chars_total": 0, "truncated": False}
    if not isinstance(value, str):
        raise ValueError("canonical textual field is not a string")
    return {"text": value[:limit], "chars_total": len(value), "truncated": len(value) > limit}


class _PinnedCanon:
    def __init__(self, canon):
        self.canon, self.identity, self.rows = canon, canon.snapshot(), {}
        if not self.identity.snapshot_id or not grammar.matches("sha256", self.identity.manifest_sha256):
            raise ValueError("canonical snapshot identity unavailable")

    def snapshot(self):
        if self.canon.snapshot() != self.identity:
            raise ValueError("canonical snapshot changed during review packet")
        return self.identity

    def snapshot_id(self):
        return self.snapshot().snapshot_id

    def row(self, kind, oid):
        self.snapshot()
        if (kind, oid) not in self.rows:
            self.rows[kind, oid] = self.canon.row(kind, oid)
        self.snapshot()
        return self.rows[kind, oid]


def _original(ref, row, kind, max_chars, max_cells):
    """Whitelisted canonical metadata and explicitly bounded payload previews."""
    oid = quote(ref.object_id, safe="")
    native = row.get("region_origin") == "DOCX_ELEMENT"
    locator_kind = ("NATIVE_XML" if native else "RAW_ARTIFACT_POINTER" if row.get("raw_locator")
                    else "PAGE" if row.get("page_id") else "UNAVAILABLE")
    if locator_kind == "UNAVAILABLE":
        raise ValueError("original locator unavailable")
    locator = {"kind": locator_kind, "value": canonical_locator(row),
               "page_id": row.get("page_id"), "docx_paragraph_path": row.get("docx_paragraph_path")}
    metadata_fields = ("object_kind", "source_id", "source_sha256", "page_id", "page_index", "page_kind",
        "page_status", "printed_label", "printed_label_status", "region_origin", "origin", "text_layer",
        "extractor_id", "extractor_version", "extraction_generation", "extraction_signature", "config_hash",
        "content_sha256", "review_status", "width_pt", "height_pt", "native_dpi", "image_dpi", "n_rows", "n_cols",
        "header_rows", "recognition_method", "raw_format", "formula_kind", "latex_parse_ok", "detected_figure_type")
    metadata = {key: row[key] for key in metadata_fields if key in row}
    geometry = {"bbox_space": row.get("bbox_space", "NONE"), "bbox": None,
                "unit": "UNKNOWN", "render_dependent": native}
    box = [row.get("bbox_" + name) for name in ("x0", "y0", "x1", "y1")]
    if any(x is not None for x in box):
        if any(x is None for x in box) or geometry["bbox_space"] == "NONE":
            raise ValueError("incomplete canonical bounding box")
        geometry["bbox"] = box
        geometry["unit"] = "pt" if geometry["bbox_space"] == "PAGE_PT_TL" else "UNKNOWN"
    field, text = canonical_text(row, kind)
    if ref.char_start is None:
        excerpt = {"field": field, "char_start": 0, **_clip(text, max_chars)}
        fragment = {"status": "NOT_REQUESTED", "representation": field}
    else:
        # canonical_resolver has already checked boundaries and hash against this
        # exact representation. Never substitute a normalized sibling field.
        excerpt = {"field": field, "char_start": ref.char_start,
                   **_clip(text[ref.char_start:ref.char_end], max_chars)}
        fragment = {"status": "VERIFIED", "representation": field, "char_start": ref.char_start,
                    "char_end": ref.char_end, "sha256": ref.fragment_sha256}
    previews = {key: _clip(row[key], max_chars) for key in
                ("caption", "figure_label", "table_label", "equation_label", "normalized_latex", "native_glyph_text")
                if row.get(key) is not None}
    warnings = ["CANONICAL_EXTRACTION_IS_NOT_ORIGINAL_BYTE_INSPECTION"]
    if kind == "PAGE":
        warnings.append("PAGE_TEXT_IS_NORMALIZED_CANONICAL_TEXT")
    if native:
        warnings.append("NATIVE_XML_LOCATION_IS_NOT_A_PHYSICAL_PAGE_OR_GEOREFERENCE")
    if row.get("origin") == "OCR":
        warnings.append("OCR_REQUIRES_VISUAL_ORIGINAL_COMPARISON")
    images = []
    for key in ("preview_artifact_id", "render_artifact_id", "image_artifact_id", "embedded_image_artifact_id"):
        if artifact := row.get(key):
            if not grammar.matches("artifact", artifact):
                raise ValueError("invalid canonical image artifact identity")
            images.append({"role": key, "artifact_id": artifact, "sha256": artifact.split(":", 1)[1],
                "metadata_url": "/v1/artifact/" + quote(artifact, safe=""),
                "content_url": "/v1/artifact/" + quote(artifact, safe="") + "/content",
                "availability": "NOT_CHECKED", "bytes_verified": False})
    links = {"object_url": f"/v1/object/{oid}", "expected_snapshot_id": ref.snapshot_id,
             "expected_content_sha256": ref.content_sha256,
             "response_snapshot_check_required": True}
    if images:
        links["image_url"] = f"/v1/{'page' if kind == 'PAGE' else 'object'}/{oid}/image"
        links["image_snapshot_header"] = "X-VKM-Snapshot"
    table = None
    if kind == "TABLE":
        cells = row.get("cells") or []
        if not isinstance(cells, list):
            raise ValueError("canonical table cells are not a list")
        preview = []
        for cell in cells[:max_cells]:
            preview.append({key: cell[key] for key in ("row", "col", "row_span", "col_span", "is_header") if key in cell}
                           | {"text": _clip(cell.get("text"), max_chars)})
        truncated = len(cells) > max_cells or any(c["text"]["truncated"] for c in preview)
        table = {"cells_preview": preview, "canonical_cells_total": len(cells),
                 "preview_truncated": truncated, "native_table_completeness": "NOT_ESTABLISHED",
                 "complete_grid_navigation": {"start_url": f"/v1/nav/table/{oid}?max_rows=200&max_chars=8000",
                     "cursor_parameter": "cursor", "cursor_field": "pagination.next_cursor",
                     "completion_field": "pagination.has_more", "follow_until_has_more_false": True,
                     "expected_snapshot_id": ref.snapshot_id, "availability": "NOT_CHECKED",
                     "meaning": "DERIVED_NAV_GRID_NOT_ORIGINAL_TRANSCRIPTION"}}
        if truncated:
            warnings.append("TABLE_PREVIEW_TRUNCATED_USE_CURSOR_AND_ORIGINAL")
    if excerpt["truncated"] or any(v["truncated"] for v in previews.values()):
        warnings.append("TEXT_PREVIEW_TRUNCATED")
    return {"support": ref.model_dump(mode="json"), "identity_check": "VERIFIED_CANONICAL_METADATA",
            "locator": locator, "fragment_check": fragment, "metadata": metadata, "geometry": geometry,
            "excerpt": excerpt, "previews": previews, "table": table, "images": images,
            "links": links, "warnings": warnings}


def build_review_packet(reader, canon, record_id, context, *, max_supports=64, max_dependency_refs=512,
                        max_text_chars=12_000, max_table_cells=100, max_packet_bytes=2_000_000) -> dict:
    """Return a deterministic packet for the exact visible evidence generation.

    Source policy is mandatory. Ref lists and the evidence record are complete
    or the call fails; only presentation previews can be truncated. The hash is
    SHA-256 of canonical JSON excluding its top-level packet_sha256 field.
    No semantic decision, admission, extraction or artifact rendering is run.
    """
    limits = {"max_supports": (max_supports, 512), "max_dependency_refs": (max_dependency_refs, 4096),
              "max_text_chars": (max_text_chars, 60_000), "max_table_cells": (max_table_cells, 1000),
              "max_packet_bytes": (max_packet_bytes, 10_000_000)}
    if any(type(n) is not int or not 1 <= n <= cap for n, cap in limits.values()):
        raise ValueError("review packet limits out of range")
    if reader.source_policy is None:
        raise ValueError("review packet requires authoritative source policy")
    # Trusted module: never mutate cached models and serialize before returning.
    # Public view() deep-copies every record, which is unnecessary for a packet.
    generation, records = reader._view()
    metadata = reader.view_metadata(records, generation)
    visible = reader._visible(records, context, generation)
    if record_id not in visible:
        raise KeyError("record not found")
    target = visible[record_id]
    closure, edges, todo = {}, {}, [target.version_ref]
    while todo:
        ref = todo.pop()
        current = visible.get(ref.record_id)
        if current is None:
            raise KeyError("record not found")
        if current.version_ref != ref:
            raise ValueError("exact dependency version unavailable; review packet cannot retarget it")
        if ref.record_id in closure:
            continue
        closure[ref.record_id] = current
        refs = version_references(current)
        if not semantic_references(current).issubset({r.record_id for r in refs}):
            raise ValueError("semantic parent lacks exact dependency pin")
        for parent in refs:
            edge = {"from": ref.model_dump(mode="json"), "to": parent.model_dump(mode="json")}
            edges[record_hash(edge)] = edge
            if len(edges) > max_dependency_refs:
                raise ReviewPacketLimit("complete dependency references exceed packet limit")
        todo.extend(refs)
    all_supports, source_policies = {}, {}
    for rid, record in sorted(closure.items()):
        for path, ref in _supports(record):
            key = record_hash(ref)
            entry = all_supports.setdefault(key, {"ref": ref, "used_by": []})
            if len(all_supports) > max_supports:
                raise ReviewPacketLimit("complete original supports exceed packet limit")
            entry["used_by"].append({"record": record.version_ref.model_dump(mode="json"), "path": path})
            policy = reader.source_policy(ref.source_id)
            policy.require(context)  # BEFORE constructing resolver/looking up any original object.
            if not record.policy.preserves(policy):
                raise PermissionError("evidence policy widens original source access")
            source_policies[ref.source_id] = policy
    if not all_supports:
        raise ValueError("review packet has no original source support")
    pinned = _PinnedCanon(canon)
    if any(e["ref"].snapshot_id != pinned.snapshot_id() for e in all_supports.values()):
        raise ValueError("original snapshot is not available; no current-snapshot retargeting")

    def authorized_policy(sid):
        policy = reader.source_policy(sid)
        policy.require(context)
        if policy != source_policies[sid]:
            raise PermissionError("source policy changed during review packet")
        return policy

    resolve = canonical_resolver(pinned, authorized_policy)
    originals = []
    preview_bytes = 0
    for key, entry in sorted(all_supports.items()):
        ref = entry["ref"]
        resolve(ref)
        kind = kind_of(ref.object_id)
        original = _original(ref, pinned.row(kind, ref.object_id), kind, max_text_chars, max_table_cells)
        original.update({"support_sha256": key, "used_by": entry["used_by"]})
        preview_bytes += len(canonical_bytes(original))
        if preview_bytes > max_packet_bytes:
            raise ReviewPacketLimit("complete review packet exceeds byte limit; narrow previews, never drop support refs")
        originals.append(original)
        # Do not retain entire canonical table payloads after their preview.
        pinned.rows.clear()
    principal = context.model_dump(mode="json")
    principal["granted_classes"] = sorted(principal["granted_classes"])
    packet = {"schema": "vkm-evidence-review-packet/1", "generation": generation, **metadata,
        "record": target.model_dump(mode="json"), "record_sha256": record_hash(target),
        "snapshot_id": pinned.identity.snapshot_id, "manifest_sha256": pinned.identity.manifest_sha256,
        "context": principal, "source_policies": {sid: p.model_dump(mode="json") for sid, p in sorted(source_policies.items())},
        "original_supports": originals, "dependency_references": [edges[k] for k in sorted(edges)],
        "dependency_versions": [r.version_ref.model_dump(mode="json") for rid, r in sorted(closure.items()) if rid != record_id],
        "references_complete": True, "supports_complete": True,
        "units": {"quantity": getattr(target, "quantity", None).model_dump(mode="json") if getattr(target, "quantity", None) else None,
                  "symbols": [{"symbol": b.symbol, "unit": b.unit, "scope": b.scope} for b in getattr(target, "symbols", ())]},
        "interpretation_context": {"time": target.time.model_dump(mode="json"),
            "provenance": target.provenance.model_dump(mode="json") if hasattr(target, "provenance") else None},
        "limits": {name: value for name, (value, _) in limits.items()},
        "decision_binding": {"target": target.version_ref.model_dump(mode="json"),
            "original_supports": [entry["ref"].model_dump(mode="json") for _, entry in sorted(all_supports.items())]},
        "warnings": ["PACKET_CREATION_IS_NOT_SOURCE_INSPECTION_OR_SEMANTIC_REVIEW",
                     "ORIGINAL_IMAGE_BYTES_NOT_READ_OR_VERIFIED", "FOLLOWUP_URLS_REQUIRE_POLICY_AND_SNAPSHOT_CHECKS"],
        "review_state": "NOT_REVIEWED", "scientific_admission": "NOT_RUN", "status": "REVIEW_MATERIAL_PREPARED"}
    pinned.snapshot()
    reader._unchanged(metadata, records, generation)
    for sid in source_policies:
        authorized_policy(sid)
    packet["packet_sha256"] = record_hash(packet)
    if len(canonical_bytes(packet)) > max_packet_bytes:
        raise ReviewPacketLimit("complete review packet exceeds byte limit; narrow previews, never drop support refs")
    return packet
