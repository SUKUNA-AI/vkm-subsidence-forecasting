"""Copies of one work in the NAV summaries (agent Б, 13.10; ``navigation.work_rollup``): ``n_works`` next to
``n_sources`` in ``parameter_summary``, one position per printed object of a work in ``find_parameters`` and
``find_tables`` (copies listed), never a merge inside one source or across works; and the API wiring over a fake NAV
(no NAV rebuild: the canon's work links). Synthetic ids; VKM-SRC-001 and 002 are copies of VKM-WRK-001 in the
synthetic canon."""
from __future__ import annotations

import pytest

from vkm_corpus.navigation.work_rollup import merge_copies, parameter_keys, summary_by_work, table_keys

WORK_OF = {"VKM-SRC-025": "VKM-WRK-013", "VKM-SRC-202": "VKM-WRK-013", "VKM-SRC-012": "VKM-WRK-012"}


def cand(cid, src, page, lo=10.0, hi=None, prop="deformation_modulus", mat="каменная соль", text=None):
    return {"candidate_id": cid, "source_id": src, "page_id": f"{src}:p{page:04d}", "page_index": page,
            "property_key": prop, "material": mat, "scale_hint": "LAB", "unit_si": "Pa", "value_si_min": lo,
            "value_si_max": hi if hi is not None else lo, "value_text": text}


def test_summary_counts_works_next_to_sources():
    rows = [{"property_key": "deformation_modulus", "n_sources": 4, "n_candidates": 9,
             "source_ids": ["VKM-SRC-012", "VKM-SRC-025", "VKM-SRC-202", "VKM-SRC-999"]}]
    out = summary_by_work(rows, WORK_OF)
    assert out[0]["n_works"] == 3 and out[0]["n_sources"] == 4 and out[0]["n_candidates"] == 9
    assert out[0]["work_ids"] == ["VKM-WRK-012", "VKM-WRK-013"]                  # 999 has no known work
    assert out[0]["copies"] == {"VKM-WRK-013": ["VKM-SRC-025", "VKM-SRC-202"]}
    assert rows[0].get("n_works") is None                                       # the input is not changed


def test_merge_parameter_copies_of_one_work():
    items = [cand("C1", "VKM-SRC-025", 82), cand("C2", "VKM-SRC-202", 82),          # one value, two files of WRK-013
             cand("C3", "VKM-SRC-202", 83),                                         # another page
             cand("C4", "VKM-SRC-025", 82),                                         # same file again: its own
             cand("C5", "VKM-SRC-012", 82),                                         # another work: its own
             cand("C6", "VKM-SRC-202", 82, lo=11.0),                                # another value
             cand("C7", "VKM-SRC-025", 90, lo=None, hi=None, text="1,5"),
             cand("C8", "VKM-SRC-202", 90, lo=None, hi=None, text="1.5")]          # printed values, OCR comma
    for c in items[6:]:
        c["value_si_min"] = c["value_si_max"] = None
    kept, stats = merge_copies(items, WORK_OF, parameter_keys, id_field="candidate_id")
    assert [c["candidate_id"] for c in kept] == ["C1", "C3", "C4", "C5", "C6", "C7"]
    assert kept[0]["copies"] == [{"candidate_id": "C2", "source_id": "VKM-SRC-202", "page_id": "VKM-SRC-202:p0082",
                                  "page_index": 82}]
    assert kept[0]["work_id"] == "VKM-WRK-013" and kept[5]["copies"][0]["candidate_id"] == "C8"
    assert "copies" not in kept[2] and stats["merged"] == 2 and stats["n_works"] == 2 and stats["n_sources"] == 3
    cut, cut_stats = merge_copies(items, WORK_OF, parameter_keys, id_field="candidate_id", limit=2)
    assert [c["candidate_id"] for c in cut] == ["C1", "C3"] and cut_stats["merged"] == 1


def test_merge_table_copies_by_number_or_nav_group():
    tables = [{"table_id": "VKM-SRC-025:p0082:t000000000001", "source_id": "VKM-SRC-025", "page_index": 82,
               "table_number": "3.1"},
              {"table_id": "VKM-SRC-202:p0082:t000000000002", "source_id": "VKM-SRC-202", "page_index": 82,
               "table_number": "3.1"},
              {"table_id": "VKM-SRC-202:p0040:t000000000003", "source_id": "VKM-SRC-202", "page_index": 40},
              {"table_id": "VKM-SRC-025:p0041:t000000000004", "source_id": "VKM-SRC-025", "page_index": 41},
              {"table_id": "VKM-SRC-012:p0082:t000000000005", "source_id": "VKM-SRC-012", "page_index": 82,
               "table_number": "3.1"}]
    groups = {"VKM-SRC-202:p0040:t000000000003": "OCL-9", "VKM-SRC-025:p0041:t000000000004": "OCL-9"}
    kept, stats = merge_copies(tables, WORK_OF, lambda t, w: table_keys(t, w, groups), id_field="table_id")
    assert [t["table_id"][-1] for t in kept] == ["1", "3", "5"] and stats["merged"] == 2
    assert kept[1]["copies"][0]["table_id"].endswith("4")                       # NAV group across shifted pages


