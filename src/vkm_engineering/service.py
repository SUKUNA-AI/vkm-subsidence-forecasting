"""Rooted inputs + fixed argv profiles on the existing isolated job runner.

Configuration is a private JSON file named by VKM_ENGINEERING_CONFIG. This
module never accepts executable paths, scripts, flags, or output paths from MCP.
The job directory is not a security sandbox for the native application.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Mapping

from vkm_jobs.errors import ToolFailure
from vkm_jobs.receipt import sha256_file
from vkm_jobs.roots import SimRoot, is_inside, repo_root, resolve_inside
from vkm_jobs.service import JobsService, git_info
from vkm_jobs.spec import read_json

TOOLS = ("gmsh", "paraview", "cloudcompare")
OWNER = "vkm-engineering.fixed_profiles/1"
PV_EXT = {".vtu", ".vtp", ".vtk", ".stl", ".ply"}
CAD_EXT = {".step", ".stp", ".brep", ".iges", ".igs"}
CLOUD_EXT = {".ply", ".xyz", ".asc", ".las", ".laz"}
# CC BIN can contain an embedded global shift that overrides -GLOBAL_SHIFT.
# Exclude it (and linked OBJ materials) rather than silently accepting a transform.
MESH_EXT = {".ply", ".stl"}


def fail(message: str, code: str = "INVALID_ARGUMENT"):
    raise ToolFailure(code, message)


def vector(value, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        fail(f"{name} needs three finite numbers")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
        fail(f"{name} needs three finite numbers")
    return [float(x) for x in value]


def positive(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        fail(f"{name} must be finite and positive")
    return float(value)


class EngineeringService:
    def __init__(self, config: dict | None = None, env: Mapping[str, str] | None = None):
        self.env = dict(os.environ if env is None else env)
        self.config_sha = None
        if config is None:
            raw = self.env.get("VKM_ENGINEERING_CONFIG")
            if not raw:
                config = {}
            else:
                p = Path(raw)
                if not p.is_absolute() or not p.is_file():
                    fail("VKM_ENGINEERING_CONFIG must name an existing private JSON file", "INVALID_ARGUMENT")
                try:
                    config = json.loads(p.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    raise ToolFailure("INVALID_ARGUMENT", "invalid engineering configuration") from exc
                self.config_sha = sha256_file(p)
        if not isinstance(config, dict) or config.get("schema", "vkm.engineering_config/1") != "vkm.engineering_config/1":
            fail("configuration schema must be vkm.engineering_config/1", "INVALID_ARGUMENT")
        if set(config) - {"schema", "executables", "read_roots", "job_root", "max_input_bytes"}:
            fail("unknown configuration keys", "INVALID_ARGUMENT")
        self.config = config
        self.executables = config.get("executables", {})
        self.read_roots = config.get("read_roots", {})
        if not isinstance(self.executables, dict) or set(self.executables) - set(TOOLS):
            fail("invalid executable configuration", "INVALID_ARGUMENT")
        if not isinstance(self.read_roots, dict) or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", k) for k in self.read_roots):
            fail("read_roots need logical lowercase names", "INVALID_ARGUMENT")
        for root in self.read_roots.values():
            if not isinstance(root, str) or not Path(root).is_absolute() or not Path(root).is_dir():
                fail("read roots must be existing absolute directories", "INVALID_ARGUMENT")
        self.max_input = config.get("max_input_bytes", 1024 * 1024 * 1024)
        if type(self.max_input) is not int or not 1 <= self.max_input <= 16 * 1024**3:
            fail("max_input_bytes must be 1..16GiB", "INVALID_ARGUMENT")
        raw_root = config.get("job_root")
        self.root = SimRoot.at(Path(raw_root), self.env) if raw_root else SimRoot(None, "configure private job_root")
        # Never allow the new-job root to mutate an input tree, including through a parent symlink.
        if self.root.path:
            for root in self.read_roots.values():
                if is_inside(self.root.path.resolve(), Path(root).resolve()):
                    fail("job_root must be outside all read_roots", "INVALID_ARGUMENT")
        logical = {f"<READ:{k}>": str(Path(v).resolve()) for k, v in self.read_roots.items()}
        logical.update({f"<{k.upper()}_EXE>": str(v) for k, v in self.executables.items() if v})
        self.jobs = JobsService(self.root, env=self.env, logical_roots=logical)

    def executable(self, tool: str) -> Path:
        if tool not in TOOLS:
            fail("unknown native tool")
        raw = self.executables.get(tool)
        if not raw or not isinstance(raw, str):
            fail(f"{tool}: executable not configured", "APP_UNAVAILABLE")
        p = Path(raw)
        if not p.is_absolute() or not p.is_file() or (os.name == "nt" and p.suffix.lower() != ".exe"):
            fail(f"{tool}: configured location is not an executable file (source directories do not qualify)", "APP_UNAVAILABLE")
        if os.name != "nt" and not os.access(p, os.X_OK):
            fail(f"{tool}: configured file is not executable", "APP_UNAVAILABLE")
        return p.resolve()

    def status(self) -> dict:
        out = {}
        for tool in TOOLS:
            item = {"configured": bool(self.executables.get(tool)), "executable_available": False,
                    "native_execution": "NOT_RUN"}
            try:
                p = self.executable(tool)
                item.update(executable_available=True, executable_sha256=sha256_file(p))
            except ToolFailure as exc:
                item["reason"] = exc.message
            out[tool] = item
        # Status never starts programs. Actual prior execution is separately identified and hash-bound.
        if self.root.path and (self.root.path / "jobs").is_dir():
            for p in sorted((self.root.path / "jobs").glob("PY-*/receipt.json"), reverse=True):
                receipt = read_json(p, default={}) or {}
                app = receipt.get("app", {})
                tool = app.get("native_tool")
                if receipt.get("meta", {}).get("owner") != OWNER or tool not in out:
                    continue
                if "last_execution" in out[tool]:
                    continue
                out[tool]["last_execution"] = {"job_id": receipt.get("job_id"), "kind": receipt.get("kind"),
                    "status": receipt.get("status"), "exit_code": receipt.get("exit_code"),
                    "executable_sha256": app.get("executable_sha256")}
                if app.get("executable_sha256") == out[tool].get("executable_sha256"):
                    out[tool]["native_execution"] = receipt.get("status")
        return {"schema": "vkm.engineering_capabilities/1", "tools": out,
                "job_root": self.root.describe(), "read_roots": sorted(self.read_roots),
                "config_sha256": self.config_sha, "profiles": {
                    "gmsh": ["probe", "mesh_box", "mesh_cad", "mesh_closed_stl_single_volume", "inspect_msh"],
                    "paraview": ["probe", "inspect", "render", "slice", "save_pvsm"],
                    "cloudcompare": ["inspect", "c2c", "c2m"]},
                "coordinate_policy": "No ICP/global shift/registration/scaling. Data coordinates retained; frame/unit supplied explicitly.",
                "scientific_admission": "NOT_GRANTED"}

    def source(self, ref: str, extensions: set[str]) -> Path:
        if not isinstance(ref, str) or ":" not in ref:
            fail("input_ref must be logical_root:relative/path")
        key, rel = ref.split(":", 1)
        if key not in self.read_roots:
            fail("unknown read root", "PATH_OUTSIDE_ROOT")
        p = resolve_inside(Path(self.read_roots[key]), rel)
        if not p.is_file() or p.suffix.lower() not in extensions:
            fail("input must be an existing file of a supported data format")
        if p.stat().st_size > self.max_input:
            fail("input exceeds configured max_input_bytes")
        return p

    @staticmethod
    def metadata(frame: str, unit: str) -> dict:
        if not isinstance(frame, str) or not 1 <= len(frame) <= 200 or any(ord(c) < 32 for c in frame):
            fail("frame must be an explicit nonempty identifier (UNKNOWN permitted)")
        if unit not in {"m", "mm", "cm", "unknown"}:
            fail("unit must be m/mm/cm/unknown; no conversion is performed")
        return {"frame": frame, "unit": unit, "coordinate_transform": "NONE", "scientific_admission": "NOT_GRANTED"}

    def submit(self, tool: str, operation: str, *, sources: list[tuple[Path, str]] = (), generated: dict[str, str] | None = None,
               args: list[str], checks: list[dict] | None = None, timeout_s: int = 120,
               meta: dict | None = None) -> dict:
        if type(timeout_s) is not int or not 1 <= timeout_s <= 3600:
            fail("timeout_s must be 1..3600")
        exe = self.executable(tool)
        self.root.require()
        draft = self.jobs.draft("PY")
        try:
            for p, name in sources:
                draft.copy_input(p, name)
            for name, text in (generated or {}).items():
                draft.write_input(name, text)
            spec = draft.spec(kind=f"engineering_{tool}_{operation}", pool="engineering", argv=[str(exe), *args],
                timeout_s=timeout_s, tree_grace_s=3, checks=[{"name": "native_exit", "kind": "exit_code", "expected": 0}, *(checks or [])],
                app_info={"native_tool": tool, "executable_sha256": sha256_file(exe)},
                git=git_info(repo_root(self.env), ["src/vkm_engineering", "pyproject.toml"]),
                outputs=["out/**"], meta={"owner": OWNER, "operation": operation, "config_sha256": self.config_sha, **(meta or {})},
                model_choices=[{"status": "MODEL_CHOICE", "description": "Fixed numerical representation/visualization only; no scientific admission."}])
            return self.jobs.submit(draft, spec)
        except Exception:
            # Preserve any submitted job; discard only a never-submitted draft.
            if not (draft.dir / "job.json").exists():
                draft.discard()
            raise

    def probe(self, tool: str) -> dict:
        if tool == "gmsh":
            return self.submit(tool, "probe", args=["-version"])
        if tool == "paraview":
            return self.paraview("probe")
        fail("CloudCompare availability is file-only until an explicit inspect/distance job executes")

    def gmsh_mesh(self, *, input_ref: str | None = None, size: float = 1.0, order: int = 1,
                  box_lengths: list[float] | None = None, frame: str = "UNKNOWN", unit: str = "unknown",
                  timeout_s: int = 120) -> dict:
        meta = self.metadata(frame, unit)
        size = positive(size, "size")
        if type(order) is not int or order not in {1, 2}:
            fail("order must be 1 or 2")
        if (input_ref is None) == (box_lengths is None):
            fail("supply exactly one of input_ref or box_lengths")
        sources = []
        if box_lengths is not None:
            lens = vector(box_lengths, "box_lengths")
            if min(lens) <= 0:
                fail("all box lengths must be positive")
            body = 'SetFactory("OpenCASCADE");\nBox(1)={0,0,0,' + ','.join(repr(v) for v in lens) + '};\n'
            operation = "mesh_box"
            meta["box_lengths"] = lens
        else:
            p = self.source(input_ref, CAD_EXT | {".stl"})
            name = "geometry" + p.suffix.lower()
            sources = [(p, name)]
            if p.suffix.lower() == ".stl":
                # Official Gmsh t13-style discrete reparametrization. This profile
                # requires ONE connected closed shell, not an implicit multi-body merge.
                body = f'Merge "{name}";\nClassifySurfaces{{40*Pi/180,1,1,Pi}};\nCreateGeometry;\nSurface Loop(1)={{Surface{{:}}}};\nVolume(1)={{1}};\n'
                operation = "mesh_closed_stl_single_volume"
                meta["precondition"] = "ONE_CONNECTED_CLOSED_SHELL; source geometry unreviewed; native checks required"
            else:
                body = f'SetFactory("OpenCASCADE");\nMerge "{name}";\n'
                operation = "mesh_cad"
        body += f'Mesh.MeshSizeMin={size!r};\nMesh.MeshSizeMax={size!r};\n'
        meta.update(mesh_size=size, element_order=order)
        return self.submit("gmsh", operation, sources=sources, generated={"profile.geo": body},
            args=["../in/profile.geo", "-3", "-order", str(order), "-format", "msh4", "-o", "../out/mesh.msh", "-v", "4"],
            checks=[{"name": "mesh_created", "kind": "file_exists", "path": "out/mesh.msh"},
                    {"name": "no_native_error", "kind": "text_absent", "path": "logs/stderr.log", "expected": "Error"},
                    {"name": "no_native_error_stdout", "kind": "text_absent", "path": "logs/stdout.log", "expected": "Error"}],
            timeout_s=timeout_s, meta=meta)

    def gmsh_inspect(self, input_ref: str, timeout_s: int = 120) -> dict:
        p = self.source(input_ref, {".msh"})
        return self.submit("gmsh", "inspect_msh", sources=[(p, "mesh.msh")], args=["../in/mesh.msh", "-check", "-v", "4"],
            checks=[{"name": "no_native_error", "kind": "text_absent", "path": "logs/stderr.log", "expected": "Error"},
                    {"name": "no_native_error_stdout", "kind": "text_absent", "path": "logs/stdout.log", "expected": "Error"}], timeout_s=timeout_s)

    def paraview(self, operation: str, input_ref: str | None = None, *, frame: str = "UNKNOWN", unit: str = "unknown",
                 origin: list[float] | None = None, normal: list[float] | None = None,
                 width: int = 1024, height: int = 768, timeout_s: int = 120) -> dict:
        if operation not in {"probe", "inspect", "render", "slice"}:
            fail("unsupported ParaView operation")
        if type(width) is not int or type(height) is not int or not 32 <= width <= 4096 or not 32 <= height <= 4096:
            fail("image dimensions must be 32..4096")
        meta = self.metadata(frame, unit)
        payload = {"operation": operation, "frame": frame, "unit": unit, "width": width, "height": height}
        sources = []
        if operation != "probe":
            p = self.source(input_ref, PV_EXT)
            name = "data" + p.suffix.lower()
            sources = [(p, name)]
            payload["input"] = name
        if operation == "slice":
            payload["origin"] = vector(origin, "origin")
            payload["normal"] = vector(normal, "normal")
            if max(abs(v) for v in payload["normal"]) == 0:
                fail("slice normal must be nonzero")
        generated = {"pv_worker.py": Path(__file__).with_name("pv_worker.py").read_text(encoding="utf-8"),
                     "payload.json": json.dumps(payload, allow_nan=False)}
        checks = [{"name": "native_report", "kind": "file_exists", "path": "out/report.json"}]
        if operation in {"render", "slice"}:
            checks += [{"name": "image", "kind": "file_exists", "path": "out/view.png"},
                       {"name": "state", "kind": "file_exists", "path": "out/view.pvsm"}]
        if operation == "slice":
            checks.append({"name": "slice_data", "kind": "file_exists", "path": "out/slice.vtp"})
        return self.submit("paraview", operation, sources=sources, generated=generated,
            args=["../in/pv_worker.py", "../in/payload.json"], checks=checks, timeout_s=timeout_s, meta=meta)

    def cloudcompare(self, operation: str, input_ref: str, reference_ref: str | None = None, *,
                     entity_type: str = "cloud", frame: str = "UNKNOWN", unit: str = "unknown", timeout_s: int = 120) -> dict:
        if operation not in {"inspect", "c2c", "c2m"} or entity_type not in {"cloud", "mesh"}:
            fail("unsupported CloudCompare profile")
        meta = self.metadata(frame, unit)
        if operation != "inspect" and (frame == "UNKNOWN" or unit == "unknown"):
            fail("distance comparisons require an explicit common frame and unit; no registration is performed")
        p = self.source(input_ref, MESH_EXT if entity_type == "mesh" else CLOUD_EXT)
        a = "compared" + p.suffix.lower()
        sources = [(p, a)]
        args = ["-SILENT", "-AUTO_SAVE", "OFF", "-C_EXPORT_FMT", "BIN", "-M_EXPORT_FMT", "BIN",
                "-O", "-GLOBAL_SHIFT", "0", "0", "0", "../in/" + a]
        if operation != "inspect":
            if entity_type != "cloud":
                fail("compared entity must be a cloud for distances")
            p = self.source(reference_ref, MESH_EXT if operation == "c2m" else CLOUD_EXT)
            b = "reference" + p.suffix.lower()
            sources.append((p, b))
            args += ["-O", "-GLOBAL_SHIFT", "0", "0", "0", "../in/" + b, "-C2M_DIST" if operation == "c2m" else "-C2C_DIST"]
            if operation == "c2m":
                args += ["-UNSIGNED"]
            if operation == "c2c":
                args += ["-POP_CLOUDS"]  # only the compared cloud is saved
        args += ["-SAVE_MESHES" if entity_type == "mesh" else "-SAVE_CLOUDS", "FILE", "../out/result.bin"]
        checks = [{"name": "native_saved_entity", "kind": "file_exists", "path": "out/result.bin"}]
        if entity_type == "cloud":
            # Native scalar export makes distance results reviewable without
            # trusting an exit code or decoding a proprietary binary in Python.
            args += ["-C_EXPORT_FMT", "ASC", "-PREC", "17", "-SEP", "COMMA", "-ADD_HEADER",
                     "-SAVE_CLOUDS", "FILE", "../out/scalars.csv"]
            checks.append({"name": "native_scalar_export", "kind": "file_exists", "path": "out/scalars.csv"})
        return self.submit("cloudcompare", operation, sources=sources, args=args,
            checks=checks,
            timeout_s=timeout_s, meta={**meta, "distance_profile": "unsignedC2C_or_explicitUNSIGNED_C2M; noICP/noMAX_DIST/noautomaticshift",
                                     "precision_note": "CloudCompare internal coordinate precision is native; no accuracy/registration claim."})

    def owned(self, job_id: str) -> JobsService:
        p = self.jobs.job_dir(job_id)
        spec = read_json(p / "job.json")
        if spec.get("meta", {}).get("owner") != OWNER:
            fail("job is not owned by vkm-engineering", "PATH_OUTSIDE_ROOT")
        return self.jobs
