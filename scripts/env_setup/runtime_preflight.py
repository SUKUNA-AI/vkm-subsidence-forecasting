"""Version/import probes for the selected local VKM runtime roles; no workloads.

Machine paths belong exclusively in an explicit, untracked JSON configuration.
The configuration cannot supply commands, Python code, or module names.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys
import time

MODULES = {
    "windows_mcp": ("mcp", "vkm_cad.mcp_server", "vkm_drawio.mcp_server", "vkm_qgis.mcp_server"),
    "windows_data": ("numpy", "cv2", "ezdxf", "duckdb"),
    "windows_xany": ("anylabeling", "onnxruntime", "PyQt6.QtCore"),
    "wsl_sam": ("torch", "torchvision", "sam2"),
    "wsl_data": ("numpy", "geopandas", "pykrige", "criticalmaas.ta1_geopackage"),
    "wsl_gempy": ("gempy", "gempy_engine"),
    "wsl_uiuc": ("torch", "torchvision", "pytorch_lightning", "torchmetrics", "adamp"),
    "qgis_worker": ("qgis.core", "osgeo.gdal", "osgeo.osr"),
    "paraview": ("paraview", "vtkmodules.vtkCommonCore"),
}
ALLOWED_ROLE_KEYS = {"interpreter", "transport", "module_paths", "dll_dirs", "environment_paths",
                     "expected_python", "distribution", "user"}
ALLOWED_ENV_PATHS = {"PYTHONHOME", "QGIS_PREFIX_PATH", "OSGEO4W_ROOT", "QT_PLUGIN_PATH"}
PREFIX = "VKM_PREFLIGHT_JSON="
DISTRIBUTIONS = {"sam2": "SAM-2", "criticalmaas.ta1_geopackage": "criticalmaas-ta1-geopackage",
                 "anylabeling": "X-AnyLabeling"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(role, settings):
    handles = []
    for directory in settings.get("dll_dirs", []):
        if hasattr(os, "add_dll_directory"):
            handles.append(os.add_dll_directory(directory))
    for directory in reversed(settings.get("module_paths", [])):
        sys.path.insert(0, directory)
    result = {"role": role, "executable": sys.executable, "python": platform.python_version(),
              "platform": platform.platform(), "imports": {}, "status": "PASS"}
    result["configured_interpreter_matches"] = os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(os.path.abspath(settings["interpreter"]))
    if not result["configured_interpreter_matches"]:
        result["status"] = "FAIL"
    wanted = settings.get("expected_python")
    if wanted and not platform.python_version().startswith(wanted + "."):
        result.update(status="FAIL", python_version_error=f"Expected {wanted}.x")
    for name in MODULES[role]:
        try:
            module = importlib.import_module(name)
            version = getattr(module, "__version__", None)
            if name == "qgis.core":
                version = module.Qgis.QGIS_VERSION
            elif name == "osgeo.gdal":
                version = module.VersionInfo("--version")
            elif name == "osgeo.osr":
                version = ".".join(str(fn()) for fn in (module.GetPROJVersionMajor,
                                    module.GetPROJVersionMinor, module.GetPROJVersionMicro))
            elif name == "vtkmodules.vtkCommonCore":
                version = module.vtkVersion.GetVTKVersion()
            elif name == "PyQt6.QtCore":
                version = module.QT_VERSION_STR
            if not version:
                try:
                    version = importlib.metadata.version(DISTRIBUTIONS.get(name, name.split(".")[0].replace("_", "-")))
                except importlib.metadata.PackageNotFoundError:
                    version = "NOT_EXPOSED_BY_MODULE"
            result["imports"][name] = {"status": "PASS", "version": str(version),
                                       "path": getattr(module, "__file__", None)}
        except Exception as exc:
            result["imports"][name] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
            result["status"] = "FAIL"
    print(PREFIX + json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] == "PASS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only", nargs="+", choices=tuple(MODULES))
    parser.add_argument("--_probe", choices=tuple(MODULES), help=argparse.SUPPRESS)
    parser.add_argument("--_settings", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._probe:
        settings = json.loads(base64.urlsafe_b64decode(args._settings).decode("utf-8"))
        return probe(args._probe, settings)
    if args.config is None or args.output is None:
        parser.error("--config and --output are required")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if set(config) - {"schema", "project_root", "wsl_project_root", "wsl_executable", "roles"}:
        parser.error("Unknown top-level configuration keys")
    if config.get("schema") != "vkm-runtime-paths/1":
        parser.error("Unsupported runtime paths schema")
    roles = config["roles"]
    if set(roles) - set(MODULES):
        parser.error("Unknown runtime role")
    selected = args.only or list(MODULES)
    if any(role not in roles for role in selected):
        parser.error("Every selected role needs explicit local configuration")
    for role in selected:
        settings = roles[role]
        if set(settings) - ALLOWED_ROLE_KEYS:
            parser.error(f"Unknown settings for {role}")
        if set(settings.get("environment_paths", {})) - ALLOWED_ENV_PATHS:
            parser.error(f"Unsupported environment setting for {role}")
        expected_transport = "wsl" if role.startswith("wsl_") else "native"
        if settings.get("transport") != expected_transport:
            parser.error(f"Incorrect transport for selected role {role}")
        absolute = PurePosixPath(settings["interpreter"]).is_absolute() if expected_transport == "wsl" else Path(settings["interpreter"]).is_absolute()
        if not absolute:
            parser.error(f"Explicit absolute interpreter path required for {role}")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Use a fresh empty output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "vkm-runtime-preflight/1", "status": "PASS", "scope": "versions/executable/import only",
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "config_sha256": digest(args.config),
              "script_sha256": digest(Path(__file__)), "roles": {},
              "excluded": ["models", "inference", "GPU workloads", "network/API probes", "installation", "MCP startup"]}
    for role in selected:
        settings = roles[role]
        encoded = base64.urlsafe_b64encode(json.dumps(settings).encode("utf-8")).decode("ascii")
        script = str(Path(__file__).resolve())
        command = [settings["interpreter"]]
        if role == "paraview":
            command.append("--force-offscreen-rendering")
        if settings["transport"] == "wsl":
            relative = Path(__file__).resolve().relative_to(Path(config["project_root"]).resolve())
            script = config["wsl_project_root"].rstrip("/") + "/" + relative.as_posix()
            command = [config["wsl_executable"], "-d", settings["distribution"], "-u", settings["user"], "--",
                       "env", "OMP_NUM_THREADS=2", "OPENBLAS_NUM_THREADS=2", "MKL_NUM_THREADS=2",
                       "PYTHONDONTWRITEBYTECODE=1", settings["interpreter"]]
        command += [script, "--_probe", role, "--_settings", encoded]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="2",
                   OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2", QT_QPA_PLATFORM="offscreen")
        env.update(settings.get("environment_paths", {}))
        if settings.get("dll_dirs"):
            env["PATH"] = os.pathsep.join(settings["dll_dirs"]) + os.pathsep + env.get("PATH", "")
        if settings.get("module_paths"):
            env["PYTHONPATH"] = os.pathsep.join(settings["module_paths"])
        started = time.monotonic()
        try:
            completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace", env=env, timeout=60,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            lines = [line[len(PREFIX):] for line in completed.stdout.splitlines() if line.startswith(PREFIX)]
            outcome = json.loads(lines[-1]) if lines else {"status": "FAIL", "error": "Probe returned no structured result"}
            outcome.update(exit_code=completed.returncode, elapsed_seconds=round(time.monotonic() - started, 3))
            if completed.returncode != 0:
                outcome["status"] = "FAIL"
            (args.output / f"{role}.stdout.log").write_text(completed.stdout, encoding="utf-8")
            (args.output / f"{role}.stderr.log").write_text(completed.stderr, encoding="utf-8")
        except Exception as exc:
            outcome = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}", "exit_code": None}
        report["roles"][role] = outcome
        if outcome["status"] != "PASS":
            report["status"] = "FAIL"
        (args.output / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    (args.output / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "roles": {r: v["status"] for r, v in report["roles"].items()}}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
