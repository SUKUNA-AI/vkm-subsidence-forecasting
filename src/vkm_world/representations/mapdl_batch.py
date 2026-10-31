"""Thin batch adapter for Ansys MAPDL (solver as adapter, CLAUDE.md ladder): run an input deck, keep a receipt.

The executable is taken from ``VKM_MAPDL_EXE`` (no machine paths in tracked files). Each run leaves a receipt with
the input SHA-256, the command, exit code, runtime, the number of errors reported in the output and the SHA-256 of
every result text file it produced.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
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
    return exe


def count_errors(out_text: str) -> int | None:
    for line in out_text.splitlines():
        if "NUMBER OF ERROR   MESSAGES ENCOUNTERED" in line:
            return int(line.split("=")[-1])
    return None


STALE_PATTERNS = ("*.txt", "*.npz", "receipt.json", "run.log")


def clear_outputs(workdir: Path, deck: str = "case.inp", jobname: str = "case", *, restart: bool = False) -> list[str]:
    """Remove results of any previous run in ``workdir`` (solver text tables, derived .npz, receipt, job files),
    keeping only the input deck, so that a failed or shorter rerun can never be read as the new result."""
    workdir = Path(workdir)
    removed = []
    candidates = set()
    for pat in STALE_PATTERNS + (f"{jobname}*",):
        candidates.update(workdir.glob(pat))
    for p in sorted(candidates):
        # A multiframe restart needs the existing solver state, including the result file.
        # Clear stale extracted tables/logs, but never erase its binary checkpoint.
        checkpoint = restart and p.name.startswith(jobname + ".") and p.suffix.lower() not in {
            ".txt", ".out", ".err", ".log", ".npz"
        }
        if p.is_file() and p.name != deck and not checkpoint:
            p.unlink()
            removed.append(p.name)
    return removed


def run_deck(workdir: Path, deck: str = "case.inp", jobname: str = "case", n_proc: int = 4,
             timeout_s: float | None = None, keep_scratch: bool = False, *, restart: bool = False) -> dict:
    """Run ``MAPDL -b -np n -i deck -o case.out -j jobname`` in ``workdir``; return the receipt dict.

    Outputs of a previous run are removed first. For ``restart=True``, require and preserve
    multiframe checkpoint files; this never turns a missing checkpoint into a fresh solve.
    Use ``keep_scratch=True`` when a subsequent process will continue this run.
    """
    workdir = Path(workdir)
    checkpoint_hashes = {}
    if restart:
        required = [workdir / f"{jobname}.rdb", workdir / f"{jobname}.ldhi"]
        frames = [p for p in workdir.glob(f"{jobname}.*") if re.fullmatch(r"\.r\d{3}", p.suffix.lower())]
        if not all(p.is_file() for p in required) or not frames:
            raise FileNotFoundError("multiframe restart requires .rdb, .ldhi and .rNNN checkpoint files")
        checkpoint_hashes = {p.name: sha256(p) for p in required + sorted(frames)}
    stale = clear_outputs(workdir, deck, jobname, restart=restart)
    cmd = [mapdl_exe(), "-b", "-np", str(n_proc), "-i", deck, "-o", f"{jobname}.out", "-j", jobname]
    t0 = time.time()
    with open(workdir / "run.log", "w", encoding="utf-8", errors="replace") as log:
        try:
            rc = subprocess.call(cmd, cwd=workdir, stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            rc = -9
    dt = time.time() - t0
    if not keep_scratch:
        for p in workdir.glob(f"{jobname}*"):
            if p.suffix in SCRATCH_SUFFIXES or p.suffix.startswith(".r0"):
                p.unlink(missing_ok=True)
    out_path = workdir / f"{jobname}.out"
    out_text = out_path.read_text(errors="ignore") if out_path.exists() else ""
    results = {p.name: sha256(p) for p in sorted(workdir.glob("*.txt"))}
    return {
        "command": "MAPDL " + " ".join(cmd[1:]),
        "input_sha256": sha256(workdir / deck),
        "exit_code": rc,
        "runtime_s": round(dt, 2),
        "n_errors": count_errors(out_text),
        "not_converged": ("SOLUTION NOT CONVERGED" in out_text) or ("unconverged" in out_text.lower()),
        "stale_outputs_removed": stale,
        "restart": restart,
        "checkpoint_input_sha256": checkpoint_hashes,
        "result_files_sha256": results,
    }
