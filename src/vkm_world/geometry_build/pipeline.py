"""Content-addressed build steps, with checked inputs, code and output inventories.

This is a small runner for existing builders, not a new intake or registry. Cache
entries are immutable and reusable only after every saved output hash is checked.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable

from vkm_world.core.io import resolve_repo_path, sha256_file, sha256_json


def inventory(directory: Path) -> dict[str, str]:
    return {p.relative_to(directory).as_posix(): sha256_file(p)
            for p in sorted(directory.rglob("*")) if p.is_file()}


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def pinned_files(root: Path, paths: list[str]) -> list[dict]:
    """Pin an explicit input file set; reject missing files and Git LFS pointers."""
    result = []
    for rel in sorted(set(paths)):
        path = resolve_repo_path(root, rel)
        if not path.is_file():
            raise FileNotFoundError(f"missing input: {rel}")
        with path.open("rb") as stream:
            if stream.read(100).startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise ValueError(f"unloaded LFS input: {rel}")
        result.append({"path": rel, "sha256": sha256_file(path)})
    return result


def verify_pins(root: Path, pins: list[dict]) -> dict[str, str]:
    expected = {p["path"]: p["sha256"] for p in pins}
    if len(expected) != len(pins):
        raise ValueError("duplicate input paths")
    actual = {p["path"]: p["sha256"] for p in pinned_files(root, list(expected))}
    if actual != expected:
        changed = sorted(p for p in actual if actual[p] != expected[p])
        raise ValueError(f"input pins changed: {changed}")
    return actual


def run_step(name: str, *, cache: Path, destination: Path,
             inputs: dict, parameters: dict, code: dict, dependencies: dict,
             builder: Callable[[Path], object]) -> dict:
    """Reuse a verified immutable entry or build one, then copy a portable bundle.

    Only declared inputs/parameters/code/dependencies participate in the key.
    Callers must enumerate ALL files read by the underlying builder. Operational
    cache-hit state is returned separately from the deterministic step receipt.
    """
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("step name must be alphanumeric with underscores")
    if destination.exists():
        raise FileExistsError(destination)
    specification = {"name": name, "inputs": inputs, "parameters": parameters,
                     "code": code, "dependencies": dependencies}
    key = sha256_json(specification)
    entry = cache / name / key
    receipt_path = entry / "step.json"
    hit = entry.exists()
    if hit:
        if not receipt_path.is_file():
            raise ValueError(f"incomplete cache entry: {name}/{key}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt["specification"] != specification or receipt["outputs"] != inventory(entry / "bundle"):
            raise ValueError(f"cache integrity failure: {name}/{key}")
    else:
        entry.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents multiple writers of one SQLite/QGIS bundle.
        lock = entry.parent / (key + ".lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        pending = entry.parent / (key + ".building")
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            if pending.exists():
                raise ValueError(f"incomplete previous build: {name}/{key}")
            pending.mkdir()
            bundle = pending / "bundle"
            builder(bundle)
            outputs = inventory(bundle)
            if not outputs:
                raise ValueError(f"empty step output: {name}")
            receipt = {"schema": "vkm.spatial_step/1", "key": key,
                       "specification": specification, "outputs": outputs}
            write_json(pending / "step.json", receipt)
            pending.rename(entry)
        finally:
            os.close(descriptor)
            lock.unlink()
    shutil.copytree(entry / "bundle", destination)
    write_json(destination / "step.json", receipt)
    return {"key": key, "cache_hit": hit, "outputs": receipt["outputs"]}


def compare_packages(left: Path, right: Path, *, semantic_digest=None) -> dict:
    """Report byte and optional semantic equality independently."""
    a, b = inventory(left), inventory(right)
    byte_differences = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    semantic_differences = []
    if semantic_digest:
        semantic_differences = sorted(k for k in a.keys() | b.keys()
                                      if k not in a or k not in b or
                                      semantic_digest(left / k) != semantic_digest(right / k))
    return {"bytes_equal": not byte_differences, "byte_differences": byte_differences,
            "semantics_equal": not semantic_differences if semantic_digest else None,
            "semantic_differences": semantic_differences if semantic_digest else None}
