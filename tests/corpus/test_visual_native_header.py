"""Compile the real native lifecycle fence; no llama/model/image is built."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_original_native_lifecycle_header_compiles_and_fences(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("C++17 compiler not installed (NOT_RUN)")
    root = Path(__file__).resolve().parents[2]
    binary = tmp_path / ("lifetime.exe" if sys.platform == "win32" else "lifetime")
    result = subprocess.run([compiler, "-std=c++17", "-pthread", "-O0", "-Wall", "-Wextra", "-Werror",
        "-I", str(root / "infra/edge/llama-server"), str(root / "tests/native/test_loaded_lifetime.cpp"),
        "-o", str(binary)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-4000:]
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr[-4000:]
