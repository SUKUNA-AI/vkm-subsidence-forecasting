"""Native, bounded OpenSearch generation observation and exact alias replacement.

This is a SEARCH_BUNDLE adapter, not a multi-store coordinator. Its caller owns
the durable intent, exclusive publisher lease and drained receivers. All query
bodies are fixed here; manifests contain identities/names, never executable SQL.
Qualification scans actual documents once; a lease then checks native index UUID,
mapping/settings and primary sequence checkpoints. Missing native proof fails closed.
The operator must protect qualified write-blocked indices from administrative
restore or edits; this does not defend against a malicious cluster administrator.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Callable

from pydantic import Field, model_validator

from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash
from vkm_corpus.search.mappings import GROUP_TYPES, INDEX_TYPES


class RemoteSearchError(ValueError):
    pass


def validate_prepare_options(publish: bool, policy_sha256: str | None) -> None:
    import re
    if type(publish) is not bool or ((not publish or policy_sha256 is not None)
            and (not isinstance(policy_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", policy_sha256))):
        raise RemoteSearchError("shadow preparation requires an explicit policy SHA-256")


def freeze_prepared_indices(client, names: list[str]) -> None:
    """Only newly built, exact indices supplied by a builder, never an alias."""
    for name in names:
        client.indices.put_settings(index=name, body={"index": {"blocks.write": True}})


class SearchBundleSpec(StrictModel):
    prefix: str = Field(pattern=r"^[a-z][a-z0-9]{1,30}$")
    indices: dict[str, str]

    @model_validator(mode="after")
    def _names(self):
        import re
        if not set(INDEX_TYPES) <= set(self.indices) <= {*INDEX_TYPES, "vectors", "pagevis"}:
            raise ValueError("bundle requires all document types and only declared retrieval channels")
        if len(set(self.indices.values())) != len(self.indices):
            raise ValueError("duplicate concrete index")
        for kind, name in self.indices.items():
            if not re.fullmatch(re.escape(self.prefix + "-" + kind) + r"-m[0-9]+-[a-z0-9][a-z0-9_-]{0,150}", name):
                raise ValueError("unsafe or foreign concrete index name")
        return self

    def aliases(self) -> dict[str, tuple[str, ...]]:
        return {**{self.prefix + "-" + k: (v,) for k, v in sorted(self.indices.items())},
                self.prefix + "-objects": tuple(sorted(self.indices[k] for k in GROUP_TYPES))}


class NativeIndex(StrictModel):
    name: str
    uuid: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    canonical_manifest_sha256: Sha256
    policy_sha256: Sha256
    mapping_sha256: Sha256
    settings_sha256: Sha256
    checkpoints: dict[str, tuple[int, int, int]]
    count: int = Field(ge=0)


class SearchBundleIdentity(StrictModel):
    schema_version: str = "vkm-search-bundle/1"
    spec: SearchBundleSpec
    cluster_uuid: str = Field(min_length=1)
    endpoints_sha256: Sha256
    indices: dict[str, NativeIndex]
    content_sha256: dict[str, Sha256]

    @model_validator(mode="after")
    def _complete(self):
        if (self.schema_version != "vkm-search-bundle/1" or set(self.indices) != set(self.spec.indices)
                or set(self.content_sha256) != set(self.indices)):
            raise ValueError("incomplete bundle identity")
        if any(v.name != self.spec.indices[k] for k, v in self.indices.items()):
            raise ValueError("index identity differs from bundle spec")
        if len({(i.snapshot_id, i.canonical_manifest_sha256, i.policy_sha256) for i in self.indices.values()}) != 1:
            raise ValueError("mixed document/policy generations")
        return self

    @property
    def sha256(self):
        return record_hash(self)


def endpoint_identity(client) -> tuple[str, str]:
    info = client.info()
    cluster = info.get("cluster_uuid")
    if not isinstance(cluster, str) or not cluster or cluster == "_na_":
        raise RemoteSearchError("actual cluster UUID unavailable")
    hosts = client.transport.hosts
    safe = [{k: h[k] for k in ("host", "port", "scheme", "url_prefix", "use_ssl") if k in h} for h in hosts]
    if not safe or any(not h.get("host") for h in safe):
        raise RemoteSearchError("actual client endpoints unavailable")
    return cluster, record_hash(sorted(safe, key=canonical_bytes))


def alias_bindings(client, spec: SearchBundleSpec) -> dict[str, tuple[str, ...]]:
    out = {}
    for alias in spec.aliases():
        try:
            response = client.indices.get_alias(name=alias)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                response = {}
            else:
                raise
        if any(body.get("aliases", {}).get(alias) != {} for body in response.values()):
            raise RemoteSearchError("filtered/routed/write aliases are not qualified")
        out[alias] = tuple(sorted(response))
    return out


def native_index(client, name: str) -> NativeIndex:
    mappings = client.indices.get_mapping(index=name)
    settings = client.indices.get_settings(index=name, params={"flat_settings": "true"})
    stats = client.indices.stats(index=name, params={"level": "shards"})
    if (set(mappings) != {name} or set(settings) != {name}
            or set(stats.get("indices", {})) != {name} or stats.get("_shards", {}).get("failed") != 0):
        raise RemoteSearchError("ambiguous or incomplete native index observation")
    mapping = mappings[name]["mappings"]
    meta = mapping.get("_meta", {})
    setting = settings[name]["settings"]
    if meta.get("build_status") != "COMPLETE" or str(setting.get("index.blocks.write")).lower() != "true":
        raise RemoteSearchError("index must be complete and write-blocked")
    state = stats["indices"][name]
    if setting.get("index.uuid") != state.get("uuid") or not state.get("uuid"):
        raise RemoteSearchError("native index UUID mismatch")
    fences = {}
    for shard, replicas in state.get("shards", {}).items():
        primary = [r for r in replicas if r.get("routing", {}).get("primary") is True]
        if len(primary) != 1 or primary[0]["routing"].get("state") != "STARTED":
            raise RemoteSearchError("primary shard unavailable")
        seq = primary[0].get("seq_no", {})
        vals = tuple(seq.get(k) for k in ("max_seq_no", "local_checkpoint", "global_checkpoint"))
        if any(type(v) is not int or v < -1 for v in vals) or len(set(vals)) != 1:
            raise RemoteSearchError("unsettled or missing primary checkpoints")
        fences[shard] = vals
    if not fences or len(fences) != int(setting.get("index.number_of_shards", 0)):
        raise RemoteSearchError("incomplete shard inventory")
    count = client.count(index=name)
    if count.get("_shards", {}).get("failed") != 0 or type(count.get("count")) is not int:
        raise RemoteSearchError("incomplete native document count")
    return NativeIndex(name=name, uuid=state["uuid"], snapshot_id=meta.get("built_from_snapshot_id"),
        canonical_manifest_sha256=meta.get("canonical_manifest_sha256"), policy_sha256=meta.get("policy_sha256"),
        mapping_sha256=record_hash(mapping), settings_sha256=record_hash(setting), checkpoints=fences,
        count=count["count"])


def content_digest(client, name: str, *, max_documents: int, timeout_seconds: float) -> tuple[int, str]:
    """Order-independent SHA of actual ID+source leaves; bounded memory and time.

    A scroll is always cleared. There is no user-supplied query and no document
    content in the returned proof. Each leaf is only 32 bytes (plus bounded IDs).
    """
    if max_documents < 0 or timeout_seconds <= 0:
        raise ValueError("invalid scan limits")
    deadline = time.monotonic() + timeout_seconds
    leaves, ids, scroll = [], set(), None
    try:
        response = client.search(index=name, body={"query": {"match_all": {}}, "sort": ["_doc"],
            "size": 1000, "track_total_hits": True}, params={"scroll": "1m", "request_timeout": timeout_seconds})
        scroll = response.get("_scroll_id")
        total = response.get("hits", {}).get("total", {})
        if not isinstance(total, dict) or total.get("relation") != "eq" or type(total.get("value")) is not int:
            raise RemoteSearchError("exact scroll total unavailable")
        expected = total["value"]
        if expected > max_documents:
            raise RemoteSearchError("content scan exceeds document budget")
        while True:
            scroll = response.get("_scroll_id") or scroll
            if response.get("timed_out") is not False or response.get("_shards", {}).get("failed") != 0:
                raise RemoteSearchError("partial content scan")
            hits = response.get("hits", {}).get("hits")
            if not isinstance(hits, list):
                raise RemoteSearchError("invalid content scan")
            for hit in hits:
                oid = hit.get("_id")
                if (hit.get("_index") != name or not isinstance(oid, str) or oid in ids
                        or not isinstance(hit.get("_source"), dict)):
                    raise RemoteSearchError("foreign, duplicate or missing source in scan")
                ids.add(oid)
                leaves.append(hashlib.sha256(canonical_bytes({"id": oid, "source": hit["_source"]})).digest())
            if len(ids) > max_documents or time.monotonic() > deadline:
                raise RemoteSearchError("content scan budget exhausted")
            if not hits:
                break
            if not scroll:
                raise RemoteSearchError("scroll identity missing")
            response = client.scroll(body={"scroll_id": scroll, "scroll": "1m"},
                                     params={"request_timeout": max(.001, deadline - time.monotonic())})
        if len(ids) != expected:
            raise RemoteSearchError("content scan count differs")
        h = hashlib.sha256(b"vkm-search-content/1\0" + str(len(leaves)).encode() + b"\0")
        for leaf in sorted(leaves):
            h.update(leaf)
        return len(leaves), h.hexdigest()
    finally:
        if scroll:
            client.clear_scroll(body={"scroll_id": [scroll]})


def qualify_bundle(client, spec: SearchBundleSpec, *, max_documents: int, timeout_seconds: float) -> SearchBundleIdentity:
    endpoint = endpoint_identity(client)
    indices, hashes = {}, {}
    deadline = time.monotonic() + timeout_seconds
    remaining = max_documents
    for kind, name in sorted(spec.indices.items()):
        before = native_index(client, name)
        count, digest = content_digest(client, name, max_documents=remaining,
                                      timeout_seconds=deadline - time.monotonic())
        if count != before.count or before != native_index(client, name):
            raise RemoteSearchError("index changed during qualification")
        remaining -= count
        indices[kind], hashes[kind] = before, digest
    if endpoint_identity(client) != endpoint:
        raise RemoteSearchError("endpoint changed during qualification")
    if any(native_index(client, entry.name) != entry for entry in indices.values()):
        raise RemoteSearchError("bundle changed before qualification completed")
    return SearchBundleIdentity(spec=spec, cluster_uuid=endpoint[0], endpoints_sha256=endpoint[1],
                                indices=indices, content_sha256=hashes)


class SearchBundleLease:
    """Process-bound proof: caller cannot create a lease by passing expected JSON.

    The native scan happens in the constructor. Persisted identity can be compared
    to ``identity`` after restart, but never substitutes for requalification.
    """
    def __init__(self, client, spec: SearchBundleSpec, *, max_documents: int, timeout_seconds: float):
        self.client = client
        self.identity = qualify_bundle(client, spec, max_documents=max_documents, timeout_seconds=timeout_seconds)

    def observe(self, *, selected: bool = True) -> SearchBundleIdentity:
        expected = self.identity
        before = alias_bindings(self.client, expected.spec) if selected else None
        if before is not None and before != expected.spec.aliases():
            raise RemoteSearchError("serving aliases select another generation")
        if endpoint_identity(self.client) != (expected.cluster_uuid, expected.endpoints_sha256):
            raise RemoteSearchError("serving cluster/client changed")
        for kind, want in expected.indices.items():
            if native_index(self.client, want.name) != want:
                raise RemoteSearchError("qualified index changed")
        if selected and alias_bindings(self.client, expected.spec) != before:
            raise RemoteSearchError("aliases changed during observation")
        return expected.model_copy(deep=True)


class NativeSearchObserver:
    """Bind native proofs to the clients/caches actually used by API routes."""
    def __init__(self, deps, spec: SearchBundleSpec, *, max_documents: int, timeout_seconds: float):
        from vkm_corpus.api.backends import OpenSearchBackend, HybridBackend
        if not isinstance(deps.search, OpenSearchBackend):
            raise RemoteSearchError("native search observer requires actual OpenSearchBackend")
        if deps.hybrid is not None and not isinstance(deps.hybrid, HybridBackend):
            raise RemoteSearchError("native hybrid observer requires actual HybridBackend")
        if deps.hybrid is not None and not {"vectors", "pagevis"} <= spec.indices.keys():
            raise RemoteSearchError("complete hybrid serving requires dense and visual search channels")
        self.deps, self.search, self.hybrid = deps, deps.search, deps.hybrid
        self.client = self.search._connect()
        self.spec = spec
        self._client_fence()
        self.lease = SearchBundleLease(self.client, spec, max_documents=max_documents, timeout_seconds=timeout_seconds)
        self.meta = {}
        for kind in ("vectors", "pagevis"):
            if kind in spec.indices:
                name = spec.indices[kind]
                mapping = self.client.indices.get_mapping(index=name)[name]["mappings"]
                self.meta[kind] = {**mapping["_meta"], "index": name, "alias": spec.prefix + "-" + kind}
        self.observe()

    def _client_fence(self):
        if (self.deps.search is not self.search or self.deps.hybrid is not self.hybrid
                or self.search._connect() is not self.client
                or self.search.settings.opensearch_index_prefix != self.spec.prefix):
            raise RemoteSearchError("actual search backend/client/prefix changed")
        if self.hybrid is not None:
            if (self.hybrid._search._connect() is not self.client
                    or self.hybrid._search.settings.opensearch_index_prefix != self.spec.prefix
                    or self.hybrid.settings.opensearch_index_prefix != self.spec.prefix):
                raise RemoteSearchError("hybrid does not read the same actual search bundle")

    def observe(self):
        from vkm_corpus.update.contracts import ComponentIdentity
        self._client_fence()
        proof = self.lease.observe()
        if self.hybrid is not None:
            for attr, kind in (("_meta", "vectors"), ("_vmeta", "pagevis")):
                cached = getattr(self.hybrid, attr, None)
                if cached is not None and (not isinstance(cached, tuple) or len(cached) != 2 or cached[1] != self.meta[kind]):
                    raise RemoteSearchError("hybrid metadata cache belongs to another generation")
        first = next(iter(proof.indices.values()))
        components = [ComponentIdentity(component="SEARCH", revision=proof.sha256, manifest_sha256=proof.sha256,
            policy_sha256=first.policy_sha256, built_from={"DOCUMENT": first.snapshot_id})]
        for kind, component in (("vectors", "DENSE"), ("pagevis", "VISUAL")):
            if kind in proof.indices:
                native = proof.indices[kind]
                digest = record_hash({"index": native.model_dump(mode="json"), "content_sha256": proof.content_sha256[kind]})
                components.append(ComponentIdentity(component=component, revision=native.uuid,
                    manifest_sha256=digest, policy_sha256=native.policy_sha256,
                    built_from={"DOCUMENT": native.snapshot_id, "SEARCH": proof.sha256}))
        self._client_fence()
        return {c.component: c.model_dump(mode="json") for c in components}


class SearchBundleSwitch:
    """Exact reversible adapter. A durable coordinator supplies the writer/drain fence.

    ``fence`` must raise when its exclusive/drained lease is lost; a bool in a
    manifest is not authorization. This class never deletes or prunes indices.
    """
    def __init__(self, previous: SearchBundleLease, candidate: SearchBundleLease, *, fence: Callable[[], None]):
        if (previous.client is not candidate.client or previous.identity.spec.prefix != candidate.identity.spec.prefix
                or set(previous.identity.spec.indices) != set(candidate.identity.spec.indices)):
            raise RemoteSearchError("switch requires one actual client and the complete same alias set")
        self.previous, self.candidate, self.fence = previous, candidate, fence
        self._client = previous.client

    def _candidate_ownership(self):
        """A rollback rejects unknown physical indices, not a failed payload.

        Native cluster, endpoint and concrete UUIDs prove what the exact alias
        removal affects. Candidate contents/checkpoints deliberately need not
        remain qualified: their failure is a reason to restore the previous
        complete, independently verified bundle.
        """
        expected = self.candidate.identity
        client = self.candidate.client
        if endpoint_identity(client) != (expected.cluster_uuid, expected.endpoints_sha256):
            raise RemoteSearchError("rollback candidate cluster/client changed")
        for native in expected.indices.values():
            settings = client.indices.get_settings(index=native.name, params={"flat_settings": "true"})
            stats = client.indices.stats(index=native.name, params={"level": "shards"})
            if (set(settings) != {native.name} or set(stats.get("indices", {})) != {native.name}
                    or stats.get("_shards", {}).get("failed") != 0
                    or settings[native.name].get("settings", {}).get("index.uuid") != native.uuid
                    or stats["indices"][native.name].get("uuid") != native.uuid):
                raise RemoteSearchError("rollback candidate physical index ownership changed")
        if endpoint_identity(client) != (expected.cluster_uuid, expected.endpoints_sha256):
            raise RemoteSearchError("rollback candidate endpoint changed during observation")

    def _move(self, source, target, *, restoring=False):
        self.fence()
        if source.client is not self._client or target.client is not self._client:
            raise RemoteSearchError("selector adapter client binding changed")
        if not restoring:
            source.observe(selected=False)
        target.observe(selected=False)
        current = alias_bindings(source.client, source.identity.spec)
        wanted = target.identity.spec.aliases()
        if current == wanted:
            target.observe()
            return {"status": "PASS", "scope": "SEARCH_BUNDLE", "identity_sha256": target.identity.sha256,
                    "changed": False}
        if current != source.identity.spec.aliases():
            raise RemoteSearchError("selectors differ from exact previous/candidate set")
        if restoring:
            self._candidate_ownership()
        actions = []
        for alias in sorted(current):
            actions.extend({"remove": {"index": name, "alias": alias, "must_exist": True}}
                           for name in current[alias] if name not in wanted[alias])
            actions.extend({"add": {"index": name, "alias": alias}}
                           for name in wanted[alias] if name not in current[alias])
        self.fence()
        # Recheck the healthy target and rejected physical source immediately
        # before the alias mutation. No index content is deleted or rewritten.
        target.observe(selected=False)
        if restoring:
            self._candidate_ownership()
        if alias_bindings(source.client, source.identity.spec) != current:
            raise RemoteSearchError("selector changed before mutation")
        result = source.client.indices.update_aliases(body={"actions": actions})
        if result.get("acknowledged") is not True:
            raise RemoteSearchError("alias operation not acknowledged; observe before recovery")
        self.fence()
        target.observe()
        return {"status": "PASS", "scope": "SEARCH_BUNDLE", "identity_sha256": target.identity.sha256,
                "changed": True}

    def apply(self):
        return self._move(self.previous, self.candidate)

    def restore(self):
        return self._move(self.candidate, self.previous, restoring=True)
