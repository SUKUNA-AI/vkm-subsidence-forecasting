"""Embedding job worker (§34–36): atomic claims, two devices, no object embedded twice, idempotent re-run."""
from __future__ import annotations

import threading

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from vkm_corpus.embeddings.artifacts import ArtifactWriter, read_table, validate  # noqa: E402
from vkm_corpus.embeddings.reembed import CanonObject  # noqa: E402
from vkm_corpus.embeddings.signature import EmbeddingConfig, text_hash  # noqa: E402
from vkm_corpus.embeddings.worker import EmbeddingWorker, Encoded, InMemoryJobQueue  # noqa: E402


class Source:
    def __init__(self, texts: dict[str, str]) -> None:
        self.texts = texts

    def objects(self, ids):
        return [(CanonObject(i, text_hash(self.texts[i]), "VKM-SRC-001", None, "BLOCK"), self.texts[i]) for i in ids]


class Enc:
    def __init__(self, backend: str, fail_on: str | None = None) -> None:
        self.backend, self.fail_on = backend, fail_on
        self.seen: list[str] = []
        self._lock = threading.Lock()

    def encode(self, texts):
        out = []
        for t in texts:
            if t == self.fail_on:
                raise ConnectionError("backend went away")
            with self._lock:
                self.seen.append(t)
            v = np.frombuffer(text_hash(t).encode()[:16], dtype=np.uint8).astype(np.float32)[:4] + 1.0
            out.append(Encoded(vector=v / np.linalg.norm(v)))
        return out


def _cfg():
    return EmbeddingConfig(model_id="org/enc", model_revision="1" * 40, weights_file="enc-Q8_0.gguf",
                           weights_sha256="a" * 64, quantization="Q8_0", mode="dense", dimension=4, pooling="cls",
                           normalization="l2", tokenizer_sha256="b" * 64)


def test_two_devices_share_the_queue_without_double_work(tmp_path):
    texts = {f"o{i:03d}": f"text {i}" for i in range(40)}
    cfg = _cfg()
    q = InMemoryJobQueue()
    q.add(cfg.signature(), "dense", sorted(texts), job_size=5)
    writer = ArtifactWriter(tmp_path, "dense", cfg, writer_id="rx580-0")
    writer_rtx = ArtifactWriter(tmp_path, "dense", cfg, writer_id="rtx-0")
    rx, rtx = Enc("llama.cpp-vulkan"), Enc("llama.cpp-cuda")
    workers = [EmbeddingWorker(q, Source(texts), rx, writer, device="RX580", name="rx580-0"),
               EmbeddingWorker(q, Source(texts), rtx, writer_rtx, device="RTX5070", name="rtx-0")]
    threads = [threading.Thread(target=w.run) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert q.counts() == {"DONE": 8}
    assert sorted(rx.seen + rtx.seen) == sorted(texts.values())          # every object exactly once
    states = {j.worker for j in q.jobs.values()}
    assert states <= {"rx580-0", "rtx-0"}
    rep = validate(writer.dir, cfg, {k: text_hash(v) for k, v in texts.items()})
    assert rep.ok, rep.as_dict()
    backends = set(read_table(writer.dir).column("backend").to_pylist())
    assert backends <= {"llama.cpp-vulkan", "llama.cpp-cuda"}


def test_rerun_of_an_already_embedded_job_does_no_inference(tmp_path):
    texts = {"a": "x", "b": "y"}
    cfg = _cfg()
    writer = ArtifactWriter(tmp_path, "dense", cfg)
    q = InMemoryJobQueue()
    q.add(cfg.signature(), "dense", ["a", "b"], job_size=2)
    enc = Enc("llama.cpp-vulkan")
    EmbeddingWorker(q, Source(texts), enc, writer, device="RX580", name="w").run()
    q.add(cfg.signature(), "dense", ["a", "b"], job_size=2)        # e.g. the same job re-queued after a crash
    r = EmbeddingWorker(q, Source(texts), enc, writer, device="RX580", name="w").run()
    assert r[0]["embedded"] == 0 and r[0]["skipped_existing"] == 2 and r[0]["part"] is None
    assert enc.seen == ["x", "y"]


def test_changed_text_is_re_embedded_only(tmp_path):
    cfg = _cfg()
    writer = ArtifactWriter(tmp_path, "dense", cfg)
    q = InMemoryJobQueue()
    enc = Enc("llama.cpp-vulkan")
    q.add(cfg.signature(), "dense", ["a", "b"], job_size=2)
    EmbeddingWorker(q, Source({"a": "x", "b": "y"}), enc, writer, device="RX580", name="w").run()
    q.add(cfg.signature(), "dense", ["a", "b"], job_size=2)
    EmbeddingWorker(q, Source({"a": "x", "b": "y2"}), enc, writer, device="RX580", name="w").run()
    assert enc.seen == ["x", "y", "y2"]


def test_retryable_failure_returns_job_to_pending_then_fails_after_max_attempts(tmp_path):
    cfg = _cfg()
    writer = ArtifactWriter(tmp_path, "dense", cfg)
    q = InMemoryJobQueue(max_attempts=2)
    q.add(cfg.signature(), "dense", ["a"], job_size=1)
    w = EmbeddingWorker(q, Source({"a": "boom"}), Enc("v", fail_on="boom"), writer, device="RX580", name="w")
    with pytest.raises(ConnectionError):
        w.run_once()
    assert q.counts() == {"PENDING": 1}
    with pytest.raises(ConnectionError):
        w.run_once()
    assert q.counts() == {"FAILED": 1}


def test_claims_are_scoped_to_the_config_signature():
    q = InMemoryJobQueue()
    q.add("s1", "dense", ["a"], job_size=1)
    assert q.claim(device="RX580", worker="w", config_signature="s2") is None
    job = q.claim(device="RX580", worker="w", config_signature="s1")
    assert job is not None and job.state == "CLAIMED_RX580"
    assert q.claim(device="RTX5070", worker="w2", config_signature="s1") is None
