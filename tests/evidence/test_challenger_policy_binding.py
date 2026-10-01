"""Adversarial serving-policy binding checks using only synthetic artifacts.

The assertions describe the required fail-closed contract. A RED result means
that the qualified policy artifact differs from the store authorizing requests.
"""
from types import SimpleNamespace
import duckdb

import pytest

from vkm_corpus.api.app import _policy_access
from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.api.production import bind_generation_guard
from vkm_corpus.contracts.access import ResourcePolicy
from vkm_corpus.contracts.policy_store import SourcePolicyStore
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.runtime import observe_components
from vkm_corpus.parquet.atomic import sha256_of
from vkm_evidence.contracts import record_hash

from test_production_serving import (
    setup, write, replace_acceptance, requires_linux_receiver,
)


def restricted_setup(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    sid = "VKM-SRC-001"
    restricted = ResourcePolicy(access_class="PRIVATE_LOCAL_ONLY", policy_version="1", authority="synthetic")
    policy = write(deps.access_policy.path, {"schema": "vkm-source-policy/1", "policies": {
        sid: restricted.model_dump(mode="json")}})
    runtime.config = runtime.config.model_copy(update={"policy": policy})
    with duckdb.connect(str(deps.canon.path)) as con:
        con.execute("INSERT INTO sources VALUES (?)", [sid])
    for spec in runtime.config.observations:
        import json
        from pathlib import Path
        body = json.loads(Path(spec.policy_binding).read_bytes())
        body["policy_sha256"] = policy.sha256
        write(Path(spec.policy_binding), body)
    observed = observe_components(runtime.config.observations)
    served = runtime.root / "served"
    old = GenerationManifest.model_validate_json((served / ((served / "CURRENT").read_text() + ".json")).read_bytes())
    manifest = old.model_copy(update={"policy_sha256": policy.sha256,
        "components": tuple(type(old.components[0]).model_validate(v) for v in observed.values())})
    write(served / (manifest.sha256 + ".json"), manifest)
    (served / "CURRENT").write_text(manifest.sha256)
    replace_acceptance(runtime, policy_sha256=policy.sha256,
        duckdb_file_sha256=sha256_of(deps.canon.path),
        components_sha256=record_hash(sorted(observed.values(), key=lambda c: c["component"])))
    deps.access_policy.sources = lambda: (sid,)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        service=SimpleNamespace(deps=deps), config=cfg)), url=SimpleNamespace(path="/v1/search"))
    assert guard()["status"] == "READY"
    with pytest.raises(ApiFailure, match="resource policy denies"):
        _policy_access(request, "reader")
    return deps, guard, request


@requires_linux_receiver
@pytest.mark.parametrize("mutation", ["path", "replace", "remove", "source_inventory"])
def test_effective_policy_rebinding_cannot_reuse_qualified_generation(tmp_path, mutation):
    deps, guard, request = restricted_setup(tmp_path)
    public = ResourcePolicy(access_class="PUBLIC", policy_version="unqualified", authority="synthetic")
    other = tmp_path / "unqualified-policy.json"
    write(other, {"schema": "vkm-source-policy/1", "policies": {
        "VKM-SRC-001": public.model_dump(mode="json")}})
    if mutation == "path":
        deps.access_policy.path = other
    elif mutation == "replace":
        deps.access_policy = SourcePolicyStore(other, lambda: ("VKM-SRC-001",))
    elif mutation == "remove":
        deps.access_policy = None
    else:
        # The canonical-serving source inventory must not silently disappear.
        deps.access_policy.sources = lambda: ()
    # Exercise the real HTTP dependency as well. The fixed implementation may
    # deny here too; production admission must independently reject rebinding.
    try:
        _policy_access(request, "reader")
    except ApiFailure:
        pass
    assert guard()["status"] == "UNAVAILABLE", "effective authorization changed without a new qualified generation"
