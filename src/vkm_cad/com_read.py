"""Read-only access to drawings the user opened in AutoCAD — attach only, allow-list only.

* The bridge attaches with ``GetActiveObject`` to an AutoCAD the user started. It never creates an application object
  (that would launch ``acad.exe /Automation``); a missing AutoCAD is ``CAD_NOT_RUNNING``.
* Every COM object is wrapped in :class:`ReadOnlyCom`: only members in :data:`READ_ALLOWLIST` can be read or called;
  setting attributes is refused. Save, close, command and variable-setting members are therefore unreachable (CAD-06).
* All calls run on one STA thread with a per-call timeout; calls rejected by a busy AutoCAD (modal dialog, running
  command) are retried with backoff, then reported as ``CAD_BUSY``.
* Results carry file names only — never the directories of the user's documents.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Iterable

from vkm_cad.errors import ToolFailure

READ_ALLOWLIST = frozenset({
    "ActiveDocument", "Documents", "Count", "Item", "Name", "FullName", "ReadOnly", "Saved", "Layers", "ModelSpace",
    "PaperSpace", "ObjectName", "Handle", "Layer", "GetBoundingBox", "GetVariable", "LayerOn", "Freeze", "Lock",
    "Color", "Linetype", "Lineweight", "Plottable", "Version",
})
MK_E_UNAVAILABLE = -2147221021
CO_E_CLASSSTRING = -2147221005
REGDB_E_CLASSNOTREG = -2147221164
RETRY_HRESULTS = {-2147418111: "RPC_E_CALL_REJECTED", -2147417846: "RPC_E_SERVERCALL_RETRYLATER"}
RETRY_DELAYS = (0.0, 0.2, 0.5, 1.0, 2.0)
CALL_TIMEOUT_S = 30.0
MAX_ENTITIES = 1000
INSUNITS = {0: "Unitless", 1: "Inches", 2: "Feet", 3: "Miles", 4: "Millimeters", 5: "Centimeters", 6: "Meters",
            7: "Kilometers", 8: "Microinches", 9: "Mils", 10: "Yards", 11: "Angstroms", 12: "Nanometers",
            13: "Microns", 14: "Decimeters", 15: "Dekameters", 16: "Hectometers", 17: "Gigameters",
            18: "Astronomical units", 19: "Light years", 20: "Parsecs", 21: "US Survey Feet"}


def hresult(exc: BaseException) -> int | None:
    value = getattr(exc, "hresult", None)
    if value is None and exc.args and isinstance(exc.args[0], int):
        value = exc.args[0]
    return value


def _is_com_object(value: Any) -> bool:
    return hasattr(value, "_oleobj_")


class ReadOnlyCom:
    """Allow-list proxy over a COM dispatch object (or a test double)."""

    __slots__ = ("_obj",)

    def __init__(self, obj: Any) -> None:
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name: str) -> Any:
        if name not in READ_ALLOWLIST:
            raise ToolFailure("CAD_POLICY_VIOLATION", f"COM member {name!r} is not on the read allow-list")
        return wrap(getattr(object.__getattribute__(self, "_obj"), name))

    def __setattr__(self, name: str, value: Any) -> None:
        raise ToolFailure("CAD_POLICY_VIOLATION", "COM objects are read-only in vkm-cad")

    def __delattr__(self, name: str) -> None:
        raise ToolFailure("CAD_POLICY_VIOLATION", "COM objects are read-only in vkm-cad")


def wrap(value: Any) -> Any:
    if isinstance(value, ReadOnlyCom):
        return value
    if _is_com_object(value):
        return ReadOnlyCom(value)
    if callable(value):
        return lambda *args: wrap(value(*args))
    return value


class ComSession:
    """One STA worker thread for COM calls; recreated after a timeout (a stuck call is abandoned)."""

    def __init__(self, timeout: float = CALL_TIMEOUT_S, attach: Callable[[list[str]], Any] | None = None,
                 com_error: type[BaseException] | None = None, sleep: Callable[[float], None] = time.sleep,
                 variant: Callable[[], Any] | None = None) -> None:
        self.timeout = timeout
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._attach = attach
        self._com_error = com_error
        self._sleep = sleep
        self._variant = variant

    # ---------------------------------------------------------------------------------------- platform binding
    def _bind(self) -> None:
        if self._attach is not None:
            return
        if sys.platform != "win32":
            raise ToolFailure("CAD_UNAVAILABLE", "AutoCAD COM is only available on Windows")
        try:
            import pywintypes  # type: ignore[import-not-found]
            import win32com.client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ToolFailure("CAD_UNAVAILABLE", "pywin32 is not installed (extra 'desktop')") from exc
        self._com_error = pywintypes.com_error
        self._attach = _attach_running(win32com.client.GetActiveObject, pywintypes.com_error)
        self._variant = _variant_array

    def _init_thread(self) -> None:
        if sys.platform == "win32" and self._variant is _variant_array:
            import pythoncom  # type: ignore[import-not-found]

            pythoncom.CoInitialize()

    def run(self, fn: Callable[[Any], Any], progids: list[str]) -> Any:
        """Attach and run ``fn(app)`` on the COM thread; ``app`` is a :class:`ReadOnlyCom`."""
        self._bind()
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vkm-cad-com",
                                                    initializer=self._init_thread)
            future: Future = self._executor.submit(self._call, fn, progids)
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout as exc:
            with self._lock:
                if self._executor is not None:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                    self._executor = None
            raise ToolFailure("CAD_TIMEOUT", f"AutoCAD did not answer within {self.timeout:.0f} s",
                              retryable=True) from exc

    def _call(self, fn: Callable[[Any], Any], progids: list[str]) -> Any:
        assert self._attach is not None
        for delay in RETRY_DELAYS:
            if delay:
                self._sleep(delay)
            try:
                app = ReadOnlyCom(self._attach(progids))
                return fn(app, self._variant)
            except ToolFailure:
                raise
            except Exception as exc:  # noqa: BLE001 - COM errors are classified below
                if self._com_error is not None and isinstance(exc, self._com_error) and \
                        hresult(exc) in RETRY_HRESULTS:
                    continue
                if self._com_error is not None and isinstance(exc, self._com_error):
                    raise ToolFailure("CAD_COM_ERROR", f"COM call failed (HRESULT {hresult(exc)})") from exc
                raise
        raise ToolFailure("CAD_BUSY", "AutoCAD rejected the calls (a dialog or command may be active); try again",
                          retryable=True)


def _attach_running(get_active: Callable[[str], Any], com_error: type[BaseException]) -> Callable[[list[str]], Any]:
    def attach(progids: list[str]) -> Any:
        for progid in progids:
            try:
                return get_active(progid)
            except com_error as exc:
                if hresult(exc) in (MK_E_UNAVAILABLE, CO_E_CLASSSTRING, REGDB_E_CLASSNOTREG):
                    continue
                raise
        raise ToolFailure("CAD_NOT_RUNNING", "no running AutoCAD to attach to; the bridge never starts AutoCAD — "
                                             "open the drawing in AutoCAD first")
    return attach


def _variant_array() -> Any:
    import pythoncom  # type: ignore[import-not-found]
    import win32com.client  # type: ignore[import-not-found]

    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0])


# ---------------------------------------------------------------------------------------------------- operations
def _safe(read: Callable[[], Any], default: Any = None) -> Any:
    try:
        return read()
    except ToolFailure:
        raise
    except Exception:  # noqa: BLE001 - optional properties differ between object types
        return default


def _documents(app: Any) -> list[Any]:
    docs = app.Documents
    return [docs.Item(i) for i in range(int(docs.Count))]


def list_open_documents(app: Any, _variant: Any = None) -> list[dict[str, Any]]:
    active = _safe(lambda: app.ActiveDocument.Name)
    out = []
    for n, doc in enumerate(_documents(app), 1):
        name = str(_safe(lambda: doc.Name, ""))
        full = str(_safe(lambda: doc.FullName, "") or "")
        out.append({"doc_ref": f"#{n}", "name": name, "active": name == active,
                    "read_only": bool(_safe(lambda: doc.ReadOnly, False)), "saved": bool(_safe(lambda: doc.Saved,
                                                                                             False)),
                    "has_file": bool(full) and os.path.isabs(full),
                    "format": os.path.splitext(name)[1].lstrip(".").lower() or None})
    return out


def find_document(app: Any, doc_ref: str) -> Any:
    docs = _documents(app)
    if doc_ref.startswith("#") and doc_ref[1:].isdigit():
        index = int(doc_ref[1:])
        if 1 <= index <= len(docs):
            return docs[index - 1]
    else:
        matches = [d for d in docs if str(_safe(lambda d=d: d.Name, "")).lower() == doc_ref.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolFailure("CAD_DOCUMENT_NOT_FOUND", f"{len(matches)} open documents are named {doc_ref!r}; "
                                                        "use the '#n' reference from cad_list_open_documents")
    raise ToolFailure("CAD_DOCUMENT_NOT_FOUND", f"no open document {doc_ref!r}")


def get_layers(doc: Any) -> list[dict[str, Any]]:
    layers = doc.Layers
    out = []
    for i in range(int(layers.Count)):
        layer = layers.Item(i)
        out.append({"name": str(layer.Name), "on": bool(_safe(lambda: layer.LayerOn, True)),
                    "frozen": bool(_safe(lambda: layer.Freeze, False)), "locked": bool(_safe(lambda: layer.Lock,
                                                                                          False)),
                    "color": _safe(lambda: int(layer.Color)), "linetype": _safe(lambda: str(layer.Linetype))})
    return out


def _point(value: Any) -> list[float] | None:
    try:
        return [float(v) for v in value][:3]
    except (TypeError, ValueError):
        return None


def get_extents(doc: Any, space: str) -> dict[str, Any]:
    lo, hi = ("EXTMIN", "EXTMAX") if space == "model" else ("PEXTMIN", "PEXTMAX")
    extmin, extmax = _point(doc.GetVariable(lo)), _point(doc.GetVariable(hi))
    insunits = _safe(lambda: int(doc.GetVariable("INSUNITS")))
    valid = bool(extmin and extmax and all(a <= b for a, b in zip(extmin, extmax))
                 and max(abs(v) for v in extmin + extmax) < 1e19)
    return {"space": space, "extmin": extmin, "extmax": extmax, "valid": valid, "insunits": insunits,
            "insunits_name": INSUNITS.get(insunits) if insunits is not None else None,
            "measurement": _safe(lambda: int(doc.GetVariable("MEASUREMENT"))),
            "coordinate_space": "DRAWING_UNITS", "crs_status": "UNKNOWN_CRS"}


def _bounding_box(entity: Any, variant: Callable[[], Any] | None) -> list[list[float]] | None:
    if variant is None:
        return None
    lo, hi = variant(), variant()
    try:
        entity.GetBoundingBox(lo, hi)
    except ToolFailure:
        raise
    except Exception:  # noqa: BLE001 - some entities have no extents
        return None
    a, b = _point(getattr(lo, "value", None)), _point(getattr(hi, "value", None))
    return [a, b] if a and b else None


def list_entities(doc: Any, variant: Callable[[], Any] | None, *, layers: Iterable[str] | None,
                  types: Iterable[str] | None, limit: int, cursor: int, include_bbox: bool) -> dict[str, Any]:
    if not 1 <= limit <= MAX_ENTITIES:
        raise ToolFailure("PAYLOAD_TOO_LARGE", f"limit must be 1…{MAX_ENTITIES}")
    layer_set = {x.lower() for x in layers} if layers else None
    type_set = {x.lower() for x in types} if types else None
    space = doc.ModelSpace
    total = int(space.Count)
    out: list[dict[str, Any]] = []
    index = max(0, cursor)
    while index < total and len(out) < limit:
        entity = space.Item(index)
        index += 1
        name = str(_safe(lambda: entity.ObjectName, ""))
        layer = str(_safe(lambda: entity.Layer, ""))
        if layer_set is not None and layer.lower() not in layer_set:
            continue
        if type_set is not None and name.lower() not in type_set:
            continue
        row = {"handle": str(_safe(lambda: entity.Handle, "")), "object_name": name, "layer": layer}
        if include_bbox:
            row["bbox"] = _bounding_box(entity, variant)
        out.append(row)
    return {"entities": out, "total_in_model_space": total, "next_cursor": index if index < total else None,
            "coordinate_space": "DRAWING_UNITS", "crs_status": "UNKNOWN_CRS"}


def get_coordinate_system(doc: Any) -> dict[str, Any]:
    cgeocs = _safe(lambda: doc.GetVariable("CGEOCS"))
    insunits = _safe(lambda: int(doc.GetVariable("INSUNITS")))
    return {"cgeocs_raw": str(cgeocs) if cgeocs not in (None, "") else None,
            "crs_code_source": "CGEOCS" if cgeocs not in (None, "") else None,
            "insunits": insunits, "insunits_name": INSUNITS.get(insunits) if insunits is not None else None,
            "measurement": _safe(lambda: int(doc.GetVariable("MEASUREMENT"))),
            "civil3d_cs": "NOT_QUERIED_IN_V0",
            "coordinate_space": "DRAWING_UNITS", "crs_status": "UNKNOWN_CRS", "epsg": None,
            "note": "the drawing's own code is reported raw; crs_status is never raised automatically and no EPSG "
                    "is inferred"}


def saved_file_info(doc: Any) -> dict[str, Any]:
    """Where the last saved state of the document lives (used only inside the bridge; never returned)."""
    return {"name": str(_safe(lambda: doc.Name, "")), "full_name": str(_safe(lambda: doc.FullName, "") or ""),
            "saved": bool(_safe(lambda: doc.Saved, False)), "read_only": bool(_safe(lambda: doc.ReadOnly, False))}
