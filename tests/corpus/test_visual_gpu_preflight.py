"""CPU-only admission tests: never run native models or claim actual CUDA proof."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[2] / "infra" / "edge" / "visual-owner" / "gpu_preflight.py"
_SPEC = importlib.util.spec_from_file_location("visual_gpu_preflight_tests", _PATH)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


@pytest.fixture
def recipe():
    return SimpleNamespace(child=SimpleNamespace(scope="VISUAL_OWNER_PRODUCTION", executable="/app/llama-server",
        arguments=gate.EXPECTED_ARGV, port=28083, resources={"weights": gate.WEIGHTS, "mmproj": gate.MMPROJ},
        implementation_files=("/opt/vkm-gpu-preflight.py",)), port=28084, environment={"CUDA_VISIBLE_DEVICES": "0"})


def test_approved_existing_partial_gpu_workload_is_admitted(recipe):
    gate.require_workload(recipe)
    assert recipe.child.arguments[recipe.child.arguments.index("-ngl") + 1] == "20"


@pytest.mark.parametrize("change", ["missing_ngl", "zero_ngl", "fit_on", "changed_layers", "cpu_device",
    "missing_gpu", "all_gpus", "wrong_child_port", "wrong_proxy_port", "synthetic", "missing_gate"])
def test_cpu_or_unapproved_workload_never_reaches_any_native_command(recipe, change):
    argv = list(recipe.child.arguments)
    if change == "missing_ngl":
        index = argv.index("-ngl"); del argv[index:index+2]
    elif change == "zero_ngl": argv[argv.index("-ngl") + 1] = "0"
    elif change == "fit_on": argv[argv.index("--fit") + 1] = "on"
    elif change == "changed_layers": argv[argv.index("-ngl") + 1] = "28"
    elif change == "cpu_device": argv += ["--device", "none"]
    elif change == "missing_gpu": recipe.environment = {}
    elif change == "all_gpus": recipe.environment["CUDA_VISIBLE_DEVICES"] = "0,1"
    elif change == "wrong_child_port": recipe.child.port = 18083
    elif change == "wrong_proxy_port": recipe.port = 18084
    elif change == "synthetic": recipe.child.scope = "SYNTHETIC"
    elif change == "missing_gate": recipe.child.implementation_files = ()
    recipe.child.arguments = tuple(argv)
    with pytest.raises(ValueError): gate.require_workload(recipe)


def test_device_parser_accepts_single_cuda_metadata_but_does_not_claim_model_loaded():
    # This literal is SYNTHETIC driver metadata, not a GPU qualification.
    metadata = gate.require_devices(b"SYNTHETIC device report\navailable devices:\n  CUDA0: NVIDIA GeForce GTX 1650 (4096 MiB)\n")
    assert metadata["device"] == "CUDA0"


@pytest.mark.parametrize("raw", [b"available devices:\nCPU0: host\n", b"available devices:\n",
    b"CUDA1: NVIDIA GeForce GTX 1650\n", b"CUDA0: NVIDIA other GPU\n",
    b"CUDA0: NVIDIA GeForce GTX 1650 SUPER (4096 MiB)\n", b"CUDA0: not NVIDIA GeForce GTX 1650 (4096 MiB)\n",
    b"CUDA0: NVIDIA GeForce GTX 1650\nCUDA1: NVIDIA GeForce GTX 1650\n", b"x" * 65537],
    ids=("cpu_only", "absent", "different_device", "different_gpu", "super_variant", "substring_impostor", "multiple_cuda", "oversized"))
def test_unavailable_wrong_or_multiple_native_cuda_devices_rejected(raw):
    with pytest.raises(ValueError): gate.require_devices(raw)


def test_live_profile_is_explicit_and_cannot_borrow_shadow_binding(recipe, tmp_path):
    raw_sha = "a" * 64
    shadow, shadow_sha = gate.workload_binding("SHADOW", raw_sha)
    live, live_sha = gate.workload_binding("LIVE", raw_sha)
    assert shadow["proxy_port"] == 28084 and live["proxy_port"] == 18083 and shadow_sha != live_sha
    with pytest.raises(ValueError): gate.require_workload(recipe, profile="LIVE")
    recipe.port = 18083
    gate.require_workload(recipe, profile="LIVE")
    with pytest.raises(ValueError): gate.require_workload(recipe)
    # Binding is checked before reading a recipe, any native command or listener.
    with pytest.raises(ValueError, match="profile binding differs"):
        gate.main(["--recipe", str(tmp_path / "absent"), "--recipe-sha256", raw_sha,
            "--workload-profile", "LIVE", "--workload-binding-sha256", shadow_sha])


@pytest.mark.parametrize("profile,sha", [("UNKNOWN", "a"*64), ("LIVE", "working-tree"), ("", "a"*64)])
def test_unbound_workload_is_rejected(profile, sha):
    with pytest.raises(ValueError): gate.workload_binding(profile, sha)


def test_bounded_actual_cpu_software_command_stops_output_at_the_pipe_limit(monkeypatch):
    # Tiny owned Python subprocess, not a native model or CUDA invocation.
    monkeypatch.setattr(gate, "OUTPUT_LIMIT", 1024)
    with pytest.raises(ValueError, match="output exceeds bound"):
        gate._bounded_command([sys.executable, "-I", "-c", "import os; os.write(1,b'x'*65536)"], dict(os.environ))
    expected = b"SYNTHETIC software\n" if os.name == "posix" else b"SYNTHETIC software\r\n"
    assert gate._bounded_command([sys.executable, "-I", "-c", "print('SYNTHETIC software')"], dict(os.environ)) == expected


def test_actual_metadata_command_timeout_kills_its_owned_process(monkeypatch):
    monkeypatch.setattr(gate, "COMMAND_TIMEOUT_S", 0.15)
    with pytest.raises(ValueError, match="deadline exceeded"):
        gate._bounded_command([sys.executable, "-I", "-c", "import time; time.sleep(10)"], dict(os.environ))


def _placement(*, wrong=False, cpu=False):
    values = []
    for i in range(30):
        name = "blk.%d.weight" % i if i < 28 else "output.weight" if i == 28 else "token_embd.weight"
        gpu = (9 <= i <= 28) and not cpu
        if wrong: gpu = i < 20
        base = 10000 + i*1000
        values.append(dict(name=name, group="blk.%d" % i if i < 28 else "output" if i == 28 else "other",
            aliases=[], tensor=100+i, storage_tensor=100+i, data=base, storage_data=base,
            buffer=200+i, base=base, buft=300+i, device=400+int(gpu), tensor_bytes=64,
            storage_bytes=64, buffer_bytes=128, host=not gpu, device_kind="GPU" if gpu else "CPU",
            device_name="CUDA0" if gpu else "CPU", buffer_name="CUDA0" if gpu else "CPU",
            view_chain=[100+i], view_offsets=[], tensor_type=0, shape=[16,1,1,1]))
    return dict(schema_version="vkm-native-target-weight-placement/1", scope="LLAMA_TARGET_WEIGHT_PLACEMENT",
        model_handle=1, context_handle=2, no_alloc=False, layer_count=28, total_layer_count=28,
        tensors=sorted(values, key=lambda row:(row["name"],row["tensor"])))


@pytest.mark.parametrize("failure", ["wrong_groups", "host_owner", "missing_placement", "missing_gpu", "changed_owner"])
def test_actual_owner_profile_is_gated_before_any_external_listener(recipe, monkeypatch, tmp_path, failure):
    from vkm_corpus.update import visual_owner_bridge as bridge_module
    events = []
    placement = _placement(wrong=failure == "wrong_groups", cpu=failure == "host_owner")
    witness = {} if failure == "missing_placement" else {"target_weight_placement": placement}
    if failure == "missing_gpu": recipe.environment = {}
    owner = SimpleNamespace(_witness=witness, observe=lambda: SimpleNamespace(process_sha256="b"*64,witness_sha256="c"*64))
    def check():
        events.append("fresh_owner_check")
        if failure == "changed_owner": raise ValueError("owner changed before proxy")
    bridge = SimpleNamespace(recipe=recipe, owner=owner, _check=check, close=lambda: events.append("closed"))
    monkeypatch.setattr(bridge_module, "create_visual_owner", lambda *a: bridge)
    monkeypatch.setattr(bridge_module, "serve_visual_owner", lambda *a: events.append("EXTERNAL_LISTENER"))
    with pytest.raises((ValueError, KeyError)):
        gate.serve_qualified_gpu_owner(tmp_path/"synthetic-control", "a"*64, recipe=recipe, profile="SHADOW",
            stop=threading.Event(), check=lambda: events.append("implementation_check"))
    assert "EXTERNAL_LISTENER" not in events
    if failure == "missing_gpu": assert events == []
    else: assert events[-1] == "closed"


def test_matching_profile_reaches_only_stubbed_listener_after_fresh_owner_check(recipe, monkeypatch, tmp_path, capsys):
    from vkm_corpus.update import visual_owner_bridge as bridge_module
    events = []
    owner = SimpleNamespace(_witness={"target_weight_placement": _placement()},
        observe=lambda: SimpleNamespace(process_sha256="b"*64,witness_sha256="c"*64))
    bridge = SimpleNamespace(recipe=recipe, owner=owner, _check=lambda: events.append("fresh_owner_check"),
        close=lambda: events.append("closed"))
    monkeypatch.setattr(bridge_module, "create_visual_owner", lambda *a: bridge)
    monkeypatch.setattr(bridge_module, "serve_visual_owner", lambda *a: events.append("STUBBED_LISTENER"))
    gate.serve_qualified_gpu_owner(tmp_path/"synthetic-control", "a"*64, recipe=recipe, profile="SHADOW",
        stop=threading.Event(), check=lambda: events.append("implementation_check"))
    assert events == ["implementation_check", "fresh_owner_check", "implementation_check", "STUBBED_LISTENER", "closed"]
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["gpu_residency"] == "NOT_PROVEN" and receipt["inference"] == "NOT_RUN"
    assert receipt["workload_binding"]["profile"] == "SHADOW"


def test_python_implementation_must_be_pinned_before_any_native_preflight(recipe, monkeypatch, tmp_path):
    from vkm_corpus.update import visual_owner_bridge as bridge_module, native_files
    files = {str(Path(gate.__file__).absolute()), *(str(Path(importlib.import_module(name).__file__).absolute())
             for name in bridge_module.BRIDGE_MODULES)}
    recipe.child.implementation_files = tuple(files)
    recipe.child.expected_sha256 = {name:"a"*64 for name in {recipe.child.executable,*files}}
    calls = []
    monkeypatch.setattr(native_files, "NativeFileWatch", lambda *a: calls.append(a))
    with pytest.raises(ValueError, match="Python/owner implementation absent"):
        with gate.implementation_fence(recipe, tmp_path/"absent-control", "a"*64): pass
    assert calls == []


def test_actual_preflight_implementation_bytes_are_checked_before_native_exec(recipe, monkeypatch, tmp_path):
    from vkm_corpus.update import visual_owner_bridge as bridge_module, native_files
    executable = tmp_path/"synthetic-native-bytefile"
    executable.write_bytes(b"SYNTHETIC software, not executable")
    control = tmp_path/"synthetic-control"
    control.write_bytes(b"{}")
    files = {str(Path(gate.__file__).absolute()),str(Path(sys.executable).resolve(strict=True)),
             *(str(Path(importlib.import_module(name).__file__).absolute()) for name in bridge_module.BRIDGE_MODULES)}
    recipe.child.executable = str(executable)
    recipe.child.implementation_files = tuple(files)
    recipe.child.expected_sha256 = {name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in {*files,str(executable)}}
    class SyntheticWatch:
        def __init__(self,*a): pass
        def check(self): pass
        def close(self): pass
    monkeypatch.setattr(native_files,"NativeFileWatch",SyntheticWatch)
    recipe.child.expected_sha256[str(executable)] = "f"*64
    commands = []
    with pytest.raises(ValueError, match="implementation bytes differ"):
        with gate.implementation_fence(recipe,control,hashlib.sha256(b"{}").hexdigest()):
            commands.append("native command would follow")
    assert commands == []
