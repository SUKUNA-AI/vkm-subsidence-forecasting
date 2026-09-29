"""Shared identifiers and dataset names of the navigation layer (NAV).

IDs are content-derived and stable across rebuilds of the same snapshot with the same rule version:

* ``SEC-<16 hex>`` — a section of a source: (source_id, method, level, ordinal, normalised title);
* ``TRM-<16 hex>`` — a concept (term): its lemma key (lower case, ё → е, single spaces);
* ``FSY-<16 hex>`` — a symbol of a formula inside its source: (source_id, normalised symbol) — symbols are never
  global (σ in two books may be two different quantities);
* ``FRF-<16 hex>`` — a textual reference to a formula: (citing block, formula);
* ``FPR-<16 hex>`` — a parameter-value candidate near a formula: (formula, symbol, value text);
* ``TOP-<16 hex>`` — a topic of the topic tree: (rule version, level, sorted member section ids) — a rebuild with the
  same members gives the same id, any change of the membership a new one.
* ``DCL-<16 hex>`` — a cluster of near-duplicate passages across sources: its sorted member unit ids.
* ``OCL-<16 hex>`` — a cluster of repeated figures, tables or formulas across sources: the object type and its sorted
  member object ids.
* ``PRM-<16 hex>`` — a parameter-value candidate of the part ``parameters``: (anchor block/table/formula, locator
  — character offset or table cell, property key, value text).
* ``TTR-<16 hex>`` — a pair of the term dictionary (part ``translations``): (relation, language and lemma key of
  both terms).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable

RULE_VERSIONS: dict[str, str] = {
    "sections": "sections_v1",
    "formulas": "formula_context_v1",
    "duplicates": "duplicates_v1",
    "object_duplicates": "object_duplicates_v1",
    "concepts": "concepts_v1",
    "topics": "topics_v1",
    "parameters": "parameters_v1",
    # bilingual term dictionary (agent TR)
    "translations": "term_translations_v1",
}

# derived datasets under $VKM_DATA_ROOT/derived/navigation/<snapshot_id>/<name>.parquet
DATASETS: tuple[str, ...] = (
    "sections", "section_pages",
    "formula_context", "formula_symbols", "formula_refs", "formula_parameters",
    "dup_clusters", "dup_members", "source_overlap",
    "object_dup_clusters", "object_dup_members", "formula_keys", "figure_hashes",
    "terms", "term_mentions", "term_edges",
    "section_aggregates", "section_vectors", "topics", "topic_members", "topic_edges",
    "parameter_candidates", "parameter_summary",
    "term_translations",
)


def _h(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def norm_text(text: str | None) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", t)).strip()


def section_id(source_id: str, method: str, level: int, ordinal: int, title: str | None) -> str:
    return "SEC-" + _h("vkm-nav-section-v1", source_id, method, int(level), int(ordinal), norm_text(title))


def term_id(lemma_key: str) -> str:
    return "TRM-" + _h("vkm-nav-term-v1", norm_text(lemma_key))


def symbol_id(source_id: str, symbol: str) -> str:
    return "FSY-" + _h("vkm-nav-symbol-v1", source_id, symbol)


def formula_ref_id(block_id: str, formula_id: str) -> str:
    return "FRF-" + _h("vkm-nav-fref-v1", block_id, formula_id)


def parameter_id(formula_id: str, symbol: str, value_text: str) -> str:
    return "FPR-" + _h("vkm-nav-fparam-v1", formula_id, symbol, value_text)


def topic_id(level: int, rule_version: str, section_ids: "list[str] | tuple[str, ...]") -> str:
    return "TOP-" + _h("vkm-nav-topic-v1", rule_version, int(level), *sorted(section_ids))


def dup_cluster_id(unit_ids: Iterable[str]) -> str:
    return "DCL-" + _h("vkm-nav-dup-cluster-v1", *sorted(set(unit_ids)))


def object_dup_cluster_id(object_type: str, object_ids: Iterable[str]) -> str:
    return "OCL-" + _h("vkm-nav-object-dup-cluster-v1", object_type, *sorted(set(object_ids)))


def parameter_candidate_id(anchor_id: str, locator: str, property_key: str, value_text: str) -> str:
    return "PRM-" + _h("vkm-nav-param-v1", anchor_id, locator, property_key, value_text)
