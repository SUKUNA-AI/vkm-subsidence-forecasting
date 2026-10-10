"""NAV part ``figure_series`` (agent FD2) on synthetic PDFs and a synthetic canon: the candidate rule chartlike_v1,
route A with figure statuses, a rotated page, the source checks, determinism of ``nav build``, the import into a NAV
directory (byte for byte) and the query functions. All data are invented; no corpus text."""
from __future__ import annotations

import hashlib
import json

import pytest

pymupdf = pytest.importorskip("pymupdf")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")
np = pytest.importorskip("numpy")

from vkm_corpus import cli  # noqa: E402
from vkm_corpus.navigation import cli as nav_cli  # noqa: E402
from vkm_corpus.navigation import figure_series as FS  # noqa: E402
from vkm_corpus.navigation import figure_series_query as Q  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402

SNAP = "snap-20260929T000000Z-fd2syn00"
X0, X1, Y0, Y1 = 100.0, 400.0, 300.0, 100.0          # plot frame (displayed page, pt): x 0…30, y 0 (Y0) … -60 (Y1)
BOX = (60.0, 80.0, 440.0, 330.0)                     # canonical figure box of every chart
RED = [(0, 0), (5, -4), (10, -12), (15, -25), (20, -40), (25, -44), (30, -45)]
BLUE = [(0, 0), (5, -2), (10, -5), (15, -11), (20, -18), (25, -21), (30, -22)]
HIDDEN = (-6, 1)                  # a vertex left of the plot (a chart's clip would hide it): in_plot_area = false


def _xy(x: float, y: float) -> tuple[float, float]:
    return X0 + (X1 - X0) * x / 30.0, Y0 + (Y1 - Y0) * y / -60.0


def _chart(page, *, rotate: bool = False, x_labels: bool = True, hidden: bool = False) -> None:
    """A chart in displayed coordinates (drawn through the derotation of a /Rotate 90 page): axes, 8 ticks, tick
    labels, axis titles and two open polylines (red, blue) — 22 line items; ``hidden``: the blue line starts at a
    vertex left of the plot area."""
    D = page.derotation_matrix if rotate else pymupdf.Identity
    P = lambda x, y: pymupdf.Point(x, y) * D  # noqa: E731
    sh = page.new_shape()
    sh.draw_line(P(X0, Y0), P(X1, Y0))
    sh.draw_line(P(X0, Y0), P(X0, Y1))
    for i in range(4):
        x, y = X0 + (X1 - X0) * i / 3, Y0 + (Y1 - Y0) * i / 3
        sh.draw_line(P(x, Y0), P(x, Y0 + 4))
        sh.draw_line(P(X0 - 4, y), P(X0, y))
    sh.finish(color=(0, 0, 0), width=0.8, closePath=False)
    sh.commit()
    rot = 90 if rotate else 0
    if x_labels:
        for i, v in enumerate((0, 10, 20, 30)):
            page.insert_text(P(X0 + (X1 - X0) * i / 3 - 5, Y0 + 16), str(v), fontsize=9, rotate=rot)
        # a reference CJK font of PyMuPDF writes Cyrillic with a ToUnicode map (the Base-14 fonts do not)
        page.insert_text(P(200, Y0 + 28), "Время, сут", fontsize=9, rotate=rot, fontname="china-s")
    for i, v in enumerate((0, -20, -40, -60)):
        page.insert_text(P(X0 - 30, Y0 + (Y1 - Y0) * i / 3 + 3), str(v), fontsize=9, rotate=rot)
    page.insert_text(P(X0 - 36, Y1 - 10), "Оседание, мм", fontsize=9, rotate=rot, fontname="china-s")
    for colour, pts in (((1, 0, 0), RED), ((0, 0, 1), [HIDDEN, *BLUE] if hidden else BLUE)):
        sh = page.new_shape()
        p = [_xy(*q) for q in pts]
        for a, b in zip(p[:-1], p[1:]):
            sh.draw_line(P(*a), P(*b))
        sh.finish(color=colour, width=1.2, closePath=False)
        sh.commit()


def _pdf(path, pages: list[dict]) -> bytes:
    doc = pymupdf.open()
    for spec in pages:
        rotate = spec.get("rotate", False)
        page = doc.new_page(width=420 if rotate else 500, height=500 if rotate else 420)
        if rotate:
            page.set_rotation(90)
        if spec.get("chart"):
            _chart(page, rotate=rotate, x_labels=spec.get("x_labels", True), hidden=spec.get("hidden", False))
        if spec.get("few_lines"):            # a small drawing with a few lines only: not chart-like
            sh = page.new_shape()
            for i in range(3):
                sh.draw_line((100, 100 + 20 * i), (300, 100 + 20 * i))
            sh.finish(color=(0, 0, 0), width=0.5, closePath=False)
            sh.commit()
    doc.save(path)
    return path.read_bytes()


VEC = [{"format": "PATHS_JSON", "artifact_id": "sha256:" + "0" * 64}]
NUMS = "0 10 20 30 0 -20 -40 -60"


