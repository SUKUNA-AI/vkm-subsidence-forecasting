"""Every Python file in scripts/ and src/ must at least parse: most scripts are not imported by any test."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(p for d in ("scripts", "src") for p in (ROOT / d).rglob("*.py"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_python_file_parses(path: Path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
