"""Thin batch adapter for Ansys MAPDL (solver as adapter, CLAUDE.md ladder): run an input deck, keep a receipt.

The executable is taken from ``VKM_MAPDL_EXE`` (no machine paths in tracked files). Each run leaves a receipt with
the input SHA-256, the command, exit code, runtime, the number of errors reported in the output and the SHA-256 of
every result text file it produced.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from numbers import Integral
from pathlib import Path

SCRATCH_SUFFIXES = {".rst", ".esav", ".full", ".emat", ".DSP", ".page", ".osav", ".ldhi", ".r001", ".rdb", ".stat",
                    ".db", ".dbb", ".lock", ".mode", ".sav", ".BCS", ".PCS", ".rnnn", ".cnm", ".cnd", ".ce"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mapdl_exe() -> str:
    exe = os.environ.get("VKM_MAPDL_EXE")
    if not exe:
        raise RuntimeError("set VKM_MAPDL_EXE to the MAPDL executable")
    resolved = shutil.which(exe)
    if resolved is None or not Path(resolved).is_file():
        raise FileNotFoundError("VKM_MAPDL_EXE must name an available executable file")
    return str(Path(resolved).resolve())


def count_errors(out_text: str) -> int | None:
    for line in out_text.splitlines():
        if "NUMBER OF ERROR   MESSAGES ENCOUNTERED" in line:
            return int(line.split("=")[-1])
    return None


STALE_PATTERNS = ("*.txt", "*.npz", "receipt.json", "run.log")


def _validate_directory_names(workdir, deck, jobname):
    workdir = Path(workdir).resolve(strict=True)
    if not workdir.is_dir():
        raise NotADirectoryError(workdir)
    if not isinstance(deck, str) or not deck or Path(deck).name != deck or deck in {".", ".."}:
        raise ValueError("deck must be a filename inside workdir")
    if not isinstance(jobname, str) or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", jobname) is None:
        raise ValueError("jobname must be a simple solver job name")
    return workdir


def _archive_files(workdir, paths, *, phase, preserve=()):
    """Verify a restorable copy of every file before removing any active output.

    Archives are never automatically deleted. On partial retirement failure,
    restore the removed originals; retained verified copies survive either way.
    """
    paths = sorted(set(paths))
    if not paths:
        return {"directory": None, "files_sha256": {}, "retired": []}
    for path in paths:
        if path.is_symlink() or path.resolve().parent != workdir or not path.is_file():
            raise ValueError("Archive inputs must be regular files directly inside workdir")
    hashes = {path.name: sha256(path) for path in paths}
    archive_root = workdir / ".mapdl-archive"
    if archive_root.is_symlink() or archive_root.resolve().parent != workdir:
        raise ValueError("Archive root must stay inside workdir")
    archive = archive_root / (phase + "-" + uuid.uuid4().hex)
    archive.mkdir(parents=True, exist_ok=False)
    for path in paths:
        shutil.copy2(path, archive / path.name)
        if sha256(archive / path.name) != hashes[path.name] or sha256(path) != hashes[path.name]:
            raise RuntimeError("Output changed or archive verification failed; originals retained")
    # Separate manifest directory prevents collision with an arbitrary solver file.
    metadata = archive / "_archive_metadata"
    metadata.mkdir()
    record = {"directory": archive.relative_to(workdir).as_posix(),
              "files_sha256": hashes, "retired": [], "phase": phase,
              "preserved": sorted(preserve)}
    manifest = metadata / "manifest.json"
    manifest.write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        for path in paths:
            if path.name not in preserve:
                path.unlink()
                record["retired"].append(path.name)
    except OSError:
        for name in record["retired"]:
            shutil.copy2(archive / name, workdir / name)
            if sha256(workdir / name) != hashes[name]:
                raise RuntimeError(f"Restore failed; verified archive remains at {archive}")
        raise
    manifest.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def _rotate_outputs(workdir, deck, jobname, restart):
    candidates = set()
    for pat in STALE_PATTERNS + (f"{jobname}*",):
        candidates.update(workdir.glob(pat))
    preserved = []
    files = []
    for p in sorted(candidates):
        # A multiframe restart needs the existing solver state, including the result file.
        # Clear stale extracted tables/logs, but never erase its binary checkpoint.
        checkpoint = restart and p.name.startswith(jobname + ".") and p.suffix.lower() not in {
            ".txt", ".out", ".err", ".log", ".npz"
        }
        if p.is_file() and p.name != deck:
            files.append(p)
            if checkpoint:
                preserved.append(p.name)
    return _archive_files(workdir, files, phase="previous", preserve=preserved)


def clear_outputs(workdir: Path, deck: str = "case.inp", jobname: str = "case", *, restart: bool = False) -> list[str]:
    """Compatibility API: retire stale files to a verified, retained archive."""
    workdir = _validate_directory_names(workdir, deck, jobname)
    return _rotate_outputs(workdir, deck, jobname, restart)["retired"]


def run_deck(workdir: Path, deck: str = "case.inp", jobname: str = "case", n_proc: int = 4,
             timeout_s: float | None = None, keep_scratch: bool = False, *, restart: bool = False) -> dict:
    """Run ``MAPDL -b -np n -i deck -o case.out -j jobname`` in ``workdir``; return the receipt dict.

    Validate all inputs before archiving previous outputs. For ``restart=True``, require and preserve
    multiframe checkpoint files; this never turns a missing checkpoint into a fresh solve.
    Use ``keep_scratch=True`` when a subsequent process will continue this run.
    """
    workdir = _validate_directory_names(workdir, deck, jobname)
    exe = mapdl_exe()
    deck_path = workdir / deck
    if not deck_path.is_file() or deck_path.is_symlink():
        raise FileNotFoundError("Input deck must be a regular file inside workdir")
    input_hash = sha256(deck_path)
    if isinstance(n_proc, bool) or not isinstance(n_proc, Integral) or n_proc <= 0:
        raise ValueError("n_proc must be a positive integer")
    if timeout_s is not None and (isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError("timeout_s must be positive and finite")
    checkpoint_hashes = {}
    if restart:
        required = [workdir / f"{jobname}.rdb", workdir / f"{jobname}.ldhi"]
        frames = [p for p in workdir.glob(f"{jobname}.*") if re.fullmatch(r"\.r\d{3}", p.suffix.lower())]
        if not all(p.is_file() and not p.is_symlink() and p.stat().st_size > 0 for p in required + frames) or not frames:
            raise FileNotFoundError("multiframe restart requires .rdb, .ldhi and .rNNN checkpoint files")
        checkpoint_hashes = {p.name: sha256(p) for p in required + sorted(frames)}
    rotation = _rotate_outputs(workdir, deck, jobname, restart)
    cmd = [exe, "-b", "-np", str(n_proc), "-i", deck, "-o", f"{jobname}.out", "-j", jobname]
    t0 = time.time()
    with open(workdir / "run.log", "w", encoding="utf-8", errors="replace") as log:
        try:
            rc = subprocess.call(cmd, cwd=workdir, stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            rc = -9
    dt = time.time() - t0
    scratch_rotation = {"directory": None, "files_sha256": {}, "retired": []}
    if not keep_scratch:
        scratch = [p for p in workdir.glob(f"{jobname}*") if p.is_file() and p.name != deck
                   and (p.suffix.lower() in {s.lower() for s in SCRATCH_SUFFIXES}
                        or re.fullmatch(r"\.r\d{3}", p.suffix.lower()))]
        scratch_rotation = _archive_files(workdir, scratch, phase="scratch")
    out_path = workdir / f"{jobname}.out"
    out_text = out_path.read_text(errors="ignore") if out_path.exists() else ""
    results = {p.name: sha256(p) for p in sorted(workdir.glob("*.txt"))}
    return {
        "command": "MAPDL " + " ".join(cmd[1:]),
        "input_sha256": input_hash,
        "exit_code": rc,
        "runtime_s": round(dt, 2),
        "n_errors": count_errors(out_text),
        "not_converged": ("SOLUTION NOT CONVERGED" in out_text) or ("unconverged" in out_text.lower()),
        "stale_outputs_removed": rotation["retired"],  # Retired from active directory, never erased.
        "previous_output_archive": rotation,
        "scratch_archive": scratch_rotation,
        "restart": restart,
        "checkpoint_input_sha256": checkpoint_hashes,
        "result_files_sha256": results,
    }
