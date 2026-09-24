"""Verify the canonical repository without model execution or truth parsing.

Run from a checkout:
    python scripts/verify_canonical_repository.py

The current main repository intentionally externalizes source PDFs, historical
bootstrap packages and the legacy SKRU1_ACTUAL_DATA_TABLES_v1 tree into the
private resources repository. If that repository is available, set
VKM_RESOURCES_ROOT or keep it at one of the detected local locations; external
dependencies will then be hash-verified as well.

Binary payloads, including sealed files, are streamed only into SHA-256.
No transformation, dataset regeneration, evaluation or network access occurs.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
PINNED = {
    "data/scenario_simulation_v2_1/manifest.json": "a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95",
    "artifacts/reconstruction/scenario_constraints_v2/manifest.json": "96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef",
    "artifacts/splits/scenario_representation_v2_1/representation_manifest.json": "0e5501cb4c3cff570fbd3763047a8dd8738d45706dd6f549556ac428521f5a6d",
}
EXTERNAL_PREFIX_MAP = {
    "inputs/bootstrap/": "08_data_archives/main_repo_snapshots/inputs_bootstrap/",
    "inputs/sources/primary/": "08_data_archives/main_repo_snapshots/inputs_sources/primary/",
    "inputs/sources/supplementary/": "08_data_archives/main_repo_snapshots/inputs_sources/supplementary/",
    "SKRU1_ACTUAL_DATA_TABLES_v1/": "08_data_archives/main_repo_snapshots/SKRU1_ACTUAL_DATA_TABLES_v1/",
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def detect_resources_root(root: Path) -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("VKM_RESOURCES_ROOT")
    if env:
        candidates.append(Path(env))
    candidates.append(root / "vkm-subsidence-forecasting_resourses")
    candidates.append(root.parent / "vkm-subsidence-forecasting_resourses")
    for candidate in candidates:
        if (candidate / ".git").exists() and (candidate / "00_registry/SOURCE_REGISTER.csv").is_file():
            return candidate.resolve()
    return None


def external_target(name: str, resources_root: Path | None) -> Path | None:
    if resources_root is None:
        return None
    for prefix, mapped_prefix in EXTERNAL_PREFIX_MAP.items():
        if name.startswith(prefix):
            suffix = name[len(prefix):]
            return resources_root / mapped_prefix / suffix
    return None


def is_externalized(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in EXTERNAL_PREFIX_MAP)


def verify(root: Path) -> dict:
    errors: list[dict] = []
    checked: dict[str, str] = {}
    external_checked: dict[str, str] = {}
    external_unchecked: list[dict] = []
    externalized_markdown_links: list[dict] = []
    visited: set[str] = set()
    resources_root = detect_resources_root(root)

    def relative_name(path: Path) -> str | None:
        try:
            return path.resolve(strict=False).relative_to(root.resolve()).as_posix()
        except ValueError:
            return None

    def check(path: Path, expected: str, size=None) -> bool:
        name = relative_name(path)
        if name is None:
            errors.append({"kind": "unsafe_path", "path": str(path)})
            return False

        candidate = path
        origin = "main"
        if not candidate.is_file() and is_externalized(name):
            candidate = external_target(name, resources_root)
            origin = "private_resources"
            if candidate is None:
                external_unchecked.append(
                    {
                        "path": name,
                        "expected_sha256": expected,
                        "size_bytes": int(size) if size is not None else None,
                    }
                )
                return True

        if candidate is None or not candidate.is_file():
            errors.append({"kind": "missing", "path": name, "origin": origin})
            return False

        actual = sha256(candidate)
        valid = actual == expected and (size is None or candidate.stat().st_size == int(size))
        if origin == "main":
            checked.setdefault(name, actual)
        else:
            external_checked.setdefault(name, actual)
        if not valid:
            errors.append(
                {
                    "kind": "hash_or_size",
                    "path": name,
                    "origin": origin,
                    "actual_sha256": actual,
                    "expected_sha256": expected,
                }
            )
        return valid

    def manifest(path: Path) -> None:
        name = path.relative_to(root).as_posix()
        if name in visited:
            return
        visited.add(name)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        for section in ("inputs", "sources", "outputs"):
            for record in payload.get(section, []):
                if not isinstance(record, dict) or not {"path", "sha256"} <= record.keys():
                    continue
                child = (path.parent if section == "outputs" else root) / record["path"]
                if check(child, record["sha256"], record.get("size_bytes")):
                    if child.is_file() and child.name in {"manifest.json", "representation_manifest.json"}:
                        manifest(child)

    for name, expected in PINNED.items():
        if check(root / name, expected):
            manifest(root / name)

    for name in ("input_manifest.csv", "source_manifest.csv", "supplementary_source_manifest.csv"):
        with (root / "configs" / name).open(encoding="utf-8-sig", newline="") as stream:
            for record in csv.DictReader(stream):
                check(root / record["relative_path"], record["sha256"], record["size_bytes"])

    representation = json.loads(
        (root / "artifacts/splits/scenario_representation_v2_1/representation_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        root / "artifacts/data_quality/scenario_simulation_v2_1/correction_receipt.json",
        representation["data_correction_receipt_sha256"],
    )

    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    ).decode("utf-8").strip("\0").split("\0")
    broken = []
    markdown_count = 0
    for name in sorted(set(names)):
        path = root / name
        if path.suffix.lower() != ".md" or not path.is_file():
            continue
        markdown_count += 1
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8-sig"), flags=re.S)
        for match in re.finditer(r'!?\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)(?:\s+"[^"]*")?\)', text):
            target = unquote(match[1].strip("<>"))
            if re.match(r"\w+://|mailto:|app:|codex:|data:", target):
                continue
            local = target.split("#", 1)[0]
            if not local:
                continue
            resolved = (path.parent / local).resolve(strict=False)
            if resolved.exists() and not resolved.is_relative_to(root / "work"):
                continue
            rel = relative_name(resolved)
            if rel is not None and is_externalized(rel):
                externalized_markdown_links.append({"path": name, "target": target})
                continue
            broken.append({"path": name, "target": target})

    if errors or broken:
        status = "FAIL"
    elif external_unchecked:
        status = "PASS_CORE_EXTERNAL_UNCHECKED"
    else:
        status = "PASS"

    return {
        "status": status,
        "pinned_hashes": {name: checked.get(name) for name in PINNED},
        "files_hash_checked_main": len(checked),
        "files_hash_checked_private_resources": len(external_checked),
        "manifests_checked": len(visited),
        "markdown_files_checked": markdown_count,
        "resources_root": str(resources_root) if resources_root else None,
        "external_archive_required": True,
        "externalized_inputs_unchecked": external_unchecked,
        "externalized_markdown_links": externalized_markdown_links,
        "errors": errors,
        "broken_local_markdown_links": broken,
        "link_scope": "local Markdown inline file/image targets; network URLs and anchors are not fetched",
        "models_executed": 0,
        "evaluator_truth_parsed": False,
    }


if __name__ == "__main__":
    result = verify(ROOT)
    destination = ROOT / "work/repo_cleanup/canonical_verification.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"].startswith("PASS") else 1)
