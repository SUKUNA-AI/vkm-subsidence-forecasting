"""Registry lookups through the VKM API and the MCP read server (agent Г, 13.10): ``GET /v1/works`` /
``find_works`` (works of an author by name key over the synthetic canon: case forms, the other script, initials,
argument errors) and ``GET /v1/entities`` / ``find_entities`` (the entity index of a published catalogue pack: a
number with its type word, sites kept apart, paging, a pack without the table, no pack), plus the hybrid switches
``author_route`` / ``entity_route`` passed to the backend. Synthetic values only."""
from __future__ import annotations

import asyncio
import csv

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.errors import ApiFailure  # noqa: E402
from vkm_corpus.api.fixtures import FakeHybrid, hybrid_hits, synthetic_service  # noqa: E402
from vkm_corpus.catalogues import pack as cpack  # noqa: E402
from vkm_corpus.catalogues.store import CatalogueStore  # noqa: E402
from vkm_corpus.search import entities as E  # noqa: E402

READ = "read-token-for-registry-tests-00000000"
H = {"Authorization": f"Bearer {READ}"}


def _entity_rows():
    def row(rid, kind, eid, number, key, site, pid, records):
        return {**{c: "" for c in E.COLUMNS}, "entity_row_id": rid, "entity_kind": kind, "entity_id": eid,
                "entity_label": number, "number": number, "number_key": key, "site": site,
                "site_attribution": "DERIVATION", "source_id": pid[:11], "page_id": pid,
                "page_index": str(int(pid[-4:])), "page_basis": "LOCATOR_PAGE",
                "catalogue": "evidence/boreholes/borehole_catalog.csv", "record_ids": records, "status": "FACT",
                "identity_note": f"catalogue object {eid}"}
    return [row("ENT-a1", "BOREHOLE", "BH-SOL-75", "75", "75", "СКРУ-1 (по чтению плана)", "VKM-SRC-001:p0001",
                "BH-SOL-75;PK-1"),
            row("ENT-a2", "BOREHOLE", "BH-SOL-75", "75", "75", "СКРУ-1 (по чтению плана)", "VKM-SRC-001:p0002",
                "BH-SOL-75"),
            row("ENT-a3", "BOREHOLE", "BH-BIB-75", "75", "75", "библиография", "VKM-SRC-002:p0001", "BH-BIB-75"),
            row("ENT-a4", "PANEL", "SKRU1:PAN13:9VP", "9ВП", "9вп", "SKRU1", "VKM-SRC-001:p0003", "C4C-1")]


def _publish(tmp_path, rows, name="entity_index.csv"):
    repo = tmp_path / "public"
    path = repo / "catalogues" / "entities" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    root = tmp_path / "root"
    cpack.pack(repo, tmp_path / "pack", commit="cd" * 20)
    cpack.publish(root, tmp_path / "pack")
    return CatalogueStore(root)


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path / "canon")
    service.deps.catalogues = _publish(tmp_path, _entity_rows())
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}))
    return service, canon, app


# ---------------------------------------------------------------------------------------------------- works
def test_find_works_by_name_key(env):
    service, _canon, _app = env
    res = service.find_works("Альфаева")                       # genitive of «Альфаев А.А.»
    summary = res.item.record
    assert res.item.envelope.object_kind == "AUTHOR_WORKS" and summary["total_works"] == 1
    assert summary["authors"][0]["name_display"] == "Альфаев А.А." and summary["authors"][0]["method"] == "INFLECTED"
    assert summary["parsed"] == {"surname": "альфаева", "initials": None, "script": "CYRL"}
    (work,) = res.items
    assert work.envelope.object_kind == "WORK" and work.envelope.object_id == "VKM-WRK-001"
    rec = work.record
    assert rec["source_ids"] == ["VKM-SRC-001", "VKM-SRC-002"] and rec["work_copy_count"] == 2
    assert rec["match_method"] == "INFLECTED" and rec["matched_authors"][0]["ordinal"] == 1
    assert [a["name_as_listed"] for a in rec["authors"]] == ["Альфаев А.А.", "Бетин Б.Б."]
    assert service.find_works("Alfaev").items[0].record["match_method"] == "TRANSLIT"
    assert service.find_works("Бетин Б.Б.").items[0].record["matched_authors"][0]["ordinal"] == 2
    assert service.find_works("Бетин В.").items == []                           # other initials: no cluster
    assert service.find_works("Гамов").item.record["total_works"] == 0
    with pytest.raises(ApiFailure) as exc:
        service.find_works("x")
    assert exc.value.code == "INVALID_ARGUMENT"


