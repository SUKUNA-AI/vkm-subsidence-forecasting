"""Entity index (agent Г, 13.10; ``scripts/build_entity_index.py``, ``search.entities``): number and site keys, the
lookup, the builder's rules on a tiny synthetic PUBLIC tree (locator pages, picks merged into their borehole, C4
object ids, observation codes, monitoring lines and benchmarks, mining geometry, «possibly the same» notes, unnumbered
records left out, quotes replaced) and the committed index: current, keyed, page ids consistent, borehole 75 of
SRC-012 on ``VKM-SRC-012:p0064`` (table 3.1 — the page the phrase «скважина 75» cannot find)."""
from __future__ import annotations

import csv
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

from vkm_corpus.search import entities as E

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
B = importlib.import_module("build_entity_index")


@pytest.mark.parametrize("raw, key", [("75", "75"), ("№ 75", "75"), ("1 036", "1036"), ("120c", "120с"),
                                      ("120с", "120с"), ("9 ВП", "9вп"), ("1-ОП", "1-оп"), ("2–бис", "2-бис"),
                                      ("559'", "559'"), ("(75)", "75"), ("V", "v"), ("XIII", "хiii")])
def test_number_key(raw, key):
    assert E.number_key(raw) == key


def test_site_key_meets_across_scripts():
    assert E.site_key("СКРУ-1") == E.site_key("SKRU1") == "skru1"
    assert E.site_key("СКРУ-1") in E.site_key("СКРУ-1 (по чтению плана)")
    assert E.site_key("БКПРУ-4") == "bkpru4"


def _row(**kw):
    base = {c: "" for c in E.COLUMNS}
    base.update(kw)
    return base


ROWS = [_row(entity_row_id="ENT-1", entity_kind="BOREHOLE", entity_id="BH-SOL-75", number="75", number_key="75",
             site="СКРУ-1 (по чтению плана)", source_id="VKM-SRC-012", page_id="VKM-SRC-012:p0064", page_index="64",
             record_ids="BH-SOL-75;PK-1;PK-2"),
        _row(entity_row_id="ENT-2", entity_kind="BOREHOLE", entity_id="BH-BIB-75", number="75", number_key="75",
             site="библиография", source_id="VKM-SRC-011", page_id="VKM-SRC-011:p0159", page_index="159"),
        _row(entity_row_id="ENT-3", entity_kind="BOREHOLE", entity_id="BH-DUR-206", number="206",
             number_key="206;209", site="Дуринская площадь", source_id="VKM-SRC-011", page_id="VKM-SRC-011:p0050"),
        _row(entity_row_id="ENT-4", entity_kind="PANEL", entity_id="SKRU1:PAN13:9VP", number="9ВП",
             number_key="9вп", site="SKRU1", source_id="VKM-SRC-096", page_id="VKM-SRC-096:p0002")]


def test_lookup_by_kind_number_site_source_page():
    ix = E.EntityIndex.from_rows(ROWS, "pack")
    assert [r["entity_row_id"] for r in ix.lookup("BOREHOLE", "75")] == ["ENT-1", "ENT-2"]     # every site
    assert [r["entity_row_id"] for r in ix.lookup("BOREHOLE", "75", site="СКРУ-1")] == ["ENT-1"]
    assert [r["entity_row_id"] for r in ix.lookup(None, "209")] == ["ENT-3"]                  # a reading variant
    assert [r["entity_row_id"] for r in ix.lookup("PANEL", "9 ВП")] == ["ENT-4"]
    assert [r["entity_row_id"] for r in ix.lookup(source_id="VKM-SRC-011")] == ["ENT-2", "ENT-3"]
    assert [r["entity_row_id"] for r in ix.lookup(page_id="VKM-SRC-012:p0064")] == ["ENT-1"]
    assert ix.lookup("LINE", "75") == [] and ix.lookup("BLOCK", "9 ВП") == []
    with pytest.raises(ValueError):
        ix.lookup("WELLHEAD", "75")
    objs = E.objects(ix.lookup("BOREHOLE", "75"))
    assert [(o["entity_id"], o["n_pages"]) for o in objs] == [("BH-SOL-75", 1), ("BH-BIB-75", 1)]


