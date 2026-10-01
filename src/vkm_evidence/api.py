"""Narrow corpus API extensions; review writes use the durable publisher only."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field

from vkm_corpus.api.envelope import Envelope, Item
from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.api.service import Result
from vkm_evidence.contracts import EvidenceBatch, ReviewDecision
from vkm_evidence.journal import JournalConflict


class ReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)
    base_revision: str = Field(pattern="^[0-9a-f]{64}$")
    review: ReviewDecision


def mount_evidence_routes(app, service, config, respond, Read, Write):
    def context(label):
        value = config.access_contexts.get(label)
        if value is None:
            raise ApiFailure("FORBIDDEN", "evidence principal is not configured")
        return value

    def reader(journal_revision=None, as_of=None, recorded_at=None):
        if service.deps.evidence is None:
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", "evidence journal is not published")
        value = service.deps.evidence
        if journal_revision is not None or recorded_at is not None:
            if journal_revision is None or as_of is None or recorded_at is None:
                raise ValueError("historical read requires exact journal_revision, as_of and recorded_at")
            value = value.at_revision(journal_revision=journal_revision, as_of=as_of, recorded_at=recorded_at)
        return value

    def invoke(func):
        try:
            return func()
        except KeyError:
            raise ApiFailure("NOT_FOUND", "evidence record unavailable") from None
        except PermissionError:
            raise ApiFailure("FORBIDDEN", "evidence access denied") from None
        except (ValueError, JournalConflict):
            raise ApiFailure("INVALID_ARGUMENT", "invalid evidence command or stale version") from None

    def result(kind, data):
        envelope = Envelope(object_id="evidence:" + data.get("generation", data.get("revision", "query")),
            object_kind=kind, object_version=data.get("generation", data.get("revision")),
            review_status="NOT_APPLICABLE", layer="WORKSPACE", payload_form="NORMALIZED", origin="CURATED")
        return Result(item=Item(envelope=envelope, record=data), next_cursor=data.get("next_cursor"))

    # Local dependency aliases cannot appear as forward references when postponed
    # annotation evaluation is active. Assign concrete annotations before registration.
    def page(request: Request, label, kind: str | None = None, cursor: str | None = None,
             limit: Annotated[int, Query(ge=1, le=500)] = 100, as_of: date | None = None,
             entity_id: str | None = None, journal_revision: str | None = None, recorded_at: datetime | None = None):
        data = invoke(lambda: reader(journal_revision, as_of, recorded_at).page(context(label), kind=kind, cursor=cursor, limit=limit,
                                           as_of=as_of, entity_id=entity_id))
        return respond(request, result("EVIDENCE_QUERY", data))
    page.__annotations__["label"] = Read
    app.get("/v1/evidence")(page)

    def get(request: Request, record_id: str, label, journal_revision: str | None = None,
            as_of: date | None = None, recorded_at: datetime | None = None):
        def read():
            if as_of is not None and journal_revision is None:
                raise ValueError("record historical read requires an exact revision")
            return reader(journal_revision, as_of, recorded_at).get(record_id, context(label))
        data = invoke(read)
        return respond(request, result("EVIDENCE_RECORD", data))
    get.__annotations__["label"] = Read
    app.get("/v1/evidence/records/{record_id}")(get)

    def dependencies(request: Request, record_id: str, label, cursor: str | None = None,
                     limit: Annotated[int, Query(ge=1, le=500)] = 500, journal_revision: str | None = None,
                     as_of: date | None = None, recorded_at: datetime | None = None):
        def read():
            if as_of is not None and journal_revision is None:
                raise ValueError("dependency historical read requires an exact revision")
            return reader(journal_revision, as_of, recorded_at).dependencies(record_id, context(label), cursor=cursor, limit=limit)
        data = invoke(read)
        return respond(request, result("EVIDENCE_DEPENDENCIES", data))
    dependencies.__annotations__["label"] = Read
    app.get("/v1/evidence/dependencies/{record_id}")(dependencies)

    def review_packet(request: Request, record_id: str, label, journal_revision: str | None = None,
                      as_of: date | None = None, recorded_at: datetime | None = None):
        from vkm_evidence.review_packet import build_review_packet
        def read():
            if as_of is not None and journal_revision is None:
                raise ValueError("review historical read requires an exact revision")
            selected = reader(journal_revision, as_of, recorded_at)
            revision, records = selected._view()
            metadata = selected.view_metadata(records, revision)
            packet = build_review_packet(selected, service.canon, record_id, context(label))
            selected._unchanged(metadata, records, revision)
            return packet
        data = invoke(read)
        return respond(request, result("EVIDENCE_RECORD", data))
    review_packet.__annotations__["label"] = Read
    app.get("/v1/evidence/review-packet/{record_id}")(review_packet)

    def review(request: Request, body: ReviewCommand, label):
        publisher = service.deps.evidence_publisher
        if publisher is None:
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", "review publisher is not configured")
        data = invoke(lambda: publisher.publish(body.request_id, body.base_revision,
                      EvidenceBatch(records=(body.review,)), context(label)))
        return respond(request, result("EVIDENCE_COMMIT", data))
    review.__annotations__["label"] = Write
    app.post("/v1/evidence/review")(review)