# ---------------------------------------------------------------------------------------------------- API wiring
pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz")
pytest.importorskip("PIL")


class FakeNav:
    def __init__(self, answers, groups=None):
        self.answers, self.groups, self.calls = answers, dict(groups or {}), []

    def run(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        return self.answers[name](*args, **kw)

    def snapshot_id(self):
        return None

    def require(self, *datasets):
        return None

    def query(self, sql, params):
        return [{"object_id": o, "cluster_id": self.groups[o]} for o in params if o in self.groups]


@pytest.fixture()
def service(tmp_path):
    from vkm_corpus.api.fixtures import synthetic_service

    svc, _canon, _fakes = synthetic_service(tmp_path)
    return svc


def test_api_parameters_tables_and_summary_by_work(service):
    a, b = "VKM-SRC-001", "VKM-SRC-002"
    nav = FakeNav({
        "find_parameters": lambda **kw: {"total": 3, "candidates": [cand("C1", a, 5), cand("C2", b, 5),
                                                                   cand("C3", b, 6)][:kw["limit"]]},
        "parameter_summary": lambda prop, material=None: {"rows": [{"n_sources": 3,
                                                                    "source_ids": [a, b, "VKM-SRC-013"]}]},
        "find_tables": lambda **kw: {"total": 2, "tables": [
            {"table_id": f"{a}:p0003:t000000000001", "source_id": a, "page_index": 3, "table_number": "2"},
            {"table_id": f"{b}:p0007:t000000000002", "source_id": b, "page_index": 7}]}},
        groups={f"{a}:p0003:t000000000001": "OCL-1", f"{b}:p0007:t000000000002": "OCL-1"})
    service.deps.nav = nav
    rec = service.nav_parameters("модуль деформации", None, None, None, None, 2).item.record
    assert nav.calls[-1][2]["limit"] == 4                                       # room for the merged copies
    assert [c["candidate_id"] for c in rec["candidates"]] == ["C1", "C3"]
    assert rec["candidates"][0]["copies"][0]["candidate_id"] == "C2" and rec["by_work"]["n_works"] == 1
    summary = service.nav_parameter_summary("модуль деформации", None).item.record
    assert summary["rows"][0]["n_works"] == 2 and summary["rows"][0]["copies"] == {"VKM-WRK-001": [a, b]}
    tables = service.nav_tables("модуль деформации", None, None, None, 5).item.record
    assert len(tables["tables"]) == 1 and tables["tables"][0]["copies"][0]["source_id"] == b
    assert tables["by_work"]["merged"] == 1 and nav.calls[-1][2]["limit"] == 10


def test_api_search_hits_carry_g1_copies_and_warn_only_for_copies_side_by_side(service):
    from vkm_corpus.api.fixtures import FakeHybrid

    a, b = "VKM-SRC-001:p0001", "VKM-SRC-002:p0001"
    base = {"object_type": "PAGE", "index": "vkm-pages-m1-b-test", "build_id": "b-test", "score": 0.03,
            "trace": {"bm25_rank": 1, "fused_rank": 1, "rrf_score": 0.03}}
    copy = {"page_id": b, "coverage": None, "kinds": ["SAME_WORK_ALIGNED_PAGE"], "work_id": "VKM-WRK-001",
            "page_offset": 0}
    # G1 kept a (001) and listed b (002, the other copy of WRK-001): no two copies side by side → no warning
    service.deps.hybrid = FakeHybrid([{**base, "id": a, "source_id": "VKM-SRC-001", "page_id": a, "copies": [copy]}])
    res = service.search_hybrid("оседание", ["PAGE"], {}, 10, None)
    assert res.items[0].record["copies"] == [copy] and res.items[0].record["work_copy_count"] == 2
    assert not any(w.code == "WORK_HAS_MULTIPLE_COPIES" for w in res.warnings)
    # both copies among the hits (G1 off): the warning stays
    service.deps.hybrid = FakeHybrid([{**base, "id": a, "source_id": "VKM-SRC-001", "page_id": a},
                                      {**base, "id": b, "source_id": "VKM-SRC-002", "page_id": b}])
    res2 = service.search_hybrid("оседание", ["PAGE"], {}, 10, None)
    assert [w.count for w in res2.warnings if w.code == "WORK_HAS_MULTIPLE_COPIES"] == [1]
    assert "copies" not in res2.items[0].record
