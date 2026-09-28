"""Reference-list segmentation and parsing (extract.bibliography, CP-38) and its canonical rows (to_canon).

All references below are synthetic (invented authors and titles in the formats met in the corpus)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vkm_corpus import ids
from vkm_corpus.extract import bibliography as bib
from vkm_corpus.extract.model import BlockX, PageX, SourceInput, SourceResult
from vkm_corpus.extract.to_canon import CanonMapper

SID = "VKM-SRC-901"


def blk(page, ro, text, *, bt="REFERENCE_LIST", x0=50.0, y0=None, origin="NATIVE", primary=True):
    y = y0 if y0 is not None else 100.0 + 20.0 * ro
    return {"object_id": f"{SID}:p{page:04d}:b{ro:03d}", "source_id": SID, "page_id": f"{SID}:p{page:04d}",
            "block_type": bt, "reading_order": ro, "origin": origin, "bbox_x0": x0, "bbox_y0": y,
            "bbox_x1": 540.0, "bbox_y1": y + 15.0, "text": text, "is_primary_layer": primary}


# ------------------------------------------------------------------------------------------------ segmentation
def test_numbered_gost_list_with_heading_and_page_break():
    blocks = [
        blk(5, 1, "Основной текст главы о сдвижении горных пород.", bt="TEXT"),
        blk(5, 2, "Список литературы", bt="HEADING"),
        blk(5, 3, "1. Иванов И.И., Петров П.П. Модель оседания поверхности // Горный журнал. 2001. № 3. С. 12–17."),
        blk(5, 4, "2. Сидоров С.С. Сдвижение толщи над камерами. – М.: Недра, 1985. – 210 с."),
        blk(5, 5, "3. Кузнецов К.К. Длительная прочность каменной соли // Физико-технические"),
        blk(5, 6, "12", bt="PAGE_NUMBER"),
        blk(6, 1, "Колонтитул", bt="PAGE_HEADER"),
        blk(6, 2, "проблемы разработки полезных ископаемых. 1999. № 4. С. 40–48."),
        blk(6, 3, "4. Smith J.A., Brown K. Subsidence over salt mines // Int. J. Rock Mech. 2010. Vol. 47. P. 1–9."),
        blk(6, 4, "Заключение", bt="HEADING"),
        blk(6, 5, "1. Это уже не список: вывод первый.", bt="TEXT"),
    ]
    entries, st = bib.extract_entries(blocks)
    assert [e.label for e in entries] == ["1", "2", "3", "4"]
    assert [e.ordinal for e in entries] == [1, 2, 3, 4]
    third = entries[2]
    assert [b["object_id"] for b in third.blocks] == [f"{SID}:p0005:b005", f"{SID}:p0006:b002"]
    assert third.continues_on_page_id == f"{SID}:p0006"
    assert third.parsed.year == 1999 and third.parsed.pages == "40–48" and third.parsed.issue == "4"
    assert entries[0].continues_on_page_id is None
    assert st.numbered_zones == 1


def test_author_year_list_hanging_indent_and_inline_tail():
    blocks = [
        blk(9, 1, "References", bt="HEADING"),
        blk(9, 2, "ADAMS, R. & BAKER, T. 1999. A creep law for rock salt. Journal of Salt Studies,", x0=54.8),
        blk(9, 3, "12, 3, 101–115.", x0=72.9),
        blk(9, 4, "CARTER, N. 2004. Damage of salt around openings. Rock Mechanics, 7, 1–20.", x0=54.8),
        blk(10, 1, "Lett. 40, 761–783. Evans, D. L., 1994a. Wave fronts in media. Geophysics 59, 644–657.",
            x0=54.8),
        blk(10, 2, "Evans, D. L., Hill, C., Moore, B., Stone, E. R., Way, J. B., Kobrick, M., Ott, H.,", x0=54.8),
        blk(10, 3, "Klein, J. D., 2001. Radar sounding of layered media. Remote Sensing 3, 5–9.", x0=54.8),
    ]
    entries, _ = bib.extract_entries(blocks)
    texts = [e.normalized_text for e in entries]
    assert len(entries) == 4, texts
    assert texts[0].endswith("12, 3, 101–115.")
    assert texts[1].endswith("Lett. 40, 761–783."), "a tail at the top of the next page continues the entry"
    assert texts[2].startswith("Evans, D. L., 1994a.")
    assert texts[3].startswith("Evans, D. L., Hill, C.") and "Klein, J. D., 2001" in texts[3]
    p = entries[0].parsed
    assert p.authors == ["ADAMS, R", "BAKER, T"] and p.year == 1999
    assert p.title == "A creep law for rock salt" and p.venue == "Journal of Salt Studies"


def test_ocr_block_with_blank_line_separated_entries():
    text = ("Davis, J. C., Statistical Methods in Geology. New York: Wiley, 1973.\n\n"
            "Koch, G. and Link, R., Analysis of Geological Data. New York: Wiley, 1986.\n\n"
            "Moore, F., Data Analysis and Regression. Reading: Addison, 1977.")
    entries, _ = bib.extract_entries([blk(3, 1, text, origin="OCR")])
    assert [e.parsed.year for e in entries] == [1973, 1986, 1977]


def test_non_bibliographic_reference_blocks_are_dropped():
    blocks = [blk(2, 1, "И. Иванов,\nзаслуженный геолог"),
              blk(2, 2, "Oxford University Press\nCopyright © 1989 by Oxford University Press, Inc."),
              blk(2, 3, "[1] The coordinates of the corners are (11,241) and (20,250).")]
    entries, st = bib.extract_entries(blocks)
    assert entries == [] and st.dropped_not_bibliographic >= 2


def test_non_primary_layer_is_ignored():
    blocks = [blk(4, 1, "1. Иванов И.И. Книга о соли. – М.: Недра, 1980. – 100 с.", primary=False)]
    assert bib.extract_entries(blocks)[0] == []


def test_heading_zone_with_entries_labelled_as_text():
    blocks = [blk(7, 1, "СПИСОК ЛИТЕРАТУРЫ", bt="HEADING"),
              blk(7, 2, "1. Указания по защите рудников от затопления. – Л.: ВНИИГ, 1992. – 120 с.", bt="TEXT"),
              blk(7, 3, "2. Орлов О.О. Прогноз оседаний // Маркшейдерия. 2009. № 2. С. 5–9.", bt="TEXT"),
              blk(7, 4, "Сведения об авторах", bt="HEADING")]
    entries, _ = bib.extract_entries(blocks)
    assert [e.label for e in entries] == ["1", "2"]


def test_unexpected_restart_inside_an_entry_is_not_a_new_entry():
    blocks = [blk(8, 1, "354. Hubbert M.K. Role of fluid pressure. Part\n1. Mechanics of porous solids // Bull. "
                        "Geol. Soc. Am. 1959. Vol. 70."),
              blk(8, 2, "355. Lotze F. Steinsalz und Kalisalze. Berlin, 1957."),
              blk(8, 3, "356. Marggraf P. Der Salzstock. Leipzig, 1960.")]
    entries, _ = bib.extract_entries(blocks)
    assert [e.label for e in entries] == ["354", "355", "356"]


# ------------------------------------------------------------------------------------------------ parsing
def test_parse_gost_journal_article():
    p = bib.parse_entry("12. Иванов И.И., Петров П.П. Модель оседания поверхности // Горный журнал. – 2001. – "
                        "№ 3. – С. 12–17.")
    assert p.authors == ["Иванов И.И", "Петров П.П"]
    assert p.title == "Модель оседания поверхности" and p.venue == "Горный журнал"
    assert (p.year, p.issue, p.pages, p.language) == (2001, "3", "12–17", "ru")
    assert p.method == "bib_rules_v1/gost" and p.confidence == 1.0


def test_parse_title_first_with_statement_of_responsibility():
    p = bib.parse_entry("Разрывная тектоника месторождения / И.И. Иванов, П.П. Петров, С.С. Сидоров. – Пермь: "
                        "Издательство, 2004. – 194 с.")
    assert p.authors == ["И.И. Иванов", "П.П. Петров", "С.С. Сидоров"]
    assert p.title == "Разрывная тектоника месторождения" and p.year == 2004


def test_parse_springer_author_year_with_doi_and_volume_issue():
    p = bib.parse_entry("Allen RJ, Moore TK (2021) Creep of salt pillars. Environ Geotech 43(2):4553–4576. "
                        "https://doi.org/10.1007/S10653-021-00934-X")
    assert p.authors == ["Allen RJ", "Moore TK"] and p.year == 2021
    assert (p.volume, p.issue, p.pages) == ("43", "2", "4553–4576")
    assert p.doi == "10.1007/s10653-021-00934-x"
    assert p.title == "Creep of salt pillars"


def test_parse_isbn_10_is_converted_with_check_digit():
    p = bib.parse_entry("Петров П.П. Горная механика. – М.: Недра, 1990. – 300 с. – ISBN 5-247-00001-3.")
    assert p.isbn == ["9785247000013"] == [ids.normalize_isbn("5247000013")]
    bad = bib.parse_entry("Петров П.П. Горная механика. – М.: Недра, 1990. – ISBN 5-247-00001-9.")
    assert bad.isbn == []


def test_parse_folds_latin_homoglyphs_in_cyrillic_names():
    p = bib.parse_entry("38. Кoзлoв И. X., Семенов B.C. Ползучесть соли // Разработка месторождений. Пермь, "
                        "1973. С. 126–129.")
    assert p.authors == ["Козлов И. Х", "Семенов В.С"]
    assert p.year == 1973


def test_parse_year_skips_ranges_in_titles():
    p = bib.parse_entry("Орлов О.О. Добыча в 1941–1945 годах // Горная история. 2015. № 1. С. 3–8.")
    assert p.year == 2015


@pytest.mark.parametrize("text", ["СПИСОК ЛИТЕРАТУРЫ", "Литература", "5. Список использованных источников",
                                  "Библиографический список", "References", "REFERENCES CITED", "Literaturverzeichnis"])
def test_heading_variants(text):
    assert bib.is_heading_text(text)


def test_config_hash_is_stable():
    assert bib.CONFIG_HASH == bib.hashlib.sha256(bib.json.dumps(bib.CONFIG, sort_keys=True, ensure_ascii=False,
                                                                separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ canonical rows
def _result() -> SourceResult:
    src = SourceInput(SID, "private/901.pdf", "a" * 64, 1000, evidence_scope="GENERAL_METHOD")
    common = dict(origin="NATIVE", region_origin="PDF_TEXT_BLOCK", extractor_id="pymupdf-blocks",
                  extractor_version="1.0", generation="1", raw_config_hash="b" * 64, text_layer="PDF_TEXT_LAYER")
    blocks = [
        BlockX(page_index=5, bbox=(50, 100, 540, 115), reading_order=1, block_type="HEADING",
               text="Список литературы", **common),
        BlockX(page_index=5, bbox=(50, 120, 540, 160), reading_order=2, block_type="REFERENCE_LIST",
               text="1. Иванов И.И. Модель оседания // Горный журнал. 2001. № 3. С. 12–17.\n"
                    "2. Петров П.П. Сдвижение толщи. – М.: Недра, 1985. – 210 с.", **common),
        BlockX(page_index=5, bbox=(50, 170, 540, 190), reading_order=3, block_type="REFERENCE_LIST",
               text="3. Сидоров С.С. Прочность соли // Физико-технические проблемы", **common),
        BlockX(page_index=6, bbox=(50, 60, 540, 80), reading_order=1, block_type="REFERENCE_LIST",
               text="разработки. 1999. № 4. С. 40–48.", **common),
    ]
    pages = [PageX(page_index=i, page_kind="PDF_PAGE", width_pt=595, height_pt=842, page_status="NATIVE_OK",
                   primary_text_layer="PDF_TEXT_LAYER", native_char_count=100, native_text_status="PRESENT_OK")
             for i in (5, 6)]
    return SourceResult(source=src, status="COMPLETE", pages=pages, blocks=blocks)


def test_canon_rows_validate_and_ids_recompute_like_b04():
    run = ids.new_run_id(datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
    out = CanonMapper(_result(), run_id=run, config_hashes={"objects": "c" * 64}).build()
    rows = out.tables["bibliography_entries"]
    assert [r.entry_label for r in rows] == ["1", "2", "3"]
    assert out.counts["bibliography_entries"] == 3
    blocks = {b.object_id: b for b in out.tables["blocks"]}
    same: dict[tuple, int] = {}
    for r in rows:
        assert r.object_kind == "BIBLIOGRAPHY_ENTRY" and r.extractor_id == bib.EXTRACTOR_ID
        assert r.raw_config_hash == bib.CONFIG_HASH and r.review_status == "AUTO_EXTRACTED_UNREVIEWED"
        assert all(bid in blocks for bid in r.list_block_ids)
        assert r.page_id == blocks[r.list_block_ids[0]].page_id
        pk = ids.producer_key(r.extractor_id, r.extraction_generation, r.raw_config_hash, r.models)
        anchor = ids.bbox_anchor(r.bbox_x0, r.bbox_y0, r.bbox_x1, r.bbox_y1)
        key = (r.page_id, anchor)
        dup = same.get(key, 0)
        same[key] = dup + 1
        assert ids.object_id(r.page_id, "BIBLIOGRAPHY_ENTRY", r.origin, r.region_origin, anchor, pk, dup) == r.object_id
        assert ("DUPLICATE_DETECTION_DISAMBIGUATED" in r.quality_flags) == (dup > 0)
    third = rows[2]
    assert third.continues_on_page_id == f"{SID}:p0006" and "CROSS_PAGE_CONTINUATION" in third.quality_flags
    assert third.parsed_year == 1999 and third.parsed_pages == "40–48"
