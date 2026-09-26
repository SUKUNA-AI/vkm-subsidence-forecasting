"""Source registry linkage and corpus coverage statuses.

The registered scientific binaries live ONLY in the private resources repository
(``00_registry/SOURCE_REGISTER.csv`` there). The public repository stores metadata-level
records (ids, bibliographic data, coverage) and never the binaries or long verbatim quotes.
"""
from __future__ import annotations

import csv
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.base import SOURCE_ID_RE


class CoverageLevel(str, Enum):
    FULLY_REVIEWED = "FULLY_REVIEWED"
    RELEVANT_SECTIONS_REVIEWED = "RELEVANT_SECTIONS_REVIEWED"
    LOW_RELEVANCE_CONFIRMED = "LOW_RELEVANCE_CONFIRMED"
    UNREADABLE_BLOCKER = "UNREADABLE_BLOCKER"
    RETIRED_NOT_EVIDENCE = "RETIRED_NOT_EVIDENCE"        # e.g. VKM-SRC-022 legacy package
    SUPERSEDED_BY_COPY = "SUPERSEDED_BY_COPY"            # e.g. VKM-SRC-013 zip → VKM-SRC-025
    PARTIAL_IN_PROGRESS = "PARTIAL_IN_PROGRESS"          # allowed only in intermediate snapshots
    UNSEEN = "UNSEEN"                                    # forbidden in a final coverage master


FINAL_ALLOWED = frozenset(CoverageLevel) - {CoverageLevel.UNSEEN, CoverageLevel.PARTIAL_IN_PROGRESS}


class SourceKind(str, Enum):
    MONOGRAPH = "monograph"
    TEXTBOOK = "textbook"
    TEACHING_MANUAL = "teaching_manual"
    PRACTICE_MANUAL = "practice_manual"
    TRAINING_MANUAL = "training_manual"
    NORMATIVE = "normative"
    DISSERTATION = "dissertation"
    DISSERTATION_ABSTRACT = "dissertation_abstract"
    THESIS = "thesis"                  # ВКР
    JOURNAL_ARTICLE = "journal_article"
    PRESENTATION = "presentation"
    DATA_ARCHIVE = "data_archive"
    WEB = "web"
    OTHER = "other"


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_id: str
    title: str | None = None
    authors: str | None = None
    year: str | None = None
    kind: SourceKind | str | None = None
    scope: str | None = None
    coverage_level: CoverageLevel = CoverageLevel.UNSEEN
    coverage_basis: str | None = Field(None, description="why this coverage level is justified")

    @field_validator("source_id")
    @classmethod
    def _sid(cls, v: str) -> str:
        if not SOURCE_ID_RE.match(v):
            raise ValueError(f"invalid source id '{v}'")
        return v


def load_register_ids(resources_root: str | Path) -> set[str]:
    """Registered ids from the private SOURCE_REGISTER.csv (requires access to the private repo)."""
    p = Path(resources_root) / "00_registry" / "SOURCE_REGISTER.csv"
    with open(p, encoding="utf-8") as f:
        return {r["resource_id"] for r in csv.DictReader(f)}


def coverage_errors(records: list[SourceRecord], final: bool = True) -> list[str]:
    errs = []
    for r in records:
        if final and r.coverage_level not in FINAL_ALLOWED:
            errs.append(f"{r.source_id}: coverage level {r.coverage_level.value} not allowed in a final coverage master")
        if r.coverage_level is not CoverageLevel.UNSEEN and not (r.coverage_basis or "").strip():
            errs.append(f"{r.source_id}: coverage level {r.coverage_level.value} without a stated basis")
    ids = [r.source_id for r in records]
    for d in {i for i in ids if ids.count(i) > 1}:
        errs.append(f"duplicate source id {d}")
    return errs
