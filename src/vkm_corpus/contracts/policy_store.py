"""Operator-owned, versioned source policy inventory; no implicit PUBLIC."""
from __future__ import annotations

import json
from pathlib import Path

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy


class SourcePolicyStore:
    def __init__(self, path: Path, sources):
        self.path, self.sources = Path(path), sources

    def read(self) -> dict[str, ResourcePolicy]:
        if self.path.is_symlink():
            raise PermissionError("RESOURCE_POLICY_UNAVAILABLE")
        payload = json.loads(self.path.read_bytes())
        if payload.get("schema") != "vkm-source-policy/1" or set(payload) != {"schema", "policies"}:
            raise ValueError("unsupported source policy inventory")
        return {sid: ResourcePolicy.model_validate(policy) for sid, policy in payload["policies"].items()}

    def for_source(self, source_id: str) -> ResourcePolicy:
        policy = self.read().get(source_id)
        if policy is None:
            raise PermissionError("RESOURCE_POLICY_UNCLASSIFIED")
        return policy

    def require_served_corpus(self, context: AccessContext):
        """Legacy NAV/search packs have no row policy columns yet.

        Admit only a principal authorized for the complete served unit. Partial
        grants fail before backend queries, counts, previews or rerank inference.
        Fine-grained evidence queries apply their own row policies.
        """
        policies = self.read()
        for sid in self.sources():
            if sid not in policies:
                raise PermissionError("RESOURCE_POLICY_UNCLASSIFIED")
            policies[sid].require(context)
