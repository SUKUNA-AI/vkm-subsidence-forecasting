"""Page-image vector projection: the visual route's k-NN index (agent VIS; V2 ``RESULTS_V2.md`` §10).

Canon of the vectors — a derived *visual* artifact (``vkm_corpus.embeddings.artifacts``, kind ``visual``)::

    derived/embeddings/visual/<model>/<revision>/<config-hash>/
        config.json                 EmbeddingConfig: mode "visual", text rule ``vkm-page-preview-v1``, image input
        part-<writer>-NNNNN.parquet object_id = page id, text_hash = artifact_sha = sha256 of the stored PAGE_PREVIEW
        _manifest-<writer>.json     parts with rows and sha256 (written last)

One row per page with a stored preview (the JPEG, long side 1024 px, is the embedded input; its content address
``sha256:<hex>`` is the page's ``preview_artifact_id``, so a changed preview is a new text hash and is re-encoded, §46).
Pages without a preview (EPUB spine items) are not in the channel.

OpenSearch holds a rebuildable projection: ``<prefix>-pagevis-m1-<build>`` behind the alias ``<prefix>-pagevis`` — one
document per page: the page vector (``knn_vector``, inner product on L2-normalised vectors, HNSW lucene as the dense
index) plus the page-level filter fields of E's page documents (the hybrid filters apply unchanged) and
``dup_group_id`` (duplicate pages collapse as in the dense leg). Why OpenSearch and not an in-service memmap (the late
pack): one vector per page is exactly the dense index's shape (filters, alias swap, rollback, ``search status``), the
API already queries OpenSearch, and the RX580 service stays a pure query encoder for this channel; the pack exists
because MaxSim over token matrices has no OpenSearch equivalent.

* :func:`build_page_vectors` — refuses unless the artifact's rows equal the CURRENT snapshot's pages with a preview
  (same page ids, same preview hashes; the streamed §64 checks of the dense projection: checksums, one signature,
  no duplicates, no missing, finite unit vectors of the right size), then index, bulk ``create``, count and sampled
  vector checks, ``_meta`` COMPLETE, one atomic alias swap; old builds are deleted by exact name only (aliased +
  previous kept for rollback: :func:`rollback_page_vectors`). ``--plan-only`` checks without writing and can list the
  pages still to encode (``missing_out``) for the WORKSTATION encoder.
* :func:`page_vector_hits` — the visual leg at query time: **exact** inner product (``script_score`` + ``knn_score``
  over the filtered pages, the V2-measured scheme; ~26 k vectors) by default, or the HNSW graph (``mode="hnsw"``).
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from vkm_corpus.config import Settings
from vkm_corpus.embeddings.page_images import PAGE_IMAGE_RULE, preview_hex
from vkm_corpus.graph.common import (LOCKS_DIR, PROJECTOR_VERSION, FileLock, ProjectionError, Snapshot, load_snapshot,
                                     require_canonical_root, utc_now, write_receipt)
from vkm_corpus.search.mappings import COMMON, PER_TYPE, make_build_id
from vkm_corpus.search.query import compile_filters
from vkm_corpus.search.vectors import (BUILDING, COMPLETE, PAGE_FIELDS, VECTOR_FIELD, _bulk, alias_indices,
                                       check_artifacts, iter_selected_vectors, knn_field, read_config_dir)

PAGEVIS_MAPPING_VERSION = "1"
PAGEVIS_SUFFIX = "pagevis"
PAGE_SOURCE_FIELDS: tuple[str, ...] = ("id", "page_id", "source_id", "work_id", "page_index", "dup_group_id",
                                       "preview_artifact_id")
SEARCH_MODES = ("exact", "hnsw")
MAX_PAGE_K = 1000
_KW = {"type": "keyword"}
_NOT_INDEXED = {"type": "keyword", "index": False, "doc_values": False}


# ---------------------------------------------------------------- names
def pagevis_alias(prefix: str) -> str:
    return f"{prefix}-{PAGEVIS_SUFFIX}".lower()


def pagevis_index_name(prefix: str, build_id: str) -> str:
    return f"{prefix}-{PAGEVIS_SUFFIX}-m{PAGEVIS_MAPPING_VERSION}-{build_id}".lower()


def parse_pagevis_index(prefix: str, name: str) -> str | None:
    head = f"{prefix}-{PAGEVIS_SUFFIX}-m{PAGEVIS_MAPPING_VERSION}-".lower()
    return name[len(head):] if name.startswith(head) else None


# ---------------------------------------------------------------- expected pages of a snapshot
def page_documents(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    """page_id → E's page document fields needed here (filters, ids, preview) of the snapshot."""
    from vkm_corpus.graph.canon import ProjectionInput
    from vkm_corpus.search.documents import iter_documents

    keep = set(PAGE_FIELDS) | {"source_id", "preview_artifact_id"}
    inp = ProjectionInput.from_snapshot(snapshot)
    try:
        return {doc["page_id"]: {k: doc[k] for k in keep if k in doc} for doc in iter_documents(inp, "pages")}
    finally:
        inp.close()


