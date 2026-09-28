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
    assert p.method == "bib_rules_v3/gost" and p.confidence == 1.0


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
        # rules v2 segmented this list the same way: the entries keep their v2 ids (raw_config_hash of v2)
        assert r.raw_config_hash == bib.CONFIG_HASH_V2 and r.config_hash == bib.CONFIG_HASH
        assert r.review_status == "AUTO_EXTRACTED_UNREVIEWED"
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


def test_bullets_and_numbers_without_a_dot():
    bullets = [blk(12, 1, "Литература", bt="HEADING"),
               blk(12, 2, " Орлов О.О., Лебедев Л.Л. Анализ оседаний // Маркшейдерия. 2012. № 1. С. 3–8."),
               blk(12, 3, " Лебедев Л.Л. Сдвижение толщи. – М.: Недра, 1990. – 200 с.")]
    entries, _ = bib.extract_entries(bullets)
    assert [e.parsed.authors for e in entries] == [["Орлов О.О", "Лебедев Л.Л"], ["Лебедев Л.Л"]]
    assert not entries[0].text.startswith("")
    numbers = [blk(13, 1, "1 Орлов О.О. Прогноз оседаний // Маркшейдерия. 2009. № 2. С. 5–9."),
               blk(13, 2, "2 Космические методы в геологии / под ред. А. В. Орлова. – СПб : Наука, 2000. – 316 с."),
               blk(13, 3, "3 Лебедев Л.Л. Сдвижение толщи. – М.: Недра, 1990. – 200 с.")]
    entries, _ = bib.extract_entries(numbers)
    assert [e.label for e in entries] == ["1", "2", "3"]
    assert [e.parsed.authors for e in entries] == [["Орлов О.О"], [], ["Лебедев Л.Л"]]
    assert entries[1].parsed.title == "Космические методы в геологии"


# ------------------------------------------------------------------------------------------------ rules v3: authors
def _authors(text):
    return bib.parse_entry(text).authors


def test_v3_extended_latin_letters_and_particles():
    assert _authors("Kılınçoğlu, H., & M. A. Ortíz. 1999. Joint use of wells. Water J. 3, 1–5.") == \
        ["Kılınçoğlu, H", "M. A. Ortíz"]
    assert _authors("Santos de Almeida AC (1996) Creep of potash openings. Rock Mech 3:1–5") == \
        ["Santos de Almeida AC"]
    assert _authors("van der Berg, J., de la Vega, V., 2001. Waves in porous media. Geophysics 50, 1–9.") == \
        ["van der Berg, J", "de la Vega, V"]
    assert _authors("vonKarst, A. R. (1954), Salt dielectrics, Nordic Press.") == ["vonKarst, A. R"]
    assert _authors("Де Ренн С. Кристаллы соли / С. Де Ренн, П. Лазур. – М.: Наука, 1971. – 450 с.") == \
        ["С. Де Ренн", "П. Лазур"]


def test_v3_tex_spacing_accents_are_joined_in_the_parse_view():
    p = bib.parse_entry("Be´ raud, S., and P. Mile` z (1992). Salt creep maps. In Rock Salt, Vol. 2.")
    assert p.authors == ["Béraud, S", "P. Milèz"]
    assert _authors("Sj¨ostrand, J., 2004. Borehole tests. Rock Mech. J. 12, 1–5.") == ["Sjöstrand, J"]
    assert _authors("Vavraˇcek, V., Svoboda, T., 2015. Salt anisotropy. Rock J. 8, 1–9.") == \
        ["Vavraček, V", "Svoboda, T"]
    assert _authors("Doblar´e, M., 1997. Finite elements. Salt J. 3, 1–5.") == ["Doblaré, M"]
    assert _authors("O´Dell, K., 2003, Radar targets. Radar J. 5, 11–17.") == ["O´Dell, K"]


def test_v3_transliterated_hyphenated_and_undotted_initials():
    assert _authors("Kornilov Yu. A., Zhdanov A. A. Mine safety model // Mining J. 2019. № 2. P. 5–9.") == \
        ["Kornilov Yu. A", "Zhdanov A. A"]
    assert _authors("Ban Y-M, Harlow JW (1990) Neutron tools. Log J 3:1–5") == ["Ban Y-M", "Harlow JW"]
    assert _authors("Lo, S-W., Harris, G.J. (2005) Brine flow. Fluids 6, 1–9.") == ["Lo, S-W", "Harris, G.J"]
    assert _authors("Нейман Дж. Прочность кристаллов. – М.: Мир, 1980. – 300 с.") == ["Нейман Дж"]
    assert _authors("Поликаров АИ. Минералы соли // Тр. ин-та. 1975. Вып. 5. С. 7–11.") == ["Поликаров АИ"]
    assert _authors("Ветров В.А, Горкин О.П. Соляные породы // Л.: Недра, 1970. - С. 3-9.") == \
        ["Ветров В.А", "Горкин О.П"]
    assert _authors("E. Baykal, Ş. Yıldız, M. Sofu. Karst risk // Karst J. 2018. Vol. 7.") == \
        ["E. Baykal", "Ş. Yıldız", "M. Sofu"]


