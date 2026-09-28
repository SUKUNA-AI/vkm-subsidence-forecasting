"""vkm-cad v1: jobs, the universal execution channel and the typed diploma operations.

Layer 1 — every write is a run of a job (``$VKM_WORK/cad_jobs/<job_id>``): ``scr`` / ``lisp`` / ``csharp`` run headless
in ``accoreconsole`` (/isolate, new documents only; the job drawing is opened from a copy and promoted only when the
run completes), ``python_com`` runs in a hidden full instance (gated). Layer 2 — typed operations built on layer 1:
the .NET host (``VkmCadHost``) for Civil 3D objects and sheets, LISP for plotting, PDF import and conversions, ezdxf
for drawings from a spec, the pure-Python fallback when Civil 3D is not used. Layer 3 — :meth:`CadJobs.capabilities`.

Science: outputs are DERIVED (TIN = INTERPOLATION; contours, differences, profiles, sheets, imports = DERIVATION),
listed with MODEL_CHOICE entries, ``crs_status = UNKNOWN_CRS`` unless an explicit transform was passed,
``review_status = AUTO_EXTRACTED_UNREVIEWED``, never an input of evidence extraction.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from vkm_cad import __version__, dotnet, draw, dxf, fallback, hidden, lisp, tables
from vkm_cad.audit import Audit
from vkm_cad.detect import DetectEnv, Installation, acad_clsid, locate
from vkm_cad.engine import ConsoleResult, ConsoleRun, CoreConsole
from vkm_cad.errors import ToolFailure
from vkm_cad.jobs import Job, JobStore
from vkm_cad.scratch import env_value, sha256_file

KINDS = ("scr", "lisp", "csharp", "python_com")
ENGINES = ("auto", "civil3d", "fallback")
CIVIL_ENGINE = "CIVIL3D_HEADLESS_DOTNET"
CONSOLE_ENGINE = "AUTOCAD_CORE_CONSOLE"
DEFAULT_TIMEOUT_S = 300.0
MAX_CODE_BYTES = 2_000_000
PAPER = {  # canonical media of "DWG To PDF.pc3" (verified names) → (landscape, portrait)
    "A4": ("ISO_full_bleed_A4_(297.00_x_210.00_MM)", "ISO_full_bleed_A4_(210.00_x_297.00_MM)"),
    "A3": ("ISO_full_bleed_A3_(420.00_x_297.00_MM)", "ISO_full_bleed_A3_(297.00_x_420.00_MM)"),
    "A2": ("ISO_full_bleed_A2_(594.00_x_420.00_MM)", "ISO_full_bleed_A2_(420.00_x_594.00_MM)"),
    "A1": ("ISO_full_bleed_A1_(841.00_x_594.00_MM)", "ISO_full_bleed_A1_(594.00_x_841.00_MM)"),
    "A0": (None, "ISO_full_bleed_A0_(841.00_x_1189.00_MM)"),
}
MODEL_UNITS_MM = {"m": 1000.0, "mm": 1.0, "cm": 10.0, "km": 1_000_000.0, "unitless": 1.0}

Body = str | Callable[[Path], str]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derivation(kind: str, method: str, engine: str, *, model_choices: list[str] | None = None,
               derived_from: list[str] | None = None, crs: dict[str, Any] | None = None,
               assumptions: list[str] | None = None) -> dict[str, Any]:
    crs = crs or tables.crs_block(None)
    return {"status": "DERIVED", "kind": kind, "method": method, "engine": engine,
            "model_choices": model_choices or [], "assumptions": assumptions or [],
            "derived_from": derived_from or [], **crs, "review_status": "AUTO_EXTRACTED_UNREVIEWED",
            "never_input_of_extraction": True}


class CadJobs:
    def __init__(self, store: JobStore, *, env: Mapping[str, str] | None = None,
                 installation: Callable[[], Installation | None] | None = None,
                 console_factory: Callable[["CadJobs"], Any] | None = None,
                 toolchain: Callable[["CadJobs"], tuple[dotnet.Toolchain | None, list[str]]] | None = None,
                 host_builder: Callable[["CadJobs", str, list[tuple[str, bytes]]], Path] | None = None,
                 report: Callable[[], dict[str, Any]] | None = None, audit: bool | None = None) -> None:
        self.store = store
        self.env = dict(os.environ if env is None else env)
        self._installation_fn = installation or (lambda: locate(DetectEnv.system(),
                                                                env_value(self.env, "VKM_ACAD_INSTALL_DIR")))
        self._console_factory = console_factory
        self._toolchain_fn = toolchain
        self._host_builder = host_builder
        self._report_fn = report
        self._inst: Installation | None = None
        self._inst_checked = False
        self._chain: tuple[dotnet.Toolchain | None, list[str]] | None = None
        self.audit_enabled = audit if audit is not None else (self.env.get("VKM_CAD_AUDIT", "1") != "0")

    # ================================================================================================ infrastructure
    def installation(self, required: bool = True) -> Installation | None:
        if not self._inst_checked:
            self._inst = self._installation_fn()
            self._inst_checked = True
        if self._inst is None and required:
            raise ToolFailure("CAD_ENGINE_UNAVAILABLE", "no AutoCAD with accoreconsole.exe is installed (or "
                                                        "VKM_ACAD_INSTALL_DIR is wrong)")
        return self._inst

    def console(self) -> Any:
        if self._console_factory is not None:
            return self._console_factory(self)
        inst = self.installation()
        assert inst is not None
        return CoreConsole(inst.accoreconsole, self.store.shared_dir("_isolated"), locale=inst.locale,
                           env=self.env)

    def toolchain(self, required: bool = True) -> dotnet.Toolchain | None:
        if self._chain is None:
            if self._toolchain_fn is not None:
                self._chain = self._toolchain_fn(self)
            else:
                inst = self.installation(required=False)
                self._chain = dotnet.discover(inst.install_dir if inst else None, env=self.env)
        chain, reasons = self._chain
        if chain is None and required:
            raise ToolFailure("DOTNET_UNAVAILABLE", "; ".join(reasons) or "no .NET toolchain")
        return chain

    def toolchain_reasons(self) -> list[str]:
        self.toolchain(required=False)
        assert self._chain is not None
        return self._chain[1]

    def civil_available(self) -> bool:
        inst = self.installation(required=False)
        chain = self.toolchain(required=False)
        return bool(inst and "C3D" in inst.products and chain and chain.civil)

    def build_plugin(self, name: str, sources: list[tuple[str, bytes]]) -> tuple[Path, dict[str, Any]]:
        if self._host_builder is not None:
            return self._host_builder(self, name, sources), {"cached": True}
        chain = self.toolchain()
        assert chain is not None
        built = dotnet.build_cached(chain, self.store.shared_dir("_plugins"), name, sources)
        return built.dll, {"cached": built.cached, "compile_s": round(built.duration_s, 2), **chain.identity()}

    def _job(self, job_id: str | None, product: str, label: str) -> Job:
        return self.store.get(job_id) if job_id else self._create(label=label, product=product)

    def _create(self, *, label: str | None, product: str, template: str = "default") -> Job:
        inst = self.installation(required=False)
        engine = {"accoreconsole_present": bool(inst), "release": inst.release_key if inst else None,
                  "year": inst.year if inst else None, "versions": inst.versions if inst else {},
                  "locale": inst.locale if inst else None, "bridge_version": __version__}
        return self.store.create(label=label, product=product, template=template, engine=engine)

    # ================================================================================================ console runs
    def _run(self, job: Job, kind: str, body: Body, *, save: bool, netload: list[Path] | None = None,
             prepare: Callable[[Path], dict[str, str] | None] | None = None, input_from: Path | None = None,
             fresh_drawing: bool = False, product: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S,
             code: str | None = None, extra: dict[str, Any] | None = None) -> tuple[str, Path, ConsoleResult]:
        """One accoreconsole process. The job drawing (or ``input_from``) is opened from a copy in the run
        directory; with ``save`` the result is saved there and promoted to the job drawing only if the run completed."""
        product = product or job.state()["product"]
        run_id, run_dir = job.new_run(kind)
        record: dict[str, Any] = {"kind": kind, "product": product, "engine": CONSOLE_ENGINE,
                                  "code_sha256": _sha(code.encode("utf-8")) if code is not None else None,
                                  **(extra or {})}
        try:
            env = {"VKM_CAD_RUN_DIR": str(run_dir), "VKM_CAD_RESULT": str(run_dir / "result.json"),
                   "VKM_CAD_FAILED_FLAG": str(run_dir / "FAILED")}
            if prepare is not None:
                env.update(prepare(run_dir) or {})
            input_drawing = None
            if input_from is not None:
                input_drawing = run_dir / ("in" + input_from.suffix.lower())
                shutil.copyfile(input_from, input_drawing)
            elif job.has_drawing() and not fresh_drawing:
                input_drawing = run_dir / "in.dwg"
                shutil.copyfile(job.drawing, input_drawing)
            record["input_sha256"] = sha256_file(input_drawing) if input_drawing else None
            save_to = run_dir / "out.dwg" if save else None
            text = body(run_dir) if callable(body) else body
            spec = ConsoleRun(product=product, body=text, run_id=run_id, run_dir=run_dir,
                              input_drawing=input_drawing, save_to=save_to, netload=netload or [], env=env,
                              timeout_s=timeout_s)
            inst = self.installation(required=False)
            with self.store.engine_lock(job.job_id, run_id):
                with Audit(inst.year if inst else None, enabled=self.audit_enabled, env=self.env) as audit:
                    result = self.console().run(spec)
                record["side_effects_audit"] = audit.result()
            record.update(result.as_record())
            job_scr = run_dir / "job.scr"
            record["script_sha256"] = sha256_file(job_scr) if job_scr.is_file() else None
            if result.ok and save_to is not None:
                record["drawing_out_sha256"] = job.promote_drawing(save_to, run_id)["sha256"]
            result.raise_for_status()
        except ToolFailure as exc:
            status = {"CAD_ENGINE_CRASHED": "CRASHED", "CAD_RUN_TIMEOUT": "TIMEOUT",
                      "CAD_DIALOG_BLOCKED": "DIALOG"}.get(exc.code, "FAILED")
            job.finish_run(run_id, {**record, "status": status, "error_code": exc.code})
            exc.details.setdefault("job_id", job.job_id)
            exc.details.setdefault("run_id", run_id)
            raise
        job.finish_run(run_id, {**record, "status": "OK", "error_code": None})
        return run_id, run_dir, result

    def _host(self, job: Job, ops: list[dict[str, Any]], *, save: bool = True,
              timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[str, Path, list[dict[str, Any]]]:
        if any(op["op"].startswith("c3d.") for op in ops) and job.state()["product"] != "C3D":
            raise ToolFailure("CIVIL3D_UNAVAILABLE", "Civil 3D operations need a job created with product=C3D")
        dll, compiled = self.build_plugin("VkmCadHost", [("VkmCadHost.cs", dotnet.host_source())])
        request = {"schema": "vkm-cad.host_request/1", "job_id": job.job_id, "ops": ops}
        run_dirs: list[Path] = []

        def prepare(run_dir: Path) -> dict[str, str]:
            run_dirs.append(run_dir)
            (run_dir / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            return {"VKM_CAD_REQUEST": str(run_dir / "request.json")}

        kind = "host:" + ",".join(op["op"] for op in ops)
        try:
            run_id, run_dir, _res = self._run(job, kind, "VKMHOST\n", save=save, netload=[dll], prepare=prepare,
                                              timeout_s=timeout_s, code=json.dumps(ops, sort_keys=True)[:1_000_000],
                                              extra={"compiled": compiled})
        except ToolFailure as exc:
            host = _read_json(run_dirs[0] / "result.json") if run_dirs else None
            if host is not None and exc.code == "CAD_SCRIPT_FAILED":
                errors = [{"op": o.get("op"), "error": o.get("error")} for o in host.get("ops", []) if not o.get("ok")]
                message = "; ".join(f"{e['op']}: {e['error']}" for e in errors) or str(host.get("error"))
                raise ToolFailure("HOST_OP_FAILED", message, details={"ops": errors, **exc.details}) from exc
            raise
        host = _read_json(run_dir / "result.json") or {}
        return run_id, run_dir, host.get("ops", [])

    # ================================================================================================ layer 1
    def job_create(self, label: str | None = None, product: str = "C3D", template: str = "default") -> dict[str, Any]:
        if template != "default":
            raise ToolFailure("INVALID_ARGUMENT", "template is 'default' (the product's profile template: C3D → "
                                                  "_Autodesk Civil 3D (Metric) NCS.dwt, ACAD → acadiso.dwt)")
        return self._create(label=label, product=product, template=template).summary()

    def job_status(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        return {**job.summary(), "receipt_content": json.loads(job.dir("receipt.json").read_text(encoding="utf-8"))}

    def job_list(self, limit: int = 50) -> dict[str, Any]:
        jobs = self.store.list(limit)
        return {"jobs": jobs, "count": len(jobs)}

    def exec(self, job_id: str, kind: str, code: str, save: bool = True,
             timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        if kind not in KINDS:
            raise ToolFailure("INVALID_ARGUMENT", f"kind is one of {KINDS}")
        if not code or len(code.encode("utf-8")) > MAX_CODE_BYTES:
            raise ToolFailure("INVALID_ARGUMENT", f"code is 1…{MAX_CODE_BYTES} bytes")
        job = self.store.get(job_id)
        if kind == "python_com":
            return self._python_com(job, code, save, timeout_s)
        if kind == "scr":
            run_id, run_dir, res = self._run(job, "exec:scr", code, save=save, timeout_s=timeout_s, code=code)
        elif kind == "lisp":
            def prepare(run_dir: Path) -> None:
                (run_dir / "user.lsp").write_bytes(lisp.BOM + code.encode("utf-8"))

            run_id, run_dir, res = self._run(job, "exec:lisp", lambda rd: lisp.load_user_lisp(rd / "user.lsp"),
                                             save=save, prepare=prepare, timeout_s=timeout_s, code=code)
        else:
            return self._csharp(job, code, save=save, timeout_s=timeout_s, expression=False)
        return self._exec_result(job, run_id, run_dir, res)

    def query(self, job_id: str, expression: str, kind: str = "lisp", timeout_s: float = 120.0) -> dict[str, Any]:
        if kind not in ("lisp", "csharp"):
            raise ToolFailure("INVALID_ARGUMENT", "query kind is lisp or csharp")
        if not expression.strip():
            raise ToolFailure("INVALID_ARGUMENT", "expression is empty")
        job = self.store.get(job_id)
        if kind == "csharp":
            return self._csharp(job, expression, save=False, timeout_s=timeout_s, expression=True)

        def prepare(run_dir: Path) -> None:
            (run_dir / "query.lsp").write_bytes(lisp.BOM + expression.encode("utf-8"))

        run_id, run_dir, res = self._run(job, "query:lisp", lambda rd: lisp.load_user_lisp(rd / "query.lsp"),
                                         save=False, prepare=prepare, timeout_s=timeout_s, code=expression)
        return self._exec_result(job, run_id, run_dir, res)

    def _exec_result(self, job: Job, run_id: str, run_dir: Path, res: ConsoleResult) -> dict[str, Any]:
        value = None
        if (run_dir / "result.txt").is_file():
            value = (run_dir / "result.txt").read_text(encoding="utf-8-sig", errors="replace").strip()
        user = _read_json(run_dir / "result.json")
        return {"job_id": job.job_id, "run_id": run_id, "ok": res.ok,
                "value": value if user is None else user.get("result"), "log": (user or {}).get("log"),
                "markers": res.markers[-20:], "duration_s": round(res.duration_s, 2),
                "drawing": job.state().get("drawing"), "console_tail": res.console_tail[-10:],
                "run_dir": job.logical(f"runs/{run_id}")}

    def _csharp(self, job: Job, code: str, *, save: bool, timeout_s: float, expression: bool) -> dict[str, Any]:
        chain = self.toolchain(required=self._host_builder is None)
        civil = job.state()["product"] == "C3D" and (chain.civil if chain else True)
        user = dotnet.user_source(code, civil=civil, expression=expression)
        dll, compiled = self.build_plugin("VkmCadUser", [("VkmCadHost.cs", dotnet.host_source()),
                                                         ("UserJob.cs", user)])
        kind = "query:csharp" if expression else "exec:csharp"
        run_id, run_dir, res = self._run(job, kind, "VKMUSER\n", save=save, netload=[dll], timeout_s=timeout_s,
                                         code=code, extra={"compiled": compiled})
        return self._exec_result(job, run_id, run_dir, res)

    def _python_com(self, job: Job, code: str, save: bool, timeout_s: float) -> dict[str, Any]:
        if not hidden.allowed(self.env):
            raise ToolFailure("HIDDEN_INSTANCE_NOT_ALLOWED", "python_com starts a hidden full AutoCAD, which writes "
                              "the user's AutoCAD profile; it needs the user's decision: "
                              f"{hidden.GATE}=1. Headless kinds (scr, lisp, csharp) need no gate.")
        inst = self.installation()
        assert inst is not None
        report = self._report_fn() if self._report_fn else {}
        clsid = self.env.get("VKM_CAD_ACAD_CLSID") or acad_clsid(inst) or ""
        if not clsid:
            raise ToolFailure("CAD_UNAVAILABLE", "the AutoCAD COM class id is not registered")
        civil = next((p["com"]["progid"] for p in report.get("products", []) if p.get("product") == "CIVIL3D"),
                     "AeccXUiLand.AeccApplication.13.8")
        product = job.state()["product"]
        run_id, run_dir = job.new_run("exec:python_com")
        record: dict[str, Any] = {"kind": "exec:python_com", "product": product, "engine": "HIDDEN_INSTANCE_COM",
                                  "code_sha256": _sha(code.encode("utf-8"))}
        try:
            input_drawing = None
            if job.has_drawing():
                input_drawing = run_dir / "in.dwg"
                shutil.copyfile(job.drawing, input_drawing)
            save_to = run_dir / "out.dwg" if save else None
            with self.store.engine_lock(job.job_id, run_id):
                with Audit(inst.year, enabled=True, env=self.env) as audit:
                    out = hidden.run(acad_exe=inst.acad_exe, product=product, language=inst.locale, clsid=clsid,
                                     civil_progid=civil, run_dir=run_dir, out_dir=job.dir("out"), code=code,
                                     input_drawing=input_drawing, save_to=save_to, timeout_s=timeout_s,
                                     env=self.env)
                record["side_effects_audit"] = audit.result()
            record.update({k: v for k, v in out.items() if k not in ("result", "log")})
            if save_to is not None and save_to.is_file():
                record["drawing_out_sha256"] = job.promote_drawing(save_to, run_id)["sha256"]
        except ToolFailure as exc:
            job.finish_run(run_id, {**record, "status": "FAILED", "error_code": exc.code})
            raise
        job.finish_run(run_id, {**record, "status": "OK", "error_code": None})
        return {"job_id": job.job_id, "run_id": run_id, "ok": True, "value": out.get("result"), "log": out.get("log"),
                "duration_s": out.get("duration_s"), "drawing": job.state().get("drawing"),
                "side_effects_audit": record.get("side_effects_audit"), "run_dir": job.logical(f"runs/{run_id}")}

    # ================================================================================================ layer 2 helpers
    def _engine(self, job: Job, requested: str) -> str:
        if requested not in ENGINES:
            raise ToolFailure("INVALID_ARGUMENT", f"engine is one of {ENGINES}")
        if requested == "fallback":
            return fallback.ENGINE
        civil_ok = job.state()["product"] == "C3D" and self.civil_available()
        if requested == "civil3d" and not civil_ok:
            raise ToolFailure("CIVIL3D_UNAVAILABLE", "Civil 3D headless needs a C3D job, a Civil 3D install and a C# "
                                                     "compiler; pass engine='fallback' for the pure-Python path")
        return CIVIL_ENGINE if civil_ok else fallback.ENGINE

    def _crs(self, job: Job) -> dict[str, Any]:
        return job.state().get("crs") or tables.crs_block(None)

    def _write_out(self, job: Job, name: str, data: bytes, kind: str, deriv: dict[str, Any],
                   run_id: str | None = None) -> dict[str, Any]:
        path = job.out_path(name, overwrite=True)
        path.write_bytes(data)
        return job.register_output(f"out/{path.name}", kind, deriv, run_id)

    def _copy_out(self, job: Job, source: Path, name: str, kind: str, deriv: dict[str, Any],
                  run_id: str | None) -> dict[str, Any]:
        if not source.is_file() or source.stat().st_size == 0:
            raise ToolFailure("CAD_SCRIPT_FAILED", f"the run did not produce {source.name}")
        target = job.out_path(name, overwrite=True)
        shutil.copyfile(source, target)
        return job.register_output(f"out/{target.name}", kind, deriv, run_id)

    # ------------------------------------------------------------------------------------------ drawing from spec
    def draw(self, spec: dict[str, Any], job_id: str | None = None, name: str = "drawing", to_dwg: bool = False,
             as_job_drawing: bool = False) -> dict[str, Any]:
        job = self._job(job_id, "ACAD", "cad_draw")
        doc, summary = draw.draw_spec(spec, {"VKM_JOB": job.job_id})
        spec_sha = _sha(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        deriv = derivation("DERIVATION", "drawing from a JSON spec (ezdxf)", "EZDXF", crs=self._crs(job),
                           derived_from=[f"spec sha256:{spec_sha}"])
        out = self._write_out(job, f"{_file(name)}.dxf", dxf.to_bytes(doc), "CAD_DXF", deriv)
        result: dict[str, Any] = {"job_id": job.job_id, "dxf": out, **summary}
        if to_dwg or as_job_drawing:
            if as_job_drawing and job.has_drawing():
                raise ToolFailure("WOULD_OVERWRITE", "the job already has a drawing; draw into a new job or pass "
                                                     "as_job_drawing=false")
            result["dwg"] = self._to_dwg(job, job.dir(out["path"]), _file(name), as_job_drawing)
        return result

    def _to_dwg(self, job: Job, source: Path, base: str, as_job_drawing: bool) -> dict[str, Any]:
        def body(run_dir: Path) -> str:
            target = run_dir / "converted.dwg"
            return (f'(command "_.SAVEAS" "2018" {lisp.lisp_path(target)})\n(vkm:idle)\n'
                    f'(if (findfile {lisp.lisp_path(target)}) (vkm:mark "CONVERTED") (vkm:fail "SAVEAS"))\n')

        run_id, run_dir, _res = self._run(job, "convert:dwg", body, save=False, input_from=source,
                                          code=f"convert {source.name}")
        deriv = derivation("DERIVATION", f"{source.suffix.upper().lstrip('.')} → DWG 2018 (Core Console SAVEAS)",
                           CONSOLE_ENGINE, crs=self._crs(job), derived_from=[job.logical(
                               source.relative_to(job.path).as_posix())])
        out = self._copy_out(job, run_dir / "converted.dwg", f"{base}.dwg", "CAD_DWG", deriv, run_id)
        if as_job_drawing:
            out["job_drawing"] = job.promote_drawing(run_dir / "converted.dwg", run_id)
        return out

    def convert(self, job_id: str, source: str = "drawing", to: str = "dxf", name: str | None = None,
                dxf_version: str = "2018", source_file: Path | None = None) -> dict[str, Any]:
        """``source``: ``drawing`` (the job drawing) or a job file ``out/…``/``in/…``; ``source_file``: a local DXF/DWG
        (e.g. a scratch document of ``cad_import_pdf_vector``) copied into ``in/`` first; ``to``: dwg or dxf."""
        job = self.store.get(job_id)
        if to not in ("dwg", "dxf"):
            raise ToolFailure("INVALID_ARGUMENT", "to is dwg or dxf")
        if dxf_version not in ("2000", "2004", "2007", "2010", "2013", "2018"):
            raise ToolFailure("INVALID_ARGUMENT", "dxf_version is 2000…2018")
        if source_file is not None:
            if Path(source_file).suffix.lower() not in (".dxf", ".dwg"):
                raise ToolFailure("FORMAT_NOT_SUPPORTED", "only .dxf and .dwg files are converted")
            entry = job.copy_input(Path(source_file), env=self.env)
            source = entry["path"]
        if source == "drawing":
            if not job.has_drawing():
                raise ToolFailure("INPUT_NOT_FOUND", "the job has no drawing yet")
            src = job.drawing
        else:
            if not source.startswith(("out/", "in/")) or ".." in source or "\\" in source:
                raise ToolFailure("INVALID_ARGUMENT", "source is 'drawing' or a job file under out/ or in/")
            src = job.dir(*source.split("/"))
            if not src.is_file():
                raise ToolFailure("INPUT_NOT_FOUND", f"{source} is not in the job")
        base = _file(name or (Path(source).stem if source != "drawing" else "drawing"))
        if to == "dwg":
            if src.suffix.lower() == ".dwg":
                deriv = derivation("DERIVATION", "copy of the job drawing", "FILE_COPY", crs=self._crs(job))
                return self._copy_out(job, src, f"{base}.dwg", "CAD_DWG", deriv, None)
            return self._to_dwg(job, src, base, as_job_drawing=False)
        run_id, run_dir, _res = self._run(job, "convert:dxf", lambda rd: lisp.dxfout(rd / "export.dxf", dxf_version),
                                          save=False, input_from=src, code=f"dxfout {dxf_version}")
        deriv = derivation("DERIVATION", f"DWG → DXF {dxf_version} (Core Console DXFOUT)", CONSOLE_ENGINE,
                           crs=self._crs(job), derived_from=[job.logical(src.relative_to(job.path).as_posix())])
        return self._copy_out(job, run_dir / "export.dxf", f"{base}.dxf", "CAD_DXF", deriv, run_id)

    # ------------------------------------------------------------------------------------------ points
    def _table_rows(self, job: Job, table: str | None, rows: list[dict[str, Any]] | None,
                    columns: dict[str, str] | None, decimal: str, delimiter: str | None) -> tuple[list[dict], dict]:
        if (table is None) == (rows is None):
            raise ToolFailure("INVALID_ARGUMENT", "pass either table (a file path) or rows (inline)")
        if table is not None:
            if Path(table).suffix.lower() not in tables.TABLE_SUFFIXES:
                raise ToolFailure("FORMAT_NOT_SUPPORTED", f"tables are {sorted(tables.TABLE_SUFFIXES)}")
            entry = job.copy_input(Path(table), env=self.env)
            records = tables.read_records(job.dir(entry["path"]), delimiter=delimiter)
        else:
            assert rows is not None
            entry = job.write_input_bytes("rows.json", json.dumps(rows, ensure_ascii=False, sort_keys=True)
                                          .encode("utf-8"), "inline rows")
            records = rows
        return tables.point_rows(records, columns, decimal=decimal), entry

    def points_from_table(self, *, job_id: str | None = None, table: str | None = None,
                          rows: list[dict[str, Any]] | None = None, columns: dict[str, str] | None = None,
                          point_group: str = "VKM_POINTS", decimal: str = ".", delimiter: str | None = None,
                          georeference: dict[str, Any] | None = None, units: str = "unitless",
                          engine: str = "auto", name_policy: str = "as_is") -> dict[str, Any]:
        if name_policy not in ("as_is", "group_prefix", "none"):
            raise ToolFailure("INVALID_ARGUMENT", "name_policy is as_is, group_prefix or none")
        job = self._job(job_id, "C3D", "c3d_points_from_table")
        if point_group in job.state().get("point_groups", {}):
            raise ToolFailure("WOULD_OVERWRITE", f"point group {point_group!r} exists in the job")
        transform = tables.check_transform(georeference)
        point_rows, entry = self._table_rows(job, table, rows, columns, decimal, delimiter)
        point_rows = tables.apply_transform(point_rows, transform)
        crs = tables.crs_block(transform)
        if transform is not None:
            job.update(lambda s: s.__setitem__("crs", crs))
        chosen = self._engine(job, engine)
        with_z = sum(1 for r in point_rows if r["z"] is not None)
        choices = [f"georeference: {transform['type']} ({transform['basis']})"] if transform else []
        if chosen == CIVIL_ENGINE and name_policy == "group_prefix":
            choices.append("COGO point names = <point group>_<name> (names are unique per drawing)")
        if chosen == CIVIL_ENGINE and name_policy == "none":
            choices.append("COGO points without names (numbers only); names kept in the JSON/DXF copies")
        deriv = derivation("DERIVATION", "points from a coordinate table (values unchanged"
                           + (", explicit transform applied)" if transform else ")"), chosen, crs=crs,
                           derived_from=[f"{entry['path']} sha256:{entry['sha256']} ({entry['source']})"],
                           model_choices=choices)
        result: dict[str, Any] = {"job_id": job.job_id, "engine": chosen, "point_group": point_group,
                                  "rows": len(point_rows), "with_z": with_z, "without_z": len(point_rows) - with_z,
                                  "input": entry, **crs}
        run_id = None
        if chosen == CIVIL_ENGINE:
            run_id, _rd, ops = self._host(job, [{"op": "c3d.points", "args": {
                "rows": point_rows, "point_group": point_group, "name_policy": name_policy}}])
            result.update({"civil3d": ops[0]["result"], "run_id": run_id, "drawing": job.state().get("drawing")})
        else:
            result["warnings"] = ["Civil 3D not used: the points live in the job store (DXF/JSON), not as COGO points"]
        doc = draw.geometry_document(units, {"VKM_JOB": job.job_id})
        draw.add_points(doc, point_rows, layer=_layer(point_group))
        stem = f"points_{_file(point_group)}"
        result["outputs"] = [
            self._write_out(job, f"{stem}.dxf", dxf.to_bytes(doc), "CAD_DXF", deriv, run_id),
            self._write_out(job, f"{stem}.json", _json_bytes({"point_group": point_group, "rows": point_rows, **crs}),
                            "POINTS_JSON", deriv, run_id)]
        job.update(lambda s: s.setdefault("point_groups", {}).__setitem__(point_group, {
            "engine": chosen, "rows": len(point_rows), "json": f"out/{stem}.json"}))
        return result

    def _group_points(self, job: Job, group: str) -> list[list[float]]:
        info = job.state().get("point_groups", {}).get(group)
        if not info:
            raise ToolFailure("INVALID_ARGUMENT", f"point group {group!r} is not known in this job")
        data = json.loads(job.dir(info["json"]).read_text(encoding="utf-8"))
        return [[r["x"], r["y"], r["z"]] for r in data["rows"] if r["z"] is not None]

    # ------------------------------------------------------------------------------------------ surfaces
    def tin_surface(self, *, job_id: str, name: str, point_group: str | None = None,
                    points: list[list[float]] | None = None, breaklines: list[list[list[float]]] | None = None,
                    boundary: list[list[float]] | None = None, max_triangle_length: float | None = None,
                    engine: str = "auto", units: str = "unitless") -> dict[str, Any]:
        job = self.store.get(job_id)
        if name in job.state().get("surfaces", {}):
            raise ToolFailure("WOULD_OVERWRITE", f"surface {name!r} exists in the job")
        if not point_group and not points:
            raise ToolFailure("INVALID_ARGUMENT", "pass point_group (from c3d_points_from_table) or points")
        chosen = self._engine(job, engine)
        crs = self._crs(job)
        derived_from = [f"point group {point_group}"] if point_group else [f"{len(points or [])} inline points"]
        run_id = None
        if chosen == CIVIL_ENGINE:
            choices = ["TIN: Civil 3D TIN surface (Delaunay) of the points"]
            if breaklines:
                choices.append(f"breaklines: {len(breaklines)} standard breakline(s) (enforced edges)")
            if boundary:
                choices.append(f"outer boundary with {len(boundary)} vertices (non-destructive)")
            if max_triangle_length:
                choices.append(f"maximum triangle edge {max_triangle_length:g}")
            args: dict[str, Any] = {"name": name, "breaklines": breaklines or [], "boundary": boundary or [],
                                    "max_triangle_length": max_triangle_length}
            if point_group and job.state().get("point_groups", {}).get(point_group, {}).get("engine") == CIVIL_ENGINE:
                args["point_group"] = point_group
            else:
                args["points"] = self._group_points(job, point_group) if point_group else points
            run_id, _rd, ops = self._host(job, [{"op": "c3d.tin", "args": args}])
            stats = ops[0]["result"]
            triangles = stats.pop("triangles_export", [])
            stats.pop("triangles_export_truncated", None)
        else:
            pts = self._group_points(job, point_group) if point_group else points or []
            tin = fallback.build_tin(pts, name=name, boundary=boundary, max_edge=max_triangle_length,
                                     breaklines=breaklines)
            choices = tin.model_choices
            stats = {**tin.stats(), "warnings": tin.warnings}
            triangles = tin.corners().tolist()
        deriv = derivation("INTERPOLATION", "TIN surface", chosen, model_choices=choices, derived_from=derived_from,
                           crs=crs)
        stem = f"tin_{_file(name)}"
        doc = draw.geometry_document(units, {"VKM_JOB": job.job_id, "VKM_SURFACE": name})
        draw.add_triangles(doc, triangles, _layer(f"VKM_TIN_{name}"))
        outputs = [self._write_out(job, f"{stem}.dxf", dxf.to_bytes(doc), "CAD_DXF", deriv, run_id),
                   self._write_out(job, f"{stem}.json", _json_bytes({"stats": stats, **deriv}), "SURFACE_STATS_JSON",
                                   deriv, run_id)]
        store = None
        if chosen != CIVIL_ENGINE:
            store = f"out/{stem}.tin.json"
            outputs.append(self._write_out(job, f"{stem}.tin.json", fallback.tin_json_bytes(tin), "FALLBACK_TIN",
                                           deriv))
        job.set_surface(name, {"engine": chosen, "kind": "TIN", "stats": stats, "run_id": run_id,
                               "fallback_tin": store})
        return {"job_id": job.job_id, "surface": name, "engine": chosen, "stats": stats, "model_choices": choices,
                "outputs": outputs, "run_id": run_id, "drawing": job.state().get("drawing"), **crs}

    def _fallback_tin(self, job: Job, name: str) -> fallback.Tin:
        path = job.surface(name).get("fallback_tin")
        if not path:
            raise ToolFailure("SURFACE_NOT_FOUND", f"surface {name!r} was built by Civil 3D; use engine auto/civil3d")
        return fallback.Tin.from_json(json.loads(job.dir(path).read_text(encoding="utf-8")))

    def contours(self, *, job_id: str, surface: str, interval: float, major_interval: float = 0.0,
                 units: str = "unitless") -> dict[str, Any]:
        job = self.store.get(job_id)
        if interval <= 0:
            raise ToolFailure("INVALID_ARGUMENT", "interval must be positive")
        chosen = job.surface(surface)["engine"]
        choices = [f"contour interval {interval:g}" + (f", major every {major_interval:g}" if major_interval else ""),
                   "no smoothing (straight segments between TIN edge crossings)"]
        run_id = None
        if chosen == CIVIL_ENGINE:
            run_id, _rd, ops = self._host(job, [{"op": "c3d.contours", "args": {
                "surface": surface, "interval": interval, "major_interval": major_interval}}])
            polylines = ops[0]["result"]["polylines"]
            choices.append("Civil 3D TinSurface.ExtractContoursAt at multiples of the interval (polylines in the "
                           "job drawing)")
        else:
            polylines = fallback.contours(self._fallback_tin(job, surface), interval, major_interval)
            choices.append("marching triangles on the fallback TIN")
        deriv = derivation("DERIVATION", f"contours of surface {surface}", chosen, model_choices=choices,
                           derived_from=[f"surface {surface}"], crs=self._crs(job))
        doc = draw.geometry_document(units, {"VKM_JOB": job.job_id, "VKM_SURFACE": surface})
        draw.add_contours(doc, polylines)
        stem = f"contours_{_file(surface)}"
        outputs = [self._write_out(job, f"{stem}.dxf", dxf.to_bytes(doc), "CAD_DXF", deriv, run_id),
                   self._write_out(job, f"{stem}.json", _json_bytes({"polylines": polylines, **deriv}),
                                   "CONTOURS_JSON", deriv, run_id)]
        levels = sorted({p["elevation"] for p in polylines if p.get("elevation") is not None})
        return {"job_id": job.job_id, "surface": surface, "engine": chosen, "count": len(polylines),
                "major_count": sum(1 for p in polylines if p.get("major")), "levels": levels,
                "outputs": outputs, "run_id": run_id, "model_choices": choices}

    def difference_surface(self, *, job_id: str, base: str, compare: str, name: str,
                           contour_interval: float = 0.0, contour_major: float = 0.0,
                           max_triangle_length: float | None = None, units: str = "unitless") -> dict[str, Any]:
        job = self.store.get(job_id)
        b, c = job.surface(base), job.surface(compare)
        if b["engine"] != c["engine"]:
            raise ToolFailure("INVALID_ARGUMENT", "both surfaces must come from the same engine")
        if f"{name}_DZ" in job.state().get("surfaces", {}):
            raise ToolFailure("WOULD_OVERWRITE", f"surface {name}_DZ exists in the job")
        chosen = b["engine"]
        choices = ["difference: dz = compare - base at the union of both vertex sets inside both surfaces, "
                   "triangulated again (linear interpolation)", "volumes: cut = compare below base"]
        run_id = None
        dz_store = None
        if chosen == CIVIL_ENGINE:
            run_id, _rd, ops = self._host(job, [{"op": "c3d.volume", "args": {
                "name": name, "base": base, "compare": compare, "dz_surface": f"{name}_DZ",
                "contour_interval": contour_interval, "contour_major": contour_major,
                "max_triangle_length": max_triangle_length}}])
            stats = ops[0]["result"]
            triangles = stats.pop("dz_triangles_export", [])
            isolines = stats.pop("isolines", [])
            choices.append("Civil 3D TIN volume surface for cut/fill volumes; dz TIN for the isolines")
        else:
            dz, stats = fallback.difference(self._fallback_tin(job, base), self._fallback_tin(job, compare),
                                            name=f"{name}_DZ", max_edge=max_triangle_length)
            triangles = dz.corners().tolist()
            isolines = fallback.contours(dz, contour_interval, contour_major) if contour_interval > 0 else []
            choices += dz.model_choices[1:]
        deriv = derivation("DERIVATION", f"difference surface {compare} - {base} (trough as dz)", chosen,
                           model_choices=choices, derived_from=[f"surface {base}", f"surface {compare}"],
                           crs=self._crs(job), assumptions=["a difference of two derived surfaces is not an "
                                                            "observation"])
        stem = f"diff_{_file(name)}"
        doc = draw.geometry_document(units, {"VKM_JOB": job.job_id, "VKM_SURFACE": name})
        draw.add_triangles(doc, triangles, _layer(f"VKM_DZ_{name}"))
        if isolines:
            draw.add_contours(doc, isolines, "VKM_DZ_ISOLINES", "VKM_DZ_ISOLINES_MAJOR")
        outputs = [self._write_out(job, f"{stem}.dxf", dxf.to_bytes(doc), "CAD_DXF", deriv, run_id),
                   self._write_out(job, f"{stem}.json", _json_bytes({"stats": stats, "isolines": isolines, **deriv}),
                                   "DIFFERENCE_JSON", deriv, run_id)]
        if chosen != CIVIL_ENGINE:
            dz_store = f"out/{stem}_DZ.tin.json"
            outputs.append(self._write_out(job, f"{stem}_DZ.tin.json", fallback.tin_json_bytes(dz), "FALLBACK_TIN",
                                           deriv))
        job.set_surface(f"{name}_DZ", {"engine": chosen, "kind": "DZ_TIN", "run_id": run_id,
                                       "fallback_tin": dz_store, "stats": stats.get("dz_surface")})
        return {"job_id": job.job_id, "name": name, "engine": chosen, "stats": stats, "isolines": len(isolines),
                "outputs": outputs, "run_id": run_id, "model_choices": choices, "dz_surface": f"{name}_DZ"}

    def alignment_profile(self, *, job_id: str, name: str, polyline: list[list[float]], surfaces: list[str],
                          station_interval: float = 10.0, profile_view_insert: list[float] | None = None,
                          exaggeration: float = 10.0) -> dict[str, Any]:
        job = self.store.get(job_id)
        if not surfaces:
            raise ToolFailure("INVALID_ARGUMENT", "surfaces is a non-empty list of surface names")
        engines = {job.surface(s)["engine"] for s in surfaces}
        if len(engines) != 1:
            raise ToolFailure("INVALID_ARGUMENT", "all surfaces must come from the same engine")
        fallback.stations(polyline, station_interval)                    # validates the polyline and the interval
        chosen = engines.pop()
        choices = [f"alignment from the polyline (tangents only, no curves); stations every {station_interval:g} "
                   "and at the end", "elevations by linear interpolation on each surface"]
        run_id = None
        if chosen == CIVIL_ENGINE:
            run_id, _rd, ops = self._host(job, [{"op": "c3d.alignment_profile", "args": {
                "name": name, "polyline": polyline, "surfaces": surfaces, "station_interval": station_interval,
                "profile_view_insert": profile_view_insert or []}}])
            extra = ops[0]["result"]
            rows = extra.pop("samples")
            choices.append("Civil 3D alignment + surface profiles (Profile.CreateFromSurface)")
        else:
            rows = fallback.profile([self._fallback_tin(job, s) for s in surfaces], polyline, station_interval)
            extra = {"alignment": name, "length": rows[-1]["station"] if rows else 0.0}
        deriv = derivation("DERIVATION", f"profile along {name}", chosen, model_choices=choices,
                           derived_from=[f"surface {s}" for s in surfaces], crs=self._crs(job))
        stem = f"profile_{_file(name)}"
        doc = draw.geometry_document("unitless", {"VKM_JOB": job.job_id, "VKM_PROFILE": name,
                                                  "VKM_PROFILE_AXES": f"x=station, y=z*{exaggeration:g}"})
        draw.add_profile(doc, rows, surfaces, exaggeration)
        outputs = [self._write_out(job, f"{stem}.csv", fallback.rows_to_csv(rows), "PROFILE_CSV", deriv, run_id),
                   self._write_out(job, f"{stem}.dxf", dxf.to_bytes(doc), "CAD_DXF", deriv, run_id)]
        return {"job_id": job.job_id, "engine": chosen, "samples": len(rows), "preview": rows[:20],
                "outputs": outputs, "run_id": run_id, "model_choices": choices, **extra}

    # ------------------------------------------------------------------------------------------ sheets and PDF
    def layout_sheet(self, *, job_id: str, name: str = "VKM_SHEET", paper: str = "A3",
                     orientation: str = "landscape", scale_denominator: float | None = None,
                     model_units: str = "m", model_window: list[list[float]] | None = None,
                     title_block: dict[str, Any] | None = None, style_table: str = "monochrome.ctb",
                     overwrite: bool = False) -> dict[str, Any]:
        job = self.store.get(job_id)
        if not job.has_drawing():
            raise ToolFailure("INPUT_NOT_FOUND", "the job has no drawing yet (draw, import or build objects first)")
        media, rotation = _media(paper, orientation)
        if model_units not in MODEL_UNITS_MM:
            raise ToolFailure("INVALID_ARGUMENT", f"model_units is one of {sorted(MODEL_UNITS_MM)}")
        fields = {k: str(v)[:120] for k, v in (title_block or {}).items()}
        if title_block is not None and scale_denominator and "scale" not in fields:
            fields["scale"] = f"1:{scale_denominator:g}"
        window_source = "argument"
        if model_window is None:
            model_window = _surfaces_window(job.state().get("surfaces", {}))
            window_source = "union of the job's surfaces (+5%)" if model_window else "drawing extents"
        args = {"name": name, "media": media, "rotation": rotation, "style_table": style_table,
                "paper_mm_per_model_unit": MODEL_UNITS_MM[model_units], "scale_denominator": scale_denominator,
                "model_window": model_window or [], "title_block": fields if title_block is not None else None,
                "overwrite": overwrite, "landscape": orientation == "landscape"}
        run_id, _rd, ops = self._host(job, [{"op": "acad.layout_sheet", "args": args}])
        job.update(lambda s: s.setdefault("layouts", {}).__setitem__(name, {"media": media, "run_id": run_id}))
        return {"job_id": job.job_id, "run_id": run_id, "drawing": job.state().get("drawing"),
                "model_window_source": window_source,
                "assumptions": [f"model units = {model_units} (tool argument; the drawing's own units are not "
                                "inferred)"], **ops[0]["result"]}

    def plot_pdf(self, *, job_id: str, layouts: list[str], out_name: str = "sheet", paper: str = "A3",
                 orientation: str = "landscape", style_table: str = "monochrome.ctb") -> dict[str, Any]:
        job = self.store.get(job_id)
        if not job.has_drawing():
            raise ToolFailure("INPUT_NOT_FOUND", "the job has no drawing yet")
        if not layouts or len(layouts) > 50:
            raise ToolFailure("INVALID_ARGUMENT", "layouts: 1…50 names ('Model' plots the model space extents)")
        media, _rot = _media(paper, orientation)

        def body(run_dir: Path) -> str:
            text = ""
            for k, layout in enumerate(layouts):
                pdf = run_dir / f"plot_{k + 1:02d}.pdf"
                if layout.lower() == "model":
                    text += lisp.plot_model(pdf, media, style_table, landscape=orientation == "landscape")
                else:
                    text += lisp.plot_layout(layout, pdf)
            return text

        run_id, run_dir, _res = self._run(job, "plot_pdf", body, save=False, code=json.dumps(layouts))
        drawing = job.state()["drawing"]
        outputs = []
        for k, layout in enumerate(layouts):
            name = f"{_file(out_name)}.pdf" if len(layouts) == 1 else \
                f"{_file(out_name)}_{k + 1:02d}_{_file(layout)}.pdf"
            deriv = derivation("DERIVATION", f"plot of {layout} (DWG To PDF.pc3, {style_table})", CONSOLE_ENGINE,
                               crs=self._crs(job), derived_from=[f"{job.logical('work/drawing.dwg')} "
                                                                 f"sha256:{drawing['sha256']}"])
            outputs.append(self._copy_out(job, run_dir / f"plot_{k + 1:02d}.pdf", name, "PDF", deriv, run_id))
        return {"job_id": job.job_id, "run_id": run_id, "pdfs": outputs}

    def pdf_import(self, *, pdf: str, pages: list[int] | None = None, job_id: str | None = None,
                   scale: float = 1.0) -> dict[str, Any]:
        pages = pages or [1]
        if len(pages) > 20 or any(int(p) < 1 for p in pages):
            raise ToolFailure("INVALID_ARGUMENT", "pages: 1…20 page numbers (1-based)")
        if not 0 < scale <= 1e6:
            raise ToolFailure("INVALID_ARGUMENT", "scale must be positive")
        if Path(pdf).suffix.lower() != ".pdf":
            raise ToolFailure("FORMAT_NOT_SUPPORTED", "cad_pdf_import reads .pdf files")
        if not Path(pdf).is_file():
            raise ToolFailure("INPUT_NOT_FOUND", f"input file not found: {Path(pdf).name}")
        job = self._job(job_id, "ACAD", "cad_pdf_import")
        entry = job.copy_input(Path(pdf), env=self.env)
        src = job.dir(entry["path"])
        results = []
        for page in pages:
            def prepare(run_dir: Path) -> None:
                shutil.copyfile(src, run_dir / "input.pdf")

            def body(run_dir: Path, page: int = int(page)) -> str:
                dwg = run_dir / "import.dwg"
                return (lisp.pdf_import(run_dir / "input.pdf", page, scale=scale) + lisp.extents_marker()
                        + f'(command "_.SAVEAS" "2018" {lisp.lisp_path(dwg)})\n(vkm:idle)\n'
                        + lisp.dxfout(run_dir / "import.dxf"))

            run_id, run_dir, res = self._run(job, "pdf_import", body, save=False, prepare=prepare,
                                             fresh_drawing=True, product="ACAD", code=f"page {page}")
            deriv = derivation("DERIVATION", f"PDF page {page} vectors → drawing (AutoCAD -PDFIMPORT)",
                               CONSOLE_ENGINE, derived_from=[f"{entry['path']} sha256:{entry['sha256']}"],
                               model_choices=[f"insert 0,0 (page lower-left); scale {scale:g}; rotation 0"],
                               assumptions=["drawing units as PDFIMPORT sets them: 1 unit = 1 inch of the page at "
                                            "scale 1, unless the PDF carries its own measurement scale (PDFs "
                                            "plotted by AutoCAD); page coordinates, not map coordinates"])
            stem = f"{_file(Path(pdf).stem)}_p{int(page):03d}"
            out_dwg = self._copy_out(job, run_dir / "import.dwg", f"{stem}.dwg", "CAD_DWG", deriv, run_id)
            out_dxf = self._copy_out(job, run_dir / "import.dxf", f"{stem}.dxf", "CAD_DXF", deriv, run_id)
            _rows, summary = dxf.extract_geometry(dxf.read_bytes(job.dir(out_dxf["path"]).read_bytes()), layers=None,
                                                  types=None, limit=200_000)
            extents = next((m[8:] for m in res.markers if m.startswith("EXTENTS ")), None)
            results.append({"page": int(page), "run_id": run_id, "dwg": out_dwg, "dxf": out_dxf, "summary": summary,
                            "extents": extents, "coordinate_space": "DRAWING_UNITS", "crs_status": "UNKNOWN_CRS"})
        return {"job_id": job.job_id, "input": entry, "pages": results}

    # ================================================================================================ layer 3
    def capabilities(self) -> dict[str, Any]:
        from vkm_cad.capabilities import build

        return build(self)


def _media(paper: str, orientation: str) -> tuple[str, int]:
    paper = str(paper).upper()
    if paper not in PAPER or orientation not in ("landscape", "portrait"):
        raise ToolFailure("INVALID_ARGUMENT", f"paper is one of {sorted(PAPER)}; orientation landscape|portrait")
    landscape, portrait = PAPER[paper]
    if orientation == "landscape":
        return (portrait, 90) if landscape is None else (landscape, 0)
    return portrait, 0


def _surfaces_window(surfaces: dict[str, Any]) -> list[list[float]] | None:
    boxes = [s["stats"] for s in surfaces.values()
             if isinstance(s.get("stats"), dict) and all(s["stats"].get(k) is not None
                                                          for k in ("x_min", "y_min", "x_max", "y_max"))]
    if not boxes:
        return None
    x0, y0 = min(b["x_min"] for b in boxes), min(b["y_min"] for b in boxes)
    x1, y1 = max(b["x_max"] for b in boxes), max(b["y_max"] for b in boxes)
    dx, dy = max(x1 - x0, 1e-6) * 0.05, max(y1 - y0, 1e-6) * 0.05
    return [[x0 - dx, y0 - dy], [x1 + dx, y1 + dy]]


def _layer(name: str) -> str:
    bad = '<>/\\":;?*|=`'
    return "".join("_" if ch in bad else ch for ch in name)[:255] or "VKM"


def _file(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name))
    return keep.strip("._")[:60] or "item"


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n").encode("utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return None
