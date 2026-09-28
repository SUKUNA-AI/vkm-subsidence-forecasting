"""Rerank contract vkm.rerank/1 (agent F; H-13, H-45, CP-18): pydantic models, limits, response invariants."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError  # noqa: E402

from vkm_corpus.retrieval import models as M  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SHA = hashlib.sha256(b"x").hexdigest()


def _text_req(n: int = 3, **kw) -> M.TextRerankRequest:
    return M.TextRerankRequest(query="оседание", candidates=[{"id": f"c{i}", "text": f"текст {i}"} for i in range(n)],
                               **kw)


def test_text_request_valid_and_limits():
    req = _text_req(24, top_n=5)
    req.check_limits()
    assert req.candidates[0].id == "c0" and req.top_n == 5
    too_many = _text_req(25)            # schema accepts: the count limit is a 413, not a 422 (H-13)
    with pytest.raises(M.RerankLimitError) as exc:
        too_many.check_limits()
    assert exc.value.limit == 24 and exc.value.actual == 25


def test_visual_request_limits():
    cands = [{"id": f"i{i}", "image_base64": "iVBORw0KGgo="} for i in range(9)]
    req = M.VisualRerankRequest(query="разрез", candidates=cands)
    with pytest.raises(M.RerankLimitError):
        req.check_limits()
    M.VisualRerankRequest(query="разрез", candidates=cands[:8]).check_limits()


@pytest.mark.parametrize("payload", [
    {"query": " ", "candidates": [{"id": "a", "text": "t"}]},                       # blank query
    {"query": "q", "candidates": []},                                               # no candidates
    {"query": "q", "candidates": [{"id": "a", "text": "t"}, {"id": "a", "text": "u"}]},   # duplicate ids
    {"query": "q", "candidates": [{"id": "a b", "text": "t"}]},                     # whitespace in id
    {"query": "q", "candidates": [{"id": "a", "text": "   "}]},                     # blank text
    {"query": "q", "candidates": [{"id": "a", "text": "t", "extra": 1}]},           # extra field
    {"query": "q", "candidates": [{"id": "a", "text": "t"}], "top_n": 0},           # top_n < 1
    {"query": "q", "candidates": [{"id": "a", "text": "t"}], "request_id": "a b"},  # bad request id
    {"query": "q", "candidates": [{"id": "a", "text": "t"}], "truncate_to_tokens": 3},
])
def test_text_request_rejects(payload):
    with pytest.raises(ValidationError):
        M.TextRerankRequest.model_validate(payload)


def test_visual_candidate_has_no_text_field():
    with pytest.raises(ValidationError):
        M.VisualRerankRequest.model_validate(
            {"query": "q", "candidates": [{"id": "a", "image_base64": "AAAA", "text": "подпись"}]})
    with pytest.raises(ValidationError):
        M.VisualRerankRequest.model_validate({"query": "q", "candidates": [{"id": "a"}]})


def _response(**kw) -> dict:
    base = dict(kind="text", request_id="r1", model_id="m", model_revision="rev", quant="fp16", placement="GPU",
                backend="b", backend_version="v", model_config_sha256=SHA, score_semantics="s", license="CC-BY-NC-4.0",
                candidate_ids=["b", "a"], scores=[0.9, 0.1],
                results=[{"id": "b", "rank": 1, "score": 0.9, "input_index": 1},
                         {"id": "a", "rank": 2, "score": 0.1, "input_index": 0}],
                n_candidates=2, top_n=2, query_sha256=SHA, input_sha256=SHA,
                latency_ms={"total": 1.0, "preprocess": 0.1, "queue": 0.0, "backend": 0.8},
                gateway_version="0.1.0", created_at="2026-09-28T00:00:00Z")
    base.update(kw)
    return base


def test_response_invariants():
    resp = M.RerankResponse.model_validate(_response())
    assert resp.layer == "SERVICE" and resp.review_status == "NOT_APPLICABLE" and resp.contract == "vkm.rerank/1"
    with pytest.raises(ValidationError):   # misaligned
        M.RerankResponse.model_validate(_response(scores=[0.9]))
    with pytest.raises(ValidationError):   # results out of order
        M.RerankResponse.model_validate(_response(candidate_ids=["a", "b"]))
    with pytest.raises(ValidationError):   # not a sha256
        M.RerankResponse.model_validate(_response(query_sha256="abc"))


def test_error_codes_have_statuses():
    assert M.ERROR_STATUS[M.E_PAYLOAD_TOO_LARGE] == 413 and M.ERROR_STATUS[M.E_INPUT_INVALID] == 422
    assert M.E_BACKEND_BUSY in M.RETRYABLE and M.E_INPUT_INVALID not in M.RETRYABLE
    assert M.CLIENT_TIMEOUT_VISUAL_S >= 120 and M.CLIENT_TIMEOUT_VISUAL_S > M.VISUAL_TIMEOUT_S
    assert M.CLIENT_TIMEOUT_TEXT_S > M.TEXT_TIMEOUT_S >= 120      # serial text service: queue inside it


def test_m0_limits_are_consistent():
    # 768 visual tokens at most: max_pixels / (28·28); one image prompt must fit the llama-server ubatch of 1024
    assert M.M0_MAX_PIXELS // (M.M0_PATCH_FACTOR ** 2) == 768
    assert 768 + 2 + 16 + M.VISUAL_QUERY_MAX_TOKENS <= 1024
    assert M.MAX_TEXT_CANDIDATES == 24 and M.MAX_VISUAL_CANDIDATES == 8 and M.TEXT_TOKEN_BUDGET == 4096


def test_no_enums_declared_in_retrieval():
    """H-01: closed vocabularies (StrEnum/Enum) are declared only in vkm_corpus.contracts."""
    offenders = []
    for path in (ROOT / "src" / "vkm_corpus" / "retrieval").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    if name in {"StrEnum", "Enum", "IntEnum"}:
                        offenders.append(f"{path.name}:{node.name}")
    assert offenders == []
