"""QUERY_TYPES_V1 (experiment E10 of benchmarks/topic_v1/PREREGISTRATION_EXPERIMENTS_2026-10-13.md): the automatic
truth rules (labels, caption words, borehole locators and page basis, author names), the frozen set and the type
metrics. No network."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import topic_bench as TB

ROOT = Path(__file__).resolve().parents[2]
QT = ROOT / "benchmarks" / "query_types_v1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def core():
    return _load("qt_truth_core", QT / "scripts" / "truth_core.py")


@pytest.fixture(scope="module")
def build():
    return _load("qt_build_set", QT / "scripts" / "build_set.py")


def test_labels_and_caption_words(core):
    assert core.label_number("рис. 3.1") == ("рис", "3.1")
    assert core.label_number("Табл. 2,6") == ("табл", "2.6")
    assert core.label_number("table 9") == ("табл", "9")
    assert core.label_number("рис. 3.1 а") is None
    assert core.content_words("Рис. 3.4. Электрооборудование очистного комбайна и схема") == \
        ["электрооборудование", "очистного"]
    assert core.content_words("Рис. 1. Схема") == []
    assert core.stem("электрооборудование") == "электр"


def test_borehole_number_regex(core):
    rx = core.number_regex("1036")
    assert rx.search("скв. 1 036 и") and rx.search("скв.1036,") and not rx.search("21036") and not rx.search("1036.5")
    rx = core.number_regex("139")
    assert rx.search("скважины 139 и 140") and not rx.search("1390") and not rx.search("2.139")


def test_surname_first(core):
    assert core.surname_first("В.А. Асанов") == "Асанов В.А."
    assert core.surname_first("Барях А.А.") == "Барях А.А."
    assert core.surname_first("Lux K.-H.") == "Lux K.-H."


def test_locators_and_page_basis(build):
    pdf, canon = build.locator_pages("012:p28:Рис. 1.1 (plan) | 012:p65:Табл. 3.1 / Рис. 3.2 | "
                                     "разбор страниц: 012:p0070, 270:r0005")
    assert pdf == [("VKM-SRC-012", 28), ("VKM-SRC-012", 65)]
    assert canon == ["VKM-SRC-012:p0070", "VKM-SRC-270:r0005"]
    found = {"VKM-SRC-023:r0028": True, "VKM-SRC-012:p0028": False}
    assert build.resolve(found, "VKM-SRC-023", 28) == ("VKM-SRC-023:r0028", True)
    assert build.resolve(found, "VKM-SRC-012", 28) == ("VKM-SRC-012:p0028", False)
    assert build.resolve(found, "VKM-SRC-012", 0) == (None, None)
    rows = [{"borehole_id": "BH-1", "alternate_ids": "1 036 | 1036", "locators": "012:p65:Табл. 3.1 | 012:p67:Рис. 3.3",
             "scope": "SKRU1", "n_mentions": "3"}]
    f = {"1036": {"VKM-SRC-012:p0065": True, "VKM-SRC-012:p0067": False, "VKM-SRC-012:p0063": False,
                  "VKM-SRC-012:p0064": False, "VKM-SRC-012:p0066": False, "VKM-SRC-012:p0068": False,
                  "VKM-SRC-012:p0069": False}}
    basis = build.page_basis(rows, ["1036"], f)
    assert basis["adopted_offset"] == 0 and basis["pdf_locator_offsets"]["0"]["share"] == 0.5
    assert basis["pdf_locator_offsets"]["-2"]["share"] == 0.5          # tie with −2 (p67 − 2 = p65): identity kept
    bt = build.borehole_truth(rows, "1036", 0, f["1036"])
    assert bt["pages"] == ["VKM-SRC-012:p0065", "VKM-SRC-012:p0067"] and bt["number_in_text"] == 1
    assert build.numbers_of(rows[0]) == ["1036", "1036"]


def test_frozen_set_matches_receipt():
    receipt = json.loads((QT / "truth_receipt_v1.json").read_text(encoding="utf-8"))
    data = (QT / "query_types_v1.jsonl").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == receipt["set_sha256"]
    qs = [json.loads(x) for x in data.decode("utf-8").splitlines() if x.strip()]
    assert len(qs) == 70 and len({q["query_id"] for q in qs}) == 70
    assert receipt["types"] == {"A_NL": 15, "B_EN": 15, "C_OBJECT": 16, "D_AUTHOR": 12, "E_PARAM": 12}
    for q in qs:
        t = q["truth"]
        assert t["kind"] in ("TOPIC", "PAGES", "SOURCES")
        if t["kind"] == "PAGES":
            assert t["targets"] and all(TB.PAGE_ID.match(x["page_id"]) for x in t["targets"])
        if t["kind"] == "SOURCES":
            assert t["source_ids"]
    banned = {"quote", "snippet", "highlights", "normalized_text", "caption", "title_or_caption", "text_snippet"}

    def keys(x):
        if isinstance(x, dict):
            return set(x) | {k for v in x.values() for k in keys(v)}
        if isinstance(x, list):
            return {k for v in x for k in keys(v)}
        return set()

    assert not any(keys(q) & banned for q in qs)


def test_type_metrics():
    S = _load("qt_score", QT / "scripts" / "score.py")
    r = TB.Ranking()
    r.add_page("VKM-SRC-001:p0001")
    r.add_page("VKM-SRC-002:p0002", ["VKM-SRC-003:p0002"])
    r.add_page("VKM-SRC-002:p0005")
    m = S.source_metrics(r, {"VKM-SRC-003", "VKM-SRC-009"})
    assert m["source_capped_recall@10"] == 0.5 and m["mrr@50"] == 0.5 and m["precision@10"] == 0.1
    t = S.pages_topic("Q", [{"page_id": "VKM-SRC-002:p0005", "alt_page_ids": []},
                            {"page_id": "VKM-SRC-004:p0001", "alt_page_ids": ["VKM-SRC-001:p0001"]}])
    pm = S.page_metrics(t, r)
    assert pm["page_recall@10"] == 1.0 and pm["mrr@50"] == 1.0 and pm["precision@10"] == pytest.approx(0.2)
