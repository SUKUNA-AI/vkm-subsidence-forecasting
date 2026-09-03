"""Fresh environment staging and durable evidence for Gate C1."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping

import pandas as pd

from .artifact_io import write_csv_atomic, write_json_atomic, write_text_atomic
from .data_contracts import ContractViolation, sha256_file
from .gate_c1_interfaces import canonical_json_sha256


def stage_fresh_environment(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    environment_path = (root / config["environment"]["path"]).resolve()
    expected_parent = (root / "work" / "environments").resolve()
    if environment_path.parent != expected_parent or environment_path.name != "gate_c_torch":
        raise ContractViolation("Refusing to stage Gate C1 outside work/environments/gate_c_torch")
    if environment_path.exists():
        shutil.rmtree(environment_path)
    work_evidence = root / "work" / "gate_c1" / "environment_staging"
    if work_evidence.exists():
        shutil.rmtree(work_evidence)
    work_evidence.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, "-m", "venv", str(environment_path)], root)
    python = environment_path / "Scripts" / "python.exe"
    install_report_path = work_evidence / "pip_install_report.raw.json"
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--requirement",
            str(root / config["environment"]["lock"]),
            "--extra-index-url",
            str(config["environment"]["torch_wheel_index"]),
            "--report",
            str(install_report_path),
        ],
        root,
    )
    freeze = _run_capture([str(python), "-m", "pip", "freeze", "--all"], root)
    sanitized_freeze = _sanitize_freeze(freeze, root)
    raw_install = json.loads(install_report_path.read_text(encoding="utf-8"))
    sanitized_install = _sanitize_install_report(raw_install, root)
    downloaded_hashes = _download_unhashed_wheels(
        python, sanitized_install, work_evidence, config, root
    )
    wheel_manifest = _wheel_manifest(sanitized_install, root, config, downloaded_hashes)
    smoke_output = work_evidence / "smoke_outputs"
    smoke_output.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(python),
            str(root / "scripts" / "run_gate_c1_environment_smoke.py"),
            "--output-root",
            str(smoke_output),
        ],
        root,
    )
    durable = root / config["artifacts"]["environment_root"]
    write_text_atomic(
        root, durable / "pip_freeze.txt", sanitized_freeze, work_scope="gate_c1_environment"
    )
    write_json_atomic(
        root,
        durable / "pip_install_report.json",
        sanitized_install,
        work_scope="gate_c1_environment",
    )
    write_csv_atomic(
        root, durable / "wheel_manifest.csv", wheel_manifest, work_scope="gate_c1_environment"
    )
    for name in ("hardware_report.json", "smoke_report.json", "determinism_report.json"):
        payload = json.loads((smoke_output / name).read_text(encoding="utf-8"))
        write_json_atomic(root, durable / name, payload, work_scope="gate_c1_environment")
    manifest = {
        "schema_version": 1,
        "status": "PASS",
        "environment_id": "gate_c_torch",
        "fresh_environment_created": True,
        "authority_path": "work/environments/gate_c_torch",
        "lock_path": config["environment"]["lock"],
        "lock_sha256": sha256_file(root / config["environment"]["lock"]),
        "pip_freeze_sha256": sha256_file(durable / "pip_freeze.txt"),
        "pip_install_report_sha256": sha256_file(durable / "pip_install_report.json"),
        "wheel_manifest_sha256": sha256_file(durable / "wheel_manifest.csv"),
        "hardware_report_sha256": sha256_file(durable / "hardware_report.json"),
        "smoke_report_sha256": sha256_file(durable / "smoke_report.json"),
        "determinism_report_sha256": sha256_file(durable / "determinism_report.json"),
        "network_scope": "environment_staging_only",
        "runtime_network_allowed": False,
        "external_pretrained_models": False,
        "absolute_paths_persisted": False,
    }
    manifest["environment_manifest_content_sha256"] = canonical_json_sha256(manifest)
    write_json_atomic(
        root, durable / "environment_manifest.json", manifest, work_scope="gate_c1_environment"
    )
    return {
        "status": "PASS",
        "environment_id": "gate_c_torch",
        "packages": len(sanitized_freeze.splitlines()),
        "wheel_records": len(wheel_manifest),
        "smoke_status": "PASS",
        "determinism_status": "PASS",
    }


def refresh_environment_evidence(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    environment_path = root / config["environment"]["path"]
    python = environment_path / "Scripts" / "python.exe"
    if not python.is_file():
        raise FileNotFoundError(python)
    smoke_output = root / "work" / "gate_c1" / "environment_refresh"
    smoke_output.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(python),
            str(root / "scripts" / "run_gate_c1_environment_smoke.py"),
            "--output-root",
            str(smoke_output),
        ],
        root,
    )
    durable = root / config["artifacts"]["environment_root"]
    manifest_path = durable / "environment_manifest.json"
    required_invariant = (
        durable / "pip_freeze.txt",
        durable / "pip_install_report.json",
        durable / "wheel_manifest.csv",
        manifest_path,
    )
    missing = [path.relative_to(root).as_posix() for path in required_invariant if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Cannot refresh Gate C1 evidence without the existing fresh locked environment records: "
            + ", ".join(missing)
        )
    for name in ("hardware_report.json", "smoke_report.json", "determinism_report.json"):
        write_json_atomic(
            root,
            durable / name,
            json.loads((smoke_output / name).read_text(encoding="utf-8")),
            work_scope="gate_c1_environment",
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "hardware_report_sha256": sha256_file(durable / "hardware_report.json"),
            "smoke_report_sha256": sha256_file(durable / "smoke_report.json"),
            "determinism_report_sha256": sha256_file(durable / "determinism_report.json"),
            "evidence_refreshed_after_code_revision": True,
        }
    )
    manifest.pop("environment_manifest_content_sha256", None)
    manifest["environment_manifest_content_sha256"] = canonical_json_sha256(manifest)
    write_json_atomic(root, manifest_path, manifest, work_scope="gate_c1_environment")
    return {"status": "PASS", "environment_id": "gate_c_torch", "evidence_refreshed": True}


def _run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Environment staging command failed ({completed.returncode}): {command[:4]}")


def _run_capture(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "pip freeze failed")
    return completed.stdout


def _sanitize_freeze(value: str, root: Path) -> str:
    normalized_root = root.as_posix().lower()
    lines = []
    for line in value.replace("\\", "/").splitlines():
        lowered = line.lower()
        if normalized_root in lowered and (line.startswith("-e ") or " @ file:" in line):
            lines.append("-e .")
        else:
            lines.append(line)
    result = "\n".join(lines).strip() + "\n"
    if normalized_root in result.lower():
        raise ContractViolation("Absolute repository path remained in durable pip freeze")
    return result


def _sanitize_install_report(payload: Mapping[str, Any], root: Path) -> dict[str, Any]:
    root_variants = {str(root), root.as_posix(), str(root).replace("\\", "/")}

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): sanitize(child) for key, child in value.items()}
        if isinstance(value, list):
            return [sanitize(child) for child in value]
        if isinstance(value, str):
            result = value
            for variant in root_variants:
                result = result.replace(variant, ".")
                result = result.replace("file:///" + variant.replace("\\", "/"), "repository://.")
            if result.startswith("file:///") and "skru" in result.lower():
                return "repository://."
            return result
        return value

    result = sanitize(dict(payload))
    for record in result.get("install", []):
        download = record.get("download_info", {})
        if download.get("dir_info", {}).get("editable") is True:
            download["url"] = "repository://."
    serialized = json.dumps(result, ensure_ascii=False)
    if str(root).lower() in serialized.lower() or root.as_posix().lower() in serialized.lower():
        raise ContractViolation("Absolute repository path remained in pip install report")
    return result


def _wheel_manifest(
    report: Mapping[str, Any],
    root: Path,
    config: Mapping[str, Any],
    downloaded_hashes: Mapping[tuple[str, str], Mapping[str, str]],
) -> pd.DataFrame:
    rows = []
    for record in report.get("install", []):
        metadata = record.get("metadata", {})
        download = record.get("download_info", {})
        archive = download.get("archive_info", {})
        hashes = archive.get("hashes", {})
        url = str(download.get("url", ""))
        editable = bool(download.get("dir_info", {}).get("editable") or "repository://" in url)
        if url.startswith("file:") or url == "repository://.":
            url = "repository://."
            sha_value = sha256_file(root / "pyproject.toml")
        else:
            sha_value = str(hashes.get("sha256", ""))
            if len(sha_value) != 64:
                fallback = downloaded_hashes.get(
                    (_normalize_package_name(str(metadata.get("name", ""))), str(metadata.get("version", "")))
                )
                if fallback is not None:
                    sha_value = str(fallback["sha256"])
        rows.append(
            {
                "package": str(metadata.get("name", "")),
                "version": str(metadata.get("version", "")),
                "source_url": url,
                "sha256": sha_value,
                "editable_repository_source": editable,
                "locally_verified_wheel": bool(
                    (_normalize_package_name(str(metadata.get("name", ""))), str(metadata.get("version", "")))
                    in downloaded_hashes
                ),
            }
        )
    frame = pd.DataFrame(rows).sort_values(["package", "version"], kind="mergesort").reset_index(drop=True)
    external = frame.loc[~frame["source_url"].eq("repository://.")]
    if external["sha256"].astype(str).str.len().ne(64).any():
        raise ContractViolation("Gate C1 wheel manifest lacks exact SHA-256")
    return frame


def _download_unhashed_wheels(
    python: Path,
    report: Mapping[str, Any],
    work_evidence: Path,
    config: Mapping[str, Any],
    root: Path,
) -> dict[tuple[str, str], dict[str, str]]:
    requirements = []
    for record in report.get("install", []):
        download = record.get("download_info", {})
        if download.get("url") == "repository://.":
            continue
        archive = download.get("archive_info", {})
        if len(str(archive.get("hashes", {}).get("sha256", ""))) == 64:
            continue
        metadata = record.get("metadata", {})
        requirements.append(f"{metadata['name']}=={metadata['version']}")
    if not requirements:
        return {}
    wheelhouse = work_evidence / "hash_verification_wheels"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "download",
            "--no-deps",
            "--dest",
            str(wheelhouse),
            "--extra-index-url",
            str(config["environment"]["torch_wheel_index"]),
            *requirements,
        ],
        root,
    )
    result: dict[tuple[str, str], dict[str, str]] = {}
    for record in report.get("install", []):
        metadata = record.get("metadata", {})
        key = (
            _normalize_package_name(str(metadata.get("name", ""))),
            str(metadata.get("version", "")),
        )
        if f"{metadata.get('name')}=={metadata.get('version')}" not in requirements:
            continue
        prefix = key[0].replace("-", "_") + "-" + key[1].replace("+", "+")
        candidates = [
            path
            for path in wheelhouse.iterdir()
            if path.is_file()
            and _normalize_package_name(path.name.split("-")[0]) == key[0]
            and key[1].split("+")[0] in path.name
        ]
        if len(candidates) != 1:
            raise ContractViolation(f"Cannot identify exact downloaded wheel for {key}: {candidates}")
        result[key] = {
            "filename": candidates[0].name,
            "sha256": sha256_file(candidates[0]),
        }
    return result


def _normalize_package_name(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(".", "-")