# ---------------------------------------------------------------------------------------------------- builder
def _write(root: Path, rel: str, rows: list[dict]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _tree(root: Path) -> None:
    _write(root, B.BOREHOLES, [
        {"borehole_id": "BH-SOL-75", "alternate_ids": "75 | скв.75", "aliases_qualified": "SOL:75",
         "sources": "VKM-SRC-012;VKM-SRC-014", "locators": "012:p28:Рис. 1.1 | 012:p64:Табл. 3.1 / Рис. 3.2 | 014:p78",
         "mine_or_site": "СКРУ-1 (по чтению плана)", "site_attribution_status": "DERIVATION", "status": "FACT",
         "identity_basis": "SAME_NUMBER_SAME_MINE_AREA: один номер"},
        {"borehole_id": "BH-BIB-75", "alternate_ids": "75 | скважина 75 (1950)", "aliases_qualified": "BIB:75",
         "sources": "VKM-SRC-011", "locators": "011:p159:Прил. 4", "mine_or_site": "библиография",
         "site_attribution_status": "UNKNOWN", "status": "FACT", "identity_basis": "single mention"},
        {"borehole_id": "BH-SHS1-2bis", "alternate_ids": "ствол № 2-бис | ствол 2-бис",
         "aliases_qualified": "SHS1:ствол 2-бис", "sources": "VKM-SRC-023", "locators": "023:r35",
         "mine_or_site": "СКРУ-1", "site_attribution_status": "FACT", "status": "FACT", "identity_basis": ""},
        {"borehole_id": "BH-GRP-1", "alternate_ids": "«в скважинах» / группа без номеров", "aliases_qualified": "",
         "sources": "VKM-SRC-023", "locators": "023:r27", "mine_or_site": "«на исследуемой территории»",
         "site_attribution_status": "", "status": "FACT", "identity_basis": ""},
        {"borehole_id": "BH-DUR-206", "alternate_ids": "206 | 209", "aliases_qualified": "DUR:206 | DUR:209",
         "sources": "VKM-SRC-011", "locators": "разбор страниц: 011:p0050, 011:p0051", "mine_or_site": "Дуринская",
         "site_attribution_status": "FACT", "status": "FACT", "identity_basis": ""}])
    _write(root, B.ISSUES, [{"issue_id": "ISS-075-BIBLIO", "issue_type": "POSSIBLE_SAME_NOT_MERGED",
                             "borehole_ids": "BH-SOL-75;BH-BIB-75", "decision": "KEPT_SEPARATE"}])
    _write(root, B.PICKS, [
        {"pick_id": "PK-1", "borehole_id": "BH-SOL-75", "source_id": "VKM-SRC-012",
         "locator": "VKM-SRC-012:p0064; Табл. 3.1", "vn_id": "", "status": "FACT"},
        {"pick_id": "PK-2", "borehole_id": "BH-SOL-75", "source_id": "VKM-SRC-012", "locator": "p.65 стр. 64",
         "vn_id": "", "status": "DERIVATION"},
        {"pick_id": "PK-3", "borehole_id": "BH-SOL-75", "source_id": "VKM-SRC-012", "locator": "без страницы",
         "vn_id": "EV-VN-S012-0001", "status": "FACT"},
        {"pick_id": "PK-4", "borehole_id": "BH-GRP-1", "source_id": "VKM-SRC-023", "locator": "p.27", "vn_id": "",
         "status": "FACT"}])
    _write(root, "evidence/world/c4/skru3_mining_calendar.csv", [
        {"row_id": "C4-1", "object_id": "SKRU3:PAN:15:B11", "object_as_printed": "Блок 11 Панели 15 «шахтного поля»",
         "site_norm": "SKRU3", "source_id": "VKM-SRC-012", "page_id": "VKM-SRC-012:p0053", "locator": "§2.3",
         "status": "FACT"},
        {"row_id": "C4-2", "object_id": "SKRU1:PAN13:9VP;SL:3PNIPU", "object_as_printed": "9 ВП и линия № 3",
         "site_norm": "SKRU1", "source_id": "VKM-SRC-096", "page_id": "VKM-SRC-096:p0002;VKM-SRC-096:p0003",
         "locator": "рис. 2", "status": "DERIVATION"},
        {"row_id": "C4-3", "object_id": "MINE:SKRU3", "object_as_printed": "рудник", "site_norm": "SKRU3",
         "source_id": "VKM-SRC-011", "page_id": "VKM-SRC-011:p0147", "locator": "", "status": "FACT"}])
    _write(root, B.OBSERVATIONS, [
        {"obs_id": "WOB-1", "line_or_benchmark": "SKRU1_PL_V", "site_norm": "SKRU1", "source_id": "VKM-SRC-044",
         "page_id": "VKM-SRC-044:p0179", "figure_label": "Рис. 106", "status": "DERIVATION"},
        {"obs_id": "WOB-2", "line_or_benchmark": "2", "site_norm": "SKRU2", "source_id": "VKM-SRC-012",
         "page_id": "VKM-SRC-012:p0044", "figure_label": "", "status": "FACT"}])
    _write(root, B.MONITORING, [
        {"cat_id": "MON-1", "spatial_support": "survey_line | Профильная линия № 2 | ids: реперы 50,51; Rp3.1",
         "scope": "SKRU2", "source_id": "VKM-SRC-012", "locator": "p103 [Рис. 4.8]", "vn_ids": "",
         "status": "FACT"},
        {"cat_id": "MON-2", "spatial_support": "site | г. Березники", "scope": "BKPRU1", "source_id": "VKM-SRC-003",
         "locator": "p5", "vn_ids": "", "status": "FACT"}])
    _write(root, B.GEOMETRY, [
        {"row_id": "MGEO-1", "spatial_entity_ids": "SKRU1:BLK:201", "mine_scope": "СКРУ-1",
         "source_ids": "VKM-SRC-011", "locators": "pdf p.130 (printed 259); Рис. 4.6", "status": "FACT"}])
    _write(root, B.RECORD_INDEX, [{"vn_id": "EV-VN-S012-0001", "source_id": "VKM-SRC-012", "pdf_page": "66"}])


def test_builder_rules_on_a_synthetic_tree(tmp_path):
    _tree(tmp_path)
    text, receipt = B.build(tmp_path)
    rows = list(csv.DictReader(text.splitlines()))
    by = {(r["entity_id"], r["page_id"]): r for r in rows}
    # borehole catalogue: one row per (object, source, page); picks merge into their borehole's page or add one
    t31 = by[("BH-SOL-75", "VKM-SRC-012:p0064")]
    assert t31["record_ids"] == "BH-SOL-75;PK-1" and t31["object_labels"] == "Табл. 3.1; Рис. 3.2"
    assert t31["number_key"] == "75" and t31["page_index"] == "64" and t31["status"] == "FACT"
    assert t31["site"] == "СКРУ-1 (по чтению плана)" and t31["site_attribution"] == "DERIVATION"
    assert "possibly the same as BH-BIB-75 (ISS-075-BIBLIO): kept separate" in t31["identity_note"]
    assert by[("BH-SOL-75", "VKM-SRC-012:p0065")]["page_basis"] == "LOCATOR_PAGE"            # «p.65» of a pick
    assert by[("BH-SOL-75", "VKM-SRC-012:p0066")]["page_basis"] == "RECORD_PDF_PAGE"          # the record's pdf_page
    assert ("BH-SOL-75", "VKM-SRC-014:p0078") in by and ("BH-BIB-75", "VKM-SRC-011:p0159") in by
    assert by[("BH-SHS1-2bis", "VKM-SRC-023:r0035")]["entity_kind"] == "SHAFT"               # DOCX render page
    assert by[("BH-DUR-206", "VKM-SRC-011:p0051")]["number_key"] == "206;209"                 # reading variants
    assert not any(r["entity_id"] == "BH-GRP-1" for r in rows)                                # no number: left out
    # C4 object ids: a block with its panel, a panel with direction letters, a profile line; a mine gives nothing
    assert by[("SKRU3:PAN:15:B11", "VKM-SRC-012:p0053")]["parent"] == "панель 15"
    assert by[("SKRU3:PAN:15:B11", "VKM-SRC-012:p0053")]["entity_label"] == "Блок 11 Панели 15 «…»"   # no quote
    assert by[("SKRU3:PAN:15", "VKM-SRC-012:p0053")]["entity_kind"] == "PANEL"
    assert by[("SKRU1:PAN13:9VP", "VKM-SRC-096:p0003")]["number"] == "9ВП"
    assert by[("SL:3PNIPU", "VKM-SRC-096:p0002")]["number_key"] == "3"
    assert not any(r["entity_id"] == "MINE:SKRU3" for r in rows)
    # observation codes: PL codes only; monitoring lines and benchmarks within their scope; mining geometry
    assert by[("SKRU1_PL_V", "VKM-SRC-044:p0179")]["object_labels"] == "Рис. 106"
    assert not any(r["catalogue"] == B.OBSERVATIONS and r["number"] == "2" for r in rows)
    mon = [r for r in rows if r["catalogue"] == B.MONITORING]
    assert sorted((r["entity_kind"], r["number_key"]) for r in mon) == [("BENCHMARK", "3.1"), ("BENCHMARK", "50"),
                                                                       ("BENCHMARK", "51"), ("PROFILE_LINE", "2")]
    assert {r["page_id"] for r in mon} == {"VKM-SRC-012:p0103"} and {r["site"] for r in mon} == {"SKRU2"}
    assert by[("SKRU1:BLK:201", "VKM-SRC-011:p0130")]["site"] == "СКРУ-1"
    # receipt: inputs with sha256, counts, skipped records; deterministic
    assert receipt["rows"] == len(rows) and receipt["counts"]["rows_without_page"] == 0
    assert {i["path"] for i in receipt["inputs"]} >= {B.BOREHOLES, B.PICKS, B.RECORD_INDEX}
    assert receipt["skipped"]["borehole_catalog: label without a number (not indexed)"] == 1
    assert receipt["counts"]["r_page_sources"] == ["VKM-SRC-023"]
    assert B.build(tmp_path) == (text, receipt)
    assert len({r["entity_row_id"] for r in rows}) == len(rows)


# ---------------------------------------------------------------------------------------------------- committed
def _committed() -> list[dict[str, str]]:
    with (ROOT / B.OUT).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_committed_index_is_current():
    text, receipt = B.build(ROOT)
    assert (ROOT / B.OUT).read_text(encoding="utf-8") == text, "rerun scripts/build_entity_index.py"
    assert json.loads((ROOT / B.RECEIPT).read_text(encoding="utf-8")) == json.loads(B.receipt_text(receipt))


def test_committed_index_rows_are_keyed_and_paged():
    rows = _committed()
    assert len(rows) > 2000 and list(rows[0]) == list(E.COLUMNS)
    assert len({r["entity_row_id"] for r in rows}) == len(rows)
    assert {r["entity_kind"] for r in rows} <= set(E.ENTITY_KINDS)
    page = re.compile(r"^(VKM-SRC-\d{3}):[pr](\d{4})$")
    for r in rows:
        assert r["number_key"], r["entity_row_id"]
        m = page.match(r["page_id"])
        assert m and m.group(1) == r["source_id"] and int(m.group(2)) == int(r["page_index"]), r["entity_row_id"]
        assert not re.search(r"«[^…»]", r["entity_label"] + r["site"]), r["entity_row_id"]        # no quotes


def test_borehole_75_of_src012_is_on_the_table_31_page():
    """The review case: «скважина 75» — table 3.1 of SRC-012 (p0064) holds «Скважина» and «75» in different cells;
    the catalogue knows the borehole there, so the entity route can lift that page."""
    ix = E.EntityIndex.from_rows(_committed())
    rows = ix.lookup("BOREHOLE", "75")
    pages = {r["page_id"] for r in rows if r["entity_id"] == "BH-SOL-75"}
    assert "VKM-SRC-012:p0064" in pages
    t31 = next(r for r in rows if r["page_id"] == "VKM-SRC-012:p0064")
    assert "Табл. 3.1" in t31["object_labels"] and "possibly the same as BH-BIB-S011-75" in t31["identity_note"]
    # the same number elsewhere stays another object
    assert len({r["entity_id"] for r in rows}) >= 2
