#!/usr/bin/env python3
"""Build two independently extracted and verified Gate A0 working trees."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MAX_MEMBER_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 20 * 1024**3


class GateA0Error(RuntimeError):
    """Raised when an input violates integrity or extraction policy."""


@dataclass(frozen=True)
class ArchiveSpec:
    archive_id: str
    zip_path: str
    expected_top_level: str
    internal_manifest: str
    manifest_size_column: str


ARCHIVES = (
    ArchiveSpec(
        archive_id="reconstruction_v3_2",
        zip_path="inputs/bootstrap/SKRU1_data_reconstruction_v3_2.zip",
        expected_top_level="SKRU1_data_reconstruction_v3_2",
        internal_manifest="metadata/dataset_manifest.csv",
        manifest_size_column="bytes",
    ),
    ArchiveSpec(
        archive_id="eda_targets_v1",
        zip_path="inputs/bootstrap/SKRU1_v3_2_EDA_targets_v1.zip",
        expected_top_level="SKRU1_v3_2_EDA_targets_v1",
        internal_manifest="metadata/artifact_manifest.csv",
        manifest_size_column="bytes",
    ),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(raw: str) -> PurePosixPath:
    if not raw or "\x00" in raw or "\\" in raw or re.match(r"^[A-Za-z]:", raw):
        raise GateA0Error(f"Unsafe relative path: {raw!r}")
    path = PurePosixPath(raw)
    unsafe_part = any(
        part in {"", ".", ".."} or ":" in part or part.endswith((" ", "."))
        for part in path.parts
    )
    if path.is_absolute() or unsafe_part:
        raise GateA0Error(f"Unsafe relative path: {raw!r}")
    return path


def inspect_zip(zip_path: Path, expected_top_level: str) -> dict[str, Any]:
    seen: set[str] = set()
    top_levels: set[str] = set()
    total_uncompressed = 0
    file_count = 0
    with zipfile.ZipFile(zip_path) as archive:
        bad_crc = archive.testzip()
        if bad_crc is not None:
            raise GateA0Error(f"CRC failure in {zip_path.name}: {bad_crc}")
        for info in archive.infolist():
            path = safe_relative_path(info.filename.rstrip("/"))
            key = path.as_posix().casefold()
            if key in seen:
                raise GateA0Error(f"Duplicate ZIP member after normalization: {info.filename}")
            seen.add(key)
            top_levels.add(path.parts[0])
            unix_type = (info.external_attr >> 16) & 0o170000
            if stat.S_ISLNK(unix_type):
                raise GateA0Error(f"Symbolic links are forbidden in ZIP: {info.filename}")
            if info.flag_bits & 0x1:
                raise GateA0Error(f"Encrypted ZIP members are forbidden: {info.filename}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise GateA0Error(f"ZIP member exceeds size policy: {info.filename}")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_TOTAL_BYTES:
                raise GateA0Error(f"ZIP exceeds total extraction size policy: {zip_path.name}")
            if not info.is_dir():
                file_count += 1

    if top_levels != {expected_top_level}:
        raise GateA0Error(
            f"Unexpected top-level entries in {zip_path.name}: {sorted(top_levels)!r}; "
            f"expected {expected_top_level!r}"
        )
    return {
        "archive": zip_path.name,
        "member_count": len(seen),
        "file_count": file_count,
        "total_uncompressed_bytes": total_uncompressed,
        "top_level": expected_top_level,
    }


def assert_inside(target: Path, allowed_root: Path) -> None:
    resolved = target.resolve()
    allowed = allowed_root.resolve()
    if resolved == allowed or allowed not in resolved.parents:
        raise GateA0Error(f"Refusing operation outside {allowed}: {resolved}")


def extract_archive(zip_path: Path, destination: Path, expected_top_level: str, work_root: Path) -> dict[str, Any]:
    inspection = inspect_zip(zip_path, expected_top_level)
    assert_inside(destination, work_root)
    if destination.exists():
        raise GateA0Error(f"Extraction destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.extracting-{uuid.uuid4().hex}"
    assert_inside(temporary, work_root)
    temporary.mkdir(parents=False)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(temporary)
        extracted_top = temporary / expected_top_level
        if not extracted_top.is_dir():
            raise GateA0Error(f"Expected extracted directory is missing: {expected_top_level}")
        os.replace(extracted_top, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return inspection


def inventory_tree(tree: Path) -> list[dict[str, Any]]:
    return [
        {
            "relative_path": path.relative_to(tree).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in sorted((item for item in tree.rglob("*") if item.is_file()), key=lambda item: item.as_posix())
    ]


def write_inventory(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("relative_path", "size_bytes", "sha256"), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def verify_manifest(
    base: Path,
    manifest_path: Path,
    size_column: str,
    expected_path_column: str = "relative_path",
    include_unmanifested: bool = True,
) -> dict[str, Any]:
    rows = load_manifest(manifest_path)
    results: list[dict[str, Any]] = []
    for record in rows:
        relative = safe_relative_path(record[expected_path_column])
        target = base.joinpath(*relative.parts)
        exists = target.is_file()
        expected_size = int(record[size_column])
        expected_hash = record["sha256"].lower()
        actual_size = target.stat().st_size if exists else None
        actual_hash = sha256(target) if exists else None
        passed = exists and actual_size == expected_size and actual_hash == expected_hash
        results.append(
            {
                "relative_path": relative.as_posix(),
                "expected_size": expected_size,
                "actual_size": actual_size,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "status": "PASS" if passed else "FAIL",
            }
        )
    failed = [row for row in results if row["status"] != "PASS"]
    manifest_paths = {row["relative_path"] for row in results}
    actual_paths = {row["relative_path"] for row in inventory_tree(base)} if include_unmanifested else set()
    return {
        "manifest": manifest_path.relative_to(base).as_posix() if base in manifest_path.parents else manifest_path.name,
        "checked": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "unmanifested_files": sorted(actual_paths - manifest_paths) if include_unmanifested else [],
        "results": results,
    }


def verify_outer_manifests(root: Path) -> dict[str, Any]:
    combined: list[dict[str, Any]] = []
    for relative_manifest in ("configs/input_manifest.csv", "configs/source_manifest.csv"):
        report = verify_manifest(root, root / relative_manifest, "size_bytes", include_unmanifested=False)
        for result in report["results"]:
            result["source_manifest"] = relative_manifest
        combined.extend(report["results"])
    failed = [row for row in combined if row["status"] != "PASS"]
    return {
        "checked": len(combined),
        "passed": len(combined) - len(failed),
        "failed": len(failed),
        "results": combined,
    }


def compare_inventories(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    left_map = {row["relative_path"]: (row["size_bytes"], row["sha256"]) for row in left}
    right_map = {row["relative_path"]: (row["size_bytes"], row["sha256"]) for row in right}
    differing = sorted(path for path in left_map.keys() & right_map.keys() if left_map[path] != right_map[path])
    missing_left = sorted(right_map.keys() - left_map.keys())
    missing_right = sorted(left_map.keys() - right_map.keys())
    return {
        "identical": not differing and not missing_left and not missing_right,
        "left_file_count": len(left_map),
        "right_file_count": len(right_map),
        "content_mismatches": differing,
        "missing_from_left": missing_left,
        "missing_from_right": missing_right,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown_report(root: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Gate A0 — проверка входов и воспроизводимая распаковка",
        "",
        f"Дата фиксации (UTC): `{report['generated_at_utc']}`.",
        "",
        f"Итог: **{'PASS' if report['status'] == 'PASS' else 'FAIL'}**.",
        "",
        "## Контрольные результаты",
        "",
        f"- Внешние manifests: {report['outer_manifests']['passed']}/{report['outer_manifests']['checked']} PASS.",
    ]
    for run_name, run_report in report["runs"].items():
        lines.append(f"- `{run_name}`: подготовлено архивов — {len(run_report['archives'])}; выходные каталоги чистые.")
        for archive_id, archive in run_report["archives"].items():
            manifest = archive["internal_manifest"]
            lines.append(
                f"  - `{archive_id}`: inventory {archive['inventory_file_count']} файлов; "
                f"внутренний manifest {manifest['passed']}/{manifest['checked']} PASS."
            )
    for archive_id, comparison in report["comparisons"].items():
        lines.append(
            f"- Сравнение независимых распаковок `{archive_id}`: "
            f"{'совпадает' if comparison['identical'] else 'НЕ совпадает'} "
            f"({comparison['left_file_count']} / {comparison['right_file_count']} файлов)."
        )
    lines.extend(
        [
            "",
            "## Подготовленные локальные каталоги",
            "",
            "- `work/run_01`: первая независимая распаковка и пустые каталоги результатов.",
            "- `work/run_02`: вторая независимая распаковка и пустые каталоги результатов.",
            "- В каждом запуске: `source/`, `eda/`, `snapshot/`, `experiments/`, `audit/`, `logs/`.",
            "",
            "Каталог `work/` намеренно исключён из Git. Проверяемые inventories и JSON-отчёт сохранены в `artifacts/`.",
            "",
            "## Интерпретация",
            "",
            "Gate A0 подтверждает целостность, безопасную структуру архивов и файловую воспроизводимость двух распаковок. Он не является оценкой статистической пригодности данных или качества будущих моделей.",
            "",
        ]
    )
    report_path = root / "docs" / "reports" / "GATE_A0_REPORT_RU.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--replace", action="store_true", help="replace only work/run_01 and work/run_02")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    work_root = root / "work"
    work_root.mkdir(parents=True, exist_ok=True)

    outer = verify_outer_manifests(root)
    if outer["failed"]:
        raise GateA0Error(f"Outer input verification failed for {outer['failed']} files")

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PENDING",
        "outer_manifests": outer,
        "runs": {},
        "comparisons": {},
    }
    inventories: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for run_name in ("run_01", "run_02"):
        run_root = work_root / run_name
        assert_inside(run_root, work_root)
        if run_root.exists():
            if not args.replace:
                raise GateA0Error(f"{run_root} already exists; use --replace for a controlled rebuild")
            shutil.rmtree(run_root)
        run_root.mkdir()
        run_report: dict[str, Any] = {"root": f"work/{run_name}", "archives": {}, "empty_output_dirs": []}

        for spec in ARCHIVES:
            group = "source" if spec.archive_id.startswith("reconstruction") else "eda"
            destination = run_root / group / spec.expected_top_level
            inspection = extract_archive(root / spec.zip_path, destination, spec.expected_top_level, work_root)
            rows = inventory_tree(destination)
            inventory_path = root / "artifacts" / "inventory" / f"{run_name}_{spec.archive_id}.csv"
            write_inventory(inventory_path, rows)
            internal = verify_manifest(destination, destination / spec.internal_manifest, spec.manifest_size_column)
            inventories[(run_name, spec.archive_id)] = rows
            run_report["archives"][spec.archive_id] = {
                "zip_path": spec.zip_path,
                "destination": destination.relative_to(root).as_posix(),
                "zip_inspection": inspection,
                "inventory": inventory_path.relative_to(root).as_posix(),
                "inventory_file_count": len(rows),
                "internal_manifest": internal,
            }
            if internal["failed"]:
                raise GateA0Error(f"Internal manifest failed: {run_name}/{spec.archive_id}")

        for directory in ("snapshot", "experiments", "audit", "logs"):
            target = run_root / directory
            target.mkdir()
            run_report["empty_output_dirs"].append(target.relative_to(root).as_posix())
        report["runs"][run_name] = run_report

    for spec in ARCHIVES:
        comparison = compare_inventories(
            inventories[("run_01", spec.archive_id)],
            inventories[("run_02", spec.archive_id)],
        )
        report["comparisons"][spec.archive_id] = comparison
        if not comparison["identical"]:
            raise GateA0Error(f"Independent extraction mismatch: {spec.archive_id}")

    report["status"] = "PASS"
    write_json(root / "artifacts" / "verification" / "gate_a0_report.json", report)
    write_json(
        root / "artifacts" / "status" / "gate_a0_status.json",
        {
            "gate": "A0",
            "status": report["status"],
            "generated_at_utc": report["generated_at_utc"],
            "run_directories": ["work/run_01", "work/run_02"],
            "comparison_passed": all(item["identical"] for item in report["comparisons"].values()),
        },
    )
    write_markdown_report(root, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "outer_checked": outer["checked"],
                "run_count": len(report["runs"]),
                "comparisons": {key: value["identical"] for key, value in report["comparisons"].items()},
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
