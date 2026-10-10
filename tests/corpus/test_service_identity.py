"""Narrow installed service profiles: missing capabilities never qualify."""
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from vkm_corpus.update import service_identity as s


@pytest.mark.parametrize("profile", [s.RETRIEVAL, s.RERANK])
def test_current_service_inventory_has_full_active_closure_without_api(profile):
    code = s.code_inventory(profile)
    deps = s.dependency_inventory(profile)
    assert "vkm_corpus.update.native_files" in code["modules"]
    assert "vkm_corpus.api.production" not in code["modules"]
    assert {"tokenizers", "huggingface-hub", "hf-xet", "pydantic-core", "httpcore"} <= deps["packages"].keys()
    assert not {"duckdb", "mcp", "torch", "transformers", "safetensors"} & deps["packages"].keys()
    assert ("pyarrow" in deps["packages"]) == (profile == s.RETRIEVAL)
    assert ("pillow" in deps["packages"]) == (profile == s.RERANK)


@pytest.mark.parametrize("package", ["tokenizers", "hf-xet", "pydantic-core"])
def test_missing_direct_or_transitive_runtime_package_blocks(monkeypatch, package):
    original = s.metadata.distribution
    def distribution(name):
        if name == package:
            raise PackageNotFoundError(name)
        return original(name)
    monkeypatch.setattr(s.metadata, "distribution", distribution)
    with pytest.raises(PackageNotFoundError):
        s.service_dependencies_identity(s.RETRIEVAL)


def test_broken_optional_native_import_blocks(monkeypatch):
    original = s.importlib.import_module
    def import_module(module):
        if module == "tokenizers":
            raise ImportError("synthetic missing native extension")
        return original(module)
    monkeypatch.setattr(s.importlib, "import_module", import_module)
    with pytest.raises(ImportError, match="native extension"):
        s.dependency_inventory(s.RERANK)


def test_unsupported_profile_or_incompatible_version_blocks(monkeypatch):
    with pytest.raises(ValueError, match="unsupported"):
        s.service_code_identity("RETRIEVAL_HF_TORCH_UNQUALIFIED")
    original = s.metadata.distribution
    monkeypatch.setattr(s.metadata, "distribution", lambda name:
        SimpleNamespace(version="0.1", requires=[]) if name == "tokenizers" else original(name))
    with pytest.raises(ValueError, match="not satisfied"):
        s.service_dependencies_identity(s.RETRIEVAL)


def test_missing_code_module_blocks(monkeypatch):
    original = s.importlib.util.find_spec
    monkeypatch.setattr(s.importlib.util, "find_spec", lambda name:
        None if name == "vkm_corpus.update.native_files" else original(name))
    with pytest.raises(ValueError, match="unavailable"):
        s.code_inventory(s.RERANK)


def test_auto_transport_presence_is_pinned_and_missing_metadata_blocks(monkeypatch):
    before = s.service_dependencies_identity(s.RERANK)
    spec, load, distribution = s.importlib.util.find_spec, s.importlib.import_module, s.metadata.distribution
    monkeypatch.setattr(s.importlib.util, "find_spec", lambda name: object() if name == "httptools" else spec(name))
    monkeypatch.setattr(s.importlib, "import_module", lambda name: object() if name == "httptools" else load(name))
    monkeypatch.setattr(s.metadata, "distribution", lambda name:
        SimpleNamespace(version="0.7.1", requires=[]) if name == "httptools" else distribution(name))
    assert s.service_dependencies_identity(s.RERANK) != before
    def missing(name):
        if name == "httptools":
            raise PackageNotFoundError(name)
        return distribution(name)
    monkeypatch.setattr(s.metadata, "distribution", missing)
    with pytest.raises(PackageNotFoundError):
        s.dependency_inventory(s.RERANK)


def test_relevant_code_and_dependency_change_identity(tmp_path, monkeypatch):
    original = s._source_path
    replacement = tmp_path / "gateway.py"
    replacement.write_bytes(b"original synthetic source")
    monkeypatch.setattr(s, "_source_path", lambda name: replacement
        if name == "vkm_corpus.retrieval.gateway" else original(name))
    before = s.service_code_identity(s.RERANK)
    replacement.write_bytes(b"changed synthetic source")
    assert s.service_code_identity(s.RERANK) != before
    before = s.service_dependencies_identity(s.RERANK)
    distribution = s.metadata.distribution
    monkeypatch.setattr(s.metadata, "distribution", lambda name:
        SimpleNamespace(version="0.23.3", requires=distribution(name).requires)
        if name == "tokenizers" else distribution(name))
    assert s.service_dependencies_identity(s.RERANK) != before


def test_dependency_extras_and_environment_markers_are_not_ignored(monkeypatch):
    # Exercise PEP508 extras without importing or installing a model library.
    monkeypatch.setitem(s.PROFILES, "TEST", s.ServiceProfile((), (("demo[encoder]==1", "json"),)))
    distributions = {
        "demo": SimpleNamespace(version="1", requires=["torch==2; extra == 'encoder'",
            "absent==9; python_version < '2'"]),
        "torch": SimpleNamespace(version="2", requires=["safetensors==3"]),
        "safetensors": SimpleNamespace(version="3", requires=[]),
    }
    monkeypatch.setattr(s.metadata, "distribution", lambda name: distributions[name])
    assert set(s.dependency_inventory("TEST")["packages"]) == {"demo", "torch", "safetensors"}
    distributions["safetensors"].version = "4"
    with pytest.raises(ValueError, match="not satisfied"):
        s.dependency_inventory("TEST")


def test_other_encoder_or_backend_cannot_use_current_qualified_profile():
    with pytest.raises(ValueError, match="runtime profile"):
        s.require_retrieval_profile({"late": SimpleNamespace()}, SimpleNamespace(processes={"late": None}))
    with pytest.raises(ValueError, match="runtime profile"):
        s.require_rerank_profile(SimpleNamespace(text=object(), visual=object()))
