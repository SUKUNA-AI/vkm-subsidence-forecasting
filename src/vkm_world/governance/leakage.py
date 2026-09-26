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
# absolute home/cloud-VM prefixes: never portable, rewritten by ``sanitize_paths`` when publishing
MACHINE_PATH_FRAGMENTS = ("/home/user/", "/tmp/claude-", "/root/.claude/")
RELATIVE_FRAGMENTS_ALLOWED_IN_PROSE = frozenset({"work/ocr/"})
# logical names used instead of machine paths in published text
PATH_REWRITES: tuple[tuple[str, str], ...] = (
    ("/home/user/vkm-subsidence-forecasting_resourses/", "$VKM_RESOURCES_ROOT/"),
    ("/home/user/vkm-subsidence-forecasting/", "<PUBLIC>/"),
    ("/home/user/work/synth/", "PRIVATE 11_evidence_vnext/canonical/"),
    ("/home/user/work/merged/", "PRIVATE 11_evidence_vnext/merged/"),
    ("/home/user/work/", "$VKM_WORK/"),
    ("/home/user/.venv-research/bin/python", "python"),
    ("/home/user/.venv-research", "<cloud research venv>"),
)
MAX_TEXT_BYTES = 5_000_000
# historical public artifacts that predate the reset (kept on 'legacy'); listed explicitly, never extended silently
ALLOWLIST: set[str] = set()
# run-kit data/log files of the cloud run keep their historical text (chunk_plan.json names the private OCR
# page files of the resources checkout); executable code is never exempt: tools read their roots from the
# environment (docs/reset_2026_09/run_kit/tools/_roots.py)
PATH_CHECK_EXEMPT_PREFIXES: tuple[str, ...] = ("docs/reset_2026_09/run_kit/",)
PATH_CHECK_NEVER_EXEMPT_SUFFIXES = {".py"}
PATH_CHECK_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".jsonl", ".csv", ".toml", ".md"}


def sanitize_paths(text: str) -> str:
    """Replace machine-specific absolute paths with logical names (idempotent)."""
    for old, new in PATH_REWRITES:
        text = text.replace(old, new)
    return text


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
            exempt = (rel.startswith(PATH_CHECK_EXEMPT_PREFIXES)
                      and p.suffix.lower() not in PATH_CHECK_NEVER_EXEMPT_SUFFIXES)
            if check_paths and p.suffix.lower() in PATH_CHECK_SUFFIXES and not exempt:
                try:
                    txt = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                if "governance/leakage.py" in rel or "test_leakage" in rel:
                    continue
                # prose may name the git-ignored relative OCR work dir; code/config/data may not
                frags = FORBIDDEN_PATH_FRAGMENTS + MACHINE_PATH_FRAGMENTS
                if p.suffix.lower() == ".md":
                    frags = tuple(f for f in frags if f not in RELATIVE_FRAGMENTS_ALLOWED_IN_PROSE)
                hits = [f for f in frags if f in txt]
                if hits:
                    problems.append(f"{rel}: contains private/absolute path fragment(s) {hits}")
    return problems