def expected_pages(pages: dict[str, dict[str, Any]]) -> dict[str, str]:
    """page_id → preview hash of every page with a stored preview (the rows the artifact must hold)."""
    out = {}
    for pid, doc in pages.items():
        h = preview_hex(doc.get("preview_artifact_id"))
        if h:
            out[pid] = h
    return out


# ---------------------------------------------------------------- mapping
def page_vector_properties(config: Any) -> dict[str, Any]:
    import copy

    props: dict[str, Any] = {k: copy.deepcopy(COMMON[k]) for k in ("id", "source_id", "page_id", "preview_artifact_id",
                                                                  *PAGE_FIELDS) if k in COMMON}
    props.update({k: copy.deepcopy(PER_TYPE["pages"][k]) for k in PAGE_FIELDS if k in PER_TYPE["pages"]})
    props.update({"snapshot_id": _KW, "config_signature": _KW, "text_hash": _NOT_INDEXED,
                  VECTOR_FIELD: knn_field(config)})
    return props


def page_vectors_body(config: Any, meta: dict[str, Any]) -> dict[str, Any]:
    return {"settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0,
                                   "refresh_interval": "-1"}},
            "mappings": {"dynamic": "strict",
                         "_meta": {**meta, "vkm_pagevis_mapping_version": PAGEVIS_MAPPING_VERSION},
                         "properties": page_vector_properties(config)}}


def page_document(page_id: str, vector: Any, page: dict[str, Any], *, text_hash: str, snapshot_id: str,
                  config_signature: str) -> dict[str, Any]:
    doc = {**page, "id": page_id, "page_id": page_id, "snapshot_id": snapshot_id,
           "config_signature": config_signature, "text_hash": text_hash, VECTOR_FIELD: vector.tolist()}
    return {k: v for k, v in doc.items() if v is not None}


# ---------------------------------------------------------------- builds, status, rollback
def list_pagevis_builds(client: Any, prefix: str) -> list[dict[str, Any]]:
    mappings = client.indices.get_mapping(index=f"{prefix}-{PAGEVIS_SUFFIX}-*", params={"allow_no_indices": "true"})
    out = []
    for name, body in mappings.items():
        build_id = parse_pagevis_index(prefix, name)
        if build_id is not None:
            out.append({"index": name, "build_id": build_id, "meta": (body.get("mappings") or {}).get("_meta") or {}})
    return sorted(out, key=lambda b: b["build_id"])


def pagevis_status(client: Any, prefix: str) -> dict[str, Any]:
    """Alias → index → ``_meta`` of the page-vector projection (``search status``, section ``page_vectors``)."""
    alias = pagevis_alias(prefix)
    targets = alias_indices(client, alias)
    builds = list_pagevis_builds(client, prefix)
    entry: dict[str, Any] = {"alias": alias, "indices": targets, "builds": [b["build_id"] for b in builds]}
    if targets:
        meta = next((b["meta"] for b in builds if b["index"] == targets[0]), {})
        entry.update({k: meta.get(k) for k in ("build_id", "built_from_snapshot_id", "config_signature", "model_id",
                                               "model_revision", "model_key", "quantization", "dimension",
                                               "space_type", "text_rule", "build_status", "vector_count",
                                               "expected_sha256")})
        entry["count"] = int(client.count(index=alias)["count"])
    return entry


