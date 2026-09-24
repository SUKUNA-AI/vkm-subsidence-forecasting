"""Verify the current canonical repository without model execution or truth parsing.

The current research state distinguishes two kinds of external paths:

1. ACTIVE scientific evidence (books/articles/theses/source PDFs), stored in the
   private resources repository and hash-verified when available.
2. LEGACY_RETIRED project-generated packages (`SKRU1_ACTUAL_DATA_TABLES_v1`,
   old v3.x reconstructed/model-ready/EDA artifacts and `inputs/bootstrap`).
   These are historical provenance only and are not current dependencies.

Frozen manifests are never rewritten merely to remove historical references.
References to retired paths are reported as skipped legacy references and do
not make canonical verification fail.

No transformation, dataset regeneration, model execution, evaluator-truth
parsing or network access occurs.
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

# Only actual scientific source evidence is an active external dependency.
ACTIVE_EXTERNAL_PREFIX_MAP = {
    "inputs/sources/primary/": "08_data_archives/main_repo_snapshots/inputs_sources/primary/",
    "inputs/sources/supplementary/": "08_data_archives/main_repo_snapshots/inputs_sources/supplementary/",
}

# These paths describe the retired exploratory/reconstructed project branch.
# They remain valid historical provenance strings inside immutable manifests,
# but current research does not require their bytes.
RETIRED_PREFIXES = (
    "SKRU1_ACTUAL_DATA_TABLES_v1/",
    "inputs/bootstrap/",
)


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


def is_active_external(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in ACTIVE_EXTERNAL_PREFIX_MAP)


def is_retired(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in RETIRED_PREFIXES)


def active_external_target(name: str, resources_root: Path | None) -> Path | None:
    if resources_root is None:
        return None
    for prefix, mapped_prefix in ACTIVE_EXTERNAL_PREFIX_MAP.items():
        if name.startswith(prefix):
            return resources_root / mapped_prefix / name[len(prefix):]
    return None


def verify(root: Path) -> dict:
    errors: list[dict] = []
    checked: dict[str, str] = {}
    external_checked: dict[str, str] = {}
    external_unchecked: list[dict] = []
    retired_skipped: list[dict] = []
    externalized_markdown_links: list[dict] = []
    retired_markdown_links: list[dict] = []
    visited: set[str] = set()
    resources_root = detect_resources_root(root)

    def relative_name(path: Path) -> str | None:
        try:
            return path.resolve(strict=False).relative_to(root.resolve()).as_posix()
        except ValueError:
            return None

    def record_retired(name: str, expected: str | None = None, source: str = "manifest") -> None:
        record = {"path": name, "source": source}
        if expected is not None:
            record["historical_expected_sha256"] = expected
        if record not in retired_skipped:
            retired_skipped.append(record)

    def check(path: Path, expected: str, size=None) -> bool:
        name = relative_name(path)
        if name is None:
            errors.append({"kind": "unsafe_path", "path": str(path)})
            return False

        # A retired historical reference is intentionally not resolved to a
        # current byte payload. Git history is the archive for that branch.
        if is_retired(name):
            record_retired(name, expected)
            return True

        candidate = path
        origin = "main"

        if not candidate.is_file() and is_active_external(name):
            candidate = active_external_target(name, resources_root)
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
                    # Recurse only into current real manifest files. Retired
                    # references intentionally have no current file to recurse into.
                    if child.is_file() and child.name in {"manifest.json", "representation_manifest.json"}:
                        manifest(child)

    for name, expected in PINNED.items():
        if check(root / name, expected):
            manifest(root / name)

    # `input_manifest.csv` belongs to the retired v3.x project package and is
    # retained only as historical provenance. Active scientific source manifests
    # remain strict dependencies.
    for name in ("source_manifest.csv", "supplementary_source_manifest.csv"):
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

    broken: list[dict] = []
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
            if rel is not None and is_retired(rel):
                retired_markdown_links.append({"path": name, "target": target})
                record_retired(rel, source="markdown")
                continue
            if rel is not None and is_active_external(rel):
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
        "active_external_evidence_required": True,
        "externalized_inputs_unchecked": external_unchecked,
        "retired_legacy_references_skipped": retired_skipped,
        "externalized_markdown_links": externalized_markdown_links,
        "retired_markdown_links": retired_markdown_links,
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
