"""vkm-rerank-gateway with fake backends (agent F): auth, 413/422 mapping, flows, per-backend limits, no shared lock."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("PIL")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.retrieval import models as M  # noqa: E402
from vkm_corpus.retrieval.gateway import GatewayConfig, Resources, create_app  # noqa: E402

TOKEN = "unit-test-token-0123456789"
RUNTIME = {"model_id": "jinaai/jina-reranker-v3.5", "model_revision": "e8a93f33", "model_code_revision": "e8a93f33",
           "tokenizer_revision": "e8a93f33", "runtime_backend": "transformers-cuda", "dtype_or_quantization": "float16",
           "torch_version": "2.14.0+cu132", "transformers_version": "4.57.3"}


class FakeCounter:
    sha256 = "0" * 64

    def __init__(self, overhead: int = 100) -> None:
        self.overhead = overhead

    def count(self, text: str) -> int:
        return len(text.split())

    def truncate(self, text: str, n: int):
        words = text.split()
        if len(words) <= n:
            return text, len(words), False
        end = len(" ".join(words[:n]))
        return text[:end], n, True

    def v35_prompt_tokens(self, query: str, docs: list[str]) -> int:
        return self.overhead + self.count(query) + sum(self.count(d) for d in docs)


class FakeText:
    def __init__(self, counter: FakeCounter, delay: float = 0.0) -> None:
        self.counter, self.delay, self.calls = counter, delay, []

    async def identity(self, refresh: bool = False):
        return dict(RUNTIME)

    async def health(self):
        return "ready"

    async def rerank(self, query, documents, top_n, token_budget):
        self.calls.append((time.monotonic(), len(documents)))
        await asyncio.sleep(self.delay)
        words = set(query.lower().split())
        scored = [(i, len(words & set(d.lower().split())) - i * 1e-3) for i, d in enumerate(documents)]
        scored.sort(key=lambda x: -x[1])
        return {"results": scored[:top_n], "total_tokens": self.counter.v35_prompt_tokens(query, documents),
                "runtime": dict(RUNTIME)}


class FakeVisual:
    def __init__(self, delay: float = 0.0, model_path: str = "/models/m0/jina-reranker-m0-Q6_K.gguf") -> None:
        self.delay, self.model_path, self.calls = delay, model_path, []

    async def health(self):
        return "ready"

    async def props(self, refresh: bool = False):
        return {"media_marker": "<__media_test__>", "model_path": self.model_path, "total_slots": 2}

    async def embed_images(self, query, images_b64):
        self.calls.append(len(images_b64))
        await asyncio.sleep(self.delay)
        from PIL import Image, ImageStat

        out = []
        for b64 in images_b64:
            img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("L")
            out.append([ImageStat.Stat(img).mean[0] / 255.0])   # brightness as the "hidden state"
        return out


class FakeHead:
    sha256 = "1" * 64

    def scores(self, hidden):
        return [float(h[0]) for h in hidden]


def _png(color: int, size=(280, 280)) -> str:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, (color, color, color)).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def make_client(text_delay=0.0, visual_delay=0.0, visual_inflight=2, visual_queue=8, overhead=100, **cfg_kw):
    counter = FakeCounter(overhead)
    res = Resources(text=FakeText(counter, text_delay), visual=FakeVisual(visual_delay), v35_tokens=counter,
                    m0_tokens=FakeCounter(), head=FakeHead())
    cfg = GatewayConfig(auth_required=True, token=TOKEN, warmup=False, visual_max_inflight=visual_inflight,
                        visual_max_queue=visual_queue, **cfg_kw)
    return TestClient(create_app(cfg, resources=res)), res


def _hdr(**extra):
    return {M.HEADER_TOKEN: TOKEN, **extra}


def _text_body(n=5, query="оседание реперов"):
    return {"query": query, "candidates": [{"id": f"d{i}", "text": f"документ {i} " + ("оседание" if i == 3 else "")}
                                           for i in range(n)]}


def test_auth_required_and_health_open():
    client, _ = make_client()
    with client:
        assert client.post("/v1/rerank/text", json=_text_body()).status_code == 401
        r = client.post("/v1/rerank/text", json=_text_body(), headers={M.HEADER_TOKEN: "wrong"})
        assert r.status_code == 401 and r.json()["error"]["code"] == M.E_UNAUTHORIZED
        assert client.get("/status").status_code == 401
        h = client.get("/health")
        assert h.status_code == 200 and h.json() == {"status": "ok", "backends": {"text": "ready", "visual": "ready"}}


def test_text_flow_contract():
    client, res = make_client()
    with client:
        r = client.post("/v1/rerank/text", json={**_text_body(), "top_n": 3}, headers=_hdr())
        assert r.status_code == 200, r.text
        body = M.RerankResponse.model_validate(r.json())
        assert body.kind == "text" and body.top_n == 3 and body.candidate_ids[0] == "d3"
        assert body.model_id == RUNTIME["model_id"] and body.license == "CC-BY-NC-4.0"
        top = body.results[0]
        text = _text_body()["candidates"][3]["text"]
        assert top.input_text_sha256 == hashlib.sha256(text.encode()).hexdigest()
        assert top.text_char_range == (0, len(text)) and not top.truncated
        assert body.n_tokens_total == res.v35_tokens.v35_prompt_tokens(_text_body()["query"],
                                                                       [c["text"] for c in _text_body()["candidates"]])
        assert not [w for w in body.warnings if w.startswith("TOKEN_COUNT_DIFFERS")]
        assert r.headers[M.HEADER_REQUEST_ID] == body.request_id


def test_text_limits_are_413_without_backend_call():
    client, res = make_client(overhead=5000)
    with client:
        r = client.post("/v1/rerank/text", json=_text_body(25), headers=_hdr())
        assert r.status_code == 413 and r.json()["error"]["code"] == M.E_PAYLOAD_TOO_LARGE
        r = client.post("/v1/rerank/text", json=_text_body(3), headers=_hdr())
        err = r.json()["error"]
        assert r.status_code == 413 and err["stage"] == "text_tokens" and err["details"]["n_tokens_total"] > 4096
        assert res.text.calls == []                      # the service never saw an over-budget prompt


def test_text_truncation_and_top_n_clamp():
    client, _ = make_client()
    with client:
        body = {"query": "оседание", "candidates": [{"id": "a", "text": " ".join(["слово"] * 40)},
                                                    {"id": "b", "text": "оседание кратко"}],
                "truncate_to_tokens": 16, "top_n": 10}
        r = client.post("/v1/rerank/text", json=body, headers=_hdr())
        data = M.RerankResponse.model_validate(r.json())
        by_id = {x.id: x for x in data.results}
        assert by_id["a"].truncated and by_id["a"].n_tokens == 16 and by_id["a"].text_char_range[1] < 40 * 6
        assert "TOP_N_CLAMPED" in data.warnings and "CANDIDATES_TRUNCATED" in data.warnings and data.top_n == 2


def test_validation_errors_are_422_and_do_not_echo_input():
    client, _ = make_client()
    with client:
        r = client.post("/v1/rerank/text", json={"query": "q", "candidates": [{"id": "a", "text": "секрет-текст",
                                                                               "extra": 1}]}, headers=_hdr())
        assert r.status_code == 422 and r.json()["error"]["code"] == M.E_INPUT_INVALID
        assert "секрет-текст" not in r.text
        r = client.post("/v1/rerank/text", content=b"{not json", headers=_hdr())
        assert r.status_code == 422


def test_visual_flow_scores_follow_pixels():
    client, res = make_client()
    with client:
        body = {"query": "светлый рисунок", "candidates": [
            {"id": "dark", "image_base64": _png(20)}, {"id": "light", "image_base64": _png(230)},
            {"id": "light-dup", "image_base64": _png(230)}, {"id": "mid", "image_base64": _png(120, (1400, 1000))}]}
        r = client.post("/v1/rerank/visual", json=body, headers=_hdr())
        assert r.status_code == 200, r.text
        data = M.RerankResponse.model_validate(r.json())
        assert data.candidate_ids[:2] == ["light", "light-dup"] and data.candidate_ids[-1] == "dark"
        s = dict(zip(data.candidate_ids, data.scores))
        assert s["light"] == s["light-dup"]
        mid = next(x for x in data.results if x.id == "mid")
        assert mid.image_size_px == (896, 644) and mid.source_image_size_px == (1400, 1000) and mid.image_tokens == 736
        assert data.placement == "mmproj=GPU; llm_layers_gpu=20/28; output=CPU" and data.quant.startswith("Q6_K")
        assert res.visual.calls == [4]


@pytest.mark.parametrize("candidates,status,code", [
    ([{"id": f"i{i}", "image_base64": "AAAA"} for i in range(9)], 413, M.E_PAYLOAD_TOO_LARGE),
    ([{"id": "a", "image_base64": "@@broken@@"}], 422, M.E_IMAGE_DECODE_FAILED),
    ([{"id": "a", "image_base64": base64.b64encode(b"not an image").decode()}], 422, M.E_IMAGE_DECODE_FAILED),
    ([{"id": "a"}], 422, M.E_INPUT_INVALID),
    ([{"id": "a", "image_base64": "AAAA", "text": "подпись"}], 422, M.E_INPUT_INVALID),
])
def test_visual_negative_contract(candidates, status, code):
    client, res = make_client()
    with client:
        r = client.post("/v1/rerank/visual", json={"query": "q", "candidates": candidates}, headers=_hdr())
        assert r.status_code == status and r.json()["error"]["code"] == code
        assert res.visual.calls == []


def test_visual_oversized_image_is_413():
    client, _ = make_client()
    big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * (M.MAX_IMAGE_BYTES + 10)).decode()
    with client:
        r = client.post("/v1/rerank/visual", json={"query": "q", "candidates": [{"id": "a", "image_base64": big}]},
                        headers=_hdr())
        assert r.status_code == 413 and r.json()["error"]["code"] == M.E_PAYLOAD_TOO_LARGE


def test_visual_busy_is_503_with_retry_after():
    client, _ = make_client(visual_delay=0.6, visual_inflight=1, visual_queue=0)
    body = {"query": "q", "candidates": [{"id": "a", "image_base64": _png(100)}]}
    out = []
    with client:
        def call():
            out.append(client.post("/v1/rerank/visual", json=body, headers=_hdr()))

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
            time.sleep(0.15)
        for t in threads:
            t.join()
    codes = sorted(r.status_code for r in out)
    assert codes == [200, 503]
    busy = next(r for r in out if r.status_code == 503)
    assert busy.json()["error"]["code"] == M.E_BACKEND_BUSY and busy.json()["error"]["retryable"]
    assert busy.headers["Retry-After"]


def test_text_is_not_blocked_by_a_slow_visual_call():
    """No lock shared between backends: a text call finishes while a visual call is still running."""
    client, _ = make_client(visual_delay=1.5, text_delay=0.05)
    done = {}
    with client:
        def visual():
            client.post("/v1/rerank/visual", json={"query": "q", "candidates": [{"id": "a", "image_base64": _png(9)}]},
                        headers=_hdr())
            done["visual"] = time.monotonic()

        def text():
            time.sleep(0.3)
            r = client.post("/v1/rerank/text", json=_text_body(), headers=_hdr())
            assert r.status_code == 200
            done["text"] = time.monotonic()

        threads = [threading.Thread(target=visual), threading.Thread(target=text)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert done["text"] < done["visual"]


def test_status_shows_roles_not_addresses():
    client, _ = make_client()
    with client:
        r = client.get("/status", headers=_hdr())
        assert r.status_code == 200
        st = M.StatusResponse.model_validate(r.json())
        assert set(st.backends) == {"text", "visual"} and st.host_role == "EDGE"
        assert st.backends["visual"].license == "CC-BY-NC-4.0" and st.backends["visual"].status == "ready"
        assert st.limits["max_text_candidates"] == 24
        assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", r.text)


def test_status_reports_model_mismatch():
    client, res = make_client()
    res.visual.model_path = "/models/other.gguf"
    with client:
        st = client.get("/status", headers=_hdr()).json()
        assert st["backends"]["visual"]["status"] == "mismatch"


def test_config_requires_token():
    with pytest.raises(ValueError):
        create_app(GatewayConfig(auth_required=True, token=None, warmup=False))
