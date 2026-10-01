"""Cached preparation may not admit replaced, absent, pointer or escaped originals."""
from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from vkm_corpus.extract.model import SourceInput
from vkm_corpus.pipeline.config import PipelineConfig
from vkm_corpus.pipeline import prepare as p


def setup_cached(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    path = root / "source.pdf"
    path.write_bytes(b"%PDF-1.4\noriginal synthetic bytes")
    src = SourceInput("VKM-SRC-001", "source.pdf", hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
    cfg = PipelineConfig(tmp_path / "data", root)
    monkeypatch.setattr(p, "library_versions", lambda: {})
    sig = p.prepare_signature(cfg, src)
    cache_file = p.prep_path(cfg.data_root, src.source_id, sig)
    cache_file.parent.mkdir(parents=True)
    cached = {"status": "PREPARED", "sentinel": "original-cache", "transient_errors": False,
              "schema": p.PREP_SCHEMA, "source_sha256": src.sha256, "prepare_signature": sig}
    cache_file.write_text(json.dumps(cached), encoding="utf-8")
    return cfg, src, path, cache_file, cached


def test_cache_hit_still_hashes_same_size_same_mtime_replacement(tmp_path, monkeypatch):
    cfg, src, path, cache_file, cached = setup_cached(tmp_path, monkeypatch)
    cache = SimpleNamespace(run_id="test")  # no writes are allowed on either path
    assert p.prepare_source(cfg, None, cache, src) == cached
    stamp = path.stat()
    path.write_bytes(path.read_bytes().replace(b"original", b"replaced"))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size and path.stat().st_mtime_ns == stamp.st_mtime_ns
    got = p.prepare_source(cfg, None, cache, src)
    assert got["status"] == "FAILED"
    assert got["errors"][0]["code"] == "SOURCE_SHA256_MISMATCH"
    assert json.loads(cache_file.read_text()) == cached


@pytest.mark.parametrize("kind", ["missing", "lfs", "escape", "windows_absolute", "bad_hash"])
def test_bad_input_never_returns_or_overwrites_cached_success(tmp_path, monkeypatch, kind):
    cfg, src, path, cache_file, cached = setup_cached(tmp_path, monkeypatch)
    if kind == "missing":
        path.unlink()
    elif kind == "lfs":
        path.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 3\n")
    elif kind == "escape":
        src.canonical_path = "../outside.pdf"
    elif kind == "windows_absolute":
        src.canonical_path = "C:\\private\\source.pdf"
    else:
        src.sha256 = ""
    got = p.prepare_source(cfg, None, SimpleNamespace(run_id="test"), src)
    assert got["status"] == "FAILED" and got["errors"]
    assert json.loads(cache_file.read_text()) == cached


def test_cached_failed_preparation_is_not_reused(tmp_path, monkeypatch):
    cfg, src, _path, cache_file, cached = setup_cached(tmp_path, monkeypatch)
    cached.update(status="FAILED", errors=[{"code": "NATIVE_EXTRACT_FAILED"}])
    cache_file.write_text(json.dumps(cached), encoding="utf-8")
    calls = []
    def extract(_cfg, _store, _src, _path, prep):
        calls.append(_src.source_id)
        prep["pagination"] = {"count": 0}
    monkeypatch.setattr(p, "_prepare_pdf", extract)
    got = p.prepare_source(cfg, None, SimpleNamespace(run_id="test", add_stage=lambda **_: None), src)
    assert calls == [src.source_id] and got["status"] == "PREPARED" and "sentinel" not in got
