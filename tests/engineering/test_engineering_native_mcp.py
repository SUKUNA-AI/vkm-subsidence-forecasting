"""Profile, root, job-ownership and actual MCP SDK contracts (no installed GUI needed)."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client

from vkm_engineering.mcp_server import build_server
from vkm_engineering.service import EngineeringService, OWNER
from vkm_jobs.errors import ToolFailure
from vkm_jobs.spec import atomic_write_json


@pytest.fixture
def service(tmp_path):
    source = tmp_path / "read"
    source.mkdir()
    (source / "cube.stl").write_text("solid fixture\nendsolid fixture\n")
    (source / "cube.vtu").write_text("<VTKFile/>\n")
    (source / "mesh.msh").write_text("$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    config = {"job_root": str(tmp_path / "newjobs"), "read_roots": {"fixture": str(source)},
              "executables": {"gmsh": sys.executable, "paraview": sys.executable, "cloudcompare": None}}
    svc = EngineeringService(config=config)
    def spawn(argv, cwd, env, log):
        return 1, "TEST_NOT_EXECUTED"
    svc.jobs._spawner = spawn
    return svc


def test_status_is_presence_not_execution(service):
    s = service.status()
    assert s["tools"]["gmsh"]["executable_available"]
    assert s["tools"]["gmsh"]["native_execution"] == "NOT_RUN"
    assert not s["tools"]["cloudcompare"]["executable_available"]
    assert not service.root.path.exists()


@pytest.mark.parametrize("ref", ["fixture:../escape.vtu", "fixture:C:/a.vtu", "fixture://server/a.vtu", "fixture:a:stream.vtu", "missing:a.vtu"])
def test_input_roots_fail_closed(service, ref):
    with pytest.raises(ToolFailure):
        service.source(ref, {".vtu"})


def test_symlink_input_denied(service, tmp_path):
    target = tmp_path / "outside.vtu"
    target.write_text("outside")
    link = Path(service.read_roots["fixture"]) / "link.vtu"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit symlinks")
    with pytest.raises(ToolFailure, match="link"):
        service.source("fixture:link.vtu", {".vtu"})


def test_no_user_code_or_argv(service):
    with pytest.raises(ToolFailure):
        service.gmsh_mesh(input_ref="fixture:arbitrary.geo")
    with pytest.raises(ToolFailure):
        service.paraview("python", "fixture:cube.vtu")
    with pytest.raises(ToolFailure):
        service.cloudcompare("icp", "fixture:cube.stl")
    with pytest.raises(ToolFailure):
        service.cloudcompare("c2c", "fixture:cube.stl", "fixture:cube.stl")


@pytest.mark.parametrize("args", [{"box_lengths": [1, 0, 1]}, {"box_lengths": [1, 1, 1], "size": float('nan')},
    {"box_lengths": [1, 1, 1], "order": 3}, {"box_lengths": [1, 1, 1], "timeout_s": 0},
    {"box_lengths": [1, 1, 1], "timeout_s": True}])
def test_numeric_validation_before_submit(service, args):
    with pytest.raises(ToolFailure):
        service.gmsh_mesh(**args)
    assert not service.root.path.exists()


def test_copied_inputs_hashes_and_fixed_profiles(service):
    job = service.gmsh_mesh(input_ref="fixture:cube.stl", size=0.25, order=2, frame="LOCAL", unit="m")
    directory = service.jobs.job_dir(job["job_id"])
    spec = json.loads((directory / "job.json").read_text())
    assert spec["meta"]["owner"] == OWNER
    assert spec["pool"] == "engineering"
    assert spec["argv"][1:] == ["../in/profile.geo", "-3", "-order", "2", "-format", "msh4", "-o", "../out/mesh.msh", "-v", "4"]
    assert len(spec["inputs"]) == 2 and all(len(x["sha256"]) == 64 for x in spec["inputs"])
    assert (directory / "in/geometry.stl").read_bytes() == (Path(service.read_roots['fixture']) / 'cube.stl').read_bytes()
    assert spec["meta"]["coordinate_transform"] == "NONE"
    assert 'SystemCall' not in (directory / 'in/profile.geo').read_text()


def test_pv_payload_not_python_interpolation(service):
    frame = "quoted ' frame; import os"
    job = service.paraview("slice", "fixture:cube.vtu", frame=frame, unit="m", origin=[.5,.5,.5], normal=[0,0,1])
    directory = service.jobs.job_dir(job["job_id"])
    payload = json.loads((directory / "in/payload.json").read_text())
    assert payload["frame"] == frame
    assert frame not in (directory / "in/pv_worker.py").read_text()
    with pytest.raises(ToolFailure):
        service.paraview("slice", "fixture:cube.vtu", origin=[0,0,0], normal=[0,0,0])


def test_only_own_jobs_can_be_read(service):
    job = service.gmsh_inspect("fixture:mesh.msh")
    path = service.jobs.job_dir(job["job_id"])
    spec = json.loads((path / "job.json").read_text())
    spec["meta"]["owner"] = "another-server"
    atomic_write_json(path / "job.json", spec)
    with pytest.raises(ToolFailure):
        service.owned(job["job_id"])


def test_source_directory_not_executable(service):
    service.executables["cloudcompare"] = service.read_roots["fixture"]
    with pytest.raises(ToolFailure, match="source directories"):
        service.executable("cloudcompare")


def test_cloudcompare_explicit_unsigned_native_scalars_no_bin_input(service):
    service.executables["cloudcompare"] = sys.executable
    source = Path(service.read_roots["fixture"])
    (source / "cloud.ply").write_text("fixture")
    job = service.cloudcompare("c2m", "fixture:cloud.ply", "fixture:cube.stl", frame="LOCAL", unit="m")
    spec = json.loads((service.jobs.job_dir(job["job_id"]) / "job.json").read_text())
    assert "-UNSIGNED" in spec["argv"]
    assert "../out/scalars.csv" in spec["argv"]
    assert "-ADD_HEADER" in spec["argv"]
    assert "AUTO" not in spec["argv"] and "-ICP" not in spec["argv"]
    (source / "embedded_shift.bin").write_bytes(b"not allowed")
    with pytest.raises(ToolFailure):
        service.cloudcompare("inspect", "fixture:embedded_shift.bin")


def test_input_size_and_optionlike_names(service):
    source = Path(service.read_roots["fixture"])
    (source / "-version.msh").write_text("fixture")
    job = service.gmsh_inspect("fixture:-version.msh")
    spec = json.loads((service.jobs.job_dir(job["job_id"]) / "job.json").read_text())
    assert spec["argv"][1] == "../in/mesh.msh"
    service.max_input = 1
    with pytest.raises(ToolFailure, match="max_input_bytes"):
        service.source("fixture:cube.vtu", {".vtu"})


def test_reject_jobroot_inside_readroot(tmp_path):
    with pytest.raises(ToolFailure, match="outside all read_roots"):
        EngineeringService(config={"job_root": str(tmp_path/'jobs'), "read_roots": {"all": str(tmp_path)}})


def test_sdk_schemas_annotations_and_failures(service):
    async def run():
        async with Client(build_server(service)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            status = await client.call_tool("engineering_status", {})
            bad = await client.call_tool("gmsh_mesh", {"box_lengths": [1, 0, 1]})
            box = await client.call_tool("gmsh_mesh", {"box_lengths": [1, 1, 1], "size": .25})
            return tools, status, bad, box
    tools, status, bad, box = asyncio.run(run())
    assert len(tools) == 14
    assert not any("publish" in name or "shell" in name for name in tools)
    assert tools["engineering_status"].annotations.read_only_hint is True
    assert tools["gmsh_mesh"].annotations.read_only_hint is False
    assert status.structured_content["ok"] is True
    assert bad.structured_content["ok"] is False
    assert box.structured_content["ok"] is True
    assert tools["engineering_job_wait"].input_schema["properties"]["timeout_s"]["maximum"] == 60


def test_actual_runner_timeout_cleanup(service):
    """Exercise the shared process-tree runner, not a simulated timeout result."""
    from vkm_jobs.procs import spawn_detached
    service.jobs._spawner = lambda argv,cwd,env,log: spawn_detached(argv,cwd=cwd,env=env,log_path=log)
    draft = service.jobs.draft("PY")
    draft.write_input("sleep.py", "import time; time.sleep(30)\n")
    spec = draft.spec(kind="engineering_test_timeout",pool="engineering",argv=[sys.executable,"../in/sleep.py"],timeout_s=1,
                      meta={"owner":OWNER},tree_grace_s=1)
    job = service.jobs.submit(draft,spec)
    result = service.jobs.wait(job["job_id"],20)
    assert result["status"] == "TIMED_OUT"
    receipt = service.jobs.receipt(job["job_id"])
    assert receipt["process_tree"]["active_after"] == 0
    assert receipt["process_tree"]["killed_by_runner"] is True
