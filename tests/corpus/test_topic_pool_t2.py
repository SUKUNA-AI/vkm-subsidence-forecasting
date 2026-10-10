"""TOPIC_BENCHMARK_V1 delta pool T2 (benchmarks/topic_v1/scripts/pool_t2.py, POOL_LABELING_T2.md) on synthetic data:
exclusions (catalogue targets, pairs judged in T1 incl. duplicate aliases, depth), duplicate grouping and provenance,
blindness of the packets and of the cross-check packet, the T1 packet format and T1 §8 draw (parity with pool_t1),
ingest checks with LLM_AGENT_T2 (the T1 files are never written) and the agreement maths of xscore."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
import json
import random
import re
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab import topic_bench as TB

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "benchmarks" / "topic_v1" / "scripts" / "pool_t2.py"
SYSTEM_WORDS = re.compile(r"\b(bm25|hybrid_late|hybrid_nolate|nav|human3|D1)\b|@\d")


@pytest.fixture(scope="module")
def t2():
    spec = importlib.util.spec_from_file_location("topic_v1_pool_t2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ synthetic world
def topic(tid: str, track: str, targets=()) -> TB.Topic:
    qs = tuple(TB.Query(f"TQ-{tid}-{i}", tid, v, f"оседание поверхности {tid} {v.lower()}")
               for i, v in enumerate(TB.VARIANTS))
    tg = tuple(TB.Target(f"{tid}/T{i + 1:03d}", p, p.split(":")[0], tuple(alts))
               for i, (p, alts) in enumerate(targets))
    return TB.Topic(tid, track, "OBS" if track == "PROCESS" else "MM", f"Тема {tid}: оседание поверхности", qs, tg)


TOPICS = [topic("PC-01", "PROCESS", [("VKM-SRC-001:p0001", ["VKM-SRC-001:p0002"])]),
          topic("MM-RS", "MODEL_FAMILY", [("VKM-SRC-002:p0010", [])])]
T1_ROWS = [
    {"query_id": "PC-01", "level": "PAGE", "doc_id": "VKM-SRC-004:p0007", "grade": "2", "status": "CANDIDATE",
     "basis": "POOL_JUDGMENT", "label_source": "LLM_AGENT_T1", "pooled_from": "bm25@1", "rationale": "SUP_DISCUSS"},
    {"query_id": "PC-01", "level": "PAGE", "doc_id": "VKM-SRC-004:p0009", "grade": "0", "status": "CANDIDATE",
     "basis": "POOL_JUDGMENT", "label_source": "LLM_AGENT_T1", "pooled_from": "nav@2", "rationale": "OFF_TOPIC"},
    {"query_id": "MM-RS", "level": "PAGE", "doc_id": "VKM-SRC-007:p0001", "grade": "3", "status": "CANDIDATE",
     "basis": "POOL_JUDGMENT", "label_source": "LLM_AGENT_T1", "pooled_from": "hybrid_late@1", "rationale": "KEY_MODEL"},
]
T1_GROUPS = [{"topic_id": "PC-01", "pages": ["VKM-SRC-004:p0009"], "aliases": ["VKM-SRC-006:p0003"]}]
CANON_DUP = {p: frozenset({"VKM-SRC-005:p0001", "VKM-SRC-004:p0007"}) for p in ("VKM-SRC-005:p0001",
                                                                               "VKM-SRC-004:p0007")}


def ranked(*items) -> list[tuple[str, frozenset[str]]]:
    """(page, duplicates…) → the ranking shape of collect_pool (aliases include the page)."""
    return [(x, frozenset({x})) if isinstance(x, str) else (x[0], frozenset(x)) for x in items]


def rankings() -> dict:
    fillers = [f"VKM-SRC-008:p{i:04d}" for i in range(1, 11)]
    rk = {s: {} for s in ("bm25", "hybrid_late", "hybrid_nolate", "nav", "human3")}
    rk["bm25"]["TQ-PC-01-0"] = ranked("VKM-SRC-001:p0001",                          # catalogue primary
                                      ("VKM-SRC-003:p0005", "VKM-SRC-001:p0002"),   # API duplicate of an alternate
                                      "VKM-SRC-004:p0007",                          # judged in T1
                                      "VKM-SRC-005:p0001",                          # canonical duplicate of a T1 page
                                      "VKM-SRC-006:p0003",                          # alias of a T1 unit
                                      "VKM-SRC-252:p0001",                          # new
                                      "VKM-SRC-007:p0001")                          # judged in T1 for MM-RS only
    rk["hybrid_late"]["TQ-PC-01-1"] = ranked(("VKM-SRC-252:p0002", "VKM-SRC-253:p0002"))
    rk["human3"]["TQ-PC-01-2"] = ranked(("VKM-SRC-253:p0002", "VKM-SRC-252:p0002"), "VKM-SRC-252:p0001")
    rk["nav"]["TQ-PC-01-0"] = ranked(*fillers, "VKM-SRC-260:p0001")                   # rank 11: beyond the depth
    rk["bm25"]["TQ-MM-RS-0"] = ranked("VKM-SRC-002:p0010", "VKM-SRC-007:p0001", "VKM-SRC-004:p0007")
    return rk


@pytest.fixture()
def world(t2):
    judged = t2.t1_judged(TOPICS, T1_ROWS, T1_GROUPS)
    delta = t2.delta_candidates(TOPICS, rankings(), {s: t2.DEPTH for s in t2.SYSTEMS}, CANON_DUP, judged)
    return judged, delta, t2.make_units(TOPICS, delta)


def defs_of(topics: list[TB.Topic]) -> dict[str, dict]:
    out = {}
    for t in topics:
        d = {"topic_id": t.topic_id, "track": t.track, "group": t.group, "title": t.title,
             "queries": [q.text for q in t.queries]}
        if t.track == "PROCESS":
            d.update(role="наблюдение вертикальных смещений", path="прямо", variables="u_z", parameters="—",
                     equations="—")
        else:
            d["models"] = ["спектральные индексы", "классификация снимков"]
        out[t.topic_id] = d
    return out


def page(source: str, index: int, text: str, captions=(), formulas: int = 0, previews=None) -> dict:
    d = {"source_id": source, "page_index": index, "text": text, "captions": list(captions), "formulas": formulas}
    if previews:
        d["formula_previews"] = previews
    return d


def pages_for(pool_topics: dict) -> dict[str, dict]:
    out = {}
    for v in pool_topics.values():
        for u in v["units"]:
            for p in u["pages"]:
                sid, _pfx, idx = TB.split_page_id(p)
                out[p] = page(sid, idx, f"Страница {p}: наблюдения оседания земной поверхности по реперам. " * 3)
    return out


# ------------------------------------------------------------------ pool: exclusions, groups, provenance
def test_t1_judged_pages_hold_rows_and_unit_aliases_not_targets(t2):
    judged = t2.t1_judged(TOPICS, T1_ROWS, T1_GROUPS)
    assert judged["PC-01"] == {"VKM-SRC-004:p0007", "VKM-SRC-004:p0009", "VKM-SRC-006:p0003"}
    assert judged["MM-RS"] == {"VKM-SRC-007:p0001"}


def test_exclusions_catalogue_t1_aliases_and_depth(world):
    _judged, delta, _units = world
    pc = delta["PC-01"]
    assert pc["excluded_catalogue_matches"] == ["VKM-SRC-001:p0001", "VKM-SRC-003:p0005"]
    assert pc["excluded_t1_judged"] == ["VKM-SRC-004:p0007", "VKM-SRC-005:p0001", "VKM-SRC-006:p0003"]
    # matched only through the canonical duplicate group: recorded as a link to the T1 page
    assert pc["t1_alias_links"] == [{"page": "VKM-SRC-005:p0001", "t1_pages": ["VKM-SRC-004:p0007"]}]
    kept = set(pc["candidates"])
    assert "VKM-SRC-007:p0001" in kept                    # judged in T1 for another topic only
    assert "VKM-SRC-260:p0001" not in kept                # rank 11 of nav
    assert {"VKM-SRC-252:p0001", "VKM-SRC-252:p0002", "VKM-SRC-253:p0002"} <= kept
    mm = delta["MM-RS"]
    assert mm["excluded_catalogue_matches"] == ["VKM-SRC-002:p0010"]
    assert mm["excluded_t1_judged"] == ["VKM-SRC-007:p0001"]
    assert set(mm["candidates"]) == {"VKM-SRC-004:p0007"}  # judged in T1 for PC-01, not for MM-RS


def test_duplicates_are_one_unit_with_best_rank_provenance(t2, world):
    _judged, _delta, units = world
    pc = {u["pages"][0]: u for u in units["PC-01"]["units"]}
    assert len(pc) == 13                                  # 10 nav pages + 007 + 252:p0001 + the duplicate pair
    pair = pc["VKM-SRC-252:p0002"]
    assert pair["pages"] == ["VKM-SRC-252:p0002", "VKM-SRC-253:p0002"] and pair["aliases"] == []
    assert pair["systems"] == {"hybrid_late": 1, "human3": 1}
    assert pair["per_page"] == {"VKM-SRC-252:p0002": {"hybrid_late": 1}, "VKM-SRC-253:p0002": {"human3": 1}}
    new = pc["VKM-SRC-252:p0001"]
    assert new["systems"] == {"bm25": 6, "human3": 2} and new["variants"] == ["NAME", "PARA2"]
    assert t2.PT.pooled_from(new["per_page"]["VKM-SRC-252:p0001"]) == "human3@2,bm25@6"
    assert [u["cid"] for u in units["PC-01"]["units"]] == [f"d{i:03d}" for i in range(1, 14)]
    summary = t2.pool_summary(units, _delta)
    assert summary["label_units"] == 14 and summary["pooled_pairs"] == 15 and summary["multi_page_units"] == 1
    assert summary["units_new_sources"] == 2 and summary["excluded_t1_judged"] == 4
    assert summary["excluded_catalogue_matches"] == 3 and summary["t1_alias_links"] == 1


def test_unit_ids_are_kept_from_an_earlier_pool(t2, world):
    _judged, delta, _units = world
    old = {"PC-01": {"units": [{"cid": "d007", "pages": ["VKM-SRC-252:p0001"]},
                               {"cid": "d002", "pages": ["VKM-SRC-252:p0002", "VKM-SRC-253:p0002"]}]}}
    units = {u["pages"][0]: u["cid"] for u in t2.make_units(TOPICS, delta, old)["PC-01"]["units"]}
    assert units["VKM-SRC-252:p0001"] == "d007" and units["VKM-SRC-252:p0002"] == "d002"
    assert len(set(units.values())) == len(units)
    assert units["VKM-SRC-007:p0001"] == "d008"                           # new ids continue after the largest kept
    assert min(c for p, c in units.items() if p.startswith("VKM-SRC-008")) == "d009"


def test_rankings_come_from_the_frozen_scorer_and_nav_from_its_sections(t2, tmp_path):
    sec = "SEC-00000000000000a1"
    runs = [{"kind": "meta", "canonical_snapshot_id": t2.SNAPSHOT},
            {"kind": "outline", "source_id": "VKM-SRC-009", "sections": [
                {"section_id": sec, "level": 1, "page_start_index": 3, "page_end_index": 4,
                 "page_start_id": "VKM-SRC-009:p0003", "page_end_id": "VKM-SRC-009:p0004"}]},
            {"kind": "run", "query_id": "TQ-PC-01-0", "topic_id": "PC-01", "system": "nav", "error": None,
             "nav": {"sections": [{"section_id": sec}], "term_units": [], "neighbour_units": []}},
            {"kind": "run", "query_id": "TQ-PC-01-0", "topic_id": "PC-01", "system": "bm25", "error": None,
             "hits": [{"page_id": "VKM-SRC-010:p0001", "source_id": "VKM-SRC-010",
                       "duplicates": ["VKM-SRC-011:p0001"]}]},
            {"kind": "end", "nav_snapshot_id": t2.SNAPSHOT}]
    exps = [{"kind": "meta", "canonical_snapshot_id": t2.SNAPSHOT},
            {"kind": "run", "query_id": "TQ-PC-01-0", "topic_id": "PC-01", "system": "human3", "error": None,
             "hits": [{"page_id": "VKM-SRC-012:p0002", "source_id": "VKM-SRC-012", "duplicates": []}]},
            {"kind": "run", "query_id": "TQ-PC-01-0", "topic_id": "PC-01", "system": "baseline", "error": None,
             "hits": [{"page_id": "VKM-SRC-013:p0002", "source_id": "VKM-SRC-013", "duplicates": []}]}]
    rp, ep = tmp_path / "runs.jsonl", tmp_path / "exp.jsonl"
    rp.write_text("\n".join(json.dumps(x) for x in runs) + "\n", encoding="utf-8")
    ep.write_text("\n".join(json.dumps(x) for x in exps) + "\n", encoding="utf-8")
    rk, info = t2.load_rankings(rp, ep)
    assert sorted(rk) == ["bm25", "human3", "nav"]                       # baseline is not a pool system
    assert rk["nav"]["TQ-PC-01-0"].pages == ["VKM-SRC-009:p0003", "VKM-SRC-009:p0004"]   # no hits: sections
    lists = t2.as_lists(rk)
    assert lists["bm25"]["TQ-PC-01-0"] == [("VKM-SRC-010:p0001", frozenset({"VKM-SRC-010:p0001",
                                                                            "VKM-SRC-011:p0001"}))]
    assert [x["file"] for x in info] == ["runs.jsonl", "exp.jsonl"]
    rp.write_text(rp.read_text(encoding="utf-8").replace(t2.SNAPSHOT, "snap-other"), encoding="utf-8")
    with pytest.raises(SystemExit):
        t2.load_rankings(rp, ep)


# ------------------------------------------------------------------ packets
def test_balanced_blocks_keep_pages_whole_and_sizes_even(t2):
    rnd = random.Random(3)
    seq = [(f"VKM-SRC-100:p{i:04d}", rnd.randint(1, 4)) for i in range(400)]
    total = sum(n for _p, n in seq)
    blocks = t2.balanced_blocks(seq, 90)
    assert len(blocks) == -(-total // 90)
    assert [p for b in blocks for p in b] == [p for p, _n in seq]
    units = dict(seq)
    sizes = [sum(units[p] for p in b) for b in blocks]
    assert sum(sizes) == total and max(sizes) - min(sizes) <= 2 * 4
    assert t2.balanced_blocks([], 90) == []


def test_packets_are_blind_and_hold_every_unit_once(t2, world):
    _judged, _delta, units = world
    pages = pages_for(units)
    defs = defs_of(TOPICS)
    stems = {tid: frozenset() for tid in defs}
    meta = {p.split(":")[0]: {"title": "Источник", "year": 2020} for p in pages}
    texts, index = t2.build_packets(TOPICS, defs, stems, units, pages, {}, {}, meta, target=5)
    assert len(texts) == 3 and [x["packet"] for x in index] == ["pk-001", "pk-002", "pk-003"]
    seen = []
    for x in index:
        text = texts[x["packet"]]
        assert not SYSTEM_WORDS.search(text), x["packet"]
        assert text.startswith(f"# {x['packet']} — страниц {x['n_pages']}, единиц {x['n_units']}\n")
        heads = re.findall(r"^### (VKM-SRC-\d{3}:p\d{4})", text, re.M)
        assert heads == x["pages"] == sorted(x["pages"])
        seen += [tuple(u.split()) for line in re.findall(r"^→ (.*)$", text, re.M) for u in line.split(" · ")]
        assert x["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert set(x) == {"packet", "pages", "units", "n_pages", "n_units", "units_new_sources", "topics", "sha256"}
    assert [p for x in index for p in x["pages"]] == sorted(p for x in index for p in x["pages"])
    expected = [(tid, u["cid"]) for tid, v in units.items() for u in v["units"]]
    assert sorted(seen) == sorted(expected) and len(seen) == len(set(seen))
    assert "копии: VKM-SRC-253:p0002" in texts["pk-003"]                  # the duplicate pair shows its copy


def test_packet_format_is_the_t1_format(t2, tmp_path, monkeypatch):
    """pool_t1.packets and pool_t2.render_packet give the same bytes for the same pages, units and blocks."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("snowballstemmer")
    PT = t2.PT
    defs = defs_of(TOPICS)
    long_text = " ".join(f"Предложение {i}: оседание земной поверхности над выработками составило {i} мм, "
                         f"наблюдения по реперам профильной линии." for i in range(40))
    pages = {"VKM-SRC-001:p0005": page("VKM-SRC-001", 5, long_text, [("рис.", "3", "Мульда оседания"),
                                                                     ("табл.", "2", "Отметки реперов"),
                                                                     ("рис.", "4", "лишняя подпись")], 2,
                                       ["u_z = f(x, t) + c_0 \\cdot t"]),
             "VKM-SRC-002:p0001": page("VKM-SRC-002", 1, "Титул."),
             "VKM-SRC-252:p0002": page("VKM-SRC-252", 2, "Короткая страница об оседании поверхности. " * 4),
             "VKM-SRC-253:p0002": page("VKM-SRC-253", 2, "Копия короткой страницы об оседании поверхности. " * 4)}
    pool_topics = {"PC-01": {"units": [{"cid": "c001", "pages": ["VKM-SRC-001:p0005"]},
                                       {"cid": "c002", "pages": ["VKM-SRC-252:p0002", "VKM-SRC-253:p0002"]},
                                       {"cid": "c003", "pages": ["VKM-SRC-099:p0001"]}]},
                   "MM-RS": {"units": [{"cid": "c001", "pages": ["VKM-SRC-001:p0005"]},
                                       {"cid": "c002", "pages": ["VKM-SRC-002:p0001"]}]}}
    secs = {"VKM-SRC-001:p0005": "Глава 2. Наблюдения"}
    meta = {"VKM-SRC-001": {"title": "Длинное название источника про оседания земной поверхности и наблюдения",
                            "year": 2012}, "VKM-SRC-252": {"title": "Новый источник", "year": None}}
    work, jv1 = tmp_path / "t1", tmp_path / "jv1"
    work.mkdir()
    jv1.mkdir()
    (jv1 / "pages.json").write_text(json.dumps({"sources": meta}, ensure_ascii=False), encoding="utf-8")
    for k, v in (("LB_WORK", work), ("CANON_DB", tmp_path / "none.duckdb"), ("J_V1", jv1)):
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("LB_JUDGMENTS", raising=False)
    monkeypatch.setattr(PT, "load_topics", lambda: TOPICS)
    monkeypatch.setattr(PT, "topic_definitions", lambda ts: defs)
    monkeypatch.setattr(PT, "translation_texts", lambda: {})
    monkeypatch.setattr(PT, "load_pool", lambda: {"topics": pool_topics})
    monkeypatch.setattr(PT, "page_data", lambda con, ps: copy.deepcopy({p: pages[p] for p in ps if p in pages}))
    monkeypatch.setattr(PT, "section_titles", lambda ps: secs)
    monkeypatch.setattr(PT, "PAGES_PER_PACKET", 2)
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: None)
    PT.packets()
    index = json.loads((work / "packets_index.json").read_text(encoding="utf-8"))
    order = {t.topic_id: i for i, t in enumerate(TOPICS)}
    stems = {tid: PT.topic_stems(d, []) for tid, d in defs.items()}
    idf = PT.stem_idf([d["text"] for d in pages.values()])
    by_page = t2.pages_of_units(pool_topics)
    assert len(index) == 2
    for x in index:
        mine = t2.render_packet(x["packet"], x["pages"], by_page, pages, defs, stems, idf, secs, meta, order)
        assert mine == (work / "packets" / f"{x['packet']}.md").read_text(encoding="utf-8")
    assert "нет в каноне снимка" in mine and "[почти нет текста]" in (work / "packets" / "pk-001.md").read_text(
        encoding="utf-8")


# ------------------------------------------------------------------ cross-check sample
def t1_rows_many(n_topics: int = 8, per_topic: int = 14, seed: int = 0) -> tuple[list[TB.Topic], list[dict]]:
    rnd = random.Random(seed)
    topics = [topic(f"PC-{i:02d}", "PROCESS") for i in range(1, n_topics // 2 + 1)] + \
             [topic(f"MM-X{i}", "MODEL_FAMILY") for i in range(1, n_topics // 2 + 1)]
    origins = ["nav@3", "bm25@2", "hybrid_late@1,nav@4", "D1@5", "hybrid_nolate@7,bm25@1"]
    codes = {3: "KEY_QUANT", 2: "SUP_ASPECT", 1: "MENTION", 0: "OFF_TOPIC"}
    rows = []
    for t in topics:
        for j in range(per_topic):
            g = rnd.choice([0, 0, 1, 1, 2, 3])
            rows.append({"query_id": t.topic_id, "level": "PAGE", "doc_id": f"VKM-SRC-{30 + j:03d}:p{j + 1:04d}",
                         "grade": str(g), "status": "CANDIDATE", "basis": "POOL_JUDGMENT",
                         "label_source": "LLM_AGENT_T1", "pooled_from": rnd.choice(origins), "rationale": codes[g]})
    return topics, rows


def test_t1_draw_is_the_t1_rule(t2, tmp_path, monkeypatch):
    topics, rows = t1_rows_many()
    groups = [{"topic_id": rows[0]["query_id"], "pages": [rows[0]["doc_id"], rows[1]["doc_id"]], "aliases": []}]
    gfile = tmp_path / "groups.json"
    gfile.write_text(json.dumps({"groups": groups}), encoding="utf-8")
    monkeypatch.setattr(t2.PT, "OUT_GROUPS", gfile)
    ref = t2.PT.draw_sample(rows, topics, seed=7)
    mine = t2.draw_t1(rows, topics, groups, {g: t2.PT.HSAMPLE_PER_GRADE for g in (3, 2, 1, 0)}, 7)
    random.Random(7).shuffle(mine)
    assert [(r["query_id"], r["doc_id"]) for r in mine] == [(r["query_id"], r["doc_id"]) for r in ref]


def test_xcheck_sample_rule(t2):
    topics, rows = t1_rows_many()
    groups = [{"topic_id": rows[0]["query_id"], "pages": [rows[0]["doc_id"], rows[1]["doc_id"]], "aliases": []}]
    exclude = frozenset((r["query_id"], r["doc_id"]) for r in rows[2:12])
    pool_topics = {t.topic_id: {"units": [{"cid": f"d{i:03d}", "pages": [f"VKM-SRC-26{i % 10}:p{i:04d}"]}
                                          for i in range(1, 9)]} for t in topics}
    per_grade = {3: 3, 2: 3, 1: 2, 0: 2}
    x = t2.draw_xcheck(topics, pool_topics, rows, groups, exclude, seed=11, n_t2=6, per_grade=per_grade)
    t1 = [u for u in x if u["set"] == "T1"]
    t2u = [u for u in x if u["set"] == "T2"]
    assert len(t2u) == 6 and all(u["grade"] is None and u["cid"] for u in t2u)
    assert sorted(u["grade"] for u in t1) == sorted(g for g, n in per_grade.items() for _ in range(n))
    assert all(u["cid"] is None and t2.PT.CODES[u["code"]] == u["grade"] for u in t1)
    assert not {(u["topic_id"], u["pages"][0]) for u in t1} & exclude
    assert {(u["topic_id"], p) for u in t1 for p in u["pages"][1:]} <= {(groups[0]["topic_id"], groups[0]["pages"][1])}
    assert x == t2.draw_xcheck(topics, pool_topics, rows, groups, exclude, seed=11, n_t2=6, per_grade=per_grade)
    other = t2.draw_xcheck(topics, pool_topics, rows, groups, exclude, seed=12, n_t2=6, per_grade=per_grade)
    assert [(u["topic_id"], u["pages"]) for u in other] != [(u["topic_id"], u["pages"]) for u in x]


def test_xcheck_packet_hides_grades_sets_and_ids(t2):
    topics = TOPICS
    xunits = [{"set": "T2", "topic_id": "PC-01", "pages": ["VKM-SRC-252:p0001"], "cid": "d004", "grade": None,
               "code": None},
              {"set": "T1", "topic_id": "MM-RS", "pages": ["VKM-SRC-007:p0001"], "cid": None, "grade": 3,
               "code": "KEY_MODEL"},
              {"set": "T1", "topic_id": "PC-01", "pages": ["VKM-SRC-004:p0009", "VKM-SRC-004:p0010"], "cid": None,
               "grade": 0, "code": "OFF_TOPIC"},
              {"set": "T2", "topic_id": "MM-RS", "pages": ["VKM-SRC-004:p0009"], "cid": "d002", "grade": None,
               "code": None}]
    pages = {p: page(p.split(":")[0], 1, f"Текст {p} об оседании поверхности. " * 3)
             for u in xunits for p in u["pages"]}
    defs = defs_of(topics)
    text, key = t2.build_xcheck(topics, defs, {tid: frozenset() for tid in defs}, xunits, pages, {}, {}, {})
    body = text.split("## Страницы", 1)[1]
    assert text.startswith(f"# {t2.XCHECK_NAME} — страниц 3, единиц 4\n")
    assert not SYSTEM_WORDS.search(text)
    assert not re.search(r"\b(KEY|SUP)_[A-Z]+|\b(MENTION|OFF_TOPIC)\b|\b[cd]\d{3}\b|\bT[12]\b", body)
    # ids in packet order: pages in page-id order, topics in set order on a page
    assert re.findall(r"^→ (.*)$", body, re.M) == ["PC-01 x001 · MM-RS x002", "MM-RS x003", "PC-01 x004"]
    assert [k["xid"] for k in key] == ["x001", "x002", "x003", "x004"]
    assert {k["xid"]: (k["set"], k["grade"]) for k in key} == {"x001": ("T1", 0), "x002": ("T2", None),
                                                              "x003": ("T1", 3), "x004": ("T2", None)}
    assert "копии: VKM-SRC-004:p0010" in body


# ------------------------------------------------------------------ ingest
def pool_for_ingest() -> dict:
    return {"PC-01": {"track": "PROCESS", "excluded_catalogue_matches": [], "excluded_t1_judged": [],
                      "t1_alias_links": [{"page": "VKM-SRC-005:p0001", "t1_pages": ["VKM-SRC-004:p0007"]}],
                      "units": [{"cid": "d001", "pages": ["VKM-SRC-252:p0001"], "systems": {"bm25": 6, "human3": 2},
                                 "variants": ["NAME"], "aliases": [],
                                 "per_page": {"VKM-SRC-252:p0001": {"bm25": 6, "human3": 2}}},
                                {"cid": "d002", "pages": ["VKM-SRC-252:p0002", "VKM-SRC-253:p0002"],
                                 "systems": {"hybrid_late": 1, "human3": 1}, "variants": ["PARA1"],
                                 "aliases": ["VKM-SRC-254:p0002"],
                                 "per_page": {"VKM-SRC-252:p0002": {"hybrid_late": 1},
                                              "VKM-SRC-253:p0002": {"human3": 1}}}]},
            "MM-RS": {"track": "MODEL_FAMILY", "excluded_catalogue_matches": [], "excluded_t1_judged": [],
                      "t1_alias_links": [],
                      "units": [{"cid": "d001", "pages": ["VKM-SRC-004:p0007"], "systems": {"nav": 4},
                                 "variants": ["NAME"], "aliases": [],
                                 "per_page": {"VKM-SRC-004:p0007": {"nav": 4}}}]}}


def test_build_rows_and_checks(t2):
    pool_topics = pool_for_ingest()
    judg = {("PC-01", "d001"): (3, "KEY_QUANT"), ("PC-01", "d002"): (1, "MENTION"), ("MM-RS", "d001"): (0, "OFF_TOPIC")}
    rows, missing, bad, extra, groups = t2.build_rows(pool_topics, judg)
    assert (missing, bad, extra) == ([], [], [])
    assert [(r["query_id"], r["doc_id"], r["grade"], r["pooled_from"]) for r in rows] == [
        ("PC-01", "VKM-SRC-252:p0001", "3", "human3@2,bm25@6"), ("PC-01", "VKM-SRC-252:p0002", "1", "hybrid_late@1"),
        ("PC-01", "VKM-SRC-253:p0002", "1", "human3@1"), ("MM-RS", "VKM-SRC-004:p0007", "0", "nav@4")]
    assert {r["label_source"] for r in rows} == {"LLM_AGENT_T2"}
    assert groups == [{"topic_id": "PC-01", "pages": ["VKM-SRC-252:p0002", "VKM-SRC-253:p0002"],
                       "aliases": ["VKM-SRC-254:p0002"]}]
    judged = t2.t1_judged(TOPICS, T1_ROWS, T1_GROUPS)
    assert t2.validate_rows(rows, TOPICS, judged) == []
    # wrong grade/code, missing, extra
    judg2 = {("PC-01", "d001"): (2, "KEY_QUANT"), ("MM-RS", "d001"): (0, "OFF_TOPIC"), ("MM-RS", "d009"): (1, "LIST")}
    _rows, missing, bad, extra, _g = t2.build_rows(pool_topics, judg2)
    assert (missing, bad, extra) == ([("PC-01", "d002")], [("PC-01", "d001")], [("MM-RS", "d009")])
    # the T1 checks with the T2 label source, the catalogue wins, T1 pairs are not labelled again
    broken = [dict(rows[0], label_source="LLM_AGENT_T1"), dict(rows[1], rationale="KEY_QUANT"),
              dict(rows[2], pooled_from="human3"), dict(rows[3], query_id="PC-01", doc_id="VKM-SRC-004:p0007"),
              dict(rows[3], doc_id="VKM-SRC-002:p0010"), dict(rows[1], status="VERIFIED")]
    problems = "\n".join(t2.validate_rows(broken, TOPICS, judged))
    for needle in ("label_source 'LLM_AGENT_T1'", "code 'KEY_QUANT' does not fit grade 1", "pooled_from 'human3'",
                   "PC-01 VKM-SRC-004:p0007: pair judged in T1", "catalogue target of the topic",
                   "VERIFIED/POOL_JUDGMENT/LLM_AGENT_T2"):
        assert needle in problems, needle


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_ingest_writes_new_files_and_never_the_t1_files(t2, tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="never written"):
        t2.ingest(out_tsv=t2.PT.OUT_TSV)
    with pytest.raises(SystemExit, match="never written"):
        t2.ingest(out_tsv=tmp_path / "q.tsv", out_groups=t2.PT.OUT_GROUPS)
    before = (sha(t2.PT.OUT_TSV), sha(t2.PT.OUT_GROUPS))
    work = tmp_path / "lb"
    (work / "judgments").mkdir(parents=True)
    (work / "pool_t2.json").write_text(json.dumps({"summary": {"canonical_snapshot_id": t2.SNAPSHOT},
                                                   "topics": pool_for_ingest()}), encoding="utf-8")
    (work / "judgments" / "pk-001.txt").write_text(
        "# pk-001 LLM_AGENT_T2\nPC-01|d001|3|KEY_QUANT\nPC-01|d002|2|SUP_FIGTAB\n", encoding="utf-8")
    monkeypatch.setenv("LB_WORK", str(work))
    monkeypatch.delenv("LB_JUDGMENTS", raising=False)
    monkeypatch.setattr(t2.PT, "load_topics", lambda: TOPICS)
    monkeypatch.setattr(t2, "load_t1", lambda: (T1_ROWS, T1_GROUPS))
    out_tsv, out_groups = tmp_path / "q.tsv", tmp_path / "g.json"
    with pytest.raises(SystemExit, match="not all pooled units"):
        t2.ingest(out_tsv=out_tsv, out_groups=out_groups)
    (work / "judgments" / "pk-002.txt").write_text("MM-RS|d001|1|NEIGHBOUR\n", encoding="utf-8")
    t2.ingest(out_tsv=out_tsv, out_groups=out_groups)
    rows = B.load_pooled_qrels(out_tsv)
    assert [(r["query_id"], r["doc_id"], r["grade"], r["rationale"]) for r in rows] == [
        ("PC-01", "VKM-SRC-252:p0001", "3", "KEY_QUANT"), ("PC-01", "VKM-SRC-252:p0002", "2", "SUP_FIGTAB"),
        ("PC-01", "VKM-SRC-253:p0002", "2", "SUP_FIGTAB"), ("MM-RS", "VKM-SRC-004:p0007", "1", "NEIGHBOUR")]
    doc = json.loads(out_groups.read_text(encoding="utf-8"))
    assert doc["label_source"] == "LLM_AGENT_T2" and doc["qrels_sha256"] == sha(out_tsv)
    assert doc["t1_alias_links"] == [{"topic_id": "PC-01", "page": "VKM-SRC-005:p0001",
                                      "t1_pages": ["VKM-SRC-004:p0007"]}]
    assert (sha(t2.PT.OUT_TSV), sha(t2.PT.OUT_GROUPS)) == before


# ------------------------------------------------------------------ agreement (xscore)
def kappa_quadratic_independent(a: list[int], b: list[int], k: int = 4) -> float:
    """1 − Σ w·O / Σ w·E with disagreement weights (i − j)² (the textbook form)."""
    n = len(a)
    ra = [a.count(i) for i in range(k)]
    cb = [b.count(j) for j in range(k)]
    o = sum((x - y) ** 2 for x, y in zip(a, b)) / n
    e = sum((i - j) ** 2 * ra[i] * cb[j] for i in range(k) for j in range(k)) / (n * n)
    return 1 - o / e


def alpha_ordinal_independent(a: list[int], b: list[int]) -> float:
    """Krippendorff's alpha, ordinal metric, from pairs: 1 − D_o / D_e with D_o the mean δ² of the units' pairs and
    D_e the mean δ² over all ordered pairs of the pooled values."""
    values = a + b
    nv = {c: values.count(c) for c in set(values)}

    def d2(c: int, e: int) -> float:
        lo, hi = min(c, e), max(c, e)
        return (sum(nv.get(g, 0) for g in range(lo, hi + 1)) - (nv[c] + nv[e]) / 2) ** 2

    do = sum(d2(x, y) for x, y in zip(a, b)) / len(a)
    de = sum(d2(x, y) for x, y in itertools.permutations(values, 2)) / (len(values) * (len(values) - 1))
    return 1 - do / de


def test_agreement_maths(t2):
    agree = t2.PT.agreement
    perfect = agree([0, 1, 2, 3, 3], [0, 1, 2, 3, 3])
    assert all(perfect[m] == 1.0 for m in ("exact", "within_one", "kappa_quadratic", "krippendorff_alpha_ordinal"))
    # hand-computed: κ_q = 11/12, α_ordinal = 1 − 7·8/624, κ = (3/4 − 1/4)/(3/4)
    res = agree([0, 1, 2, 3], [0, 1, 3, 3])
    assert (res["exact"], res["within_one"], res["kappa"]) == (0.75, 1.0, 0.6667)
    assert res["kappa_quadratic"] == round(11 / 12, 4) and res["krippendorff_alpha_ordinal"] == round(1 - 56 / 624, 4)
    rnd = random.Random(5)
    for _ in range(25):
        n = rnd.randint(5, 60)
        a = [rnd.randint(0, 3) for _ in range(n)]
        b = [min(3, max(0, x + rnd.choice([-2, -1, 0, 0, 0, 1]))) for x in a]
        if len(set(a + b)) < 2:
            continue
        res = agree(a, b)
        assert res["kappa_quadratic"] == pytest.approx(kappa_quadratic_independent(a, b), abs=6e-5)
        assert res["krippendorff_alpha_ordinal"] == pytest.approx(alpha_ordinal_independent(a, b), abs=6e-5)


def test_xscore_against_the_key(t2, tmp_path, monkeypatch):
    key = {"units": [{"xid": "x001", "set": "T1", "topic_id": "PC-01", "pages": ["VKM-SRC-004:p0007"], "grade": 3},
                     {"xid": "x002", "set": "T1", "topic_id": "MM-RS", "pages": ["VKM-SRC-007:p0001"], "grade": 0},
                     {"xid": "x003", "set": "T2", "topic_id": "PC-01", "pages": ["VKM-SRC-252:p0001"], "grade": None},
                     {"xid": "x004", "set": "T2", "topic_id": "MM-RS", "pages": ["VKM-SRC-252:p0009"], "grade": None},
                     {"xid": "x005", "set": "T2", "topic_id": "MM-RS", "pages": ["VKM-SRC-252:p0010"], "grade": None}]}
    text = ("# xc-001 second labeller\nPC-01|x001|2|SUP_DISCUSS\nMM-RS|x002|0|OFF_TOPIC\nPC-01|x003|3|KEY_OBS\n"
            "PC-01|x004|1|MENTION\nMM-RS|x005|2|KEY_MODEL\nMM-RS|x099|1|LIST\nbroken line\n"
            "MM-RS|x002|1|MENTION\n")
    second, problems = t2.parse_judgments(text, "xc.txt")
    assert any("bad line" in p for p in problems) and any("judged twice" in p for p in problems)
    pending = t2.xscore_result(key, second, None)
    assert pending["T2"] is None and pending["pending_t2"] == ["x003"] and pending["t2_ingested"] is False
    assert pending["missing"] == ["x004", "x005"]
    assert any("belongs to MM-RS" in p for p in pending["problems"])
    assert any("x005: code 'KEY_MODEL' does not fit grade 2" in p for p in pending["problems"])
    assert any("x099: unknown unit" in p for p in pending["problems"])
    assert pending["T1"]["n"] == 2 and pending["T1"]["exact"] == 0.0 and pending["T1"]["within_one"] == 1.0
    done = t2.xscore_result(key, second, {("PC-01", "VKM-SRC-252:p0001"): 3})
    assert done["T2"]["n"] == 1 and done["T2"]["exact"] == 1.0 and done["all"]["n"] == 3
    assert done["all"]["confusion_reference_rows"][0][1] == 1         # x002: reference 0 → second's last answer 1
    assert done["all"]["confusion_reference_rows"][3] == [0, 0, 1, 1]  # x001: 3 → 2; x003: 3 → 3
    assert {d["xid"] for d in done["disagreements"]} == {"x001", "x002"}
    # the command: key in LB_WORK, T2 grades from the ingested file
    work = tmp_path / "lb"
    work.mkdir()
    (work / "xcheck_key.json").write_text(json.dumps(key), encoding="utf-8")
    qrels = tmp_path / "t2.tsv"
    qrels.write_text("\t".join(B.POOLED_COLUMNS) + "\nPC-01\tPAGE\tVKM-SRC-252:p0001\t2\tCANDIDATE\tPOOL_JUDGMENT\t"
                     "LLM_AGENT_T2\tbm25@1\tSUP_ASPECT\n", encoding="utf-8")
    monkeypatch.setenv("LB_WORK", str(work))
    monkeypatch.setattr(t2, "OUT_TSV", qrels)
    answers = tmp_path / "xc-001.txt"
    answers.write_text("PC-01|x001|3|KEY_QUANT\nMM-RS|x002|0|NO_TEXT\nPC-01|x003|3|KEY_MECH\n", encoding="utf-8")
    t2.xscore(answers)
    res = json.loads((work / "xcheck_score.json").read_text(encoding="utf-8"))
    assert res["T1"]["exact"] == 1.0 and res["T2"]["n"] == 1 and res["T2"]["second_higher"] == 1
    assert res["all"]["n"] == 3 and res["file"] == "xc-001.txt"


def test_score_t2_merges_t1_and_t2_labels_and_alias_links(tmp_path, monkeypatch):
    """score_t2.merged_labels: T1 ∪ T2 rows, T2 groups appended, a T2 t1_alias_links page becomes an alias of its T1
    unit; a pair labelled in both T1 and T2 is refused."""
    spec = importlib.util.spec_from_file_location("topic_v1_score_t2", SCRIPT.parent / "score_t2.py")
    st2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st2)
    head = "query_id\tlevel\tdoc_id\tgrade\tstatus\tbasis\tlabel_source\tpooled_from\trationale\n"
    t1_tsv, t2_tsv, t2_groups = tmp_path / "t1.tsv", tmp_path / "t2.tsv", tmp_path / "t2_groups.json"
    t1_tsv.write_text(head + "PC-01\tPAGE\tVKM-SRC-001:p0001\t3\tCANDIDATE\tPOOL_JUDGMENT\tLLM_AGENT_T1\tbm25@1\tKEY_OBS\n",
                      encoding="utf-8")
    t2_tsv.write_text(head + "PC-01\tPAGE\tVKM-SRC-252:p0002\t2\tCANDIDATE\tPOOL_JUDGMENT\tLLM_AGENT_T2\tnav@2\tSUP_ASPECT\n",
                      encoding="utf-8")
    t2_groups.write_text(json.dumps({"groups": [{"topic_id": "PC-01", "pages": ["VKM-SRC-252:p0002"],
                                                  "aliases": ["VKM-SRC-253:p0002"]}],
                                     "t1_alias_links": [{"topic_id": "PC-01", "page": "VKM-SRC-260:p0001",
                                                         "t1_pages": ["VKM-SRC-001:p0001"]}]}), encoding="utf-8")
    monkeypatch.setattr(st2.P2, "load_t1", lambda: (B.load_pooled_qrels(t1_tsv), []))
    monkeypatch.setattr(st2.P2, "OUT_TSV", t2_tsv)
    monkeypatch.setattr(st2.P2, "OUT_GROUPS", t2_groups)
    monkeypatch.setattr(st2.PT, "OUT_TSV", t1_tsv)
    rows, groups, info = st2.merged_labels()
    assert [(r["query_id"], r["doc_id"], r["label_source"]) for r in rows] == [
        ("PC-01", "VKM-SRC-001:p0001", "LLM_AGENT_T1"), ("PC-01", "VKM-SRC-252:p0002", "LLM_AGENT_T2")]
    assert {"topic_id": "PC-01", "pages": ["VKM-SRC-001:p0001"], "aliases": ["VKM-SRC-260:p0001"]} in groups
    assert {"topic_id": "PC-01", "pages": ["VKM-SRC-252:p0002"], "aliases": ["VKM-SRC-253:p0002"]} in groups
    assert info["t1_rows"] == 1 and info["t2_rows"] == 1 and info["t1_alias_links"] == 1
    t2_tsv.write_text(head + "PC-01\tPAGE\tVKM-SRC-001:p0001\t1\tCANDIDATE\tPOOL_JUDGMENT\tLLM_AGENT_T2\tnav@2\tMENTION\n",
                      encoding="utf-8")
    with pytest.raises(SystemExit):
        st2.merged_labels()
