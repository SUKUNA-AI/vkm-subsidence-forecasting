"""Serving qualification covers actual database clients and native extensions."""
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from vkm_corpus.api.production import serving_dependencies_identity
from vkm_corpus.update import service_identity as identity


@pytest.mark.parametrize("package", ["psycopg-binary", "neo4j", "opensearch-py", "httpcore", "numpy",
                                     "pymorphy3", "pymorphy3-dicts-ru", "dawg2-python", "snowballstemmer"])
def test_actual_database_and_transitive_dependency_version_changes_identity(monkeypatch, package):
    before = serving_dependencies_identity()
    original = identity.metadata.distribution

    def distribution(name):
        dist = original(name)
        if name == package:
            # Compatible patch change still invalidates the qualified generation.
            pieces = dist.version.split(".")
            pieces[-1] = str(int(pieces[-1]) + 1)
            return SimpleNamespace(version=".".join(pieces), requires=dist.requires)
        return dist

    monkeypatch.setattr(identity.metadata, "distribution", distribution)
    if package == "psycopg-binary":
        # psycopg pins its binary extra to exactly the same version: a mismatch
        # must close admission rather than produce a usable alternate identity.
        with pytest.raises(ValueError, match="not satisfied"):
            serving_dependencies_identity()
    else:
        assert serving_dependencies_identity() != before


@pytest.mark.parametrize("package", ["psycopg-binary", "neo4j", "opensearch-py", "httpcore",
                                     "pymorphy3-dicts-ru", "dawg2-python"])
def test_missing_actual_client_or_native_transitive_dependency_blocks(monkeypatch, package):
    original = identity.metadata.distribution

    def distribution(name):
        if name == package:
            raise PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(identity.metadata, "distribution", distribution)
    with pytest.raises(PackageNotFoundError):
        serving_dependencies_identity()


def test_broken_psycopg_native_binary_import_blocks(monkeypatch):
    original = identity.importlib.import_module

    def import_module(name):
        if name == "psycopg_binary.pq":
            raise ImportError("synthetic missing native database extension")
        return original(name)

    monkeypatch.setattr(identity.importlib, "import_module", import_module)
    with pytest.raises(ImportError, match="native database extension"):
        serving_dependencies_identity()


def test_alternate_database_driver_implementation_is_not_qualified(monkeypatch):
    import psycopg.pq
    from vkm_corpus.update.generation import GenerationUnavailable
    monkeypatch.setattr(psycopg.pq, "__impl__", "python")
    with pytest.raises(GenerationUnavailable, match="binary database driver"):
        serving_dependencies_identity()


def test_corrupt_or_previously_degraded_query_morphology_is_not_qualified(monkeypatch):
    from vkm_corpus.api.production import require_navigation_runtime
    from vkm_corpus.navigation import concepts_query
    from vkm_corpus.update.generation import GenerationUnavailable
    require_navigation_runtime()
    original = concepts_query._morph
    monkeypatch.setattr(concepts_query, "_morph", lambda: SimpleNamespace(name="crude"))
    with pytest.raises(GenerationUnavailable, match="degraded"):
        require_navigation_runtime()
    def broken():
        raise ValueError("synthetic corrupt dictionary")
    monkeypatch.setattr(concepts_query, "_morph", broken)
    with pytest.raises(GenerationUnavailable, match="unavailable"):
        require_navigation_runtime()
    monkeypatch.setattr(concepts_query, "_morph", original)
