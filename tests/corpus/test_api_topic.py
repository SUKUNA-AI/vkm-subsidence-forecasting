"""Topic dossier (``reconstruct_topic``) on synthetic data only: the API's synthetic canon, a synthetic NAV build
(sections, formulas — served by the real query functions) and a synthetic catalogue pack; retrieval is stubbed.

Covers the parts a–g, NAV-only and full mode, family/source dedup, snippets, the hard budget with its trimmed report,
determinism, graceful degradation (no retrieval, no NAV, no concepts, no catalogues), the envelope, the HTTP routes
and the MCP tool. No corpus text."""
from __future__ import annotations

import asyncio
import csv
import json

import pytest

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("pydantic")
pytest.importorskip("PIL")

from vkm_corpus.api import topic  # noqa: E402
from vkm_corpus.api.envelope import Envelope  # noqa: E402
from vkm_corpus.api.errors import ApiFailure  # noqa: E402
from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service  # noqa: E402
from vkm_corpus.catalogues import pack as cpack  # noqa: E402
from vkm_corpus.catalogues.store import CatalogueStore  # noqa: E402
from vkm_corpus.navigation import store as nav_store  # noqa: E402
from vkm_corpus.navigation.formulas import definition_key  # noqa: E402

NAV_SNAP = "snap-nav-topic-test"
S1, S2, S3, S4 = (f"SEC-000000000000000{i}" for i in (1, 2, 3, 4))
QUERY = "оседание земной поверхности"


# ---------------------------------------------------------------------------------------------------- synthetic NAV
def _table(rows: list[dict], types: dict[str, pa.DataType]) -> pa.Table:
    return pa.table({k: pa.array([r.get(k) for r in rows], type=t) for k, t in types.items()})


def _write_nav(nav_dir, formula_id: str) -> None:
    s, i16, i32, f64, b = pa.string(), pa.int16(), pa.int32(), pa.float64(), pa.bool_()
    sec_types = {"section_id": s, "source_id": s, "work_id": s, "parent_section_id": s, "level": i16, "ordinal": i32,
                 "numbering": s, "title": s, "title_path": s, "page_start_id": s, "page_end_id": s,
                 "page_start_index": i32, "page_end_index": i32, "method": s, "confidence": f64,
                 "heading_block_id": s, "rule_version": s}

    def sec(sid, src, parent, level, ordinal, title, path, first, last):
        return {"section_id": sid, "source_id": src, "work_id": "VKM-WRK-001", "parent_section_id": parent,
                "level": level, "ordinal": ordinal, "numbering": None, "title": title, "title_path": path,
                "page_start_id": f"{src}:p{first:04d}", "page_end_id": f"{src}:p{last:04d}",
                "page_start_index": first, "page_end_index": last, "method": "PDF_OUTLINE", "confidence": 1.0,
                "heading_block_id": None, "rule_version": "sections_v1"}

    sections = [sec(S1, "VKM-SRC-001", None, 1, 1, "Глава 1. Оседание земной поверхности",
                    "Глава 1. Оседание земной поверхности", 1, 2),
                sec(S2, "VKM-SRC-001", S1, 2, 2, "1.1 Мульда сдвижения",
                    "Глава 1. Оседание земной поверхности › 1.1 Мульда сдвижения", 2, 2),
                sec(S3, "VKM-SRC-001", None, 1, 3, "Глава 2. Формулы оседания", "Глава 2. Формулы оседания", 3, 3),
                sec(S4, "VKM-SRC-002", None, 1, 1, "Введение", "Введение", 1, 1)]
    pq.write_table(_table(sections, sec_types), nav_dir / "sections.parquet")
    pages = [(S1, "VKM-SRC-001", 1), (S2, "VKM-SRC-001", 2), (S3, "VKM-SRC-001", 3), (S4, "VKM-SRC-002", 1)]
    pq.write_table(_table([{"section_id": a, "page_id": f"{b}:p{c:04d}", "source_id": b, "page_index": c,
                            "rule_version": "sections_v1"} for a, b, c in pages],
                          {"section_id": s, "page_id": s, "source_id": s, "page_index": i32, "rule_version": s}),
                   nav_dir / "section_pages.parquet")
    ctx = {"formula_id": formula_id, "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0003", "page_index": 3,
           "kind": "DISPLAY", "latex_len": 12, "equation_number": "2.1", "equation_number_raw": "(2.1)",
           "number_method": "LATEX_TAG", "number_block_id": None, "extra_numbers": [], "section_id": S3,
           "intro_block_id": None, "where_block_ids": [], "next_block_id": None, "host_block_id": None,
           "n_symbols": 2, "n_defined_symbols": 1, "n_refs_in": 1, "n_parameters": 1,
           "rule_version": "formula_context_v1", "review_status": "AUTO_EXTRACTED_UNREVIEWED"}
    pq.write_table(_table([ctx], {**{k: s for k in ctx}, "page_index": i32, "latex_len": i32,
                                  "extra_numbers": pa.list_(s), "where_block_ids": pa.list_(s), "n_symbols": i32,
                                  "n_defined_symbols": i32, "n_refs_in": i32, "n_parameters": i32}),
                   nav_dir / "formula_context.parquet")
    definition = "оседание земной поверхности"
    sym = {"formula_id": formula_id, "source_id": "VKM-SRC-001", "symbol": "η", "symbol_key": "η", "symbol_id": "sym1",
           "role": "LHS", "n_occurrences": 1, "in_formula": True, "definition": definition, "unit": "мм",
           "definition_block_id": None, "definition_symbol_raw": "η", "definition_key": definition_key(definition),
           "match_method": "EXACT", "rule_version": "formula_symbols_v1", "review_status": "AUTO_EXTRACTED_UNREVIEWED"}
    pq.write_table(_table([sym], {**{k: s for k in sym}, "n_occurrences": i32, "in_formula": b}),
                   nav_dir / "formula_symbols.parquet")
    ref = {"ref_id": "ref1", "block_id": "VKM-SRC-001:p0002:b000000000001", "source_id": "VKM-SRC-001",
           "page_id": "VKM-SRC-001:p0002", "formula_id": formula_id, "number_text": "(2.1)", "number_key": "2.1",
           "ref_type": "MENTION", "cue": "по формуле", "resolution": "UNIQUE", "n_candidates": 1, "n_mentions": 1,
           "char_start": 3, "citing_formula_id": None, "rule_version": "formula_refs_v1",
           "review_status": "AUTO_EXTRACTED_UNREVIEWED"}
    pq.write_table(_table([ref], {**{k: s for k in ref}, "n_candidates": i32, "n_mentions": i32, "char_start": i32}),
                   nav_dir / "formula_refs.parquet")
    par = {"parameter_id": "par1", "formula_id": formula_id, "source_id": "VKM-SRC-001", "symbol": "k",
           "symbol_raw": "k", "value_text": "0,5", "value": 0.5, "value_min": None, "value_max": None, "unit": None,
           "block_id": None, "in_formula": False, "context_kind": "WHERE_BLOCK", "rule_version": "formula_params_v1",
           "review_status": "AUTO_EXTRACTED_UNREVIEWED"}
    pq.write_table(_table([par], {**{k: s for k in par}, "value": f64, "value_min": f64, "value_max": f64,
                                  "in_formula": b}), nav_dir / "formula_parameters.parquet")


