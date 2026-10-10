"""Issuer regenerates actual synthetic canonical units, never embeddings/models."""
import copy
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.graph.common import load_snapshot
from vkm_corpus.graph.synthetic import write_synthetic_canonical_root
from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow
from vkm_corpus.embeddings.pack import build_pack
from vkm_corpus.embeddings.signature import EmbeddingConfig
from vkm_corpus.search.vectors import export_units_at
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.runtime import BoundFile
from vkm_corpus.update import pack_policy as pp
from vkm_evidence.contracts import canonical_bytes


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value))
    return BoundFile(path=str(path), sha256=sha256_of(path))


def pack_assets(root, canonical=None, policy_path=None):
    root.mkdir(parents=True, exist_ok=True)
    canonical = canonical or write_synthetic_canonical_root(root / "canonical")
    snapshot = load_snapshot(canonical)
    unitdir = root / "units"
    export_units_at(canonical, snapshot, out_dir=unitdir)
    cfg = EmbeddingConfig(model_id="synthetic", model_revision="a" * 40, weights_file="synthetic-only",
        weights_sha256="b" * 64, quantization="F32", mode="multivector", dimension=8, pooling="none",
        normalization="l2", tokenizer_sha256="c" * 64, heads_sha256="d" * 64)
    writer = ArtifactWriter(root / "artifacts", "multivector", cfg, writer_id="synthetic")
    units = [json.loads(line) for line in (unitdir / "units.jsonl").read_text().splitlines()]
    vectors = np.array([[1., 0., 0., 0., 0., 0., 0., 0.]], dtype=np.float32)
    writer.write_part([EmbeddingRow(object_id=u["unit_id"], text_hash=u["text_hash"], source_id=u["source_id"],
        page_id=u["page_id"], object_type=u["kind"], vectors=vectors, token_ids=[1], worker="synthetic", backend="synthetic") for u in units])
    result = build_pack(writer.dir, unitdir)
    pack = writer.dir / "packs" / result["pack_id"]
    context = write(root / "context.json", AccessContext(principal="synthetic", execution="CLOUD"))
    if policy_path is None:
        sources = set()
        for path in snapshot.datasets["sources"].paths:
            sources.update(pq.read_table(path, columns=["source_id"])["source_id"].to_pylist())
        policy = ResourcePolicy(access_class="PUBLIC", policy_version="synthetic-1", authority="synthetic")
        policy_ref = write(root / "policy.json", {"schema": "vkm-source-policy/1", "policies": {s: policy.model_dump(mode="json") for s in sources}})
    else:
        policy_ref = BoundFile(path=str(policy_path), sha256=sha256_of(policy_path))
    request = pp.PackPolicyRequest(canonical_root=str(canonical), snapshot_id=snapshot.snapshot_id,
        canonical_manifest_sha256=snapshot.manifest_sha256, pack_manifest=BoundFile(path=str(pack / "pack.json"), sha256=sha256_of(pack / "pack.json")),
        policy=policy_ref, context=context, max_units=10000, max_source_rows=10000,
        max_canonical_bytes=100_000_000, max_pack_bytes=100_000_000, max_spool_bytes=100_000_000,
        memory_gib=4, min_free_disk_gib=.001, timeout_seconds=60)
    return request, pack


def qualify(root, request):
    output = root / "qualification"
    output.mkdir(mode=0o700)
    proof = pp._qualify(request, output)
    return output, proof


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = tmp_path_factory.mktemp("pack-policy")
    request, pack = pack_assets(root)
    out, proof = qualify(root, request)
    return request, pack, out, proof


def test_actual_reproduced_unit_and_pack_identities_issue_exact_binding(prepared):
    request, pack, out, proof = prepared
    binding = json.loads((out / "binding.json").read_bytes())
    loaded = proof.loaded_pack.model_dump(mode="json")
    checked = pp.verify_pack_policy_receipt((out / "qualification.json").read_bytes(), sha256_of(out / "qualification.json"), binding, loaded)
    assert checked == proof
    assert proof.unit_count == proof.loaded_pack.count and proof.source_count > 0
    assert proof.model_quality == proof.scientific_qualification == "NOT_RUN"
    assert proof.binding.canonical_manifest_sha256 == request.canonical_manifest_sha256
    assert set(binding) == {"status", "component_manifest_sha256", "policy_sha256", "snapshot_id", "canonical_manifest_sha256"}


