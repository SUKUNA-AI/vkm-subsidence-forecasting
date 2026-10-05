"""Authoritative occurrence verification, including bounded original-text spans."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from vkm_corpus.contracts.access import ResourcePolicy
from vkm_evidence.contracts import ObjectRef


@dataclass(frozen=True)
class OriginalObject:
    ref: ObjectRef
    policy: ResourcePolicy
    text: str | None = None


class ObjectCatalogue:
    def __init__(self, objects: tuple[OriginalObject, ...]):
        self.objects = {(o.ref.snapshot_id, o.ref.object_id): o for o in objects}
        if len(self.objects) != len(objects):
            raise ValueError("duplicate authoritative occurrence")

    def validate(self, ref: ObjectRef) -> ResourcePolicy:
        obj = self.objects.get((ref.snapshot_id, ref.object_id))
        if obj is None:
            raise ValueError("unknown original occurrence")
        identity = ("source_id", "source_sha256", "snapshot_id", "object_id", "object_version",
                    "content_sha256", "locator", "extraction_generation")
        if any(getattr(ref, k) != getattr(obj.ref, k) for k in identity):
            raise ValueError("original occurrence identity mismatch")
        if ref.char_start is not None:
            if obj.text is None or ref.char_end > len(obj.text):
                raise ValueError("original text span unavailable or out of bounds")
            fragment = obj.text[ref.char_start:ref.char_end].encode("utf-8")
            if hashlib.sha256(fragment).hexdigest() != ref.fragment_sha256:
                raise ValueError("original text fragment hash mismatch")
        return obj.policy


def served_object_version(canon, row) -> str | None:
    """The occurrence version an evidence support must cite (owner decision 05.10.2026).

    A signed object is identified by its producing-step ``extraction_signature``.
    A legacy object without a signature (the extraction config of historical
    snapshots was never recorded and is not backfilled) is identified by the
    version the API itself serves for it: ``<content_sha256>@<canonical commit>``.
    That pins the exact bytes of the served snapshot; the extraction signature
    stays UNKNOWN and is never invented. A signed object never matches this form.
    """
    signature = row.get("extraction_signature")
    if signature is not None:
        return signature          # a signed object (an empty or corrupt signature never matches a ref)
    if not hasattr(canon, "commit_of"):
        raise ValueError("canonical commit unavailable for an unsigned object")
    commit = canon.commit_of(row.get("source_id"))
    content = row.get("content_sha256")
    return f"{content}@{commit}" if commit and content else None


def canonical_locator(row):
    """Exact stored native path/raw pointer, otherwise the canonical page ID."""
    return row.get("raw_locator") or row.get("docx_paragraph_path") or row.get("page_id")


def canonical_text(row, kind):
    """The one declared character-span representation for each canonical kind.

    A block uses extracted text, a page its consolidated normalized text. A
    table/formula uses preserved raw markup, never a different normalized field
    merely because its characters happen to match a claimed fragment.
    """
    field = {"BLOCK": "text", "PAGE": "normalized_text", "TABLE": "raw_output",
             "FORMULA": "raw_output", "FIGURE": "caption", "BIBLIOGRAPHY_ENTRY": "text"}.get(kind)
    value = row.get(field) if field else None
    if value is not None and not isinstance(value, str):
        raise ValueError("canonical textual field is not a string")
    return field, value


def canonical_resolver(canon, policy_for_source):
    """Resolve against the served canonical snapshot, never a model's own JSON."""
    def resolve(ref: ObjectRef) -> ResourcePolicy:
        # A packet/API caller supplies a policy callback that authorizes the
        # source here, before any object content is read.
        policy = policy_for_source(ref.source_id)
        snapshot = canon.snapshot() if hasattr(canon, "snapshot") else canon.snapshot_id()
        if canon.snapshot_id() != ref.snapshot_id:
            raise ValueError("original snapshot is not available")
        from vkm_corpus.api.canon import kind_of
        kind = kind_of(ref.object_id)
        if (kind not in {"BLOCK", "PAGE", "TABLE", "FORMULA", "FIGURE", "BIBLIOGRAPHY_ENTRY"}
                or ref.object_id.split(":", 1)[0] != ref.source_id):
            raise ValueError("canonical occurrence identity mismatch")
        row = canon.row(kind, ref.object_id)
        # The version is taken once, before the snapshot re-check, so a row and a commit can never come
        # from two different snapshots.
        version = served_object_version(canon, row) if row is not None else None
        current = canon.snapshot() if hasattr(canon, "snapshot") else canon.snapshot_id()
        if current != snapshot:
            raise ValueError("original snapshot changed during resolution")
        if row is None:
            raise ValueError("original object is unavailable")
        if (row.get("object_id") != ref.object_id or row.get("object_kind", kind) != kind
                or row.get("source_id") != ref.source_id or row.get("source_sha256") != ref.source_sha256
                or row.get("content_sha256") != ref.content_sha256
                or str(row.get("extraction_generation")) != ref.extraction_generation
                or not version or version != ref.object_version):
            raise ValueError("canonical occurrence identity mismatch")
        # Locators must be reconstructed from authoritative metadata. Caller text
        # is not evidence that a printed page or XML path exists.
        locator = canonical_locator(row)
        if ref.locator != locator:
            raise ValueError("canonical locator mismatch")
        _, text = canonical_text(row, kind)
        original = ObjectRef(**ref.model_dump(exclude={"char_start", "char_end", "fragment_sha256"}))
        return ObjectCatalogue((OriginalObject(original, policy, text),)).validate(ref)
    return resolve
