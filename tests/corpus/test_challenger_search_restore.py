"""A failed candidate must not prevent an exact healthy previous restore."""
import pytest

from vkm_corpus.update.remote_search import SearchBundleSwitch, RemoteSearchError
from test_remote_search import pair


@pytest.mark.parametrize("already_restored", [False, True])
def test_exact_previous_search_restore_does_not_depend_on_failed_candidate_content(already_restored):
    client, previous, candidate = pair()
    operation = SearchBundleSwitch(previous, candidate, fence=lambda: None)
    operation.apply()
    if already_restored:
        operation.restore()
    # Native UUIDs and the exact alias set are unchanged. Only the rejected
    # candidate data changes; the previous frozen bundle remains fully valid.
    bad = client.data[candidate.identity.spec.indices["pages"]]
    bad["docs"]["id-1"]["value"] = "synthetic rejected candidate corruption"
    bad["seq"] += 1
    previous.observe(selected=False)
    writes = len(client.writes)
    result = operation.restore()
    assert result["status"] == "PASS"
    assert result["changed"] is (not already_restored)
    assert len(client.writes) == writes + (not already_restored)
    assert client.aliases == {k: list(v) for k, v in previous.identity.spec.aliases().items()}
    previous.observe()


@pytest.mark.parametrize("fault", ["candidate_uuid", "previous_content", "endpoint", "mixed_aliases", "client"])
def test_restore_preserves_ownership_and_healthy_previous_checks(fault):
    client, previous, candidate = pair()
    operation = SearchBundleSwitch(previous, candidate, fence=lambda: None)
    operation.apply()
    if fault == "candidate_uuid":
        data = client.data[candidate.identity.spec.indices["pages"]]
        data["uuid"] = data["settings"]["index.uuid"] = "unrelated-recreated-index"
    elif fault == "previous_content":
        data = client.data[previous.identity.spec.indices["pages"]]
        data["seq"] += 1
    elif fault == "endpoint":
        client.transport.hosts[0]["port"] += 1
    elif fault == "mixed_aliases":
        client.aliases["vkm-pages"] = [previous.identity.spec.indices["pages"]]
    else:
        import copy
        # The same expected UUIDs do not authorize replacing the bound client.
        candidate.client = previous.client = copy.deepcopy(client)
    writes = len(client.writes)
    with pytest.raises(RemoteSearchError):
        operation.restore()
    assert len(client.writes) == writes


def test_restore_rechecks_healthy_previous_after_last_writer_fence():
    client, previous, candidate = pair()
    operation = SearchBundleSwitch(previous, candidate, fence=lambda: None)
    operation.apply()
    calls = []

    def fence():
        calls.append(1)
        if len(calls) == 2:
            client.data[previous.identity.spec.indices["pages"]]["seq"] += 1

    operation.fence = fence
    writes = len(client.writes)
    with pytest.raises(RemoteSearchError):
        operation.restore()
    assert len(client.writes) == writes
