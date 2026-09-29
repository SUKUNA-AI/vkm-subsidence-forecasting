"""RX580 service with the visual slot (agent VIS): the text tower of Qwen3-VL-Embedding-2B as a third resident model
— config role, chat-template query ids, pooled L2 vector under role ``visual``, the query signature with the template,
404 without the slot; and the RX580 gate's verdict logic (numpy only). Fakes, no GPU."""
from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")

from vkm_corpus.embeddings.fakes import FakeBackend, FakeTokenizer  # noqa: E402
from vkm_corpus.embeddings.specs import QWEN3VL_QUERY_TEMPLATE, get  # noqa: E402
from vkm_corpus.embeddings.tokenize import SpecTokenizer  # noqa: E402
from vkm_corpus.retrieval_service.config import ModelSlot, load_config  # noqa: E402
from vkm_corpus.retrieval_service.encoders import QueryEncoder  # noqa: E402

DIM = 2048


def _visual(delay_s: float = 0.0) -> QueryEncoder:
    spec = get("qwen3-vl-emb-2b")
    tok = SpecTokenizer(spec, FakeTokenizer(bos=None, eos="<|endoftext|>",
                                            specials=("<|im_start|>", "<|im_end|>", "<pad>")))
    slot = ModelSlot(role="visual", key=spec.key, gguf="qwen3-vl-emb-2b-F16.gguf", quant="F16", gguf_sha256="f" * 64,
                     port=3, extra_args=("-ot", "token_embd\\.weight=CPU"))
    return QueryEncoder(slot, spec, tok, FakeBackend(DIM, pooled=True, delay_s=delay_s, name="visual"))


def test_template_ids_end_with_the_pooled_token():
    spec = get("qwen3-vl-emb-2b")
    ft = FakeTokenizer(bos=None, eos="<|endoftext|>", specials=("<|im_start|>", "<|im_end|>"))
    st = SpecTokenizer(spec, ft)
    q = st.encode("схема целиков", "query")
    words = [ft.token_to_id(w) for w in ("Find", "document", "image", "схема", "целиков", "assistant")]
    assert all(w in q.ids for w in words) and q.ids[-1] == ft.token_to_id("<|endoftext|>")
    assert q.ids.index(ft.token_to_id("Find")) < q.ids.index(ft.token_to_id("схема"))       # system turn first
    other = st.encode("схема целиков", "query", prefix="Retrieve maps.")
    assert ft.token_to_id("Retrieve") in other.ids and ft.token_to_id("Find") not in other.ids
    doc = st.encode("схема", "document")                     # documents are images: text path is the plain prefix
    assert ft.token_to_id("assistant") not in doc.ids
    assert "{instruction}" in QWEN3VL_QUERY_TEMPLATE and QWEN3VL_QUERY_TEMPLATE.endswith("<|im_start|>assistant\n")


def test_visual_encoder_returns_a_unit_vector_and_a_template_signature():
    enc = _visual()
    out = enc.encode("карта оседаний")
    assert out.role == "visual" and out.vector.shape == (DIM,) and abs(np.linalg.norm(out.vector) - 1) < 1e-5
    assert enc.pooling == "last" and enc.qconfig.as_dict()["template"] == QWEN3VL_QUERY_TEMPLATE
    assert enc.qconfig.quantization == "F16" and enc.qconfig.dimension == DIM


def test_config_accepts_one_visual_slot(tmp_path):
    p = tmp_path / "rx580.json"
    p.write_text(json.dumps({"models": [
        {"role": "dense", "key": "jina-v5-nano-retrieval", "gguf": "/m/n.gguf", "port": 1},
        {"role": "late", "key": "mlateon", "gguf": "/m/l.gguf", "port": 2},
        {"role": "visual", "key": "qwen3-vl-emb-2b", "gguf": "/m/q.gguf", "port": 3, "quant": "F16",
         "extra_args": ["-ot", "token_embd\\.weight=CPU"]}]}), encoding="utf-8")
    cfg = load_config({"VKM_RX580_CONFIG": str(p)})
    assert cfg.slot("visual").extra_args == ("-ot", "token_embd\\.weight=CPU")
    p.write_text(json.dumps({"models": [{"role": "image", "key": "k", "gguf": "g"}]}), encoding="utf-8")
    from vkm_corpus.retrieval_service.config import ServiceConfigError

    with pytest.raises(ServiceConfigError, match="dense\\|late\\|visual"):
        load_config({"VKM_RX580_CONFIG": str(p)})


