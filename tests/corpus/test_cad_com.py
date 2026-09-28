"""Attach-only COM reading on fake COM objects (CAD-05, CAD-06): allow-list proxy, attach errors, retries, timeouts,
document lookup, and a source lint that keeps launch/command members out of the bridge."""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from vkm_cad import com_read
from vkm_cad.com_read import (CO_E_CLASSSTRING, MK_E_UNAVAILABLE, ComSession, ReadOnlyCom, _attach_running,
                              find_document, get_coordinate_system, get_extents, get_layers, list_entities,
                              list_open_documents)
from vkm_cad.errors import ToolFailure

ROOT = Path(__file__).resolve().parents[2]


class FakeComError(Exception):
    def __init__(self, hresult: int):
        super().__init__(hresult, "fake")
        self.hresult = hresult


class Obj:
    """A fake COM dispatch object (``_oleobj_`` marks it as COM for the proxy)."""

    _oleobj_ = object()

    def __init__(self, **props):
        self.__dict__.update(props)

    # members the bridge must never reach
    def Save(self):  # noqa: N802
        raise AssertionError("Save reached")

    def SaveAs(self, *a):  # noqa: N802
        raise AssertionError("SaveAs reached")

    def SetVariable(self, *a):  # noqa: N802
        raise AssertionError("SetVariable reached")

    def SendCommand(self, *a):  # noqa: N802
        raise AssertionError("SendCommand reached")

    def Close(self, *a):  # noqa: N802
        raise AssertionError("Close reached")


class Collection(Obj):
    def __init__(self, items):
        super().__init__(Count=len(items))
        self._items = items

    def Item(self, i):  # noqa: N802
        return self._items[i]


class Variant:
    def __init__(self):
        self.value = None


def _entity(handle, name, layer, box=((0, 0, 0), (1, 1, 0))):
    def get_bbox(lo, hi):
        lo.value, hi.value = box
    ent = Obj(Handle=handle, ObjectName=name, Layer=layer)
    ent.GetBoundingBox = get_bbox
    return ent


def _document(name, full, saved=True, cgeocs=""):
    variables = {"EXTMIN": (0.0, 0.0, 0.0), "EXTMAX": (100.0, 50.0, 0.0), "INSUNITS": 6, "MEASUREMENT": 1,
                 "PEXTMIN": (1e20, 1e20, 1e20), "PEXTMAX": (-1e20, -1e20, -1e20), "CGEOCS": cgeocs}
    layers = Collection([Obj(Name="0", LayerOn=True, Freeze=False, Lock=False, Color=7, Linetype="Continuous"),
                         Obj(Name="Скважины", LayerOn=False, Freeze=True, Lock=True, Color=1, Linetype="DASHED")])
    space = Collection([_entity("1A", "AcDbLine", "0"), _entity("1B", "AcDbCircle", "Скважины"),
                        _entity("1C", "AcDbLine", "Скважины")])
    doc = Obj(Name=name, FullName=full, ReadOnly=False, Saved=saved, Layers=layers, ModelSpace=space)
    doc.GetVariable = lambda n: variables[n]
    return doc


def _app():
    docs = [_document("plan.dwg", "Z:/fake/plan.dwg"), _document("Drawing1.dwg", "", saved=False, cgeocs="UTM84-39N")]
    return Obj(Documents=Collection(docs), ActiveDocument=docs[1])


def test_read_operations_on_fake_autocad():
    app = ReadOnlyCom(_app())
    docs = list_open_documents(app)
    assert [d["doc_ref"] for d in docs] == ["#1", "#2"] and docs[1]["active"] and not docs[1]["has_file"]
    assert docs[0]["format"] == "dwg" and "fake" not in str(docs)             # names only, never directories
    doc = find_document(app, "#1")
    layers = get_layers(doc)
    assert layers[1] == {"name": "Скважины", "on": False, "frozen": True, "locked": True, "color": 1,
                         "linetype": "DASHED"}
    ext = get_extents(doc, "model")
    assert ext["valid"] and ext["extmax"] == [100.0, 50.0, 0.0] and ext["insunits_name"] == "Meters"
    assert get_extents(doc, "paper")["valid"] is False
    page = list_entities(doc, Variant, layers=["скважины"], types=None, limit=1, cursor=0, include_bbox=True)
    assert [e["handle"] for e in page["entities"]] == ["1B"] and page["next_cursor"] == 2
    assert page["entities"][0]["bbox"] == [[0.0, 0.0, 0.0], [1.0, 1.0, 0.0]]
    rest = list_entities(doc, Variant, layers=["Скважины"], types=["acdbline"], limit=10, cursor=2,
                         include_bbox=False)
    assert [e["handle"] for e in rest["entities"]] == ["1C"] and rest["next_cursor"] is None
    cs = get_coordinate_system(find_document(app, "drawing1.dwg"))
    assert cs["cgeocs_raw"] == "UTM84-39N" and cs["crs_status"] == "UNKNOWN_CRS" and cs["epsg"] is None
    with pytest.raises(ToolFailure) as exc:
        find_document(app, "#9")
    assert exc.value.code == "CAD_DOCUMENT_NOT_FOUND"
    with pytest.raises(ToolFailure) as exc:
        list_entities(doc, Variant, layers=None, types=None, limit=5000, cursor=0, include_bbox=False)
    assert exc.value.code == "PAYLOAD_TOO_LARGE"


