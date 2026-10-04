"""Declarative registry of canonical datasets: model, class, schema version, keys, partitioning.

Schema versions stay ``0.x`` (MINOR is incompatible) until the freeze allowed by H-46.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from vkm_corpus.contracts import models as m
from vkm_corpus.contracts.vocab import DatasetClass, EnvelopeProfile, ObjectKind

SCHEMA_VERSION = "0.1.0"

# Exact pre-locator schemas from Git (schemas/corpus/arrow_schemas.json before
# 0.1.1). This is a narrow read migration, not permission to accept arbitrary schemas.
HISTORICAL_LOCATOR_SCHEMAS = {
    "figures": ("0.1.0", "898cc0fa9f8ed1aadc3a6f71f635b885cf06cf9f28d595e70e5fed2b19a5cc50"),
    "tables": ("0.1.0", "087678688e16af432f289c530c3ba76dad7c8ff1ba6a8bd7f050ff0502b1a1e8"),
    "formulas": ("0.1.0", "1f94c66a512a6cdd9a617b822754a01bf0e2cb327905067ad73ca160d39e2c0f"),
}

# Exact tables schema before the additive unreviewed continuation declaration.
HISTORICAL_CONTINUATION_SCHEMAS = {
    "tables": ("0.1.1", "6d4ec517fb8373617acfc092b4e1d75dac75ddf7e237f9cb34802a0c3d3ea487"),
}


def historical_omissions(name: str, version: str | None) -> frozenset[str]:
    """Columns absent from an exact historical version; never mutate an old hash by NULL padding."""
    if name in HISTORICAL_LOCATOR_SCHEMAS and version == HISTORICAL_LOCATOR_SCHEMAS[name][0]:
        return frozenset({"raw_locator", "continuation_provenance"} if name == "tables" else {"raw_locator"})
    if name in HISTORICAL_CONTINUATION_SCHEMAS and version == HISTORICAL_CONTINUATION_SCHEMAS[name][0]:
        return frozenset({"continuation_provenance"})
    return frozenset()


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    model: type[m.RowModel]
    dataset_class: DatasetClass
    profile: EnvelopeProfile
    primary_key: tuple[str, ...]
    sort_key: tuple[str, ...]
    object_kind: str | None = None
    version: str = SCHEMA_VERSION

    @property
    def stored(self) -> bool:
        return self.dataset_class != DatasetClass.DERIVED_VIEW

    @property
    def partition_keys(self) -> tuple[str, ...]:
        if self.dataset_class == DatasetClass.HEAD_PER_SOURCE:
            return ("source_id", "run")
        return ("run",)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(self.model.model_fields)


def _spec(name, model, cls, profile, pk, sort, kind=None) -> DatasetSpec:
    # 0.1.1 appends a nullable source locator; old rows remain readable as UNKNOWN.
    version = "0.1.1" if name in ("figures", "tables", "formulas") else SCHEMA_VERSION
    if name == "tables":
        version = "0.1.2"
    return DatasetSpec(name, model, DatasetClass(cls), EnvelopeProfile(profile), tuple(pk), tuple(sort), kind, version)


R, H, L, V = (DatasetClass.REGISTRY_GLOBAL, DatasetClass.HEAD_PER_SOURCE, DatasetClass.APPEND_LOG,
              DatasetClass.DERIVED_VIEW)

DATASETS: dict[str, DatasetSpec] = {s.name: s for s in [
    # registry (one REGISTRY commit rebuilds all of them)
    _spec("sources", m.SourceRow, R, "REG", ["source_id"], ["source_id"], ObjectKind.SOURCE),
    _spec("works", m.WorkRow, R, "REG", ["work_id"], ["work_id"], ObjectKind.WORK),
    _spec("source_work_links", m.SourceWorkLinkRow, R, "LINK", ["object_id"],
          ["source_id", "link_type", "object_id"], ObjectKind.SOURCE_WORK_LINK),
    _spec("work_relations", m.WorkRelationRow, R, "LINK", ["object_id"], ["from_work_id", "relation", "to_work_id"],
          ObjectKind.WORK_RELATION),
    _spec("source_relations", m.SourceRelationRow, R, "LINK", ["object_id"],
          ["from_source_id", "relation", "to_source_id", "object_id"], ObjectKind.SOURCE_RELATION),
    _spec("authors", m.AuthorRow, R, "REG", ["author_id"], ["author_id"], ObjectKind.AUTHOR),
    _spec("work_authors", m.WorkAuthorRow, R, "LINK", ["object_id"], ["work_id", "ordinal"], ObjectKind.WORK_AUTHOR),
    _spec("venues", m.VenueRow, R, "REG", ["venue_id"], ["venue_id"], ObjectKind.VENUE),
    # document objects (one partition per source in the head commit)
    _spec("documents", m.DocumentRow, H, "DOC", ["object_id"], ["object_id"], ObjectKind.DOCUMENT),
    _spec("pages", m.PageRow, H, "DOC", ["page_id"], ["page_index"], ObjectKind.PAGE),
    _spec("blocks", m.BlockRow, H, "DOC", ["object_id"], ["page_id", "text_layer", "reading_order", "object_id"],
          ObjectKind.BLOCK),
    _spec("figures", m.FigureRow, H, "DOC", ["object_id"], ["page_id", "bbox_y0", "bbox_x0", "object_id"],
          ObjectKind.FIGURE),
    _spec("tables", m.TableRow, H, "DOC", ["object_id"], ["page_id", "bbox_y0", "bbox_x0", "object_id"],
          ObjectKind.TABLE),
    _spec("formulas", m.FormulaRow, H, "DOC", ["object_id"], ["page_id", "bbox_y0", "bbox_x0", "object_id"],
          ObjectKind.FORMULA),
    _spec("bibliography_entries", m.BibliographyEntryRow, H, "DOC", ["object_id"],
          ["page_id", "ordinal_in_list", "object_id"], ObjectKind.BIBLIOGRAPHY_ENTRY),
    # journals (history: every file of every run is part of a snapshot)
    _spec("processing_runs", m.ProcessingRunRow, L, "LOG", ["processing_run_id", "record_phase"],
          ["processing_run_id", "record_phase"]),
    _spec("processing_steps", m.ProcessingStepRow, L, "LOG", ["step_id"],
          ["source_id", "page_index", "stage", "attempt", "step_id"]),
    _spec("errors", m.ErrorRow, L, "LOG", ["error_id"], ["source_id", "page_id", "created_at", "error_id"]),
    _spec("artifacts", m.ArtifactRow, L, "LOG", ["artifact_id", "created_by_run_id"],
          ["artifact_id", "created_by_run_id"]),
    # derived by SQL only (duckdb/sql); the model documents the view's columns
    _spec("bibliography_links", m.BibliographyLinkRow, V, "LINK", ["object_id"],
          ["citing_source_id", "entry_id", "cited_work_id"], ObjectKind.BIBLIOGRAPHY_LINK),
]}

REGISTRY_DATASETS: tuple[str, ...] = tuple(n for n, s in DATASETS.items() if s.dataset_class == R)
DOCUMENT_DATASETS: tuple[str, ...] = tuple(n for n, s in DATASETS.items() if s.dataset_class == H)
LOG_DATASETS: tuple[str, ...] = tuple(n for n, s in DATASETS.items() if s.dataset_class == L)
STORED_DATASETS: tuple[str, ...] = tuple(n for n, s in DATASETS.items() if s.stored)
# objects that live on a page (have object ids of the page-object grammar)
PAGE_OBJECT_DATASETS: tuple[str, ...] = ("blocks", "figures", "tables", "formulas", "bibliography_entries")
# columns that change on every re-run of the same producer without changing the content: excluded from
# content fingerprints (no-op commits, determinism K-02). Version bumps are content changes (a new commit, K-07).
VOLATILE_COLUMNS: frozenset[str] = frozenset({"created_at", "processing_run_id"})


def dataset(name: str) -> DatasetSpec:
    try:
        return DATASETS[name]
    except KeyError:
        raise KeyError(f"unknown dataset {name!r}; known: {sorted(DATASETS)}") from None


def iter_datasets(stored_only: bool = False) -> Iterator[DatasetSpec]:
    for spec in DATASETS.values():
        if stored_only and not spec.stored:
            continue
        yield spec


def dataset_for_model(model: type) -> DatasetSpec:
    for spec in DATASETS.values():
        if spec.model is model:
            return spec
    raise KeyError(f"no dataset for model {model.__name__}")
