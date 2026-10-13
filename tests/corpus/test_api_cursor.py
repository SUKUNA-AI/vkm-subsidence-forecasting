"""Page cursors of ``/v1/search``, ``/v1/search/hybrid``, ``/v1/objects/query`` and ``/v1/source/{id}/pages``: a cursor
that is not an offset is INVALID_ARGUMENT (it used to restart the list silently); a page never goes past the end
``next_cursor`` reports; a cursor at or past the end is an empty page with ``CURSOR_PAST_END``. Synthetic canon and
fake backends (``search_size_fixture``: 20 hits)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")

from fastapi.testclient import TestClient  # noqa: E402

import search_size_fixture as F  # noqa: E402
from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.errors import ApiFailure  # noqa: E402
from vkm_corpus.api.fixtures import synthetic_service  # noqa: E402
from vkm_corpus.api.service import cursor_offset  # noqa: E402

READ = "cursor-read-token-0000000000000000000"
H = {"Authorization": f"Bearer {READ}"}
Q = "оседание земной поверхности"


@pytest.fixture()
def client(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path)
    service.deps.hybrid = F.rich_hybrid(canon)
    service.deps.search = F.rich_search(canon)
    return TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"})))


def _ok(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_cursor_offset():
    assert [cursor_offset(c) for c in (None, "", "0", "20", "007")] == [0, 0, 0, 20, 7]
    for bad in ("abc", "-1", " 1", "1.5", "²", "١"):
        with pytest.raises(ApiFailure) as exc:
            cursor_offset(bad)
        assert exc.value.code == "INVALID_ARGUMENT" and "next_cursor" in exc.value.hint


@pytest.mark.parametrize("cursor", ["abc", "-1", " 1", "1.5", "²"])
def test_a_cursor_that_is_not_an_offset_is_refused(client, cursor):
    for path, req in (("/v1/search", {"query": Q}), ("/v1/search/hybrid", {"query": Q}),
                      ("/v1/objects/query", {"kinds": ["FIGURE"]})):
        resp = client.post(path, headers=H, json={**req, "cursor": cursor})
        err = resp.json()["error"]
        assert resp.status_code == 400 and err["code"] == "INVALID_ARGUMENT", (path, resp.text)
        assert "next_cursor" in err["hint"]
    for path, params in (("/v1/search", {"q": Q}), ("/v1/search/hybrid", {"q": Q}),
                         ("/v1/source/VKM-SRC-001/pages", {})):
        resp = client.get(path, params={**params, "cursor": cursor}, headers=H)
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "INVALID_ARGUMENT", (path, resp.text)


def test_hybrid_pages_end_where_next_cursor_says(client):
    req = {"query": Q, "limit": 4, "candidates": 10}                 # 20 fused hits, the answer ends at 10
    seen, cursor = [], None
    for _ in range(10):
        body = _ok(client.post("/v1/search/hybrid", headers=H, json={**req, "cursor": cursor}))
        seen += [it["record"]["rank"] for it in body["items"]]
        assert not any(w["code"] == "CURSOR_PAST_END" for w in body["meta"]["warnings"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == list(range(1, 11))                                  # the last page is cut at the end (9, 10)
    for cursor in ("10", "15", "100"):
        body = _ok(client.post("/v1/search/hybrid", headers=H, json={**req, "cursor": cursor}))
        assert body["items"] == [] and body["next_cursor"] is None
        [past] = [w for w in body["meta"]["warnings"] if w["code"] == "CURSOR_PAST_END"]
        assert past["count"] == 10
    first = _ok(client.post("/v1/search/hybrid", headers=H, json={"query": Q, "cursor": "0"}))
    assert len(first["items"]) == 20 and first["next_cursor"] is None   # "0" is the first page


def test_bm25_objects_and_pages_past_the_end_are_empty_with_a_warning(client):
    for path, req in (("/v1/search", {"query": Q, "limit": 10, "cursor": "20"}),
                      ("/v1/objects/query", {"kinds": ["FIGURE"], "cursor": "5"})):
        body = _ok(client.post(path, headers=H, json=req))
        assert body["items"] == [] and body["next_cursor"] is None
        assert [w["code"] for w in body["meta"]["warnings"]] == ["CURSOR_PAST_END"]
    pages = _ok(client.get("/v1/source/VKM-SRC-001/pages", params={"cursor": "50"}, headers=H))
    assert pages["items"] == [] and pages["meta"]["warnings"][0]["code"] == "CURSOR_PAST_END"
    middle = _ok(client.post("/v1/search", headers=H, json={"query": Q, "limit": 10, "cursor": "10"}))
    assert len(middle["items"]) == 10 and "CURSOR_PAST_END" not in {w["code"] for w in middle["meta"]["warnings"]}