def test_find_works_http_and_cursor(env):
    _service, _canon, app = env
    client = TestClient(app)
    body = client.get("/v1/works", params={"author": "Альфаев А.А.", "limit": 1}, headers=H).json()
    assert body["ok"] and body["items"][0]["envelope"]["object_id"] == "VKM-WRK-001" and body["next_cursor"] is None
    assert client.get("/v1/works", params={"author": "А"}, headers=H).json()["ok"] is False     # too short
    past = client.get("/v1/works", params={"author": "Альфаев", "cursor": "5"}, headers=H).json()
    assert past["items"] == [] and past["meta"]["warnings"][0]["code"] == "CURSOR_PAST_END"


# ---------------------------------------------------------------------------------------------------- entities
def test_find_entities_number_with_type_word_and_sites_apart(env):
    service, _canon, _app = env
    res = service.find_entities(number="скв. 75")
    rec = res.item.record
    assert rec["query"]["kind"] == "BOREHOLE" and rec["query"]["number"] == "75" and rec["total"] == 3
    assert [r["page_id"] for r in rec["rows"]] == ["VKM-SRC-001:p0001", "VKM-SRC-001:p0002", "VKM-SRC-002:p0001"]
    assert rec["rows"][0]["page_index"] == 1 and rec["rows"][0]["record_ids"] == ["BH-SOL-75", "PK-1"]
    assert [(o["entity_id"], o["n_pages"]) for o in rec["objects"]] == [("BH-SOL-75", 2), ("BH-BIB-75", 1)]
    assert [w.code for w in res.warnings] == ["ENTITY_SEVERAL_OBJECTS"] and res.warnings[0].count == 2
    env_ = res.item.envelope
    assert env_.object_kind == "ENTITY_INDEX" and env_.layer == "PROJECTION" and env_.origin == "DERIVED"
    assert env_.projection.engine == "catalogues" and env_.projection.build_id == rec["pack_id"]
    assert env_.review_status == "AUTO_EXTRACTED_UNREVIEWED" and rec["navigation_only"] is True
    one = service.find_entities(kind="BOREHOLE", number="75", site="СКРУ-1").item.record
    assert one["total"] == 2 and {o["entity_id"] for o in one["objects"]} == {"BH-SOL-75"}
    assert service.find_entities(kind="PANEL", number="9 ВП").item.record["rows"][0]["entity_id"] == "SKRU1:PAN13:9VP"
    assert service.find_entities(page_id="VKM-SRC-001:p0003").item.record["total"] == 1
    paged = service.find_entities(number="75", limit=2)
    assert len(paged.item.record["rows"]) == 2 and paged.next_cursor == "2"
    last = service.find_entities(number="75", limit=2, cursor="2")
    assert len(last.item.record["rows"]) == 1 and last.next_cursor is None