@pytest.mark.parametrize("fault", ["text_hash", "source", "page", "object", "duplicate", "offset", "tokens_bytes", "missing", "config"])
def test_actual_index_or_pack_mismatch_blocks_without_receipt(tmp_path, fault):
    request, pack = pack_assets(tmp_path)
    index = pq.read_table(pack / "index.parquet")
    rows = index.to_pylist()
    if fault == "text_hash": rows[0]["text_hash"] = "0" * 64
    if fault == "source": rows[0]["source_id"] = "VKM-SRC-999"
    if fault == "page": rows[0]["page_id"] = "VKM-SRC-001:p9999"
    if fault == "object": rows[0]["object_ids"] = ["foreign-object"]
    if fault == "duplicate": rows[-1] = copy.deepcopy(rows[0])
    if fault == "offset": rows[0]["token_offset"] = 1
    if fault == "missing": rows.pop()
    pq.write_table(pa.Table.from_pylist(rows, schema=index.schema), pack / "index.parquet")
    manifest = json.loads((pack / "pack.json").read_bytes())
    manifest["files"]["index.parquet"] = {"sha256": sha256_of(pack / "index.parquet"), "bytes": (pack / "index.parquet").stat().st_size}
    if fault == "tokens_bytes": (pack / "tokens.f16").write_bytes(b"corruption")
    if fault == "config": manifest["config"]["text_rule"] = "vkm-units-v1/Z"
    pin = write(pack / "pack.json", manifest)
    request = request.model_copy(update={"pack_manifest": pin})
    with pytest.raises(ValueError): qualify(tmp_path, request)
    assert not (tmp_path / "qualification" / "qualification.json").exists()


@pytest.mark.parametrize("fault", ["canonical_bytes", "canonical_hash", "missing_policy", "local_only", "unit_budget", "source_budget", "spool_budget", "pack_budget"])
def test_policy_and_bounded_reproduction_gates(tmp_path, fault):
    request, pack = pack_assets(tmp_path)
    updates = {}
    if fault == "canonical_bytes":
        snapshot = load_snapshot(Path(request.canonical_root), request.snapshot_id)
        p = snapshot.datasets["blocks"].paths[0]; p.write_bytes(p.read_bytes() + b"changed")
    if fault == "canonical_hash": updates["canonical_manifest_sha256"] = "0" * 64
    if fault in {"missing_policy", "local_only"}:
        policy = json.loads(Path(request.policy.path).read_bytes())
        if fault == "missing_policy": policy["policies"] = {}
        else:
            for value in policy["policies"].values(): value["access_class"] = "PRIVATE_LOCAL_ONLY"
        updates["policy"] = write(Path(request.policy.path), policy)
    if fault == "unit_budget": updates["max_units"] = 1
    if fault == "source_budget": updates["max_source_rows"] = 1
    if fault == "spool_budget": updates["max_spool_bytes"] = 1
    if fault == "pack_budget": updates["max_pack_bytes"] = 1
    with pytest.raises((ValueError, PermissionError)): qualify(tmp_path, request.model_copy(update=updates))
    assert not (tmp_path / "qualification" / "qualification.json").exists()


@pytest.mark.parametrize("fault", ["generic", "loaded_file", "binding", "code", "missing_check", "quality_pass", "count"])
def test_receipt_verifier_requires_typed_issuer_and_actual_loaded_identity(prepared, fault):
    request, pack, out, proof = prepared
    receipt = proof.model_dump(mode="json")
    loaded, binding = proof.loaded_pack.model_dump(mode="json"), proof.binding.model_dump(mode="json")
    if fault == "generic": receipt = {"status": "PASS"}
    if fault == "loaded_file": loaded["files"]["tokens.f16"] = "0" * 64
    if fault == "binding": binding["canonical_manifest_sha256"] = "0" * 64
    if fault == "code": receipt["issuer"]["code"]["vkm_corpus.update.pack_policy"] = "0" * 64
    if fault == "missing_check": receipt["checks"].pop("unit_text")
    if fault == "quality_pass": receipt["model_quality"] = "PASS"
    if fault == "count": receipt["unit_count"] += 1
    raw = canonical_bytes(receipt)
    import hashlib
    with pytest.raises(ValueError): pp.verify_pack_policy_receipt(raw, hashlib.sha256(raw).hexdigest(), binding, loaded)


def test_input_change_while_replaying_units_prevents_publication(tmp_path, monkeypatch):
    request, pack = pack_assets(tmp_path)
    from vkm_corpus.retrieval_lab import units
    render = units.render
    done = False
    def changed(*args, **kwargs):
        nonlocal done
        if not done:
            done = True
            Path(request.policy.path).write_bytes(Path(request.policy.path).read_bytes() + b" ")
        return render(*args, **kwargs)
    monkeypatch.setattr(units, "render", changed)
    with pytest.raises(ValueError, match="byte identity"): qualify(tmp_path, request)
    assert not (tmp_path / "qualification" / "qualification.json").exists()


def test_public_issuer_always_uses_fixed_bounded_child(tmp_path, monkeypatch):
    request, pack = pack_assets(tmp_path / "inputs")
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv[1:4] == ["-m", "vkm_corpus.update.pack_policy", "--worker"]
        assert kwargs["timeout"] == request.timeout_seconds and kwargs["memory_gib"] == request.memory_gib
        p = pp.checked_file(argv[4], argv[5])
        pp._qualify(pp.PackPolicyRequest.model_validate_json(p.read_bytes()), p.parent)
        return 0
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setattr(pp, "bounded_subprocess", runner)
    result = pp.issue_pack_policy(request, tmp_path / "output")
    assert len(calls) == 1 and set(result) == {"binding", "qualification"}


def test_issuer_code_identity_is_stable_when_executed_as_a_worker_module():
    import runpy
    worker = runpy.run_path(str(Path(pp.__file__)), run_name="_synthetic_pack_policy_worker")
    assert worker["issuer_identity"]() == pp.issuer_identity()
