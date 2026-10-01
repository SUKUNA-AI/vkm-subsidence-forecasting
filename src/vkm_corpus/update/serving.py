"""Native observers built from the API's selected backends, never sidecar claims.

Configuration contains fixed typed specifications and immutable artifact paths.
Binding is asynchronous because identity endpoints use the actual async clients;
model inference and selector mutations are never part of receiver startup.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Literal

from pydantic import Field

from vkm_corpus.update.contracts import ComponentIdentity
from vkm_corpus.update.remote_control import ControlSpec
from vkm_corpus.update.remote_search import NativeSearchObserver, SearchBundleSpec
from vkm_corpus.update.remote_services import NativeServiceObserver
from vkm_corpus.update.runtime import BoundFile, read_bound
from vkm_evidence.contracts import StrictModel


def _bound_json(ref: BoundFile):
    from pathlib import Path
    path = Path(ref.path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("unavailable or indirect bounded native profile")
    data = path.read_bytes()
    if len(data) > 2 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise ValueError("native profile bytes changed")
    return json.loads(data)


class NativeServingProfile(StrictModel):
    schema_version: Literal["vkm-native-serving-profile/1"] = "vkm-native-serving-profile/1"
    search: SearchBundleSpec
    graph_manifest: BoundFile
    late_policy_binding: BoundFile
    late_qualification: BoundFile
    control: ControlSpec
    max_search_documents: int = Field(ge=1, le=100_000_000)
    search_timeout_seconds: float = Field(gt=0, le=3600)
    graph_page_size: int = Field(default=500, ge=1, le=5000)


class NativeServingBindings:
    @classmethod
    async def bind(cls, deps, profile_file: BoundFile):
        from vkm_corpus.graph.shadow_bundle import CombinedGraphBundle
        from vkm_corpus.update.remote_graph import NativeGraphObserver

        def components():
            profile = NativeServingProfile.model_validate(_bound_json(profile_file))
            if any(getattr(deps, name, None) is None for name in ("graph", "search", "hybrid", "rerank", "control")):
                raise ValueError("native production profile requires complete graph/search/retrieval/rerank/control")
            graph_file = read_bound(profile.graph_manifest)
            graph = CombinedGraphBundle.read(graph_file.parent, manifest_sha256=profile.graph_manifest.sha256)
            search = NativeSearchObserver(deps, profile.search, max_documents=profile.max_search_documents,
                                          timeout_seconds=profile.search_timeout_seconds)
            return profile, search, NativeGraphObserver(deps, graph, page_size=profile.graph_page_size)

        obj = cls()
        obj.deps, obj.profile_file = deps, profile_file
        obj.profile, obj.search, obj.graph = await asyncio.to_thread(components)
        obj.services = await NativeServiceObserver.bind(deps, control_spec=obj.profile.control)
        await obj.services.observe()
        obj.observe_components()
        return obj

    def _profile_fence(self):
        if NativeServingProfile.model_validate(_bound_json(self.profile_file)) != self.profile:
            raise ValueError("native serving profile changed")

    def observe_components(self):
        self._profile_fence()
        binding = _bound_json(self.profile.late_policy_binding)
        pack = self.services.pack_proof
        from vkm_corpus.update.pack_policy import verify_pack_policy_receipt
        qualification_file = read_bound(self.profile.late_qualification)
        if qualification_file.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("late qualification exceeds native profile limit")
        verify_pack_policy_receipt(qualification_file.read_bytes(),
            self.profile.late_qualification.sha256, binding, pack)
        if (set(binding) != {"status", "component_manifest_sha256", "policy_sha256", "snapshot_id", "canonical_manifest_sha256"}
                or binding["status"] != "PASS" or binding["component_manifest_sha256"] != pack["manifest_sha256"]
                or binding["snapshot_id"] != pack["snapshot_id"]):
            raise ValueError("loaded late pack lacks its exact policy binding")
        late = ComponentIdentity(component="LATE", revision=pack["pack_id"],
            manifest_sha256=pack["manifest_sha256"], policy_sha256=binding["policy_sha256"],
            built_from={"DOCUMENT": pack["snapshot_id"]})
        result = {**self.search.observe(), **self.graph.observe(), "LATE": late.model_dump(mode="json")}
        if set(result) != {"GRAPH", "SEARCH", "DENSE", "LATE", "VISUAL"}:
            raise ValueError("incomplete native serving component set")
        self._profile_fence()
        return result

    def verify_document(self, document):
        """A matching snapshot label alone must not prove matching original bytes."""
        from pydantic import TypeAdapter
        from vkm_evidence.contracts import Sha256
        binding = _bound_json(self.profile.late_policy_binding)
        TypeAdapter(Sha256).validate_python(binding["canonical_manifest_sha256"])
        wanted = (document["revision"], document["manifest_sha256"], document["policy_sha256"])
        packed = (binding["snapshot_id"], binding["canonical_manifest_sha256"], binding["policy_sha256"])
        first = next(iter(self.search.lease.identity.indices.values()))
        indexed = (first.snapshot_id, first.canonical_manifest_sha256, first.policy_sha256)
        if packed != wanted or indexed != wanted or self.graph.document_origin() != wanted:
            raise ValueError("native projections reference different canonical/policy bytes")

    async def observe_services(self):
        self._profile_fence()
        result = await self.services.observe()
        self._profile_fence()
        return result