def fake_explore(canon_block: str):
    def explore(con, term, limit=20):
        return {"query": term, "match": {"term_id": "TRM-00000000000000a1", "lemma": "оседание земной поверхности",
                                         "language": "ru", "kind": "NP", "df_units": 3, "df_sources": 2, "tf": 9,
                                         "seed": True, "community": None, "surface_forms": [], "match": "lemma_key"},
                "alternatives": [], "focus": None,
                "definitions": [{"block_id": canon_block, "page_id": "VKM-SRC-001:p0001", "source_id": "VKM-SRC-001",
                                 "rule": "def_nazyvaetsya"}],
                "same_as": [], "broader": [{"term_id": "TRM-00000000000000a2", "lemma": "оседание", "df_units": 5}],
                "narrower": [],
                "neighbours": [{"term_id": "TRM-00000000000000b2", "lemma": "мульда сдвижения", "npmi": 0.6,
                                "score": 0.4, "n_units": 3, "n_sources": 2,
                                "example_page_ids": ["VKM-SRC-001:p0002"]}][:limit],
                "top_units": [{"unit_id": S2, "unit_kind": "SECTION", "section_id": S2, "source_id": "VKM-SRC-001",
                               "tf": 2, "tfidf": 1.5, "page_ids": ["VKM-SRC-001:p0002"]}],
                "top_sources": [{"source_id": "VKM-SRC-001", "n_units": 2, "tf": 5}], "note": "synthetic"}
    return explore