def test_find_entities_argument_errors_and_missing_index(env, tmp_path):
    service, _canon, _app = env
    for kw in ({}, {"kind": "WELLHEAD", "number": "75"}, {"source_id": "SRC-1"}):
        with pytest.raises(ApiFailure) as exc:
            service.find_entities(**kw)
        assert exc.value.code in ("INVALID_ARGUMENT", "INVALID_ID")
    other = tmp_path / "other"
    service.deps.catalogues = _publish(other, [{"model_id": "MM-X-001", "name_ru": "x"}], name="model_registry.csv")
    service.deps.registry = None
    with pytest.raises(ApiFailure) as exc:
        service.find_entities(number="75")
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE" and "entity index" in exc.value.message
    service.deps.catalogues = CatalogueStore(tmp_path / "nothing-published")
    service.deps.registry = None
    with pytest.raises(ApiFailure) as exc:
        service.find_entities(number="75")
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE"


def test_find_entities_http(env):
    _service, _canon, app = env
    client = TestClient(app)
    body = client.get("/v1/entities", params={"kind": "LINE", "number": "75"}, headers=H).json()
    assert body["ok"] and body["item"]["record"]["total"] == 0                     # no profile line 75
    body = client.get("/v1/entities", params={"number": "скв. 75", "site": "библиография"}, headers=H).json()
    assert [r["entity_id"] for r in body["item"]["record"]["rows"]] == ["BH-BIB-75"]
    bad = client.get("/v1/entities", headers=H).json()
    assert bad["ok"] is False and bad["error"]["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------------------------------- MCP
def test_mcp_tools(env):
    pytest.importorskip("mcp")
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    _service, _canon, app = env
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def go():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            works = await c.call_tool("find_works", {"author": "Альфаева", "limit": 5})
            ents = await c.call_tool("find_entities", {"kind": "BOREHOLE", "number": "75", "limit": 5})
            bad = await c.call_tool("find_entities", {"kind": "WELLHEAD"})
            return tools, works, ents, bad

    tools, works, ents, bad = asyncio.run(go())
    assert tools["find_works"].annotations.read_only_hint and tools["find_entities"].annotations.read_only_hint
    assert works.is_error is False and works.structured_content["items"][0]["envelope"]["object_id"] == "VKM-WRK-001"
    rec = ents.structured_content["item"]["record"]
    assert ents.is_error is False and rec["total"] == 3 and len(rec["objects"]) == 2
    assert bad.is_error                                                              # schema: unknown kind


# ---------------------------------------------------------------------------------------------------- hybrid
def test_hybrid_switches_reach_the_backend(env):
    service, canon, _app = env
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    service.search_hybrid("работы Альфаева", ["PAGE"], {}, 5, None, author_route=False, entity_route=True)
    assert service.deps.hybrid.requests[-1]["author_route"] is False
    assert service.deps.hybrid.requests[-1]["entity_route"] is True
    service.search_hybrid("оседание", ["PAGE"], {}, 5, None)
    assert "author_route" not in service.deps.hybrid.requests[-1]


def test_brief_view_shows_the_author_route_and_rank():
    from vkm_corpus.api import brief

    page = {"envelope": {"object_id": "VKM-SRC-044:p0050", "object_kind": "PAGE", "source_id": "VKM-SRC-044",
                         "page_id": "VKM-SRC-044:p0050", "review_status": "AUTO_EXTRACTED_UNREVIEWED",
                         "origin": "NATIVE", "flags": []},
            "record": {"rank": 1, "object_type": "PAGE", "highlights": [],
                       "trace": {"bm25_rank": 7, "fused_rank": 3, "author_rank": 1}}}

    def body(status):
        return {"ok": True, "meta": {"request_id": "r", "canonical_snapshot_id": "s", "warnings": []},
                "item": {"envelope": {}, "record": {"route": "default", "late": True, "timings_ms": {"total": 1},
                                                    "stages": {"bib_route": {"status": "APPLIED"},
                                                               "author_route": {"status": status}}}},
                "items": [page], "next_cursor": None}

    out = brief.brief_response(body("APPLIED"), kind="hybrid")
    assert out["run"]["stages"]["author_route"] == "APPLIED" and out["items"][0]["author_rank"] == 1
    assert "author_route" not in brief.brief_response(body("NOT_DETECTED"), kind="hybrid")["run"]["stages"]