def test_v3_all_caps_names_but_not_all_caps_titles():
    assert _authors("VAN EEKHOF H.A., HULST T. & URBAN J.L. 1984 Salt flow. Salt J 1, 1–9.") == \
        ["VAN EEKHOF H.A", "HULST T", "URBAN J.L"]
    assert _authors("SANTOS DE LIMA, A.J. 2002. Salt pillars. Rock J 4, 1–9.") == \
        ["SANTOS DE LIMA, A.J"]
    assert _authors("SALTPROJ II. KRAUSE, K.-P., STEIN, D., JUNG, M, HAAS, U. 2007. Title of report.") == \
        ["KRAUSE, K.-P", "STEIN, D", "JUNG, M", "HAAS, U"]
    assert _authors("ЖУКОВ И.И., ПОПОВ П.П. Сдвижение толщи // Горный журнал. 1990. № 3. С. 5–9.") == \
        ["ЖУКОВ И.И", "ПОПОВ П.П"]
    assert _authors("PROCEEDINGS OF THE FIRST CONFERENCE 1990. Title of the volume. Publisher.") == []


def test_v3_homoglyphs_of_other_scripts_and_ocr_letters():
    # Cyrillic М and Greek Ε among Latin names; Greek Ε among Cyrillic ones; OCR «J1.» and «3.» in Cyrillic text
    cyr_em, greek_epsilon = chr(0x041C), chr(0x0395)
    assert _authors(f"Gorbin {cyr_em}. E., Stenberg E., Salt waves, Archiv 3 (1970), 1–9.") == \
        ["Gorbin M. E", "Stenberg E"]
    assert _authors(f"Зубов {greek_epsilon}. Н., Малышев Н. И., Ползучесть соли, Изв. вузов, № 3, 1977.") == \
        ["Зубов Е. Н", "Малышев Н. И"]
    assert _authors("55001. Захаров, J1. И. и Хоменко, И. М. Соли калия. Химия, 1971, № 3, с. 5.") == \
        ["Захаров, Л. И", "Хоменко, И. М"]
    assert _authors("55002. Гайдамак, 3. И. Калийные удобрения. Агрохимия, 1972, № 2, с. 38.") == ["Гайдамак, З. И"]
    # a Latin look-alike initial next to a Cyrillic surname stays Cyrillic after a Latin word
    assert _authors("Обзор 1999 года (M=3,1) / A. А. Петров, Д. А. Сидоров // Сейсмология. 2000. С. 1–5.") == \
        ["А. А. Петров", "Д. А. Сидоров"]


def test_v3_letter_spaced_and_broken_surnames():
    assert _authors("55003. М о р о з о в , Н . А . Калийные рудники. Горное дело, 1970, № 2, с. 12.") == \
        ["Морозов, Н. А"]
    assert _authors("55004. С аблин, А. С. и Орлов, В. А. Проходка стволов. Горное дело, 1971, с. 20.") == \
        ["Саблин, А. С", "Орлов, В. А"]
    assert _authors("71. Ра дуг ин Ю. Н., Ку л и к о в а С. Т., Соль, «Недра», 1981.") == \
        ["Радугин Ю. Н", "Куликова С. Т"]
    # one-letter words and short words are not glued to a title
    assert _authors("5. К вопросу об А. С. Иванове // Вестник. 1990. № 2. С. 3–5.") == []
    assert "Katoand" not in " ".join(_authors("133. Kato and L. W., Lee E. H., Salt tests, Trans. 4, 1975."))


def test_v3_corporate_authors_in_the_author_year_slot():
    assert _authors("US Geological Survey. 2008. Groundwater atlas of the basin. Reston, VA.") == \
        ["US Geological Survey"]
    assert _authors("IFG 2017a. Salzmechanik. Bericht, Leipzig.") == ["IFG"]
    assert _authors("15. Acme Consulting (2011) Well charts. Acme, Houston") == ["Acme Consulting"]
    assert _authors("Organisation for Economic Co-operation and Development. 2019. Water outlook. Paris.") == \
        ["Organisation for Economic Co-operation and Development"]
    assert _authors("BUNDESAMT FÜR STRAHLENSCHUTZ (BFS) 2014. Endlagerbericht. Berlin.") == \
        ["BUNDESAMT FÜR STRAHLENSCHUTZ (BFS)"]
    p = bib.parse_entry("US Geological Survey. 2008. Groundwater atlas of the basin. Reston, VA.")
    assert p.year == 2008 and p.title == "Groundwater atlas of the basin"