# ---------------------------------------------------------------------------------------------------- catalogues
CATALOGUES = {
    "catalogues/physics/physics_coverage_and_execution_matrix.csv": [
        {"process_id": "PC-01", "process": "Оседание земной поверхности над выработками", "domain": "surface",
         "group": "SURF", "causal_role": "итог", "causal_path_to_subsidence": "выемка → оседание",
         "required_variables": "u_z",
         "required_parameters": "плотность пород (ρ); модуль деформации; отметки реперов; схема ходов; g",
         "governing_equations": "η = f(t) (синтетическое)", "readiness": "PARAMETERS_MISSING",
         "data_gaps": "GAP-900: синтетический пробел", "status": "FACT", "scope": "SKRU1", "confidence": "HIGH",
         "source_ids": "VKM-SRC-001", "locator": "VKM-SRC-001 p.1", "vn_ids": "EV-VN-S001-0001",
         "best_evidence_scope": "SKRU1", "math_model_ids_FORMULAS_draft": "MM-T-001", "notes": "0,45 как напечатано"},
        {"process_id": "PC-02", "process": "Ползучесть каменной соли", "domain": "rheology", "group": "RHEO",
         "causal_role": "", "causal_path_to_subsidence": "", "required_variables": "", "required_parameters": "",
         "governing_equations": "", "readiness": "EVIDENCE_WEAK", "data_gaps": "", "status": "FACT",
         "scope": "VKM_REGIONAL", "confidence": "MEDIUM", "source_ids": "VKM-SRC-002", "locator": "", "vn_ids": "",
         "best_evidence_scope": "", "math_model_ids_FORMULAS_draft": "", "notes": ""},
    ],
    "catalogues/physics/process_evidence_links.csv": [
        {"process_id": "PC-01", "vn_id": "EV-VN-S001-0001", "kind": "parameter", "source_id": "VKM-SRC-001",
         "locator": "VKM-SRC-001 p.1", "pdf_page": "1", "status": "FACT", "evidence_type": "LAB_TEST",
         "scope": "VKM_REGIONAL", "scale": "LAB", "confidence": "HIGH", "role": "KEY"},
        {"process_id": "PC-01", "vn_id": "EV-VN-S001-0002", "kind": "monitoring_observation",
         "source_id": "VKM-SRC-001", "locator": "VKM-SRC-001 p.2", "pdf_page": "2", "status": "FACT",
         "evidence_type": "FIELD_OBSERVATION", "scope": "SKRU1", "scale": "FIELD", "confidence": "HIGH",
         "role": "SUPPORT"},
    ],
    "catalogues/physics/physics_conflicts.csv": [
        {"conflict_id": "PCF-01", "variable": "плотность пород", "conflict_type": "VALUE", "values_kept": "A | B",
         "handling": "DISCRETE_SET", "affected_processes": "PC-01", "status": "UNKNOWN", "scope": "SKRU1",
         "confidence": "HIGH", "source_ids": "VKM-SRC-001", "locator": "", "vn_ids": "EV-VN-S001-0001"}],
    "catalogues/mathematics/MATHEMATICAL_MODEL_REGISTRY.csv": [
        {"model_id": "MM-T-001", "name_ru": "Синтетическая модель оседания", "physical_meaning_ru": "", "group": "T",
         "math_class": "ALGEBRAIC", "equation_plain": "η = k·t", "scale": "FIELD", "site_applicability": "SKRU1",
         "source_ids": "VKM-SRC-001", "locator": "VKM-SRC-001 p.3", "status": "FACT", "confidence": "HIGH",
         "vn_ids": "", "intended_role": ""}],
    "catalogues/mathematics/formula_conflicts.csv": [
        {"conflict_id": "FC-001", "model_ids": "MM-T-001", "source_ids": "VKM-SRC-001", "locators": "", "vn_ids": "",
         "conflict_type": "FORM", "form_a": "a", "form_b": "b", "description_ru": "синтетический конфликт форм",
         "resolution_needed": "нет", "status": "OPEN"}],
    "catalogues/causal/causal_graph_nodes.csv": [
        {"node_id": "N_A", "label_ru": "Выемка", "process_ids": "", "status": "FACT"},
        {"node_id": "N_B", "label_ru": "Оседание поверхности", "process_ids": "PC-01", "status": "FACT"}],
    "catalogues/causal/causal_graph_edges.csv": [
        {"edge_id": "CE-001", "from_node": "N_A", "to_node": "N_B", "edge_type": "physical", "mechanism": "синтетика",
         "strength": "STRONG", "process_ids": "PC-01", "status": "FACT", "scope": "SKRU1"}],
    "catalogues/observations/observation_operator_design.csv": [
        {"operator_id": "OO-01", "modality": "levelling", "observable": "превышения реперов",
         "predicted_output": "оседания", "operator_status": "DESIGN", "skru1_data_availability": "PARTIAL",
         "linked_processes": "PC-01", "status": "FACT", "scope": "SKRU1"}],
    "evidence/materials/mechanics_evidence_catalog.csv": [
        {"row_id": "MR-MECH-9001", "category": "density_unit_weight", "variable": "density",
         "vn_ids": "EV-VN-S001-0001", "value_as_printed": "2,1", "site_scope": "VKM_REGIONAL", "scale": "LAB",
         "source_id": "VKM-SRC-001", "status": "FACT"}],
    "evidence/materials/rheology_evidence_catalog.csv": [
        {"rheo_id": "MR-RHEO-T1", "law_name": "синтетический закон ползучести", "law_family": "power law",
         "lithology": "каменная соль", "scale": "LAB", "site_scope": "VKM_REGIONAL", "source_id": "VKM-SRC-002",
         "locator": "p.9", "vn_ids": "", "status": "FACT", "planned_role": "", "applicability_SKRU1": ""}],
    "evidence/sources/SOURCE_COVERAGE_MASTER.csv": [
        {"source_id": "VKM-SRC-001", "title": "синтетический источник", "document_type": "dissertation",
         "geographic_scope": "синтетика", "mine_attribution": "SKRU1"}],
}


def write_catalogues(repo) -> None:
    for rel, rows in CATALOGUES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


# ---------------------------------------------------------------------------------------------------- environment
@pytest.fixture()
def env(tmp_path):
    service, canon, fakes = synthetic_service(tmp_path / "canon")
    root = tmp_path / "root"
    nav_dir = root / "derived" / "navigation" / NAV_SNAP
    nav_dir.mkdir(parents=True)
    _write_nav(nav_dir, canon.ids["formula"])
    nav_store.pack(nav_dir)
    nav_store.publish(root, NAV_SNAP)
    service.deps.nav = nav_store.NavStore(root, canonical_db=canon.duckdb_path,
                                          functions={"explore_concept": fake_explore(canon.ids["block"])})
    repo = tmp_path / "public"
    write_catalogues(repo)
    cpack.pack(repo, tmp_path / "pack", commit="ab" * 20)
    cpack.publish(root, tmp_path / "pack")
    service.deps.catalogues = CatalogueStore(root)
    service.deps.hybrid = None
    return service, canon, root


def _dossier(service, query=QUERY, **kw):
    result = service.reconstruct_topic(query, **kw)
    env_ = result.item.envelope
    Envelope.model_validate(env_.model_dump())
    return result.item.record, [w.code for w in result.warnings], env_


