"""figure_series rows: DERIVATION + AUTO_EXTRACTED_UNREVIEWED only, per-point errors, availability date."""
from __future__ import annotations

import numpy as np
import pytest

from vkm_corpus.figures import core
from vkm_corpus.figures.dataset import (FigureContext, SeriesValidationError, build_rows, summarize, validate_row,
                                        write_jsonl)
from vkm_corpus.figures.primitives import Path, Text


def _result():
    texts = [Text(str(v), v / 10 - 0.1, v / 10 + 0.1, -0.2, 0.1) for v in (0, 10, 20, 30)]
    texts += [Text(str(v), -0.6, -0.2, v / 100, 0.1) for v in (0, 100, 200)]
    texts += [Text("Оседание, мм", -1.2, -0.7, 1.0, 0.1), Text("Время, сутки", 1.0, 2.0, -0.45, 0.1)]
    paths = [Path(np.array([[0, 0], [3, 0]]), None, 0.01, "LINE"), Path(np.array([[0, 0], [0, 2]]), None, 0.01,
                                                                          "LINE")]
    paths += [Path(np.array([[0, 0], [1, 0.5], [2, 1.2], [3, 1.8]]), 0x3366CC, 0.02, "POLY")]
    return core.digitize(texts, paths, (-1.3, -0.6, 3.2, 2.2), quantum=1e-4)


def _ctx(**kw):
    base = dict(figure_id="VKM-SRC-999:p0001:fabc", source_id="VKM-SRC-999", page_id="VKM-SRC-999:p0001",
                work_id="VKM-WRK-999", publication_year=2023, available_from="2023-12-31",
                available_basis="ASSUMED_FROM_PUBLICATION", caption_text_for_hints="расчётные оседания")
    base.update(kw)
    return FigureContext(**base)


def test_rows_are_derivation_with_errors_and_availability():
    rows = build_rows(_ctx(), _result(), "A_NATIVE_VECTOR", {"source_sha256": "0" * 64}, {"k": 1})
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "DERIVATION" and r["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    assert r["series_nature"] == "UNCLASSIFIED" and "MODEL_HINT_IN_CAPTION" in r["flags"]
    assert r["y_quantity_raw"] == "Оседание" and r["y_unit_raw"] == "мм"
    assert r["x_unit_raw"] == "сутки" and r["x_is_time"]
    assert r["available_from"] == "2023-12-31"
    assert all(p["y_err"] is not None for p in r["points"])
    assert [round(p["y"]) for p in r["points"]] == [0, 50, 120, 180]
    assert summarize(rows)["series"] == 1


def test_validation_rejects_fact_and_missing_errors():
    r = build_rows(_ctx(), _result(), "A_NATIVE_VECTOR", {}, {})[0]
    with pytest.raises(SeriesValidationError):
        validate_row({**r, "status": "FACT"})
    with pytest.raises(SeriesValidationError):
        validate_row({**r, "review_status": "REVIEWED_MEASUREMENT"})
    bad = {**r, "points": [{**r["points"][0], "y_err": None}]}
    with pytest.raises(SeriesValidationError):
        validate_row(bad)
    with pytest.raises(SeriesValidationError):
        validate_row({**r, "available_from": None, "publication_year": None})


def test_unknown_availability_stays_unknown():
    rows = build_rows(_ctx(available_from=None, publication_year=None, available_basis="UNKNOWN"), _result(),
                      "A_NATIVE_VECTOR", {}, {})
    assert rows[0]["available_from"] is None and "AVAILABILITY_UNKNOWN" in rows[0]["flags"]


def test_jsonl_is_deterministic(tmp_path):
    rows = build_rows(_ctx(), _result(), "B_AUTOCAD_PDFIMPORT", {"cad_job": "CADJ-x"}, {})
    a = write_jsonl(rows, tmp_path / "a.jsonl")
    b = write_jsonl(list(reversed(rows)), tmp_path / "b.jsonl")
    assert a == b


def test_publish_writes_a_manifest_with_hashes(tmp_path, capsys):
    import hashlib
    import json

    from vkm_corpus.figures import sweep

    rows = build_rows(_ctx(), _result(), "A_NATIVE_VECTOR", {}, {})
    src = tmp_path / "src"
    src.mkdir()
    sha = write_jsonl(rows, src / "figure_series_v0.jsonl")
    sweep.main(["publish", "--src", str(src), "--dst", str(tmp_path / "nav"), "--snapshot", "snap-x"])
    man = json.loads((tmp_path / "nav" / "manifest.json").read_text(encoding="utf-8"))
    assert man["files"]["figure_series_v0.jsonl"]["sha256"] == sha
    assert hashlib.sha256((tmp_path / "nav" / "figure_series_v0.jsonl").read_bytes()).hexdigest() == sha
    assert man["status"] == "DERIVATION" and man["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
    assert json.loads(capsys.readouterr().out)["figure_series_v0.jsonl"]["sha256"] == sha