def test_v3_full_names_statement_of_responsibility_and_editors():
    assert _authors("18. Hao Lin, Wei Zhang, Jun Liu. Salt cavern model // Energy J. 2016. V. 5. P. 1–11.") == \
        ["Hao Lin", "Wei Zhang", "Jun Liu"]
    assert _authors("13. Brine inflow / Mei Wang, Tao Zhang // Mine Water. 2012. Vol. 3. P. 1–9.") == \
        ["Mei Wang", "Tao Zhang"]
    # a venue is not a two-word surname
    assert _authors("International Symposium, T. J. Hale, ed. Society of Engineers, New York, pp. 21 25.") == []
    # editors, compilers and translators are not authors
    assert _authors("2. Геомеханика соли / отв. ред. И.И. Иванов. – Пермь, 2005. – 200 с.") == []
    assert _authors("4. Rock salt tests / Ed. K.R. Sen, V.M. Rao. – 2009. – 120 p.") == []
    assert _authors("5. Магниторазведка: справочник / под ред. Е.А. Мудрова. – М.: Недра, 1985. – 300 с.") == []


def test_v3_authors_listed_after_the_description_and_prefixes():
    assert _authors("54001. Соли калия. Химия, т. 2, 1970, с. 10—15. — Авт.: И. И. Иванов, "
                    "П. П. Петров [и др.]") == ["И. И. Иванов", "П. П. Петров"]
    assert _authors("7. Атлас соляных пород / / Л.: Недра, 1980. -120 с. (авт. Ярцев Я.Я., Попов А.Л. и др.).") \
        == ["Ярцев Я.Я", "Попов А.Л"]
    assert _authors("Для цитирования: Иванов И.И. Трещины в соли // Горный журнал. 2021. № 3. С. 5–9.") == \
        ["Иванов И.И"]
    assert _authors("16. См. Bellows R., Kalin R., Salt domes, Geo J. 12 (1980), 1–9.") == \
        ["Bellows R", "Kalin R"]
    assert _authors("[2] – Петров П.А. Соляной карст // Вестник. 2008. Т. 2. С. 10-15") == ["Петров П.А"]


def test_v3_index_given_names_and_text_layers_without_spaces():
    assert _authors("56001. Иванов, Владимир. Рудник у реки. Очерк. Урал, 1970, № 4, с. 20.") == \
        ["Иванов, Владимир"]
    assert _authors("3. SmithB,JonesT,Brown S (2003) Salt logging. Log J 4:1–9") == \
        ["Smith B", "Jones T", "Brown S"]


# ------------------------------------------------------------------------------------------------ rules v3: segmentation
def test_v3_blocks_holding_a_tail_and_the_next_start():
    # the layout cut every block after a finished-looking line: the venue of one entry + the start of the next
    blocks = [
        blk(40, 1, "References", bt="HEADING"),
        blk(40, 2, "Abel, A., 2001. Waves in rocks."),
        blk(40, 3, "Geophysics 64, 10–20.\nBaker, B., 2002. Creep of salt."),
        blk(40, 4, "J. Salt 3, 30–40.\nCole, C., Dunn, D., 2003. Damage of salt."),
        blk(40, 5, "Rock Mech. 7, 1–9.\nEvans, E., 2004. Poroelastic waves."),
        blk(40, 6, "Geophys. J. 5, 5–15.\nFox, F., 2005. Salt domes. Tectonics 2, 1–3."),
    ]
    entries, _ = bib.extract_entries(blocks)
    texts = [e.normalized_text for e in entries]
    assert texts == ["Abel, A., 2001. Waves in rocks. Geophysics 64, 10–20.",
                     "Baker, B., 2002. Creep of salt. J. Salt 3, 30–40.",
                     "Cole, C., Dunn, D., 2003. Damage of salt. Rock Mech. 7, 1–9.",
                     "Evans, E., 2004. Poroelastic waves. Geophys. J. 5, 5–15.",
                     "Fox, F., 2005. Salt domes. Tectonics 2, 1–3."]
    assert [e.parsed.authors for e in entries][2] == ["Cole, C", "Dunn, D"]
    # rules v2 opened an entry at each venue line and glued it to the next entry
    v2, _ = bib.extract_entries(blocks, bib.SEG_V2)
    assert v2[1].normalized_text.startswith("Geophysics 64, 10–20. Baker")


