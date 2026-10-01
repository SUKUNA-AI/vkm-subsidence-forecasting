"""Packed NAV payload bytes must be covered by serving admission, not only meta."""
import json

import duckdb
import pytest

from vkm_corpus.api.production import bind_generation_guard
from vkm_corpus.navigation.store import NavStore
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.contracts import ComponentIdentity, GenerationManifest
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.runtime import Observation, observe_components
from vkm_evidence.contracts import record_hash
from test_production_serving import setup, write, replace_acceptance, requires_linux_receiver, SNAP


def with_nav(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    directory = root / "derived/navigation" / SNAP
    canonical = json.loads((root / "canonical/_snapshots" / (SNAP + ".json")).read_bytes())
    manifest = write(directory / "manifest.json", {"format": "vkm-nav-manifest-v1",
        "identity_status": "SNAPSHOT_VERIFIED", "snapshot": {
            "snapshot_id": canonical["snapshot_id"],
            "manifest_sha256": sha256_of(root / "canonical/_snapshots" / (SNAP + ".json"))},
        "datasets": {}})
    (directory.parent / "CURRENT").write_text(SNAP)
    packed = directory / "nav.duckdb"
    meta = {"identity_status": "SNAPSHOT_VERIFIED", "manifest_sha256": manifest.sha256,
        "snapshot": json.loads((directory / "manifest.json").read_bytes())["snapshot"], "snapshot_id": SNAP}
    with duckdb.connect(str(packed)) as con:
        con.execute("CREATE TABLE nav_meta AS SELECT ? AS meta_json", [json.dumps(meta)])
        con.execute("CREATE TABLE sections AS SELECT 'S-1' AS section_id, 'qualified synthetic title' AS title")
    binding = write(directory / "policy-binding.json", {"status": "PASS",
        "component_manifest_sha256": manifest.sha256, "policy_sha256": runtime.config.policy.sha256})
    spec = Observation(component="NAV", native_manifest=manifest.path, policy_binding=binding.path,
        runtime_database=str(packed))
    runtime.config = runtime.config.model_copy(update={"observations": (*runtime.config.observations, spec)})
    observed = observe_components(runtime.config.observations)
    served = runtime.root / "served"
    old = GenerationManifest.model_validate_json((served / ((served / "CURRENT").read_text() + ".json")).read_bytes())
    generation = old.model_copy(update={"components": tuple(ComponentIdentity.model_validate(v) for v in observed.values())})
    write(served / (generation.sha256 + ".json"), generation)
    (served / "CURRENT").write_text(generation.sha256)
    replace_acceptance(runtime, nav_file_sha256=sha256_of(packed),
        components_sha256=record_hash(sorted(observed.values(), key=lambda c: c["component"])))
    deps.nav = NavStore(root, canonical_db=deps.canon.path)
    return deps, runtime, cfg, root, packed


@requires_linux_receiver
@pytest.mark.parametrize("before_bind", [False, True])
def test_packed_nav_payload_change_cannot_reuse_unchanged_origin_metadata(tmp_path, before_bind):
    deps, runtime, cfg, root, packed = with_nav(tmp_path)
    guard = None if before_bind else bind_generation_guard(deps, runtime, cfg, root)
    if guard:
        assert guard()["status"] == "READY"
    # Atomic publication avoids DuckDB's read-only connection configuration
    # conflict and models the operational artifact replacement receivers face.
    import shutil
    changed = packed.with_suffix(".replacement")
    shutil.copyfile(packed, changed)
    with duckdb.connect(str(changed)) as con:
        previous = con.execute("SELECT meta_json FROM nav_meta").fetchall()
        con.execute("UPDATE sections SET title='unqualified synthetic corruption'")
        assert con.execute("SELECT meta_json FROM nav_meta").fetchall() == previous
    changed.replace(packed)
    if before_bind:
        with pytest.raises(GenerationUnavailable):
            bind_generation_guard(deps, runtime, cfg, root)
    else:
        assert guard()["status"] == "UNAVAILABLE", "packed NAV contents changed while its origin metadata remained identical"