# ---------------------------------------------------------------------------------------------------- tests
def test_text_helpers():
    assert topic.query_stems("Гипотезы начальных напряжений") == ["гипотез", "начальн", "напряжен"]
    assert topic.stem("закладка") == topic.stem("закладки") == "закладк"
    assert topic.begins("закладочный", "закладк") and topic.begins("механический", topic.stem("механика"))
    assert not topic.begins("механизация", topic.stem("механика"))            # no over-short stems
    assert topic.query_stems("механика и закладка") == ["механик", "закладк"]          # stop words dropped
    s = topic.snippet("x " * 200 + "начальное поле напряжений в массиве " + "y " * 200, ["началь", "напряже"])
    assert s and len(s) <= 200 and "начальное поле напряжений" in s and s.startswith("…") and s.endswith("…")
    assert topic.snippet("ничего общего", ["заклад"]) is None
    assert topic._split_parameters("ρ(z; по стратонам); модуль E; g; 1,2–1,5 (цит.)") == ["ρ(z; по стратонам)",
                                                                                           "модуль E"]
    for cell, name in (("UCS смеси 3,5–4,0 МПа в 10 лет", "UCS смеси"), ("Δb=5 м (дефолт)", "Δb"),
                       ("k_зап (учебные 0,72–0,85; другое 0,75)", "k_зап"),
                       ("λ как набор гипотез: 0,45 (NORMATIVE) | 0,6 (БКПРУ-2)", "λ как набор гипотез"),
                       ("h_з, закон (PC-38), подпор", "h_з, закон, подпор"),
                       ("ρ(z) по стратонам", "ρ(z) по стратонам")):
        assert topic._param_name(cell) == name                             # names only: no printed values in gaps
    assert topic._adjacent(["началь", "напряже"], ["начальное", "напряжение", "массива"])
    assert not topic._adjacent(["началь", "напряже"], ["напряжение", "в", "начальной", "точке"])


