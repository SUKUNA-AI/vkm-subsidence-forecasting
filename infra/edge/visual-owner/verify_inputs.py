"""Offline build-input guard, not a loaded-model or image qualification.

Only approved native source and prepared software artifacts enter the build.
This reader executes Git metadata commands, never an input-selected program.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

UPSTREAM = "4da6337767f973e2b4d0797e5b323d77d8565e4a"
SOURCE_ARCHIVE = "eef03eca6f90860158bc1b237845a60492520270ab88b023f9f5cafefcb8d5f1"
BASES = {
    "python": "python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26",
    "devel": "nvidia/cuda:13.2.1-devel-ubuntu24.04@sha256:44a9504c6dfb50b1241464241b02a93871928f373de6f5a644cf5fe9f080aa63",
    "runtime": "nvidia/cuda:13.2.1-runtime-ubuntu24.04@sha256:4c17c2e7474ca1d470428b25e87e95756f51e7da3b663859894a03bc5a7ebdae",
}
CONTROL = {"schema_version", "status", "base_images", "source_commit", "source_archive_sha256",
           "public_source_commit", "files"}
FOLDERS = {"builder-debs", "runtime-debs", "wheelhouse", "patches"}
FIXED = {"dependencies.lock.txt", "vkm-loaded-lifetime.h", "vkm-weight-placement.h", "vkm_world-0.1.0-py3-none-any.whl", "llama-source.tar", "gpu_preflight.py",
         "builder-debs/SHA256SUMS", "runtime-debs/SHA256SUMS", "Dockerfile", ".dockerignore", "verify_inputs.py", "requirements.txt"}
PATCHES = {"0001-qwen2vl-skip-lm-head-for-embeddings.patch", "0002-vkm-owned-loaded-witness.patch"}
CACHED = {"Dockerfile.cached-native", "verify_native_cache.py", "native-cache-plan.json",
          "native-delta/server-context.cpp"}
CACHED_IMAGE = "sha256:c0c0280b8f59e040fadaba3f14bea0f9d8dae93c86fd9de7ca76f8203006609a"


def unique_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate build-input field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite build-input field")))


def ordinary(path: Path, limit=1024**3, *, allow_empty=False):
    for value in (path, *path.parents):
        if stat.S_ISLNK(value.lstat().st_mode):
            raise ValueError("indirect build input")
    before = path.stat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or not int(not allow_empty) <= before.st_size <= limit):
        raise ValueError("ordinary bounded immutable build input required")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    after = path.stat()
    if len(raw) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("build input changed while reading")
    return raw


def git(source: Path, *args):
    environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return subprocess.check_output(["git", "--no-optional-locks", "-c", "core.autocrlf=false", "-c", "core.fsmonitor=false", "-c",
                                   "core.hooksPath=" + os.devnull, "-C", str(source), *args], env=environment, timeout=60)


def verify_source(source: Path, expected_commit: str, archive_sha256: str):
    """Compare every working-tree byte with its pinned Git blob, not mtimes."""
    if git(source, "rev-parse", "HEAD").decode("ascii").strip() != expected_commit:
        raise ValueError("upstream commit differs")
    records = git(source, "ls-tree", "-r", "-z", expected_commit).split(b"\0")
    names, total = set(), 0
    for record in records:
        if not record:
            continue
        identity, raw_name = record.split(b"\t", 1)
        mode, kind, blob = identity.split(b" ")
        name = raw_name.decode("utf-8")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or name in names or kind != b"blob":
            raise ValueError("unsupported upstream source identity")
        path = source / relative
        if mode not in {b"100644", b"100755"}:
            raise ValueError("upstream source must be ordinary files")
        raw = ordinary(path, 64 * 1024**2, allow_empty=True)
        total += len(raw)
        if total > 512 * 1024**2 or len(names) >= 20000:
            raise ValueError("upstream source exceeds preparation budget")
        if hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest() != blob.decode("ascii"):
            raise ValueError("upstream working-tree bytes differ from pinned Git blob")
        names.add(name)
    actual = set()
    for parent, directories, files in os.walk(source, followlinks=False):
        if Path(parent) == source:
            directories[:] = [name for name in directories if name != ".git"]
        if any((Path(parent) / name).is_symlink() for name in directories):
            raise ValueError("indirect upstream source directory")
        actual.update((Path(parent) / name).relative_to(source).as_posix() for name in files)
    if actual != names:
        raise ValueError("upstream tree contains missing or untracked inputs")
    archive = git(source, "archive", "--format=tar", expected_commit)
    if hashlib.sha256(archive).hexdigest() != archive_sha256:
        raise ValueError("upstream source archive differs")
    return {"files": len(names), "source_bytes": total}


def verify(root: Path, expected_manifest_sha256: str):
    raw = ordinary(root / "inputs.json", 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError("exact approved build manifest differs")
    plan = unique_json(raw)
    if (set(plan) != CONTROL or plan["schema_version"] != "vkm-visual-owner-build-inputs/1"
            or plan["status"] != "PLANNED_NOT_BUILT_NOT_MODEL_QUALIFIED" or plan["base_images"] != BASES
            or plan["source_commit"] != UPSTREAM or not re.fullmatch(r"[0-9a-f]{40}", plan["public_source_commit"])
            or plan["source_archive_sha256"] != SOURCE_ARCHIVE):
        raise ValueError("approved build-input contract differs")
    inventory = plan["files"]
    if not isinstance(inventory, dict) or not 18 <= len(inventory) <= 512:
        raise ValueError("bounded complete software input inventory required")
    if inventory.get("llama-source.tar") != plan["source_archive_sha256"]:
        raise ValueError("native archive file must match the approved source archive")
    actual = set(FIXED)
    cached = set(inventory) & CACHED
    if cached and cached != CACHED:
        raise ValueError("cached native inputs must be present together")
    actual.update(cached)
    # An unlisted delta is never silently ignored by a cold build manifest.
    present = {name for name in CACHED if (root / name).exists()}
    if present != cached or not cached and (root / "native-delta").exists():
        raise ValueError("cached native context differs from the approved variant")
    if cached:
        directory = root / "native-delta"
        if (not directory.is_dir() or directory.is_symlink()
                or {p.name for p in directory.iterdir()} != {"server-context.cpp"}):
            raise ValueError("only the approved cached source delta is allowed")
    for folder in FOLDERS:
        path = root / folder
        if not path.is_dir() or path.is_symlink():
            raise ValueError("direct software input directory required")
        actual.update((Path(folder) / item.name).as_posix() for item in path.iterdir())
    if actual != set(inventory):
        raise ValueError("software input inventory differs")
    total = 0
    for name, expected in inventory.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or len(path.parts) > 2:
            raise ValueError("invalid build artifact path")
        if name not in FIXED | CACHED:
            folder, filename = path.parts
            if (folder in {"builder-debs", "runtime-debs"} and not filename.endswith(".deb")
                    or folder == "wheelhouse" and not filename.endswith(".whl")
                    or folder == "patches" and filename not in PATCHES):
                raise ValueError("unsupported software input")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("exact software artifact hash required")
        content = ordinary(root / path)
        total += len(content)
        if total > 1024**3 or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("software input bytes or budget differ")
    if cached:
        cache = unique_json(ordinary(root / "native-cache-plan.json", 2 * 1024**2))
        candidate = cache.get("candidate_source_files") if isinstance(cache, dict) else None
        if (not isinstance(candidate, dict) or cache.get("schema_version") != "vkm-native-cache-plan/1"
                or cache.get("status") != "PINNED_CACHE_DELTA_NOT_BUILT_NOT_MODEL_QUALIFIED"
                or cache.get("cache_image_id") != CACHED_IMAGE or cache.get("upstream_commit") != UPSTREAM
                or cache.get("source_archive_sha256") != SOURCE_ARCHIVE
                or cache.get("base_images") != BASES
                or any(candidate.get("/src/tools/server/" + native) != inventory[artifact]
                       for native, artifact in (
                           ("server-context.cpp", "native-delta/server-context.cpp"),
                           ("vkm-loaded-lifetime.h", "vkm-loaded-lifetime.h"),
                           ("vkm-weight-placement.h", "vkm-weight-placement.h")))):
            raise ValueError("cached candidate source bytes are not bound to the build inputs")
    source = verify_source(root / "llama-source", UPSTREAM, plan["source_archive_sha256"])
    if total + source["source_bytes"] > 1024**3:
        raise ValueError("combined source/software inputs exceed approved download/context budget")
    return {"status": "BUILD_INPUTS_VALIDATED_NOT_IMAGE_OR_MODEL_QUALIFIED",
            "native_route": "PINNED_CACHE_DELTA" if cached else "COLD_SOURCE_BUILD",
            "software_files": len(inventory), "software_bytes": total, "source": source,
            "public_source_commit": plan["public_source_commit"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(verify(args.root.absolute(), args.manifest_sha256), sort_keys=True))


if __name__ == "__main__":
    main()
