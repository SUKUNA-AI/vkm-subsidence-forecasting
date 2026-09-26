"""PRIVATE → PUBLIC leakage guard.

The public repository may hold ids, locators, values, bibliographic metadata and short paraphrases.
It must NOT hold registered scientific binaries (PDF/DjVu/DOCX/archives from the private corpus),
verbatim-quote columns, OCR text dumps or private absolute paths.
"""
from __future__ import annotations

import csv
import json
import re
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
    # all_records.jsonl / kind_*.csv live only in the rebuilt merge dir (run kit merge_sweep.py); the committed
    # summaries are in PRIVATE 11_evidence_vnext/merged/ (review finding DOCS_LEAKAGE-016)
    ("/home/user/work/merged/", "$VKM_WORK/merged/"),
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


# catalogue cells may not name the PRIVATE OCR work dir even relatively (prose may: RELATIVE_FRAGMENTS_ALLOWED_IN_PROSE)
DATA_PATH_REWRITES: tuple[tuple[str, str], ...] = (("work/ocr/", "PRIVATE:ocr/"),)


def sanitize_paths(text: str, data: bool = False) -> str:
    """Replace machine-specific absolute paths with logical names (idempotent).

    ``data=True`` (catalogue cells, JSON) also rewrites relative references to the PRIVATE OCR work dir."""
    for old, new in PATH_REWRITES + (DATA_PATH_REWRITES if data else ()):
        text = text.replace(old, new)
    return text


# ---------------------------------------------------------------- verbatim text (review finding DOCS_LEAKAGE-023)
SHINGLE_WORDS = 12
VERBATIM_LIMIT_WORDS = 25            # a PUBLIC text may not repeat this many consecutive words of a source quote
BIBLIO_COLUMN_HINTS = ("cited_work", "citation", "title", "authors", "bibliograph", "reference")  # bibliographic entries, not quotes
_WORD = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)


def words(text: str) -> list[str]:
    return [w.replace("ё", "е") for w in _WORD.findall(text.lower())]


def quote_shingles(texts) -> set[str]:
    """All 12-word shingles of the given verbatim quotes."""
    out: set[str] = set()
    for text in texts:
        w = words(text)
        out.update(" ".join(w[k:k + SHINGLE_WORDS]) for k in range(len(w) - SHINGLE_WORDS + 1))
    return out


def longest_shared_run(tokens: list[str], shingles: set[str]) -> tuple[int, int, int]:
    """(alphabetic words, start, end) of the longest stretch of ``tokens`` covered by consecutive known shingles.

    Numbers are values, which PUBLIC may hold; only the prose part of a copied stretch counts."""
    best = (0, 0, 0)
    start = None
    n = len(tokens) - SHINGLE_WORDS + 1
    for k in range(n + 1):
        hit = k < n and " ".join(tokens[k:k + SHINGLE_WORDS]) in shingles
        if hit and start is None:
            start = k
        if not hit and start is not None:
            end = k - 1 + SHINGLE_WORDS
            alpha = sum(1 for w in tokens[start:end] if not w.isdigit())
            if alpha > best[0]:
                best = (alpha, start, end)
            start = None
    return best


def _json_keys(obj) -> set[str]:
    """All object keys of a JSON value, lower-cased, at any depth (review finding DOCS_LEAKAGE-023)."""
    keys: set[str] = set()
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            keys |= {k.strip().lower() for k in cur if isinstance(k, str)}
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return keys


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
        if p.suffix.lower() in (".jsonl", ".json"):
            try:
                with open(p, encoding="utf-8") as f:
                    docs = ([json.loads(line) for line in f if line.strip()] if p.suffix.lower() == ".jsonl"
                            else [json.load(f)])
                bad = set()
                for d in docs:
                    bad |= FORBIDDEN_COLUMNS & _json_keys(d)
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
