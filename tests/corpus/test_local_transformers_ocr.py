from __future__ import annotations

import pytest

from vkm_corpus.cli import main


def test_local_transformers_import_is_an_explicit_producer_command(capsys):
    with pytest.raises(SystemExit) as done:
        main(["run", "import-local-ocr", "--help"])
    assert done.value.code == 0
    assert "--receipt-sha256" in capsys.readouterr().out

@pytest.fixture
def import_args(tmp_path, monkeypatch):
    pytest.importorskip("pymupdf")
    from vkm_corpus.extract.synthetic import make_pdf, make_register, register_row
    from vkm_corpus.parquet.layout import init_root

    resources = tmp_path / "resources"
    resources.mkdir()
    make_pdf(resources / "a.pdf", pages=("text", "text"))
    make_register(resources, [register_row("VKM-SRC-901", "a.pdf", resources)])
    data = tmp_path / "staging"
    init_root(data, "STAGING")
    receipt = tmp_path / "draft.json"
    receipt.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VKM_RESOURCES_ROOT", str(resources))
    monkeypatch.setenv("VKM_DATA_ROOT", str(data))
    monkeypatch.setenv("VKM_DATA_ROLE", "producer")
    return data, ["run", "import-local-ocr", "--source", "VKM-SRC-901", "--page", "1", "--page", "2",
                  "--isolated-staging", str(data), "--receipt", str(receipt), "--receipt-sha256", "0" * 64,
                  "--execution", str(receipt)]


def test_bad_receipt_digest_changes_no_staging_files(import_args, capsys):
    data, args = import_args
    def files():
        return {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    before = files()
    assert main(args) != 0
    assert "digest mismatch" in capsys.readouterr().err
    assert files() == before


def test_import_refuses_current_marker_even_if_empty(import_args, capsys):
    from vkm_corpus.parquet.layout import CanonLayout

    data, args = import_args
    layout = CanonLayout(data)
    (layout.canonical / layout.CURRENT).touch()
    assert main(args) != 0
    assert "without publication state" in capsys.readouterr().err


def test_pending_local_cache_requires_explicit_exact_exploratory_packet(tmp_path):
    from types import SimpleNamespace
    from vkm_corpus.pipeline.cache import StageCache
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.local_transformers_ocr import STAGE, _stage_signature, source_entry

    source = SimpleNamespace(source_id="VKM-SRC-901", sha256="2" * 64)
    cfg = PipelineConfig(data_root=tmp_path, resources_root=tmp_path)
    cache = StageCache(tmp_path, "local-test")
    try:
        assert source_entry(cfg, cache, source) is None
        values = {"source_sha256": source.sha256, "packet_sha256": "1" * 64}
        cache.add_stage(stage_signature=_stage_signature(values), stage=STAGE, source_id=source.source_id,
                        page_index=None, outputs=values)
        with pytest.raises(ValueError, match="explicit exploratory packet"):
            source_entry(cfg, cache, source)
        cfg.local_ocr_packet_sha256 = "1" * 64
        assert source_entry(cfg, cache, source)["outputs"] == values
        cfg.profile = "production"
        with pytest.raises(ValueError, match="explicit exploratory packet"):
            source_entry(cfg, cache, source)
        cfg.profile = "exploratory"
        values["source_sha256"] = "3" * 64
        with pytest.raises(ValueError, match="source or signature changed"):
            source_entry(cfg, cache, source)
    finally:
        cache.close()
