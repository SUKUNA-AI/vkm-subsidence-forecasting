"""``embed encode``: objects → a running (fake) llama-server → derived artifacts; §64-valid; the second run embeds
nothing (§46: same text hash + signature → 0 inference)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pyarrow")
tokenizers = pytest.importorskip("tokenizers")

import vkm_corpus  # noqa: E402
from vkm_corpus.embeddings import cli  # noqa: E402
from vkm_corpus.embeddings.llama import LlamaError, LlamaServerClient  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _word_tokenizer(path) -> None:
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    words = ["[UNK]", "[PAD]", "оседание", "соль", "мульда", "ползучесть", "shaft", "InSAR", "блок", "рис"]
    tok = Tokenizer(WordLevel({w: i for i, w in enumerate(words)}, unk_token="[UNK]"))
    tok.pre_tokenizer = Whitespace()
    path.mkdir(parents=True, exist_ok=True)
    tok.save(str(path / "tokenizer.json"))


def test_encode_writes_valid_artifacts_and_is_idempotent(tmp_path, capsys):
    port = _free_port()
    src = os.path.dirname(os.path.dirname(os.path.abspath(vkm_corpus.__file__)))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (src, os.environ.get("PYTHONPATH", "")) if p)}
    proc = subprocess.Popen([sys.executable, "-m", "vkm_corpus.embeddings.fake_server", "--port", str(port),
                             "--pooling", "cls", "--dim", "768"], env=env)
    try:
        client = LlamaServerClient(f"http://127.0.0.1:{port}")
        for _ in range(100):
            try:
                if client.health().get("http_status") == 200:
                    break
            except LlamaError:
                time.sleep(0.05)
        tok_dir = tmp_path / "tok"
        _word_tokenizer(tok_dir)
        gguf = tmp_path / "m.gguf"
        gguf.write_bytes(b"GGUF-fake")
        cfg = {"service": {"spawn": False}, "models": [{
            "role": "dense", "key": "granite-311m-r2", "gguf": str(gguf), "quant": "Q8_0",
            "tokenizer_dir": str(tok_dir), "url": f"http://127.0.0.1:{port}", "doc_max_len": 64}]}
        (tmp_path / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
        words = ["оседание соль", "мульда", "ползучесть соль", "shaft InSAR", "блок рис"]
        docs = [{"object_id": f"VKM-SRC-001:p{i:04d}:b{i}", "text": f"{words[i % 5]} {i}", "source_id": "VKM-SRC-001",
                 "object_kind": "BLOCK"} for i in range(10)]
        (tmp_path / "docs.jsonl").write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in docs),
                                             encoding="utf-8")
        argv = ["encode", "--config", str(tmp_path / "cfg.json"), "--docs", str(tmp_path / "docs.jsonl"),
                "--data-root", str(tmp_path / "data"), "--job-size", "4"]
        assert cli.main(argv) == 0
        first = json.loads(capsys.readouterr().out)[0]
        assert first["embedded"] == 10 and first["jobs"] == 3 and first["validation"]["ok"]
        assert first["queue"] == {"DONE": 3}
        assert cli.main(argv) == 0
        second = json.loads(capsys.readouterr().out)[0]
        assert second["embedded"] == 0 and second["jobs"] == 0 and second["validation"]["ok"]
        assert second["config_signature"] == first["config_signature"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