def pagevis_meta(client: Any, prefix: str) -> dict[str, Any]:
    """``_meta`` of the aliased COMPLETE build (+ index, alias); LookupError when there is none."""
    alias = pagevis_alias(prefix)
    targets = alias_indices(client, alias)
    if len(targets) != 1:
        raise LookupError(f"page-vector alias {alias} is not built yet (search build-page-vectors)")
    meta = next((b["meta"] for b in list_pagevis_builds(client, prefix) if b["index"] == targets[0]), {})
    if meta.get("build_status") != COMPLETE or not meta.get("dimension"):
        raise LookupError(f"the aliased page-vector build {targets[0]} is not COMPLETE")
    return {**meta, "index": targets[0], "alias": alias}


def prune_pagevis_builds(client: Any, prefix: str, keep: int = 2) -> list[str]:
    current = set(alias_indices(client, pagevis_alias(prefix)))
    builds = list_pagevis_builds(client, prefix)
    newest = max((parse_pagevis_index(prefix, c) or "" for c in current), default=None)
    older_complete = [b["index"] for b in builds if b["index"] not in current
                      and b["meta"].get("build_status") == COMPLETE]
    kept = current | (set(older_complete[-(keep - 1):]) if keep > 1 else set())
    deleted = []
    for b in builds:
        if b["index"] in kept or (newest is not None and b["build_id"] > newest):
            continue
        client.indices.delete(index=b["index"])
        deleted.append(b["index"])
    return deleted


def rollback_page_vectors(client: Any, prefix: str) -> dict[str, Any]:
    """Move the page-vector alias to the previous COMPLETE build."""
    alias = pagevis_alias(prefix)
    current = alias_indices(client, alias)
    current_build = parse_pagevis_index(prefix, current[0]) if current else None
    older = [b for b in list_pagevis_builds(client, prefix) if b["meta"].get("build_status") == COMPLETE
             and (current_build is None or b["build_id"] < current_build)]
    if not older:
        raise ProjectionError("E_NO_PREVIOUS_BUILD", "page vectors: no earlier COMPLETE build to roll back to",
                              stage="rollback")
    actions = [{"remove": {"index": old, "alias": alias}} for old in current]
    actions.append({"add": {"index": older[-1]["index"], "alias": alias}})
    client.indices.update_aliases(body={"actions": actions})
    return {"target": older[-1]["index"], "actions": actions}


# ---------------------------------------------------------------- build
@dataclass
class PageVectorBuildOptions:
    embeddings: str                          # visual artifact config directory (or {"artifact_dir": …} JSON)
    snapshot_id: str | None = None           # assertion: must equal CURRENT
    prefix: str | None = None
    plan_only: bool = False
    missing_out: str | None = None           # plan: JSON of the pages still to encode (page, source, preview id)
    keep_failed: bool = False
    keep_builds: int = 2
    chunk_docs: int = 200
    sample: int = 32
    verify_checksums: bool = True
    skip_if_current: bool = False
    command: str = "search build-page-vectors"


