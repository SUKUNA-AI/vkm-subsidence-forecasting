"""This GPU workload's admission gate; never a model residency proof.

The generic native owner remains device-neutral. This entrypoint accepts only
the explicitly approved existing mixed CPU/GPU profile, forbids CPU-only launch,
and probes the actual CUDA backend before its first model load.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

WEIGHTS = "/models/m0/jina-reranker-m0-Q6_K.gguf"
MMPROJ = "/models/mmproj/mmproj-jina-reranker-m0-Q8_0.gguf"
EXPECTED_ARGV = ("-m", WEIGHTS, "--mmproj", MMPROJ, "--embeddings", "--pooling", "last",
    "-ngl", "20", "--fit", "off", "-fa", "on", "-c", "1024", "-np", "1", "-b", "1024", "-ub", "512",
    "--image-min-tokens", "4", "--image-max-tokens", "768", "--no-warmup", "--cache-ram", "0",
    "--no-webui", "--metrics", "-t", "6", "--host", "127.0.0.1", "--port", "28083")
PROFILES = {"SHADOW": 28084, "LIVE": 18083}
OUTPUT_LIMIT = 65536
COMMAND_TIMEOUT_S = 20


def workload_binding(profile, recipe_sha256):
    if profile not in PROFILES or not re.fullmatch(r"[0-9a-f]{64}", recipe_sha256):
        raise ValueError("explicit approved GPU workload binding required")
    value = {"schema_version": "vkm-visual-gpu-workload-binding/1", "profile": profile,
             "recipe_sha256": recipe_sha256, "proxy_port": PROFILES[profile], "child_port": 28083}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    return value, hashlib.sha256(raw).hexdigest()


def require_workload(recipe, *, profile="SHADOW"):
    child = recipe.child
    if (child.scope != "VISUAL_OWNER_PRODUCTION" or child.executable != "/app/llama-server"
            or tuple(child.arguments) != EXPECTED_ARGV or child.port != 28083
            or profile not in PROFILES or recipe.port != PROFILES[profile]
            or recipe.environment.get("CUDA_VISIBLE_DEVICES") != "0"
            or child.resources.get("weights") != WEIGHTS or child.resources.get("mmproj") != MMPROJ):
        raise ValueError("approved GPU workload differs; CPU-only/auto-fit profiles are forbidden")
    if "/opt/vkm-gpu-preflight.py" not in child.implementation_files:
        raise ValueError("GPU preflight absent from load-time implementation inventory")


def require_devices(raw: bytes):
    if len(raw) > 65536:
        raise ValueError("native CUDA device report exceeds bound")
    text = raw.decode("utf-8", errors="strict")
    devices = re.findall(r"^\s*(CUDA[0-9]+):\s+(.+)$", text, re.MULTILINE)
    if (len(devices) != 1 or devices[0][0] != "CUDA0"
            or re.fullmatch(r"NVIDIA GeForce GTX 1650(?: \([^\r\n()]{1,256}\))?", devices[0][1]) is None):
        raise ValueError("exact single native CUDA0 device unavailable; no CPU model fallback")
    return {"device": devices[0][0], "description": devices[0][1]}


def _bounded_command(command, environment):
    # Output is software/driver metadata, not corpus input or credentials.
    # Stop at the pipe bound, rather than buffering arbitrary output first.
    process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True,
        start_new_session=True, bufsize=0)
    chunks, failed, finished = [], [], threading.Event()
    def read():
        size = 0
        try:
            while block := os.read(process.stdout.fileno(), 4096):
                size += len(block)
                if size > OUTPUT_LIMIT:
                    failed.append("native preflight output exceeds bound")
                    return
                chunks.append(block)
        except OSError:
            failed.append("native preflight output unavailable")
        finally:
            finished.set()
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + COMMAND_TIMEOUT_S
    try:
        if not finished.wait(max(0, deadline - time.monotonic())):
            raise ValueError("native preflight deadline exceeded")
        if failed: raise ValueError(failed[0])
        if process.wait(timeout=max(0.01, deadline-time.monotonic())) != 0:
            raise ValueError("native preflight command failed")
        return b"".join(chunks)
    finally:
        # Kill the owned process group as well if it retained a pipe in a child.
        if os.name == "posix":
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        reader.join(timeout=2)
        process.stdout.close()


class _NoDiagnostics:
    def enter(self, stage): pass
    def placement(self, groups): pass
    def cleanup_failure(self, component, exc): pass


@contextlib.contextmanager
def implementation_fence(recipe, path, raw_sha256, *, diagnostics=None):
    """Qualify executable/library/code bytes before the first native command."""
    diag = _NoDiagnostics() if diagnostics is None else diagnostics
    from vkm_corpus.parquet.atomic import sha256_of
    from vkm_corpus.update.native_files import NativeFileWatch
    from vkm_corpus.update.remote_retrieval import file_signature
    from vkm_corpus.update.visual_owner_bridge import BRIDGE_MODULES
    inventory = set(recipe.child.implementation_files)
    required = {str(Path(__file__).absolute()), str(Path(sys.executable).resolve(strict=True))}
    for name in BRIDGE_MODULES:
        required.add(str(Path(importlib.import_module(name).__file__).absolute()))
    if not required <= inventory:
        raise ValueError("actual GPU preflight/Python/owner implementation absent from inventory")
    selected = {recipe.child.executable, *inventory}
    if not selected <= set(recipe.child.expected_sha256):
        raise ValueError("native preflight implementation hashes absent from recipe")
    watch = NativeFileWatch([path, *selected])
    failed = False
    try:
        signatures = {p: file_signature(p) for p in (str(path), *selected)}
        if any(Path(p).stat().st_nlink != 1 for p in signatures):
            raise ValueError("native preflight implementation aliases forbidden")
        if (sha256_of(path) != raw_sha256
                or any(sha256_of(Path(p)) != recipe.child.expected_sha256[p] for p in selected)):
            raise ValueError("native preflight implementation bytes differ")
        def check():
            watch.check()
            if any(file_signature(p) != before for p, before in signatures.items()):
                raise ValueError("native preflight implementation changed")
        check()
        yield check
    except BaseException:
        failed = True
        raise
    finally:
        try:
            watch.close()
        except Exception as cleanup:  # secondary; never replaces a primary failure
            diag.cleanup_failure("IMPLEMENTATION_FENCE_CLOSE", cleanup)
            if not failed: raise


def preflight(recipe, *, recipe_raw_sha256, profile="SHADOW", check):
    require_workload(recipe, profile=profile)
    check()
    environment = dict(recipe.environment)
    # CUDA backend is loaded dynamically; direct executable ldd is insufficient.
    backend = Path("/app/libggml-cuda.so")
    if not backend.is_file() or backend.is_symlink():
        raise ValueError("ordinary pinned CUDA backend required before model load")
    if str(backend) not in recipe.child.implementation_files:
        raise ValueError("CUDA backend absent from the load-time implementation inventory")
    libraries = _bounded_command(["/usr/bin/ldd", str(backend)], environment)
    check()
    if b"not found" in libraries:
        raise ValueError("CUDA runtime dependency missing before model load")
    devices = require_devices(_bounded_command([recipe.child.executable, "--list-devices"], environment))
    check()
    return {"schema": "vkm-visual-gpu-device-preflight/1",
            "status": "CUDA_DEVICE_PREFLIGHT_PASSED_NOT_MODEL_QUALIFIED", "recipe_sha256": recipe_raw_sha256,
            "workload_binding": workload_binding(profile, recipe_raw_sha256)[0],
            "native_gpu_layers": 20, "fit": "off", "visible_devices": "0", "devices": devices,
            "model_load": "NOT_RUN", "gpu_residency": "NOT_PROVEN"}


def serve_qualified_gpu_owner(path, raw_sha256, *, recipe, profile, stop, check, diagnostics=None,
                              on_listening=None, cancel=None):
    """THIS GPU recipe refuses to open a proxy for HOST/wrong native placement."""
    from vkm_corpus.update import visual_owner_bridge
    from vkm_corpus.update.visual_placement import TargetWeightPlacement, require_cuda_weight_profile
    diag = _NoDiagnostics() if diagnostics is None else diagnostics
    owner_options = {} if diagnostics is None else {"diagnostics": diagnostics}
    if cancel is not None: owner_options["cancel"] = cancel
    bridge = None
    failed = False
    try:
        require_workload(recipe, profile=profile)
        check()
        bridge = visual_owner_bridge.create_visual_owner(path, raw_sha256, **owner_options)
        diag.enter("PLACEMENT_PROFILE")
        if bridge.recipe != recipe:
            raise ValueError("actual GPU owner recipe differs from preflight")
        require_workload(bridge.recipe, profile=profile)
        identity = bridge.owner.observe()
        placement = TargetWeightPlacement.model_validate(bridge.owner._witness["target_weight_placement"])
        diag.placement(placement.summary()["groups"])
        summary = require_cuda_weight_profile(placement)
        bridge._check()
        check()
        receipt = {"schema_version": "vkm-visual-gpu-startup-placement/1",
            "status": "APPROVED_TARGET_WEIGHT_PLACEMENT_BEFORE_PROXY",
            "workload_binding": workload_binding(profile, raw_sha256)[0],
            "process_sha256": identity.process_sha256, "witness_sha256": identity.witness_sha256,
            "placement_sha256": placement.sha256, "observation": summary,
            "gpu_residency": "NOT_PROVEN", "inference": "NOT_RUN"}
        print(json.dumps(receipt, sort_keys=True), flush=True)
        def listening():
            if on_listening is not None: on_listening()
            if diagnostics is not None:
                print(diagnostics.line("visual_gpu_owner_ready"), file=sys.stderr, flush=True)
        serve_options = ({} if diagnostics is None and on_listening is None
                         else {"diagnostics": diagnostics, "on_listening": listening})
        visual_owner_bridge.serve_visual_owner(bridge, stop, **serve_options)
    except BaseException:
        failed = True
        raise
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception as cleanup:  # secondary; never replaces a primary failure
                diag.cleanup_failure("BRIDGE_CLOSE", cleanup)
                if not failed: raise


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--recipe-sha256", required=True)
    parser.add_argument("--workload-profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--workload-binding-sha256", required=True)
    # Optional operator-owned writable directory for the private diagnostic receipt.
    parser.add_argument("--diagnostics-dir", type=Path)
    return parser


def main(argv=None, *, diagnostics=None):
    diag = _NoDiagnostics() if diagnostics is None else diagnostics
    diag.enter("GPU_RECIPE")
    args = _parser().parse_args(argv)
    # A stop signal before serving interrupts startup once (closed class in the
    # diagnostic line, owned child closed by the normal cleanup path); after the
    # listener opens it only requests the ordinary graceful stop.
    stop = threading.Event()
    state = {"serving": False, "interrupted": False}
    def on_signal(*_):
        stop.set()
        if not state["serving"] and not state["interrupted"]:
            state["interrupted"] = True
            raise InterruptedError("startup interrupted by stop signal")
    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return _startup(args, diag, diagnostics, stop, state)
    finally:
        for sig, handler in previous.items(): signal.signal(sig, handler)


def _startup(args, diag, diagnostics, stop, state):
    _, binding = workload_binding(args.workload_profile, args.recipe_sha256)
    if binding != args.workload_binding_sha256:
        raise ValueError("approved raw recipe/workload profile binding differs")
    path = args.recipe.absolute()
    for parent in (path, *path.parents):
        if stat.S_ISLNK(parent.lstat().st_mode): raise ValueError("indirect GPU recipe")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= 1024*1024:
        raise ValueError("ordinary bounded GPU recipe required")
    raw = path.read_bytes()
    after = path.stat()
    if (len(raw) > 1024*1024 or hashlib.sha256(raw).hexdigest() != args.recipe_sha256
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
               (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError("approved GPU recipe bytes changed")
    from vkm_corpus.update.visual_owner_bridge import VisualOwnerRecipe, _json
    recipe = VisualOwnerRecipe.model_validate(_json(raw))
    require_workload(recipe, profile=args.workload_profile)
    diag.enter("IMPLEMENTATION_FENCE")
    fence_options = {} if diagnostics is None else {"diagnostics": diagnostics}
    with implementation_fence(recipe, path, args.recipe_sha256, **fence_options) as check:
        diag.enter("CUDA_PREFLIGHT")
        print(json.dumps(preflight(recipe, recipe_raw_sha256=args.recipe_sha256,
            profile=args.workload_profile, check=check), sort_keys=True), flush=True)
        check()
        serve_qualified_gpu_owner(path, args.recipe_sha256, recipe=recipe,
            profile=args.workload_profile, stop=stop, check=check,
            on_listening=lambda: state.update(serving=True), cancel=stop.is_set, **fence_options)
    return 0


def run(argv=None):
    """Exit-code entrypoint. Prints one closed public diagnostic line on failure.

    The fixed marker stays first so existing log readers keep working. No
    exception text, path, argv, environment or credential is ever printed.
    """
    diagnostics, directory = None, None
    try:
        from vkm_corpus.update.owner_diagnostics import StartupDiagnostics
        from vkm_corpus.update.visual_owner_bridge import code_identity
        known, _ = _parser().parse_known_args(argv)
        directory = known.diagnostics_dir
        identity = {"recipe_sha256": known.recipe_sha256}
        try:
            identity["code_sha256"] = hashlib.sha256((code_identity() + hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest()).encode("ascii")).hexdigest()
        except Exception:
            pass
        diagnostics = StartupDiagnostics(kind="visual", identity=identity)
    except BaseException:
        diagnostics = None  # argument/import failure: fall back to the bare marker
    try:
        return main(argv, diagnostics=diagnostics)
    except Exception as exc:
        if diagnostics is None:
            print("visual_gpu_owner_startup_unavailable", file=sys.stderr, flush=True)
        else:
            diagnostics.fail(exc)
            diagnostics.write_private(directory)
            print(diagnostics.line("visual_gpu_owner_startup_unavailable"), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