def test_example_config_has_the_visual_slot():
    from pathlib import Path

    ex = json.loads((Path(__file__).resolve().parents[2] / "infra/core/rx580/rx580.example.json").read_text("utf-8"))
    vis = [m for m in ex["models"] if m["role"] == "visual"]
    assert len(vis) == 1 and vis[0]["key"] == "qwen3-vl-emb-2b" and vis[0]["quant"] == "F16"
    assert vis[0]["extra_args"] == ["-ot", "token_embd\\.weight=CPU"]


def test_embed_query_role_visual_and_404_without_the_slot():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from vkm_corpus.retrieval_service.app import create_app

    with TestClient(create_app(None, encoders={"visual": _visual()})) as c:
        r = c.post("/embed/query", json={"text": "разрез пласта", "role": "visual"}).json()
        info = c.get("/model-info").json()
        h = c.get("/health").json()
    assert len(r["visual"]["vector"]) == DIM and r["visual"]["model"] == "qwen3-vl-emb-2b"
    assert r["visual"]["dimension"] == DIM and len(r["visual"]["signature"]) == 64
    assert info["models"][0]["query_config"]["template"] == QWEN3VL_QUERY_TEMPLATE
    assert {m["role"] for m in h["models"]} == {"visual"}
    with TestClient(create_app(None, encoders={})) as c:
        assert c.post("/embed/query", json={"text": "x", "role": "visual"}).status_code == 404


# ---------------------------------------------------------------- RX580 gate (numpy only)
def _ref(tmp_path, n_q=12, n_p=300, noise=0.0, dim=64):
    rng = np.random.default_rng(7)
    q = rng.standard_normal((n_q, dim)).astype(np.float32)
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    d = rng.standard_normal((n_p, dim)).astype(np.float32)
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    rx = q + noise * rng.standard_normal(q.shape).astype(np.float32)
    return q, d, rx / np.linalg.norm(rx, axis=1, keepdims=True)


def test_gate_evaluate_and_verdict():
    from vkm_corpus.embeddings.visual_gate import GATE_VISUAL, evaluate, verdict

    q, d, rx = _ref(None, noise=0.001)
    ev = evaluate(q, rx, d)
    assert ev["query_vectors"]["cos_mean"] > 0.999 and ev["ranking"]["top10_overlap"] > 0.9
    good = {"tokenization": {"equal": True}, "parity": ev, "query_latency_sequential": {"p95_ms": 480.0},
            "resident_after_load": {"device": {"vram_used_mib": 3900.0}}}
    assert verdict(good)["verdict"] == "PASS" and verdict(good)["thresholds"] == GATE_VISUAL
    slow = {**good, "query_latency_sequential": {"p95_ms": 1500.0}}
    assert verdict(slow)["verdict"] == "FAIL" and not verdict(slow)["checks"]["latency_p95"]
    _, _, far = _ref(None, noise=0.2)
    bad = {**good, "parity": evaluate(q, far, d)}
    v = verdict(bad)
    assert v["verdict"] == "FAIL" and not v["checks"]["query_cos_mean"]
    full = {**good, "resident_after_load": {"device": {"vram_used_mib": 7500.0}}}
    assert not verdict(full)["checks"]["device_vram"]
    assert not verdict({**good, "tokenization": {"equal": False}})["checks"]["tokenization"]


def test_gate_reference_loader(tmp_path):
    from vkm_corpus.embeddings.visual_gate import load_visual_reference

    q, d, _ = _ref(tmp_path, n_q=3)
    np.save(tmp_path / "q_dense.npy", q)
    np.save(tmp_path / "d_dense.npy", d.astype(np.float16))
    (tmp_path / "meta.json").write_text(json.dumps({"probe": "benchmark queries"}), encoding="utf-8")
    (tmp_path / "tokens_query.jsonl").write_text("".join(json.dumps({"query_id": f"Q{i}", "text": "t", "ids": [1, 2]})
                                                         + "\n" for i in range(3)), encoding="utf-8")
    ref = load_visual_reference(tmp_path)
    assert ref["q"].shape == (3, 64) and ref["d"].dtype == np.float32 and ref["queries"][0]["ids"] == [1, 2]
    (tmp_path / "tokens_query.jsonl").write_text(json.dumps({"ids": [1]}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_visual_reference(tmp_path)