def _resolve(path: str, root: Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    if p.is_file():
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not (isinstance(raw, dict) and raw.get("artifact_dir")):
            raise ProjectionError("E_PREFLIGHT", "page-vector JSON must name artifact_dir", stage="plan")
        p = Path(raw["artifact_dir"])
        p = p if p.is_absolute() else root / p
    if not p.is_dir():
        raise ProjectionError("E_PREFLIGHT", "page-vector artifact directory does not exist", stage="plan")
    return p


def _expected_sha(expected: dict[str, str]) -> str:
    return hashlib.sha256("".join(f"{k}\t{v}\n" for k, v in sorted(expected.items())).encode()).hexdigest()


def model_key_of_visual(config: Any) -> str | None:
    try:
        from vkm_corpus.embeddings.specs import SPECS
    except Exception:  # noqa: BLE001
        return None
    keys = [k for k, s in SPECS.items() if s.model_id == config.model_id and s.model_revision == config.model_revision
            and s.family == "visual"]
    return keys[0] if len(keys) == 1 else None


def build_page_vectors(settings: Settings, options: PageVectorBuildOptions, *, client: Any = None,
                       pages: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Checks + build + alias swap of the page-vector projection; returns the receipt (also written under
    receipts/projections/opensearch)."""
    from vkm_corpus.search.indexer import check_prefix

    prefix = check_prefix(options.prefix or settings.opensearch_index_prefix)
    root = require_canonical_root(settings)
    t0 = time.monotonic()
    receipt: dict[str, Any] = {"engine": "opensearch", "kind": "page_vectors", "prefix": prefix,
                               "command": options.command, "pagevis_mapping_version": PAGEVIS_MAPPING_VERSION,
                               "projector_version": PROJECTOR_VERSION, "started_at": utc_now(), "status": "PLANNED"}
    created: list[str] = []
    with FileLock(root / LOCKS_DIR / f"opensearch-pagevis-{prefix}.lock"):
        try:
            snapshot = load_snapshot(root, None)
            receipt["current_snapshot_id"] = snapshot.snapshot_id
            if options.snapshot_id and options.snapshot_id != snapshot.snapshot_id:
                raise ProjectionError("E_REFUSED", f"--snapshot {options.snapshot_id} is not CURRENT "
                                      f"({snapshot.snapshot_id}); page vectors are built only for the current snapshot",
                                      stage="plan")
            art_dir = _resolve(options.embeddings, root)
            kind, config = read_config_dir(art_dir)
            if kind != "visual" or config.mode != "visual":
                raise ProjectionError("E_MODE_UNSUPPORTED", f"artifact kind {kind!r}: only visual (page-image) "
                                      "vectors are projected here", stage="plan")
            if config.text_rule != PAGE_IMAGE_RULE:
                raise ProjectionError("E_REFUSED", f"artifact input rule {config.text_rule} != {PAGE_IMAGE_RULE}",
                                      stage="plan")
            sig = config.signature()
            page_docs = pages if pages is not None else page_documents(snapshot)
            expected = expected_pages(page_docs)
            receipt["embeddings"] = {"config_signature": sig, "model_id": config.model_id,
                                     "model_revision": config.model_revision, "quantization": config.quantization,
                                     "dimension": config.dimension, "normalization": config.normalization,
                                     "storage_precision": config.storage_precision, "text_rule": config.text_rule,
                                     "model_key": model_key_of_visual(config), "image": dict(config.image)}
            receipt["pages"] = {"snapshot_pages": len(page_docs), "with_preview": len(expected),
                                "without_preview": len(page_docs) - len(expected),
                                "expected_sha256": _expected_sha(expected)}
            receipt["mapping"] = {VECTOR_FIELD: knn_field(config)}
            if options.skip_if_current and not options.plan_only:
                if client is None:
                    from vkm_corpus.search.client import connect

                    client = connect(settings)
                cur = pagevis_status(client, prefix)
                if cur.get("build_status") == COMPLETE and cur.get("built_from_snapshot_id") == snapshot.snapshot_id \
                        and cur.get("config_signature") == sig and cur.get("expected_sha256") == \
                        receipt["pages"]["expected_sha256"]:
                    receipt.update({"status": "SKIPPED_CURRENT", "build_id": cur.get("build_id"),
                                    "index": (cur.get("indices") or [None])[0], "page_vectors": cur})
                    return receipt
            report, selection = check_artifacts(art_dir, config, expected, verify_checksums=options.verify_checksums)
            receipt["checks_64"] = report.as_dict()
            if options.missing_out:
                todo = sorted(set(report.missing) | set(report.bad_vectors))
                rows = [{"page_id": pid, "source_id": page_docs[pid].get("source_id"),
                         "preview_artifact_id": page_docs[pid].get("preview_artifact_id")} for pid in todo]
                out = Path(options.missing_out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps({"schema": "vkm.page_vectors_todo/1", "snapshot_id": snapshot.snapshot_id,
                                           "config_signature": sig, "pages": rows}, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                receipt["missing_out"] = {"pages": len(rows)}
            if not report.ok:
                raise ProjectionError("E_CHECK_FAILED", "pre-import checks (§64) failed; nothing was imported",
                                      stage="verify", details=report.as_dict())
            if options.plan_only:
                receipt["status"] = "PLAN_ONLY"
                return receipt
            if client is None:
                from vkm_corpus.search.client import connect

                client = connect(settings)
            build_id = f"{make_build_id(snapshot.manifest_sha256)}-{sig[:8]}"
            taken = {b["build_id"] for b in list_pagevis_builds(client, prefix)}
            base, n = build_id, 1
            while build_id in taken:
                n += 1
                build_id = f"{base}-{n}"
            index = pagevis_index_name(prefix, build_id)
            receipt.update({"build_id": build_id, "index": index})
            manifests_sha = hashlib.sha256("".join(
                hashlib.sha256(m.read_bytes()).hexdigest() for m in sorted(art_dir.glob("_manifest-*.json"))
            ).encode()).hexdigest()
            meta = {"build_id": build_id, "built_from_snapshot_id": snapshot.snapshot_id,
                    "canonical_manifest_sha256": snapshot.manifest_sha256, "config_signature": sig,
                    "model_id": config.model_id, "model_revision": config.model_revision,
                    "model_key": model_key_of_visual(config), "quantization": config.quantization,
                    "dimension": config.dimension, "normalization": config.normalization,
                    "space_type": knn_field(config)["method"]["space_type"],
                    "storage_precision": config.storage_precision, "text_rule": config.text_rule,
                    "expected_sha256": receipt["pages"]["expected_sha256"],
                    "artifact_manifests_sha256": manifests_sha, "projector_version": PROJECTOR_VERSION,
                    "prefix": prefix, "build_status": BUILDING}
            client.indices.create(index=index, body=page_vectors_body(config, meta))
            created.append(index)
            started = time.monotonic()
            rng = random.Random(build_id)
            sample: dict[str, list[float]] = {}

            def docs() -> Iterator[dict[str, Any]]:
                for pid, vec in iter_selected_vectors(art_dir, selection, config.storage_precision):
                    doc = page_document(pid, vec, page_docs[pid], text_hash=expected[pid],
                                        snapshot_id=snapshot.snapshot_id, config_signature=sig)
                    share = max(0.01, options.sample / max(1, len(expected)))
                    if len(sample) < options.sample and rng.random() < share:
                        sample[pid] = doc[VECTOR_FIELD]
                    yield doc

            ok, errors = _bulk(client, index, docs(), options.chunk_docs)
            receipt["timings_s"] = {"index": round(time.monotonic() - started, 3)}
            if errors or ok != len(selection):
                raise ProjectionError("E_BULK_FAILED", f"{len(errors)} failed bulk item(s), {ok} of {len(selection)} "
                                      "indexed", stage="index", details={"errors": errors[:20]})
            client.indices.refresh(index=index)
            client.indices.put_settings(index=index, body={"index": {"refresh_interval": None}})
            client.indices.forcemerge(index=index, params={"max_num_segments": 1})
            client.indices.refresh(index=index)
            count = int(client.count(index=index)["count"])
            problems = [] if count == len(expected) else [f"index holds {count} page vectors, snapshot has "
                                                          f"{len(expected)} pages with a preview"]
            if sample:
                found = client.mget(index=index, body={"ids": sorted(sample)})["docs"]
                for item in found:
                    got = (item.get("_source") or {}).get(VECTOR_FIELD)
                    want = sample.get(item["_id"])
                    if not item.get("found") or got is None or len(got) != len(want) or \
                            max(abs(float(a) - float(b)) for a, b in zip(got, want)) > 1e-6:
                        problems.append(f"sampled vector differs: {item['_id']}")
            receipt["checks"] = {"count": count, "expected": len(expected), "sampled": len(sample),
                                 "problems": problems[:20]}
            if problems:
                raise ProjectionError("E_COUNT_MISMATCH", "post-import checks failed", stage="verify",
                                      details={"problems": problems[:20]})
            client.indices.put_mapping(index=index, body={"_meta": {
                **meta, "vkm_pagevis_mapping_version": PAGEVIS_MAPPING_VERSION, "build_status": COMPLETE,
                "vector_count": count, "completed_at": utc_now().isoformat()}})
            alias = pagevis_alias(prefix)
            actions = [{"remove": {"index": old, "alias": alias}} for old in alias_indices(client, alias)
                       if old != index]
            actions.append({"add": {"index": index, "alias": alias}})
            client.indices.update_aliases(body={"actions": actions})
            receipt["alias_actions"] = actions
            receipt["pruned"] = prune_pagevis_builds(client, prefix, keep=options.keep_builds)
            receipt["status"] = COMPLETE
        except ProjectionError as exc:
            receipt["status"] = "FAILED"
            receipt["error"] = exc.as_dict()
            _drop(client, created, options, receipt)
            raise
        except Exception as exc:
            receipt["status"] = "FAILED"
            receipt["error"] = {"code": "E_INTERNAL", "stage": "index", "message": f"{type(exc).__name__}: {exc}"}
            _drop(client, created, options, receipt)
            raise
        finally:
            receipt["finished_at"] = utc_now()
            receipt.setdefault("timings_s", {})["total"] = round(time.monotonic() - t0, 3)
            run_id = receipt.get("build_id") or f"plan-{int(time.time())}"
            receipt["receipt_ref"] = write_receipt(root, "opensearch", f"{prefix}-pagevis-{run_id}", receipt)
    return receipt


def _drop(client: Any, created: list[str], options: PageVectorBuildOptions, receipt: dict[str, Any]) -> None:
    if client is None or not created or options.keep_failed:
        return
    deleted = []
    for name in created:
        try:
            client.indices.delete(index=name)
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001 - recorded; the original error is raised
            receipt.setdefault("delete_errors", []).append(f"{name}: {type(exc).__name__}")
    receipt["deleted_failed_indices"] = deleted


# ---------------------------------------------------------------- query time: the visual leg
def page_vector_body(vector: list[float], k: int, filters: dict[str, Any], *, mode: str = "exact",
                     space_type: str = "innerproduct", ef_search: int | None = None) -> dict[str, Any]:
    """Top-``k`` pages by inner product with the page-level filters: exact (``script_score`` + ``knn_score`` over the
    filtered pages — V2's exact cosine) or approximate (HNSW ``knn`` with efficient filtering)."""
    if mode not in SEARCH_MODES:
        raise ValueError(f"page-vector search mode must be one of {SEARCH_MODES}")
    clauses, must_not = compile_filters(filters)
    k = max(1, min(int(k), MAX_PAGE_K))
    source = {"includes": list(PAGE_SOURCE_FIELDS)}
    if mode == "exact":
        inner: dict[str, Any] = {"bool": {"filter": clauses, "must_not": must_not}} if (clauses or must_not) \
            else {"match_all": {}}
        return {"size": k, "_source": source, "query": {"script_score": {"query": inner, "script": {
            "source": "knn_score", "lang": "knn",
            "params": {"field": VECTOR_FIELD, "query_value": vector, "space_type": space_type}}}}}
    knn: dict[str, Any] = {"vector": vector, "k": k}
    if clauses or must_not:
        knn["filter"] = {"bool": {"filter": clauses, "must_not": must_not}}
    if ef_search:
        knn["method_parameters"] = {"ef_search": int(ef_search)}
    return {"size": k, "_source": source, "query": {"knn": {VECTOR_FIELD: knn}}}


def inner_product_of(score: float, space_type: str = "innerproduct") -> float:
    """OpenSearch k-NN score → inner product (= cosine of unit vectors): 1 + ip for ip ≥ 0, else 1 / (1 − ip)."""
    s = float(score)
    if space_type != "innerproduct":
        return s
    return s - 1.0 if s >= 1.0 else 1.0 - 1.0 / s if s > 0 else float("-inf")


@dataclass
class PageHit:
    page_id: str
    score: float                      # inner product (cosine) of the page vector and the query vector
    source: dict[str, Any]


def page_vector_hits(resp: dict[str, Any], limit: int, *, collapse_duplicates: bool,
                     space_type: str = "innerproduct") -> list[PageHit]:
    """OpenSearch answer → pages in score order (ties: page id), duplicate groups collapsed (first page stays)."""
    rows = []
    for h in resp.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        pid = str(src.get("page_id") or src.get("id") or h.get("_id"))
        rows.append(PageHit(pid, inner_product_of(h.get("_score") or 0.0, space_type), src))
    rows.sort(key=lambda r: (-r.score, r.page_id))
    out: list[PageHit] = []
    seen: set[str] = set()
    for r in rows:
        group = (r.source.get("dup_group_id") or r.page_id) if collapse_duplicates else r.page_id
        if group in seen:
            continue
        seen.add(group)
        out.append(r)
        if len(out) >= limit:
            break
    return out