def test_nav_only_dossier_covers_all_parts(env):
    service, canon, _root = env
    record, warnings, envelope = _dossier(service, paraphrases=["мульда сдвижения"])
    assert envelope.object_kind == "TOPIC_DOSSIER" and envelope.layer == "PROJECTION"
    assert envelope.review_status == "AUTO_EXTRACTED_UNREVIEWED" and envelope.origin == "DERIVED"
    assert envelope.projection.engine == "navigation" and envelope.projection.build_id == NAV_SNAP
    assert record["mode"] == "NAV_ONLY" and "RETRIEVAL_UNAVAILABLE" in warnings
    assert record["inputs"]["catalogues"]["pack_id"] == "ab" * 6
    assert [f["kind"] for f in record["formulations"]] == ["query", "paraphrase", "neighbours"]
    assert record["formulations"][2]["text"] == f"{QUERY} мульда сдвижения"   # widened by the concept neighbour
    # (b) one section per branch: S1 (title «Оседание земной поверхности») keeps its child S2 (the paraphrase's
    #     title) as related; the concept alone adds no section
    secs = {s["section_id"]: s for s in record["sections"]}
    assert S1 in secs and S2 not in secs and S2 in secs[S1]["related_sections"] and S3 in secs
    assert {s["tier"] for s in record["sections"]} == {"CORE"}               # synthetic sources: VKM_REGIONAL
    assert record["pages"]["core"][0]["page_id"] == "VKM-SRC-001:p0001" and record["pages"]["rest"] == []
    assert secs[S1]["pages"] == {"first_id": "VKM-SRC-001:p0001", "last_id": "VKM-SRC-001:p0002",
                                 "first_index": 1, "last_index": 2}
    unit = secs[S1]["units"][0]
    assert unit["page_id"] == "VKM-SRC-001:p0001" and len(unit["snippet"]) <= 200
    assert all(s["review_status"] == "AUTO_EXTRACTED_UNREVIEWED" for s in record["sections"])
    # (c) the formula of S3 (by its pages and by the meaning of η), with symbols and parameter candidates
    f = record["formulas"][0]
    assert f["formula_id"] == canon.ids["formula"] and f["equation_number"] == "2.1" and f["section_id"] == S3
    assert f["symbols"][0]["symbol"] == "η" and f["parameters"] == [{"symbol": "k", "value_text": "0,5", "unit": None}]
    assert any(m.startswith("def:") for m in f["match"]) and f["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    # (d) concept
    assert record["concept"]["match"]["term_id"] == "TRM-00000000000000a1"
    assert record["concept"]["neighbours"][0]["lemma"] == "мульда сдвижения"
    # (e) provenance + CITES: work 001 cites 050 (synthetic DOI link)
    src = record["sources"][0]
    assert src["source_id"] == "VKM-SRC-001" and src["work_id"] == "VKM-WRK-001" and src["n_sections"] >= 1
    assert src["evidence_catalogue"] == {"document_type": "dissertation", "geographic_scope": "синтетика",
                                         "mine_attribution": "SKRU1"}
    assert record["citations"]["most_cited"][0]["work_id"] == "VKM-WRK-050"
    # (f) catalogues
    cat = record["catalogue"]
    p = cat["processes"][0]
    assert p["process_id"] == "PC-01" and p["readiness"] == "PARAMETERS_MISSING" and p["n_evidence"] == 2
    assert p["evidence"][0]["vn_id"] == "EV-VN-S001-0001"                     # KEY first
    assert {e["status"] for e in p["evidence"]} == {"FACT"} and {e["scope"] for e in p["evidence"]} == {
        "VKM_REGIONAL", "SKRU1"}
    assert [m["model_id"] for m in cat["models"]] == ["MM-T-001"]
    assert {c["conflict_id"] for c in cat["conflicts"]} == {"PCF-01", "FC-001"}
    assert cat["causal"][0]["edge_id"] == "CE-001" and cat["operators"][0]["operator_id"] == "OO-01"
    # (g) gaps: never values; density linked only to another scope, the modulus has no record, the curated gap
    gaps = {(g["process_id"], g["coverage"]): g for g in record["gaps"]}
    assert gaps[("PC-01", "LINKED_OTHER_SCOPE")]["parameter"].startswith("плотность")
    assert gaps[("PC-01", "LINKED_OTHER_SCOPE")]["evidence_vn_ids"] == ["EV-VN-S001-0001"]
    missing = {g["parameter"]: g for g in record["gaps"] if g["coverage"] == "NO_LINKED_RECORD"}
    assert set(missing) == {"модуль деформации", "отметки реперов"}
    assert missing["модуль деформации"]["where_to_look"] == ["mechanics_evidence_catalog"]
    assert "mining_geometry_catalog" in missing["отметки реперов"]["where_to_look"][0]
    assert gaps[("PC-01", "CURATED_GAP")]["parameter"].startswith("GAP-900")
    assert gaps[("PC-01", "NOT_MATCHED")]["parameter"] == "схема ходов"      # not recognised: not checked, not
    assert gaps[("PC-01", "NOT_MATCHED")]["status"] == "NOT_CHECKED"         # claimed missing
    assert all(g["status"] == "UNKNOWN" for g in record["gaps"] if g["coverage"] != "NOT_MATCHED")
    assert not any("g" == g["parameter"] for g in record["gaps"])            # 1-letter items are not parameters
    assert "2,1" not in json.dumps(record["gaps"], ensure_ascii=False)      # no value is filled into a gap
    # markdown: ids and pages, within the budget
    md = record["markdown"]
    assert md.startswith("# Досье темы") and S1 in md and "PC-01" in md and "UNKNOWN" in md
    assert len(md) <= 12_000 and record["budget"]["markdown_chars"] == len(md)


class Stub:
    """Retrieval stand-in: the same ranked pages for every formulation (a formulation may add its own)."""

    def __init__(self, units, extra=None):
        self.units, self.extra, self.calls = units, extra or {}, []

    def search(self, query, *, source_ids=None, limit=50):
        self.calls.append((query, tuple(source_ids) if source_ids else None, limit))
        units = [u for u in self.units + self.extra.get(query, [])
                 if not source_ids or u["page_id"].split(":")[0] in source_ids]
        return {"engine": "stub", "build_id": "v-stub", "built_from_snapshot_id": "snap-x", "units": units}


def _units(canon):
    return [{"page_id": "VKM-SRC-001:p0002", "rank": 1, "unit_id": "u-1", "object_ids": [canon.ids["block"]]},
            {"page_id": "VKM-SRC-001:p0009", "rank": 2, "unit_id": "u-stale", "object_ids": []},
            {"page_id": "VKM-SRC-002:p0001", "rank": 3, "unit_id": "u-3", "object_ids": []}]


def test_retrieval_formulations_tiers_and_rrf(env):
    service, canon, _root = env
    stub = Stub(_units(canon), extra={"связанный пересказ": [
        {"page_id": "VKM-SRC-002:p0001", "rank": 1, "unit_id": "u-para", "object_ids": []}]})
    service.deps.topic_retrieval = stub
    record, warnings, _env = _dossier(service, paraphrases=["связанный пересказ"])
    core = ("VKM-SRC-001", "VKM-SRC-002", "VKM-SRC-013")        # VKM_REGIONAL in the register (022: retired)
    texts = [QUERY, "связанный пересказ", f"{QUERY} мульда сдвижения"]
    assert sorted(stub.calls, key=repr) == sorted(((t, s, topic.RETRIEVAL_UNITS) for t in texts
                                                   for s in (core, None)), key=repr)
    retrieval = record["inputs"]["retrieval"]
    assert record["mode"] == "FULL" and retrieval["searches"] == 6 and retrieval["pages"] == {"CORE": 2, "REST": 0}
    assert "STALE_PROJECTION" in warnings and record["inputs"]["core_tier"]["sources"] == 3
    pages = record["pages"]["core"]                                          # RRF over formulations × tiers
    assert [p["page_id"] for p in pages] == ["VKM-SRC-001:p0002", "VKM-SRC-002:p0001"]
    assert pages[0]["rrf"] == pytest.approx(6 / 61, abs=1e-6)               # first in all six lists
    assert pages[1]["rrf"] == pytest.approx(4 / 63 + 2 / 62, abs=1e-6)       # third (after a stale page) or second
    assert set(pages[1]["formulations"]) == {"query", "paraphrase", "neighbours"}
    top = record["sections"][0]
    assert top["section_id"] in (S1, S2) and "retrieval" in top["signals"] and top["tier"] == "CORE"
    unit = top["units"][0]
    assert unit["unit_id"] == "u-1" and unit["page_id"] == "VKM-SRC-001:p0002" and unit["retrieval_rank"] == 1
    assert unit["snippet"] and "земной поверхности" in unit["snippet"]         # from the unit's block (canon)
    assert S4 in {s["section_id"] for s in record["sections"]}               # a retrieval-only section of 002
    # a source filter replaces the tiers and reaches every part
    record, _w, envelope = _dossier(service, source_ids=["VKM-SRC-002"])
    assert {c[1] for c in stub.calls[-2:]} == {("VKM-SRC-002",)} and envelope.source_id == "VKM-SRC-002"
    assert {s["source_id"] for s in record["sections"]} == {"VKM-SRC-002"}
    assert [s["source_id"] for s in record["sources"]] == ["VKM-SRC-002"] and record["formulas"] == []


def test_two_tiers_are_shown_apart(env):
    service, canon, _root = env
    service._topic_cache["core"] = (("core", canon.snapshot_id, "ab" * 6), {"VKM-SRC-001"})   # 002 = the rest
    service.deps.topic_retrieval = Stub(_units(canon))
    record, _w, _e = _dossier(service)
    assert [p["page_id"] for p in record["pages"]["core"]] == ["VKM-SRC-001:p0002"]
    assert [p["page_id"] for p in record["pages"]["rest"]] == ["VKM-SRC-002:p0001"]
    tiers = {s["section_id"]: s["tier"] for s in record["sections"]}
    assert tiers[S4] == "REST" and all(t == "CORE" for s, t in tiers.items() if s != S4)
    assert {s["source_id"]: s["tier"] for s in record["sources"]} == {"VKM-SRC-001": "CORE", "VKM-SRC-002": "REST"}
    md = record["markdown"]
    assert md.index("## Разделы — ядро ВКМ") < md.index("## Разделы — остальной корпус")
    assert "## Источники — ядро ВКМ" in md and "## Источники — остальной корпус" in md


def test_paraphrases_also_match_catalogue_processes(env):
    service, _canon, _root = env
    plain, _w, _e = _dossier(service)
    assert [p["process_id"] for p in plain["catalogue"]["processes"]] == ["PC-01"]
    record, _w, _e = _dossier(service, paraphrases=["ползучесть соли"])
    procs = {p["process_id"]: p for p in record["catalogue"]["processes"]}
    assert list(procs) == ["PC-01", "PC-02"] and procs["PC-02"]["matched_words"] == []   # the query's words: none
    assert procs["PC-02"]["score"] < procs["PC-01"]["score"]
    assert procs["PC-01"]["evidence_pages_found"] == 2          # its evidence pages 1 and 2 are the pages of S1
    assert topic._evidence_page({"source_id": "VKM-SRC-001", "pdf_page": "12-13"}) == "VKM-SRC-001:p0012"
    assert topic._evidence_page({"source_id": "VKM-SRC-001", "pdf_page": ""}) is None


def test_core_tier_rule():
    """Core = sources named by the evidence catalogues + register scope VKM/SKRU/regional, mapped or verbatim (an
    unmapped list of mines maps to no scope); analogue, general-method and retired sources are the rest."""
    register = [("VKM-SRC-100", ["VKM_REGIONAL"], "VKM_regional"), ("VKM-SRC-101", [], "SKRU1_SKRU2_SKRU3"),
                ("VKM-SRC-102", ["NON_VKM"], "NON_VKM_ANALOG"), ("VKM-SRC-103", [], "LEGACY_RETIRED"),
                ("VKM-SRC-104", ["GENERAL_METHOD"], "GENERAL_METHOD"), ("VKM-SRC-106", ["OTHER_VKM_SITE"], "VKM_x")]

    class Canon:
        def snapshot_id(self):
            return "snap-synthetic"

        def query(self, sql, params):
            return [{"source_id": s, "site_scope": sc, "site_scope_raw": raw} for s, sc, raw in register]

    class Catalogues:
        def rows(self, table, columns=None):
            return [{"source_ids": "VKM-SRC-104; VKM-SRC-105"}] if table == "mathematical_model_registry" else []

    st = topic._State(req=topic.TopicRequest(query="закладка"), stems=["закладк"])
    st.inputs["catalogues"] = {"pack_id": "abababababab"}
    builder = topic.DossierBuilder(Canon(), catalogues=Catalogues())
    core = builder._core_sources(st, True)
    assert core == {"VKM-SRC-100", "VKM-SRC-101", "VKM-SRC-104", "VKM-SRC-105", "VKM-SRC-106"}
    assert st.inputs["core_tier"]["catalogued"] == 2 and st.inputs["core_tier"]["sources"] == 5
    again = topic._State(req=st.req, stems=st.stems, inputs={"catalogues": {"pack_id": "abababababab"}})
    assert builder._core_sources(again, True) is core and again.inputs["core_tier"] == st.inputs["core_tier"]
    no_pack = topic.DossierBuilder(Canon())._core_sources(topic._State(req=st.req, stems=st.stems), False)
    assert no_pack == {"VKM-SRC-100", "VKM-SRC-101", "VKM-SRC-106"}


def test_a_long_section_lends_its_rank_to_formulas_near_its_hits_only():
    def ctx(fid, page, number, n_symbols, n_refs, n_params):
        return {"formula_id": fid, "source_id": "VKM-SRC-001", "page_id": f"VKM-SRC-001:p{page:04d}",
                "page_index": page, "kind": "DISPLAY", "equation_number": number, "section_id": "SEC-long",
                "n_defined_symbols": n_symbols, "n_refs_in": n_refs, "n_parameters": n_params}

    class Nav:
        def query(self, sql, params):
            if "WHERE kind = 'DISPLAY'" in sql:     # a chapter of 40 pages: a rich formula far from the hit page
                return [ctx("F-far", 35, "3.1", 3, 3, 1), ctx("F-near", 21, None, 1, 0, 0)]
            return []

        def run(self, name, **kwargs):
            return []

    class Canon:
        def query(self, sql, params):
            return []

    st = topic._State(req=topic.TopicRequest(query="ползучесть соли"), stems=["ползуч", "сол"])
    st.sections = [{"source_id": "VKM-SRC-001", "rank": 1, "tier": "CORE", "units": [{"page_id": "VKM-SRC-001:p0020"}],
                    "pages": {"first_index": 1, "last_index": 40}}]
    topic.DossierBuilder(Canon(), nav=Nav())._formulas(st)
    assert [(f["formula_id"], f["match"]) for f in st.formulas] == [("F-near", ["section#1:near"]),
                                                                     ("F-far", ["section#1"])]
    assert st.formulas[0]["score"] > 5 * st.formulas[1]["score"]


def test_figures_and_tables_near_the_hits(env):
    service, canon, _root = env
    service.deps.topic_retrieval = Stub(_units(canon))
    record, _w, _e = _dossier(service, query="синтетическая схема")
    visual = record["visual"]
    assert visual and visual[0]["object_id"] == canon.ids["figure"] and visual[0]["kind"] == "FIGURE"
    assert visual[0]["page_id"] == "VKM-SRC-001:p0002" and visual[0]["matched"] == 2
    assert f"`{canon.ids['figure']}` [рисунок]" in record["markdown"]


def test_hybrid_adapter_maps_hits_to_units(env):
    _service, canon, _root = env
    retrieval = topic.HybridTopicRetrieval(FakeHybrid(hybrid_hits(canon)))
    out = retrieval.search("оседание", source_ids=["VKM-SRC-001"], limit=10)
    assert out["engine"] == "opensearch-hybrid" and out["build_id"] == "v-test"
    request = retrieval.backend.requests[-1]
    assert request["kinds"] == ("PAGE",) and request["filters"] == {"source_id": ["VKM-SRC-001"]}
    first = out["units"][0]
    assert first["page_id"] == "VKM-SRC-001:p0001" and first["unit_id"] == "u1-0000000000000001"
    assert first["object_ids"] == [canon.ids["block"], canon.ids["block2"]]
    assert topic.make_retrieval(None, None) is None and topic.make_retrieval("x", object()) == "x"


def test_budget_is_hard_and_trims_lowest_priority_first(env):
    service, _canon, _root = env
    full, _w, _e = _dossier(service)
    tiny, _w, _e = _dossier(service, budget_chars=1_000)
    assert len(tiny["markdown"]) <= 1_000 and tiny["budget"]["markdown_chars"] == len(tiny["markdown"])
    small, _w, _e = _dossier(service, budget_chars=1_600)
    md = small["markdown"]
    assert len(md) <= 1_600 and small["budget"]["markdown_chars"] == len(md)
    trimmed = small["budget"]["trimmed"]
    assert trimmed and all(t["how_to_get"] for t in trimmed.values())
    assert "Не вошло" in md
    kept = {c for c, items in (("sections", small["sections"]), ("causal", small["catalogue"]["causal"]))
            if items}
    assert "sections" in kept and "causal" not in kept                       # low priority goes first
    assert len(full["markdown"]) > len(md)
    # deterministic: the same call gives the same dossier (timings aside)
    again, _w, _e = _dossier(service, budget_chars=1_600)
    small.pop("timings_ms"), again.pop("timings_ms")
    assert small == again


def test_degradation_without_concepts_catalogues_nav(env, tmp_path):
    service, canon, root = env

    def broken(con, term, limit=20):
        raise RuntimeError("no concept tables in this build")

    service.deps.nav = nav_store.NavStore(root, canonical_db=canon.duckdb_path, functions={"explore_concept": broken})
    record, warnings, _env = _dossier(service)
    assert "CONCEPTS_UNAVAILABLE" in warnings and record["concept"] is None and record["sections"]
    service.deps.catalogues = CatalogueStore(tmp_path / "no-root")
    record, warnings, _env = _dossier(service)
    assert "CATALOGUES_UNAVAILABLE" in warnings and record["gaps"] == []
    assert record["catalogue"]["processes"] == [] and record["sections"]
    service.deps.nav = nav_store.NavStore(tmp_path / "no-nav")
    with pytest.raises(ApiFailure) as exc:                                   # nothing left to build from
        service.reconstruct_topic(QUERY)
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE"
    service.deps.catalogues = CatalogueStore(root)
    record, warnings, envelope = _dossier(service)
    assert "NAV_UNAVAILABLE" in warnings and envelope.projection.engine == "catalogues"
    assert record["sections"] == [] and record["catalogue"]["processes"][0]["process_id"] == "PC-01"


def test_topics_are_used_when_the_build_has_them(env):
    service, canon, root = env
    service.deps.nav = nav_store.NavStore(root, canonical_db=canon.duckdb_path, functions={  # agent T's shapes
        "explore_concept": fake_explore(canon.ids["block"]),
        "find_topics": lambda con, terms, limit=10: [{
            "topic_id": "TOP-1", "level": 1, "n_sections": 4, "n_sources": 2, "label_terms": ["оседание", "мульда"],
            "central_section_ids": [S1], "score": 1.2, "matched": [terms]}],
        "topic": lambda con, topic_id: {
            "topic_id": topic_id, "label_terms": ["оседание", "мульда"], "central_sections": [{"section_id": S1}],
            "members": [{"section_id": S2, "source_id": "VKM-SRC-001"}], "sources": [{"source_id": "VKM-SRC-001"}]}})
    record, _w, _e = _dossier(service)
    topic_ = record["topics"][0]
    assert topic_["topic_id"] == "TOP-1" and "`TOP-1` оседание, мульда" in record["markdown"]
    assert topic_["detail"] == {"section_ids": [S1, S2], "source_ids": ["VKM-SRC-001"], "terms": ["оседание", "мульда"]}

    def broken_detail(con, topic_id):
        raise LookupError(topic_id)

    service.deps.nav._functions["topic"] = broken_detail
    record, warnings, _e = _dossier(service)
    assert record["topics"][0]["topic_id"] == "TOP-1" and "TOPICS_UNAVAILABLE" in warnings


def test_arguments_are_checked(env):
    service, *_ = env
    for kw in ({"budget_chars": 999}, {"budget_chars": 60_001}, {"source_ids": ["SRC-1"]}, {"max_sections": 0}):
        with pytest.raises(ApiFailure) as exc:
            service.reconstruct_topic(QUERY, **kw)
        assert exc.value.code in ("INVALID_ARGUMENT", "INVALID_ID")
    with pytest.raises(ApiFailure):
        service.reconstruct_topic("   ")


# ---------------------------------------------------------------------------------------------------- HTTP and MCP
READ = "topic-read-token-00000000000000000000"
HR = {"Authorization": f"Bearer {READ}"}


def test_http_routes(env):
    pytest.importorskip("fastapi")
    pytest.importorskip("pytz")
    from fastapi.testclient import TestClient

    from vkm_corpus.api.app import ApiConfig, create_app

    service, *_ = env
    client = TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}, write_tokens={})))
    resp = client.get("/v1/topic", params={"q": QUERY, "budget": 4000, "source_id": ["VKM-SRC-001"]}, headers=HR)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    Envelope.model_validate(body["item"]["envelope"])
    assert body["item"]["envelope"]["object_kind"] == "TOPIC_DOSSIER"
    assert len(body["item"]["record"]["markdown"]) <= 4000
    assert "RETRIEVAL_UNAVAILABLE" in {w["code"] for w in body["meta"]["warnings"]}
    post = client.post("/v1/topic", json={"query": QUERY, "budget_chars": 2000, "max_formulas": 0,
                                          "paraphrases": ["мульда сдвижения"]}, headers=HR)
    assert post.status_code == 200 and post.json()["item"]["record"]["formulas"] == []
    assert post.json()["item"]["record"]["formulations"][1] == {"kind": "paraphrase", "text": "мульда сдвижения"}
    got = client.get("/v1/topic", params={"q": QUERY, "paraphrase": ["мульда сдвижения", "subsidence trough"]},
                     headers=HR).json()["item"]["record"]["formulations"]
    assert [f["text"] for f in got if f["kind"] == "paraphrase"] == ["мульда сдвижения", "subsidence trough"]
    four = ["мульда сдвижения", "subsidence trough", "оседание поверхности", "прогиб толщи"]
    got = client.get("/v1/topic", params={"q": QUERY, "paraphrase": four}, headers=HR).json()["item"]["record"]
    assert [f["text"] for f in got["formulations"]] == [QUERY, *four]    # the caller's come first; 5 at most
    assert client.get("/v1/topic", params={"q": QUERY, "paraphrase": ["a"] * 5}, headers=HR).status_code == 400
    assert client.get("/v1/topic", params={"q": QUERY, "budget": 10}, headers=HR).status_code == 400
    assert client.get("/v1/topic", params={"q": QUERY, "source_id": "SRC-1"}, headers=HR).status_code == 400
    assert client.post("/v1/topic", json={"query": QUERY, "extra": 1}, headers=HR).status_code == 400
    assert client.get("/v1/topic", params={"q": QUERY}).status_code == 401


