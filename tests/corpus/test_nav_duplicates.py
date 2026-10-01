"""Near-duplicate passages across sources (NAV part ``duplicates``) on synthetic rows: text rules, CPU kernels, kinds,
primary rules, the kNN channel, query functions, CLI and the concepts hook. All texts are invented; no corpus text.
The GPU backend is compared with the CPU one only where CuPy and a CUDA device are available."""
from __future__ import annotations

import hashlib
import json

import pytest

np = pytest.importorskip("numpy")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation import cli as nav_cli  # noqa: E402
from vkm_corpus.navigation import duplicates as D  # noqa: E402
from vkm_corpus.navigation import duplicates_query as Q  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402

# ------------------------------------------------------------------------------------------------ synthetic canon
P1 = ("Образцы каменной соли испытывали при постоянной нагрузке в течение трёх месяцев. Скорость установившейся "
      "ползучести росла с температурой и девиатором напряжений. На графиках видна стадия затухания, затем участок "
      "почти постоянной скорости и ускорение перед разрушением. Для описания данных принят степенной закон.")
P2 = ("Гидравлическая закладка выработанного пространства снижает конвергенцию камер и уменьшает оседание земной "
      "поверхности над панелью. Плотность закладочного массива контролировали отбором проб через каждые двадцать "
      "метров. Наблюдения по реперам показали замедление оседаний через полгода после начала работ.")
P3 = ("Водозащитная толща отделяет рудник от надсолевых водоносных горизонтов, поэтому её сплошность считается главным "
      "условием безопасной отработки. Трещины в глинисто-мергельной пачке выявляли скважинной геофизикой и "
      "сейсморазведкой. Участки с аномалиями включали в план детальных наблюдений.")
P4 = ("Маркшейдерская служба выполняет нивелирование по профильным линиям дважды в год. Точность превышений "
      "контролируют по замыканию ходов и повторным измерениям на опорных реперах. Результаты сводят в каталог отметок "
      "и строят графики оседаний по каждой линии наблюдательной станции.")
P5 = ("В статье рассматривается влияние ширины междукамерных целиков на их несущую способность при разной глубине "
      "разработки. Расчёты выполнены методом конечных элементов для трёх вариантов геометрии камер и целиков.")
ABS = ("Аннотация. Предложена методика оценки устойчивости целиков по данным натурных наблюдений за конвергенцией. "
       "Показано, что скорость конвергенции согласуется с расчётными напряжениями в целиках. Ключевые слова: целик, "
       "конвергенция, устойчивость, наблюдения.")
Q_TEXT = ("Газодинамические явления на калийных рудниках связаны с зонами замещения и скоплениями свободных газов в "
          "глинистых прослоях. Выбросы соли и газа чаще происходят при проходке подготовительных выработок вблизи "
          "тектонических нарушений.")
P6 = ("Температура массива на глубине пятисот метров составляет около двадцати градусов, а её рост с глубиной "
      "определяет режим вентиляции. Тепловые расчёты выполняли по данным термометрии скважин и рудничного воздуха.")
P7 = ("Сейсмическая сеть рудника регистрирует слабые события в кровле отработанных панелей. Каталог событий "
      "сопоставляют с планами горных работ и данными о затоплении отдельных участков шахтного поля.")
TEMPLATE = ["Министерство науки и высшего образования Российской Федерации",
            "Федеральное государственное бюджетное образовательное учреждение высшего образования",
            "На правах рукописи",
            "Диссертация на соискание ученой степени кандидата технических наук",
            "Научный руководитель: доктор технических наук, профессор"]
REFLIST = ("Список литературы\n1. Петров П.П. Устойчивость целиков // Горный журнал. – 2001. – № 3. – С. 12–15.\n"
           "2. Сидоров С.С. Ползучесть соли. – М.: Недра, 1999. – 200 с.\n"
           "3. Кузнецов К.К. Оседания поверхности // Физико-технические проблемы. – 2005. – № 4. – С. 30–41.")


P8 = ("Рудничная вентиляция обеспечивает подачу свежего воздуха к забоям по системе выработок и скважин. Главные "
      "вентиляторные установки работают на всасывание, а распределение воздуха регулируют перемычками и дверями.")


