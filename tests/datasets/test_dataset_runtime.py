"""Vendor-runtime tests are opt-in; a missing runtime is NOT_RUN, never a fabricated PASS."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.qgis_runtime
def test_native_runtime_rehearsal(tmp_path):
    executable = os.environ.get("VKM_DATASETS_GDAL_PYTHON")
    if not executable:
        pytest.skip("NOT_RUN: configure existing VKM_DATASETS_GDAL_PYTHON runtime")
    script = Path(__file__).with_name("runtime_synthetic.py")
    process = subprocess.run([executable, "-B", str(script), str(tmp_path / "native-rehearsal")],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert process.returncode == 0, process.stdout + process.stderr
