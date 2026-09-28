"""``vkm-corpus run`` CLI (agent C): plan-only writes no rows; ``--recall-model`` is refused without the sha256 of the
exact confirmed plan (H-05, H-12); ``--page`` needs a single source."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pymupdf")
pytest.importorskip("pyarrow")

from vkm_corpus.extract.synthetic import make_pdf, make_register, register_row  # noqa: E402


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    res = tmp_path / "resources"
    (res / "docs").mkdir(parents=True)
    make_pdf(res / "docs" / "a.pdf", pages=("text", "empty"))
    make_pdf(res / "docs" / "b.pdf", pages=("text",))
    make_register(res, [register_row("VKM-SRC-901", "docs/a.pdf", res), register_row("VKM-SRC-902", "docs/b.pdf", res)])
    data = tmp_path / "staging"
    monkeypatch.setenv("VKM_DATA_ROOT", str(data))
    monkeypatch.setenv("VKM_DATA_ROLE", "producer")
    monkeypatch.setenv("VKM_RESOURCES_ROOT", str(res))
    monkeypatch.delenv("VKM_OCR_URL", raising=False)
    monkeypatch.delenv("VKM_MODELS_DIR", raising=False)
    return data


def _plan(capsys):
    from vkm_corpus.cli import main

    assert main(["run", "plan", "--source", "VKM-SRC-901"]) == 0
    out = capsys.readouterr().out
    first = json.loads(out[: out.index("\n}\n") + 3])
    return first


def test_plan_only_writes_no_document_rows(cli_env, capsys):
    plan = _plan(capsys)
    assert len(plan["plan_sha256"]) == 64 and plan["sources"][0]["action"] == "PROCESS"
    assert not list((cli_env / "canonical").glob("pages/**/*.parquet"))
    assert list((cli_env / "canonical" / "_runs").glob("run=*/END.json"))  # the plan run is recorded


def test_recall_model_needs_the_confirmed_plan(cli_env, capsys):
    from vkm_corpus.cli import main

    plan = _plan(capsys)
    assert main(["run", "extract", "--source", "VKM-SRC-901", "--recall-model", "--no-ocr"]) == 4
    assert main(["run", "extract", "--source", "VKM-SRC-901", "--recall-model", "--no-ocr",
                 "--confirm-plan", "0" * 64]) == 4
    assert plan["plan_sha256"] != "0" * 64


def test_page_range_needs_one_source(cli_env):
    from vkm_corpus.cli import main

    assert main(["run", "extract", "--source", "VKM-SRC-901,VKM-SRC-902", "--page", "1-2"]) == 2
