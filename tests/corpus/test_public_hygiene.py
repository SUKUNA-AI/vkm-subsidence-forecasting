"""PUBLIC hygiene of the Corpus Platform (decision CP-04), on top of ``vkm_world.governance.leakage.scan``.

The leakage guard does not look at runtime data formats, infra file types, private IPv4 addresses or secrets. These
checks cover the platform areas of the repository (tracked and untracked-not-ignored files).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, scan

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_AREAS = ("src/vkm_corpus/", "src/vkm_cad/", "src/vkm_drawio/", "infra/", "tests/corpus/",
                  "docs/corpus_platform/", "docs/implementation_work/", "docs/diagrams/", "schemas/corpus/")
RUNTIME_EXT = {".epub", ".djv", ".djvu", ".parquet", ".duckdb", ".arrow", ".feather", ".gguf", ".safetensors"}
TEXT_EXT = {".py", ".md", ".json", ".jsonl", ".yml", ".yaml", ".toml", ".txt", ".csv", ".sh", ".ps1", ".service",
            ".timer", ".conf", ".ini", ".cfg", ".sql", ".cypher", ".example", ".drawio", ".svg", ".xml", ".env"}
TEXT_NAMES = {"Dockerfile", "compose.yml", "compose.yaml"}
PRIVATE_IPV4 = re.compile(r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})"
                          r"(?![\d.])")
HOST_PATH = re.compile(r"(?<![\w$])(?:[A-Za-z]:[\\/](?:Users|Диплом|VKM|Program)|/home/[A-Za-z_][\w.-]*/|/mnt/[a-z]/"
                       r"|/Users/[\w.-]+/)")
SECRET = re.compile(r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bghp_[A-Za-z0-9]{20,}|\bhf_[A-Za-z0-9]{20,}"
                    r"|\bsk-[A-Za-z0-9]{20,}|(?i:password)\s*[:=]\s*['\"][^'\"<>{}$]{6,}['\"])")
# files that must quote the forbidden patterns to test them
SELF = {"tests/corpus/test_public_hygiene.py"}


def _platform_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout
    return sorted({p for p in out.splitlines() if p.startswith(PLATFORM_AREAS) and (ROOT / p).is_file()})


def _is_text(rel: str) -> bool:
    p = Path(rel)
    return p.suffix.lower() in TEXT_EXT or p.name in TEXT_NAMES or p.name.startswith(".env")


def test_no_runtime_or_corpus_binaries():
    bad = [p for p in _platform_files() if Path(p).suffix.lower() in RUNTIME_EXT]
    assert bad == [], bad


@pytest.mark.parametrize("pattern,label", [(PRIVATE_IPV4, "private IPv4"), (HOST_PATH, "host path"),
                                           (SECRET, "secret")])
def test_no_ips_host_paths_or_secrets(pattern, label):
    hits = []
    for rel in _platform_files():
        if rel in SELF or not _is_text(rel):
            continue
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and "host-path-ok" not in line:
                hits.append(f"{rel}:{n}: {label}")
    assert hits == [], "\n".join(hits[:30])


def test_platform_files_pass_leakage_scan():
    files = [ROOT / p for p in _platform_files()]
    problems = scan(ROOT, files=files)
    assert problems == [], "\n".join(problems[:20])


def test_hygiene_patterns_detect_violations():
    assert PRIVATE_IPV4.search("url = http://192.168.1.7:9200")
    assert PRIVATE_IPV4.search("10.42.0.1:8333")
    assert not PRIVATE_IPV4.search("bind 127.0.0.1:7687 and version 10.4.3")
    assert HOST_PATH.search("/home/someone/vkm/staging") and HOST_PATH.search("C:\\Users\\x")
    assert not HOST_PATH.search("$VKM_DATA_ROOT/canonical")
    assert SECRET.search("password: 'hunter2hunter2'") and not SECRET.search("password: '<placeholder>'")
    assert "quote" in FORBIDDEN_COLUMNS
