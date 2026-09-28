"""DjVu (agent C): S-expression parsing, coordinates, IFF walk vs djvused on a synthetic bundled DjVu made with
cjb2/djvm/djvused (skipped when DjVuLibre is not installed — NOT_RUN)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from vkm_corpus.extract import djvu


def test_sexpr_parser_unescapes_utf8_octal():
    data = b'(page 0 0 100 200 (line 10 20 90 40 (word 10 20 50 40 "\\320\\220\\320\\221") (word 55 20 90 40 "x\\"y")))'
    expr = djvu.parse_sexpr(data)
    z = djvu._zone(expr[0])
    assert z.kind == "page" and z.children[0].kind == "line"
    assert djvu.zone_text(z) == 'АБ x"y'


def test_px_to_pt_flips_y():
    # 300 dpi page 3000 px high: a box at the bottom (y 0..300 px) is at the bottom in PAGE_PT_TL
    x0, y0, x1, y1 = djvu.px_to_pt((0, 0, 300, 300), 3000, 300)
    assert (x0, x1) == (0.0, 72.0) and y0 == pytest.approx(648.0) and y1 == pytest.approx(720.0)


def test_page_lines_carry_para_and_region():
    data = (b'(page 0 0 1000 1000 (region 0 0 1000 1000 (para 0 500 1000 1000 (line 0 900 500 950 '
            b'(word 0 900 200 950 "one") (word 210 900 500 950 "two")) (line 0 800 500 850 (word 0 800 500 850 "three'
            b'")))))')
    z = djvu._zone(djvu.parse_sexpr(data)[0])
    lines = djvu.page_lines(z, 1000, 100)
    assert [ln["text"] for ln in lines] == ["one two", "three"]
    assert lines[0]["para"] == 1 and lines[0]["region"] == 1
    assert lines[0]["bbox"][1] < lines[1]["bbox"][1]


needs_djvulibre = pytest.mark.skipif(not all(shutil.which(t) for t in ("cjb2", "djvm", "djvused", "djvudump")),
                                     reason="DjVuLibre not installed (NOT_RUN)")


@needs_djvulibre
def test_synthetic_bundled_djvu_two_counters_and_text_layer(tmp_path):
    from PIL import Image, ImageDraw

    pages = []
    for i in range(3):
        img = Image.new("1", (600, 800), 1)
        ImageDraw.Draw(img).rectangle([50 + i * 10, 50, 300, 200], fill=0)
        pbm = tmp_path / f"p{i}.pbm"
        img.save(pbm)
        out = tmp_path / f"p{i}.djvu"
        subprocess.run(["cjb2", "-dpi", "300", str(pbm), str(out)], check=True, capture_output=True)
        pages.append(str(out))
    bundle = tmp_path / "doc.djvu"
    subprocess.run(["djvm", "-c", str(bundle), *pages], check=True, capture_output=True)
    txt = tmp_path / "t.dsed"
    txt.write_text('(page 0 0 600 800 (line 50 600 300 750 (word 50 600 300 750 "синтетика")))', encoding="utf-8")
    subprocess.run(["djvused", str(bundle), "-e", f"select 2; set-txt {txt}; save"], check=True, capture_output=True)
    struct = djvu.iff_structure(bundle)
    assert len(struct.pages) == 3 and struct.bundled is True
    assert djvu.page_count_cli(bundle) == 3
    assert struct.pages[1].has_text and not struct.pages[0].has_text
    layers = djvu.text_layer_all(bundle)
    assert set(layers) == {2}
    lines = djvu.page_lines(layers[2], 800, 300)
    assert lines[0]["text"] == "синтетика"
    from vkm_corpus.extract.pages import paginate_djvu

    p = paginate_djvu(bundle)
    assert p.count == p.check_count == 3 and p.agree