def ocr_light(text: str) -> str:
    """A second scan of the same page: Latin look-alikes, a hyphenated line break, ё lost."""
    return text.replace("о", "o", 3).replace("а", "a", 2).replace("ё", "е").replace("нивелирование", "нивели-\nрование")


def ocr(text: str) -> str:
    """... plus a real recognition error that normalisation does not undo."""
    return ocr_light(text.replace("Маркшейдерская", "Маркщейдерская"))


# source → (work, work type, year, link type, pages {index: [(block type, text)]})
CANON = {
    "SYN-A": ("WRK-A", "MONOGRAPH", 1996, "FULL_COPY", {1: [("HEADING", "Глава 1. Свойства соли"), ("TEXT", P1)],
                                                         2: [("TEXT", P2)], 3: [("TEXT", P3)], 4: [("TEXT", P4)]}),
    "SYN-B": ("WRK-A", "MONOGRAPH", 1996, "FULL_COPY", {1: [("TEXT", ocr(P4))]}),
    "SYN-C": ("WRK-C", "TEXTBOOK", 2008, "FULL_COPY", {1: [("TEXT", P1)], 2: [("TEXT", P2)], 3: [("TEXT", P3)],
                                                        4: [("TEXT", Q_TEXT)]}),
    "SYN-D": ("WRK-D", "JOURNAL_ARTICLE", 2015, "FULL_COPY", {1: [("TITLE", "Устойчивость целиков"), ("TEXT", ABS)],
                                                               2: [("TEXT", P5)]}),
    "SYN-E": ("WRK-E", "JOURNAL_ISSUE", 2015, "FULL_COPY", {3: [("TEXT", P7)],
                                                             7: [("TITLE", "Устойчивость целиков"), ("TEXT", ABS)]}),
    "SYN-F": ("WRK-F", "DISSERTATION", 2019, "FULL_COPY", {1: [("TEXT", t) for t in TEMPLATE]
                                                           + [("TITLE", "Иванов Иван. Прочность соли")]}),
    "SYN-G": ("WRK-G", "DISSERTATION", 2020, "FULL_COPY", {1: [("TEXT", t) for t in TEMPLATE]
                                                           + [("TITLE", "Петров Пётр. Закладка камер")]}),
    "SYN-H": ("WRK-H", "DISSERTATION", 2021, "FULL_COPY", {1: [("TEXT", t) for t in TEMPLATE]
                                                           + [("TITLE", "Сидоров Сидор. Оседания")]}),
    "SYN-I": ("WRK-I", "MONOGRAPH", 2012, "FULL_COPY", {1: [("TEXT", Q_TEXT)]}),
    "SYN-J": ("WRK-J", "MONOGRAPH", None, "FULL_COPY", {1: [("TEXT", P6)]}),
    "SYN-K": ("WRK-K", "MONOGRAPH", 2005, "FULL_COPY", {1: [("TEXT", P6)]}),
    "SYN-L": ("WRK-L", "MONOGRAPH", 2000, "FULL_COPY", {1: [("TEXT", REFLIST)]}),
    "SYN-M": ("WRK-M", "MONOGRAPH", 2001, "FULL_COPY", {1: [("TEXT", REFLIST)]}),
    "SYN-N": ("WRK-N", "MONOGRAPH", 2010, "FULL_COPY", {1: [("TEXT", P8)], 2: [("TEXT", "Короткая строка.")]}),
}