def make_world(tmp_path, *, tamper: bool = False):
    """Resources root with four sources and a canon DuckDB: 901 (a chart; a chart without x labels; a drawing with
    numbers but few lines; a drawing without numbers), 902 (a chart on a /Rotate 90 page), 903 (sha256 differs from
    the canon), 904 (file missing)."""
    root = tmp_path / "resources"
    (root / "src").mkdir(parents=True)
    sha = {"VKM-SRC-901": hashlib.sha256(_pdf(root / "src" / "a.pdf", [{"chart": True, "hidden": True},
                                                                         {"chart": True, "x_labels": False},
                                                                         {"few_lines": True}])).hexdigest(),
           "VKM-SRC-902": hashlib.sha256(_pdf(root / "src" / "b.pdf", [{"chart": True, "rotate": True}])).hexdigest(),
           "VKM-SRC-903": "f" * 64,
           "VKM-SRC-904": "e" * 64}
    _pdf(root / "src" / "c.pdf", [{"chart": True}])
    db = tmp_path / "canon.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA canonical")
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.snapshot (snapshot_id VARCHAR, manifest_sha256 VARCHAR, pipeline_version VARCHAR)")
    con.execute("INSERT INTO meta.snapshot VALUES (?, 'm', '0.1.0')", [SNAP])
    con.execute("""CREATE TABLE canonical.figures (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR,
        layout_class VARCHAR, figure_label VARCHAR, caption_block_id VARCHAR, caption VARCHAR,
        caption_normalized VARCHAR, bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_space VARCHAR,
        embedded_image_artifact_id VARCHAR, vector_artifacts STRUCT(format VARCHAR, artifact_id VARCHAR)[])""")
    con.execute("CREATE TABLE canonical.pages (page_id VARCHAR, source_id VARCHAR, page_index INTEGER, "
                "rotation_deg SMALLINT, normalized_text VARCHAR)")
    con.execute("CREATE TABLE canonical.blocks (page_id VARCHAR, bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, "
                "bbox_y1 DOUBLE, bbox_space VARCHAR, is_primary_layer BOOLEAN, text VARCHAR)")
    con.execute("CREATE TABLE canonical.sources (source_id VARCHAR, canonical_path VARCHAR, source_sha256 VARCHAR, "
                "site_scope_raw VARCHAR)")
    con.execute("CREATE TABLE ws_t (source_id VARCHAR, work_id VARCHAR, page_start INTEGER, page_end INTEGER, "
                "is_primary BOOLEAN)")
    con.execute("CREATE TABLE wa_t (work_id VARCHAR, publication_year SMALLINT, available_latest_day DATE, "
                "available_basis VARCHAR)")
    con.execute("CREATE VIEW work_sources AS SELECT * FROM ws_t")
    con.execute("CREATE VIEW works_availability AS SELECT * FROM wa_t")
    con.execute("INSERT INTO ws_t VALUES ('VKM-SRC-901', 'VKM-WRK-901', NULL, NULL, true)")
    con.execute("INSERT INTO wa_t VALUES ('VKM-WRK-901', 2015, DATE '2015-12-31', 'ASSUMED_FROM_PUBLICATION')")
    for sid, rel, scope in (("VKM-SRC-901", "src/a.pdf", "ВКМ; синтетика"), ("VKM-SRC-902", "src/b.pdf", None),
                            ("VKM-SRC-903", "src/c.pdf", None), ("VKM-SRC-904", "src/missing.pdf", None)):
        con.execute("INSERT INTO canonical.sources VALUES (?, ?, ?, ?)", [sid, rel, sha[sid], scope])
    figs = [  # id, source, page, rotation, label, caption, numbers near the box, box
        ("VKM-SRC-901:p0001:f000000000001", "VKM-SRC-901", 1, 0, "Рис. 1", "Рис. 1. Оседание поверхности во времени",
         NUMS, BOX),
        ("VKM-SRC-901:p0001:f000000000002", "VKM-SRC-901", 1, 0, None, "Логотип", "", (450, 20, 490, 60)),
        ("VKM-SRC-901:p0002:f000000000003", "VKM-SRC-901", 2, 0, "Рис. 2", "Рис. 2. Прогноз оседаний (расчёт)", NUMS,
         BOX),
        ("VKM-SRC-901:p0003:f000000000004", "VKM-SRC-901", 3, 0, None, "Схема", "1 2 3 4 5 6 7", (90, 90, 310, 150)),
        ("VKM-SRC-902:p0001:f000000000005", "VKM-SRC-902", 1, 90, "Рис. 3", "Рис. 3. Конвергенция", NUMS, BOX),
        ("VKM-SRC-903:p0001:f000000000006", "VKM-SRC-903", 1, 0, "Рис. 4", "Рис. 4", NUMS, BOX),
        ("VKM-SRC-904:p0001:f000000000007", "VKM-SRC-904", 1, 0, "Рис. 5", "Рис. 5", NUMS, BOX),
    ]
    pages = set()
    for fid, sid, pno, rot, label, cap, nums, box in figs:
        pid = f"{sid}:p{pno:04d}"
        if pid not in pages:
            pages.add(pid)
            con.execute("INSERT INTO canonical.pages VALUES (?, ?, ?, ?, ?)", [pid, sid, pno, rot, cap])
        con.execute("INSERT INTO canonical.figures VALUES (?, ?, ?, 'CHART', ?, NULL, ?, ?, ?, ?, ?, ?, 'PAGE_PT_TL', "
                    "NULL, ?)", [fid, sid, pid, label, cap, cap, *box, VEC])
        if nums:
            con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, ?, ?, 'PAGE_PT_TL', true, ?)",
                        [pid, box[0] + 5, box[1] + 5, box[0] + 30, box[1] + 20, nums])
    con.close()
    if tamper:
        (root / "src" / "a.pdf").write_bytes(b"%PDF-1.4 not the canonical file")
    return root, db


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fs")
    root, db = make_world(tmp)
    stats: dict = {}
    con = duckdb.connect(str(db), read_only=True)
    try:
        tables = FS.build(con, resources=str(root), workers=1, ocr="off", stats=stats)
    finally:
        con.close()
    return {"root": root, "db": db, "tables": tables, "stats": stats, "tmp": tmp}


