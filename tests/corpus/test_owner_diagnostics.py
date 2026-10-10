"""Closed startup diagnostics: no secret/path/payload text, primary vs cleanup kept apart."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from vkm_corpus.update import owner_diagnostics as od

SENTINEL = "SENTINEL-TOKEN-7f3a /srv/owner-private/models/w.gguf Bearer abcdef"


class _Clock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value


def _diag(clock=None):
    return od.StartupDiagnostics(kind="visual", identity={"recipe_sha256": "a" * 64, "code_sha256": "b" * 64,
                                                          "token": SENTINEL}, clock=clock or _Clock())


def _validation_error():
    class Strict(BaseModel):
        value: int
    try:
        Strict.model_validate({"value": SENTINEL})
    except ValidationError as exc:
        return exc
    raise AssertionError("validation error expected")


@pytest.mark.parametrize("exc,label", [
    (ValueError(SENTINEL), "ValueError"), (ConnectionRefusedError(SENTINEL), "ConnectionRefusedError"),
    (httpx.ConnectError(SENTINEL), "ConnectError"), (httpx.ReadTimeout(SENTINEL), "ReadTimeout"),
    (PermissionError(13, SENTINEL), "PermissionError"), (json.JSONDecodeError(SENTINEL, "x", 0), "JSONDecodeError"),
    (type("ForeignNamedError", (Exception,), {})(SENTINEL), "OTHER"),
    (type("Sub", (ValueError,), {})(SENTINEL), "ValueError")])
def test_exception_label_is_allowlisted_class_never_text(exc, label):
    assert od.classify(exc) == label
    assert od.classify(_validation_error()) == "ValidationError"


def test_deadline_projection_localizes_fence_step_and_witness_status():
    clock = _Clock(); diag = _diag(clock)
    for stage in ("GPU_RECIPE", "IMPLEMENTATION_FENCE", "CUDA_PREFLIGHT", "RECIPE_IDENTITY", "FILE_INVENTORY", "SPAWN"):
        diag.enter(stage); clock.value += .5
    diag.spawned(); diag.enter("NATIVE_LOAD_PROOF")
    diag.fence_failure("LISTENER", ConnectionRefusedError(SENTINEL)); clock.value += 30
    for _ in range(3):
        diag.fence_failure("WITNESS_STATUS", "ValueError", http_status=503); clock.value += 50
    diag.deadline(child_alive=True)
    diag.fail(ValueError(SENTINEL))
    value = diag.public()
    assert od.validate_public(value)
    assert value["terminal_stage"] == "NATIVE_LOAD_PROOF" and value["outcome"] == "LOAD_PROOF_DEADLINE"
    assert value["fence_step"] == "WITNESS_STATUS" and value["exception_class"] == "ValueError"
    assert value["cause_code"] == "NATIVE_LOAD_PROOF/LOAD_PROOF_DEADLINE/WITNESS_STATUS/ValueError"
    assert value["witness_http_status"] == [[503, 3]]
    assert value["child"] == {"spawned": True, "alive_at_terminal": True, "exit_code": None}
    assert ["LISTENER", "ConnectionRefusedError", 1, 3000, 3000] in value["fence_failures"]
    assert value["identity"] == {"code_sha256": "b" * 64, "recipe_sha256": "a" * 64}


def test_child_exit_is_distinct_from_deadline():
    diag = _diag(); diag.enter("NATIVE_LOAD_PROOF")
    diag.fence_failure("CHILD_PROCESS", ValueError(SENTINEL)); diag.child_exit(3); diag.fail(ValueError(SENTINEL))
    value = diag.public()
    assert value["outcome"] == "CHILD_EXITED_DURING_LOAD" and value["child"]["exit_code"] == 3
    assert value["child"]["alive_at_terminal"] is False


def test_cleanup_failure_never_replaces_the_primary_cause():
    diag = _diag(); diag.enter("CREDENTIAL_READ")
    diag.fail(PermissionError(SENTINEL))
    diag.cleanup_failure("BRIDGE_CLOSE", RuntimeError(SENTINEL))
    diag.enter("SERVING"); diag.fail(OSError(SENTINEL))
    value = diag.public()
    assert value["terminal_stage"] == "CREDENTIAL_READ" and value["exception_class"] == "PermissionError"
    assert value["secondary"] == [["BRIDGE_CLOSE", "RuntimeError"]]
    assert value["outcome"] == "FAILED" and value["fence_step"] is None


def test_public_line_and_private_receipt_never_contain_sentinel(tmp_path):
    diag = _diag(); diag.enter("NATIVE_LOAD_PROOF")
    diag.fence_failure("MAPPED_IMPLEMENTATION", ValueError(SENTINEL))
    diag.unexpected_mapping(["libextra.so.1", SENTINEL, "../../etc/passwd"])
    diag.placement({"blk.0": "HOST", "output": "GPU", SENTINEL: "GPU"})
    diag.cleanup_failure(SENTINEL, ValueError(SENTINEL))
    diag.fail(_validation_error())
    line = diag.line("visual_gpu_owner_startup_unavailable")
    marker, raw = line.split(" ", 1)
    assert marker == "visual_gpu_owner_startup_unavailable" and od.validate_public(json.loads(raw))
    name = diag.write_private(tmp_path.resolve())
    private = (tmp_path / name).read_text(encoding="ascii")
    for text in (line, private):
        assert "SENTINEL" not in text and "/home/" not in text and "Bearer" not in text and str(tmp_path) not in text
    data = json.loads(private)
    assert data["unexpected_mapped_basenames"] == ["INVALID", "libextra.so.1"]
    assert data["placement_groups"] == {"INVALID": "GPU", "blk.0": "HOST", "output": "GPU"}
    assert json.loads(raw)["unexpected_mapped_count"] == 2
    assert ["INVALID", "ValueError"] in json.loads(raw)["secondary"]
    if os.name == "posix":
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600


def test_private_receipt_requires_explicit_direct_directory_and_never_overwrites(tmp_path):
    diag = _diag()
    assert diag.write_private("relative-dir") is None
    assert diag.public()["secondary"] == [["PRIVATE_RECEIPT_WRITE", "ValueError"]]
    first = diag.write_private(tmp_path.resolve())
    assert first and diag.write_private(tmp_path.resolve()) is None
    assert diag.public()["secondary"][-1] == ["PRIVATE_RECEIPT_WRITE", "FileExistsError"]


def test_closed_vocabulary_and_bounds_hold_under_misuse():
    diag = _diag()
    diag.enter("FREE TEXT " + SENTINEL)
    for i in range(5000):
        diag.fence_failure("BOGUS" if i % 2 else "LISTENER", ValueError(), http_status=100 + i % 400)
    diag.child_exit(10_000)
    value = diag.public()
    assert od.validate_public(value) and value["terminal_stage"] == "INVALID"
    assert len(value["witness_http_status"]) <= od.MAX_STATUS_KINDS
    assert {f[0] for f in value["fence_failures"]} == {"INVALID", "LISTENER"}
    assert value["child"]["exit_code"] is None
    assert SENTINEL not in json.dumps(value)


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(extra=1), lambda v: v.update(cause_code="free text"),
    lambda v: v.update(outcome="PASS"), lambda v: v["child"].update(exit_code="3"),
    lambda v: v.update(fence_failures=[["LISTENER", "ValueError", 1, 0, "x"]]),
    lambda v: v["identity"].update(recipe_sha256=SENTINEL), lambda v: v.update(payload="raw")])
def test_validator_rejects_any_open_field(mutate):
    value = _diag().public(); assert od.validate_public(value)
    mutate(value)
    assert not od.validate_public(value)


def test_null_diagnostics_has_the_same_surface():
    for name in ("enter", "spawned", "fence_failure", "unexpected_mapping", "placement", "child_exit",
                 "deadline", "fail", "cleanup_failure", "ready"):
        assert callable(getattr(od.NULL, name))
