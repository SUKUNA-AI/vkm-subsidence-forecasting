"""CADFIX (29.09.2026): ``cad_layout_sheet`` in a plain-AutoCAD job (a ``cad_draw`` DXF made the job drawing) crashed the
Core Console.

Root cause, from the .NET runtime event of the crashed runs: ``System.AccessViolationException`` in
``AcDbViewport.setIsOn`` ← ``Viewport.set_On`` ← ``CoreOps.LayoutSheetSteps``. In the plain AutoCAD Core Console
(``/product ACAD``) ``Viewport.On = true`` inside a transaction that also erased a viewport of the same layout (AutoCAD's
default viewport of the new layout) ends the process. A fresh ``acadiso`` drawing crashed the same way; the Civil 3D
console survives that order, which is why the v1 smoke (a Civil 3D job) passed. Without AutoCAD these tests pin the
host's order in the C# source and the crash report of the Python side; the live runs are in
``test_cad_layout_sheet_live.py``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from vkm_cad import dotnet
from vkm_cad.cadjobs import CadJobs, host_trace_tail
from vkm_cad.detect import Installation
from vkm_cad.engine import ConsoleResult
from vkm_cad.errors import ToolFailure
from vkm_cad.jobs import JobStore


# ------------------------------------------------------------------------------------------------ C# source order
def _code_only(src: str) -> str:
    """The C# source with comments, string and char literals blanked (same length), so braces and tokens are code."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif src[i] == '"' or two in ('@"', '$"'):
            verbatim = two == '@"'
            j = i + (2 if two in ('@"', '$"') else 1)
            while j < n:
                if verbatim and src[j] == '"' and src[j + 1:j + 2] == '"':
                    j += 2
                    continue
                if not verbatim and src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
        elif src[i] == "'":
            j = i + 1
            while j < n and src[j] != "'":
                j += 2 if src[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _transaction_blocks(code: str) -> list[str]:
    """Bodies of ``using (var tr = ….StartTransaction())`` blocks."""
    blocks = []
    for m in re.finditer(r"using\s*\(\s*var\s+\w+\s*=\s*[\w.]+\.StartTransaction\(\)\s*\)", code):
        start = code.index("{", m.end())
        depth, k = 0, start
        while True:
            if code[k] == "{":
                depth += 1
            elif code[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        blocks.append(code[start:k + 1])
    return blocks


def _method(code: str, name: str) -> str:
    m = re.search(r"\b" + name + r"\s*\([^)]*\)\s*\{", code)
    assert m, f"method {name} not found in the host source"
    depth, k = 0, m.end() - 1
    while True:
        depth += {"{": 1, "}": -1}.get(code[k], 0)
        if depth == 0:
            return code[m.start():k + 1]
        k += 1


def test_no_viewport_is_turned_on_in_a_transaction_that_erases_one():
    code = _code_only(dotnet.host_source().decode("utf-8"))
    blocks = _transaction_blocks(code)
    turn_on = [b for b in blocks if re.search(r"\.On\s*=\s*true", b)]
    assert turn_on, "the host no longer turns a viewport on anywhere: update this regression test"
    for body in turn_on:                    # the Core Console (/product ACAD) crashes in AcDbViewport::setIsOn otherwise
        assert ".Erase(" not in body
    assert len(re.findall(r"\.On\s*=\s*true", code)) == sum(len(re.findall(r"\.On\s*=\s*true", b)) for b in turn_on)


def test_page_setup_before_activation_and_the_default_viewport_is_kept():
    code = _code_only(dotnet.host_source().decode("utf-8"))
    steps = _method(code, "LayoutSheetSteps")
    activation = steps.index("lm.CurrentLayout = name")
    assert 0 <= steps.find("PageSetup(") < activation                 # the new layout never binds to a system printer
    assert "SetPlotConfigurationName" not in steps                    # only through PageSetup
    assert "SetPlotConfigurationName" in _method(code, "PageSetup")
    after = steps[activation:]
    assert "reuseDefault" in after and ".Erase()" in after              # AutoCAD's default viewport is reused, others go
    back = [m.start() for m in re.finditer(r'lm\.CurrentLayout\s*=\s*"', steps)]   # literals are blanked: "Model"
    assert back and back[-1] > steps.rindex(".On = true")              # back to Model after the viewport is on


# ------------------------------------------------------------------------------------------------ crash report
class CrashingConsole:
    """The engine crashed while the host was at its last breadcrumb (no result.json, exit code 1, WER child killed)."""

    def __init__(self, trace: list[str]):
        self.trace = trace
        self.specs: list = []

    def run(self, spec):
        self.specs.append(spec)
        (spec.run_dir / "host_trace.txt").write_text("\n".join(self.trace) + "\n", encoding="utf-8")
        return ConsoleResult(exit_code=1, timed_out=False, crashed=True, dialog=False, duration_s=2.1,
                             script_read=True, completed=False, ok=False,
                             markers=[f"BEGIN {spec.run_id}", "NETLOAD VkmCadHost.dll"], failures=[],
                             console_tail=["Регенерация модели - кэширование видовых экранов."],
                             killed=["werfault.exe", "accoreconsole.exe"], windows=[], foreign_cad_processes={},
                             command=["accoreconsole.exe"])


def _jobs(tmp_path: Path, console) -> CadJobs:
    inst = Installation(tmp_path / "acad", "R25.1", 2026, "ru-RU", "rus", {"ACAD": "ACAD-9101:419"},
                        {"ACAD": "25.1.60.0"})
    chain = dotnet.Toolchain(["csc.exe"], "VS_ROSLYN", tmp_path, "8.0.22", tmp_path, False, [])
    return CadJobs(JobStore(tmp_path / "jobs"), env={}, installation=lambda: inst, console_factory=lambda _j: console,
                   toolchain=lambda _j: (chain, []), host_builder=lambda _j, name, _s: tmp_path / f"{name}.dll",
                   audit=False)


def test_a_crash_of_the_host_names_its_last_stage(tmp_path):
    trace = ["op acad.layout_sheet", "acad.layout_sheet: layout", "acad.layout_sheet: commit page setup",
             "acad.layout_sheet: activate layout", "acad.layout_sheet: viewport on"]
    jobs = _jobs(tmp_path, CrashingConsole(trace))
    jid = jobs.job_create("line 12", "ACAD")["job_id"]
    job = jobs.store.get(jid)
    seed = tmp_path / "seed.dwg"
    seed.write_bytes(b"AC1032 drawing from cad_draw")
    before = job.promote_drawing(seed, "R000")
    with pytest.raises(ToolFailure) as exc:
        jobs.layout_sheet(job_id=jid, name="LINE12_PROFILE", model_units="unitless",
                          title_block={"title": "Профиль оседаний по профильной линии 12"})
    assert exc.value.code == "CAD_ENGINE_CRASHED" and exc.value.details["run_id"] == "R001"
    assert exc.value.details["host_trace_tail"][-1] == "acad.layout_sheet: viewport on"
    state = job.state()
    run = state["runs"][-1]
    assert run["status"] == "CRASHED" and run["host_trace_tail"] == trace
    assert state["drawing"]["sha256"] == before["sha256"]              # nothing promoted
    receipt = job.dir("receipt.json").read_text(encoding="utf-8")
    assert "viewport on" in receipt and str(tmp_path) not in receipt and str(tmp_path).replace("\\", "/") not in receipt


def test_capabilities_name_the_viewport_pitfall(tmp_path):
    caps = _jobs(tmp_path, CrashingConsole([])).capabilities()
    assert any("AcDbViewport::setIsOn" in p and "erased" in p for p in caps["headless_pitfalls"])


def test_host_trace_tail(tmp_path):
    assert host_trace_tail(tmp_path) == []
    (tmp_path / "host_trace.txt").write_text("\n".join(f"s{i}" for i in range(20)) + "\n\n" + "x" * 300 + "\n",
                                             encoding="utf-8")
    tail = host_trace_tail(tmp_path)
    assert len(tail) == 8 and tail[0] == "s13" and tail[-1] == "x" * 200


def test_layout_sheet_request_of_a_plain_autocad_job(tmp_path):
    """A job made by cad_draw is an ACAD job: the sheet goes to the .NET host without Civil 3D and without asking for a
    host-created viewport (the host reuses the layout's default viewport unless told otherwise)."""

    class OkConsole:
        def __init__(self):
            self.specs: list = []

        def run(self, spec):
            self.specs.append(spec)
            ops = json.loads(Path(spec.env["VKM_CAD_REQUEST"]).read_text(encoding="utf-8"))["ops"]
            result = {"layout": ops[0]["args"]["name"], "media": ops[0]["args"]["media"],
                      "viewport": "default viewport of the layout (created by AutoCAD on activation)"}
            (spec.run_dir / "result.json").write_text(json.dumps({"ok": True, "ops": [
                {"op": ops[0]["op"], "ok": True, "result": result}]}), encoding="utf-8")
            spec.save_to.write_bytes(b"AC1032 saved")
            return ConsoleResult(exit_code=0, timed_out=False, crashed=False, dialog=False, duration_s=0.1,
                                 script_read=True, completed=True, ok=True,
                                 markers=[f"BEGIN {spec.run_id}", f"END {spec.run_id} OK"], failures=[],
                                 console_tail=[], killed=[], windows=[], foreign_cad_processes={},
                                 command=["accoreconsole.exe"])

    console = OkConsole()
    jobs = _jobs(tmp_path, console)
    jid = jobs.job_create("line 12", "ACAD")["job_id"]
    seed = tmp_path / "seed.dwg"
    seed.write_bytes(b"AC1032 drawing")
    jobs.store.get(jid).promote_drawing(seed, "R000")
    sheet = jobs.layout_sheet(job_id=jid, name="LINE12_PROFILE", model_units="unitless", style_table="acad.ctb",
                              model_window=[[-25, -255], [385, 20]], title_block={"title": "Профиль", "scale": "усл."})
    args = json.loads((console.specs[0].run_dir / "request.json").read_text(encoding="utf-8"))["ops"][0]["args"]
    assert console.specs[0].product == "ACAD" and "reuse_default_viewport" not in args
    assert args["style_table"] == "acad.ctb" and args["title_block"]["scale"] == "усл."
    assert sheet["viewport"].startswith("default viewport") and sheet["drawing"]["from_run"] == "R001"
