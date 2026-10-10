"""Original native allocation metadata guard; no model or CUDA execution."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_native_weight_placement_header_compiles_and_rejects_invalid_metadata(tmp_path):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("C++17 compiler not installed (NOT_RUN)")
    root = Path(__file__).resolve().parents[2]
    binary = tmp_path / ("placement.exe" if sys.platform == "win32" else "placement")
    result = subprocess.run([compiler,"-std=c++17","-O0","-Wall","-Wextra","-Werror",
        "-I",str(root/"infra/edge/llama-server"),str(root/"tests/native/test_weight_placement.cpp"),
        "-o",str(binary)],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr[-4000:]
    result = subprocess.run([str(binary)],capture_output=True,text=True,timeout=5)
    assert result.returncode == 0,result.stderr[-4000:]
