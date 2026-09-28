"""Re-embed policy (постановка лаборатории §46, §34–36, §63).

The unit of work is (object_id, text_hash) under one embedding config signature:

* same text hash + same signature → **no inference** (``unchanged``);
* new object or changed text → embed only that object (``to_embed``);
* object no longer in the canon → its rows become ``orphaned`` (kept as history, excluded from the projection).

``text_hash`` is the sha256 of the exact embedded text produced by the text rule of the config (§17); a metadata-only
change of a canonical row (same text) therefore never triggers inference. A different config (model revision, weights,
quantisation, dimension, instruction, text rule …) is a different signature and a different artifact directory: the
plan for it starts from zero, and the old directory is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


@dataclass(frozen=True)
class CanonObject:
    object_id: str
    text_hash: str
    source_id: str | None = None
    page_id: str | None = None
    object_type: str | None = None
    content_sha256: str | None = None


@dataclass
class EmbedPlan:
    config_signature: str
    to_embed: list[CanonObject] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)

    @property
    def inference_count(self) -> int:
        return len(self.to_embed)

    def summary(self) -> dict:
        return {"config_signature": self.config_signature, "objects": len(self.to_embed) + len(self.unchanged),
                "to_embed": len(self.to_embed), "new": len(self.new), "changed": len(self.changed),
                "unchanged": len(self.unchanged), "orphaned": len(self.orphaned)}


def plan(config_signature: str, canon: Iterable[CanonObject], existing: Mapping[str, set[str]]) -> EmbedPlan:
    """``existing``: object_id → text hashes already embedded under ``config_signature`` (see
    :func:`vkm_corpus.embeddings.artifacts.existing_hashes`)."""
    p = EmbedPlan(config_signature)
    seen: set[str] = set()
    for obj in sorted(canon, key=lambda o: o.object_id):
        if obj.object_id in seen:
            raise ValueError(f"duplicate canonical object {obj.object_id}")
        seen.add(obj.object_id)
        hashes = existing.get(obj.object_id)
        if hashes and obj.text_hash in hashes:
            p.unchanged.append(obj.object_id)
            continue
        (p.changed if hashes else p.new).append(obj.object_id)
        p.to_embed.append(obj)
    p.orphaned = sorted(set(existing) - seen)
    return p


def batches(objects: list[CanonObject], size: int) -> list[list[CanonObject]]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    return [objects[i:i + size] for i in range(0, len(objects), size)]