def _rows(world, name):
    return world["tables"][name].to_pylist()


# ------------------------------------------------------------------------------------------------ builder
def test_candidates_statuses_and_counts(world):
    st = world["stats"]
    figs = {f["figure_id"]: f for f in _rows(world, "figure_series_figures")}
    assert st["with_native_vectors"] == 7 and st["numeric_rule_pass"] == 6          # the logo has no numbers
    assert st["not_chartlike_after_numeric_rule"] == 1                              # numbers, but 3 lines only
    status = {fid.rsplit(":", 1)[1]: f["figure_status"] for fid, f in figs.items()}
    assert status == {"f000000000001": "DIGITIZED", "f000000000003": "X_UNCALIBRATED",
                      "f000000000005": "DIGITIZED", "f000000000006": "SOURCE_HASH_MISMATCH",
                      "f000000000007": "SOURCE_UNAVAILABLE"}
    assert st["figure_status"] == {"DIGITIZED": 2, "SOURCE_HASH_MISMATCH": 1, "SOURCE_UNAVAILABLE": 1,
                                   "X_UNCALIBRATED": 1}
    for f in figs.values():
        assert f["status"] == "DERIVATION" and f["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
        assert f["rule_version"] == "figure_series_v1" and f["route"] == "A_NATIVE_VECTOR"
    bad = [f for f in figs.values() if f["figure_status"].startswith("SOURCE_")]
    assert all(f["n_series"] == 0 and "CANDIDATE_RULE_NOT_EVALUATED" in f["flags"] for f in bad)
    assert st["chartlike"] == 3 and st["candidates_rule_not_evaluated"] == 2 and st["partial"] is None
    assert st["source_status"] == {"OK": 2, "SOURCE_HASH_MISMATCH": 1, "SOURCE_UNAVAILABLE": 1}


def test_route_a_values_errors_units_and_availability(world):
    figs = {f["figure_id"]: f for f in _rows(world, "figure_series_figures")}
    f1 = figs["VKM-SRC-901:p0001:f000000000001"]
    assert f1["x_title_raw"] == "Время, сут" and f1["x_unit_raw"] == "сут" and f1["x_is_time"] is True
    assert f1["y_quantity_raw"] == "Оседание" and f1["y_unit_raw"] == "мм"
    assert f1["x_cal_method"] == "SNAPPED_TO_TICKS" and f1["n_series_both_axes"] == 2 and f1["n_time_series"] == 2
    assert f1["available_from"] == "2015-12-31" and f1["available_basis"] == "ASSUMED_FROM_PUBLICATION"
    assert "SCOPE_INHERITED_FROM_SOURCE" in f1["flags"] and f1["source_site_scope_raw"] == "ВКМ; синтетика"
    series = [s for s in _rows(world, "figure_series") if s["figure_id"] == f1["figure_id"]]
    pts = [p for p in _rows(world, "figure_series_points") if p["figure_id"] == f1["figure_id"]]
    got = {s["series_color"]: sorted((p["x"], p["y"]) for p in pts if p["series_id"] == s["series_id"])
           for s in series}
    assert np.allclose(got["#ff0000"], RED, atol=1e-2)
    assert np.allclose(got["#0000ff"], sorted([HIDDEN, *BLUE]), atol=1e-2)       # the hidden vertex is kept …
    for p in pts:
        assert p["x_err"] > 0 and p["y_err"] > 0 and p["x_unit_raw"] == "сут" and p["y_unit_raw"] == "мм"
        assert p["status"] == "DERIVATION" and p["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
        assert p["available_from"] == "2015-12-31" and p["x_cal_method"] == "SNAPPED_TO_TICKS"
        assert p["x_page_pt"] is not None and p["y_cal_rms_pt"] is not None
        assert p["in_plot_area"] is (abs(p["x"] - HIDDEN[0]) > 1e-2)               # … and marked outside the plot
    by_colour = {s["series_color"]: s for s in series}
    assert by_colour["#0000ff"]["n_points_outside_plot"] == 1
    assert "POINTS_OUTSIDE_PLOT_AREA" in by_colour["#0000ff"]["flags"]
    assert by_colour["#ff0000"]["n_points_outside_plot"] == 0 and by_colour["#ff0000"]["flags"] == \
        ["LEGEND_UNMATCHED", "SCOPE_INHERITED_FROM_SOURCE"]
    assert f1["n_points_outside_plot"] == 1 and "POINTS_OUTSIDE_PLOT_AREA" in f1["flags"]
    for s in series:
        assert s["series_id"].startswith("FS-") and s["axes_calibrated"] == "BOTH" and s["series_nature"] == \
            "UNCLASSIFIED"
        assert json.loads(s["calibration"])["x"]["kind"] == "LINEAR"
        prov = json.loads(s["provenance"])
        assert prov["source_sha256"] and prov["digitizer_version"] == "fd-0.1.4" and prov["route"] == "A"
        assert abs(s["x_max"] - 30.0) < 1e-2 and "EXTRAPOLATED_BEYOND_TICKS" not in s["flags"]
    assert abs(by_colour["#ff0000"]["x_min"]) < 1e-2 and abs(by_colour["#0000ff"]["x_min"] - HIDDEN[0]) < 1e-2


def test_uncalibrated_axis_keeps_no_invented_values_and_model_hint(world):
    fid = "VKM-SRC-901:p0002:f000000000003"
    f = next(f for f in _rows(world, "figure_series_figures") if f["figure_id"] == fid)
    assert f["axis_status"] == "X_UNCALIBRATED" and f["x_axis_kind"] is None and f["y_axis_kind"] == "LINEAR"
    assert {"MODEL_HINT_IN_CAPTION", "OCR_HELPER_UNAVAILABLE"} <= set(f["flags"])
    series = [s for s in _rows(world, "figure_series") if s["figure_id"] == fid]
    assert series and all(s["axes_calibrated"] == "Y_ONLY" and "X_UNCALIBRATED" in s["flags"] for s in series)
    pts = [p for p in _rows(world, "figure_series_points") if p["figure_id"] == fid]
    assert pts and all(p["x"] is None and p["x_err"] is None and p["y"] is not None for p in pts)


def test_rotated_page_is_read_in_the_displayed_frame(world):
    f = next(f for f in _rows(world, "figure_series_figures") if f["figure_id"].endswith("f000000000005"))
    assert f["figure_status"] == "DIGITIZED" and f["page_rotation"] == 90 and "ROTATED_PAGE" in f["flags"]
    assert f["available_basis"] == "UNKNOWN" and "AVAILABILITY_UNKNOWN" in f["flags"]
    pts = [p for p in _rows(world, "figure_series_points") if p["figure_id"] == f["figure_id"]]
    assert np.allclose(sorted((p["x"], p["y"]) for p in pts), sorted(RED + BLUE), atol=1e-2)


def test_no_resources_root_is_skipped(world, monkeypatch):
    monkeypatch.delenv("VKM_RESOURCES_ROOT", raising=False)
    con = duckdb.connect(str(world["db"]), read_only=True)
    st: dict = {}
    try:
        assert FS.build(con, stats=st) is None and "WORKSTATION" in st["reason"]
    finally:
        con.close()


def _build_cli(db, out, root, *extra):
    argv = ["nav", "build", "--duckdb", str(db), "--out", str(out), "--part", "figure_series",
            "--option", f"figure_series.resources={root}", "--option", "figure_series.ocr=off", *extra]
    assert cli.main(argv) == 0
    return {n: hashlib.sha256((out / f"{n}.parquet").read_bytes()).hexdigest() for n in FS.DATASETS}


def test_nav_build_is_deterministic_across_worker_counts(world):
    a = _build_cli(world["db"], world["tmp"] / "b1", world["root"], "--option", "figure_series.workers=1")
    b = _build_cli(world["db"], world["tmp"] / "b2", world["root"], "--option", "figure_series.workers=2")
    assert a == b
    m = json.loads((world["tmp"] / "b1" / "manifest.json").read_text(encoding="utf-8"))
    assert m["parts"]["figure_series"]["status"] == "BUILT"
    assert m["parts"]["figure_series"]["rule_version"] == "figure_series_v1"
    assert {m["datasets"][n]["sha256"] for n in FS.DATASETS} == set(a.values())
    assert str(world["root"]) not in json.dumps(m["parts"]["figure_series"]["stats"])   # no machine path in stats


def test_registration():
    parts = list(nav_cli.PARTS)
    assert parts[-1] == "topics" and "figure_series" in parts
    assert nav_cli.resolve_part("figure_series") is FS.build
    assert nav_cli.datasets_of("figure_series") == FS.DATASETS + FS.RASTER_DATASETS
    assert nav_ids.RULE_VERSIONS["figure_series"] == FS.RULE_VERSION == "figure_series_v1"
    assert set(FS.DATASETS) <= set(nav_ids.DATASETS)
    assert store.resolve("find_figure_series") is Q.find_figure_series
    assert store.resolve("figure_series") is Q.get_figure_series
    # the raster pages of FD's sweep are part of the code: a build needs no file for them
    pages = FS._raster_pages("fd_monitoring")
    assert len(pages) == 9 and sum(len(v) for v in pages.values()) == 38 and pages["VKM-SRC-034"] == [2]
    assert FS._raster_pages({"VKM-SRC-002": [15, 12, 12]}) == {"VKM-SRC-002": [12, 15]}


# ------------------------------------------------------------------------------------------------ import
def _nav_dir(tmp, snap=SNAP):
    d = tmp / "nav"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"section_id": ["SEC-0000000000000001"], "source_id": ["VKM-SRC-901"]}),
                   d / "sections.parquet")
    sha = hashlib.sha256((d / "sections.parquet").read_bytes()).hexdigest()
    (d / "manifest.json").write_text(json.dumps({
        "format": "vkm-nav-manifest-v1", "snapshot": {"snapshot_id": snap},
        "datasets": {"sections": {"path": "sections.parquet", "part": "sections", "rows": 1, "sha256": sha}},
        "parts": {"sections": {"status": "BUILT"}}}), encoding="utf-8")
    return d


