"""``vkm-corpus run`` CLI (agent C): plan-only writes no rows; ``--recall-model`` is refused without the sha256 of the
exact confirmed plan (H-05, H-12); ``--page`` needs a single source."""
from __future__ import annotations

import json
from pathlib import Path

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


def test_no_write_plan_does_not_initialize_missing_stage(cli_env, capsys):
    from vkm_corpus.cli import main

    assert main(["run", "plan", "--no-write", "--source", "VKM-SRC-901"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["plan_sha256"]) == 64 and plan["sources"][0]["action"] == "PROCESS"
    assert "run_id" not in plan and "plan_artifact_id" not in plan
    assert not cli_env.exists()


def test_no_write_plan_preserves_existing_stage(cli_env, capsys):
    from vkm_corpus.cli import main

    journaled = _plan(capsys)
    def inventory():
        return {p.relative_to(cli_env): p.read_bytes() if p.is_file() else None for p in cli_env.rglob("*")}

    before = inventory()
    assert main(["run", "plan", "--no-write", "--source", "VKM-SRC-901"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert (plan["plan_sha256"], plan["sources"]) == (journaled["plan_sha256"], journaled["sources"])
    assert inventory() == before


def test_no_write_plan_reads_cached_epub_without_touching_artifacts(cli_env, capsys):
    pytest.importorskip("lxml")
    from vkm_corpus.cli import main
    from vkm_corpus.extract.synthetic import make_epub
    from vkm_corpus.parquet.layout import init_root
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.context import open_cache, open_store
    from vkm_corpus.pipeline.prepare import prepare_source
    from vkm_corpus.pipeline.sources import load_sources

    resources = cli_env.parent / "resources"
    make_epub(resources / "docs" / "a.epub", n_units=1)
    make_register(resources, [register_row("VKM-SRC-901", "docs/a.epub", resources)])
    init_root(cli_env, "STAGING")
    cfg = PipelineConfig(cli_env, resources)
    run = "RUN-20260928T010000Z-0000aaaa"
    store, cache = open_store(cfg, run, "fixture"), open_cache(cfg, run, "fixture")
    try:
        assert prepare_source(cfg, store, cache, load_sources(resources)[0])["status"] == "PREPARED"
    finally:
        store.close()
        cache.close()
    before = sorted((p.relative_to(cli_env), p.read_bytes()) for p in cli_env.rglob("*") if p.is_file())
    assert main(["run", "plan", "--no-write", "--source", "VKM-SRC-901"]) == 0
    row, = json.loads(capsys.readouterr().out)["sources"]
    assert row["prepare"] == "CACHED" and row["ocr_tasks_known"] >= 1
    assert sorted((p.relative_to(cli_env), p.read_bytes()) for p in cli_env.rglob("*") if p.is_file()) == before


def test_ops_worker_reads_actual_plan_without_staging_writes(cli_env, monkeypatch):
    import vkm_corpus
    from vkm_corpus.ops.worker import Selection, Worker, WorkerConfig, stable_plan, subprocess_runner

    monkeypatch.setenv("PYTHONPATH", str(Path(vkm_corpus.__file__).resolve().parents[1]))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    worker = object.__new__(Worker)  # Exercise the real CLI subprocess without starting a job database.
    worker.config = WorkerConfig(worker_id="synthetic", publish_target=None, reconcile_cmd=None)
    worker.runner = subprocess_runner
    selection = Selection("VKM-SRC-901")
    assert len(stable_plan(worker._pipeline_plan(selection), selection)["pipeline_plan_sha256"]) == 64
    assert not cli_env.exists()


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