def test_mcp_tool_returns_markdown_and_structured_json(env):
    pytest.importorskip("mcp")
    pytest.importorskip("fastapi")
    pytest.importorskip("pytz")
    import httpx
    from mcp import Client

    from vkm_corpus.api.app import ApiConfig, create_app
    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    service, *_ = env
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}, write_tokens={}))
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            ok = await client.call_tool("reconstruct_topic", {"query": QUERY, "budget_chars": 3000,
                                                              "source_ids": ["VKM-SRC-001"],
                                                              "paraphrases": ["мульда сдвижения"]})
            bad = await client.call_tool("reconstruct_topic", {"query": QUERY, "budget_chars": 5})
            return tools["reconstruct_topic"], ok, bad

    tool, ok, bad = asyncio.run(go())
    assert tool.annotations.read_only_hint and "UNKNOWN" in tool.description
    assert ok.is_error is False and ok.content[0].text.startswith("# Досье темы") and len(ok.content) == 1
    assert "предупреждения: " in ok.content[0].text and "RETRIEVAL_UNAVAILABLE" in ok.content[0].text
    assert len(ok.content[0].text) <= 3000                                   # the MCP text keeps the budget
    assert ok.structured_content["ok"] and ok.structured_content["item"]["envelope"]["object_kind"] == "TOPIC_DOSSIER"
    assert ok.structured_content["item"]["record"]["formulations"][1]["kind"] == "paraphrase"
    assert len(ok.structured_content["item"]["record"]["markdown"]) <= 3000
    assert bad.is_error
