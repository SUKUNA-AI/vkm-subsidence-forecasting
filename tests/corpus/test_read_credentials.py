"""Host READ inventory: actual files, portable rejection and executable denial.

Only synthetic credentials and corpus fixtures are used. No symlink creation,
elevated Windows privileges, external service, model or production credential.
"""
from __future__ import annotations

import json
import os
import traceback

from fastapi.testclient import TestClient
import pytest

from vkm_corpus.api.app import ApiConfig, create_app
from vkm_corpus.api.fixtures import synthetic_service
from vkm_corpus.config import ConfigError, load_settings
from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.contracts.policy_store import SourcePolicyStore


BASE = "synthetic-base-read-" + "0" * 32
ALLOWED = "synthetic-allowed-read-" + "1" * 32
DENIED = "synthetic-denied-read-" + "2" * 32
WRITE = "synthetic-write-" + "3" * 32
PAYLOAD = "SYNTHETIC_PRIVATE_TOKEN_PAYLOAD_" + "9" * 32


def _file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    path.chmod(0o600)
    return path


def _json(path, value):
    return _file(path, json.dumps(value, ensure_ascii=False))


@pytest.fixture
def inventory(tmp_path):
    root = tmp_path / "host-operator"
    tokens = {"allowed": _file(root / "allowed.secret", ALLOWED),
              "denied": _file(root / "denied.secret", DENIED)}
    contexts = {
        "read": AccessContext(principal="base-reader", execution="LOCAL"),
        "allowed": AccessContext(principal="allowed-reader", execution="LOCAL",
                                  granted_classes={"PRIVATE_LOCAL_ONLY"}),
        "denied": AccessContext(principal="denied-reader", execution="CLOUD"),
        "write": AccessContext(principal="synthetic-writer", execution="LOCAL"),
    }
    context_file = _json(root / "contexts.json", {k: v.model_dump(mode="json") for k, v in contexts.items()})
    payload = {"schema": "vkm-api-read-credentials/1", "principals": {
        label: {"token_file": str(path)} for label, path in tokens.items()}}
    path = _json(root / "inventory.json", payload)
    settings = load_settings({"VKM_API_TOKEN": BASE, "VKM_API_WRITE_TOKEN": WRITE})
    return {"settings": settings, "path": path, "payload": payload, "tokens": tokens,
            "contexts": contexts, "context_file": context_file, "env": {
                "VKM_API_READ_CREDENTIALS_FILE": str(path), "VKM_ACCESS_CONTEXT_FILE": str(context_file)}}


def _load(inventory):
    return ApiConfig.from_settings(inventory["settings"], environ=inventory["env"])


def _rejected(inventory, caplog):
    with pytest.raises(ConfigError) as error:
        _load(inventory)
    diagnostic = "".join(traceback.format_exception(error.value)) + caplog.text
    for secret in (BASE, ALLOWED, DENIED, WRITE, PAYLOAD):
        assert secret not in diagnostic
    # Errors may identify the configuration kind, but not the local inventory
    # or token-file location, nor JSON parser fragments from its payload.
    assert str(inventory["path"]) not in str(error.value)
    assert str(inventory["tokens"]["allowed"]) not in str(error.value)
    return str(error.value)


def test_from_settings_loads_two_distinct_read_principals_from_host_files(inventory):
    cfg = _load(inventory)
    assert cfg.read_tokens == {BASE: "read", ALLOWED: "allowed", DENIED: "denied"}
    assert cfg.write_tokens == {WRITE: "write"}
    assert cfg.access_contexts == inventory["contexts"]
    assert cfg.access_contexts["allowed"] != cfg.access_contexts["denied"]


def test_configured_denied_principal_reaches_real_policy_denial_before_backend(inventory, tmp_path, monkeypatch, caplog):
    service, _, _ = synthetic_service(tmp_path / "synthetic-canon")
    sources = tuple(r["source_id"] for r in service.canon.query("SELECT source_id FROM sources"))
    policy = ResourcePolicy(access_class="PRIVATE_LOCAL_ONLY", experimental_role="INPUT",
                            policy_version="synthetic-v1", authority="synthetic-owner")
    path = _json(tmp_path / "policy.json", {"schema": "vkm-source-policy/1",
        "policies": {sid: policy.model_dump(mode="json") for sid in sources}})
    service.deps.access_policy = SourcePolicyStore(path, lambda: sources)
    app = create_app(service, _load(inventory))
    with TestClient(app) as client:
        allowed = client.get("/v1/source/VKM-SRC-001", headers={"Authorization": "Bearer " + ALLOWED})
        assert allowed.status_code == 200 and allowed.json()["ok"]
        def no_backend(*args, **kwargs):
            pytest.fail("denied principal reached a corpus content backend")
        monkeypatch.setattr(service, "get_source", no_backend)
        denied = client.get("/v1/source/VKM-SRC-001", headers={"Authorization": "Bearer " + DENIED})
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "FORBIDDEN"
        invalid = client.get("/v1/source/VKM-SRC-001", headers={"Authorization": "Bearer wrong"})
        assert invalid.status_code == 401 and invalid.json()["error"]["code"] == "UNAUTHORIZED"
        write = client.post("/v1/reprocess/page", headers={"Authorization": "Bearer " + ALLOWED},
            json={"target_id": "VKM-SRC-001:p0001", "reason": "synthetic credential test"})
        assert write.status_code == 403
    for token in (BASE, ALLOWED, DENIED, WRITE):
        assert token not in denied.text + invalid.text + write.text + caplog.text


