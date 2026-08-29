#!/usr/bin/env python3
"""Capture a portable, machine-readable snapshot of the active environment."""

from __future__ import annotations

import argparse
import ctypes
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_NAMES = (
    "numpy",
    "pandas",
    "geopandas",
    "shapely",
    "scipy",
    "opencv-python-headless",
    "Pillow",
    "pyogrio",
    "matplotlib",
    "PyYAML",
    "pytest",
    "nbformat",
    "nbclient",
    "ipykernel",
    "torch",
    "torchvision",
    "scikit-learn",
    "lightgbm",
    "xgboost",
)


def run_command(command: list[str], cwd: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": str(exc)}
    return {
        "available": True,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def total_memory_bytes() -> int | None:
    if sys.platform != "win32":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.total_physical)


def package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in PACKAGE_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def powershell_json(script: str, cwd: Path) -> Any:
    result = run_command(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd,
    )
    if result.get("returncode") != 0 or not result.get("stdout"):
        return {"query": result}
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {"raw": result["stdout"], "query": result}


def torch_snapshot() -> dict[str, Any]:
    try:
        import torch
    except Exception as exc:  # pragma: no cover - depends on local environment
        return {"installed": False, "error": f"{type(exc).__name__}: {exc}"}

    result: dict[str, Any] = {
        "installed": True,
        "version": torch.__version__,
        "compiled_cuda": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
    }
    if torch.cuda.is_available():
        result["devices"] = [
            {
                "index": index,
                "name": torch.cuda.get_device_name(index),
                "capability": list(torch.cuda.get_device_capability(index)),
                "total_memory_bytes": torch.cuda.get_device_properties(index).total_memory,
            }
            for index in range(torch.cuda.device_count())
        ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    out_dir = root / "artifacts" / "environment"
    out_dir.mkdir(parents=True, exist_ok=True)

    pip_freeze = run_command([sys.executable, "-m", "pip", "freeze", "--all"], root)
    freeze_text = pip_freeze.get("stdout", "") if pip_freeze.get("returncode") == 0 else ""
    (out_dir / "pip_freeze.txt").write_text(freeze_text + ("\n" if freeze_text else ""), encoding="utf-8")

    nvidia = run_command(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total,compute_cap",
            "--format=csv,noheader,nounits",
        ],
        root,
    )
    nvidia_header = run_command(["nvidia-smi"], root)
    cuda_match = re.search(r"CUDA(?: UMD)? Version:\s*([0-9.]+)", nvidia_header.get("stdout", ""))
    cpu_info = powershell_json(
        "Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress",
        root,
    )
    disk_info = powershell_json(
        "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,Size | ConvertTo-Json -Compress",
        root,
    )
    git_status = run_command(["git", "status", "--short"], root)
    short_status = git_status.get("stdout", "") if git_status.get("returncode") == 0 else ""

    report = {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository_root": ".",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "architecture": platform.architecture()[0],
        },
        "hardware": {
            "logical_cpu_count": os.cpu_count(),
            "cpu": cpu_info,
            "total_memory_bytes": total_memory_bytes(),
            "nvidia_smi": nvidia,
            "nvidia_driver_cuda_compatibility": cuda_match.group(1) if cuda_match else None,
            "physical_disks": disk_info,
        },
        "packages": package_versions(),
        "torch": torch_snapshot(),
        "git": {
            "version": run_command(["git", "--version"], root),
            "branch": run_command(["git", "branch", "--show-current"], root),
            "is_dirty": bool(short_status),
            "status_entry_count": len(short_status.splitlines()) if short_status else 0,
        },
        "pip_freeze": {
            "path": "artifacts/environment/pip_freeze.txt",
            "captured": bool(freeze_text),
            "command": pip_freeze,
        },
    }
    output = out_dir / "environment.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"environment": output.relative_to(root).as_posix(), "python": report["python"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