def make_canon(con, canon=CANON) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS canonical")
    con.execute("CREATE TABLE canonical.pages (page_id VARCHAR, source_id VARCHAR, page_index INTEGER)")
    con.execute("""CREATE TABLE canonical.blocks (object_id VARCHAR, page_id VARCHAR, source_id VARCHAR,
                   is_primary_layer BOOLEAN, block_type VARCHAR, reading_order INTEGER, normalized_text VARCHAR)""")
    con.execute("""CREATE TABLE canonical.works (work_id VARCHAR, publication_year SMALLINT, work_type VARCHAR,
                   anchor_source_id VARCHAR)""")
    con.execute("""CREATE TABLE canonical.source_work_links (source_id VARCHAR, work_id VARCHAR, link_type VARCHAR,
                   is_primary BOOLEAN, curation_status VARCHAR)""")
    works = {}
    for sid, (wid, wtype, year, link, pages) in canon.items():
        works.setdefault(wid, (year, wtype, sid))
        con.execute("INSERT INTO canonical.source_work_links VALUES (?, ?, ?, true, 'CURATED')", [sid, wid, link])
        for idx in range(1, max(pages) + 1):
            pid = f"{sid}:p{idx:04d}"
            con.execute("INSERT INTO canonical.pages VALUES (?, ?, ?)", [pid, sid, idx])
            for ro, (btype, text) in enumerate(pages.get(idx, []), 1):
                con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, true, ?, ?, ?)",
                            [f"{pid}:b{ro}", pid, sid, btype, ro, text])
            # the secondary text layer and running headers never enter units
            con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, false, 'TEXT', 90, ?)",
                        [f"{pid}:b90", pid, sid, P7])
            con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, true, 'PAGE_HEADER', 0, ?)",
                        [f"{pid}:b91", pid, sid, "Колонтитул издания"])
    for wid, (year, wtype, anchor) in works.items():
        con.execute("INSERT INTO canonical.works VALUES (?, ?, ?, ?)", [wid, year, wtype, anchor])


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    make_canon(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def built(con):
    stats: dict = {}
    res = D.build(con, stats=stats, backend="cpu")
    return res, stats


def _clusters_by_sources(res) -> dict[tuple, dict]:
    return {tuple(c["source_ids"]): c for c in res["dup_clusters"].to_pylist()}


# ------------------------------------------------------------------------------------------------ text rules
def test_norm_key_folds_case_script_hyphenation_and_punctuation():
    assert D.norm_key("Сiльвинит — KCl!") == D.norm_key("сильвинит kcl")
    assert D.norm_key("дефор-\nмации, ёмкость") == D.norm_key("деформации емкость")
    assert D.norm_key("кaменнaя сoль") == D.norm_key("каменная соль")  # Latin look-alikes inside Russian words
    assert D.norm_key(ocr_light(P4)) == D.norm_key(P4) != D.norm_key(ocr(P4))


def test_template_abstract_and_bibliography_rules():
    assert all(D.is_template(t) for t in TEMPLATE)
    assert D.is_template("Исследование выполнено при финансовой поддержке РФФИ.")
    assert D.is_template("All rights reserved. No part of this publication may be reproduced.")
    assert not D.is_template(P1) and not D.is_template(P2)
    assert D.has_abstract_marker(ABS) and not D.has_abstract_marker(P3)
    assert D.bibliography_share(REFLIST) == pytest.approx(1.0)
    assert D.bibliography_share(P1) == 0.0
    assert D.split_sentences("Первое. Второе!\nТретье") == ["Первое.", "Второе!", "Третье"]


# ------------------------------------------------------------------------------------------------ CPU kernels
def test_shingles_minhash_intersections_and_components():
    norms = [D.norm_key(P1), D.norm_key(P1), D.norm_key(P2), D.norm_key(P1[:120] + P2[:200])]
    keys, starts, counts = D.shingle_keys(norms, 8)
    assert counts[0] == counts[1] > 0 and len(keys) == int(counts.sum())
    k0 = keys[starts[0]:starts[0] + counts[0]]
    assert (np.diff(k0.astype(np.int64)) > 0).all()                    # unique and sorted per unit
    seeds = np.random.default_rng(1).integers(0, 2 ** 63, size=12, dtype=np.uint64)
    sig = D.minhash(keys, starts, counts, seeds)
    assert (sig[0] == sig[1]).all() and not (sig[0] == sig[2]).all()
    assert (0, 1) in D.lsh_pairs(sig, np.array([0, 1, 2, 3]), rows=3, max_bucket=10)
    assert (0, 1) not in D.lsh_pairs(sig, np.array([0, 0, 2, 3]), rows=3, max_bucket=10)   # same source: no pair
    assert (0, 1) in D.lsh_pairs(sig, np.array([0, 1, 2, 3]), rows=3, max_bucket=1)        # star over a big bucket
    inter = D.intersections(keys, starts, counts, np.array([0, 0, 2]), np.array([1, 2, 3]))
    assert inter[0] == counts[0] and inter[1] < counts[0] // 4 and inter[2] > 0
    np.testing.assert_array_equal(D.components(5, np.array([0, 3]), np.array([1, 4])), [0, 0, 2, 3, 3])


def test_knn_cpu_excludes_self():
    rng = np.random.default_rng(3)
    mat = rng.normal(size=(6, 8)).astype(np.float32)
    mat[1] = mat[0]
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    nn = D.knn(mat, 3)
    assert nn.shape == (6, 3) and all(i not in nn[i] for i in range(6))
    assert nn[0, 0] == 1 and nn[1, 0] == 0


# ------------------------------------------------------------------------------------------------ builder
def test_build_schemas_ids_and_rule_version(built):
    res, stats = built
    assert res["dup_clusters"].schema == D.CLUSTERS_SCHEMA
    assert res["dup_members"].schema == D.MEMBERS_SCHEMA
    assert res["source_overlap"].schema == D.OVERLAP_SCHEMA
    members = res["dup_members"].to_pylist()
    for c in res["dup_clusters"].to_pylist():
        mine = [m["unit_id"] for m in members if m["cluster_id"] == c["cluster_id"]]
        assert c["cluster_id"] == nav_ids.dup_cluster_id(mine) and c["n_members"] == len(mine)
        assert c["rule_version"] == nav_ids.RULE_VERSIONS["duplicates"] == "duplicates_v1"
        assert c["kind"] in D.KINDS and c["primary_rule"] in D.PRIMARY_RULES
    assert all(m["unit_id"].startswith("u1-") and m["unit_kind"] == "BLOCK_GROUP" for m in members)
    assert stats["counts"]["units_reference_list"] == 2 and stats["counts"]["units_short"] >= 1
    assert stats["params"]["min_containment"] == D.DEFAULTS["min_containment"]
    assert set(D.DEFAULTS) - {"backend", "gpu_mem_limit_gb"} <= set(stats["model_choices"])
    assert stats["backend"] == "cpu" and stats["layer_status"] == "DERIVED"


def test_kinds_and_primary_rules(built):
    res, _ = built
    cl = _clusters_by_sources(res)
    assert set(cl) == {("SYN-A", "SYN-C"), ("SYN-A", "SYN-B"), ("SYN-D", "SYN-E"), ("SYN-F", "SYN-G", "SYN-H"),
                       ("SYN-C", "SYN-I"), ("SYN-J", "SYN-K")} | set()
    reprints = [c for c in res["dup_clusters"].to_pylist() if c["source_ids"] == ["SYN-A", "SYN-C"]]
    assert len(reprints) == 3                                          # three passages reprinted in the textbook
    assert all(c["kind"] == "REPRINT" and c["primary_source_id"] == "SYN-A" and c["primary_rule"] == "EARLIEST_YEAR"
               and c["primary_year"] == 1996 for c in reprints)
    same = cl[("SYN-A", "SYN-B")]
    assert same["kind"] == "SAME_WORK_COPY" and same["primary_source_id"] == "SYN-A"
    assert same["primary_rule"] == "SAME_WORK_ANCHOR" and same["n_works"] == 1
    ab = cl[("SYN-D", "SYN-E")]
    assert ab["kind"] == "SHARED_ABSTRACT" and ab["primary_source_id"] == "SYN-D"
    assert ab["primary_rule"] == "TIE_WORK_TYPE"                       # the article before the journal issue
    bp = cl[("SYN-F", "SYN-G", "SYN-H")]
    assert bp["kind"] == "BOILERPLATE" and bp["primary_source_id"] is None and bp["primary_rule"] == "NOT_APPLICABLE"
    assert bp["template_share"] >= 0.5 and bp["reference_source_id"] == "SYN-F"
    po = cl[("SYN-C", "SYN-I")]
    assert po["kind"] == "PARTIAL_OVERLAP" and po["primary_source_id"] == "SYN-C"
    assert po["primary_rule"] == "EARLIEST_YEAR"
    unk = cl[("SYN-J", "SYN-K")]
    assert unk["primary_source_id"] is None and unk["primary_rule"] == "UNKNOWN_YEAR"
    assert unk["reference_source_id"] == "SYN-K"                      # the known year first; still no primary claim


def test_members_flags_and_similarity(built):
    res, _ = built
    members = res["dup_members"].to_pylist()
    by_cluster: dict[str, list] = {}
    for m in members:
        by_cluster.setdefault(m["cluster_id"], []).append(m)
    for c in res["dup_clusters"].to_pylist():
        ms = by_cluster[c["cluster_id"]]
        assert {m["source_id"] for m in ms if m["is_reference"]} == {c["reference_source_id"]}
        assert {m["source_id"] for m in ms if m["is_primary"]} == ({c["primary_source_id"]} if c["primary_source_id"]
                                                                    else set())
        for m in ms:
            assert 0.0 <= m["similarity"] <= 1.0 and 0.0 <= m["containment"] <= 1.0 and m["cosine"] is None
            if m["is_reference"]:
                assert m["containment"] == 1.0
    ocr_copy = [m for m in members if m["source_id"] == "SYN-B"][0]
    assert 0.8 < ocr_copy["containment"] < 1.0 and ocr_copy["page_id"] == "SYN-B:p0001"
    assert ocr_copy["block_ids"] == ["SYN-B:p0001:b1"]                # header and secondary layer excluded


def test_source_overlap_rows(built):
    res, _ = built
    rows = {(r["source_a"], r["source_b"]): r for r in res["source_overlap"].to_pylist()}
    ac = rows[("SYN-A", "SYN-C")]
    assert ac["n_shared_units"] == 3 and ac["n_shared_content"] == 3 and ac["relation"] == "REPRINT"
    assert ac["share_of_a"] == pytest.approx(3 / 4) and ac["share_of_b"] == pytest.approx(3 / 4)
    assert not ac["same_work"] and rows[("SYN-A", "SYN-B")]["same_work"]
    assert rows[("SYN-F", "SYN-G")]["relation"] == "BOILERPLATE" and rows[("SYN-F", "SYN-G")]["n_shared_content"] == 0
    assert all(a < b for a, b in rows)


def test_deterministic_rebuild(con, built):
    again = D.build(con, backend="cpu")
    for name in ("dup_clusters", "dup_members", "source_overlap"):
        assert again[name].equals(built[0][name])


# ------------------------------------------------------------------------------------------------ kNN channel
def _vec(text: str, dim: int = 64) -> list[float]:
    s = D.norm_key(text)
    v = np.zeros(dim)
    for i in range(len(s) - 2):
        v[int(hashlib.md5(s[i:i + 3].encode()).hexdigest(), 16) % dim] += 1.0
    return (v / np.linalg.norm(v)).astype(np.float32).tolist()


def _write_vectors(tmp_path, con, *, stale: str | None = None):
    units = D.load_units(con)
    rows = {"object_id": [], "object_type": [], "text_hash": [], "vector": []}
    for u in units:
        rows["object_id"].append(u["unit_id"])
        rows["object_type"].append("BLOCK_GROUP")
        th = hashlib.sha256(u["text"].encode("utf-8")).hexdigest()
        rows["text_hash"].append("0" * 64 if u["unit_id"] == stale else th)
        rows["vector"].append(_vec(u["text"]))
    d = tmp_path / "dense"
    d.mkdir()
    pq.write_table(pa.table(rows, schema=pa.schema([("object_id", pa.string()), ("object_type", pa.string()),
                                                    ("text_hash", pa.string()),
                                                    ("vector", pa.list_(pa.float32()))])), d / "part-00000.parquet")
    (d / "config.json").write_text(json.dumps({"config": {"model_id": "synthetic-trigram-hash", "dimension": 64},
                                               "config_signature": "synthetic"}), encoding="utf-8")
    return d, units


def test_knn_channel_finds_exact_copies_and_skips_stale_vectors(tmp_path, con, built):
    stale = [u for u in D.load_units(con) if u["source_id"] == "SYN-N"][0]["unit_id"]
    vdir, _ = _write_vectors(tmp_path, con, stale=stale)
    stats: dict = {}
    res = D.build(con, vectors=vdir, stats=stats, backend="cpu", channels=("knn",), knn_min_cosine=0.9)
    assert stats["vectors"]["stale"] == 1 and stats["vectors"]["model_id"] == "synthetic-trigram-hash"
    assert stats["counts"]["candidates_lsh"] == 0 and stats["counts"]["verified_knn_only"] > 0
    found = {tuple(c["source_ids"]) for c in res["dup_clusters"].to_pylist()}
    assert {("SYN-A", "SYN-C"), ("SYN-D", "SYN-E"), ("SYN-J", "SYN-K"), ("SYN-C", "SYN-I")} <= found
    exact = [m for m in res["dup_members"].to_pylist() if m["source_id"] in ("SYN-D", "SYN-E")]
    assert all(m["cosine"] == pytest.approx(1.0) for m in exact)
    both = D.build(con, vectors=vdir, backend="cpu")                    # LSH + kNN: same clusters as LSH alone here
    assert both["dup_clusters"].column("cluster_id").equals(built[0]["dup_clusters"].column("cluster_id"))
    with pytest.raises(FileNotFoundError):
        D.build(con, vectors=tmp_path / "missing", backend="cpu")


# ------------------------------------------------------------------------------------------------ queries
@pytest.fixture()
def qcon(built):
    c = duckdb.connect()
    for name in Q.DATASETS:
        c.register(f"nav_{name}", built[0][name])
    yield c
    c.close()


def test_copies_of_unit_page_block_and_missing(qcon, built):
    members = built[0]["dup_members"].to_pylist()
    c_p1 = [m for m in members if m["page_id"] == "SYN-C:p0001"][0]
    out = Q.copies_of(qcon, c_p1["unit_id"])
    assert out["match"] == "unit" and len(out["clusters"]) == 1
    cl = out["clusters"][0]
    assert cl["kind"] == "REPRINT" and cl["primary_source_id"] == "SYN-A" and not cl["this_is_primary"]
    assert [m["source_id"] for m in cl["copies"]] == ["SYN-A"] and cl["copies"][0]["is_primary"]
    assert out["pages"][0]["page_id"] == "SYN-A:p0001" and out["pages"][0]["has_primary"]
    assert Q.copies_of(qcon, "SYN-A:p0004")["match"] == "page"
    blk = Q.copies_of(qcon, "SYN-E:p0007:b2")
    assert blk["match"] == "block" and blk["clusters"][0]["kind"] == "SHARED_ABSTRACT"
    assert blk["clusters"][0]["copies"][0]["source_id"] == "SYN-D"
    none = Q.copies_of(qcon, "SYN-N:p0001")
    assert none["match"] is None and none["clusters"] == [] and "AUTO_EXTRACTED_UNREVIEWED" in none["note"]


def test_source_overlap_query(qcon):
    out = Q.source_overlap(qcon, "SYN-C")
    rows = {r["other_source_id"]: r for r in out["overlaps"]}
    assert set(rows) == {"SYN-A", "SYN-I"} and out["overlaps"][0]["other_source_id"] == "SYN-A"
    assert rows["SYN-A"]["relation"] == "REPRINT" and rows["SYN-A"]["earlier"] == "other"
    assert rows["SYN-I"]["relation"] == "PARTIAL_OVERLAP" and rows["SYN-I"]["earlier"] == "this"
    assert rows["SYN-A"]["share_of_this"] == pytest.approx(3 / 4)
    assert Q.source_overlap(qcon, "SYN-F", include_boilerplate=False)["overlaps"] == []
    assert Q.source_overlap(qcon, "SYN-C", min_shared=2)["n_overlaps"] == 1


def test_query_functions_registered_in_store():
    assert store.QUERY_FUNCTIONS["copies_of"].endswith("duplicates_query:copies_of")
    assert store.resolve("copies_of") is Q.copies_of and store.resolve("source_overlap") is Q.source_overlap


# ------------------------------------------------------------------------------------------------ CLI, ids
def test_ids_and_parts_registration():
    assert nav_ids.dup_cluster_id(["u1-b", "u1-a"]) == nav_ids.dup_cluster_id(["u1-a", "u1-b", "u1-a"])
    assert nav_ids.dup_cluster_id(["u1-a"]).startswith("DCL-")
    assert {"dup_clusters", "dup_members", "source_overlap"} <= set(nav_ids.DATASETS)
    order = list(nav_cli.PARTS)
    assert order.index("sections") < order.index("duplicates") < order.index("concepts")
    assert nav_cli.parse_part_options(["duplicates.backend=cpu", "concepts.drop_duplicate_blocks=true",
                                       "duplicates.knn_min_cosine=0.75"]) == {
        "duplicates": {"backend": "cpu", "knn_min_cosine": 0.75}, "concepts": {"drop_duplicate_blocks": True}}
    for bad in ("backend=cpu", "nosuch.key=1", "duplicates.=1"):
        with pytest.raises(ValueError):
            nav_cli.parse_part_options([bad])


def test_cli_build_writes_datasets_and_manifest(tmp_path):
    db = tmp_path / "canon.duckdb"
    c = duckdb.connect(str(db))
    make_canon(c)
    vdir, _ = _write_vectors(tmp_path, c)
    c.close()
    out = tmp_path / "nav"
    from vkm_corpus import cli

    assert cli.main(["nav", "build", "--duckdb", str(db), "--out", str(out), "--part", "duplicates",
                     "--vectors", str(vdir), "--option", "duplicates.backend=cpu"]) == 0
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    part = m["parts"]["duplicates"]
    assert part["status"] == "BUILT" and part["rule_version"] == "duplicates_v1"
    assert part["options"] == {"backend": "cpu"}
    assert part["stats"]["vectors"]["used"] and part["stats"]["backend"] == "cpu"
    assert part["stats"]["params"]["knn_min_cosine"] == D.DEFAULTS["knn_min_cosine"]
    for name in ("dup_clusters", "dup_members", "source_overlap"):
        meta = m["datasets"][name]
        assert pq.read_table(out / meta["path"]).num_rows == meta["rows"] > 0 and len(meta["sha256"]) == 64
    con = duckdb.connect()
    try:
        Q.attach(con, out)
        assert Q.copies_of(con, "SYN-A:p0001")["clusters"][0]["kind"] == "REPRINT"
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ concepts hook
EP1 = ("The brine inflow barrier protects the potash mine from the aquifer above the salt. Engineers watch the brine "
       "inflow barrier with gauges and repeat the leveling of the barrier surface every season.")
EP2 = ("Rock salt samples were loaded in compression until failure. The creep rate grew with temperature and with the "
       "deviatoric stress applied to the samples during the long tests in the laboratory.")
EP3 = ("Surveyors measured the subsidence trough above the old panels with leveling lines and satellite radar. The "
       "trough grew slowly after the mining stopped and reached a stable shape within several years.")


def test_concepts_hook_counts_a_copied_passage_once():
    from vkm_corpus.navigation import concepts as C

    canon = {"SYN-X": ("WRK-X", "MONOGRAPH", 2001, "FULL_COPY", {1: [("HEADING", "Brine inflow"), ("TEXT", EP1)],
                                                                  2: [("TEXT", EP2)]}),
             "SYN-Y": ("WRK-Y", "MONOGRAPH", 2010, "FULL_COPY", {1: [("HEADING", "Brine inflow"), ("TEXT", EP1)]}),
             "SYN-Z": ("WRK-Z", "MONOGRAPH", 2012, "FULL_COPY", {1: [("TEXT", EP3)]})}
    c = duckdb.connect()
    try:
        make_canon(c, canon)
        dup = D.build(c, backend="cpu")["dup_members"]
        assert D.copy_block_ids(dup) == {"SYN-Y:p0001:b1", "SYN-Y:p0001:b2"}
        opts = {"seeds": (), "workers": 1, "backend": "duckdb", "communities": False, "morphology": "crude"}
        plain = C.build(c, dup_members=dup, **opts)
        hooked = C.build(c, dup_members=dup, drop_duplicate_blocks=True, **opts)
        with pytest.raises(ValueError):
            C.build(c, drop_duplicate_blocks=True, **opts)
    finally:
        c.close()
    brine = lambda res: [t for t in res["terms"].to_pylist() if "brine" in t["lemma_key"]]  # noqa: E731
    assert brine(plain) and all(t["df_sources"] == 2 for t in brine(plain))
    assert brine(hooked) == []                                          # the reprint no longer adds a second source
    assert C.build_info(hooked["terms"])["counts"]["blocks_dropped_as_copies"] == 2
    assert C.build_info(plain["terms"])["counts"]["blocks_dropped_as_copies"] == 0


# ------------------------------------------------------------------------------------------------ GPU parity
def _gpu() -> bool:
    return D._gpu_modules() is not None


@pytest.mark.gpu
@pytest.mark.skipif(not _gpu(), reason="CuPy / CUDA device not available")
def test_gpu_backend_matches_cpu(tmp_path, con, built):
    vdir, _ = _write_vectors(tmp_path, con)
    cpu = D.build(con, vectors=vdir, backend="cpu")
    gpu = D.build(con, vectors=vdir, backend="gpu")
    for name in ("dup_clusters", "dup_members", "source_overlap"):
        assert gpu[name].equals(cpu[name])