def test_v3_line_led_list_opens_entries_at_names_on_the_column_edge():
    blocks = [
        blk(41, 1, "References", bt="HEADING"),
        blk(41, 2, "Abel, A., 2001. Waves in rocks. Geophysics 64,"),
        blk(41, 3, "10–20.\nBaker, B., 2002. Creep of salt. J. Salt 3,"),
        blk(41, 4, "30–40.\nCole, C., 2003. Damage of salt. Rock Mech. 7,"),
        blk(41, 5, "1–9.\nDunn, D. 2010. Salt tables and trans-"),
        blk(41, 6, "Evans, E. 2011. Brine flow in front of con-"),
    ]
    texts = [e.normalized_text for e in bib.extract_entries(blocks)[0]]
    assert texts[-2:] == ["Dunn, D. 2010. Salt tables and trans-", "Evans, E. 2011. Brine flow in front of con-"]


def test_v3_inline_splits_extended_names_springer_particles_and_agencies():
    blocks = [
        blk(42, 1, "References", bt="HEADING"),
        blk(42, 2, "Abel, A. 1990. Title one. Soil J. 12:10–17. Kılınçoğlu, H., & M. A. Ortíz. 1999. Title two. "
                   "Water J. 3, 1–5."),
        blk(42, 3, "Baker B (2020) Title three. Salt J 4. https://doi.org/10.1000/xyz123 "
                   "Santos de Almeida AC (1996) Title four. Rock Mech 3:1–5"),
        blk(42, 4, "Cole, C. 1991. Title five. J. Hydrol. 5, 12–17. US Geological Survey. 2008. Groundwater atlas. "
                   "Reston."),
        blk(42, 5, "Dunn, D. 2001. Title six. Chichester: Wiley. Evans, E. W. (1994) Title seven. Frost J. 5, "
                   "11-18."),
    ]
    heads = [e.normalized_text.split(".")[0] for e in bib.extract_entries(blocks)[0]]
    assert heads == ["Abel, A", "Kılınçoğlu, H", "Baker B (2020) Title three", "Santos de Almeida AC (1996) Title four",
                     "Cole, C", "US Geological Survey", "Dunn, D", "Evans, E"]


def test_v3_author_year_head_after_an_unfinished_line_but_not_inside_an_author_list():
    blocks = [
        blk(43, 1, "References", bt="HEADING"),
        blk(43, 2, "Abel, A., Baker, B., 2005a. Salt creep laws:\n"
                   "Abel, A., 2003b. Salt domes. Tectonics 4, 11–17."),
        blk(43, 3, "Cole, C., Dunn, D., Evans, E., Fox, F.,\nGray, G., 2001. Radar sounding. Remote Sens. 3, 5–9."),
        blk(43, 4, "Hill, H., 2002. Salt creep. Rock Mech. 1, 1–5."),
    ]
    texts = [e.normalized_text for e in bib.extract_entries(blocks)[0]]
    assert texts[:2] == ["Abel, A., Baker, B., 2005a. Salt creep laws:",
                         "Abel, A., 2003b. Salt domes. Tectonics 4, 11–17."]
    assert texts[2].startswith("Cole, C.") and "Gray, G., 2001" in texts[2]


def test_v3_next_expected_number_inside_a_line():
    blocks = [blk(44, 1, "Литература", bt="HEADING"),
              blk(44, 2, "3. Доклад о недрах / Минприроды. – М., 2012. – 4. Иванов И.И. Соль и газ // Вестник. "
                         "2010. № 2. С. 5–9."),
              blk(44, 3, "5. Петров П.П. Соль. – М.: Недра, 1990. – Т. 6. Вып. 2."),
              blk(44, 4, "6. Сидоров С.С. Сдвижение // Маркшейдерия. 2001. № 1. С. 3–8.")]
    entries, _ = bib.extract_entries(blocks)
    assert [e.label for e in entries] == ["3", "4", "5", "6"]
    assert entries[2].normalized_text.endswith("Т. 6. Вып. 2."), "«Т. 6.» is not the next number"
    index = [blk(45, 1, "55458. Миронов, М. М. Калийные удобрения. Агрохимия, 1972, № 3, с. 49— 50. 55459. Нестеров, "
                        "Л. Н. Соли натрия. Агрохимия, 1972, № 3, с. 51."),
             blk(45, 2, "55460. Орлов, О. О. Гипс. Агрохимия, 1972, № 3, с. 52.")]
    assert [e.label for e in bib.extract_entries(index)[0]] == ["55458", "55459", "55460"]