@pytest.mark.parametrize("member", ["Save", "SaveAs", "SetVariable", "SendCommand", "Close", "PostCommand",
                                    "Application", "Delete", "AddLine"])
def test_proxy_blocks_everything_outside_the_allow_list(member):
    doc = ReadOnlyCom(_document("a.dwg", "Z:/a.dwg"))
    with pytest.raises(ToolFailure) as exc:
        getattr(doc, member)
    assert exc.value.code == "CAD_POLICY_VIOLATION"
    with pytest.raises(ToolFailure):
        doc.Name = "renamed"
    child = doc.ModelSpace.Item(0)                                    # wrapped all the way down
    assert isinstance(child, ReadOnlyCom)
    with pytest.raises(ToolFailure):
        child.Delete


def test_attach_errors_and_session_behaviour():
    attach = _attach_running(lambda progid: (_ for _ in ()).throw(FakeComError(
        CO_E_CLASSSTRING if progid.endswith("25.1") else MK_E_UNAVAILABLE)), FakeComError)
    with pytest.raises(ToolFailure) as exc:
        attach(["AutoCAD.Application.25.1", "AutoCAD.Application"])
    assert exc.value.code == "CAD_NOT_RUNNING"

    calls = {"n": 0}

    def flaky(_progids):
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeComError(-2147418111)                        # RPC_E_CALL_REJECTED
        return _app()

    session = ComSession(attach=flaky, com_error=FakeComError, sleep=lambda s: None, variant=Variant)
    assert session.run(lambda app, v: len(list_open_documents(app)), ["P"]) == 2 and calls["n"] == 3

    busy = ComSession(attach=lambda p: (_ for _ in ()).throw(FakeComError(-2147417846)), com_error=FakeComError,
                      sleep=lambda s: None, variant=Variant)
    with pytest.raises(ToolFailure) as exc:
        busy.run(lambda app, v: None, ["P"])
    assert exc.value.code == "CAD_BUSY" and exc.value.retryable

    other = ComSession(attach=lambda p: (_ for _ in ()).throw(FakeComError(-1)), com_error=FakeComError,
                       sleep=lambda s: None, variant=Variant)
    with pytest.raises(ToolFailure) as exc:
        other.run(lambda app, v: None, ["P"])
    assert exc.value.code == "CAD_COM_ERROR"

    gate = threading.Event()
    slow = ComSession(timeout=0.2, attach=lambda p: _app(), com_error=FakeComError, variant=Variant)
    with pytest.raises(ToolFailure) as exc:
        slow.run(lambda app, v: gate.wait(5), ["P"])
    gate.set()
    assert exc.value.code == "CAD_TIMEOUT"
    assert slow.run(lambda app, v: "recovered", ["P"]) == "recovered"   # a fresh thread after the timeout


FORBIDDEN_NAMES = {"Dispatch", "DispatchEx", "EnsureDispatch", "CreateObject", "SendCommand", "PostCommand",
                   "DynamicDispatch", "CoCreateInstance", "GetActiveObject"}
# v1: the hidden-instance runner wraps the ROT object of the AutoCAD process it started itself (PID-checked);
# COM activation (DispatchEx/CreateObject/CoCreateInstance), GetActiveObject and command members stay forbidden there
ALLOWED_BY_FILE = {"hidden_runner.py": {"Dispatch"}, "com_read.py": {"GetActiveObject"}}


def test_bridge_source_never_creates_automation_objects():
    """CAD-05: no COM activation, no attach to a running session outside the read module, no command members."""
    hits = []
    for path in (ROOT / "src" / "vkm_cad").rglob("*.py"):
        allowed = ALLOWED_BY_FILE.get(path.name, set())
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if name in FORBIDDEN_NAMES and name not in allowed:
                hits.append(f"{path.name}:{node.lineno} {name}")
    assert hits == []
    read_module = (ROOT / "src" / "vkm_cad" / "com_read.py").read_text(encoding="utf-8")
    assert "GetActiveObject" in read_module
    runner = (ROOT / "src" / "vkm_cad" / "hidden_runner.py").read_text(encoding="utf-8")
    assert "GetWindowThreadProcessId" in runner and "owner == pid" in runner       # only its own started process
    assert "_acad_pids()" in runner                                                  # refuses a running user session
    assert not (com_read.READ_ALLOWLIST & FORBIDDEN_NAMES)