def test_import_copies_byte_for_byte_and_merges_the_manifest(world, tmp_path):
    bundle = world["tmp"] / "b1"
    if not bundle.exists():
        _build_cli(world["db"], bundle, world["root"], "--option", "figure_series.workers=1")
    nav = _nav_dir(tmp_path)
    plan = FS.import_bundle(bundle, nav, canon_duckdb=world["db"], dry_run=True)
    assert plan["dry_run"] and plan["canon_check"]["missing_figure_ids"] == 0
    assert not (nav / "figure_series.parquet").exists()
    out = FS.import_bundle(bundle, nav, canon_duckdb=world["db"])
    assert out["imported"]
    for n in FS.DATASETS:
        assert (nav / f"{n}.parquet").read_bytes() == (bundle / f"{n}.parquet").read_bytes()
    m = json.loads((nav / "manifest.json").read_text(encoding="utf-8"))
    assert "sections" in m["datasets"] and m["parts"]["sections"]["status"] == "BUILT"
    assert m["parts"]["figure_series"]["imported"]["bundle_manifest_sha256"] == out["bundle_manifest_sha256"]
    # The dependency contract records a portable, explicitly unqualified directory
    # identity. It cannot promote an unhashed resource directory to checked bytes.
    assert str(world["root"]) not in json.dumps(m["parts"]["figure_series"])
    assert m["parts"]["figure_series"]["options"]["resources"] == {
        "directory": world["root"].name, "qualification": "UNVERIFIED_EXTERNAL_DIRECTORY"}
    # served: pack, publish, query through the NAV store
    root = tmp_path / "data"
    served = root / "derived" / "navigation" / SNAP
    served.parent.mkdir(parents=True)
    nav.rename(served)
    store.pack(served)
    # This import fixture has partial source identity, so publication is explicitly exploratory.
    store.publish(root, SNAP, require_verified=False)
    assert store.NavStore(root, canonical_db=world["db"]).meta()["identity_status"] == "AD_HOC_UNVERIFIED"
    ns = store.NavStore(root, canonical_db=world["db"])
    hit = ns.run("find_figure_series", text="оседания", limit=5)
    assert hit["total_figures"] == 2 and hit["captions_searched"] is True
    got = ns.run("figure_series", hit["figures"][0]["series"][0]["series_id"])
    assert got["series"][0]["points_returned"] == 7


