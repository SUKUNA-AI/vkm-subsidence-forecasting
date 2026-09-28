"""vkm-drawio service: update operations, sha conflicts, jail, overwrite and format policy, the public leakage policy,
executable discovery and the CLI-backed tools with a fake draw.io (DRW-05…DRW-10). Machine-path, address and token
samples are assembled at run time so that this file itself stays clean for the hygiene scan."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from vkm_drawio.errors import ToolFailure
from vkm_drawio.locate import DrawioLocation, env_value, locate
from vkm_drawio.model import DiagramSpec
from vkm_drawio.ops import Operation
from vkm_drawio.policy import policy_problems
from vkm_drawio.service import DrawioService
from vkm_drawio.workspace import Workspace, atomic_write, check_relative
from vkm_drawio.xmlio import read_diagram_bytes

OPS = TypeAdapter(list[Operation])
BASE = {"pages": [{"id": "p", "name": "Page", "nodes": [
    {"id": "box", "label": "Контейнер", "preset": "container", "x": 0, "y": 0, "w": 300, "h": 200},
    {"id": "a", "label": "A", "parent": "box", "x": 20, "y": 40},
    {"id": "b", "label": "B", "x": 400, "y": 40}],
    "edges": [{"id": "ab", "source": "a", "target": "b", "label": "rel"}]}]}


class FakeCli:
    """Stands in for vkm_drawio.drawio_cli: records calls, returns tiny outputs."""

    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (320).to_bytes(4, "big") + (200).to_bytes(4, "big")
           + b"\x08\x02\x00\x00\x00" + b"\x00" * 16)

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def version(self, exe):
        return "31.5.3-fake"

    def export_file(self, exe, src, out_suffix, **kw):
        from vkm_drawio.drawio_cli import CliRun

        self.calls.append(("export", {"suffix": out_suffix, **kw}))
        blob = self.PNG if out_suffix == ".png" else b"%PDF-fake" if out_suffix == ".pdf" else b"<svg/>"
        return blob, CliRun(0, 0.01, "")

    def layout_page(self, exe, page, layout, *, fixed):
        self.calls.append(("layout", {"layout": layout, "fixed": dict(fixed)}))
        for n, cell in enumerate(c for c in page.cells if c.vertex):
            cell.geometry.x, cell.geometry.y = 10.0 + 200 * n, 10.0
            if cell.id in fixed:
                cell.geometry.x, cell.geometry.y = fixed[cell.id]
        return page

    def open_gui(self, exe, path):
        self.calls.append(("open", {"name": path.name}))
        return 4242


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    return Workspace(public_root=tmp_path / "public", work_root=tmp_path / "work")


@pytest.fixture()
def svc(ws) -> DrawioService:
    return DrawioService(ws, locator=lambda: DrawioLocation(False, reason="not installed (test)"))


@pytest.fixture()
def fake_svc(ws, tmp_path) -> tuple[DrawioService, FakeCli]:
    cli = FakeCli()
    exe = tmp_path / "draw.io.exe"
    return DrawioService(ws, locator=lambda: DrawioLocation(True, "ENV", exe, "$VKM_DRAWIO_EXE"), cli=cli), cli


def _create(svc: DrawioService, root: str = "work", path: str = "d.drawio", spec: dict = BASE) -> dict:
    return svc.create(root, path, DiagramSpec.model_validate(spec))


# ------------------------------------------------------------------------------------------------ operations
def test_update_operations_apply_atomically(svc):
    sha = _create(svc)["sha256"]
    out = svc.update("work", "d.drawio", sha, OPS.validate_python([
        {"op": "add_node", "node": {"id": "c", "label": "C"}},
        {"op": "update_node", "id": "a", "label": "A2", "style_set": {"fillColor": "#dae8fc"}, "tooltip": "t",
         "props": {"kind": "X"}},
        {"op": "add_edge", "edge": {"id": "bc", "source": "b", "target": "c"}},
        {"op": "update_edge", "id": "ab", "label": "rel2", "waypoints": [[350, 60]]},
        {"op": "rename_page", "page": "Page", "name": "Renamed"},
        {"op": "add_page", "page_spec": {"id": "q", "name": "Q", "layout": "grid", "nodes": [{"id": "z"}]}},
    ]))
    assert out["changed"] and out["sha256_before"] == sha and len(out["applied"]) == 6
    page = svc.read("work", "d.drawio")["pages"][0]
    nodes = {n["id"]: n for n in page["nodes"]}
    assert page["name"] == "Renamed" and nodes["c"]["x"] == 560.0          # auto-placed right of the content
    assert nodes["a"]["label"] == "A2" and nodes["a"]["tooltip"] == "t" and nodes["a"]["props"] == {"kind": "X"}
    assert "fillColor=#dae8fc" in nodes["a"]["style"]
    assert {e["id"]: e for e in page["edges"]}["ab"]["waypoints"] == [[350.0, 60.0]]


def test_update_conflict_and_all_or_nothing(svc, ws):
    sha = _create(svc)["sha256"]
    with pytest.raises(ToolFailure) as exc:
        svc.update("work", "d.drawio", "0" * 64, OPS.validate_python([{"op": "canonicalize"}]))
    assert exc.value.code == "DIAGRAM_CONFLICT" and exc.value.details["actual_sha256"] == sha
    before = (ws.work_root / "d.drawio").read_bytes()
    with pytest.raises(ToolFailure) as exc:                                  # second op fails → nothing written
        svc.update("work", "d.drawio", sha, OPS.validate_python([
            {"op": "add_node", "node": {"id": "ok", "x": 1, "y": 1}},
            {"op": "add_edge", "edge": {"id": "dangling", "source": "ok", "target": "missing"}}]))
    assert exc.value.code == "INVALID_DIAGRAM_OP" and exc.value.details["op_index"] == 1
    assert (ws.work_root / "d.drawio").read_bytes() == before


@pytest.mark.parametrize("ops,fragment", [
    ([{"op": "remove_node", "id": "box"}], "cascade"),
    ([{"op": "add_node", "node": {"id": "a", "x": 1, "y": 1}}], "exists"),
    ([{"op": "update_node", "id": "box", "parent": "a"}], "cycle"),
    ([{"op": "update_node", "id": "ab", "label": "x"}], "not a node"),
    ([{"op": "remove_edge", "id": "a"}], "not an edge"),
    ([{"op": "remove_page"}], "only page"),
    ([{"op": "rename_page", "page": "nope", "name": "x"}], "not found"),
    ([{"op": "update_node", "id": "a", "props": {"label": "x"}}], "reserved"),
    ([{"op": "update_node", "id": "a", "link": "file:x"}], "link"),
])
def test_invalid_operations(svc, ops, fragment):
    sha = _create(svc)["sha256"]
    with pytest.raises(ToolFailure) as exc:
        svc.update("work", "d.drawio", sha, OPS.validate_python(ops))
    assert exc.value.code == "INVALID_DIAGRAM_OP" and fragment in exc.value.message


def test_remove_node_cascade_removes_children_and_edges(svc):
    sha = _create(svc)["sha256"]
    out = svc.update("work", "d.drawio", sha, OPS.validate_python([{"op": "remove_node", "id": "box",
                                                                     "cascade": True}]))
    page = svc.read("work", "d.drawio")["pages"][0]
    assert [n["id"] for n in page["nodes"]] == ["b"] and page["edges"] == [] and "3 cells" in out["applied"][0]


def test_spec_validation():
    bad = [
        {"pages": [{"id": "p", "name": "p", "nodes": [{"id": "a"}]}]},                          # layout none w/o x
        {"pages": [{"id": "p", "name": "p", "layout": "grid", "nodes": [{"id": "a"}, {"id": "a"}]}]},
        {"pages": [{"id": "p", "name": "p", "layout": "grid", "nodes": [{"id": "1"}]}]},       # reserved id
        {"pages": [{"id": "p", "name": "p", "layout": "grid", "nodes": [{"id": "a", "parent": "b"}, {"id": "b",
                                                                                         "parent": "a"}]}]},
        {"pages": [{"id": "p", "name": "p", "layout": "grid", "nodes": [{"id": "a"}],
                    "edges": [{"id": "e", "source": "a", "target": "zz"}]}]},
        {"pages": [{"id": "p", "name": "p", "layout": "drawio:spiral", "nodes": []}]},
        {"pages": [{"id": "p", "name": "p", "layout": "grid", "nodes": [{"id": "a", "link": "javascript:x"}]}]},
        {"pages": [{"id": "p", "name": "p", "layout": "grid", "nodes": [{"id": "a", "extra": 1}]}]},
    ]
    for spec in bad:
        with pytest.raises(ValidationError):
            DiagramSpec.model_validate(spec)


# ------------------------------------------------------------------------------------------------ jail and policy
@pytest.mark.parametrize("rel", ["../x.drawio", "a/../../x.drawio", "/abs.drawio", "\\abs.drawio", "C" + ":/x.drawio",
                                 "\\\\server\\share\\x.drawio", "x.drawio:stream", "CON.drawio", "sub/nul",
                                 "trailing./x.drawio", "a//b.drawio", "", "x" * 201, "bad|name.drawio"])
def test_jail_rejects_escapes(ws, rel):
    with pytest.raises(ToolFailure) as exc:
        ws.resolve("public", rel)
    assert exc.value.code == "PATH_OUTSIDE_WORKSPACE"


def test_jail_rejects_links_under_the_root(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    ws.public_root.mkdir(parents=True)
    link = ws.public_root / "link"
    try:
        if sys.platform == "win32":
            import subprocess

            proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if proc.returncode != 0:
                pytest.skip("cannot create a junction here")
        else:
            os.symlink(outside, link)
    except OSError:
        pytest.skip("cannot create links here")
    with pytest.raises(ToolFailure) as exc:
        ws.resolve("public", "link/x.drawio")
    assert exc.value.code == "PATH_OUTSIDE_WORKSPACE"


def test_roots_formats_and_overwrite(svc, ws):
    with pytest.raises(ToolFailure) as exc:
        Workspace(None, None, "no repo", "VKM_WORK is not set").root("work")
    assert exc.value.code == "ROOT_UNAVAILABLE"
    with pytest.raises(ToolFailure) as exc:
        ws.root("private")
    assert exc.value.code == "PATH_OUTSIDE_WORKSPACE"
    with pytest.raises(ToolFailure) as exc:
        ws.check_format("public", ws.public_root / "x.pdf")
    assert exc.value.code == "FORMAT_NOT_ALLOWED_IN_ROOT"
    with pytest.raises(ToolFailure) as exc:
        ws.check_format("work", ws.work_root / "x.exe")
    assert exc.value.code == "FORMAT_NOT_SUPPORTED"
    _create(svc)
    with pytest.raises(ToolFailure) as exc:
        _create(svc)
    assert exc.value.code == "WOULD_OVERWRITE"
    assert svc.create("work", "d.drawio", DiagramSpec.model_validate(BASE), overwrite=True)["bytes"] > 0
    with pytest.raises(ToolFailure) as exc:
        svc.create("work", "d.svg", DiagramSpec.model_validate(BASE))
    assert exc.value.code == "FORMAT_NOT_SUPPORTED"
    target = ws.work_root / "raw.bin"
    atomic_write(target, b"1", overwrite=False)
    with pytest.raises(ToolFailure):
        atomic_write(target, b"2", overwrite=False)
    assert target.read_bytes() == b"1" and not [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]


def _policy_spec(**node) -> DiagramSpec:
    return DiagramSpec.model_validate({"pages": [{"id": "p", "name": "p", "nodes": [
        {"id": "a", "x": 0, "y": 0, **node}]}]})


@pytest.mark.parametrize("node,rule", [
    ({"label": "see " + "D" + ":" + "\\" + "data\\x.pdf"}, "HOST_PATH_WINDOWS"),
    ({"tooltip": "\\" + "\\" + "fileserver\\share\\x"}, "HOST_PATH_UNC"),
    ({"label": "/" + "home" + "/someone/vkm"}, "HOST_PATH_POSIX"),
    ({"label": "core at " + ".".join(["192", "168", "7", "3"])}, "PRIVATE_IPV4"),
    ({"label": "tailnet " + ".".join(["100", "101", "2", "3"])}, "PRIVATE_IPV4"),
    ({"props": {"note": "gh" + "p_" + "A" * 30}}, "SECRET"),
    ({"label": "pass" + "word=" + "hunter2hunter2"}, "SECRET"),
    ({"style": "shape=image;image=" + "data:image/png;base64,AAAA"}, "EMBEDDED_IMAGE_DATA"),
])
def test_public_policy_rules(svc, node, rule):
    spec = _policy_spec(**node)
    assert rule in {p["rule"] for p in policy_problems(__import__("vkm_drawio.build").build.build_diagram(spec))}
    with pytest.raises(ToolFailure) as exc:
        svc.create("public", "x.drawio", spec)
    assert exc.value.code == "LEAKAGE_POLICY_VIOLATION"
    assert all(set(p) == {"where", "rule"} for p in exc.value.details["problems"])   # text is never echoed
    assert svc.create("work", "x.drawio", spec)["bytes"] > 0                          # drafts are not policed


def test_policy_accepts_clean_diagrams_and_placeholders(svc):
    spec = _policy_spec(label="$VKM_DATA_ROOT/canonical · 127.0.0.1 · version 25.1.60.0 · token=${VKM_MCP_TOKEN}",
                        link="https://example.org/docs")
    assert policy_problems(__import__("vkm_drawio.build").build.build_diagram(spec)) == []
    assert svc.create("public", "clean.drawio", spec)["bytes"] > 0


def test_unsafe_links_in_foreign_files_are_caught(svc, ws):
    ws.public_root.mkdir(parents=True)
    foreign = ('<mxfile><diagram id="d" name="d"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
               '<UserObject id="u" label="x" link="file:///etc/passwd"><mxCell vertex="1" parent="1"><mxGeometry '
               'width="10" height="10" as="geometry"/></mxCell></UserObject></root></mxGraphModel></diagram></mxfile>')
    (ws.public_root / "foreign.drawio").write_text(foreign, encoding="utf-8")
    sha = hashlib.sha256(foreign.encode("utf-8")).hexdigest()
    with pytest.raises(ToolFailure) as exc:
        svc.update("public", "foreign.drawio", sha, OPS.validate_python([{"op": "canonicalize"}]))
    assert exc.value.code == "LEAKAGE_POLICY_VIOLATION"
    assert {p["rule"] for p in exc.value.details["problems"]} >= {"UNSAFE_LINK"}


# ------------------------------------------------------------------------------------------------ workspace from env
def test_workspace_from_env(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    ws = Workspace.from_env({"CLAUDE_PROJECT_DIR": str(repo), "VKM_WORK": "${VKM_WORK}"})
    assert ws.public_root == repo / "docs" / "diagrams" and ws.work_root is None      # unexpanded literal = unset
    ok = Workspace.from_env({"CLAUDE_PROJECT_DIR": str(repo), "VKM_WORK": str(tmp_path / "w")})
    assert ok.work_root == (tmp_path / "w").resolve() / "diagrams"
    res = tmp_path / "private"
    inside = Workspace.from_env({"VKM_WORK": str(res / "w"), "VKM_RESOURCES_ROOT": str(res)})
    assert inside.work_root is None and "RESOURCES" in inside.work_reason
    in_repo = Workspace.from_env({"CLAUDE_PROJECT_DIR": str(repo), "VKM_WORK": str(repo / "docs")})
    assert in_repo.work_root is None and "git-ignored" in in_repo.work_reason


def test_check_relative_accepts_normal_paths():
    assert check_relative("sub/dir/Диаграмма-1.drawio") == ["sub", "dir", "Диаграмма-1.drawio"]
    assert check_relative("**/*.drawio", allow_glob=True) == ["**", "*.drawio"]


# ------------------------------------------------------------------------------------------------ locate
def test_locate_order_and_literals(tmp_path):
    exe = tmp_path / "draw.io.exe"
    exe.write_bytes(b"")
    assert env_value({"X": "${X}"}, "X") is None and env_value({"X": " "}, "X") is None
    got = locate({"VKM_DRAWIO_EXE": str(exe)}, appx=lambda: None)
    assert (got.found, got.discovery, got.exe_logical) == (True, "ENV", "$VKM_DRAWIO_EXE")
    got = locate({"VKM_DRAWIO_EXE": str(tmp_path / "missing.exe")}, appx=lambda: None)
    assert not got.found and got.discovery == "ENV"
    appx_dir = tmp_path / "pkg"
    (appx_dir / "app").mkdir(parents=True)
    (appx_dir / "app" / "draw.io.exe").write_bytes(b"")
    got = locate({"VKM_DRAWIO_EXE": "${VKM_DRAWIO_EXE}"}, appx=lambda: str(appx_dir), which=lambda n: None)
    assert got.discovery == "APPX" and "WindowsApps" not in got.exe_logical and str(tmp_path) not in got.exe_logical
    pf = tmp_path / "pf"
    (pf / "draw.io").mkdir(parents=True)
    (pf / "draw.io" / "draw.io.exe").write_bytes(b"")
    got = locate({"ProgramFiles": str(pf)}, appx=lambda: None, which=lambda n: None)
    assert got.discovery == "PROGRAM_FILES" and got.exe_logical.startswith("%ProgramFiles%")
    got = locate({}, appx=lambda: None, which=lambda n: str(exe) if n == "drawio" else None)
    assert got.discovery == "PATH" and got.exe_logical == "PATH:drawio"
    got = locate({}, appx=lambda: None, which=lambda n: None)
    assert not got.found and "VKM_DRAWIO_EXE" in got.reason
    assert str(tmp_path) not in str(got.public())


# ------------------------------------------------------------------------------------------------ CLI-backed tools
def test_cli_tools_unavailable_without_drawio(svc):
    _create(svc)
    for call in (lambda: svc.export("work", "d.drawio", "png"), lambda: svc.preview("work", "d.drawio"),
                 lambda: svc.open("work", "d.drawio")):
        with pytest.raises(ToolFailure) as exc:
            call()
        assert exc.value.code == "DRAWIO_UNAVAILABLE"
    layout_spec = {"pages": [{"id": "p", "name": "p", "layout": "drawio:verticalFlow", "nodes": [{"id": "a"}]}]}
    with pytest.raises(ToolFailure) as exc:
        _create(svc, path="l.drawio", spec=layout_spec)
    assert exc.value.code == "DRAWIO_UNAVAILABLE"
    status = svc.status()
    assert status["drawio"]["found"] is False and status["capabilities"]["export"] is False
    assert status["capabilities"]["create"] is True


def test_export_preview_open_with_fake_cli(fake_svc, ws):
    svc, cli = fake_svc
    _create(svc, "public", "d.drawio")
    out = svc.export("public", "d.drawio", "svg")
    assert out["out_path"] == "d.svg" and (ws.public_root / "d.svg").read_bytes() == b"<svg/>"
    assert out["embed_diagram"] is True and cli.calls[-1][1]["embed_diagram"] is True
    with pytest.raises(ToolFailure) as exc:
        svc.export("public", "d.drawio", "svg")
    assert exc.value.code == "WOULD_OVERWRITE"
    with pytest.raises(ToolFailure) as exc:
        svc.export("public", "d.drawio", "pdf")
    assert exc.value.code == "FORMAT_NOT_ALLOWED_IN_ROOT"
    pdf = svc.export("public", "d.drawio", "pdf", all_pages=True, out_root="work")
    assert pdf["out_root"] == "work" and (ws.work_root / "d.pdf").is_file()
    with pytest.raises(ToolFailure) as exc:
        svc.export("public", "d.drawio", "png", page=2)
    assert exc.value.code == "INVALID_ARGUMENT"
    blob, meta = svc.preview("public", "d.drawio", max_side=512)
    assert blob.startswith(b"\x89PNG") and meta["px"] == [320, 200] and meta["mime_type"] == "image/png"
    assert cli.calls[-1][1].get("width") == 512                          # wide content → width-limited
    opened = svc.open("public", "d.drawio")
    assert opened["launched"] and opened["pid"] == 4242 and cli.calls[-1][0] == "open"


def test_drawio_layout_keeps_explicit_coordinates(fake_svc, ws):
    svc, cli = fake_svc
    spec = {"pages": [{"id": "p", "name": "p", "layout": "drawio:elkLayered:RIGHT", "nodes": [
        {"id": "a"}, {"id": "b", "x": 900, "y": 500}], "edges": [{"id": "e", "source": "a", "target": "b"}]}]}
    out = _create(svc, path="l.drawio", spec=spec)
    assert out["pages"][0]["layout_engine"] == "drawio 31.5.3-fake:elkLayered:RIGHT"
    assert cli.calls[0] == ("layout", {"layout": "drawio:elkLayered:RIGHT", "fixed": {"b": (900.0, 500.0)}})
    nodes = {n["id"]: n for n in svc.read("work", "l.drawio")["pages"][0]["nodes"]}
    assert (nodes["a"]["x"], nodes["b"]["x"], nodes["b"]["y"]) == (10.0, 900.0, 500.0)


def test_list_and_read_embedded(fake_svc, ws):
    svc, _cli = fake_svc
    _create(svc, "work", "a/one.drawio")
    _create(svc, "work", "two.drawio")
    listing = svc.list("work")
    assert [f["path"] for f in listing["files"]] == ["a/one.drawio", "two.drawio"]
    assert all(f["pages"] == 1 and len(f["sha256"]) == 64 for f in listing["files"])
    with pytest.raises(ToolFailure):
        svc.list("work", "../*")
    with pytest.raises(ToolFailure) as exc:
        svc.read("work", "missing.drawio")
    assert exc.value.code == "NOT_FOUND"
    (ws.work_root / "broken.drawio").write_text("<mxfile><diagram>%%%</diagram></mxfile>", encoding="utf-8")
    with pytest.raises(ToolFailure) as exc:
        svc.read("work", "broken.drawio")
    assert exc.value.code == "DIAGRAM_PARSE_ERROR"
    diagram = read_diagram_bytes((ws.work_root / "two.drawio").read_bytes())
    assert [c.id for c in diagram.pages[0].cells] == ["0", "1", "box", "a", "b", "ab"]
