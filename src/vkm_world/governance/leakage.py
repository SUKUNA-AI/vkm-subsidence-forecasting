"""PRIVATE → PUBLIC leakage guard.

The public repository may hold ids, locators, values, bibliographic metadata and short paraphrases.
It must NOT hold registered scientific binaries (PDF/DjVu/DOCX/archives from the private corpus),
verbatim-quote columns, OCR text dumps or private absolute paths.
"""
from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

FORBIDDEN_EXT = {".pdf", ".djvu", ".docx", ".doc", ".zip", ".7z", ".rar", ".xlsx", ".xls", ".tif", ".tiff"}
FORBIDDEN_COLUMNS = {"quote", "verbatim_quote", "ocr_text", "page_text", "full_text"}
FORBIDDEN_PATH_FRAGMENTS = ("/home/user/vkm-subsidence-forecasting_resourses/", "E:\\Диплом", "work/ocr/")
MAX_TEXT_BYTES = 5_000_000
# historical public artifacts that predate the reset (kept on 'legacy'); listed explicitly, never extended silently
ALLOWLIST: set[str] = set()
# run-kit files of the cloud run intentionally carry VM paths (rewritten by localize_paths.sh)
PATH_CHECK_EXEMPT_PREFIXES: tuple[str, ...] = ("docs/reset_2026_09/run_kit/",)
PATH_CHECK_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".jsonl", ".csv", ".toml"}


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True).stdout
        return [root / p for p in out.splitlines() if p]
    except Exception:  # noqa: BLE001
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


def scan(root: str | Path, files: list[Path] | None = None, check_paths: bool = True) -> list[str]:
    root = Path(root)
    problems: list[str] = []
    for p in files if files is not None else tracked_files(root):
        rel = p.relative_to(root).as_posix()
        if rel in ALLOWLIST or not p.exists():
            continue
        if p.suffix.lower() in FORBIDDEN_EXT:
            problems.append(f"{rel}: binary type {p.suffix} not allowed in PUBLIC (belongs to the private corpus)")
            continue
        if p.suffix.lower() in {".csv", ".tsv"}:
            try:
                with open(p, encoding="utf-8", newline="") as f:
                    header = next(csv.reader(f, delimiter="\t" if p.suffix == ".tsv" else ","), [])
                bad = FORBIDDEN_COLUMNS & {h.strip().lower() for h in header}
                if bad:
                    problems.append(f"{rel}: verbatim/OCR text columns {sorted(bad)} not allowed in PUBLIC")
            except (UnicodeDecodeError, StopIteration):
                pass
        if p.suffix.lower() == ".jsonl":
            try:
                with open(p, encoding="utf-8") as f:
                    first = json.loads(f.readline() or "{}")
                bad = FORBIDDEN_COLUMNS & set(first)
                if bad:
                    problems.append(f"{rel}: verbatim/OCR text fields {sorted(bad)} not allowed in PUBLIC")
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        if p.suffix.lower() in {".md", ".csv", ".json", ".jsonl", ".py", ".yaml", ".yml", ".txt"}:
            if p.stat().st_size > MAX_TEXT_BYTES:
                problems.append(f"{rel}: text file larger than {MAX_TEXT_BYTES} bytes")
            if check_paths and p.suffix.lower() in PATH_CHECK_SUFFIXES and not rel.startswith(PATH_CHECK_EXEMPT_PREFIXES):
                try:
                    txt = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for frag in FORBIDDEN_PATH_FRAGMENTS:
                    if frag in txt and "governance/leakage.py" not in rel and "test_leakage" not in rel:
                        problems.append(f"{rel}: contains private/absolute path fragment '{frag}'")
    return problems
