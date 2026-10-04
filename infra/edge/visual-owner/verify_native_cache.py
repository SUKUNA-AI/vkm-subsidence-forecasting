"""Immutable native cache and exact three-source delta; no model is loaded."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from pathlib import Path, PurePosixPath

CACHE_IMAGE = "sha256:c0c0280b8f59e040fadaba3f14bea0f9d8dae93c86fd9de7ca76f8203006609a"
UPSTREAM = "4da6337767f973e2b4d0797e5b323d77d8565e4a"
ARCHIVE = "eef03eca6f90860158bc1b237845a60492520270ab88b023f9f5cafefcb8d5f1"
SOURCE_COUNT = 3648
OVERLAYS = {"/src/tools/server/server-context.cpp", "/src/tools/server/vkm-loaded-lifetime.h",
            "/src/tools/server/vkm-weight-placement.h"}
BASES = {
    "python": "python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26",
    "devel": "nvidia/cuda:13.2.1-devel-ubuntu24.04@sha256:44a9504c6dfb50b1241464241b02a93871928f373de6f5a644cf5fe9f080aa63",
    "runtime": "nvidia/cuda:13.2.1-runtime-ubuntu24.04@sha256:4c17c2e7474ca1d470428b25e87e95756f51e7da3b663859894a03bc5a7ebdae",
}
FLAGS = {"CMAKE_BUILD_TYPE": "Release", "CMAKE_CUDA_ARCHITECTURES": "75",
    "CMAKE_CUDA_COMPILER": "/usr/local/cuda/bin/nvcc", "CMAKE_CXX_COMPILER": "/usr/bin/c++",
    "CMAKE_EXE_LINKER_FLAGS": "-Wl,--allow-shlib-undefined", "GGML_BACKEND_DL": "ON",
    "GGML_CPU_ALL_VARIANTS": "ON", "GGML_CUDA": "ON", "GGML_NATIVE": "OFF",
    "LLAMA_BUILD_EXAMPLES": "OFF", "LLAMA_BUILD_TESTS": "OFF", "LLAMA_CURL": "OFF"}
FIELDS = {"schema_version", "status", "cache_image_id", "upstream_commit", "source_archive_sha256",
    "base_images", "source_files", "candidate_source_files", "files", "links", "cmake_flags",
    "driver_dependency", "compiler_version", "cuda_compiler_version", "cold_inputs_sha256"}
EXPECTED_FILES = {
    "/src/build/CMakeCache.txt", "/src/build/compile_commands.json",
    "/out/llama-server", "/out/BUILD_CGROUP_LIMITS.txt", "/out/SHA256SUMS", "/out/libggml-cuda.so",
    "/out/libggml-base.so.0.25.3", "/out/libggml.so.0.25.3", "/out/libllama-common.so.0.5.0",
    "/out/libllama-server-impl.so", "/out/libllama.so.0.5.0", "/out/libmtmd.so.0.5.0",
    *["/out/libggml-cpu-" + name + ".so" for name in (
        "alderlake", "cannonlake", "cascadelake", "cooperlake", "haswell", "icelake", "ivybridge",
        "piledriver", "sandybridge", "sapphirerapids", "skylakex", "sse42", "x64", "zen4")],
    "/usr/bin/x86_64-linux-gnu-gcc-13", "/usr/bin/x86_64-linux-gnu-g++-13", "/usr/bin/cmake", "/usr/bin/ninja",
    "/usr/local/cuda-13.2/bin/nvcc",
    *["/usr/lib/x86_64-linux-gnu/" + name for name in ("libc.so.6", "libdl.so.2", "libgcc_s.so.1",
        "libgomp.so.1.0.0", "libm.so.6", "libpthread.so.0", "librt.so.1", "libstdc++.so.6.0.33", "ld-linux-x86-64.so.2")],
    *["/usr/local/cuda-13.2/targets/x86_64-linux/lib/" + name for name in (
        "libcublas.so.13.4.0.1", "libcublasLt.so.13.4.0.1", "libcudart.so.13.2.75")],
}
EXPECTED_LINKS = {
    "/out/libggml-base.so": "libggml-base.so.0", "/out/libggml-base.so.0": "libggml-base.so.0.25.3",
    "/out/libggml.so": "libggml.so.0", "/out/libggml.so.0": "libggml.so.0.25.3",
    "/out/libllama-common.so": "libllama-common.so.0", "/out/libllama-common.so.0": "libllama-common.so.0.5.0",
    "/out/libllama.so": "libllama.so.0", "/out/libllama.so.0": "libllama.so.0.5.0",
    "/out/libmtmd.so": "libmtmd.so.0", "/out/libmtmd.so.0": "libmtmd.so.0.5.0",
}


def unique(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError("duplicate native cache field")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite native cache field")))


def image_path(root: Path, name: str):
    path = PurePosixPath(name)
    if (not path.is_absolute() or ".." in path.parts or "\\" in name or "\0" in name
            or any(ord(c) < 32 for c in name) or path.parts[1] not in {"src", "out", "usr"}):
        raise ValueError("invalid native cache image path")
    return root.joinpath(*path.parts[1:])


def digest(path: Path):
    before = path.stat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 1024**3
            or path.is_symlink()):
        raise ValueError("ordinary native cache file required")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b""): result.update(block)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("native cache file changed during verification")
    return result.hexdigest()


def require_map(value, *, source=False):
    if not isinstance(value, dict) or not 1 <= len(value) <= 20000:
        raise ValueError("bounded native cache inventory required")
    for name, sha in value.items():
        if (not isinstance(name, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or source and not name.startswith("/src/")):
            raise ValueError("exact native cache file hash required")


def verify(root: Path, plan_path: Path, expected_sha: str, *, phase="before"):
    if phase not in {"before", "after-overlay"}: raise ValueError("explicit native cache phase required")
    if plan_path.is_symlink() or plan_path.stat().st_size > 2*1024*1024 or plan_path.stat().st_nlink != 1:
        raise ValueError("ordinary bounded native cache plan required")
    raw = plan_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("exact approved native cache plan differs")
    plan = unique(raw)
    if (set(plan) != FIELDS or plan["schema_version"] != "vkm-native-cache-plan/1"
            or plan["status"] != "PINNED_CACHE_DELTA_NOT_BUILT_NOT_MODEL_QUALIFIED"
            or plan["cache_image_id"] != CACHE_IMAGE or plan["upstream_commit"] != UPSTREAM
            or plan["source_archive_sha256"] != ARCHIVE or plan["base_images"] != BASES
            or plan["cmake_flags"] != FLAGS or plan["driver_dependency"] != "libcuda.so.1:RUNTIME_NOT_PROVEN"
            or plan["cold_inputs_sha256"] != "391d564d3e824d51747694ff13d89f4be18a2edf0e77aaf3a7447e4b09af5106"
            or plan["compiler_version"] != "c++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0"
            or plan["cuda_compiler_version"] != "Build cuda_13.2.r13.2/compiler.37668154_0"):
        raise ValueError("native cache provenance or configuration differs")
    for key in ("source_files", "candidate_source_files", "files"): require_map(plan[key], source=key != "files")
    before, after = plan["source_files"], plan["candidate_source_files"]
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    if (len(before) != SOURCE_COUNT or len(after) != SOURCE_COUNT+1 or changed != OVERLAYS
            or set(before) - set(after) or set(after) - set(before) != {"/src/tools/server/vkm-weight-placement.h"}):
        raise ValueError("native cache delta must change only the exact three approved sources")
    if set(plan["files"]) != EXPECTED_FILES or plan["links"] != EXPECTED_LINKS:
        raise ValueError("complete native cache binary/configuration inventory required")
    root = root.resolve(strict=True)
    inventory = dict(before if phase == "before" else after)
    inventory.update(plan["files"])
    for name, sha in inventory.items():
        path = image_path(root, name)
        if path.resolve(strict=True) != path.absolute() or not path.is_relative_to(root) or digest(path) != sha:
            raise ValueError("native cache source/binary/dependency bytes differ")
    for name, target in plan["links"].items():
        path = image_path(root, name)
        if (not isinstance(target, str) or not path.is_symlink() or str(path.readlink()) != target
                or not path.resolve(strict=True).is_relative_to(root)
                or "/" + path.resolve(strict=True).relative_to(root).as_posix() not in plan["files"]):
            raise ValueError("native cache library alias differs")
    actual = {}
    for line in image_path(root, "/src/build/CMakeCache.txt").read_text().splitlines():
        if ":" in line and "=" in line:
            name, value = line.split(":", 1)
            if name in FLAGS: actual[name] = value.split("=", 1)[1]
    if actual != FLAGS: raise ValueError("actual native cached compiler configuration differs")
    # Unknown source files can never enter the cached compile through includes.
    found = set()
    for path in image_path(root, "/src").rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("src/build/"): continue
        if path.is_symlink(): raise ValueError("indirect native cached source")
        if path.is_file(): found.add("/"+relative)
    if found != set(before if phase == "before" else after): raise ValueError("unexpected native cached source file")
    return {"schema_version": "vkm-native-cache-verification/1", "phase": phase,
        "status": "CACHE_SOURCE_AND_BINARY_BYTES_VERIFIED_NO_MODEL", "cache_image_id": CACHE_IMAGE,
        "plan_sha256": expected_sha, "source_files": len(before if phase == "before" else after),
        "checked_files": len(inventory), "cpu_model_execution": "NOT_RUN", "gpu_model_execution": "NOT_RUN"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--phase", choices=("before", "after-overlay"), required=True)
    args = parser.parse_args(argv)
    print(json.dumps(verify(Path("/"), args.plan, args.plan_sha256, phase=args.phase), sort_keys=True))


if __name__ == "__main__": main()
