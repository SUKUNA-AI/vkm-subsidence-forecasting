"""Native vocabulary and gateway request tokenizer are different loaded objects."""
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from vkm_corpus.update import remote_models as rm
from vkm_corpus.update.remote_services import _body, rerank_identity
from test_remote_services import H, payloads


def test_legacy_text_proof_keeps_explicit_standalone_default():
    value = payloads()["rerank"]["models"]["text"]
    del value["tokenizer_binding"]
    assert rm.NativeModelProof.model_validate(value).tokenizer_binding == "STANDALONE_LOADED"


@pytest.mark.parametrize("change", ["text_embedded", "visual_other_bytes", "unknown_binding"])
def test_embedded_binding_cannot_claim_an_unloaded_tokenizer(change):
    value = copy.deepcopy(payloads()["rerank"]["models"]["visual"])
    if change == "text_embedded": value["kind"] = "text"
    if change == "visual_other_bytes": value["resources"]["tokenizer"] = "b" * 64
    if change == "unknown_binding": value["tokenizer_binding"] = "FILE_NEAR_MODEL"
    with pytest.raises(ValueError): rm.NativeModelProof.model_validate(value)


def test_visual_gateway_json_is_separately_bound_not_native_vocabulary():
    value = payloads()["rerank"]
    value["resources"]["visual_tokenizer"] = "b" * 64
    result = rerank_identity(value, H)
    assert result.resources["visual_tokenizer"] == "b" * 64
    assert result.resources["visual_model_tokenizer"] == H
    assert result.resources["visual_model_weights"] == H


@pytest.mark.parametrize("change", ["visual_legacy", "visual_standalone", "text_gateway_mismatch"])
def test_qualified_service_rejects_legacy_visual_file_attestation(change):
    value = payloads()["rerank"]
    if change == "visual_legacy": del value["models"]["visual"]["tokenizer_binding"]
    if change == "visual_standalone": value["models"]["visual"]["tokenizer_binding"] = "STANDALONE_LOADED"
    if change == "text_gateway_mismatch": value["resources"]["text_tokenizer"] = "b" * 64
    with pytest.raises(ValueError): rerank_identity(value, H)


@pytest.mark.parametrize("raw", [b'{"kind":"text","kind":"visual"}',
    b'{"resources":{"weights":"a","weights":"b"}}', b'{"epoch":NaN}',
    b'{"value":Infinity}', b'{"value":"\xff"}', b'{'])
def test_identity_parser_rejects_ambiguous_or_invalid_protocol(raw):
    with pytest.raises(ValueError): rm.native_identity_json(raw)


def test_gateway_body_does_not_silently_accept_duplicate_ready():
    value = json.dumps(payloads()["rerank"]).encode()
    raw = b'{"status":"CLOSED",' + value[1:]
    with pytest.raises(ValueError): _body(httpx.Response(200, content=raw))


def test_same_inference_client_rejects_duplicate_native_model_proof():
    value = json.dumps(payloads()["rerank"]["models"]["visual"]).encode()
    raw = b'{"kind":"text",' + value[1:]
    async def run():
        async with httpx.AsyncClient(base_url="http://native.test", transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=raw))) as client:
            backend = SimpleNamespace(client=lambda: client)
            with pytest.raises(ValueError):
                await rm.read_backend_model_identity(backend, "visual", "synthetic-token")
    asyncio.run(run())
