"""Service configuration and CLI registration (no GPU)."""
from __future__ import annotations

import argparse
import json

import pytest

from vkm_corpus.retrieval_service.config import ServiceConfigError, load_config


def _write(tmp_path, data) -> str:
    p = tmp_path / "rx580.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_load_config_with_env_overrides(tmp_path):
    token = tmp_path / "tok"
    token.write_text("abc\n", encoding="utf-8")
    path = _write(tmp_path, {"service": {"keepalive_s": 5},
                             "models": [{"role": "dense", "key": "granite-311m-r2", "gguf": "/m/g.gguf", "port": 1},
                                        {"role": "late", "key": "mlateon", "gguf": "/m/l.gguf", "port": 2,
                                         "extra_args": ["--dim", "8"]}],
                             "search": {"dense_index": "vkm-exp-x"}})
    cfg = load_config({"VKM_RX580_CONFIG": path, "VKM_RX580_KEEPALIVE_S": "0", "VKM_RX580_TOKEN_FILE": str(token),
                       "VKM_OPENSEARCH_URL": "http://127.0.0.1:9200"})
    assert cfg.keepalive_s == 0.0 and cfg.token == "abc"
    assert cfg.slot("dense").endpoint == "http://127.0.0.1:1" and cfg.slot("late").extra_args == ("--dim", "8")
    assert cfg.search.opensearch_url == "http://127.0.0.1:9200" and cfg.search.dense_index == "vkm-exp-x"


def test_config_rejects_unknown_keys_duplicate_roles_and_missing_path(tmp_path):
    with pytest.raises(ServiceConfigError):
        load_config({})
    bad = _write(tmp_path, {"models": [{"role": "dense", "key": "k", "gguf": "g", "gpu_lock": True}]})
    with pytest.raises(ServiceConfigError, match="unknown"):
        load_config({"VKM_RX580_CONFIG": bad})
    dup = _write(tmp_path, {"models": [{"role": "dense", "key": "a", "gguf": "g"},
                                       {"role": "dense", "key": "b", "gguf": "h"}]})
    with pytest.raises(ServiceConfigError, match="one model per role"):
        load_config({"VKM_RX580_CONFIG": dup})


def test_cli_groups_register():
    from vkm_corpus.embeddings import cli as embed_cli
    from vkm_corpus.retrieval_service import cli as svc_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="group")
    embed_cli.register(sub)
    svc_cli.register(sub)
    a = parser.parse_args(["embed", "signature", "--key", "granite-311m-r2", "--gguf", "g.gguf", "--quant", "Q8_0",
                           "--tokenizer-dir", "t"])
    assert a.func.__name__ == "_signature"
    assert parser.parse_args(["retrieval", "serve", "--port", "8790"]).port == 8790


def test_embed_specs_command(capsys):
    from vkm_corpus.embeddings import cli as embed_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="group")
    embed_cli.register(sub)
    args = parser.parse_args(["embed", "specs"])
    assert args.func(args) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["key"] for r in rows} >= {"jina-v5-nano-retrieval", "jina-colbert-v2", "bge-m3", "pplx-embed-late-0.6b"}
