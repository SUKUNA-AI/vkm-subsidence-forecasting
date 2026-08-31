"""Reproducible staging helpers for the three isolated Gate B6 environments."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping
from urllib.parse import unquote

from .artifact_io import resolve_repo_path
from .b6_governance import effective_environment_settings
from .data_contracts import ContractViolation, sha256_file


def stage_environment(root: Path, config: Mapping[str, Any], environment_id: str) -> dict[str, Any]:
    if environment_id not in config["environments"]:
        raise KeyError(environment_id)
    settings = effective_environment_settings(root, config, environment_id)
    environment_root = (root / "work" / "environments" / environment_id).resolve()
    expected_parent = (root / "work" / "environments").resolve()
    if environment_root.parent != expected_parent:
        raise ContractViolation("Environment staging path escaped work/environments")
    python_path = environment_root / "Scripts" / "python.exe"
    staging_root = root / "work" / "environment_staging" / environment_id
    durable_root = (
        root
        / "artifacts"
        / "model_selection"
        / "t1_b6_expanded_v1"
        / "environments"
        / environment_id
    )
    staging_root.mkdir(parents=True, exist_ok=True)
    durable_root.mkdir(parents=True, exist_ok=True)
    commands: list[dict[str, Any]] = []
    if not python_path.is_file():
        commands.append(_run([sys.executable, "-m", "venv", str(environment_root)], root))
    if not python_path.is_file():
        raise RuntimeError(f"Failed to create environment: {environment_root}")
    commands.append(
        _run(
            [str(python_path), "-m", "pip", "install", "--upgrade", "pip==26.0.1"],
            root,
        )
    )
    lock_path = resolve_repo_path(root, settings["lock"])
    install_report = staging_root / "pip_install_report.json"
    install_command = [
        str(python_path),
        "-m",
        "pip",
        "install",
        "--report",
        str(install_report),
        "-r",
        str(lock_path),
    ]
    if settings.get("cuda_wheel_index"):
        install_command.extend(["--extra-index-url", str(settings["cuda_wheel_index"])])
    commands.append(_run(install_command, root, timeout=7200))
    wheel_report = staging_root / "pip_wheel_report.json"
    wheel_command = [
        str(python_path),
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--report",
        str(wheel_report),
        "-r",
        str(lock_path),
    ]
    if settings.get("cuda_wheel_index"):
        wheel_command.extend(["--extra-index-url", str(settings["cuda_wheel_index"])])
    commands.append(_run(wheel_command, root, timeout=7200))
    cache_capture = _run([str(python_path), "-m", "pip", "cache", "dir"], root)
    commands.append(cache_capture)
    pip_cache_root = Path(str(cache_capture.get("stdout", "")).strip())
    freeze = _run([str(python_path), "-m", "pip", "freeze", "--all"], root)
    commands.append(freeze)
    raw_freeze_path = staging_root / "pip_freeze.raw.txt"
    raw_freeze_path.write_text(str(freeze.get("stdout", "")) + "\n", encoding="utf-8", newline="\n")
    normalized = normalize_pip_freeze(str(freeze.get("stdout", "")), root)
    durable_freeze_path = durable_root / "pip_freeze.txt"
    durable_freeze_path.write_text(normalized, encoding="utf-8", newline="\n")
    durable_install_report = durable_root / "pip_install_report.json"
    if install_report.is_file():
        payload = json.loads(install_report.read_text(encoding="utf-8"))
        payload = enrich_report_hashes_from_pip_cache(payload, pip_cache_root)
        payload = sanitize_install_report(payload, root)
        durable_install_report.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    durable_wheel_report = durable_root / "pip_wheel_report.json"
    if wheel_report.is_file():
        payload = json.loads(wheel_report.read_text(encoding="utf-8"))
        payload = enrich_report_hashes_from_pip_cache(payload, pip_cache_root)
        payload = sanitize_install_report(payload, root)
        durable_wheel_report.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    smoke_command = [
        str(python_path),
        str(root / "scripts" / "run_b6_environment_smoke.py"),
        "--environment-id",
        environment_id,
    ]
    commands.append(_run(smoke_command, root, timeout=3600))
    return {
        "schema_version": 1,
        "environment_id": environment_id,
        "python_executable": python_path.relative_to(root).as_posix(),
        "lock_path": lock_path.relative_to(root).as_posix(),
        "lock_sha256": sha256_file(lock_path),
        "pip_freeze_path": durable_freeze_path.relative_to(root).as_posix(),
        "pip_freeze_sha256": sha256_file(durable_freeze_path),
        "install_report_path": durable_install_report.relative_to(root).as_posix()
        if durable_install_report.is_file()
        else None,
        "wheel_report_path": durable_wheel_report.relative_to(root).as_posix()
        if durable_wheel_report.is_file()
        else None,
        "commands": [
            {key: value for key, value in record.items() if key != "stdout"}
            for record in commands
        ],
        "status": "PASS" if all(record["returncode"] == 0 for record in commands) else "FAIL",
    }


def refresh_environment_evidence(
    root: Path, config: Mapping[str, Any], environment_id: str
) -> dict[str, Any]:
    """Rebuild durable, sanitized evidence from an already staged environment.

    This phase performs no dependency installation and makes no network
    request.  It is useful when evidence normalization changes while the exact
    environment and the raw pip reports in ``work/`` remain unchanged.
    """

    if environment_id not in config["environments"]:
        raise KeyError(environment_id)
    environment_root = (root / "work" / "environments" / environment_id).resolve()
    expected_parent = (root / "work" / "environments").resolve()
    if environment_root.parent != expected_parent:
        raise ContractViolation("Environment evidence path escaped work/environments")
    python_path = environment_root / "Scripts" / "python.exe"
    if not python_path.is_file():
        raise ContractViolation(f"Environment is not staged: {environment_id}")
    staging_root = root / "work" / "environment_staging" / environment_id
    durable_root = (
        root
        / "artifacts"
        / "model_selection"
        / "t1_b6_expanded_v1"
        / "environments"
        / environment_id
    )
    durable_root.mkdir(parents=True, exist_ok=True)
    cache_capture = _run([str(python_path), "-m", "pip", "cache", "dir"], root)
    pip_cache_root = Path(str(cache_capture.get("stdout", "")).strip())
    freeze = _run([str(python_path), "-m", "pip", "freeze", "--all"], root)
    normalized = normalize_pip_freeze(str(freeze.get("stdout", "")), root)
    durable_freeze_path = durable_root / "pip_freeze.txt"
    durable_freeze_path.write_text(normalized, encoding="utf-8", newline="\n")

    report_records: dict[str, Any] = {}
    for filename in ("pip_install_report.json", "pip_wheel_report.json"):
        raw_path = staging_root / filename
        if not raw_path.is_file():
            raise ContractViolation(f"Raw environment report is missing: {raw_path.relative_to(root)}")
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        payload = enrich_report_hashes_from_pip_cache(payload, pip_cache_root)
        payload = sanitize_install_report(payload, root)
        durable_path = durable_root / filename
        durable_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        report_records[filename] = {
            "path": durable_path.relative_to(root).as_posix(),
            "sha256": sha256_file(durable_path),
        }
    return {
        "schema_version": 1,
        "environment_id": environment_id,
        "mode": "offline_evidence_refresh",
        "python_executable": python_path.relative_to(root).as_posix(),
        "pip_freeze_path": durable_freeze_path.relative_to(root).as_posix(),
        "pip_freeze_sha256": sha256_file(durable_freeze_path),
        "reports": report_records,
        "network_accessed": False,
        "status": "PASS",
    }


def normalize_pip_freeze(text: str, root: Path) -> str:
    lines: list[str] = []
    root_variants = {str(root).lower(), root.as_posix().lower(), root.as_uri().lower()}
    project_written = False
    for line in text.splitlines():
        lowered = line.lower().replace("\\", "/")
        if "skru1-research" in lowered or "skru1_research" in lowered or any(
            value.replace("\\", "/") in lowered for value in root_variants
        ):
            if not project_written:
                lines.append("-e .")
                project_written = True
            continue
        lines.append(line)
    if not project_written:
        lines.insert(0, "-e .")
    return "\n".join(lines) + "\n"


def sanitize_install_report(payload: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """Retain only the fields required to reproduce the installation.

    A raw pip report embeds complete package descriptions and README examples,
    including unrelated illustrative absolute paths.  The durable evidence is
    deliberately smaller: exact source URL, archive hashes, package identity,
    requested/direct flags and the relevant interpreter platform contract.
    """

    root_text = root.as_posix().lower()
    install: list[dict[str, Any]] = []
    for raw_item in payload.get("install", []):
        item = json.loads(json.dumps(raw_item))
        metadata = item.get("metadata", {})
        raw_download = item.get("download_info", {})
        url = str(raw_download.get("url", ""))
        if url.startswith("file:"):
            decoded_url = unquote(url).lower().replace("\\\\", "/")
            if root_text in decoded_url or str(metadata.get("name", "")).lower() == "skru1-research":
                url = "repo-relative:."
            else:
                raise ContractViolation("Unexpected local file dependency in pip evidence")
        raw_archive = raw_download.get("archive_info", {})
        archive: dict[str, Any] = {"hashes": dict(raw_archive.get("hashes", {}))}
        if raw_archive.get("hash_source"):
            archive["hash_source"] = str(raw_archive["hash_source"])
        install.append(
            {
                "download_info": {"url": url, "archive_info": archive},
                "is_direct": bool(item.get("is_direct", False)),
                "is_yanked": bool(item.get("is_yanked", False)),
                "requested": bool(item.get("requested", False)),
                "metadata": {
                    "name": metadata.get("name"),
                    "version": metadata.get("version"),
                    "requires_python": metadata.get("requires_python"),
                },
            }
        )
    environment_keys = (
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "python_full_version",
        "python_version",
        "sys_platform",
    )
    environment = payload.get("environment", {})
    return {
        "version": payload.get("version"),
        "pip_version": payload.get("pip_version"),
        "install": install,
        "environment": {key: environment.get(key) for key in environment_keys},
    }


def enrich_report_hashes_from_pip_cache(
    payload: Mapping[str, Any], pip_cache_root: Path
) -> dict[str, Any]:
    """Fill hashes omitted by package indexes from pip's verified response cache.

    Some official PyTorch simple-index links do not carry a wheel hash fragment,
    so pip's PEP 658 report legitimately leaves ``archive_info.hashes`` empty.
    The exact downloaded response body is nevertheless retained in pip's
    content-addressed HTTP cache.  Hashing that body records the wheel bytes
    actually used without copying multi-gigabyte wheels into the repository.
    """

    enriched = json.loads(json.dumps(payload))
    http_cache = pip_cache_root / "http-v2"
    for item in enriched.get("install", []):
        download = item.get("download_info", {})
        url = str(download.get("url", ""))
        archive = download.setdefault("archive_info", {})
        hashes = archive.setdefault("hashes", {})
        if hashes.get("sha256") or not url.startswith(("https://", "http://")):
            continue
        cache_key = hashlib.sha224(url.encode("utf-8")).hexdigest()
        body = http_cache.joinpath(*cache_key[:5], f"{cache_key}.body")
        if body.is_file():
            hashes["sha256"] = sha256_file(body)
            archive["hash_source"] = "pip_http_cache_response_body"
    return enriched


def _run(command: list[str], cwd: Path, *, timeout: int = 600) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Environment command failed ({completed.returncode}): {' '.join(command[:5])}\n{completed.stderr[-4000:]}"
        )
    return {
        "command": [Path(value).name if index == 0 else value for index, value in enumerate(command)],
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }
