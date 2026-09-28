"""Topics of NAV (§7, rule topics_v1) on a synthetic canon with synthetic unit vectors (no corpus text): vector
freshness filter, apparatus exclusion, shared-page split at headings, section aggregates, nested topic levels, stable
ids, labels (N3 and fallback), query functions, CLI (--vectors / --inputs) and the CPU/GPU backends."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.embeddings.signature import embedding_signature  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402
from vkm_corpus.navigation import sections as S  # noqa: E402
from vkm_corpus.navigation import topics as T  # noqa: E402
from vkm_corpus.navigation import topics_query as Q  # noqa: E402
from vkm_corpus.retrieval_lab.canon import CanonReader  # noqa: E402
from vkm_corpus.retrieval_lab.units import build_units  # noqa: E402

CONFIG_SIG = "c" * 64
DIM = 16
DDL = """
CREATE SCHEMA canonical;
CREATE SCHEMA meta;
CREATE TABLE meta.snapshot (snapshot_id VARCHAR, manifest_sha256 VARCHAR, pipeline_version VARCHAR);
INSERT INTO meta.snapshot VALUES ('snap-topics-test', 'ab', '0.1.0');
CREATE TABLE canonical.pages (page_id VARCHAR, source_id VARCHAR, page_index INTEGER, page_kind VARCHAR,
  printed_page_labels VARCHAR[], normalized_text VARCHAR, char_count INTEGER);
CREATE TABLE canonical.blocks (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR, block_type VARCHAR,
  reading_order INTEGER, is_primary_layer BOOLEAN, normalized_text VARCHAR, language VARCHAR,
  bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE);
CREATE TABLE canonical.figures (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR, figure_label VARCHAR,
  caption VARCHAR, caption_normalized VARCHAR, caption_block_id VARCHAR, detected_figure_type VARCHAR,
  image_artifact_id VARCHAR, bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE);
CREATE TABLE canonical.tables (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR, table_label VARCHAR,
  caption VARCHAR, caption_normalized VARCHAR, caption_block_id VARCHAR, normalized_text VARCHAR,
  image_artifact_id VARCHAR, bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE);
CREATE TABLE canonical.formulas (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR, equation_label VARCHAR,
  normalized_latex VARCHAR, raw_output VARCHAR, raw_format VARCHAR, image_artifact_id VARCHAR,
  bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE);
CREATE TABLE canonical.bibliography_entries (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR,
  normalized_text VARCHAR, language VARCHAR);
CREATE TABLE canonical.source_work_links (source_id VARCHAR, work_id VARCHAR, is_primary BOOLEAN);
CREATE TABLE canonical.works (work_id VARCHAR, title VARCHAR);
"""
# invented sentences (≥ 250 characters per block, so text units never merge across a heading)
THEMES = {
    "creep": ("Ползучесть каменной соли при постоянной нагрузке изучали в длительных опытах на образцах. "
              "Скорость ползучести растет с температурой и девиатором напряжений, ползучесть соли нелинейна. "),
    "backfill": ("Закладка выработанного пространства снижает деформации кровли и целиков камер рудника. "
                 "Закладочный материал подают в камеры после отработки, закладка камер идет по графику работ. "),
    "subsidence": ("Оседание земной поверхности над выработками измеряют нивелированием по профильным линиям. "
                   "Мульда сдвижения растет со временем, оседание поверхности замедляется после закладки. "),
}
TITLES = {"creep": "Ползучесть соли", "backfill": "Закладка камер", "subsidence": "Оседание поверхности"}
TERMS = {"creep": ["ползучесть", "каменная соль"], "backfill": ["закладка", "закладочный материал"],
         "subsidence": ["оседание", "мульда сдвижения"]}
BASE = {"creep": np.eye(DIM)[0], "backfill": 0.7 * np.eye(DIM)[0] + 0.714 * np.eye(DIM)[1],
        "subsidence": np.eye(DIM)[2]}
SOURCES = ("SYN-A", "SYN-B", "SYN-C")
TEST_OPTS = {"resolution": (1.0, 1.5, 1.0), "knn_k": (3, 2, 2), "min_topic_size": (2, 1, 1), "backend": "cpu",
             "n_central_units": 3, "edge_min_cosine": 0.3}


def _text(theme: str, n: int) -> str:
    return f"{THEMES[theme]}Блок {n}. {THEMES[theme]}"


class Corpus:
    """Three sources × three themes; SYN-B has a page shared by two sections; SYN-A ends with a reference list
    (no bibliography entries) and SYN-C with a bibliography page; SYN-C has a chapter over two sections."""

    def __init__(self, path: str | None = None) -> None:
        self.con = duckdb.connect(path or ":memory:")
        self.con.execute(DDL)
        self.n = 0
        self.sections: list[dict] = []
        self.section_pages: list[tuple[str, str, str, int]] = []
        self.theme_of: dict[str, str] = {}
        self.page_theme: dict[str, str] = {}

    def page(self, sid: str, i: int) -> str:
        pid = f"{sid}:p{i:04d}"
        self.con.execute("INSERT INTO canonical.pages VALUES (?, ?, ?, 'PDF_PAGE', [], '', 0)", [pid, sid, i])
        return pid

    def block(self, sid: str, pid: str, btype: str, text: str, ro: int, y0: float) -> str:
        self.n += 1
        oid = f"{pid}:b{self.n:05d}"
        self.con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, ?, ?, true, ?, 'ru', 50, ?, 550, ?)",
                         [oid, sid, pid, btype, ro, text, y0, y0 + 40])
        return oid

    def section(self, sid: str, title: str, level: int, ordinal: int, first: int, last: int, heading: str | None,
                parent: str | None = None, own_pages: bool = True, theme: str | None = None) -> str:
        sec = S.section_id(sid, "PDF_OUTLINE", level, ordinal, title)
        self.sections.append({"section_id": sec, "source_id": sid, "work_id": f"W-{sid}", "parent_section_id": parent,
                              "level": level, "ordinal": ordinal, "numbering": None, "title": title,
                              "title_path": title, "page_start_id": f"{sid}:p{first:04d}",
                              "page_end_id": f"{sid}:p{last:04d}", "page_start_index": first, "page_end_index": last,
                              "method": "PDF_OUTLINE", "confidence": 0.9, "heading_block_id": heading,
                              "rule_version": "sections_v1"})
        if own_pages:
            for i in range(first, last + 1):
                self.section_pages.append((sec, f"{sid}:p{i:04d}", sid, i))
        if theme:
            self.theme_of[sec] = theme
        return sec

    def tables(self) -> tuple[pa.Table, pa.Table]:
        rows = {f.name: [r.get(f.name) for r in self.sections] for f in S.SECTIONS_SCHEMA}
        sp = {"section_id": [r[0] for r in self.section_pages], "page_id": [r[1] for r in self.section_pages],
              "source_id": [r[2] for r in self.section_pages], "page_index": [r[3] for r in self.section_pages],
              "rule_version": ["sections_v1"] * len(self.section_pages)}
        return (pa.Table.from_pydict(rows, schema=S.SECTIONS_SCHEMA),
                pa.Table.from_pydict(sp, schema=S.SECTION_PAGES_SCHEMA))


def make_corpus(path: str | None = None) -> Corpus:
    c = Corpus(path)
    for sid in SOURCES:
        c.con.execute("INSERT INTO canonical.source_work_links VALUES (?, ?, true)", [sid, f"W-{sid}"])
        c.con.execute("INSERT INTO canonical.works VALUES (?, ?)", [f"W-{sid}", f"Синтетическая работа {sid}"])
    order = 0
    # SYN-A: creep p1-2 (+ formula, figure), backfill p3-4, subsidence p5-6 (+ table), references p7 (no bib rows)
    for k, (theme, first) in enumerate((("creep", 1), ("backfill", 3), ("subsidence", 5))):
        heading = None
        for i in (first, first + 1):
            pid = c.page("SYN-A", i)
            c.page_theme[pid] = theme
            h = c.block("SYN-A", pid, "HEADING", TITLES[theme], 1, 60) if i == first else None
            heading = heading or h
            c.block("SYN-A", pid, "TEXT", _text(theme, c.n), 2, 120)
        c.section("SYN-A", TITLES[theme], 1, k + 1, first, first + 1, heading, theme=theme)
    c.con.execute("INSERT INTO canonical.formulas VALUES ('SYN-A:p0001:f1', 'SYN-A', 'SYN-A:p0001', '(1)', "
                  "'\\dot\\varepsilon = A \\sigma^n', NULL, 'LATEX', NULL, 50, 170, 550, 190)")
    c.con.execute("INSERT INTO canonical.figures VALUES ('SYN-A:p0002:g1', 'SYN-A', 'SYN-A:p0002', 'Рис. 1', "
                  "'Кривая ползучести образца', 'Кривая ползучести образца', NULL, 'CHART', NULL, 50, 200, 550, 400)")
    c.con.execute("INSERT INTO canonical.tables VALUES ('SYN-A:p0005:t1', 'SYN-A', 'SYN-A:p0005', 'Таблица 1', "
                  "'Оседания реперов', 'Оседания реперов', NULL, 'репер | оседание\n1 | 10', NULL, 50, 200, 550, 300)")
    pid = c.page("SYN-A", 7)
    h = c.block("SYN-A", pid, "HEADING", "Литература", 1, 60)
    c.block("SYN-A", pid, "REFERENCE_LIST", "1. Автор А. Синтетическая книга. 2001. " * 8, 2, 120)
    c.section("SYN-A", "Литература", 1, 4, 7, 7, h)
    # SYN-B: creep p1-2, backfill starts on p2 (shared page) to p3, subsidence p4-5
    p1, p2, p3 = c.page("SYN-B", 1), c.page("SYN-B", 2), c.page("SYN-B", 3)
    hc = c.block("SYN-B", p1, "HEADING", TITLES["creep"], 1, 60)
    c.block("SYN-B", p1, "TEXT", _text("creep", c.n), 2, 120)
    c.block("SYN-B", p2, "TEXT", _text("creep", c.n), 1, 60)
    hb = c.block("SYN-B", p2, "HEADING", TITLES["backfill"], 2, 400)
    c.block("SYN-B", p2, "TEXT", _text("backfill", c.n), 3, 460)
    c.con.execute("INSERT INTO canonical.formulas VALUES ('SYN-B:p0002:f1', 'SYN-B', 'SYN-B:p0002', '(2)', "
                  "'q = k h', NULL, 'LATEX', NULL, 50, 520, 550, 540)")          # below the backfill heading
    c.block("SYN-B", p3, "TEXT", _text("backfill", c.n), 1, 60)
    c.section("SYN-B", TITLES["creep"], 1, 1, 1, 2, hc, theme="creep")
    c.section("SYN-B", TITLES["backfill"], 1, 2, 2, 3, hb, theme="backfill")
    c.page_theme.update({p1: "creep", p3: "backfill"})
    first_sub = None
    for i in (4, 5):
        pid = c.page("SYN-B", i)
        c.page_theme[pid] = "subsidence"
        h = c.block("SYN-B", pid, "HEADING", TITLES["subsidence"], 1, 60) if i == 4 else None
        first_sub = first_sub or h
        c.block("SYN-B", pid, "TEXT", _text("subsidence", c.n), 2, 120)
    c.section("SYN-B", TITLES["subsidence"], 1, 3, 4, 5, first_sub, theme="subsidence")
    # SYN-C: chapter (p1-4, no own pages) over creep p1-2 and backfill p3-4; subsidence p5-6; bibliography p7
    chapter = c.section("SYN-C", "Глава 1", 1, 1, 1, 4, None, own_pages=False)
    ordinal = 2
    for theme, first in (("creep", 1), ("backfill", 3), ("subsidence", 5)):
        heading = None
        for i in (first, first + 1):
            pid = c.page("SYN-C", i)
            c.page_theme[pid] = theme
            h = c.block("SYN-C", pid, "HEADING", TITLES[theme], 1, 60) if i == first else None
            heading = heading or h
            c.block("SYN-C", pid, "TEXT", _text(theme, c.n), 2, 120)
        c.section("SYN-C", TITLES[theme], 2 if theme != "subsidence" else 1, ordinal, first, first + 1, heading,
                  parent=chapter if theme != "subsidence" else None, theme=theme)
        ordinal += 1
    pid = c.page("SYN-C", 7)
    h = c.block("SYN-C", pid, "HEADING", "Список литературы", 1, 60)
    c.block("SYN-C", pid, "REFERENCE_LIST", "1. Автор Б. Синтетическая статья. 2002. " * 8, 2, 120)
    c.con.execute("INSERT INTO canonical.bibliography_entries VALUES ('SYN-C:p0007:e1', 'SYN-C', 'SYN-C:p0007', "
                  "'Автор Б. Синтетическая статья. 2002.', 'ru')")
    c.section("SYN-C", "Список литературы", 1, 5, 7, 7, h)
    c.chapter = chapter
    c.order = order
    return c


def unit_vector(theme: str, source: str, rng) -> np.ndarray:
    v = BASE[theme] + 0.25 * np.eye(DIM)[3 + SOURCES.index(source)] + 0.05 * rng.standard_normal(DIM)
    return v / np.linalg.norm(v)


def write_vectors(c: Corpus, out: Path, *, stale: bool = True, bad_hash: bool = True) -> dict:
    """Vectors of the current units (+ a stale unit and a unit with a wrong text hash)."""
    rows = CanonReader(c.con, "test").load_all()
    units = build_units(rows["pages"], rows["blocks"], rows["figures"], rows["tables"], rows["formulas"],
                        rows["bibliography"])
    rng = np.random.default_rng(3)
    recs = []
    for u in units:
        if u.kind == "BIB_ENTRY":
            continue
        theme = c.page_theme.get(u.page_id, "subsidence")
        if u.page_id == "SYN-B:p0002":                        # shared page: the unit text tells its theme
            theme = "backfill" if "Закладка" in u.text or "q = k h" in u.text else "creep"
        recs.append((u.unit_id, u.source_id, u.page_id, u.kind, u.text_sha256, unit_vector(theme, u.source_id, rng)))
    if stale:
        recs.append(("u1-" + "0" * 16, "SYN-A", "SYN-A:p0001", "BLOCK_GROUP", "f" * 64, BASE["subsidence"]))
    if bad_hash:
        u0 = recs[0]
        recs[0] = (u0[0], u0[1], u0[2], u0[3], "e" * 64, u0[5])
    out.mkdir(parents=True, exist_ok=True)
    t = pa.table({
        "object_id": [r[0] for r in recs], "source_id": [r[1] for r in recs], "page_id": [r[2] for r in recs],
        "object_type": [r[3] for r in recs], "text_hash": [r[4] for r in recs],
        "model_id": ["test/model"] * len(recs), "model_revision": ["rev"] * len(recs),
        "config_hash": [CONFIG_SIG] * len(recs),
        "embedding_signature": [embedding_signature(r[0], r[4], CONFIG_SIG) for r in recs],
        "dimension": pa.array([DIM] * len(recs), pa.int32()), "precision": ["float32"] * len(recs),
        "vector": pa.array([r[5].astype(np.float32).tolist() for r in recs], pa.list_(pa.float32()))})
    half = t.num_rows // 2
    pq.write_table(t.slice(0, half), out / "part-test-00000.parquet")
    pq.write_table(t.slice(half), out / "part-test-00001.parquet")
    (out / "config.json").write_text(json.dumps({"config_signature": CONFIG_SIG, "kind": "dense",
                                                 "config": {"text_rule": "vkm-units-v1/A", "dimension": DIM}}),
                                     encoding="utf-8")
    return {u.unit_id: u for u in units}


def n3_tables(c: Corpus) -> tuple[pa.Table, pa.Table]:
    """Synthetic N3 rows: two terms per theme mentioned in every section of that theme."""
    from vkm_corpus.navigation.concepts import MENTIONS_SCHEMA, TERMS_SCHEMA, Morphology, phrase_keys

    morph = Morphology()
    terms, mentions = {f.name: [] for f in TERMS_SCHEMA}, {f.name: [] for f in MENTIONS_SCHEMA}
    for theme, lemmas in TERMS.items():
        for rank, lemma in enumerate(lemmas):
            key = phrase_keys(lemma, morph)[0]
            tid = nav_ids.term_id(key)
            for f, v in (("term_id", tid), ("lemma", lemma), ("lemma_key", key), ("surface_forms", [lemma]),
                         ("language", "ru"), ("n_words", len(lemma.split())), ("kind", "NP"), ("df_units", 3),
                         ("df_sources", 3), ("tf", 30), ("cvalue", 1.0), ("idf", 1.0), ("seed", False),
                         ("community", None), ("morphology", morph.name), ("rule_version", "concepts_v1")):
                terms[f].append(v)
            for sec, th in c.theme_of.items():
                if th != theme:
                    continue
                for f, v in (("term_id", tid), ("unit_id", "NCU-" + sec[4:]), ("unit_kind", "SECTION"),
                             ("section_id", sec), ("source_id", next(s["source_id"] for s in c.sections
                                                                   if s["section_id"] == sec)),
                             ("tf", 5 - rank), ("tfidf", float(5 - rank)), ("page_ids", []), ("best_block_ids", []),
                             ("rule_version", "concepts_v1")):
                    mentions[f].append(v)
    return pa.table(terms, schema=TERMS_SCHEMA), pa.table(mentions, schema=MENTIONS_SCHEMA)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("topics")
    c = make_corpus()
    vdir = tmp / "vectors" / CONFIG_SIG
    units = write_vectors(c, vdir)
    sections, section_pages = c.tables()
    terms, mentions = n3_tables(c)
    stats: dict = {}
    res = T.build(c.con, sections=sections, section_pages=section_pages, terms=terms, term_mentions=mentions,
                  vectors=str(vdir), stats=stats, **TEST_OPTS)
    return {"c": c, "vdir": vdir, "units": units, "sections": sections, "section_pages": section_pages,
            "terms": terms, "mentions": mentions, "res": res, "stats": stats, "tmp": tmp}


def _sec(c: Corpus, sid: str, theme: str) -> str:
    return next(s for s, th in c.theme_of.items() if th == theme and s in
                {x["section_id"] for x in c.sections if x["source_id"] == sid})


# ------------------------------------------------------------------------------------------ vectors and units
def test_vectors_are_filtered_by_snapshot_and_text_hash(world):
    v = world["stats"]["vectors"]
    assert v["config_hash"] == CONFIG_SIG and v["dimension"] == DIM and v["n_parts"] == 2
    assert v["stale_not_in_snapshot"] == 1 and v["text_hash_mismatch"] == 1
    assert v["apparatus_text_units"] == 1                      # the reference list of SYN-A
    assert v["apparatus_page_headings"] == 1                   # «Список литературы» of SYN-C (entries parsed)
    assert v["units_without_vector"]["BIB_ENTRY"] == 1
    assert v["bad_embedding_signature"] == 0 and v["text_hash_checked"] is True


def test_read_vectors_rejects_mixed_configs(tmp_path, world):
    t = pq.read_table(world["vdir"] / "part-test-00000.parquet")
    d = tmp_path / "mixed"
    d.mkdir()
    pq.write_table(t, d / "part-a.parquet")
    other = t.set_column(t.schema.get_field_index("config_hash"), "config_hash",
                         pa.array(["d" * 64] * t.num_rows))
    pq.write_table(other, d / "part-b.parquet")
    with pytest.raises(ValueError, match="config_hash"):
        T.read_vectors(d)


def test_bad_embedding_signature_is_dropped(tmp_path, world):
    t = pq.read_table(world["vdir"] / "part-test-00000.parquet")
    sig = t.column("embedding_signature").to_pylist()
    sig[0] = "0" * 64
    d = tmp_path / "badsig"
    d.mkdir()
    pq.write_table(t.set_column(t.schema.get_field_index("embedding_signature"), "embedding_signature",
                                pa.array(sig)), d / "part-a.parquet")
    vec = T.read_vectors(d)
    assert vec["info"]["bad_embedding_signature"] == 1 and len(vec["ids"]) == t.num_rows - 1


# ------------------------------------------------------------------------------------------ sections
def test_schemas_and_rule_versions(world):
    res = world["res"]
    assert set(res) == {"section_aggregates", "section_vectors", "topics", "topic_members", "topic_edges"}
    assert res["section_aggregates"].schema.equals(T.SECTION_AGGREGATES_SCHEMA)
    assert res["topics"].schema.equals(T.TOPICS_SCHEMA)
    assert res["topic_members"].schema.equals(T.TOPIC_MEMBERS_SCHEMA)
    assert res["topic_edges"].schema.equals(T.TOPIC_EDGES_SCHEMA)
    for t in res.values():
        assert set(t.column("rule_version").to_pylist()) <= {"topics_v1"}
    assert nav_ids.RULE_VERSIONS["topics"] == "topics_v1"
    assert {"section_aggregates", "topics"} <= set(nav_ids.DATASETS)


def test_section_vectors_are_unit_means(world):
    c, res = world["c"], world["res"]
    sv = res["section_vectors"]
    x = np.asarray(sv.column("vector").to_pylist())
    assert np.allclose(np.linalg.norm(x, axis=1), 1.0, atol=1e-6)
    ids = sv.column("section_id").to_pylist()
    themed = set(c.theme_of)
    assert set(ids) == themed                                  # both reference sections have no vector
    for i, sid in enumerate(ids):                              # each vector points to its theme
        th = c.theme_of[sid]
        assert x[i] @ (BASE[th] / np.linalg.norm(BASE[th])) > 0.85, (sid, th)
    agg = {r["section_id"]: r for r in res["section_aggregates"].to_pylist()}
    refs = [s["section_id"] for s in c.sections if s["title"] == "Литература"][0]
    assert agg[refs]["n_units"] == 0 and agg[refs]["central_unit_ids"] == []
    assert c.chapter not in agg                                # no own pages: no aggregate row


def test_shared_page_is_split_at_the_heading(world):
    c, res = world["c"], world["res"]
    agg = {r["section_id"]: r for r in res["section_aggregates"].to_pylist()}
    creep_b, backfill_b = _sec(c, "SYN-B", "creep"), _sec(c, "SYN-B", "backfill")
    units = world["units"]
    creep_pages = [units[u].page_id for u in agg[creep_b]["central_unit_ids"]]
    back_units = [units[u] for u in agg[backfill_b]["central_unit_ids"]]
    assert agg[creep_b]["n_units"] == 2 and "SYN-B:p0002" in creep_pages     # the text before the heading
    assert agg[backfill_b]["n_units"] == 3                                    # heading group, formula, page 3
    assert any(u.kind == "FORMULA" for u in back_units)
    st = world["stats"]["assignment"]
    assert st["shared_pages"] == 1 and st["placed_by_heading"] == 3 and st["shared_unplaced"] == 0
    assert agg[backfill_b]["n_formulas"] == 1 and agg[creep_b]["n_formulas"] == 0


def test_counts_central_units_and_key_terms(world):
    c, res = world["c"], world["res"]
    agg = {r["section_id"]: r for r in res["section_aggregates"].to_pylist()}
    creep_a = _sec(c, "SYN-A", "creep")
    assert agg[creep_a]["n_formulas"] == 1 and agg[creep_a]["n_figures"] == 1
    assert agg[_sec(c, "SYN-A", "subsidence")]["n_tables"] == 1
    bib = [s["section_id"] for s in c.sections if s["title"] == "Список литературы"][0]
    assert agg[bib]["n_bib_entries"] == 1
    r = agg[creep_a]
    assert 1 <= len(r["central_unit_ids"]) <= 3
    assert len(r["central_unit_ids"]) == len(r["central_page_ids"]) == len(r["central_object_ids"])
    assert all(p.startswith("SYN-A:p000") for p in r["central_page_ids"])
    assert r["key_terms"][:2] == ["ползучесть", "каменная соль"]
    assert r["key_term_ids"][0].startswith("TRM-")


# ------------------------------------------------------------------------------------------ topics
def test_topics_group_themes_across_sources_in_nested_levels(world):
    c, res = world["c"], world["res"]
    topics = {r["topic_id"]: r for r in res["topics"].to_pylist()}
    members = res["topic_members"].to_pylist()
    l1 = {}
    for m in members:
        if m["level"] == 1:
            l1.setdefault(m["topic_id"], set()).add(m["section_id"])
    groups = sorted(sorted({c.theme_of[s] for s in secs}) for secs in l1.values())
    assert groups == [["backfill"], ["creep"], ["subsidence"]]
    for tid, secs in l1.items():
        t = topics[tid]
        assert t["n_sections"] == 3 and t["n_sources"] == 3 and t["level"] == 1
        assert t["topic_id"] == nav_ids.topic_id(1, "topics_v1", sorted(secs))
        assert len(t["central_section_ids"]) == 3
        assert 0 < t["coherence"] <= 1
    # every section once per level; a parent's members are the union of its children's members
    levels = sorted({t["level"] for t in topics.values()})
    assert levels[0] == 1 and len(levels) >= 2
    for lv in levels:
        secs = [m["section_id"] for m in members if m["level"] == lv]
        assert len(secs) == len(set(secs)) == 9
    by_topic: dict[str, set] = {}
    for m in members:
        by_topic.setdefault(m["topic_id"], set()).add(m["section_id"])
    for t in topics.values():
        kids = [k for k in topics.values() if k["parent_topic_id"] == t["topic_id"]]
        if kids:
            assert set().union(*(by_topic[k["topic_id"]] for k in kids)) == by_topic[t["topic_id"]]
            assert t["n_children"] == len(kids)
        if t["parent_topic_id"]:
            assert topics[t["parent_topic_id"]]["level"] == t["level"] + 1
    creep_topic = next(tid for tid, secs in l1.items() if {c.theme_of[s] for s in secs} == {"creep"})
    assert topics[creep_topic]["label_terms"][0] == "ползучесть"
    # creep and backfill are closer to each other than to subsidence: they share the level-2 parent
    back_topic = next(tid for tid, secs in l1.items() if {c.theme_of[s] for s in secs} == {"backfill"})
    sub_topic = next(tid for tid, secs in l1.items() if {c.theme_of[s] for s in secs} == {"subsidence"})
    assert topics[creep_topic]["parent_topic_id"] == topics[back_topic]["parent_topic_id"]
    assert topics[sub_topic]["parent_topic_id"] != topics[creep_topic]["parent_topic_id"]
    edges = res["topic_edges"].to_pylist()
    assert edges and all(e["cosine"] >= TEST_OPTS["edge_min_cosine"] for e in edges)
    assert any({e["topic_id_a"], e["topic_id_b"]} == {creep_topic, back_topic} for e in edges)


def test_build_is_deterministic_and_ids_are_stable(world):
    c = world["c"]
    again = T.build(c.con, sections=world["sections"], section_pages=world["section_pages"], terms=world["terms"],
                    term_mentions=world["mentions"], vectors=str(world["vdir"]), **TEST_OPTS)
    for name, t in world["res"].items():
        assert again[name].equals(t), name


def test_fallback_labels_without_n3(world):
    c = world["c"]
    stats: dict = {}
    res = T.build(c.con, sections=world["sections"], section_pages=world["section_pages"], vectors=str(world["vdir"]),
                  stats=stats, **TEST_OPTS)
    assert stats["labels"].startswith("fallback")
    agg = {r["section_id"]: r for r in res["section_aggregates"].to_pylist()}
    kt = agg[_sec(c, "SYN-A", "creep")]["key_terms"]
    assert kt and all(len(k) >= 3 for k in kt) and any(k.startswith("ползуч") for k in kt[:3])
    labels = [r["label_terms"] for r in res["topics"].to_pylist() if r["level"] == 1]
    assert any(any(x.startswith("закладк") for x in lab) for lab in labels)
    assert all(k.startswith("WRD-") for k in agg[_sec(c, "SYN-A", "creep")]["key_term_ids"])


def test_skipped_without_vectors_or_sections(world):
    stats: dict = {}
    assert T.build(world["c"].con, sections=world["sections"], section_pages=world["section_pages"],
                   stats=stats) is None
    assert "vectors" in stats["skipped"]
    assert T.build(world["c"].con, vectors=str(world["vdir"]), stats={}) is None


def test_defaults_run_on_a_tiny_corpus(world):
    res = T.build(world["c"].con, sections=world["sections"], section_pages=world["section_pages"],
                  vectors=str(world["vdir"]), backend="cpu")
    assert res["topics"].num_rows >= 1 and res["topic_members"].num_rows >= 9


# ------------------------------------------------------------------------------------------ algorithms
def test_louvain_and_merge_small():
    # two 4-cliques joined by one weak edge + an isolated pair
    edges = [(a, b, 1.0) for g in ((0, 1, 2, 3), (4, 5, 6, 7)) for i, a in enumerate(g) for b in g[i + 1:]]
    edges += [(3, 4, 0.1), (8, 9, 1.0)]
    s, d, w = (np.asarray(x) for x in zip(*edges))
    lab = T.louvain(10, s, d, w, resolution=1.0)
    assert len(set(lab[:4])) == 1 and len(set(lab[4:8])) == 1 and lab[0] != lab[4] and lab[8] == lab[9]
    assert np.array_equal(lab, T.louvain(10, s, d, w, resolution=1.0))
    merged = T.merge_small(lab, s, d, w, 3)                        # the pair has no link: it stays
    assert len(set(merged)) == 3
    s2, d2, w2 = np.r_[s, 9], np.r_[d, 0], np.r_[w, 0.5]
    merged = T.merge_small(lab, s2, d2, w2, 3)
    assert merged[9] == merged[0] and merged[8] == merged[0]


def test_dyadic_sums_are_order_free():
    rng = np.random.default_rng(0)
    x = T.dyadic(rng.standard_normal((500, 8)).astype(np.float32) / 3, T.VEC_BITS)
    b = T.Backend("cpu")
    seg = np.zeros(500, dtype=np.int64)
    one = b.segment_sum(x, np.arange(500), seg, np.ones(500), 1)
    rev = b.segment_sum(x, np.arange(500)[::-1].copy(), seg, np.ones(500), 1)
    assert np.array_equal(one, rev)


def test_cpu_knn_splits_same_and_other_groups():
    x = T._normalise_rows(np.asarray([[1, 0], [0.9, 0.1], [0.8, 0.2], [0, 1]], dtype=np.float32))
    b = T.Backend("cpu")
    s, d, sim = b.knn(x, np.asarray([0, 0, 1, 1]), 1, 1)
    pairs = set(zip(s.tolist(), d.tolist()))
    assert (0, 1) in pairs and (0, 2) in pairs and (3, 2) in pairs and all(a != c for a, c in pairs)


@pytest.mark.gpu
@pytest.mark.skipif(importlib.util.find_spec("cupy") is None or importlib.util.find_spec("cugraph") is None,
                    reason="CuPy/cuGraph not installed (GPU backend)")
def test_gpu_backend_matches_cpu_vectors_and_repeats(world):
    if not T.gpu_available():
        pytest.skip("no CUDA device")
    c = world["c"]
    kw = dict(sections=world["sections"], section_pages=world["section_pages"], terms=world["terms"],
              term_mentions=world["mentions"], vectors=str(world["vdir"]))
    gpu = T.build(c.con, **kw, **{**TEST_OPTS, "backend": "gpu"})
    gpu2 = T.build(c.con, **kw, **{**TEST_OPTS, "backend": "gpu"})
    assert gpu["section_vectors"].equals(world["res"]["section_vectors"])
    assert gpu["section_aggregates"].equals(world["res"]["section_aggregates"])
    for name in gpu:
        assert gpu[name].equals(gpu2[name]), name


# ------------------------------------------------------------------------------------------ queries
@pytest.fixture(scope="module")
def qcon(world):
    d = world["tmp"] / "nav"
    d.mkdir(exist_ok=True)
    for name, t in world["res"].items():
        pq.write_table(t, d / f"{name}.parquet")
    pq.write_table(world["sections"], d / "sections.parquet")
    pq.write_table(world["section_pages"], d / "section_pages.parquet")
    pq.write_table(world["terms"], d / "terms.parquet")
    edges = pa.table({"edge_id": pa.array([], pa.string()), "kind": pa.array([], pa.string()),
                      "src_term_id": pa.array([], pa.string()), "dst_term_id": pa.array([], pa.string()),
                      "dst_ref": pa.array([], pa.string()), "weight": pa.array([], pa.float64())})
    con = world["c"].con
    Q.attach(con, d)
    con.execute(f"CREATE OR REPLACE TEMP VIEW nav_terms AS SELECT * FROM read_parquet('{(d / 'terms.parquet').as_posix()}')")
    con.register("nav_term_edges", edges)
    return con


def test_get_topic_has_path_children_members_and_neighbours(world, qcon):
    c = world["c"]
    creep_b = _sec(c, "SYN-B", "creep")
    l1 = next(r for r in Q.section_topics(qcon, creep_b) if r["level"] == 1)
    t = Q.get_topic(qcon, l1["topic_id"])
    assert t["level"] == 1 and t["n_sections"] == 3 and t["path"] and t["path"][0]["level"] > 1
    assert {m["source_id"] for m in t["members"]} == set(SOURCES)
    assert all(m["title"] == TITLES["creep"] and m["page_start_id"] for m in t["members"])
    assert [m["rank"] for m in t["members"]] == [1, 2, 3]
    assert {s["source_id"] for s in t["sources"]} == set(SOURCES)
    assert all(s["work_title"].startswith("Синтетическая работа") for s in t["sources"])
    assert t["neighbours"] and t["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    parent = Q.get_topic(qcon, t["parent_topic_id"])
    assert any(ch["topic_id"] == t["topic_id"] for ch in parent["children"])
    assert Q.get_topic(qcon, "TOP-missing") is None


def test_find_topics_by_label_and_member_terms(world, qcon):
    hits = Q.find_topics(qcon, "ползучести")
    assert hits and hits[0]["level"] == 1 and hits[0]["label_terms"][0] == "ползучесть"
    both = Q.find_topics(qcon, ["закладка", "ползучесть"], level=2)
    assert both and all(h["level"] == 2 for h in both)
    assert Q.find_topics(qcon, "несуществующее слово") == []
    assert Q.find_topics(qcon, []) == []


def test_similar_sections_prefers_other_sources_and_uses_subtrees(world, qcon):
    c = world["c"]
    creep_a = _sec(c, "SYN-A", "creep")
    r = Q.similar_sections(qcon, creep_a, k=2)
    assert r["basis"] == "own" and len(r["results"]) == 2
    assert {h["source_id"] for h in r["results"]} == {"SYN-B", "SYN-C"}
    assert all(c.theme_of[h["section_id"]] == "creep" for h in r["results"])
    assert all(h["topic_id"] and h["title"] == TITLES["creep"] for h in r["results"])
    same = Q.similar_sections(qcon, creep_a, k=8, other_sources_only=False)
    assert any(h["source_id"] == "SYN-A" for h in same["results"])
    chap = Q.similar_sections(qcon, c.chapter, k=3)             # the chapter has no own pages: subtree mean
    assert chap["basis"] == "subtree" and chap["n_subtree_sections"] == 2
    assert all(h["source_id"] != "SYN-C" for h in chap["results"])
    assert Q.similar_sections(qcon, "SEC-missing")["results"] == []


# ------------------------------------------------------------------------------------------ CLI and store
def test_cli_builds_topics_from_inputs_and_vectors(world, tmp_path):
    db = tmp_path / "canon.duckdb"
    c = make_corpus(str(db))
    write_vectors(c, tmp_path / "vec" / CONFIG_SIG)
    c.con.close()
    inputs = tmp_path / "nav_in"
    inputs.mkdir()
    sections, section_pages = world["sections"], world["section_pages"]
    pq.write_table(sections, inputs / "sections.parquet")
    pq.write_table(section_pages, inputs / "section_pages.parquet")
    (inputs / "manifest.json").write_text(json.dumps({"snapshot": {"snapshot_id": "snap-topics-test"}}),
                                          encoding="utf-8")
    from vkm_corpus import cli

    out = tmp_path / "topics"
    assert cli.main(["nav", "build", "--duckdb", str(db), "--out", str(out), "--part", "topics"]) == 0
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert m["parts"]["topics"]["status"] == "SKIPPED_NO_INPUT"
    assert cli.main(["nav", "build", "--duckdb", str(db), "--out", str(out), "--part", "topics",
                     "--vectors", str(tmp_path / "vec" / CONFIG_SIG), "--inputs", str(inputs)]) == 0
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    part = m["parts"]["topics"]
    assert part["status"] == "BUILT" and part["rule_version"] == "topics_v1"
    assert set(part["datasets"]) == {"section_aggregates", "section_vectors", "topics", "topic_members",
                                     "topic_edges"}
    assert m["vectors"]["dir"] == CONFIG_SIG and m["vectors"]["n_parts"] == 2
    assert m["inputs"]["snapshot_id"] == "snap-topics-test" and set(m["inputs"]["datasets"]) == {
        "sections", "section_pages"}
    assert "sections" not in m["datasets"]                     # inputs are read, never copied
    assert part["stats"]["labels"].startswith("fallback")
    for name in part["datasets"]:
        assert pq.read_table(out / f"{name}.parquet").num_rows == m["datasets"][name]["rows"]
    bad = tmp_path / "nav_other"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"snapshot": {"snapshot_id": "snap-other"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="snap-other"):
        cli.main(["nav", "build", "--duckdb", str(db), "--out", str(tmp_path / "x"), "--part", "topics",
                  "--vectors", str(tmp_path / "vec" / CONFIG_SIG), "--inputs", str(bad)])


def test_parts_and_query_functions_are_registered():
    from vkm_corpus.navigation import cli as nav_cli
    from vkm_corpus.navigation import store

    assert list(nav_cli.PARTS)[-1] == "topics"
    assert nav_cli.resolve_part("topics") is T.build
    for name in ("topic", "find_topics", "similar_sections", "section_topics"):
        assert callable(store.resolve(name))
