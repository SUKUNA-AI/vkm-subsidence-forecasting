import pytest

from vkm_corpus.update.contracts import ServiceIdentity
from vkm_corpus.update.generation import GenerationCoordinator, GenerationUnavailable
from test_deployment_lifecycle import manifest
from test_deployment_lifecycle import rig


def service():
    return ServiceIdentity(service="CONTROL", instance_sha256="a" * 64, code_sha256="b" * 64,
        dependencies_sha256="c" * 64, config_sha256="d" * 64, endpoint_sha256="e" * 64,
        runtime_sha256="f" * 64, capabilities=("JOB_STATUS",))


def test_required_service_identity_cannot_be_omitted_or_echoed_as_component():
    generation = manifest("new").model_copy(update={"services": (service(),)})
    observed = {c.component: c.model_dump(mode="json") for c in generation.components}
    for services in (None, {}, {"OTHER": service().model_dump(mode="json")}):
        with pytest.raises(GenerationUnavailable, match="service identities omitted"):
            GenerationCoordinator.verify(generation, observed, services)
    GenerationCoordinator.verify(generation, observed, {"CONTROL": service().model_dump(mode="json")})
    altered = service().model_copy(update={"runtime_sha256": "1" * 64})
    with pytest.raises(GenerationUnavailable, match="service identity differs"):
        GenerationCoordinator.verify(generation, observed, {"CONTROL": altered.model_dump(mode="json")})


def test_empty_service_manifest_cannot_hide_an_observed_live_service():
    generation = manifest("new")
    observed = {c.component: c.model_dump(mode="json") for c in generation.components}
    with pytest.raises(GenerationUnavailable, match="service identities omitted"):
        GenerationCoordinator.verify(generation, observed, {"CONTROL": service().model_dump(mode="json")})
    GenerationCoordinator.verify(generation, observed, {})


def test_deployment_rejects_unlisted_service_before_changing_selectors(tmp_path):
    controller, old, new, state, calls, _ = rig(tmp_path)
    controller.service_observer = lambda: {"CONTROL": service().model_dump(mode="json")}
    with pytest.raises(GenerationUnavailable, match="service identities omitted"):
        controller.plan(new, "update")
    assert calls == [] and controller._current().sha256 == old.sha256
    assert not (controller.root / "MAINTENANCE").exists()
