"""Normalisation of raw GLM-OCR outputs and the CP-22 anomaly flags / stop window (agent C; synthetic outputs)."""
from __future__ import annotations

import pytest

from vkm_corpus.ocr import normalize as n
from vkm_corpus.ocr import quality as q


@pytest.mark.parametrize("raw,latex,label", [
    ("$$E = m c^{2}$$", "E = m c^{2}", None),
    ("\\[ a + b \\tag{3.12} \\]", "a + b", "(3.12)"),
    ("$$x_{1} = y \\quad (2.4)$$", "x_{1} = y", "(2.4)"),
    ("\\( \\frac{a}{b} \\)", "\\frac{a}{b}", None),
])
def test_formula(raw, latex, label):
    got = n.normalize_formula(raw)
    assert got["normalized_latex"] == latex and got["equation_label"] == label and got["latex_parse_ok"]


def test_formula_structure_check():
    assert n.normalize_formula("$$\\frac{a}{b$$")["latex_parse_ok"] is False
    assert n.normalize_formula("\\begin{array}{l} a \\end{array}")["latex_parse_ok"] is True
    assert n.normalize_formula("")["normalized_latex"] is None


def test_html_table_with_spans():
    html = ("<table><thead><tr><th rowspan='2'>A</th><th colspan='2'>B</th></tr><tr><th>b1</th><th>b2</th></tr>"
            "</thead><tr><td>1</td><td>2</td><td>3</td></tr></table>")
    t = n.normalize_table(html)
    assert (t["n_rows"], t["n_cols"], t["raw_format"]) == (3, 3, "HTML") and t["structure_ok"]
    a = next(c for c in t["cells"] if c["text"] == "A")
    assert a["row_span"] == 2 and a["is_header"]
    b1 = next(c for c in t["cells"] if c["text"] == "b1")
    assert (b1["row"], b1["col"]) == (1, 1)
    assert t["normalized_text"].splitlines()[-1] == "1 | 2 | 3"


def test_markdown_table_and_text():
    t = n.normalize_table("| a | b |\n|---|---|\n| 1 | 2 |")
    assert (t["n_rows"], t["n_cols"], t["raw_format"]) == (2, 2, "MARKDOWN")
    assert n.normalize_table("no table here")["cells"] == []
    assert n.text_from_markdown("## Заголовок\n**жирный** текст<br>строка") == "Заголовок\nжирный текст\nстрока"


def test_call_flags():
    assert q.call_flags("abc", "length", 0.2) == ["TRUNCATED"]
    assert q.call_flags("", "stop", 0.2) == ["EMPTY_ON_INK"]
    assert q.call_flags("", "stop", 0.0) == []
    loop = "повтор строки " * 200
    assert "REPETITION" in q.call_flags(loop, "stop", 0.2)
    assert q.call_flags("Обычный синтетический ответ модели без повторов.", "stop", 0.2) == []


def _html_table(rows: list[list[str]]) -> str:
    return "<table border=\"1\">" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + \
        "</table>"


def test_table_cut_at_cap_and_empty_row_loop():
    # rule v2: a loop of empty rows after the last printed row is dropped; an output cut at the token cap keeps its
    # complete rows instead of losing the whole grid
    rows = "".join(f"<tr><td>{i}</td><td>{i},5</td></tr>" for i in range(1, 6))
    loop = "<tr><td></td><td></td></tr>" * 40
    t = n.normalize_table("<table border=\"1\">" + rows + loop + "</table>")
    assert (t["n_rows"], t["n_cols"], t["structure_ok"]) == (5, 2, True)
    cut = n.normalize_table("<table border=\"1\">" + rows + loop + "<tr><td></td><td")
    assert (cut["n_rows"], cut["n_cols"], cut["raw_format"]) == (5, 2, "HTML")
    assert cut["normalized_text"].splitlines()[-1] == "5 | 5,5"
    spacer = n.normalize_table("<table><tr><td>a</td></tr><tr><td></td></tr><tr><td>b</td></tr></table>")
    assert spacer["n_rows"] == 3  # an empty row inside the table stays
    # an empty cell spanning into the dropped rows is clipped to the kept grid
    span = n.normalize_table("<table><tr><td>x</td><td rowspan=\"3\"></td></tr><tr><td></td></tr>"
                             "<tr><td></td></tr></table>")
    assert span["n_rows"] == 1 and all(c["row"] + c["row_span"] <= span["n_rows"] for c in span["cells"])


def test_repetition_ignores_table_markup():
    # rule v2: a real table with many (even empty) cells is not a loop – the markup n-grams do not count
    numeric = _html_table([[str(i), f"{i},{i % 7}", str(90 + i % 5), f"+{100 + 10 * i}", f"-{150 - 5 * i}",
                            str(5000 + 250 * i)] for i in range(1, 16)])
    schedule = _html_table([["Синтетическая операция " + str(i), f"{i},5", str(40 * i)] + [""] * 16
                            for i in range(1, 12)])
    toc = "\n".join(f"Синтетический раздел {i} {'.' * 60} {i * 7}" for i in range(1, 25))
    md = "| a | b | c |\n|---|---|---|\n" + "\n".join(f"| {i} | {i * 3} |  |" for i in range(40))
    for content in (numeric, schedule, toc, md):
        assert q.call_flags(content, "stop", 0.2) == [], content[:80]
    # a looping table stays a loop: the same cell text over and over
    looping = _html_table([[f"{i} синт/блок"] + ["3 синт/блок"] * 8 for i in range(60)])
    assert "REPETITION" in q.call_flags(looping, "stop", 0.2)
    assert q.recognised_text("<td>a</td><td>b</td> | x ....... y") == "a b x . y"


def test_stop_window():
    w = q.StopWindow(window=200)
    for _ in range(199):
        w.add([])
    w.check()  # window not full yet
    for _ in range(5):
        w.add(["TRUNCATED"])
    with pytest.raises(q.StopRun):
        w.check()
    w2 = q.StopWindow(window=200)
    for i in range(400):
        w2.add(["EMPTY_ON_INK"] if i % 200 == 0 else [])
    w2.check()  # 1/200 = 0.5 % ≤ 1 %
