from __future__ import annotations

import csv
import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest

from scripts.prepare_gate_a0 import GateA0Error, compare_inventories, inspect_zip, verify_manifest


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_test_directory(name: str) -> Path:
    directory = Path("work") / "tests" / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory



def test_zip_traversal_is_rejected() -> None:
    tmp_path = local_test_directory("zip_traversal")
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escape.txt", "forbidden")
    with pytest.raises(GateA0Error, match="Unsafe relative path"):
        inspect_zip(archive, "expected")


def test_manifest_detects_content_mismatch() -> None:
    tmp_path = local_test_directory("manifest_mismatch")
    data = tmp_path / "data"
    data.mkdir()
    target = data / "sample.txt"
    target.write_text("trusted", encoding="utf-8")
    manifest = data / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("relative_path", "bytes", "sha256"))
        writer.writeheader()
        writer.writerow(
            {
                "relative_path": "sample.txt",
                "bytes": target.stat().st_size,
                "sha256": file_hash(target),
            }
        )

    assert verify_manifest(data, manifest, "bytes")["failed"] == 0
    target.write_text("changed", encoding="utf-8")
    assert verify_manifest(data, manifest, "bytes")["failed"] == 1


def test_inventory_comparison_is_order_independent() -> None:
    first = [
        {"relative_path": "b.csv", "size_bytes": 2, "sha256": "b"},
        {"relative_path": "a.csv", "size_bytes": 1, "sha256": "a"},
    ]
    second = list(reversed(first))
    assert compare_inventories(first, second)["identical"] is True