@pytest.mark.parametrize("where", ["schema", "principals", "label", "token_file"])
def test_duplicate_json_fields_are_rejected_at_every_inventory_level(inventory, caplog, where):
    p = json.dumps(str(inventory["tokens"]["allowed"]))
    schema = '"schema":"vkm-api-read-credentials/1"'
    principal = '"allowed":{"token_file":' + p + '}'
    if where == "schema":
        raw = '{' + schema + ',' + schema + ',"principals":{' + principal + '}}'
    elif where == "principals":
        raw = '{' + schema + ',"principals":{' + principal + '},"principals":{' + principal + '}}'
    elif where == "label":
        raw = '{' + schema + ',"principals":{' + principal + ',' + principal + '}}'
    else:
        raw = '{' + schema + ',"principals":{"allowed":{"token_file":' + p + ',"token_file":' + p + '}}}'
    _file(inventory["path"], raw)
    assert _rejected(inventory, caplog) == "host READ credential inventory is invalid"


@pytest.mark.parametrize("collision", ["inventory_bearer", "base_other_label", "write_bearer", "unknown_context"])
def test_duplicate_bearer_write_collision_and_unknown_context_fail_closed(inventory, caplog, collision):
    if collision == "inventory_bearer":
        _file(inventory["tokens"]["denied"], ALLOWED)
    elif collision == "base_other_label":
        _file(inventory["tokens"]["allowed"], BASE)
    elif collision == "write_bearer":
        _file(inventory["tokens"]["allowed"], WRITE)
    else:
        value = inventory["payload"]
        value["principals"]["unconfigured"] = value["principals"].pop("denied")
        _json(inventory["path"], value)
    _rejected(inventory, caplog)


@pytest.mark.parametrize("fault", ["relative_inventory", "parent_inventory", "inventory_directory",
    "hardlinked_inventory", "relative_token", "parent_token", "token_directory", "hardlinked_token"])
def test_path_indirection_is_rejected_without_symlink_privileges(inventory, caplog, monkeypatch, fault):
    root = inventory["path"].parent
    if fault == "relative_inventory":
        monkeypatch.chdir(root)
        inventory["env"]["VKM_API_READ_CREDENTIALS_FILE"] = inventory["path"].name
    elif fault == "parent_inventory":
        (root / "subdirectory").mkdir()
        inventory["env"]["VKM_API_READ_CREDENTIALS_FILE"] = str(root / "subdirectory" / ".." / inventory["path"].name)
    elif fault == "inventory_directory":
        inventory["env"]["VKM_API_READ_CREDENTIALS_FILE"] = str(root)
    elif fault == "hardlinked_inventory":
        os.link(inventory["path"], root / "inventory-alias.json")
    elif fault == "hardlinked_token":
        os.link(inventory["tokens"]["allowed"], root / "token-alias.secret")
    else:
        if fault == "relative_token":
            monkeypatch.chdir(root)
            value = inventory["tokens"]["allowed"].name
        elif fault == "parent_token":
            (root / "subdirectory").mkdir()
            value = str(root / "subdirectory" / ".." / inventory["tokens"]["allowed"].name)
        else:
            value = str(root)
        inventory["payload"]["principals"]["allowed"]["token_file"] = value
        _json(inventory["path"], inventory["payload"])
    assert _rejected(inventory, caplog) == "host READ credential inventory is invalid"


