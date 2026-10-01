"""Version-bound, policy-filtered evidence reads and temporal/dependency views."""
from __future__ import annotations

import base64
import json
import threading
from copy import deepcopy
from datetime import date

from vkm_corpus.contracts.access import AccessContext
from vkm_evidence.contracts import ReviewDecision, ScientificUseAdmission, record_hash
from vkm_evidence.journal import EvidenceJournal
from vkm_evidence.validation import admission_state
from vkm_evidence.temporal import HistoricalReadContext, records_at


class EvidenceReader:
    def __init__(self, journal: EvidenceJournal, *, source_policy=None, historical: HistoricalReadContext | None = None):
        self.journal = journal
        self.source_policy = source_policy
        self._lock = threading.Lock()
        self._revision, self._records = None, {}
        self._history_revision, self._history_records = None, {}
        self.historical = historical
        self._historical_records = None

    def _current_view(self):
        with self._lock:
            revision = self.journal.revision
            if revision != self._revision:
                self._records = self.journal.records(revision)
                self._revision = revision
            return revision, self._records

    def _view(self):
        current_revision, current = self._current_view()
        if self.historical is None:
            return current_revision, current
        revision = self.historical.journal_revision
        if revision not in {c["revision"] for c in self.journal.commits(current_revision)}:
            raise ValueError("historical revision is not retained in the current journal")
        with self._lock:
            if self._historical_records is None:
                self.journal.records(revision)  # validate the complete correction chain
                self._historical_records = records_at(self.journal.iter_records(revision), self.historical)
            return revision, self._historical_records

    def at_revision(self, *, journal_revision, as_of, recorded_at):
        historical = HistoricalReadContext(journal_revision=journal_revision, as_of=as_of, recorded_at=recorded_at)
        return EvidenceReader(self.journal, source_policy=self.source_policy, historical=historical)

    def view_metadata(self, records, revision):
        current_revision, current = self._current_view()
        if self.historical is None and current_revision != revision:
            raise ValueError("evidence generation changed during read")
        body = {"journal_revision": revision,
                "historical": self.historical.model_dump(mode="json") if self.historical else None,
                "current_journal_revision": current_revision,
                "current_record_policy_sha256": record_hash({rid: r.policy.model_dump(mode="json")
                    for rid, r in sorted(current.items())}),
                "source_policy_sha256": self.policy_fingerprint(records, revision)}
        return {"view_context": body, "view_context_sha256": record_hash(body)}

    def _unchanged(self, metadata, records, revision):
        current = self.view_metadata(records, revision)
        if current["view_context"]["source_policy_sha256"] != metadata["view_context"]["source_policy_sha256"]:
            raise PermissionError("source policy changed during read")
        if current != metadata:
            raise ValueError("evidence view or current policy changed during read")

    def view(self):
        # Public library callers receive isolated models. Internal read paths
        # serialize bounded results and never deep-copy the whole corpus per query.
        revision, records = self._view()
        return revision, deepcopy(records)

    def _versions(self, records, revision=None):
        """Current and actually retained historical pins, owned by this reader.

        A corrected dependency may use an original absent from its current
        version. Access revocation must also cover that pinned original.
        """
        from vkm_evidence.validation import version_references
        current = {r.version_ref: r for r in records.values()}
        if all(ref in current for r in records.values() for ref in version_references(r)):
            return current
        revision = revision or self._revision
        with self._lock:
            if self._history_revision == revision:
                return self._history_records
            versions = dict(current)
            if hasattr(self.journal, "iter_records"):
                versions.update((r.version_ref, r) for r in self.journal.iter_records(revision))
            self._history_revision, self._history_records = revision, versions
            return versions

    def policy_fingerprint(self, records, revision=None):
        sources = {s.source_id for r in self._versions(records, revision).values()
                   for s in (*r.supports, *(s for b in getattr(r, "symbols", ()) for s in b.supports))}
        if self.source_policy is None:
            return record_hash({"mode": "RECORD_POLICY_ONLY"})
        return record_hash({sid: self.source_policy(sid).model_dump(mode="json") for sid in sorted(sources)})

    def _visible(self, records, context, revision=None):
        from collections import deque
        from vkm_evidence.validation import version_references
        versions = self._versions(records, revision)
        current = self._current_view()[1] if self.historical is not None else records
        allowed = {ref for ref, r in versions.items() if r.policy.permits(context)
                   and (r.record_id not in current or current[r.record_id].policy.permits(context))}
        if self.source_policy is not None:
            policies = {}
            for ref in tuple(allowed):
                record = versions[ref]
                supports = (*record.supports, *(s for b in getattr(record, "symbols", ()) for s in b.supports))
                for s in supports:
                    if s.source_id not in policies:
                        policies[s.source_id] = self.source_policy(s.source_id)
                if any(not policies[s.source_id].permits(context) for s in supports):
                    allowed.remove(ref)
        # A visible relation/review must not disclose a denied target's identity.
        reverse = {}
        for version, record in versions.items():
            for ref in version_references(record):
                reverse.setdefault(ref, set()).add(version)
        todo = deque(set(reverse) - allowed)
        visited = set(todo)
        while todo:
            for rid in reverse.get(todo.popleft(), ()):
                allowed.discard(rid)
                if rid not in visited:
                    visited.add(rid)
                    todo.append(rid)
        visible = {rid: r for rid, r in records.items() if r.version_ref in allowed}
        return self._known_at(visible, self.historical.as_of) if self.historical else visible

    @staticmethod
    def _known_at(visible, as_of):
        from collections import deque
        from vkm_evidence.temporal import usable_at
        from vkm_evidence.validation import version_references
        allowed = {rid for rid, r in visible.items() if usable_at(r, as_of) is True}
        reverse = {}
        for rid, r in visible.items():
            for ref in version_references(r):
                reverse.setdefault(ref.record_id, set()).add(rid)
                if ref.record_id not in visible or visible[ref.record_id].version_ref != ref:
                    allowed.discard(rid)
        todo = deque(set(visible) - allowed)
        visited = set(todo)
        while todo:
            for rid in reverse.get(todo.popleft(), ()):
                allowed.discard(rid)
                if rid not in visited:
                    visited.add(rid)
                    todo.append(rid)
        return {rid: visible[rid] for rid in allowed}

    def get(self, record_id: str, context: AccessContext) -> dict:
        revision, records = self._view()
        metadata = self.view_metadata(records, revision)
        visible = self._visible(records, context, revision)
        if record_id not in visible:
            raise KeyError("record not found")  # denied and absent have the same result
        record = visible[record_id]
        result = {"generation": revision, "record": record.model_dump(mode="json"),
                  "record_sha256": record_hash(record), **metadata}
        if isinstance(record, ScientificUseAdmission):
            result["current_admission"] = admission_state(record, self._current_view()[1], context)
            if self.historical:
                result["historical_admission"] = admission_state(record, records, context)
        self._unchanged(metadata, records, revision)
        return result

    def page(self, context: AccessContext, *, kind: str | None = None, cursor: str | None = None,
             limit: int = 100, as_of: date | None = None, entity_id: str | None = None) -> dict:
        if not 1 <= limit <= 500:
            raise ValueError("limit outside 1..500")
        revision, records = self._view()
        metadata = self.view_metadata(records, revision)
        if self.historical and as_of is not None and as_of != self.historical.as_of:
            raise ValueError("historical as_of differs from pinned read context")
        binding = record_hash({"generation": revision, "principal": context.model_dump(mode="json"),
                               "kind": kind, "as_of": as_of.isoformat() if as_of else None, "entity": entity_id,
                               "view_context_sha256": metadata["view_context_sha256"]})
        after = ""
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError()
                token = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                if set(token) != {"binding", "after"} or token["binding"] != binding or not isinstance(token["after"], str):
                    raise ValueError()
                after = token["after"]
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid or stale evidence cursor") from exc
        visible = self._visible(records, context, revision)
        if as_of:
            visible = self._known_at(visible, as_of)
        eligible = []
        for rid, record in sorted(visible.items()):
            if kind and record.kind != kind:
                continue
            if entity_id and entity_id not in {getattr(record, "entity_id", None), *getattr(record, "participants", ())}:
                continue
            eligible.append((rid, record))
        selected = [(rid, r) for rid, r in eligible if rid > after][:limit + 1]
        has_more = len(selected) > limit
        selected = selected[:limit]
        next_cursor = None
        if has_more:
            next_cursor = base64.urlsafe_b64encode(json.dumps({"binding": binding, "after": selected[-1][0]},
                separators=(",", ":")).encode()).decode().rstrip("=")
        result = {"generation": revision, "items": [r.model_dump(mode="json") for _, r in selected], **metadata,
                "total_permitted": len(eligible), "has_more": has_more, "next_cursor": next_cursor,
                "counts": {"publications": "NOT_DERIVED", "independent_observations": "NOT_DERIVED"}}
        self._unchanged(metadata, records, revision)
        return result

    def dependencies(self, record_id: str, context: AccessContext, *, limit=500, cursor=None) -> dict:
        from vkm_evidence.validation import version_references
        if not 1 <= limit <= 500:
            raise ValueError("limit outside 1..500")
        revision, records = self._view()
        metadata = self.view_metadata(records, revision)
        visible = self._visible(records, context, revision)
        if record_id not in visible:
            raise KeyError("record not found")
        direct = tuple(dict.fromkeys(version_references(visible[record_id])))
        reverse = {}
        for rid, record in visible.items():
            for ref in version_references(record):
                reverse.setdefault(ref.record_id, set()).add(rid)
        affected, todo = set(), [record_id]
        while todo:
            target = todo.pop()
            for rid in reverse.get(target, ()):
                if rid not in affected:
                    affected.add(rid)
                    todo.append(rid)
        affected.discard(record_id)
        binding = record_hash({"generation": revision, "principal": context.model_dump(mode="json"),
            "view_context_sha256": metadata["view_context_sha256"], "record": record_id, "route": "dependencies"})
        after = ""
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError()
                token = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                if set(token) != {"binding", "after"} or token["binding"] != binding or not isinstance(token["after"], str):
                    raise ValueError()
                after = token["after"]
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid or stale dependency cursor") from exc
        selected = [rid for rid in sorted(affected) if rid > after][:limit + 1]
        has_more = len(selected) > limit
        selected = selected[:limit]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"binding": binding, "after": selected[-1]},
            separators=(",", ":")).encode()).decode().rstrip("=") if has_more else None
        result = {"generation": revision, "direct": [r.model_dump(mode="json") for r in direct], **metadata,
                "computational": [r.model_dump(mode="json") for r in visible[record_id].depends_on],
                "affected": selected, "total_affected": len(affected), "has_more": has_more,
                "next_cursor": next_cursor, "record_sha256": record_hash(visible[record_id])}
        self._unchanged(metadata, records, revision)
        return result
