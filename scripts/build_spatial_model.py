#!/usr/bin/env python3
"""Build the private, reproducible VKM spatial package with the installed GIS Python.

Pin selected inputs once: --pin --config work/<run>/model.json.
Build to a new directory: --config work/<run>/model.json --out work/<run>/package.
VKM_RESOURCES_ROOT must name the existing PRIVATE checkout. No source is mutated.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/"src"), str(ROOT)]
from vkm_world.geometry_build.pipeline import (pinned_files, verify_pins, run_step,
                                               write_json, inventory)


def git_head(root):
    result = subprocess.run([os.environ.get("VKM_GIT_EXECUTABLE","git"),"rev-parse","HEAD"], cwd=root,
                            text=True, encoding="utf-8", capture_output=True, check=True)
    return result.stdout.strip()


def runtime_versions():
    from osgeo import gdal
    from qgis.core import Qgis
    import numpy, shapely, matplotlib, scipy, PIL
    return {"python":sys.version.split()[0], "qgis":Qgis.QGIS_VERSION,
            "gdal":gdal.VersionInfo("RELEASE_NAME"), "numpy":numpy.__version__,
            "shapely":shapely.__version__, "matplotlib":matplotlib.__version__,
            "scipy":scipy.__version__, "pillow":PIL.__version__}


def paraview_runtime():
    executable = os.environ.get("VKM_PARAVIEW_PYTHON")
    if not executable or not Path(executable).is_file():
        raise ValueError("set VKM_PARAVIEW_PYTHON to the installed pvpython")
    result = subprocess.run([executable,"--version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=True, timeout=30)
    from vkm_world.core.io import sha256_file
    return {"version":result.stdout.strip() or result.stderr.strip(),
            "executable_sha256":sha256_file(Path(executable))}


def integration_paths():
    return ["scripts/build_spatial_model.py", "src/vkm_world/geometry_build/scene.py",
            "src/vkm_world/geometry_build/pipeline.py",
            "src/vkm_world/geometry_build/paraview_export.py",
            "src/vkm_world/geometry_build/paraview_worker.py"]


def validate_recipe_pins(res, config, current):
    """Reject added/removed inputs as well as byte drift before writing output."""
    if set(config["stages"]) != set(current):
        raise ValueError("configuration stage set differs from current recipe")
    for name,stage in config["stages"].items():
        if set(current[name]) != {p["path"] for p in stage["private_inputs"]}:
            raise ValueError(f"input file set changed: {name}; create a new pinned configuration")
        verify_pins(res, stage["private_inputs"])
        if set(code_paths(name)) != {p["path"] for p in stage["code"]}:
            raise ValueError(f"code file set changed: {name}")
        verify_pins(ROOT, stage["code"])
    if set(integration_paths()) != {p["path"] for p in config["integration_code"]}:
        raise ValueError("integration code file set changed")
    verify_pins(ROOT,config["integration_code"])


def code_paths(name):
    common = ["src/vkm_world/geometry_build/pipeline.py", "src/vkm_world/core/io.py"]
    own = {"terrain":["scripts/build_dma_surface_package.py", "src/vkm_datasets/gis.py",
                      "src/vkm_datasets/manifest.py", "src/vkm_datasets/__init__.py",
                      "src/vkm_datasets/policy.py", "src/vkm_corpus/contracts/access_vocab.py"],
           "geology":["src/vkm_world/geometry_build/geology.py", "src/vkm_world/geometry_build/source_section.py"],
           "mining":["src/vkm_world/geometry_build/mining.py", "scripts/check_dma_formulas.py"],
           "regional":["src/vkm_world/geometry_build/scene.py", "scripts/build_dma_surface_package.py"]}
    return common + own[name]


def terrain_inputs(res):
    from scripts.build_dma_surface_package import DSET, DEMD, OSMD
    result = [(DSET/"dataset_manifest.json").as_posix()]
    for folder in [DSET/"original", DSET/"conversions", OSMD]:
        result.extend(p.relative_to(res).as_posix() for p in sorted((res/folder).rglob("*")) if p.is_file())
    intake = json.loads((res/DEMD/"INTAKE_RECEIPT.json").read_text(encoding="utf-8"))
    result += [(DEMD/"INTAKE_RECEIPT.json").as_posix()]
    result.extend((DEMD/f["path"]).as_posix() for f in intake["files"])
    return sorted(set(result))


def pin_configuration(res: Path, path: Path):
    if path.exists():
        raise FileExistsError("choose a new config path; pinned inputs are immutable")
    selected = selected_input_paths(res)
    stages = {name:{"private_inputs":pinned_files(res, paths), "code":pinned_files(ROOT, code_paths(name))}
              for name,paths in selected.items()}
    config = {"schema":"vkm.spatial_build/1", "baseline_public_commit":git_head(ROOT),
              "baseline_private_commit":git_head(res), "mine_scope":["SKRU1","SKRU2","SKRU3"],
              "authorization":"D-20, 2026-10-16; this Codex session and its built-in subagents",
              "output_policy":"PRIVATE_OR_IGNORED_WORK_ONLY", "stages":stages,
              "integration_code":pinned_files(ROOT,integration_paths()),
              "parameters":{"terrain":"existing_30m_2000m_buffer", "regional_pixel_m":30,
                            "regional_buffer_m":2000, "vertical_exaggeration":1,
                            "planimetry_interpretation":"C4_SYMBOL_LINK_CHECKED_V2",
                            "absolute_geology_vertical_tie":None},
              "runtime":runtime_versions(),
              "paraview_runtime":paraview_runtime(),
              "scientific_admission":"NOT_CHECKED", "solvers":"NOT_RUN", "ml":"NOT_RUN"}
    write_json(path, config)
    return {"configuration":path.relative_to(ROOT).as_posix(),
            "stages":{n:len(s["private_inputs"]) for n,s in stages.items()}}


def selected_input_paths(res):
    from vkm_world.geometry_build import mining, geology
    return {"terrain":terrain_inputs(res), "mining":mining.input_paths(res),
            "geology":geology.input_paths(res)}


def build(res, config, out, cache):
    from scripts.build_dma_surface_package import build as terrain_build
    from vkm_world.geometry_build.geology import build_geology_package
    from vkm_world.geometry_build.mining import build_mining_package
    from vkm_world.geometry_build.scene import build_regional_surface, build_qgis_scenes, ensure_qgis_application
    from vkm_world.geometry_build.paraview_export import build_paraview_package
    if config.get("schema") != "vkm.spatial_build/1":
        raise ValueError("unsupported configuration schema")
    if config.get("mine_scope") != ["SKRU1","SKRU2","SKRU3"]:
        raise ValueError("this recipe builds the supported source coverage of SKRU1/2/3")
    if config["runtime"] != runtime_versions():
        raise ValueError("GIS runtime changed; create a new pinned configuration")
    if config["paraview_runtime"] != paraview_runtime():
        raise ValueError("ParaView runtime changed; create a new pinned configuration")
    expected_parameters = {"terrain":"existing_30m_2000m_buffer", "regional_pixel_m":30,
                           "regional_buffer_m":2000, "vertical_exaggeration":1,
                           "planimetry_interpretation":"C4_SYMBOL_LINK_CHECKED_V2",
                           "absolute_geology_vertical_tie":None}
    params = config["parameters"]
    if (set(params) != set(expected_parameters) or any(
            params[k] != v for k,v in expected_parameters.items()
            if k not in {"regional_pixel_m","regional_buffer_m"})):
        raise ValueError("unsupported parameters; implement and document changed methods before repinning")
    # Validate every pin before creating a partial output package.
    current = selected_input_paths(res)
    validate_recipe_pins(res,config,current)
    ensure_qgis_application()
    out.mkdir(parents=True, exist_ok=False)
    steps = {}
    builders = {"terrain":terrain_build,"mining":build_mining_package,"geology":build_geology_package}
    for name in ["terrain","mining","geology"]:
        specification = config["stages"][name]

        def checked_builder(dest, n=name, spec=specification):
            result = builders[n](res,dest)
            if n == "terrain" and result.get("qgis_verification",{}).get("status") != "PASS":
                raise RuntimeError("terrain project failed independent reading; failed cache stage preserved")
            verify_pins(res,spec["private_inputs"])
            verify_pins(ROOT,spec["code"])
            return result

        steps[name] = run_step(name, cache=cache, destination=out/name,
                               inputs={"private:"+p["path"]:p["sha256"] for p in specification["private_inputs"]},
                               code={p["path"]:p["sha256"] for p in specification["code"]},
                               parameters={"method":name+"_v1", "runtime":config["runtime"]}, dependencies={},
                               builder=checked_builder)
        print(json.dumps({"step":name,"cache_hit":steps[name]["cache_hit"]}), flush=True)
    # Regional surface depends on plan extent and the pinned DEM, not on the geological picks.
    code = pinned_files(ROOT,code_paths("regional"))
    steps["regional"] = run_step("regional", cache=cache, destination=out/"regional",
                                 inputs={"private:"+p["path"]:p["sha256"] for p in config["stages"]["terrain"]["private_inputs"]
                                         if "copernicus_dem" in p["path"]},
                                 code={p["path"]:p["sha256"] for p in code},
                                 parameters={"pixel_m":params["regional_pixel_m"],"buffer_m":params["regional_buffer_m"],
                                             "runtime":config["runtime"]},
                                 dependencies={n:steps[n]["key"] for n in ["terrain","mining"]},
                                 builder=lambda dest:build_regional_surface(res,out/"mining",out/"terrain",dest,
                                     pixel_m=params["regional_pixel_m"],buffer_m=params["regional_buffer_m"]))
    scenes = build_qgis_scenes(out)
    def export_paraview(dest):
        receipt = build_paraview_package(out,dest)
        if receipt.get("status") != "PASS":
            raise RuntimeError("ParaView export failed; inspect preserved stage receipt/logs")
        return receipt

    steps["paraview"] = run_step("paraview",cache=cache,destination=out/"paraview",
        inputs={}, parameters={"runtime":config["paraview_runtime"],"vertical_scale":1},
        code={p["path"]:p["sha256"] for p in config["integration_code"]
              if "paraview_" in p["path"]},
        dependencies={n:steps[n]["key"] for n in ["terrain","mining","geology","regional"]},
        builder=export_paraview)
    print(json.dumps({"step":"paraview","cache_hit":steps["paraview"]["cache_hit"]}),flush=True)
    for stage in config["stages"].values():
        verify_pins(res,stage["private_inputs"])
        verify_pins(ROOT,stage["code"])
    verify_pins(ROOT,config["integration_code"])
    write_json(out/"configuration.json", config)
    manifest = {"schema":"vkm.spatial_package/1", "configuration_sha256":hashlib.sha256(
                    json.dumps(config,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                "step_keys":{n:s["key"] for n,s in steps.items()}, "scenes":scenes,
                "code":{p["path"]:p["sha256"] for p in pinned_files(ROOT,["scripts/build_spatial_model.py",
                             "src/vkm_world/geometry_build/scene.py"])},
                "epistemic_status":"DERIVATION", "scientific_admission":"NOT_CHECKED",
                "solvers":"NOT_RUN", "ml":"NOT_RUN", "production":"UNCHANGED"}
    write_json(out/"manifest.json",manifest)
    write_json(out/"output_inventory.json",inventory(out))
    # Cache-hit events are operational evidence outside the reproducible package.
    write_json(out.parent/(out.name+"_execution.json"), {"steps":steps})
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pin",action="store_true")
    ap.add_argument("--config",type=Path,required=True)
    ap.add_argument("--out",type=Path)
    ap.add_argument("--cache",type=Path,default=Path("work/spatial_model_cache"))
    args = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        ap.error("set VKM_RESOURCES_ROOT to the existing PRIVATE checkout")
    res = Path(res).resolve()
    config = (ROOT/args.config).resolve()
    if not config.is_relative_to((ROOT/"work").resolve()) and not config.is_relative_to(res):
        ap.error("private configuration must remain under work/ or PRIVATE")
    if args.pin:
        return pin_configuration(res,config)
    if args.out is None:
        ap.error("--out is required for a build")
    out,cache = (ROOT/args.out).resolve(), (ROOT/args.cache).resolve()
    for path in [out,cache]:
        if not path.is_relative_to((ROOT/"work").resolve()) and not path.is_relative_to(res):
            ap.error("geometry and cache must remain under work/ or PRIVATE")
    return build(res,json.loads(config.read_text(encoding="utf-8")),out,cache)


if __name__ == "__main__":
    status = 1
    try:
        print(json.dumps(main(),ensure_ascii=False,indent=2),flush=True)
        status = 0
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        # The installed Qt/GDAL runtime can crash at interpreter shutdown after provider teardown.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(status)