def test_v3_corporate_and_normative_entries_open_entries_in_author_led_lists():
    blocks = [blk(46, 1, "References", bt="HEADING"),
              blk(46, 2, "Abel, A. 1990. Title one. J. Hydrol. 5, 12–17."),
              blk(46, 3, "Baker, B. 1991. Title two. J. Hydrol. 6, 1–7."),
              blk(46, 4, "US Environmental Protection Agency. 2011. Salt water guidance. Denver, CO."),
              blk(46, 5, "Cole, C. 1992. Title three. J. Hydrol. 7, 2–9.")]
    texts = [e.normalized_text for e in bib.extract_entries(blocks)[0]]
    assert len(texts) == 4 and texts[2].startswith("US Environmental Protection Agency. 2011.")
    assert bib.extract_entries(blocks)[0][2].parsed.authors == ["US Environmental Protection Agency"]
    ru = [blk(47, 1, "Литература", bt="HEADING"),
          blk(47, 2, "Иванов И.И. Сдвижение толщи. – М.: Недра, 1985. – 210 с."),
          blk(47, 3, "Петров П.П. Оседания. – Л.: ВНИМИ, 1986. – 100 с."),
          blk(47, 4, "Указания по охране шахт. – Пермь, 2010. – 50 с."),
          blk(47, 5, "Сидоров С.С. Соль. – Пермь, 1990. – 50 с.")]
    assert len(bib.extract_entries(ru)[0]) == 4


def test_v3_patterns_do_not_backtrack_catastrophically():
    import time

    names = ", ".join(f"Surname{c} AB" for c in "abcdefghijklmnopqrstuvwxyzabcdefghijklmn") + " Title"
    caps = " ".join(["WORD"] * 60) + " 1990x"
    t0 = time.perf_counter()
    for s in (names, caps, "A" + "b" * 3000 + " CD", "e´ " * 500):
        bib._split_inline(s)
        bib.parse_entry(s)
        bib.ENTITY_START.match(s)
    assert time.perf_counter() - t0 < 2.0


# ------------------------------------------------------------------------------------------------ rules v3: stable ids
def test_v2_configuration_hash_is_frozen():
    assert bib.CONFIG_HASH_V2 == "3dbcafd43813bbb392cfa3e1bde9e35b5ae1cfe06c59d6a57a86697efc449a0c"
    assert bib.CONFIG_HASH != bib.CONFIG_HASH_V2 and bib.SEGMENTATIONS[-1] is bib.SEG_V3


def test_raw_config_hashes_keep_v2_hash_only_where_the_region_segments_as_in_v2():
    blocks = [
        blk(48, 1, "References", bt="HEADING"),
        blk(48, 2, "Abel, A., 2001. Waves in rocks. Geophysics 64, 10–20."),
        blk(48, 3, "Baker, B., 2002. Creep of salt. J. Salt 3, 30–40. Kılınçoğlu, H., 2003. Salt karst. Karst 1, 1–9."),
        blk(48, 4, "Cole, C., 2004. Salt domes. Tectonics 2, 1–3."),
    ]
    entries, _ = bib.extract_entries(blocks)
    assert [e.normalized_text[:10] for e in entries] == ["Abel, A., ", "Baker, B.,", "Kılınçoğlu", "Cole, C., "]
    v2, _ = bib.extract_entries(blocks, bib.SEG_V2)
    assert len(v2) == 3, "rules v2 kept the extended-Latin name inside the previous entry"
    hashes = bib.raw_config_hashes(entries, blocks, lambda b, o, t: b["object_id"])
    assert hashes == [bib.CONFIG_HASH_V2, bib.CONFIG_HASH, bib.CONFIG_HASH, bib.CONFIG_HASH_V2]


def test_canon_ids_stay_stable_for_unchanged_entries_and_change_for_changed_regions():
    run = ids.new_run_id(datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
    res = _result()
    rows = CanonMapper(res, run_id=run, config_hashes={"objects": "c" * 64}).build().tables["bibliography_entries"]
    # the ids of rules v2: the same anchors with the v2 configuration hash in the producer key
    alloc = ids.ObjectIdAllocator()
    for r in rows:
        pk = ids.producer_key(r.extractor_id, r.extraction_generation, bib.CONFIG_HASH_V2, r.models)
        anchor = ids.bbox_anchor(r.bbox_x0, r.bbox_y0, r.bbox_x1, r.bbox_y1)
        assert alloc.allocate(r.page_id, "BIBLIOGRAPHY_ENTRY", r.origin, r.region_origin, anchor, pk)[0] == r.object_id