def test_import_refusals(world, tmp_path):
    bundle = world["tmp"] / "b1"
    if not bundle.exists():
        _build_cli(world["db"], bundle, world["root"], "--option", "figure_series.workers=1")
    with pytest.raises(FS.ImportRefused, match="snapshot mismatch"):
        FS.import_bundle(bundle, _nav_dir(tmp_path / "x", snap="snap-other"))
    partial = world["tmp"] / "partial"
    _build_cli(world["db"], partial, world["root"], "--option", "figure_series.sources=VKM-SRC-901")
    with pytest.raises(FS.ImportRefused, match="partial"):
        FS.import_bundle(partial, _nav_dir(tmp_path / "y"))
    assert FS.import_bundle(partial, _nav_dir(tmp_path / "z"), allow_partial=True, dry_run=True)["dry_run"]
    broken = tmp_path / "broken"
    broken.mkdir()
    for f in bundle.iterdir():
        (broken / f.name).write_bytes(f.read_bytes())
    t = pq.read_table(broken / "figure_series_figures.parquet")
    pq.write_table(t.slice(0, 1), broken / "figure_series_figures.parquet")
    with pytest.raises(FS.ImportRefused, match="sha256"):
        FS.import_bundle(broken, _nav_dir(tmp_path / "w"))
    other = duckdb.connect(str(tmp_path / "other.duckdb"))
    other.execute("CREATE SCHEMA canonical")
    other.execute("CREATE SCHEMA meta")
    other.execute("CREATE TABLE meta.snapshot (snapshot_id VARCHAR)")
    other.execute("INSERT INTO meta.snapshot VALUES (?)", [SNAP])
    other.execute("CREATE TABLE canonical.figures (object_id VARCHAR)")
    other.close()
    with pytest.raises(FS.ImportRefused, match="not in canonical.figures"):
        FS.import_bundle(bundle, _nav_dir(tmp_path / "v"), canon_duckdb=tmp_path / "other.duckdb")
    assert FS.main(["import", "--bundle", str(broken), "--nav-dir", str(_nav_dir(tmp_path / "u"))]) == 2


# ------------------------------------------------------------------------------------------------ queries
@pytest.fixture()
def qcon(world):
    con = duckdb.connect()
    for name, table in world["tables"].items():
        con.register(f"nav_{name}", table)
    con.execute(f"ATTACH '{world['db'].as_posix()}' AS canon (READ_ONLY)")
    con.execute("CREATE SCHEMA canonical")
    con.execute("CREATE VIEW canonical.figures AS SELECT * FROM canon.canonical.figures")
    yield con
    con.close()


