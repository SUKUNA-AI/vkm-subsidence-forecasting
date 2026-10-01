"""Structured tables and repeated figures, tables and formulas through the VKM API and MCP (agent G2):
``/v1/nav/table/{id}``, ``/v1/nav/tables``, ``/v1/nav/object_copies/{id}``, ``/v1/nav/shared_formulas`` and the tools
``get_table_structured``, ``find_tables``, ``copies_of_object``, ``shared_formulas`` — the real query functions of the
parts ``tables`` and ``object_duplicates`` over a packed synthetic NAV build (no corpus text); a build without the
part answers DEPENDENCY_UNAVAILABLE naming the missing dataset."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("numpy")
pytest.importorskip("PIL")
pytest.importorskip("pytz")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.envelope import Envelope  # noqa: E402
from vkm_corpus.api.fixtures import synthetic_service  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402
from vkm_corpus.navigation import object_duplicates as OD  # noqa: E402
from nav_manifest_fixture import write_nav_manifest  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402
from vkm_corpus.navigation import tables as TB  # noqa: E402

READ = "read-token-for-nav-tables-0000000000"
HR = {"Authorization": f"Bearer {READ}"}
NAV_SNAP = "snap-nav-tables-test"
LATEX = r"\sigma = \frac{E \varepsilon}{1 - \nu^{2}} + \sqrt{\eta t}"         # distinctive: renamed forms count
LATEX_RENAMED = r"\tau = \frac{G \gamma}{1 - \mu^{2}} + \sqrt{\kappa s}"
OTHER_FIGURE = "VKM-SRC-002:p0003:f0000000000a1"
OTHER_FORMULAS = ("VKM-SRC-002:p0004:m0000000000b1", "VKM-SRC-002:p0005:m0000000000b2")


def _tables(table_id: str) -> dict[str, pa.Table]:
    nav = nav_ids.table_id(table_id)
    src, page = table_id.split(":")[0], table_id.rsplit(":", 1)[0]
    rv = {"rule_version": "tables_v1", "review_status": "AUTO_EXTRACTED_UNREVIEWED"}
    structure = [{"table_id": table_id, "nav_table_id": nav, "source_id": src, "page_id": page, "page_index": 1,
                  "section_id": None, "table_label": "Таблица 2", "table_number": "2", "caption": "Свойства соли",
                  "n_rows": 3, "n_cols": 2, "n_header_rows": 1, "header_method": "FIRST_NUMERIC_ROW", "n_bands": 1,
                  "n_blocks": 1, "orientation": "NORMAL", "parse_method": "BANDS", "confidence": 0.8,
                  "structure_ok": True, "covers_region": True, "quality_flags": [],
                  "property_keys": ["deformation_modulus"], "materials": ["каменная соль"], **rv}]
    columns = [{"table_id": table_id, "nav_table_id": nav, "block": 0, "col": 0, "role": "LABEL",
                "header_path": ["Порода"], "header_text": "Порода", **rv},
               {"table_id": table_id, "nav_table_id": nav, "block": 0, "col": 1, "role": "VALUE",
                "header_path": ["Модуль деформации, ГПа"], "header_text": "Модуль деформации, ГПа", "unit_raw": "ГПа",
                "unit_source": "HEADER", "property_key": "deformation_modulus",
                "property_label": "модуль деформации", **rv}]

    def cell(row: int, col: int, role: str, text: str, **kw: object) -> dict[str, object]:
        return {"cell_id": nav_ids.table_cell_id(nav, row, col), "table_id": table_id, "nav_table_id": nav,
                "source_id": src, "page_id": page, "row": row, "col": col, "row_span": 1, "col_span": 1, "band": 0,
                "block": 0, "text": text, "text_clean": text, "is_header": role == "HEADER", "row_role": role,
                "flags": [], **kw, **rv}

    cells = [cell(0, 0, "HEADER", "Порода"), cell(0, 1, "HEADER", "Модуль деформации, ГПа"),
             cell(1, 0, "DATA", "каменная соль", row_label="каменная соль"),
             cell(1, 1, "DATA", "12,5", value_type="NUMBER", value_text="12,5", value_min=12.5, value_max=12.5,
                  unit_raw="ГПа", unit_source="HEADER", row_label="каменная соль"),
             cell(2, 0, "DATA", "сильвинит", row_label="сильвинит"),
             cell(2, 1, "DATA", "218", value_type="NUMBER", value_text="218", flags=["DECIMAL_POINT_SUSPECT"],
                  row_label="сильвинит")]
    return {"table_structure": pa.Table.from_pylist(structure, schema=TB.TABLE_STRUCTURE_SCHEMA()),
            "table_columns": pa.Table.from_pylist(columns, schema=TB.TABLE_COLUMNS_SCHEMA()),
            "table_cells": pa.Table.from_pylist(cells, schema=TB.TABLE_CELLS_SCHEMA())}


def _object_duplicates(figure: str, formula: str, *, with_formula_key: bool = True) -> dict[str, pa.Table]:
    rv = {"rule_version": "object_duplicates_v1"}
    cid = nav_ids.object_dup_cluster_id("FIGURE", [figure, OTHER_FIGURE])
    clusters = [{"cluster_id": cid, "object_type": "FIGURE", "kind": "REPRINT", "match_basis": "IMAGE", "n_members": 2,
                 "n_sources": 2, "n_works": 2, "n_groups": 2, "source_ids": ["VKM-SRC-001", "VKM-SRC-002"],
                 "work_ids": [], "primary_source_id": "VKM-SRC-001", "primary_object_id": figure,
                 "primary_year": 1990, "primary_rule": "EARLIEST_YEAR", "reference_source_id": "VKM-SRC-001",
                 "reference_object_id": figure, **rv}]
    members = [{"cluster_id": cid, "object_id": figure, "object_type": "FIGURE", "kind": "REPRINT",
                "source_id": "VKM-SRC-001", "year": 1990, "page_id": figure.rsplit(":", 1)[0], "page_index": 1,
                "match": "REFERENCE", "similarity": 1.0, "is_primary": True, "is_reference": True, **rv},
               {"cluster_id": cid, "object_id": OTHER_FIGURE, "object_type": "FIGURE", "kind": "REPRINT",
                "source_id": "VKM-SRC-002", "year": 2001, "page_id": OTHER_FIGURE.rsplit(":", 1)[0], "page_index": 3,
                "match": "IMAGE", "similarity": 0.94, "image_distance": 2, "is_primary": False, "is_reference": False,
                **rv}]
    keys = []
    for fid, latex, year, src in ((formula, LATEX, 1990, "VKM-SRC-001"), (OTHER_FORMULAS[0], LATEX, 2001, "VKM-SRC-002"),
                                  (OTHER_FORMULAS[1], LATEX_RENAMED, 2005, "VKM-SRC-002")):
        if fid == formula and not with_formula_key:
            continue
        c = OD.canonical_formula(latex)
        keys.append({"formula_id": fid, "source_id": src, "work_id": None, "year": year,
                     "page_id": fid.rsplit(":", 1)[0], "page_index": 1, "formula_kind": "DISPLAY",
                     "equation_number": "3.1", "latex_key": c["latex_key"], "shape_key": c["shape_key"],
                     "key_hash": c["key_hash"], "shape_hash": c["shape_hash"], "trivial": c["trivial"],
                     "distinctive": c["distinctive"], **rv})
    return {"object_dup_clusters": pa.Table.from_pylist(clusters, schema=OD.CLUSTERS_SCHEMA),
            "object_dup_members": pa.Table.from_pylist(members, schema=OD.MEMBERS_SCHEMA),
            "formula_keys": pa.Table.from_pylist(keys, schema=OD.FORMULA_KEYS_SCHEMA)}


def _publish(root, datasets: dict[str, pa.Table]) -> store.NavStore:
    nav_dir = root / "derived" / "navigation" / NAV_SNAP
    nav_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"section_id": ["SEC-0000000000000001"], "source_id": ["VKM-SRC-001"], "level": [1],
                             "title": ["Глава 1"], "title_path": ["Глава 1"]}), nav_dir / "sections.parquet")
    for name, table in datasets.items():
        pq.write_table(table, nav_dir / f"{name}.parquet")
    write_nav_manifest(nav_dir, NAV_SNAP)
    store.pack(nav_dir)
    store.publish(root, NAV_SNAP)
    return nav_dir


@pytest.fixture()
def env(tmp_path):
    service, canon, _fakes = synthetic_service(tmp_path / "canon")
    ids = canon.ids
    _publish(tmp_path / "root", {**_tables(ids["table"]), **_object_duplicates(ids["figure"], ids["formula"])})
    service.deps.nav = store.NavStore(tmp_path / "root", canonical_db=canon.duckdb_path)
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}, write_tokens={}))
    return TestClient(app), service, canon, app, tmp_path


def _ok(resp, kind):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    env = body["item"]["envelope"]
    Envelope.model_validate(env)
    assert env["object_kind"] == kind and env["layer"] == "PROJECTION" and env["origin"] == "DERIVED"
    assert env["review_status"] == "AUTO_EXTRACTED_UNREVIEWED" and env["projection"]["engine"] == "navigation"
    record = body["item"]["record"]
    assert record["nav_snapshot_id"] == NAV_SNAP and "not evidence" in record["note"]
    return body, record


def _error(resp, code):
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == code, body
    return body["error"]


def test_structured_table_by_canonical_and_nav_id(env):
    client, _service, canon, _app, _tmp = env
    table = canon.ids["table"]
    body, rec = _ok(client.get(f"/v1/nav/table/{table}", headers=HR), "NAV_TABLE")
    env_ = body["item"]["envelope"]
    assert env_["object_id"] == table and env_["source_id"] == table.split(":")[0]
    assert env_["page_id"] == table.rsplit(":", 1)[0]
    assert rec["found"] and rec["table"]["nav_table_id"] == nav_ids.table_id(table)
    assert [c["property_key"] for c in rec["columns"] if c.get("property_key")] == ["deformation_modulus"]
    assert [r["role"] for r in rec["rows"]] == ["HEADER", "DATA", "DATA"]
    assert rec["rows"][2]["cells"][1]["flags"] == ["DECIMAL_POINT_SUSPECT"] and rec["markdown"].startswith("| Порода")
    body2, rec2 = _ok(client.get(f"/v1/nav/table/{nav_ids.table_id(table)}", params={"max_rows": 2}, headers=HR),
                      "NAV_TABLE")
    assert body2["item"]["envelope"]["object_id"] == table and len(rec2["rows"]) == 2
    assert rec2["truncated"]["rows"] is True
    # errors: not a table id, a table without a grid in this build, a bad cap, no token
    _error(client.get(f"/v1/nav/table/{table.rsplit(':', 1)[0]}", headers=HR), "INVALID_ID")
    _error(client.get("/v1/nav/table/TBL-zz", headers=HR), "INVALID_ID")
    missing = client.get("/v1/nav/table/VKM-SRC-001:p0001:t0000000000ff", headers=HR)
    assert missing.status_code == 404 and "find_tables" in _error(missing, "NOT_FOUND")["hint"]
    _error(client.get(f"/v1/nav/table/{table}", params={"max_rows": 0}, headers=HR), "INVALID_ARGUMENT")
    assert client.get(f"/v1/nav/table/{table}").status_code == 401


def test_find_tables_by_property_material_source_and_words(env):
    client, _service, canon, _app, _tmp = env
    table = canon.ids["table"]
    body, rec = _ok(client.get("/v1/nav/tables", params={"property": "модуль деформации"}, headers=HR),
                    "NAV_TABLES")
    assert rec["total"] == 1 and rec["tables"][0]["table_id"] == table
    assert rec["tables"][0]["matched_columns"][0]["property_key"] == "deformation_modulus"
    assert rec["query"]["property_keys"] == ["deformation_modulus"]
    _b, rec = _ok(client.get("/v1/nav/tables", params={"material": "каменная соль", "text": "свойства соли"},
                             headers=HR), "NAV_TABLES")
    assert [t["table_id"] for t in rec["tables"]] == [table]
    _b, rec = _ok(client.get("/v1/nav/tables", params={"source_id": table.split(":")[0]}, headers=HR), "NAV_TABLES")
    assert rec["total"] == 1
    _b, rec = _ok(client.get("/v1/nav/tables", params={"property": "плотность"}, headers=HR), "NAV_TABLES")
    assert rec["total"] == 0 and rec["tables"] == []                          # a search without hits: 200
    _b, rec = _ok(client.get("/v1/nav/tables", params={"property": "несуществующее свойство"}, headers=HR),
                  "NAV_TABLES")
    assert rec["unresolved"] == ["property"] and rec["total"] == 0
    _error(client.get("/v1/nav/tables", headers=HR), "INVALID_ARGUMENT")
    _error(client.get("/v1/nav/tables", params={"source_id": "SRC-1"}, headers=HR), "INVALID_ARGUMENT")
    _error(client.get("/v1/nav/tables", params={"text": "x", "limit": 101}, headers=HR), "INVALID_ARGUMENT")


def test_copies_of_an_object(env):
    client, _service, canon, _app, _tmp = env
    figure = canon.ids["figure"]
    body, rec = _ok(client.get(f"/v1/nav/object_copies/{figure}", headers=HR), "NAV_OBJECT_COPIES")
    assert body["item"]["envelope"]["object_id"] == figure and body["item"]["envelope"]["page_id"] == \
        figure.rsplit(":", 1)[0]
    [cluster] = rec["clusters"]
    assert cluster["kind"] == "REPRINT" and cluster["this_is_primary"] is True
    assert [c["object_id"] for c in cluster["copies"]] == [OTHER_FIGURE] and cluster["copies"][0]["image_distance"] == 2
    # an object without repeats: 200 with an empty list and a hint
    _b, rec = _ok(client.get(f"/v1/nav/object_copies/{canon.ids['table']}", headers=HR), "NAV_OBJECT_COPIES")
    assert rec["clusters"] == [] and "no repeat" in rec["hint"]
    _error(client.get(f"/v1/nav/object_copies/{canon.ids['block']}", headers=HR), "INVALID_ID")
    _error(client.get("/v1/nav/object_copies/not-an-id", headers=HR), "INVALID_ID")
    _error(client.get("/v1/nav/object_copies/VKM-SRC-001:p0001:f0000000000ff", headers=HR), "NOT_FOUND")
    _error(client.get(f"/v1/nav/object_copies/{figure}", params={"limit": 0}, headers=HR), "INVALID_ARGUMENT")


def test_shared_formulas_by_id_and_by_latex(env):
    client, service, canon, _app, tmp = env
    formula = canon.ids["formula"]
    body, rec = _ok(client.get("/v1/nav/shared_formulas", params={"ref": formula}, headers=HR),
                    "NAV_SHARED_FORMULAS")
    assert body["item"]["envelope"]["object_id"] == formula and rec["match"] == "formula_id"
    assert rec["n_exact"] == 2 and rec["n_renamed"] == 1 and rec["n_works"] == 2    # two sources, no work ids
    assert [f["formula_id"] for w in rec["exact"] for f in w["formulas"]] == [formula, OTHER_FORMULAS[0]]
    assert rec["renamed"][0]["formulas"][0]["formula_id"] == OTHER_FORMULAS[1]
    _b, rec = _ok(client.get("/v1/nav/shared_formulas", params={"ref": formula, "renamed": "false"}, headers=HR),
                  "NAV_SHARED_FORMULAS")
    assert rec["n_renamed"] == 0
    body, rec = _ok(client.get("/v1/nav/shared_formulas", params={"ref": LATEX_RENAMED}, headers=HR),
                    "NAV_SHARED_FORMULAS")
    assert body["item"]["envelope"]["object_id"].startswith("latex:") and body["item"]["envelope"]["source_id"] is None
    assert rec["match"] == "latex" and rec["n_exact"] == 1 and rec["n_renamed"] == 2
    _error(client.get("/v1/nav/shared_formulas", params={"ref": canon.ids["figure"]}, headers=HR), "INVALID_ID")
    _error(client.get("/v1/nav/shared_formulas", params={"ref": "VKM-SRC-001:p0001:m0000000000ff"}, headers=HR),
           "NOT_FOUND")
    # a formula of the canon without a canonical key in this build: never compared as LaTeX
    root2 = tmp / "root2"
    _publish(root2, _object_duplicates(canon.ids["figure"], formula, with_formula_key=False))
    service.deps.nav = store.NavStore(root2, canonical_db=canon.duckdb_path)
    _b, rec = _ok(client.get("/v1/nav/shared_formulas", params={"ref": formula}, headers=HR), "NAV_SHARED_FORMULAS")
    assert rec["match"] is None and rec["exact"] == [] and rec["reason"].startswith("NO_FORMULA_KEY")


def test_a_build_without_the_parts_is_a_missing_dependency(env):
    client, service, canon, _app, tmp = env
    root = tmp / "bare"
    _publish(root, {})
    service.deps.nav = store.NavStore(root, canonical_db=canon.duckdb_path)
    for path, params in ((f"/v1/nav/table/{canon.ids['table']}", {}), ("/v1/nav/tables", {"property": "E"}),
                         (f"/v1/nav/object_copies/{canon.ids['figure']}", {}),
                         ("/v1/nav/shared_formulas", {"ref": canon.ids["formula"]})):
        r = client.get(path, params=params, headers=HR)
        assert r.status_code == 503, (path, r.text)
        err = _error(r, "DEPENDENCY_UNAVAILABLE")
        assert "has no" in err["message"] and err["stage"] == "navigation"
    assert "table_structure" in client.get("/v1/nav/tables", params={"text": "x"}, headers=HR).json()["error"][
        "message"]
    assert service.deps.nav.datasets() == frozenset({"sections"})
    service.deps.nav = None
    _error(client.get("/v1/nav/tables", params={"text": "x"}, headers=HR), "DEPENDENCY_UNAVAILABLE")


def test_mcp_tools(env):
    pytest.importorskip("mcp")
    import httpx
    from mcp import Client

    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    _client, _service, canon, app, _tmp = env
    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app)))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            out = {
                "table": await client.call_tool("get_table_structured", {"table_id": canon.ids["table"],
                                                                         "max_rows": 5}),
                "tables": await client.call_tool("find_tables", {"property": "модуль деформации", "limit": 3}),
                "copies": await client.call_tool("copies_of_object", {"object_id": canon.ids["figure"]}),
                "formulas": await client.call_tool("shared_formulas", {"ref": LATEX, "renamed": False}),
                "bad_table": await client.call_tool("get_table_structured", {"table_id": "a/b"}),
                "bad_object": await client.call_tool("copies_of_object", {"object_id": canon.ids["block"]}),
            }
            return tools, out

    tools, out = asyncio.run(go())
    for name in ("get_table_structured", "find_tables", "copies_of_object", "shared_formulas"):
        assert tools[name].annotations.read_only_hint and not tools[name].annotations.destructive_hint
    for name in ("table", "tables", "copies", "formulas"):
        assert out[name].is_error is False and out[name].structured_content["ok"], name
    assert out["table"].structured_content["item"]["envelope"]["object_kind"] == "NAV_TABLE"
    assert out["tables"].structured_content["item"]["record"]["tables"][0]["table_id"] == canon.ids["table"]
    assert out["copies"].structured_content["item"]["record"]["clusters"][0]["copies"][0]["object_id"] == OTHER_FIGURE
    assert out["formulas"].structured_content["item"]["record"]["n_exact"] == 2
    assert out["bad_table"].is_error and out["bad_object"].is_error              # schema: ids of the right kind
    schema = tools["copies_of_object"].input_schema["properties"]["object_id"]["pattern"]
    assert ":[ftm][0-9a-f]{12}" in schema
