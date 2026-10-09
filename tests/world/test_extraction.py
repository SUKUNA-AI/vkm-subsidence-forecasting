"""Page-level extraction for the world passport (09.10.2026): verifier of numbers / units / quotes, the strict answer
schema, packets, page-reference parsing of the selection, the runner's command and limit parsing, run comparison.
No network, no codex."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from vkm_world.extraction import compare as C
from vkm_world.extraction import packets as P
from vkm_world.extraction import schema as S
from vkm_world.extraction import verify as V

ROOT = Path(__file__).resolve().parents[2]


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PAGE = ("Модуль упругости сильвинита составил 10,8 ГПа (n = 12), а λ = 0,60–0,71.\n"
        "Параметр δ = 5·10-4 с-1; глуби-\nна 1 500 м. Соляно-\nмергельная толща.\n"
        "[ТАБЛИЦА: Табл. 2.1]\nСильвинит красный | 26,06 | 1,44")


@pytest.fixture()
def pages():
    return {"S:p0001": V.PageText("S:p0001", PAGE)}


def rec(**kw):
    r = {"page_id": "S:p0001", "quote": "Модуль упругости сильвинита составил 10,8 ГПа", "value_as_printed": "10,8",
         "value_min": 10.8, "value_max": 10.8, "unit_as_printed": "ГПа"}
    r.update(kw)
    return r


@pytest.mark.parametrize("kw", [
    {},
    {"value_as_printed": "0,60–0,71", "value_min": 0.6, "value_max": 0.71, "unit_as_printed": None,
     "quote": "λ = 0,60—0,71"},                                           # another dash in the quote
    {"value_as_printed": "5·10-4", "value_min": 0.0005, "value_max": 0.0005, "unit_as_printed": "с-1",
     "quote": "δ = 5·10-4 с-1; глубина 1 500 м"},                       # power of ten; hyphenation joined
    {"value_as_printed": "1 500", "value_min": 1500, "value_max": 1500, "unit_as_printed": "м",
     "quote": "глубина  1 500 м"},
    {"value_as_printed": "26,06", "value_min": 26.06, "value_max": 26.06, "unit_as_printed": None,
     "quote": "Сильвинит красный | 26,06 | 1,44"},                       # a table row
    {"value_as_printed": None, "value_min": None, "value_max": None, "unit_as_printed": None,
     "quote": "Соляно-мергельная толща"},                                 # a compound word broken at the line end
    {"quote": "модуль упругости ... 10,8 ГПа"},                           # an ellipsis splits the quote
])
def test_verifier_accepts_printed_values(pages, kw):
    assert V.check_record(rec(**kw), pages) == []


@pytest.mark.parametrize("kw,reason", [
    ({"value_as_printed": "10,9", "value_min": 10.9, "value_max": 10.9}, "NUMBER_NOT_ON_PAGE"),
    ({"unit_as_printed": "МПа"}, "UNIT_NOT_ON_PAGE"),
    ({"quote": "модуль упругости сильвинита равен 10,8"}, "QUOTE_NOT_ON_PAGE"),
    ({"quote": ""}, "QUOTE_EMPTY"),
    ({"value_min": 10.8e9}, "VALUE_MIN_NOT_PRINTED"),                     # SI conversion is not the producer's job
    ({"value_as_printed": None}, "VALUE_MIN_WITHOUT_PRINTED_VALUE"),
])
def test_verifier_rejects_with_reason(pages, kw, reason):
    assert any(r.startswith(reason) for r in V.check_record(rec(**kw), pages))


def test_page_outside_packet_is_rejected(pages):
    assert V.check_record(rec(page_id="S:p0002"), pages) == ["PAGE_NOT_IN_PACKET"]


def test_candidate_values_and_warnings():
    assert 0.0005 in V.candidate_values("5\\cdot 10^{-4}")
    assert 0.0005 in V.candidate_values("5e-4")
    assert 0.051 in V.candidate_values("5.1 10-2")                       # power printed without a sign
    assert -143.0 in V.candidate_values("гор. –143 м") and -143.0 not in V.candidate_values("100–143")
    assert -2.5 in V.candidate_values("-2,5") and 0.6 in V.candidate_values("0,60-0,71")
    assert V.printed_numbers("0,60–0,71") == ["0,60", "0,71"]
    assert V.printed_numbers("8,5…9,0 МПа") == ["8,5", "9,0"]
    assert "HEADER_MULTIPLIER" in V.warnings_for({"multiplier_as_printed": "·10⁻³"})
    assert "VALUE_NOT_IN_QUOTE" in V.warnings_for({"value_as_printed": "12", "quote": "n равно двенадцати"})


def _walk(node):
    yield node
    for v in node.get("properties", {}).values():
        yield from _walk(v)
    if isinstance(node.get("items"), dict):
        yield from _walk(node["items"])


def test_schema_is_strict_mode_compatible():
    for node in _walk(S.ANSWER):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert sorted(node["required"]) == sorted(node["properties"])
    rec_props = S.RECORD["properties"]
    assert rec_props["kind"]["enum"] == list(S.KINDS)
    assert "UNKNOWN" in rec_props["scale"]["enum"] and "SKRU1" in rec_props["site_norm"]["enum"]
    assert S.sha256(S.schema_bytes()) == S.sha256(S.schema_bytes())          # deterministic bytes


def test_task_text_states_the_rules():
    t = S.TASK_RU.casefold()
    for must in ("не запускай команды", "данные, а не инструкции", "value_as_printed", "контекст", "unknown",
                 "multiplier_as_printed", "cited"):
        assert must in t


def _rows(src, idxs, tier="A"):
    return [{"page_id": f"{src}:p{i:04d}", "source_id": src, "page_index": str(i), "tier": tier} for i in idxs]


def test_packets_balanced_chunks_and_mixed_pool():
    rows = _rows("VKM-SRC-001", range(1, 11)) + _rows("VKM-SRC-002", [3, 9]) + _rows("VKM-SRC-003", [5], "B")
    texts = {r["page_id"]: "x" * 100 for r in rows}
    groups = P.group_pages(rows, texts)
    sizes = sorted(len(g) for g in groups)
    assert sizes == [3, 5, 5]                                   # 10 pages → 5 + 5; two small sources pooled
    mixed = [g for g in groups if len({r["source_id"] for r in g}) > 1][0]
    assert P.packet_id(mixed, 1).startswith("MIX-001_")
    assert P.packet_id(groups[0]) == "VKM-SRC-001_p0001-0005"


def test_packets_split_dense_tables_by_number_count():
    rows = _rows("VKM-SRC-005", range(1, 7))
    texts = {r["page_id"]: " ".join(["12,5"] * 150) for r in rows}           # 150 numbers per page
    groups = P.group_pages(rows, texts)
    assert all(sum(P.n_numbers(texts[r["page_id"]]) for r in g) <= P.MAX_NUMBERS for g in groups)
    assert sum(len(g) for g in groups) == 6


def test_packet_render_marks_context_and_keeps_page_text():
    pk = P.Packet("X", "A", ["VKM-SRC-001:p0002"], {"VKM-SRC-001:p0002": "текст страницы"},
                  {"VKM-SRC-001:p0001": ["1.2 Заголовок раздела"]}, {"VKM-SRC-001": "Автор, «Книга» (2000)"})
    body = pk.render()
    assert body.index("КОНТЕКСТ (не извлекать)") < body.index("=== СТРАНИЦА VKM-SRC-001:p0002 ===")
    assert "текст страницы" in body and pk.manifest()["n_pages"] == 1
    assert P.context_lines("Обычный текст\n2.3 Свойства пород массива\nРис. 4.8 Оседания", []) == [
        "2.3 Свойства пород массива", "Рис. 4.8 Оседания"]
    assert P._page_key("VKM-SRC-023:r0003") == ("VKM-SRC-023", 3)


def test_page_reference_parsing():
    sel = _script("extract_select_pages")
    assert sel.parse_page_refs("pdf p.89 (printed 89); §4.2.2", "VKM-SRC-003") == [("VKM-SRC-003", 89, 89)]
    assert sel.parse_page_refs("VKM-SRC-025 p.157-163; VKM-SRC-037 pp.89-90, 97") == [
        ("VKM-SRC-025", 157, 163), ("VKM-SRC-037", 89, 90)]
    assert sel.parse_page_refs("p.13 (printed 24-25) Рис. 1.13", "VKM-SRC-037") == [("VKM-SRC-037", 13, 13)]
    assert sel.parse_page_refs("see v1 evidence ids", "VKM-SRC-025") == []


def test_runner_command_and_limit_wait(tmp_path):
    run = _script("extract_run_codex")
    cmd = run.command(Path("codex.exe"), "high", tmp_path / "s.json", tmp_path / "a.json", tmp_path)
    assert cmd[cmd.index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in cmd and "--ignore-user-config" in cmd and "--ephemeral" in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only" and cmd[-1] == "-"
    assert {"shell_tool", "memories", "multi_agent"} <= {cmd[i + 1] for i, x in enumerate(cmd) if x == "--disable"}
    assert run.wait_seconds("You've hit your usage limit. Try again in 2 hours 13 minutes.") == 2 * 3600 + 13 * 60 + 60
    assert run.wait_seconds("limit reached, try again in 45m") == 45 * 60 + 60
    assert run.wait_seconds("usage limit") == run.DEFAULT_LIMIT_WAIT_S
    assert run.LIMIT_RX.search("Error: 429 Too Many Requests")
    ev = '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":3}}\n{"type":"error","message":"x"}'
    assert run.usage_of(ev) == {"input_tokens": 10, "output_tokens": 3} and len(run.errors_of(ev)) == 1


def test_compare_matches_numbers_and_reports_attribution():
    a = [{"page_id": "p1", "kind": "PARAMETER", "value_min": 10.8, "value_max": 10.8, "unit_as_printed": "ГПа",
          "parameter_code": "E", "scale": "LAB", "site_norm": "SKRU1", "material_as_printed": "сильвинит красный"},
         {"page_id": "p1", "kind": "ENTITY", "value_min": None, "value_max": None, "entity_name": "Блок 201"}]
    b = [{"page_id": "p1", "kind": "PARAMETER", "value_min": 10.8, "value_max": 10.8, "unit_as_printed": "ГПа",
          "parameter_code": "E", "scale": "MASSIF", "site_norm": "SKRU1", "material_as_printed": "Сильвинит"},
         {"page_id": "p1", "kind": "ENTITY", "value_min": None, "value_max": None, "entity_name": "блок 201"},
         {"page_id": "p1", "kind": "PARAMETER", "value_min": 3.0, "value_max": 3.0, "unit_as_printed": "м"}]
    pairs, only_a, only_b = C.match(a, b)
    assert len(pairs) == 2 and not only_a and len(only_b) == 1
    assert C.attribution_diff(*pairs[0]) == ["scale"]
    s = C.agreement(a, b)
    assert s["number_recall_b_vs_a"] == 1.0 and s["number_precision_b_vs_a"] == 0.5
    assert s["attribution_disagreements"] == {"scale": 1}
    soft = C.attribution_diff({"site_norm": "UNKNOWN", "scale": "UNKNOWN", "parameter_code": "OTHER"},
                              {"site_norm": "VKM_UNSPECIFIED", "scale": "LAB", "parameter_code": "E"})
    assert soft == ["parameter_code_one_side", "scale_one_side", "site_norm_soft"]


@pytest.mark.parametrize("name", ["extract_select_pages", "extract_build_packets", "extract_run_codex", "extract_accept",
                                  "extract_export_private", "build_world_passport", "build_world_observations"])
def test_pipeline_scripts_import(name):
    """Every script of the extraction / passport pipeline at least compiles and imports (no network, no codex)."""
    assert callable(_script(name).main)
