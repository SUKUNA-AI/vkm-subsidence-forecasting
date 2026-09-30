"""Figures, tables and formulas repeated across sources (NAV part ``object_duplicates``) on synthetic rows: LaTeX
canonical forms, image hashes on numpy arrays, candidate scans, table numbers, the builder (kinds, primaries, the
mutual-best assignment, context channels), query functions, CLI registration. Figure hashes enter through the hash
cache, so no image library is needed; the Pillow decoding path is tested only where Pillow is installed. All data are
invented; no corpus text or image."""
from __future__ import annotations

import hashlib
import json
from itertools import combinations

import pytest

np = pytest.importorskip("numpy")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation import cli as nav_cli  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402
from vkm_corpus.navigation import object_duplicates as OD  # noqa: E402
from vkm_corpus.navigation import object_duplicates_query as Q  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402


# ------------------------------------------------------------------------------------------------ formulas: canon
def key(latex: str) -> str:
    return OD.canonical_formula(latex)["latex_key"]


def test_canonical_latex_normalises_notation_variants():
    assert key(r"\sigma_{1} = E \cdot \varepsilon_{1}") == key(r"{\sigma}_1 = E\epsilon_1") == key(r"σ_1=E ε_1")
    assert key(r"\left( a + b \right)^{2}") == key(r"(a+b)^2")
    assert key(r"\frac{a}{b}") == key(r"\frac ab") != key(r"\frac{ab}{c}")
    assert key(r"\frac{ab}{c}") != key(r"\frac{a}{bc}")
    assert key(r"x = 0{,}5 y") == key(r"x = 0,5 y") == key(r"x = 0.5 y")
    assert key(r"\sigma = E \varepsilon ,") == key(r"\sigma = E \varepsilon.") == key(r"\sigma = E \varepsilon")
    assert key(r"x \leq y \tag{3.2}") == key(r"x \le y")
    assert key(r"\int f \mathrm{d} x") == key(r"\int f d x")
    assert key(r"\Delta t = t_2 - t_1").startswith("Δ t")          # an increment, not a symbol Δ
    assert key(r"\mathbf{E} = m c^{2}") == key(r"E = m c^2")         # fonts dropped


def test_shape_key_renames_identifiers_in_order():
    a = OD.canonical_formula(r"\sigma = E \varepsilon + \frac{\eta}{E} \dot{\varepsilon}")
    b = OD.canonical_formula(r"\tau = G \gamma + \frac{\mu}{G} \dot{\gamma}")
    assert a["latex_key"] != b["latex_key"] and a["shape_key"] == b["shape_key"] and a["shape_hash"] == b["shape_hash"]
    assert a["identifiers"][0] == "σ" and a["shape_key"].startswith("v1 = v2 v3")
    c = OD.canonical_formula(r"\sigma = E \varepsilon^{2}")
    assert c["shape_key"] != OD.canonical_formula(r"\sigma = E \varepsilon")["shape_key"]   # powers are structure


@pytest.mark.parametrize("latex,reason", [
    (r"\sigma_{p}", "NO_RELATION"), (r"\mathrm{MgCl}_{2}", "NO_RELATION"), (r"x = 5", "FEW_IDENTIFIERS"),
    (r"i = 1, 2, \ldots, n", "NOTATION"), (r"a = b", "SHORT"), ("", "EMPTY"),
])
def test_trivial_formulas(latex, reason):
    c = OD.canonical_formula(latex)
    assert c["trivial"] and c["trivial_reason"] == reason and not c["distinctive"]


def test_distinctive_formula():
    c = OD.canonical_formula(r"\varepsilon(t) = \frac{1}{E}\left[\sigma(t) + \int_0^t K(t-\tau)\sigma(\tau) d\tau\right]")
    assert not c["trivial"] and c["distinctive"] and c["n_struct"] >= 2
    assert not OD.canonical_formula(r"\sigma = E \varepsilon")["distinctive"]


def test_definitions_and_token_ratio():
    assert OD.definitions_agree({"модул упругост"}, {"модул деформац"}) == "DEFINITIONS_AGREE"
    assert OD.definitions_agree({"плотност", "объем"}, {"скорост", "врем"}) == "DEFINITIONS_DISAGREE"
    assert OD.definitions_agree(set(), {"скорост"}) == "DEFINITIONS_UNKNOWN"
    assert OD.token_ratio(["a", "=", "b"], ["a", "=", "b"]) == 1.0 > OD.token_ratio(["a", "=", "b"], ["a", "=", "c"])


# ------------------------------------------------------------------------------------------------ labels, numbers
def test_labels_and_caption_bodies():
    assert OD.label_number("Рис. 3.12.") == "3.12" and OD.label_number("Table 10.5") == "10.5"
    assert OD.label_number("Рисунок 4") == "4" and OD.label_number("Fig. 2a") == "2a" and OD.label_number(None) is None
    assert OD.caption_body("Рис. 3.1. Схема отработки панели") == "Схема отработки панели"
    assert OD.caption_body("Figure 2: Stress path") == "Stress path"
    assert OD.caption_body("Без номера") == "Без номера"


def test_table_numbers_and_windows():
    nums = OD.table_numbers("16,42 | 1,80 | 2 | 1642 | 0.93")
    assert [d for d, _, _ in nums] == ["1642", "180", "2", "1642", "093"]
    assert [inf for _, _, inf in nums] == [True, True, False, True, True]
    grid = OD.table_numbers("0.1 | 0.2 | 0.3 | 0.4 | 0.5")
    assert len(OD._number_keys(grid, 3)) == 0                       # an arithmetic progression is a grid, not data
    small = OD.table_numbers("1 | 2 | 5 | 7 | 9")
    assert len(OD._number_keys(small, 3)) == 0                      # small integers are not informative
    data = OD.table_numbers("24,7 | 18,7 | 21,3 | 14,2")
    assert len(OD._number_keys(data, 3)) == 2