@pytest.mark.parametrize("fault", ["short", "long", "byte_bound", "embedded_whitespace", "non_ascii", "json_payload"])
def test_invalid_token_payload_never_appears_in_startup_error(inventory, caplog, fault):
    values = {"short": "tiny", "long": PAYLOAD + "a" * 256, "byte_bound": PAYLOAD + "b" * 512,
              "embedded_whitespace": PAYLOAD + "\nsecret-tail", "non_ascii": PAYLOAD + "ы",
              "json_payload": json.dumps({"secret": PAYLOAD})}
    _file(inventory["tokens"]["allowed"], values[fault])
    assert _rejected(inventory, caplog) == "host READ credential inventory is invalid"


@pytest.mark.parametrize("fault", ["over_64k", "empty", "seventeen", "malformed", "unknown_field", "invalid_label",
    "wrong_schema", "non_object", "non_object_principals", "invalid_spec", "inline_token", "missing_token"])
def test_inventory_bounds_and_parser_errors_are_sanitized(inventory, caplog, fault):
    value = inventory["payload"]
    if fault == "over_64k":
        _file(inventory["path"], b" " * 65537)
    elif fault == "malformed":
        _file(inventory["path"], '{"schema": "' + PAYLOAD)
    else:
        if fault == "empty":
            value["principals"] = {}
        elif fault == "seventeen":
            value["principals"] = {"p" + str(i): {"token_file": str(inventory["tokens"]["allowed"])} for i in range(17)}
        elif fault == "unknown_field":
            value["token_payload"] = PAYLOAD
        elif fault == "invalid_label":
            value["principals"][PAYLOAD + "/not-a-label"] = value["principals"].pop("allowed")
        elif fault == "wrong_schema":
            value["schema"] = "vkm-api-read-credentials/0"
        elif fault == "non_object":
            value = [PAYLOAD]
        elif fault == "non_object_principals":
            value["principals"] = ["allowed", "denied"]
        elif fault == "invalid_spec":
            value["principals"]["allowed"] = PAYLOAD
        elif fault == "inline_token":
            value["principals"]["allowed"] = {"token": PAYLOAD}
        else:
            inventory["tokens"]["allowed"].unlink()
        _json(inventory["path"], value)
    assert _rejected(inventory, caplog) == "host READ credential inventory is invalid"


@pytest.mark.parametrize("count", [1, 16])
def test_principal_count_boundary_and_token_length_boundaries(inventory, count):
    root = inventory["path"].parent
    principals, contexts, expected = {}, dict(inventory["contexts"]), {BASE: "read"}
    for i in range(count):
        label = "boundary" + str(i)
        token = str(i).zfill(32) if i % 2 == 0 else str(i).zfill(256)
        path = _file(root / (label + ".secret"), token + "\n")
        principals[label] = {"token_file": str(path)}
        contexts[label] = AccessContext(principal=label, execution="LOCAL")
        expected[token] = label
    _json(inventory["path"], {"schema": "vkm-api-read-credentials/1", "principals": principals})
    _json(inventory["context_file"], {k: v.model_dump(mode="json") for k, v in contexts.items()})
    cfg = _load(inventory)
    assert cfg.read_tokens == expected
    assert cfg.write_tokens == {WRITE: "write"}


def test_absent_inventory_preserves_existing_single_principal_configuration(inventory):
    cfg = ApiConfig.from_settings(inventory["settings"], environ={})
    assert cfg.read_tokens == {BASE: "read"} and cfg.write_tokens == {WRITE: "write"}


def test_explicit_base_bearer_repetition_only_preserves_its_original_label(inventory):
    inventory["payload"]["principals"] = {"read": {"token_file": str(inventory["tokens"]["allowed"])}}
    _file(inventory["tokens"]["allowed"], BASE)
    _json(inventory["path"], inventory["payload"])
    cfg = _load(inventory)
    assert cfg.read_tokens == {BASE: "read"}


def test_operator_credential_cannot_be_an_additional_read_bearer(inventory, caplog):
    inventory["env"]["VKM_DEPLOYMENT_TOKEN_FILE"] = str(inventory["tokens"]["allowed"])
    with pytest.raises(ValueError, match="deployment credential must be separate") as error:
        _load(inventory)
    diagnostic = "".join(traceback.format_exception(error.value)) + caplog.text
    assert ALLOWED not in diagnostic and DENIED not in diagnostic


def test_native_file_failure_redacts_exception_payload_and_cause(inventory, monkeypatch, caplog):
    from vkm_corpus.update import admission
    original = admission._ordinary_bytes
    def fail_after_actual_read(path, limit):
        assert original(path, limit)  # a real inventory, no synthetic path bypass
        raise admission.AdmissionUnavailable(PAYLOAD + " " + str(inventory["path"]))
    monkeypatch.setattr(admission, "_ordinary_bytes", fail_after_actual_read)
    assert _rejected(inventory, caplog) == "host READ credential inventory is invalid"
