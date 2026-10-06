"""Re-anchor ("rebase") evidence supports onto a new canonical snapshot.

A canonical snapshot switch makes every pinned ``ObjectRef`` unresolvable: the
resolver accepts only the served ``snapshot_id`` and the exact ``content_sha256``
/ ``object_version`` of that snapshot. This module plans revision+1 records whose
supports point at the new snapshot. It never edits an existing record: each new
revision carries ``supersedes`` = the exact previous version, and publication goes
through the ordinary single-writer journal with the resolver of the NEW snapshot.

For every original support a match status is decided and recorded:

* ``UNCHANGED`` - the canonical text of the referenced object is identical;
* ``QUOTE_FOUND_PRIMARY`` - the record's quote (normalized: NFC, soft hyphens,
  line-break hyphenation, whitespace) occurs in the new canonical text;
* ``QUOTE_FOUND_SECONDARY_LAYER`` - only in a non-primary text layer of the same
  page still present in the new snapshot; the blocks of that layer are added as
  supports, so the layer is named by the anchor itself;
* ``FUZZY`` - best approximate substring (Levenshtein, case-folded) at or above
  the policy threshold;
* ``NOT_FOUND`` - needs owner review; re-pointed only if the policy allows;
* ``BLOCKED`` - structural: object absent, source bytes changed, reviewed record,
  review/admission kind, stale pin, invalid original anchor. Never re-pointed.

Every status other than UNCHANGED/NOT_FOUND requires the numbers of the quote
(ASCII digits with their decimal separators) to equal the numbers of the matched
span widened to whole number tokens: OCR changes digits.

Planning is read-only. Quotes and full records are PRIVATE; ``public_receipt`` and
``review_list`` contain counts, IDs, statuses and hashes only.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal, Mapping

import numpy as np
from pydantic import Field, TypeAdapter, field_validator

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_evidence.contracts import (Claim, EvidenceBatch, EvidenceRecord, FormulaInterpretation, Identifier,
                                    Mention, ObjectRef, Observation, Sha256, StrictModel, VersionRef,
                                    canonical_bytes, record_hash)
from vkm_evidence.journal import MAX_BATCH_BYTES, MAX_BATCH_RECORDS, EvidenceJournal
from vkm_evidence.objects import canonical_locator, canonical_resolver, canonical_text, served_object_version
from vkm_evidence.validation import version_references

RULE_VERSION = "evidence-anchor-rebase/1"
MATCHER = "nfc-hyphenation-whitespace/exact+levenshtein-substring-casefold/1"
STATUSES = ("UNCHANGED", "QUOTE_FOUND_PRIMARY", "QUOTE_FOUND_SECONDARY_LAYER", "FUZZY", "NOT_FOUND", "BLOCKED")
Status = Literal["UNCHANGED", "QUOTE_FOUND_PRIMARY", "QUOTE_FOUND_SECONDARY_LAYER", "FUZZY", "NOT_FOUND", "BLOCKED"]
Baseline = Literal["EXACT", "FUZZY", "ABSENT", "NO_QUOTE", "NOT_APPLICABLE"]
RECORD = TypeAdapter(EvidenceRecord)
# Review, admission and identity decisions bind a reviewer's judgement to exact versions. Moving
# them to new anchors would invent a review of content nobody inspected.
OWNER_DECISION_KINDS = frozenset({"REVIEW_DECISION", "SCIENTIFIC_USE_ADMISSION", "EVIDENCE_TRANSFER",
                                  "ENTITY_RESOLUTION"})
QUOTED_FIELDS = ("historical_quote_field:VERBATIM", "historical_quote_field:PARAPHRASE")
PRIMARY = "PRIMARY"
_NUM = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
_HYPHENS = frozenset("-‐‑")
_BREAKS = frozenset("\n\r  \x0b\x0c")


class RebaseBlocked(ValueError):
    """Stable reason codes only; never quote text, values or machine paths."""


class RebasePolicy(StrictModel):
    fuzzy_threshold: float = Field(90.0, ge=50.0, le=100.0)
    fuzzy_min_chars: int = Field(24, ge=8, le=10_000)
    max_quote_chars: int = Field(4_000, ge=100, le=100_000)
    # HOLD: NOT_FOUND records are listed for review and keep their old version.
    # REPOINT_IF_BASELINE_ABSENT: re-point when the quote was not found in the OLD text either
    #   (the anchor was page identity, never text), except numeric mismatches.
    # REPOINT: re-point every NOT_FOUND support whose object still exists.
    not_found: Literal["HOLD", "REPOINT_IF_BASELINE_ABSENT", "REPOINT"] = "HOLD"


class SnapshotIdentity(StrictModel):
    snapshot_id: Identifier
    manifest_sha256: Sha256 | None = None


class AnchorMatch(StrictModel):
    old: ObjectRef
    status: Status
    reasons: tuple[str, ...] = ()
    new: tuple[ObjectRef, ...] = ()
    repoint: bool = False
    layer: str | None = None
    score: float | None = None
    span: tuple[int, int] | None = None     # offsets in the matched text (new object or concatenated layer)
    baseline: Baseline = "NOT_APPLICABLE"


class RecordRebase(StrictModel):
    record_id: Identifier
    kind: str
    from_version: VersionRef
    status: Status
    action: Literal["REBASED", "NOOP", "HELD"]
    reasons: tuple[str, ...] = ()
    supports: tuple[AnchorMatch, ...]
    to_version: VersionRef | None = None


class StaleDependent(StrictModel):
    record_id: Identifier
    pinned: VersionRef


class RebasePlan(StrictModel):
    schema_version: Literal["vkm-evidence-rebase-plan/1"] = "vkm-evidence-rebase-plan/1"
    rule_version: Literal["evidence-anchor-rebase/1"] = RULE_VERSION
    matcher: str = MATCHER
    base_revision: Sha256
    from_snapshots: tuple[SnapshotIdentity, ...]
    to_snapshot: SnapshotIdentity
    policy: RebasePolicy
    actor: str = Field(min_length=1, max_length=200)
    recorded_at: datetime
    entries: tuple[RecordRebase, ...]
    batch: tuple[EvidenceRecord, ...] = ()
    stale_dependents: tuple[StaleDependent, ...] = ()
    scientific_admission: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("REBASE_TIMESTAMP_REQUIRES_TIMEZONE")
        return value.astimezone(timezone.utc)

    @property
    def sha256(self) -> str:
        return record_hash(self)


class RebaseApproval(StrictModel):
    """Owner decision on one exact plan. Counts make held/re-pointed NOT_FOUND records explicit."""
    schema_version: Literal["vkm-evidence-rebase-approval/1"] = "vkm-evidence-rebase-approval/1"
    plan_sha256: Sha256
    base_revision: Sha256
    to_snapshot_id: Identifier
    publisher: str = Field(min_length=1)
    authority: str = Field(min_length=1)
    acknowledged_held: int = Field(ge=0)
    acknowledged_not_found_repointed: int = Field(0, ge=0)


# ------------------------------------------------------------------------------------------------ text matching
def normalize(text: str, *, fold: bool = False) -> tuple[str, list[int], list[int]]:
    """Matching key plus, per key character, the original [start, end) of its source cluster.

    NFC per grapheme cluster (base + combining marks), soft hyphens dropped, a hyphen
    between letters across a line break joined, whitespace runs collapsed to one space,
    edges stripped. ``fold`` adds Unicode case folding (fuzzy matching only). Digits and
    decimal separators are never rewritten.
    """
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    n, i, space = len(text), 0, None
    while i < n:
        c = text[i]
        if c == "­":
            i += 1
            continue
        if c.isspace():
            if space is None:
                space = i
            i += 1
            continue
        if c in _HYPHENS and space is None and out and out[-1].isalpha():
            j, broken = i + 1, False
            while j < n and (text[j].isspace() or text[j] == "­"):
                broken |= text[j] in _BREAKS
                j += 1
            if broken and j < n and text[j].isalpha():
                i = j
                continue
        j = i + 1
        while j < n and unicodedata.combining(text[j]):
            j += 1
        cluster = unicodedata.normalize("NFC", text[i:j])
        if fold:
            cluster = cluster.casefold()
        if space is not None:
            if out:
                out.append(" ")
                starts.append(space)
                ends.append(space + 1)
            space = None
        for ch in cluster:
            out.append(ch)
            starts.append(i)
            ends.append(j)
        i = j
    return "".join(out), starts, ends


def numbers(text: str) -> list[str]:
    return _NUM.findall(text)


def numbers_at(key: str, start: int, end: int) -> list[str]:
    """Numbers of key[start:end] widened to whole number tokens at both edges."""
    for m in _NUM.finditer(key):
        if m.start() < start < m.end():
            start = m.start()
        if m.start() < end < m.end():
            end = m.end()
        if m.start() >= end:
            break
    return numbers(key[start:end])


def best_substring(pattern: str, text: str) -> tuple[int, int, int]:
    """(Levenshtein distance, start, end) of the best-matching substring of ``text`` (Sellers).

    Deterministic: the first minimal end, then the shortest span ending there.
    """
    if not pattern:
        return 0, 0, 0
    if not text:
        return len(pattern), 0, 0

    def run(p, t):
        P = np.frombuffer(p.encode("utf-32-le"), dtype="<u4")
        T = np.frombuffer(t.encode("utf-32-le"), dtype="<u4")
        n = len(T)
        idx = np.arange(n + 1, dtype=np.int64)
        prev = np.zeros(n + 1, dtype=np.int64)
        tmp = np.empty(n + 1, dtype=np.int64)
        for i in range(len(P)):
            tmp[0] = i + 1
            np.minimum(prev[:-1] + (T != P[i]), prev[1:] + 1, out=tmp[1:])
            prev = np.minimum.accumulate(tmp - idx) + idx
        return prev

    last = run(pattern, text)
    end = int(np.argmin(last))
    distance = int(last[end])
    back = run(pattern[::-1], text[:end][::-1])
    start = end - int(np.argmin(back))
    return distance, start, end


def _exact(qkey: str, cand) -> tuple[tuple[int, int] | None, bool]:
    """First normalized occurrence whose numbers agree; (span in original text, numeric failure seen)."""
    key, starts, ends = cand
    expected, at, failed = numbers(qkey), key.find(qkey), False
    while at >= 0 and qkey:
        if numbers_at(key, at, at + len(qkey)) == expected:
            return (starts[at], ends[at + len(qkey) - 1]), failed
        failed = True
        at = key.find(qkey, at + 1)
    return None, failed


def match_quote(quote: str, candidates: list[tuple[str, str]], policy: RebasePolicy) -> dict:
    """Matching ladder over (layer, original text) candidates; the first one is PRIMARY."""
    qkey = normalize(quote)[0]
    if not qkey:
        return {"kind": "NONE", "reason": "QUOTE_EMPTY"}
    numeric_failure = False
    keyed = [(layer, normalize(text)) for layer, text in candidates]
    for layer, cand in keyed:
        span, failed = _exact(qkey, cand)
        numeric_failure |= failed
        if span is not None:
            return {"kind": "EXACT", "layer": layer, "span": span}
    if len(qkey) < policy.fuzzy_min_chars:
        return {"kind": "NONE", "reason": "NUMERIC_MISMATCH" if numeric_failure else "QUOTE_TOO_SHORT_FOR_FUZZY"}
    if len(qkey) > policy.max_quote_chars:
        return {"kind": "NONE", "reason": "QUOTE_TOO_LONG_FOR_FUZZY"}
    fq = normalize(quote, fold=True)[0]
    expected = numbers(fq)
    scored = []
    for order, (layer, text) in enumerate(candidates):
        key, starts, ends = normalize(text, fold=True)
        if not key:
            continue
        distance, s, e = best_substring(fq, key)
        score = round(100.0 * max(0.0, 1.0 - distance / len(fq)), 2)
        scored.append((-score, order, layer, key, starts, ends, s, e, score))
    best_score = None
    for _, _, layer, key, starts, ends, s, e, score in sorted(scored, key=lambda x: (x[0], x[1])):
        best_score = score if best_score is None else best_score
        if score < policy.fuzzy_threshold:
            break
        if e > s and numbers_at(key, s, e) == expected:
            return {"kind": "FUZZY", "layer": layer, "span": (starts[s], ends[e - 1]), "score": score}
        numeric_failure = True
    return {"kind": "NONE", "reason": "NUMERIC_MISMATCH" if numeric_failure else "BELOW_THRESHOLD",
            "score": best_score}


# ------------------------------------------------------------------------------------------------ canon access
class _Canon:
    """Read-only cache over one CanonStore; rows and layers are fetched once per object/page."""

    def __init__(self, canon):
        self.canon = canon
        info = canon.snapshot()
        self.identity = SnapshotIdentity(snapshot_id=info.snapshot_id, manifest_sha256=info.manifest_sha256)
        self._rows: dict[tuple[str, str], dict | None] = {}
        self._layers: dict[str, list[tuple[str, str, list[tuple[int, int, dict]]]]] = {}

    @property
    def snapshot_id(self) -> str:
        return self.identity.snapshot_id

    def check(self):
        info = self.canon.snapshot()
        if (info.snapshot_id, info.manifest_sha256) != (self.identity.snapshot_id, self.identity.manifest_sha256):
            raise RebaseBlocked("REBASE_CANONICAL_SNAPSHOT_CHANGED")

    def row(self, kind: str, object_id: str) -> dict | None:
        key = (kind, object_id)
        if key not in self._rows:
            self._rows[key] = self.canon.row(kind, object_id)
        return self._rows[key]

    def secondary_layers(self, page_id: str | None, exclude: str):
        """Non-primary text layers of a page: (layer, concatenated text, [(start, end, block row)])."""
        if not page_id:
            return []
        if page_id not in self._layers:
            rows = self.canon.query(
                "SELECT * FROM blocks WHERE page_id = ? AND is_primary_layer IS FALSE "
                "ORDER BY coalesce(text_layer, origin, ''), reading_order NULLS LAST, native_order NULLS LAST, "
                "object_id", [page_id])
            groups: dict[str, list[dict]] = {}
            for row in rows:
                groups.setdefault(row.get("text_layer") or row.get("origin") or "UNKNOWN_LAYER", []).append(row)
            layers = []
            for layer in sorted(groups):
                parts, offsets, cursor = [], [], 0
                for row in groups[layer]:
                    text = row.get("text")
                    if not isinstance(text, str) or not text:
                        continue
                    if parts:
                        cursor += 1
                    offsets.append((cursor, cursor + len(text), row))
                    parts.append(text)
                    cursor += len(text)
                if parts:
                    layers.append((layer, "\n".join(parts), offsets))
            self._layers[page_id] = layers
        return [(layer, text, [o for o in offsets if o[2].get("object_id") != exclude])
                for layer, text, offsets in self._layers[page_id]]


def _kind_of(object_id: str) -> str:
    from vkm_corpus.api.canon import kind_of
    return kind_of(object_id)


def _anchor(canon: _Canon, kind: str, row: dict, object_id: str) -> ObjectRef | None:
    version = served_object_version(canon.canon, row)
    if not version or not row.get("content_sha256") or row.get("extraction_generation") is None:
        return None
    return ObjectRef(source_id=row["source_id"], source_sha256=row["source_sha256"], snapshot_id=canon.snapshot_id,
                     object_id=object_id, object_version=version, content_sha256=row["content_sha256"],
                     locator=canonical_locator(row), extraction_generation=str(row["extraction_generation"]))


def _with_span(ref: ObjectRef, text: str, span: tuple[int, int]) -> ObjectRef:
    s, e = span
    return ref.model_copy(update={"char_start": s, "char_end": e,
                                  "fragment_sha256": hashlib.sha256(text[s:e].encode("utf-8")).hexdigest()})


def record_quote(record) -> str | None:
    """The text a record itself quotes, if it declares one; free propositions are not quotes."""
    if isinstance(record, Claim) and any(q in record.qualifiers for q in QUOTED_FIELDS):
        return record.proposition
    if isinstance(record, Mention):
        return record.literal
    if isinstance(record, Observation) and record.original_value.strip():
        return record.original_value
    if isinstance(record, FormulaInterpretation) and record.representation != "IMAGE_ONLY":
        return record.original_form
    return None


def _all_supports(record) -> tuple[ObjectRef, ...]:
    return (*record.supports, *(s for b in getattr(record, "symbols", ()) for s in b.supports))


# ------------------------------------------------------------------------------------------------ planning
class _Planner:
    def __init__(self, canons, target, policy, authorize, resolve_target):
        self.canons, self.target, self.policy = canons, target, policy
        self.authorize, self.resolve_target = authorize, resolve_target
        self._resolvers = {sid: canonical_resolver(c.canon, authorize) for sid, c in canons.items()}

    def blocked(self, ref, *reasons):
        return AnchorMatch(old=ref, status="BLOCKED", reasons=tuple(reasons))

    def match(self, record, ref: ObjectRef) -> AnchorMatch:
        source = self.canons.get(ref.snapshot_id)
        if source is None:
            return self.blocked(ref, "SNAPSHOT_NOT_PROVIDED")
        try:
            self._resolvers[ref.snapshot_id](ref)
        except PermissionError:
            raise
        except Exception:  # noqa: BLE001 - any failure of the authoritative check is a code
            return self.blocked(ref, "ORIGINAL_ANCHOR_UNRESOLVED")
        kind = _kind_of(ref.object_id)
        old_row = source.row(kind, ref.object_id)
        new_row = self.target.row(kind, ref.object_id)
        if new_row is None:
            return self.blocked(ref, "OBJECT_ABSENT_IN_TARGET")
        if new_row.get("source_sha256") != ref.source_sha256 or new_row.get("source_id") != ref.source_id:
            return self.blocked(ref, "SOURCE_IDENTITY_CHANGED")
        base = _anchor(self.target, kind, new_row, ref.object_id)
        if base is None:
            return self.blocked(ref, "TARGET_OBJECT_VERSION_UNAVAILABLE")
        _, old_text = canonical_text(old_row, kind)
        _, new_text = canonical_text(new_row, kind)
        if old_text is not None and old_text == new_text or (
                old_text is None and new_text is None and old_row.get("content_sha256") == new_row.get("content_sha256")):
            new = base if ref.char_start is None else base.model_copy(update={
                "char_start": ref.char_start, "char_end": ref.char_end, "fragment_sha256": ref.fragment_sha256})
            return self.verified(AnchorMatch(old=ref, status="UNCHANGED", new=(new,), repoint=True, layer=PRIMARY))
        quote = old_text[ref.char_start:ref.char_end] if ref.char_start is not None and old_text else record_quote(record)
        if not quote or not quote.strip():
            return self.not_found(ref, base, "NO_QUOTE", "NO_QUOTE")
        baseline = match_quote(quote, [(PRIMARY, old_text or "")], self.policy)["kind"]
        baseline = {"EXACT": "EXACT", "FUZZY": "FUZZY"}.get(baseline, "ABSENT")
        candidates = [(PRIMARY, new_text or "")]
        layers = self.target.secondary_layers(new_row.get("page_id") or (ref.object_id if kind == "PAGE" else None),
                                              exclude=ref.object_id)
        candidates += [(layer, text) for layer, text, _ in layers]
        found = match_quote(quote, candidates, self.policy)
        if found["kind"] == "NONE":
            return self.not_found(ref, base, found["reason"], baseline, score=found.get("score"))
        layer, span = found["layer"], found["span"]
        status = ("FUZZY" if found["kind"] == "FUZZY" else
                  "QUOTE_FOUND_PRIMARY" if layer == PRIMARY else "QUOTE_FOUND_SECONDARY_LAYER")
        extra = dict(layer=layer, score=found.get("score"), span=span, baseline=baseline)
        if layer == PRIMARY:
            if ref.char_start is None:
                return self.verified(AnchorMatch(old=ref, status=status, new=(base,), repoint=True, **extra))
            if status == "FUZZY":
                return AnchorMatch(old=ref, status=status, reasons=("SPAN_REQUIRES_EXACT_MATCH",), new=(base,), **extra)
            return self.verified(AnchorMatch(old=ref, status=status, new=(_with_span(base, new_text, span),),
                                             repoint=True, **extra))
        offsets = next(o for name, _, o in layers if name == layer)
        covering = [(s, e, row) for s, e, row in offsets if s < span[1] and e > span[0]]
        blocks = []
        for s, e, row in covering:
            anchor = _anchor(self.target, "BLOCK", row, row["object_id"])
            if anchor is None:
                return AnchorMatch(old=ref, status=status, reasons=("LAYER_BLOCK_VERSION_UNAVAILABLE",),
                                   new=(base,), **extra)
            blocks.append((s, e, row, anchor))
        if not blocks:
            return AnchorMatch(old=ref, status=status, reasons=("LAYER_BLOCK_UNRESOLVED",), new=(base,), **extra)
        if ref.char_start is not None:
            if status == "FUZZY" or len(blocks) != 1:
                return AnchorMatch(old=ref, status=status, reasons=("SPAN_REQUIRES_EXACT_MATCH",), new=(base,),
                                   **extra)
            s, _, row, anchor = blocks[0]
            text = row["text"]
            new = (_with_span(anchor, text, (span[0] - s, span[1] - s)),)
        else:
            new = (base, *(anchor for *_, anchor in blocks))
        return self.verified(AnchorMatch(old=ref, status=status, new=new, repoint=True, **extra))

    def not_found(self, ref, base, reason, baseline, score=None):
        allowed = (self.policy.not_found == "REPOINT" or (
            self.policy.not_found == "REPOINT_IF_BASELINE_ABSENT" and baseline in {"ABSENT", "NO_QUOTE"}
            and reason != "NUMERIC_MISMATCH"))
        match = AnchorMatch(old=ref, status="NOT_FOUND", reasons=(reason,), new=(base,), repoint=allowed,
                            score=score, baseline=baseline, layer=None)
        return self.verified(match) if allowed else match

    def verified(self, match: AnchorMatch) -> AnchorMatch:
        """Every proposed anchor must pass the authoritative resolver of the target snapshot."""
        try:
            for ref in match.new:
                self.resolve_target(ref)
        except PermissionError:
            raise
        except Exception:  # noqa: BLE001
            return match.model_copy(update={"status": "BLOCKED", "repoint": False,
                                            "reasons": (*match.reasons, "TARGET_ANCHOR_UNRESOLVED")})
        return match


def _worst(statuses: Iterable[str]) -> str:
    return max(statuses, key=STATUSES.index)


def _rewrite(record, mapping: dict[ObjectRef, tuple[ObjectRef, ...]], deps: dict[str, VersionRef],
             previous: dict[str, VersionRef], actor: str, recorded_at: datetime):
    def refs(values):
        result = []
        for ref in values:
            for new in mapping.get(ref, (ref,)):
                if new not in result:
                    result.append(new)
        return [r.model_dump(mode="json") for r in result]

    payload = record.model_dump(mode="json")
    payload["supports"] = refs(record.supports)
    for i, binding in enumerate(getattr(record, "symbols", ())):
        payload["symbols"][i]["supports"] = refs(binding.supports)
    for field in ("depends_on", "references"):
        payload[field] = [(deps[r.record_id] if r.record_id in deps and r == previous[r.record_id] else r)
                          .model_dump(mode="json") for r in getattr(record, field)]
    payload.update(revision=record.revision + 1, supersedes=record.version_ref.model_dump(mode="json"),
                   actor=actor, recorded_at=recorded_at.isoformat())
    return RECORD.validate_python(payload)


def plan_rebase(journal: EvidenceJournal, *, canons: Iterable, target, policy: RebasePolicy,
                source_policy: Callable[[str], ResourcePolicy], context: AccessContext,
                recorded_at: datetime, target_snapshot_id: str, revision: str | None = None) -> RebasePlan:
    """Read-only plan of revision+1 records re-anchored on ``target``; nothing is written."""
    base_revision = revision or journal.revision
    target_canon = _Canon(target)
    if target_canon.snapshot_id != target_snapshot_id:
        raise RebaseBlocked("TARGET_SNAPSHOT_MISMATCH")
    sources = {target_canon.snapshot_id: target_canon}
    for canon in canons:
        if canon is target:
            continue
        wrapped = _Canon(canon)
        if wrapped.snapshot_id in sources:
            raise RebaseBlocked("DUPLICATE_SOURCE_SNAPSHOT")
        sources[wrapped.snapshot_id] = wrapped
    policies: dict[str, ResourcePolicy] = {}

    def authorize(sid):
        policy_ = source_policy(sid)
        if not isinstance(policy_, ResourcePolicy):
            raise PermissionError("RESOURCE_POLICY_UNCLASSIFIED")
        policy_.require(context)
        if policies.setdefault(sid, policy_) != policy_:
            raise RebaseBlocked("REBASE_SOURCE_POLICY_CHANGED")
        return policy_

    planner = _Planner(sources, target_canon, policy, authorize, canonical_resolver(target, authorize))
    current = journal.records(base_revision)
    for record in current.values():
        record.policy.require(context)   # a denied record aborts the whole plan; no partial counts

    matches: dict[str, tuple[AnchorMatch, ...]] = {}
    reasons: dict[str, list[str]] = {}
    for rid in sorted(current):
        record = current[rid]
        refs = list(dict.fromkeys(_all_supports(record)))
        if not refs:
            continue
        found = tuple(planner.match(record, ref) for ref in refs)
        matches[rid], reasons[rid] = found, []
        if all(m.new == (m.old,) for m in found):
            continue                                      # already anchored on the target: NOOP
        if record.kind in OWNER_DECISION_KINDS:
            reasons[rid].append("KIND_REQUIRES_OWNER_DECISION")
        if record.review_state != "UNREVIEWED":
            reasons[rid].append("REVIEWED_RECORD_REQUIRES_NEW_REVIEW")
        for m in found:
            for ref in m.new:
                if not record.policy.preserves(authorize(ref.source_id)):
                    reasons[rid].append("POLICY_WIDENING")
        for ref in version_references(record):
            if ref.record_id not in current or current[ref.record_id].version_ref != ref:
                reasons[rid].append("STALE_DEPENDENCY_PIN")

    def movable(rid):
        found = matches[rid]
        return (not reasons[rid] and all(m.repoint for m in found)
                and not all(m.new == (m.old,) for m in found))

    candidates = {rid for rid in matches if movable(rid)}
    # Dependencies inside the batch are re-pinned to their new versions: topological order; cycles are held.
    order, state = [], {}

    def visit(rid, trail):
        if state.get(rid) == "done":
            return True
        if state.get(rid) == "active":
            for member in trail[trail.index(rid):]:
                reasons[member].append("DEPENDENCY_CYCLE")
            return False
        state[rid] = "active"
        ok = True
        for ref in version_references(current[rid]):
            if ref.record_id in candidates and ref.record_id != rid:
                ok &= visit(ref.record_id, trail + [ref.record_id])
        state[rid] = "done"
        order.append(rid)
        return ok

    for rid in sorted(candidates):
        visit(rid, [rid])
    previous = {rid: r.version_ref for rid, r in current.items()}
    new_versions: dict[str, VersionRef] = {}
    rebased: dict[str, object] = {}
    for rid in order:
        if reasons[rid]:          # a held dependency keeps its current version, so its pin stays valid
            continue
        mapping = {m.old: m.new for m in matches[rid]}
        record = _rewrite(current[rid], mapping, new_versions, previous, context.principal, recorded_at)
        rebased[rid], new_versions[rid] = record, record.version_ref

    entries = []
    for rid in sorted(matches):
        found = matches[rid]
        status = _worst(m.status for m in found)
        if rid in rebased:
            action = "REBASED"
        elif all(m.new == (m.old,) for m in found):
            action = "NOOP"
        else:
            action = "HELD"
        entries.append(RecordRebase(record_id=rid, kind=current[rid].kind, from_version=previous[rid], status=status,
                                    action=action, reasons=tuple(dict.fromkeys(
                                        [*reasons[rid], *(x for m in found for x in m.reasons)])),
                                    supports=found, to_version=new_versions.get(rid)))
    stale = sorted({(rid, ref) for rid, record in current.items() if rid not in rebased
                    for ref in version_references(record)
                    if ref.record_id in rebased and ref == previous[ref.record_id]},
                   key=lambda x: (x[0], x[1].record_id))
    target_canon.check()
    for wrapped in sources.values():
        wrapped.check()
    for sid, policy_ in policies.items():
        if source_policy(sid) != policy_:
            raise RebaseBlocked("REBASE_SOURCE_POLICY_CHANGED")
    if journal.revision != base_revision and revision is None:
        raise RebaseBlocked("REBASE_JOURNAL_ADVANCED_DURING_PLAN")
    batch = tuple(rebased[rid] for rid in sorted(rebased))
    if len(batch) > MAX_BATCH_RECORDS or len(canonical_bytes(EvidenceBatch(records=batch))) > MAX_BATCH_BYTES:
        raise RebaseBlocked("REBASE_BATCH_LIMIT")
    return RebasePlan(base_revision=base_revision, actor=context.principal, recorded_at=recorded_at,
                      from_snapshots=tuple(sorted((c.identity for sid, c in sources.items()
                                                   if sid != target_canon.snapshot_id),
                                                  key=lambda x: x.snapshot_id)),
                      to_snapshot=target_canon.identity, policy=policy, entries=tuple(entries), batch=batch,
                      stale_dependents=tuple(StaleDependent(record_id=rid, pinned=ref) for rid, ref in stale))


# ------------------------------------------------------------------------------------------------ reports
def _counter(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def public_receipt(plan: RebasePlan, *, inputs_sha256: Mapping[str, str] | None = None,
                   tool_sha256: str | None = None) -> dict:
    """Counts, identities and hashes only: no quote text, no record payloads, no machine paths."""
    supports = [m for e in plan.entries for m in e.supports]
    per_source: dict[str, dict[str, int]] = {}
    for m in supports:
        bucket = per_source.setdefault(m.old.source_id, {})
        bucket[m.status] = bucket.get(m.status, 0) + 1
    actions = _counter(e.action for e in plan.entries)
    batch_bytes = canonical_bytes(EvidenceBatch(records=plan.batch)) if plan.batch else b""
    return {
        "schema": "vkm-evidence-rebase-receipt/1",
        "status": "PLANNED" if plan.batch else "NOOP" if actions.keys() <= {"NOOP"} else "NOTHING_REBASABLE",
        "tool": {"module": "vkm_evidence.rebase", "rule_version": plan.rule_version, "matcher": plan.matcher,
                 "module_sha256": tool_sha256 or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "journal_base_revision": plan.base_revision,
        "from_snapshots": [s.model_dump(mode="json") for s in plan.from_snapshots],
        "to_snapshot": plan.to_snapshot.model_dump(mode="json"),
        "inputs_sha256": dict(sorted((inputs_sha256 or {}).items())),
        "policy": plan.policy.model_dump(mode="json"),
        "recorded_at": plan.recorded_at.isoformat(),
        "actor": plan.actor,
        "counts": {
            "records_with_supports": len(plan.entries),
            "records_by_action": actions,
            "records_by_status": _counter(e.status for e in plan.entries),
            "supports_by_status": _counter(m.status for m in supports),
            "support_reasons": _counter(r for m in supports for r in m.reasons),
            "record_reasons": _counter(r for e in plan.entries for r in e.reasons),
            "baseline_by_status": _counter(f"{m.baseline}->{m.status}" for m in supports),
            "secondary_layers": _counter(m.layer for m in supports if m.status != "UNCHANGED" and m.layer
                                         and m.layer != PRIMARY),
            "not_found_repointed": sum(1 for m in supports if m.status == "NOT_FOUND" and m.repoint),
            "stale_dependents": len(plan.stale_dependents),
        },
        "per_source": {sid: dict(sorted(v.items())) for sid, v in sorted(per_source.items())},
        "outputs": {"plan_sha256": plan.sha256, "batch_records": len(plan.batch),
                    "batch_sha256": hashlib.sha256(batch_bytes).hexdigest() if plan.batch else None,
                    "request_id": f"evidence-rebase:{plan.sha256}" if plan.batch else None},
        "review_state": "UNCHANGED_UNREVIEWED_ONLY",
        "scientific_admission": "NOT_ESTABLISHED",
    }


def review_list(plan: RebasePlan) -> dict:
    """Records needing owner attention: IDs, statuses, reason codes, object IDs. No text."""
    items = []
    for e in plan.entries:
        if e.action == "NOOP" or (e.action == "REBASED" and e.status in {"UNCHANGED", "QUOTE_FOUND_PRIMARY"}):
            continue
        items.append({"record_id": e.record_id, "kind": e.kind, "action": e.action, "status": e.status,
                      "reasons": list(e.reasons),
                      "supports": [{"object_id": m.old.object_id, "status": m.status, "repoint": m.repoint,
                                    "layer": m.layer, "score": m.score, "baseline": m.baseline,
                                    "anchors": [r.object_id for r in m.new]} for m in e.supports]})
    return {"schema": "vkm-evidence-rebase-review/1", "plan_sha256": plan.sha256,
            "to_snapshot_id": plan.to_snapshot.snapshot_id, "items": items,
            "stale_dependents": [{"record_id": s.record_id, "pinned_record_id": s.pinned.record_id}
                                 for s in plan.stale_dependents]}


def held_count(plan: RebasePlan) -> int:
    return sum(1 for e in plan.entries if e.action == "HELD")


def not_found_repointed(plan: RebasePlan) -> int:
    return sum(1 for e in plan.entries if e.action == "REBASED" for m in e.supports if m.status == "NOT_FOUND")


# ------------------------------------------------------------------------------------------------ outputs and publish
def _public_root() -> Path:
    return Path(__file__).resolve().parents[2]


def write_outputs(plan: RebasePlan, private_dir: Path, *, public_dir: Path | None = None,
                  inputs_sha256: Mapping[str, str] | None = None) -> dict:
    """Immutable PRIVATE package (plan, batch, map) plus public-safe receipt and review list."""
    from vkm_evidence.migration import _path, _write_once

    private_dir = Path(private_dir).absolute()
    if private_dir.resolve().is_relative_to(_public_root().resolve()):
        raise RebaseBlocked("PRIVATE_REBASE_STORAGE_REQUIRED")
    _path(private_dir, ".")
    receipt = public_receipt(plan, inputs_sha256=inputs_sha256)
    review = review_list(plan)
    artifacts = {"plan.json": canonical_bytes(plan),
                 "map.json": canonical_bytes([e.model_dump(mode="json") for e in plan.entries])}
    if plan.batch:
        artifacts["batch.json"] = canonical_bytes(EvidenceBatch(records=plan.batch))
    public = {"receipt.json": canonical_bytes(receipt) + b"\n", "review.json": canonical_bytes(review) + b"\n"}
    for name, data in {**artifacts, **public}.items():
        _write_once(_path(private_dir, name), data)
    if public_dir is not None:
        for name, data in public.items():
            _write_once(_path(Path(public_dir).absolute(), name), data)
    return {**receipt, "artifacts_sha256": {name: hashlib.sha256(data).hexdigest()
                                            for name, data in {**artifacts, **public}.items()}}


def publish_rebase(journal: EvidenceJournal, plan: RebasePlan, approval: RebaseApproval, *, owners: frozenset[str],
                   context: AccessContext, replan: Callable[[], RebasePlan], after_commit=None) -> dict:
    """Publish one owner-approved, freshly reproduced plan through the ordinary journal gate."""
    plan = RebasePlan.model_validate_json(plan.model_dump_json())
    approval = RebaseApproval.model_validate_json(approval.model_dump_json())
    if (approval.plan_sha256 != plan.sha256 or approval.base_revision != plan.base_revision
            or approval.to_snapshot_id != plan.to_snapshot.snapshot_id or approval.authority not in owners
            or approval.publisher != context.principal or plan.actor != context.principal):
        raise RebaseBlocked("REBASE_OWNER_APPROVAL_REQUIRED")
    if approval.acknowledged_held != held_count(plan) or \
            approval.acknowledged_not_found_repointed != not_found_repointed(plan):
        raise RebaseBlocked("REBASE_APPROVAL_COUNTS_MISMATCH")
    if not plan.batch:
        raise RebaseBlocked("REBASE_NOTHING_TO_PUBLISH")
    if journal.object_validator is None:
        raise RebaseBlocked("TARGET_RESOLVER_REQUIRED")
    if replan().sha256 != plan.sha256:
        raise RebaseBlocked("REBASE_PLAN_NO_LONGER_REPRODUCIBLE")
    committed = journal.publish("evidence-rebase:" + plan.sha256, plan.base_revision,
                                EvidenceBatch(records=plan.batch), context, after_commit=after_commit)
    return {"schema": "vkm-evidence-rebase-publish-receipt/1", "status": "COMMITTED", "plan_sha256": plan.sha256,
            "approval_sha256": record_hash(approval), "journal": committed,
            "to_snapshot_id": plan.to_snapshot.snapshot_id, "records": len(plan.batch),
            "held": held_count(plan), "scientific_admission": "NOT_ESTABLISHED"}