# ------------------------------------------------------------------------------------------------ image hashes
def pattern(seed: int = 1, size: int = 288) -> np.ndarray:
    rng = np.random.default_rng(seed)
    cells = rng.integers(0, 256, size=(size // 16, size // 16)).astype(np.uint8)
    return np.kron(cells, np.ones((16, 16), dtype=np.uint8))


def test_bin_resize_and_hash_invariances():
    img = pattern()
    assert OD.bin_resize(np.ones((5, 7)), 3, 3).shape == (3, 3)
    assert np.allclose(OD.bin_resize(np.full((64, 64), 7, dtype=np.uint8), 32, 32), 7.0)
    h = OD.image_hashes(img)
    framed = np.full((388, 388), 255, dtype=np.uint8)
    framed[50:338, 50:338] = img
    hf = OD.image_hashes(framed)
    assert hf["phash_trim"][0] == h["phash_trim"][0] and (hf["trim_width"], hf["trim_height"]) == (288, 288)
    big = np.kron(img, np.ones((2, 2), dtype=np.uint8))
    assert OD.image_hashes(big)["phash"] == h["phash"]                 # scale
    rot = OD.image_hashes(np.rot90(img))
    assert rot["phash_trim"][0] == h["phash_trim"][1]                  # a quarter turn is one of the D4 hashes
    assert rot["dhash_trim"][0] == h["dhash_trim"][1]
    other = OD.image_hashes(pattern(7))
    assert int(np.bitwise_count(np.uint64(other["phash"]) ^ np.uint64(h["phash"]))) > 12
    blank = OD.image_hashes(np.full((200, 300), 250, dtype=np.uint8))
    assert blank["thumb_std"] == 0.0 and blank["ink_share"] == 0.0


def test_content_box_and_d4():
    g = np.full((200, 300), 255, dtype=np.uint8)
    g[40:120, 100:250] = 0
    assert OD.content_box(g) == (100, 40, 250, 120)
    assert OD.content_box(np.full((50, 50), 255, dtype=np.uint8)) is None
    t = np.arange(9).reshape(3, 3)
    v = OD.d4(t)
    assert len(v) == 8 and (v[5] == np.fliplr(t)).all() and (v[7] == np.flipud(t)).all()


def test_figure_candidates_match_brute_force():
    rng = np.random.default_rng(5)
    n = 60
    ph = rng.integers(0, 2 ** 63, size=n, dtype=np.uint64)
    trim = rng.integers(0, 2 ** 63, size=(n, 8), dtype=np.uint64)
    ph[7], trim[7] = ph[3] ^ np.uint64(0b111), trim[3] ^ np.uint64(0b1)
    trim[20, 2] = trim[11, 0]                                          # a rotation match (20 = a turn of 11)
    src = np.array([i % 5 for i in range(n)])
    got = set(OD.figure_candidates(ph, trim, src, 12, block=7))
    bc = lambda x, y: int(np.bitwise_count(x ^ y))  # noqa: E731
    want = {(i, j) for i in range(n) for j in range(i + 1, n) if src[i] != src[j]
            and min([bc(ph[i], ph[j]), bc(trim[i, 0], trim[j, 0])] + [bc(trim[i, 0], trim[j, g]) for g in range(1, 8)])
            <= 12}
    assert got == want and (3, 7) in got and (11, 20) in got


def test_mutual_best_keeps_only_mutual_pairs():
    src = ["S1", "S1", "S2", "S2"]                                      # panels a, b in S1; a', b' in S2
    edges = {(0, 2): (2, 1), (1, 3): (2, 1), (0, 3): (3, 5), (1, 2): (3, 5)}
    assert OD.mutual_best(edges, src) == [(0, 2), (1, 3)]
    assert OD.mutual_best({(0, 2): (1,), (0, 3): (1,)}, ["S1", "S9", "S2", "S2"]) == [(0, 2), (0, 3)]   # ties kept


def test_shared_counts_match_brute_force():
    rng = np.random.default_rng(2)
    keys = [np.unique(rng.integers(0, 40, size=15).astype(np.uint64)) for _ in range(12)]
    src = np.array([i % 4 for i in range(12)])
    got = OD._shared_counts(keys, src, max_df=100)
    want = {(i, j): len(set(keys[i].tolist()) & set(keys[j].tolist())) for i, j in combinations(range(12), 2)
            if src[i] != src[j]}
    assert got == {k: v for k, v in want.items() if v}


# ------------------------------------------------------------------------------------------------ synthetic canon
SOURCES = {  # source → (work, work type, year, link type, pages)
    "SYN-A": ("WRK-A", "MONOGRAPH", 1990, "FULL_COPY", 6),
    "SYN-A2": ("WRK-A", "MONOGRAPH", 1990, "FULL_COPY", 6),
    "SYN-B": ("WRK-B", "TEXTBOOK", 2005, "FULL_COPY", 8),
    "SYN-C": ("WRK-C", "MONOGRAPH", 2010, "FULL_COPY", 6),
    "SYN-D": ("WRK-D", "JOURNAL_ARTICLE", 2015, "FULL_COPY", 4),
    "SYN-E": ("WRK-E", "JOURNAL_ARTICLE", 2016, "FULL_COPY", 4),
    "SYN-F": ("WRK-F", "JOURNAL_ARTICLE", 2017, "FULL_COPY", 4),
    "SYN-G": ("WRK-G", "MONOGRAPH", None, "FULL_COPY", 3),
}
RNG = np.random.default_rng(20260929)


def rand64() -> int:
    return int(RNG.integers(0, 2 ** 63, dtype=np.uint64))


def flip(v: int, n: int, offset: int = 0) -> int:
    for b in range(offset, offset + n):
        v ^= 1 << b
    return v


def hrow(phash: int, trim: list[int] | None = None, dh: list[int] | None = None, *, w: int = 600, h: int = 400,
         tw: int | None = None, th: int | None = None, std: float = 40.0) -> dict:
    trim = trim if trim is not None else [phash] + [rand64() for _ in range(7)]
    dh = dh if dh is not None else [rand64() for _ in range(8)]
    return {"phash": phash, "phash_trim": trim, "dhash_trim": dh, "width": w, "height": h, "trim_width": tw or w,
            "trim_height": th or h, "thumb_std": std, "ink_share": 0.2}


def similar(base: dict, n: int, offset: int = 0) -> dict:
    """The same image with ``n`` bits of every hash changed (a re-render)."""
    return {**base, "phash": flip(base["phash"], n, offset),
            "phash_trim": [flip(x, n, offset) for x in base["phash_trim"]],
            "dhash_trim": [flip(x, n, offset) for x in base["dhash_trim"]]}


CAP_PANEL = "Рис. 3.3. Графики деформирования образцов каменной соли при одноосном сжатии"
CAP_REDRAWN = "Кривые ползучести образцов каменной соли при разных нагрузках и температурах"


def figure_rows() -> list[tuple[str, str, int, str | None, dict, str]]:
    """(object id, source, page index, caption, hash row, artifact id)."""
    h1 = hrow(rand64(), dh=[rand64() for _ in range(8)])
    h2 = hrow(rand64())
    h3 = hrow(rand64())
    h8a = hrow(rand64())
    h8b = similar(h8a, 4, offset=20)
    logo = hrow(rand64(), w=180, h=120)
    red = hrow(rand64())
    red_b = {**hrow(flip(red["phash"], 20)), "phash_trim": [flip(red["phash_trim"][0], 20)] + [rand64() for _ in range(7)]}
    rot_b = hrow(rand64(), w=1200, h=300, tw=1200, th=300)
    rot_c = hrow(rand64(), w=300, h=1200, tw=300, th=1200)
    rot_c["phash_trim"][0], rot_c["dhash_trim"][0] = rot_b["phash_trim"][1], rot_b["dhash_trim"][1]
    rot_c["phash_trim"][3], rot_c["dhash_trim"][3] = rot_b["phash_trim"][0], rot_b["dhash_trim"][0]
    low = hrow(rand64(), std=1.0)
    return [
        ("SYN-A:p0002:f000000000001", "SYN-A", 2, "Рис. 1.1. Разрез", h1, "A1"),
        ("SYN-A2:p0002:f000000000001", "SYN-A2", 2, "Рис. 1.1. Разрез", similar(h1, 2), "A21"),
        ("SYN-A:p0003:f000000000002", "SYN-A", 3, "Рис. 1.2. Схема отработки панели с закладкой камер", h2, "A2x"),
        ("SYN-B:p0005:f000000000002", "SYN-B", 5, "Рис. 3.4. Схема отработки панели с закладкой камер", h2, "B2x"),
        ("SYN-A:p0004:f000000000003", "SYN-A", 4, None, h3, "A3"),
        ("SYN-C:p0002:f000000000003", "SYN-C", 2, None, similar(h3, 4), "C3"),
        ("SYN-A:p0005:f000000000004", "SYN-A", 5, "Рис. 2.1. " + CAP_REDRAWN, red, "A4"),
        ("SYN-B:p0006:f000000000004", "SYN-B", 6, "Рис. 5.1. " + CAP_REDRAWN, red_b, "B4"),
        ("SYN-A:p0006:f00000000008a", "SYN-A", 6, CAP_PANEL, h8a, "A8a"),
        ("SYN-A:p0006:f00000000008b", "SYN-A", 6, CAP_PANEL, h8b, "A8b"),
        ("SYN-A2:p0006:f00000000008a", "SYN-A2", 6, CAP_PANEL, similar(h8a, 1, offset=40), "A28a"),
        ("SYN-A2:p0006:f00000000008b", "SYN-A2", 6, CAP_PANEL, similar(h8b, 1, offset=40), "A28b"),
        ("SYN-D:p0001:f000000000009", "SYN-D", 1, None, logo, "LOGO"),
        ("SYN-E:p0001:f000000000009", "SYN-E", 1, None, logo, "LOGO"),
        ("SYN-F:p0001:f000000000009", "SYN-F", 1, None, logo, "LOGO"),
        ("SYN-B:p0007:f000000000007", "SYN-B", 7, None, rot_b, "B7"),
        ("SYN-C:p0004:f000000000007", "SYN-C", 4, None, rot_c, "C7"),
        ("SYN-C:p0003:f000000000006", "SYN-C", 3, None, low, "C6"),
        ("SYN-G:p0001:f000000000006", "SYN-G", 1, None, similar(low, 2), "G6"),
        ("SYN-G:p0002:f000000000005", "SYN-G", 2, "Рис. 9. Карта", hrow(rand64()), "G5"),
    ]


FIGURES = figure_rows()          # drawn once: every use sees the same hashes


def cells(rows: list[list[str]]) -> list[dict]:
    return [{"row": r, "col": c, "row_span": 1, "col_span": 1, "is_header": r == 0, "text": t}
            for r, row in enumerate(rows) for c, t in enumerate(row)]


T_ROCK = [["Порода", "Предел прочности на сжатие, МПа", "Модуль упругости, ГПа"],
          ["Каменная соль", "24,7", "18,7"], ["Сильвинит пестрый", "21,3", "14,2"], ["Карналлит", "12,8", "6,5"]]
T_ROCK_B = [["Порода", "Предел прочности на сжатие, МПа", "Модуль упругости, ГПа"],
            ["Каменная соль", "24.7", "18.7"], ["Сильвинит пестрый", "21.3", "14.2"], ["Карналит", "12.8", "6.5"]]
T_CREEP = [["Нагрузка, МПа", "Скорость ползучести, 1/сут", "Время, сут"],
           ["12,5", "0,0031", "118"], ["15,5", "0,0077", "96"], ["18,5", "0,0192", "74"], ["21,5", "0,0415", "53"]]
T_CREEP_EN = [["Load, MPa", "Creep rate, 1/day", "Time, days"],
              ["12.5", "0.0031", "118"], ["15.5", "0.0077", "96"], ["18.5", "0.0192", "74"], ["21.5", "0.0415", "53"]]
T_SPEC_1 = [["Параметр", "Значение"], ["Производительность, т/ч", "420"], ["Ширина ленты, мм", "800"],
            ["Скорость движения ленты, м/с", "2,0"], ["Мощность привода, кВт", "110"]]
T_SPEC_2 = [["Параметр", "Значение"], ["Производительность, т/ч", "650"], ["Ширина ленты, мм", "1000"],
            ["Скорость движения ленты, м/с", "2,5"], ["Мощность привода, кВт", "250"]]
T_HEADER = [["Наименование показателя", "Значение показателя", "Примечание к показателю"]]


def table_rows() -> list[tuple[str, str, int, list]]:
    return [("SYN-A:p0002:t000000000001", "SYN-A", 2, T_ROCK), ("SYN-B:p0005:t000000000001", "SYN-B", 5, T_ROCK_B),
            ("SYN-A:p0004:t000000000002", "SYN-A", 4, T_CREEP), ("SYN-C:p0003:t000000000002", "SYN-C", 3, T_CREEP_EN),
            ("SYN-A:p0006:t000000000003", "SYN-A", 6, T_SPEC_1), ("SYN-C:p0005:t000000000003", "SYN-C", 5, T_SPEC_2),
            ("SYN-D:p0002:t000000000009", "SYN-D", 2, T_HEADER), ("SYN-E:p0002:t000000000009", "SYN-E", 2, T_HEADER),
            ("SYN-F:p0002:t000000000009", "SYN-F", 2, T_HEADER)]


F_MAXWELL = r"\sigma = E \cdot \varepsilon + \frac{\eta}{E} \dot{\varepsilon}"
F_MAXWELL_C = r"\sigma = E \varepsilon + \frac {\eta}{E} \dot {\varepsilon},"
F_HERED_B = r"\varepsilon(t) = \frac{\sigma_0}{E} \left(1 + \int_0^t K(t-\tau) d\tau\right)"
F_HERED_C = r"e(t) = \frac{s_0}{E} (1 + \int_0^t R(t - \tau) d \tau)"


def formula_rows() -> list[tuple[str, str, int, str, str | None]]:
    """(object id, source, page index, LaTeX, printed number)."""
    return [
        ("SYN-A:p0002:m000000000001", "SYN-A", 2, F_MAXWELL, None),
        ("SYN-C:p0003:m000000000001", "SYN-C", 3, F_MAXWELL_C, None),
        ("SYN-B:p0003:m000000000002", "SYN-B", 3, F_HERED_B, None),
        ("SYN-C:p0004:m000000000002", "SYN-C", 4, F_HERED_C, None),
        ("SYN-D:p0003:m000000000003", "SYN-D", 3, r"u = a + b x + c x^{2} + e x^{3}", None),
        ("SYN-E:p0003:m000000000003", "SYN-E", 3, r"w = p + q y + r y^{2} + s y^{3}", None),
        ("SYN-A:p0003:m000000000004", "SYN-A", 3, r"\sigma_{p}", None),
        ("SYN-C:p0002:m000000000004", "SYN-C", 2, r"\sigma_{p}", None),
        ("SYN-A:p0005:m000000000005", "SYN-A", 5, r"\tau = \tau_{0} + \sigma \mathrm{tg} \varphi", "3.2"),
        ("SYN-B:p0006:m000000000005", "SYN-B", 6, r"\tau = \tau_{0} + \sigma \mathrm{tg} \varphi_{1}", "3.2"),
        ("SYN-A:p0004:m000000000006", "SYN-A", 4, r"K = \frac{\sigma_{1} - \sigma_{3}}{2 c}", None),
        ("SYN-A2:p0004:m000000000006", "SYN-A2", 4, r"K = \frac{\sigma_{1} - \sigma_{3}}{2 c}", None),
    ]


def make_canon(con) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS canonical")
    con.execute("CREATE TABLE canonical.pages (page_id VARCHAR, source_id VARCHAR, page_index INTEGER)")
    con.execute("CREATE TABLE canonical.works (work_id VARCHAR, publication_year SMALLINT, work_type VARCHAR, "
                "anchor_source_id VARCHAR)")
    con.execute("CREATE TABLE canonical.source_work_links (source_id VARCHAR, work_id VARCHAR, link_type VARCHAR, "
                "is_primary BOOLEAN, curation_status VARCHAR)")
    con.execute("CREATE TABLE canonical.figures (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR, "
                "figure_label VARCHAR, caption VARCHAR, caption_normalized VARCHAR, layout_class VARCHAR, "
                "image_artifact_id VARCHAR, embedded_image_artifact_id VARCHAR)")
    con.execute("CREATE TABLE canonical.artifacts (artifact_id VARCHAR, artifact_kind VARCHAR, storage_relpath VARCHAR, "
                "media_type VARCHAR, image_width_px INTEGER, image_height_px INTEGER)")
    con.execute('CREATE TABLE canonical.tables (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR, table_label '
                'VARCHAR, caption VARCHAR, caption_normalized VARCHAR, cells STRUCT("row" INTEGER, col INTEGER, '
                'row_span SMALLINT, col_span SMALLINT, is_header BOOLEAN, "text" VARCHAR)[], normalized_text VARCHAR, '
                'header_rows SMALLINT, n_rows INTEGER, n_cols INTEGER)')
    con.execute("CREATE TABLE canonical.formulas (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR, "
                "formula_kind VARCHAR, normalized_latex VARCHAR, raw_output VARCHAR, raw_format VARCHAR, "
                "equation_label VARCHAR)")
    works = {}
    for sid, (wid, wtype, year, link, n_pages) in SOURCES.items():
        works.setdefault(wid, (year, wtype, sid))
        con.execute("INSERT INTO canonical.source_work_links VALUES (?, ?, ?, true, 'CURATED')", [sid, wid, link])
        for i in range(1, n_pages + 1):
            con.execute("INSERT INTO canonical.pages VALUES (?, ?, ?)", [f"{sid}:p{i:04d}", sid, i])
    for wid, (year, wtype, anchor) in works.items():
        con.execute("INSERT INTO canonical.works VALUES (?, ?, ?, ?)", [wid, year, wtype, anchor])
    seen = set()
    for oid, sid, page, cap, _h, aid in FIGURES:
        art = "sha256:" + hashlib.sha256(aid.encode()).hexdigest()
        con.execute("INSERT INTO canonical.figures VALUES (?, ?, ?, ?, ?, ?, 'RASTER_IMAGE', ?, NULL)",
                    [oid, sid, f"{sid}:p{page:04d}", OD.label_number(cap) and f"Рис. {OD.label_number(cap)}", cap, cap,
                     art])
        if art not in seen:
            seen.add(art)
            rel = "figures/" + art[7:9] + "/" + art[9:11] + "/" + art[7:] + ".png"
            con.execute("INSERT INTO canonical.artifacts VALUES (?, 'FIGURE_CROP', ?, 'image/png', 600, 400)",
                        [art, rel])
    for oid, sid, page, rows in table_rows():
        con.execute("INSERT INTO canonical.tables VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, ?, ?)",
                    [oid, sid, f"{sid}:p{page:04d}", cells(rows), len(rows), len(rows[0])])
    for oid, sid, page, latex, _num in formula_rows():
        con.execute("INSERT INTO canonical.formulas VALUES (?, ?, ?, 'DISPLAY', ?, ?, 'LATEX', NULL)",
                    [oid, sid, f"{sid}:p{page:04d}", latex, latex])


def hash_cache() -> pa.Table:
    rows = {f.name: [] for f in OD.FIG_HASH_SCHEMA}
    for oid, sid, _page, _cap, h, aid in FIGURES:
        art = "sha256:" + hashlib.sha256(aid.encode()).hexdigest()
        for k, v in {"object_id": oid, "source_id": sid, "artifact_id": art, "artifact_kind": "FIGURE_CROP",
                     "status": "OK", "error": None, "low_information": h["thumb_std"] < 6, "hash_rule": OD.HASH_RULE,
                     "rule_version": OD.RULE_VERSION, **h}.items():
            rows[k].append(v)
    return pa.table(rows, schema=OD.FIG_HASH_SCHEMA)


def context_inputs() -> dict:
    so = pa.table({"source_a": ["SYN-A"], "source_b": ["SYN-B"], "relation": ["REPRINT"], "n_shared_content": [5],
                   "same_work": [False]})
    dm = pa.table({"cluster_id": ["DCL-x", "DCL-x", "DCL-y", "DCL-y"], "source_id": ["SYN-A", "SYN-B", "SYN-A", "SYN-A2"],
                   "page_index": [5, 6, 4, 4]})
    fc = pa.table({"formula_id": [r[0] for r in formula_rows()], "equation_number": [r[4] for r in formula_rows()],
                   "section_id": [None] * len(formula_rows())})
    fs = pa.table({"formula_id": ["SYN-B:p0003:m000000000002", "SYN-C:p0004:m000000000002"],
                   "definition": ["деформация", "деформация"], "definition_key": ["деформац", "деформац"]})
    return {"source_overlap": so, "dup_members": dm, "formula_context": fc, "formula_symbols": fs}


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    make_canon(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def built(con):
    stats: dict = {}
    res = OD.build(con, figure_hashes=hash_cache(), stats=stats, **context_inputs())
    return res, stats


def clusters_by(res, object_type: str) -> dict[tuple, dict]:
    mem = res["object_dup_members"].to_pylist()
    out = {}
    for c in res["object_dup_clusters"].to_pylist():
        if c["object_type"] == object_type:
            ids = tuple(sorted(m["object_id"] for m in mem if m["cluster_id"] == c["cluster_id"]))
            out[ids] = c
    return out


# ------------------------------------------------------------------------------------------------ builder
def test_schemas_ids_and_stats(built):
    res, stats = built
    assert res["object_dup_clusters"].schema == OD.CLUSTERS_SCHEMA
    assert res["object_dup_members"].schema == OD.MEMBERS_SCHEMA
    assert res["formula_keys"].schema == OD.FORMULA_KEYS_SCHEMA and res["figure_hashes"].schema == OD.FIG_HASH_SCHEMA
    mem = res["object_dup_members"].to_pylist()
    for c in res["object_dup_clusters"].to_pylist():
        ids = [m["object_id"] for m in mem if m["cluster_id"] == c["cluster_id"]]
        assert c["cluster_id"] == nav_ids.object_dup_cluster_id(c["object_type"], ids) and c["n_members"] == len(ids)
        assert c["kind"] in OD.KINDS and c["primary_rule"] in OD.PRIMARY_RULES
        assert c["rule_version"] == nav_ids.RULE_VERSIONS["object_duplicates"] == "object_duplicates_v1"
    assert all(m["match"] in OD.MATCHES for m in mem)
    assert stats["layer_status"] == "DERIVED" and stats["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    assert stats["object_types"]["FIGURE"]["hashing"]["cached"] == len(FIGURES)
    assert stats["publication_groups"]["text_relations"] == "USED"
    assert set(OD.DEFAULTS) - {"workers", "object_types", "hash_cache", "max_image_pixels"} <= set(stats["model_choices"])
    assert res["formula_keys"].num_rows == len(formula_rows())


def test_figure_kinds_primaries_and_assignment(built):
    res, stats = built
    cl = clusters_by(res, "FIGURE")
    same = cl[("SYN-A2:p0002:f000000000001", "SYN-A:p0002:f000000000001")]
    assert same["kind"] == "SAME_WORK_COPY" and same["primary_source_id"] == "SYN-A"
    assert same["primary_rule"] == "SAME_WORK_ANCHOR"
    rep = cl[("SYN-A:p0003:f000000000002", "SYN-B:p0005:f000000000002")]
    assert rep["kind"] == "REPRINT" and rep["primary_source_id"] == "SYN-A" and rep["primary_rule"] == "EARLIEST_YEAR"
    reused = cl[("SYN-A:p0004:f000000000003", "SYN-C:p0002:f000000000003")]
    assert reused["kind"] == "REUSED_FIGURE" and reused["match_basis"] == "IMAGE" and reused["primary_year"] == 1990
    red = cl[("SYN-A:p0005:f000000000004", "SYN-B:p0006:f000000000004")]
    assert red["kind"] == "REDRAWN" and red["match_basis"] == "CAPTION"
    logo = cl[("SYN-D:p0001:f000000000009", "SYN-E:p0001:f000000000009", "SYN-F:p0001:f000000000009")]
    assert logo["kind"] == "BOILERPLATE" and logo["primary_source_id"] is None and logo["primary_rule"] == "NOT_APPLICABLE"
    rot = cl[("SYN-B:p0007:f000000000007", "SYN-C:p0004:f000000000007")]
    assert rot["kind"] == "REUSED_FIGURE"
    # the panels a/b of one figure pair up with their own copies, never across
    assert ("SYN-A2:p0006:f00000000008a", "SYN-A:p0006:f00000000008a") in cl
    assert ("SYN-A2:p0006:f00000000008b", "SYN-A:p0006:f00000000008b") in cl
    # low-information images and unrelated images stay out
    flat = {o for ids in cl for o in ids}
    assert not flat & {"SYN-C:p0003:f000000000006", "SYN-G:p0001:f000000000006", "SYN-G:p0002:f000000000005"}
    mem = {m["object_id"]: m for m in res["object_dup_members"].to_pylist()}
    assert mem["SYN-C:p0004:f000000000007"]["transform"] in ("ROT90", "ROT270")
    assert mem["SYN-B:p0006:f000000000004"]["context"] == "ALIGNED_TEXT"
    assert mem["SYN-A:p0003:f000000000002"]["is_primary"] and not mem["SYN-B:p0005:f000000000002"]["is_primary"]
    assert stats["object_types"]["FIGURE"]["counts"]["verified_by_tier"]["CAPTION/ALIGNED_TEXT"] >= 1


def test_table_channels_and_kinds(built):
    res, _ = built
    cl = clusters_by(res, "TABLE")
    rock = cl[("SYN-A:p0002:t000000000001", "SYN-B:p0005:t000000000001")]
    assert rock["kind"] == "REPRINT" and rock["match_basis"] == "CELLS"
    creep = cl[("SYN-A:p0004:t000000000002", "SYN-C:p0003:t000000000002")]
    assert creep["kind"] == "REUSED_TABLE" and creep["match_basis"] == "NUMBERS"       # translated: numbers only
    header = cl[("SYN-D:p0002:t000000000009", "SYN-E:p0002:t000000000009", "SYN-F:p0002:t000000000009")]
    assert header["kind"] == "BOILERPLATE"
    flat = {o for ids in cl for o in ids}
    assert "SYN-C:p0005:t000000000003" not in flat                     # the same labels with other values
    mem = {m["object_id"]: m for m in res["object_dup_members"].to_pylist()}
    assert mem["SYN-C:p0003:t000000000002"]["number_containment"] == 1.0


def test_formula_groups_kinds_and_keys(built):
    res, stats = built
    cl = clusters_by(res, "FORMULA")
    maxwell = cl[("SYN-A:p0002:m000000000001", "SYN-C:p0003:m000000000001")]
    assert maxwell["kind"] == "SHARED_FORMULA" and maxwell["match_basis"] == "LATEX"
    hered = cl[("SYN-B:p0003:m000000000002", "SYN-C:p0004:m000000000002")]
    assert hered["kind"] == "SHARED_FORMULA" and hered["match_basis"] == "LATEX_RENAMED"
    near = cl[("SYN-A:p0005:m000000000005", "SYN-B:p0006:m000000000005")]
    assert near["kind"] == "REPRINT" and near["match_basis"] == "LATEX_NEAR"
    same = cl[("SYN-A2:p0004:m000000000006", "SYN-A:p0004:m000000000006")]
    assert same["kind"] == "SAME_WORK_COPY"
    flat = {o for ids in cl for o in ids}
    assert not flat & {"SYN-D:p0003:m000000000003", "SYN-E:p0003:m000000000003"}     # a generic polynomial form
    assert not flat & {"SYN-A:p0003:m000000000004", "SYN-C:p0002:m000000000004"}     # trivial
    counts = stats["object_types"]["FORMULA"]["counts"]
    assert counts["renamed_links_dropped_unanchored"] >= 1 and counts["near_pairs"] == 1
    keys = {k["formula_id"]: k for k in res["formula_keys"].to_pylist()}
    assert keys["SYN-A:p0003:m000000000004"]["trivial"] and keys["SYN-A:p0003:m000000000004"]["trivial_reason"]
    assert keys["SYN-A:p0005:m000000000005"]["equation_number"] == "3.2"
    mem = {m["object_id"]: m for m in res["object_dup_members"].to_pylist()}
    renamed = [m for m in mem.values() if m["match"] == "LATEX_RENAMED"]
    assert renamed and renamed[0]["context"] == "DEFINITIONS_AGREE"


def test_context_channels_are_optional(con):
    stats: dict = {}
    res = OD.build(con, figure_hashes=hash_cache(), stats=stats)
    kinds = {(c["object_type"], c["kind"]) for c in res["object_dup_clusters"].to_pylist()}
    assert ("FIGURE", "REDRAWN") not in kinds and ("FORMULA", "REPRINT") not in kinds
    assert stats["publication_groups"]["text_relations"] == "UNAVAILABLE"
    assert stats["publication_groups"]["aligned_page_pairs"] == "UNAVAILABLE"
    only = OD.build(con, figure_hashes=hash_cache(), object_types=("TABLE",))
    assert {c["object_type"] for c in only["object_dup_clusters"].to_pylist()} == {"TABLE"}
    assert only["figure_hashes"].num_rows == 0 and only["formula_keys"].num_rows == 0


def test_without_images_or_cache_figures_are_recorded_not_hashed(con, monkeypatch):
    monkeypatch.delenv("VKM_DATA_ROOT", raising=False)
    stats: dict = {}
    res = OD.build(con, object_types=("FIGURE",), stats=stats)
    status = {r["status"] for r in res["figure_hashes"].to_pylist()}
    assert status == {"NO_ARTIFACT_ROOT"} and res["object_dup_clusters"].num_rows == 0


def test_deterministic_rebuild(con, built):
    again = OD.build(con, figure_hashes=hash_cache(), **context_inputs())
    for name in ("object_dup_clusters", "object_dup_members", "formula_keys", "figure_hashes"):
        assert again[name].equals(built[0][name])


# ------------------------------------------------------------------------------------------------ queries
@pytest.fixture()
def qcon(built):
    c = duckdb.connect()
    for name in Q.DATASETS:
        c.register(f"nav_{name}", built[0][name])
    yield c
    c.close()


def test_copies_of_object(qcon):
    out = Q.copies_of_object(qcon, "SYN-C:p0002:f000000000003")
    assert out["object_type"] == "FIGURE" and len(out["clusters"]) == 1
    c = out["clusters"][0]
    assert c["kind"] == "REUSED_FIGURE" and not c["this_is_primary"]
    assert [m["source_id"] for m in c["copies"]] == ["SYN-A"] and c["copies"][0]["is_primary"]
    assert c["this"][0]["image_distance"] == 4
    t = Q.copies_of_object(qcon, "SYN-A:p0004:t000000000002")
    assert t["clusters"][0]["kind"] == "REUSED_TABLE" and t["clusters"][0]["this_is_primary"]
    none = Q.copies_of_object(qcon, "SYN-G:p0002:f000000000005")
    assert none["clusters"] == [] and none["object_type"] is None and "AUTO_EXTRACTED_UNREVIEWED" in none["note"]


def test_shared_formulas_by_id_and_latex(qcon):
    by_id = Q.shared_formulas(qcon, "SYN-A:p0002:m000000000001")
    assert by_id["match"] == "formula_id" and by_id["n_exact"] == 2 and by_id["n_works"] == 2
    assert [w["work_id"] for w in by_id["exact"]] == ["WRK-A", "WRK-C"]            # earliest year first
    assert by_id["clusters"][0]["kind"] == "SHARED_FORMULA"
    assert any(f["this"] for f in by_id["exact"][0]["formulas"])
    by_latex = Q.shared_formulas(qcon, r"\sigma=E\varepsilon+\frac{\eta}{E}\dot{\varepsilon}")
    assert by_latex["match"] == "latex" and by_latex["n_exact"] == 2
    renamed = Q.shared_formulas(qcon, r"\theta(t) = \frac{p_0}{E}\left(1 + \int_0^t Q(t-\tau)d\tau\right)")
    assert renamed["distinctive"] and renamed["n_exact"] == 0 and renamed["n_renamed"] == 2
    assert Q.shared_formulas(qcon, r"\theta(t) = \frac{p_0}{E}\left(1 + \int_0^t Q(t-\tau)d\tau\right)",
                             renamed=False)["n_renamed"] == 0
    triv = Q.shared_formulas(qcon, r"\sigma_{p}")
    assert triv["trivial"] and triv["n_exact"] == 2 and triv["clusters"] == []


def test_query_functions_registered_and_attach(tmp_path, built):
    assert store.resolve("copies_of_object") is Q.copies_of_object
    assert store.resolve("shared_formulas") is Q.shared_formulas
    for name in Q.DATASETS + ("figure_hashes",):
        pq.write_table(built[0][name], tmp_path / f"{name}.parquet")
    c = duckdb.connect()
    try:
        Q.attach(c, tmp_path)
        assert Q.copies_of_object(c, "SYN-A:p0003:f000000000002")["clusters"][0]["kind"] == "REPRINT"
        assert c.execute("SELECT count(*) FROM nav_figure_hashes").fetchone()[0] == len(FIGURES)
    finally:
        c.close()


# ------------------------------------------------------------------------------------------------ CLI, ids
def test_ids_parts_and_datasets_registration():
    assert nav_ids.object_dup_cluster_id("FIGURE", ["b", "a"]) == nav_ids.object_dup_cluster_id("FIGURE", ["a", "b", "a"])
    assert nav_ids.object_dup_cluster_id("FIGURE", ["a"]) != nav_ids.object_dup_cluster_id("TABLE", ["a"])
    assert nav_ids.object_dup_cluster_id("TABLE", ["a"]).startswith("OCL-")
    assert {"object_dup_clusters", "object_dup_members", "formula_keys", "figure_hashes"} <= set(nav_ids.DATASETS)
    order = list(nav_cli.PARTS)
    assert order.index("duplicates") < order.index("object_duplicates") < order.index("concepts")
    assert order[-1] == "topics" and order.index("tables") == order.index("formulas") + 1      # parameters read
    assert order.index("parameters") == order.index("tables") + 1                             # the table grids
    assert "figure_hashes" not in nav_cli.datasets_of("object_duplicates")          # an earlier build's cache
    assert nav_cli.parse_part_options(["object_duplicates.workers=4"]) == {"object_duplicates": {"workers": 4}}


def test_cli_build_with_inputs_and_artifacts(tmp_path):
    db = tmp_path / "canon.duckdb"
    c = duckdb.connect(str(db))
    make_canon(c)
    c.close()
    inputs = tmp_path / "earlier"
    inputs.mkdir()
    for name, table in {**context_inputs(), "figure_hashes": hash_cache()}.items():
        pq.write_table(table, inputs / f"{name}.parquet")
    arts = tmp_path / "artifacts"
    arts.mkdir()
    out = tmp_path / "nav"
    from vkm_corpus import cli

    assert cli.main(["nav", "build", "--duckdb", str(db), "--out", str(out), "--part", "object_duplicates",
                     "--inputs", str(inputs), "--artifacts", str(arts), "--option", "object_duplicates.workers=1"]) == 0
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    part = m["parts"]["object_duplicates"]
    assert part["status"] == "BUILT" and part["rule_version"] == "object_duplicates_v1"
    assert m["artifacts"] == {"dir": "artifacts"} and part["options"] == {"workers": 1}
    assert part["stats"]["object_types"]["FIGURE"]["hashing"]["cached"] == len(FIGURES)
    for name in ("object_dup_clusters", "object_dup_members", "formula_keys", "figure_hashes"):
        meta = m["datasets"][name]
        assert pq.read_table(out / meta["path"]).num_rows == meta["rows"] > 0 and len(meta["sha256"]) == 64
    with pytest.raises(SystemExit):
        cli.main(["nav", "build", "--duckdb", str(db), "--out", str(out), "--part", "object_duplicates",
                  "--artifacts", str(tmp_path / "missing")])


# ------------------------------------------------------------------------------------------------ Pillow decoding
def test_decoding_hashing_and_statuses_with_pillow(tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    root = tmp_path / "artifacts"
    img = pattern(3)
    framed = np.full((388, 388), 255, dtype=np.uint8)
    framed[50:338, 50:338] = img
    files = {"one": img, "two": framed}
    c = duckdb.connect()
    try:
        make_canon(c)
        c.execute("DELETE FROM canonical.figures")
        c.execute("DELETE FROM canonical.artifacts")
        for i, (name, arr) in enumerate(files.items()):
            art = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
            rel = f"figures/{art[7:9]}/{art[9:11]}/{art[7:]}.png"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.stack([arr] * 3, axis=-1)).convert("RGBA").save(root / rel)
            src = ("SYN-A", "SYN-C")[i]
            c.execute("INSERT INTO canonical.figures VALUES (?, ?, ?, NULL, NULL, NULL, 'RASTER_IMAGE', ?, NULL)",
                      [f"{src}:p0002:f00000000000{i}", src, f"{src}:p0002", art])
            c.execute("INSERT INTO canonical.artifacts VALUES (?, 'FIGURE_CROP', ?, 'image/png', 0, 0)", [art, rel])
        broken = "sha256:" + "b" * 64
        (root / "figures/bb/bb").mkdir(parents=True, exist_ok=True)
        (root / f"figures/bb/bb/{'b' * 64}.png").write_bytes(b"not a png")
        for oid, art, rel in (("SYN-B:p0002:f000000000007", broken, f"figures/bb/bb/{'b' * 64}.png"),
                              ("SYN-B:p0003:f000000000008", "sha256:" + "c" * 64, f"figures/cc/cc/{'c' * 64}.png"),
                              ("SYN-B:p0004:f000000000009", "sha256:" + "d" * 64, "../outside.png")):
            c.execute("INSERT INTO canonical.figures VALUES (?, 'SYN-B', ?, NULL, NULL, NULL, 'RASTER_IMAGE', ?, NULL)",
                      [oid, oid.rsplit(":", 1)[0], art])
            c.execute("INSERT INTO canonical.artifacts VALUES (?, 'FIGURE_CROP', ?, 'image/png', 0, 0)", [art, rel])
        monkeypatch.setattr(OD, "_POOL_MIN_TASKS", 1)
        res = OD.build(c, artifacts=root, object_types=("FIGURE",), workers=2)
    finally:
        c.close()
    status = {r["object_id"]: r["status"] for r in res["figure_hashes"].to_pylist()}
    assert status["SYN-A:p0002:f000000000000"] == status["SYN-C:p0002:f000000000001"] == "OK"
    assert status["SYN-B:p0002:f000000000007"] == "DECODE_ERROR"
    assert status["SYN-B:p0003:f000000000008"] == "MISSING_FILE"
    assert status["SYN-B:p0004:f000000000009"] == "NO_STORED_FILE"
    hashes = {r["object_id"]: r for r in res["figure_hashes"].to_pylist()}
    assert hashes["SYN-A:p0002:f000000000000"]["phash_trim"][0] == OD.image_hashes(img)["phash_trim"][0]
    cl = res["object_dup_clusters"].to_pylist()
    assert len(cl) == 1 and cl[0]["kind"] == "REUSED_FIGURE"            # the framed copy matches through its content box
