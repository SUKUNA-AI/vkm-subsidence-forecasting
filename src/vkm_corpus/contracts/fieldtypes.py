"""Annotated field types of the contracts: Arrow width markers and ID/hash constraints.

The Arrow type of every field is derived from its annotation (``vkm_corpus.contracts.arrow``). Integer widths are
always explicit — a bare ``int`` is rejected by the schema test — and timestamps are always timezone-aware UTC.
The markers carry Arrow type *names* (strings), so the contracts import without pyarrow.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

from vkm_corpus.ids import grammar as g


class ArrowType:
    """Annotated marker: explicit Arrow type of a field, by name (``int16``, ``int32``, ``int64``, ``float64``...)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ArrowType({self.name!r})"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


Int16 = Annotated[int, ArrowType("int16"), Field(ge=-32768, le=32767)]
Int32 = Annotated[int, ArrowType("int32"), Field(ge=-(2 ** 31), le=2 ** 31 - 1)]
Int64 = Annotated[int, ArrowType("int64")]
Float64 = Annotated[float, ArrowType("float64")]
UtcDatetime = Annotated[datetime, ArrowType("timestamp[us, tz=UTC]"), AfterValidator(_utc)]
Date32 = Annotated[date, ArrowType("date32")]

Sha256Hex = Annotated[str, StringConstraints(pattern=g.SHA256_HEX)]
ArtifactId = Annotated[str, StringConstraints(pattern=g.ARTIFACT_ID)]
SourceId = Annotated[str, StringConstraints(pattern=g.SOURCE_ID)]
DocumentId = Annotated[str, StringConstraints(pattern=g.DOCUMENT_ID)]
PageId = Annotated[str, StringConstraints(pattern=g.PAGE_ID)]
PageObjectId = Annotated[str, StringConstraints(pattern=g.OBJECT_ID)]
WorkId = Annotated[str, StringConstraints(pattern=g.WORK_ID)]
AuthorId = Annotated[str, StringConstraints(pattern=g.AUTHOR_ID)]
VenueId = Annotated[str, StringConstraints(pattern=g.VENUE_ID)]
RunId = Annotated[str, StringConstraints(pattern=g.RUN_ID)]
StepId = Annotated[str, StringConstraints(pattern=g.STEP_ID)]
ErrorId = Annotated[str, StringConstraints(pattern=g.ERROR_ID)]
CommitId = Annotated[str, StringConstraints(pattern=g.COMMIT_ID)]
SemVer = Annotated[str, StringConstraints(pattern=g.SEMVER)]
LanguageCode = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}$")]
RelPath = Annotated[str, StringConstraints(min_length=1), AfterValidator(g.check_relative_path)]
LogicalRef = Annotated[str, StringConstraints(min_length=1), AfterValidator(g.check_logical_ref)]