def test_find_by_words_unit_time_and_source(qcon):
    r = Q.find_figure_series(qcon, text="оседания поверхности")
    assert [f["figure_id"] for f in r["figures"]] == ["VKM-SRC-901:p0001:f000000000001"]
    f = r["figures"][0]
    assert "caption" in f["matched_in"] and f["n_series_matched"] == 2 and f["y"]["unit_raw"] == "мм"
    assert f["caption"].startswith("Рис. 1") and r["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    assert {s["series_color"] for s in f["series"]} == {"#ff0000", "#0000ff"}
    by_title = Q.find_figure_series(qcon, text="оседание")          # axis title of both digitized charts
    assert by_title["total_figures"] == 2 and by_title["figures"][0]["score"] >= by_title["figures"][1]["score"]
    assert Q.find_figure_series(qcon, unit="mm")["total_figures"] == 2           # мм ↔ mm
    assert Q.find_figure_series(qcon, unit="МПа")["total_figures"] == 0
    assert Q.find_figure_series(qcon, time_series=True, source_id="VKM-SRC-902")["total_series"] == 2
    assert Q.find_figure_series(qcon, text="гравитация")["figures"] == []
    loose = Q.find_figure_series(qcon, text="прогноз", calibrated_only=False)
    assert [f["figure_status"] for f in loose["figures"]] == ["X_UNCALIBRATED"]
    assert loose["figures"][0]["series"][0]["axes_calibrated"] == "Y_ONLY"
    assert Q.find_figure_series(qcon, text="прогноз")["figures"] == []           # calibrated only by default
    failed = Q.find_figure_series(qcon, source_id="VKM-SRC-903", calibrated_only=False)
    assert failed["figures"][0]["figure_status"] == "SOURCE_HASH_MISMATCH" and failed["figures"][0]["series"] == []


def test_get_by_series_and_figure(qcon):
    fid = "VKM-SRC-901:p0001:f000000000001"
    whole = Q.get_figure_series(qcon, fid)
    assert whole["figure"]["figure_status"] == "DIGITIZED" and len(whole["series"]) == 2
    assert whole["points_total"] == 15 and not whole["points_truncated"]
    assert whole["figure"]["n_points_outside_plot"] == 1
    assert [p["in_plot_area"] for s in whole["series"] for p in s["points"]].count(False) == 1
    s = whole["series"][0]
    assert s["calibration"]["y"]["n_labels"] == 4 and s["provenance"]["rule_version"] == "figure_series_v1"
    assert s["x"]["unit_raw"] == "сут" and s["points"][0]["x_err"] > 0
    one = Q.get_figure_series(qcon, s["series_id"], max_points=3)
    assert [x["series_id"] for x in one["series"]] == [s["series_id"]]
    assert one["points_returned"] == 3 and one["points_truncated"] is True
    empty = Q.get_figure_series(qcon, "VKM-SRC-904:p0001:f000000000007")
    assert empty["series"] == [] and empty["figure"]["figure_status"] == "SOURCE_UNAVAILABLE"
    assert Q.get_figure_series(qcon, "FS-0000000000000000") is None
    assert Q.get_figure_series(qcon, "VKM-SRC-901:p0001:f000000000002") is None    # not a candidate


def test_query_without_the_part_is_unavailable():
    con = duckdb.connect()
    with pytest.raises(store.NavUnavailable):
        Q.find_figure_series(con, text="оседание")
    with pytest.raises(store.NavUnavailable):
        Q.get_figure_series(con, "FS-0000000000000000")


def test_quality_flag_helpers():
    from vkm_corpus.figures.calibrate import Axis, fit_axis
    from vkm_corpus.figures.primitives import Path

    box = (100.0, 100.0, 400.0, 300.0)
    assert FS.in_plot_area(box, 250, 200) and FS.in_plot_area(box, 402, 200)          # 1 % tolerance
    assert FS.in_plot_area(box, 80, 200) is False and FS.in_plot_area(box, None, 1) is None
    # the chart frame runs below the lowest labelled tick: the frame, not the tick span, bounds the plot area
    line = lambda a, b: Path(np.array([a, b], float), None, 0.8, "LINE")  # noqa: E731
    frame = [line((100, 80), (400, 80)), line((100, 340), (400, 340)), line((100, 80), (100, 340)),
             line((400, 80), (400, 340)), line((430, 100), (600, 100)),        # a neighbour panel's line: too short
             line((150, 300), (150, 304))]                                      # a tick mark
    assert FS.frame_box(frame, box) == [100.0, 80.0, 400.0, 340.0]
    assert FS.frame_box([], box) == list(box)
    # a line on the box edge frames that side; a line one box away (a neighbour panel, the figure border) is not
    # the frame, nor is anything beyond half the box size
    framed = [line((100, 300), (400, 300)), line((100, 500), (400, 500)), line((100, 100), (400, 100)),
              line((100, 20), (400, 20)), line((-60, 100), (-60, 300))]
    assert FS.frame_box(framed, box) == [100.0, 100.0, 400.0, 300.0]
    ax = Axis("y", "LINEAR", 0.0, 1.0, labels=[["0", 0.0, 0, True, "NATIVE"], ["10", 10.0, 10, True, "NATIVE"]])
    assert FS.beyond_ticks_share(ax, [0, 5, 10, 12.4]) == 0.0                           # a quarter span allowed
    assert FS.beyond_ticks_share(ax, [0, 5, 13, 30]) == 0.5 and FS.beyond_ticks_share(None, [1]) == 0.0
    # curves drawn as filled outlines of their strokes: elongated coloured fills, no coloured strokes
    seg = np.array([[0, 0], [6, 1.5], [6.6, 2.4], [6, 3.0], [0, 1.5], [-0.6, 0.6], [0, 0]], float)
    fills = [Path(seg + [7 * i, 0], 0xFF0000, 0.0, "FILL") for i in range(25)]
    assert FS.outlined_strokes(fills) == (25, 0)
    assert FS.outlined_strokes([Path(np.array([[0, 0], [9, 9]], float), 0xFF0000, 1, "LINE")] * 3) == (0, 3)
    # fd-0.1.4: a narrow range far from zero stays linear, decades may be logarithmic
    years = [[str(y), float(y), 10.0 * (y - 1998), True, "NATIVE"] for y in range(1998, 2004)]
    assert fit_axis("x", years, "NUM", 3.0).kind == "LINEAR"
    decades = [[str(v), float(v), 50.0 * i, True, "NATIVE"] for i, v in enumerate((10, 100, 1000, 10000))]
    assert fit_axis("y", decades, "NUM", 3.0).kind == "LOG10"


def test_axis_plausibility_flags():
    from vkm_corpus.figures.calibrate import Axis
    from vkm_corpus.figures.primitives import Text

    box = (100.0, 100.0, 400.0, 300.0)

    def column(x, values, positions, a, b):
        labels = [[str(v), float(v), float(p), True, "NATIVE"] for v, p in zip(values, positions)]
        texts = [Text(str(v), x - 6, x + 6, float(p), 6.0) for v, p in zip(values, positions)]
        return Axis("y", "LINEAR", a, b, labels=labels), texts

    left, t_left = column(80, (0, -20, -40, -60), (300, 233.3, 166.7, 100), -90.0, 0.3)
    inside, t_inside = column(330, (600, 575, 550, 525), (140, 160, 180, 200), 775.0, -1.25)
    assert not FS.labels_inside_plot(left, t_left, box) and FS.labels_inside_plot(inside, t_inside, box)
    # a second y axis on the right with another scale; the same scale repeated on the right is not a second axis
    right, t_right = column(420, (0, 1, 2, 3), (300, 233.3, 166.7, 100), 4.5, -0.015)
    mirror, t_mirror = column(420, (0, -20, -40, -60), (300, 233.3, 166.7, 100), -90.0, 0.3)
    assert FS.other_axis(left, [left, right], t_left + t_right, box)
    assert not FS.other_axis(left, [left, mirror], t_left + t_mirror, box)
    # a compact column of numbered legend samples is not a second axis (it spans < 30 % of the plot), nor is a scale
    # standing far from the frame (a colour bar); a short right axis at the frame is one
    legend, t_legend = column(420, (1, 2, 3), (120, 135, 150), -7.0, 1.0 / 15)
    assert not FS.other_axis(left, [left, legend], t_left + t_legend, box)
    bar, t_bar = column(470, (0, 1, 2, 3), (300, 233.3, 166.7, 100), 4.5, -0.015)
    assert not FS.other_axis(left, [left, bar], t_left + t_bar, box)
    short, t_short = column(412, (2, 0, -2), (140, 180, 220), 9.0, -0.05)
    assert FS.other_axis(left, [left, short], t_left + t_short, box)
    narrow = (100.0, 100.0, 380.0, 300.0)            # a frame found too narrow: the axis line beside the labels counts
    assert not FS.other_axis(left, [left, short], t_left + t_short, box, frame=narrow)
    axis_line = ([], [(100.0, 300.0, 401.0)])        # (horizontal, vertical) neutral segments: x = 401 over y 100…300
    assert FS.other_axis(left, [left, short], t_left + t_short, box, frame=narrow, segments=axis_line)
    # numbers of a legend inside the frame are no axis, even beside a grid line
    legend_in, t_legend_in = column(360, (2, 0, -2), (140, 180, 220), 9.0, -0.05)
    grid = ([], [(100.0, 300.0, 350.0)])
    assert not FS.other_axis(left, [left, legend_in], t_left + t_legend_in, box, segments=grid)
    # the x and y axis lines of one chart meet at its corner; x labels under another panel do not belong to this plot
    def row(y, values, positions):
        labels = [[str(v), float(v), float(p), True, "NATIVE"] for v, p in zip(values, positions)]
        return Axis("x", "LINEAR", 0.0, 0.1, labels=labels), [Text(str(v), p - 6, p + 6, y, 6.0)
                                                             for v, p in zip(values, positions)]
    xs, t_xs = row(312, (0, 20, 40, 60), (100, 200, 300, 400))           # the upper panel's own x labels
    xs_low, t_xs_low = row(512, (0, 10, 20, 30), (100, 200, 300, 400))    # the lower panel's
    pieces = ([(100.0, 250.0, 300.0), (250.5, 400.0, 300.2), (100.0, 400.0, 500.0)], [(100.0, 300.0, 100.0)])
    lines = FS.merged_lines(pieces)                   # an x axis drawn in two pieces is one line
    assert [(a, b) for a, b, _ in lines[0]] == [(100.0, 400.0), (100.0, 400.0)] and len(lines[1]) == 1
    assert not FS.labels_detached(xs, left, t_xs + t_left, box, lines, [xs, left])
    everything = t_xs_low + t_left + t_xs
    assert FS.labels_detached(xs_low, left, everything, box, lines, [xs_low, left, xs])
    # the frame reaches the chart's own axis lines when they meet (the x axis below the lowest labelled y tick)
    xs_axis, t_xs_axis = row(352, (0, 10, 20, 30), (100, 200, 300, 400))
    chart = FS.merged_lines(([(100.0, 400.0, 340.0)], [(100.0, 340.0, 100.0)]))
    assert FS.axes_frame(box, xs_axis, left, t_xs_axis + t_left, chart) == [100.0, 100.0, 400.0, 340.0]
    assert FS.axes_frame(box, xs_axis, left, t_xs_axis + t_left, ([], [])) == list(box)
    # stacked panels sharing the lower x axis (the upper panel prints none) are sound; without lines: not judged
    assert not FS.labels_detached(xs_low, left, t_xs_low + t_left, box, lines, [xs_low, left])
    assert not FS.labels_detached(xs_low, left, everything, box, ([], []), [xs_low, left, xs])
    # 10³ read as «103»: «10x» labels with an exponent step of 1 to 3
    def tens(*ts):
        return Axis("y", "LINEAR", 0, 1, labels=[[t, float(t), 50.0 * i, True, "NATIVE"] for i, t in enumerate(ts)])
    assert FS.power_of_ten_labels(tens("100", "101", "102", "103"))
    assert FS.power_of_ten_labels(tens("100", "102", "104"))
    assert not FS.power_of_ten_labels(left) and not FS.power_of_ten_labels(tens("100", "105", "110"))
    # an axis kept on three labels is weak on irregular values or after dropping a label off their step inside their
    # range; «0, 20, 30» without a misplaced «10» is a sound axis
    three = Axis("y", "LINEAR", 0, 1, labels=[["0", 0.0, 0, True, "NATIVE"], ["20", 20.0, 20, True, "NATIVE"],
                                              ["30", 30.0, 30, True, "NATIVE"]])
    assert not FS.weak_axis(three) and not FS.weak_axis(left)
    three.dropped = ["10"]
    assert not FS.weak_axis(three)
    three.dropped = ["10", "25"]
    assert FS.weak_axis(three)
    odd = Axis("y", "LOG10", 0, 1, labels=[["0.32", 0.32, 0, True, "NATIVE"], ["0.062", 0.062, 5, True, "NATIVE"],
                                           ["0.171", 0.171, 9, True, "NATIVE"]])
    assert FS.weak_axis(odd) and not FS.regular_ticks(odd)
    fine = Axis("x", "LOG10", 0, 1, labels=[[t, float(t), i, True, "NATIVE"] for i, t in enumerate(("10", "15", "20",
                                                                                                    "30"))])
    assert FS.regular_ticks(fine) and not FS.weak_axis(fine)
    # «1,000 2,000 3,000»: a thousands comma or a decimal comma — ambiguous; «0,5 1,0 1,5» is a decimal comma
    commas = Axis("x", "LINEAR", 0, 1, labels=[[t, float(t.replace(",", ".")), i, True, "NATIVE"]
                                               for i, t in enumerate(("1,000", "2,000", "3,000"))])
    decimals = Axis("x", "LINEAR", 0, 1, labels=[[t, float(t.replace(",", ".")), i, True, "NATIVE"]
                                                 for i, t in enumerate(("0,5", "1,0", "1,5"))])
    assert FS.thousands_ambiguous(commas, []) and not FS.thousands_ambiguous(decimals, [])
    # «80 000» split by the text layer into «80» + «000» (the gap of a space) — but not two close labels «100» «110»
    split, t_split = column(80, (10, 20, 30), (280, 250, 220), 310.0 / 3, -1.0 / 3)
    groups = [Text("000", t.x1 + 1.5, t.x1 + 12.0, t.yc, 6.0) for t in t_split]
    assert FS.thousands_ambiguous(split, t_split + groups)
    far = [Text("110", t.x1 + 2.6, t.x1 + 14.0, t.yc, 6.0) for t in t_split]
    assert not FS.thousands_ambiguous(split, t_split + far) and not FS.thousands_ambiguous(split, t_split)
    assert {"AXIS_LABELS_INSIDE_PLOT", "AXIS_LABELS_DETACHED", "SECOND_Y_AXIS", "POWER_OF_TEN_LABELS",
            "THOUSANDS_SEPARATOR_AMBIGUOUS", "WEAK_AXIS_CALIBRATION"} <= FS.SUSPECT_FLAGS


def test_gallery_opens_without_a_server(world, tmp_path):
    from vkm_corpus.figures import gallery

    bundle = world["tmp"] / "b1"
    if not bundle.exists():
        _build_cli(world["db"], bundle, world["root"], "--option", "figure_series.workers=1")
    out = tmp_path / "gallery"
    rep = gallery.build_gallery(bundle, world["db"], out, resources=str(world["root"]), workers=1)
    assert rep["figures"] == 5 and rep["crops"] == 3 and rep["crop_errors"] == 2        # 903/904: no crop
    html_text = (out / "index.html").read_text(encoding="utf-8")
    data = json.loads(html_text.split('id="data">', 1)[1].split("</script>", 1)[0])
    assert [f["status"] for f in data["figures"]][:2] == ["DIGITIZED", "DIGITIZED"]      # digitized first
    for f in data["figures"]:
        for key in ("crop", "overlay", "replot", "csv"):
            if f[key]:
                assert not f[key].startswith(("/", "file:")) and (out / f[key]).is_file()   # relative, present
    assert str(world["root"]) not in html_text and str(tmp_path) not in html_text
    csv_text = (out / "csv" / "VKM-SRC-901_p0001_f000000000001.csv").read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0].startswith("figure_id,series_id") and "DERIVATION" in csv_text
    assert "stroke-dasharray" in (out / "replot" / "VKM-SRC-901_p0001_f000000000001.svg").read_text(encoding="utf-8")


def test_unit_normalisation():
    assert Q.norm_unit("мм") == Q.norm_unit("mm") == Q.norm_unit(" MM. ") == "мм"
    assert Q.norm_unit("сутки") == Q.norm_unit("сут.") == Q.norm_unit("days") == "сут"
    assert Q.norm_unit("мм/год") == Q.norm_unit("mm/yr") and Q.norm_unit(None) is None
