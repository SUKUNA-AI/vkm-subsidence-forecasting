"""Verify the canonical repository without model execution or truth parsing.

Run from a checkout: python scripts/verify_canonical_repository.py
Binary payloads, including sealed files, are streamed only into SHA-256.
No transformation, dataset regeneration, evaluation or network access occurs.
"""
from __future__ import annotations

import csv
import hashlib
import json
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


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(root: Path) -> dict:
    errors: list[dict] = []
    checked: dict[str, str] = {}
    visited: set[str] = set()

    def check(path: Path, expected: str, size=None) -> bool:
        path = path.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            errors.append({"kind": "missing_or_unsafe", "path": str(path)})
            return False
        name = path.relative_to(root).as_posix()
        actual = checked.setdefault(name, sha256(path))
        valid = actual == expected and (size is None or path.stat().st_size == int(size))
        if not valid:
            errors.append({"kind": "hash_or_size", "path": name})
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
                    # Only manifests are parsed. Table, truth and target bytes are never decoded.
                    if child.name in {"manifest.json", "representation_manifest.json"}:
                        manifest(child)

    for name, expected in PINNED.items():
        if check(root / name, expected):
            manifest(root / name)
    for name in ("input_manifest.csv", "source_manifest.csv", "supplementary_source_manifest.csv"):
        with (root / "configs" / name).open(encoding="utf-8-sig", newline="") as stream:
            for record in csv.DictReader(stream):
                check(root / record["relative_path"], record["sha256"], record["size_bytes"])

    # The representation's correction receipt is an additional pinned dependency.
    representation = json.loads((root / "artifacts/splits/scenario_representation_v2_1/representation_manifest.json").read_text(encoding="utf-8"))
    check(root / "artifacts/data_quality/scenario_simulation_v2_1/correction_receipt.json",
          representation["data_correction_receipt_sha256"])

    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode("utf-8").strip("\0").split("\0")
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
            resolved = (path.parent / local).resolve()
            if not resolved.exists() or resolved.is_relative_to(root / "work"):
                broken.append({"path": name, "target": target})
    return {
        "status": "FAIL" if errors or broken else "PASS",
        "pinned_hashes": {name: checked.get(name) for name in PINNED},
        "files_hash_checked": len(checked), "manifests_checked": len(visited),
        "markdown_files_checked": markdown_count,
        "errors": errors, "broken_local_markdown_links": broken,
        "link_scope": "local Markdown inline file/image targets; network URLs and anchors are not fetched",
        "models_executed": 0, "evaluator_truth_parsed": False,
    }


if __name__ == "__main__":
    result = verify(ROOT)
    destination = ROOT / "work/repo_cleanup/canonical_verification.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
