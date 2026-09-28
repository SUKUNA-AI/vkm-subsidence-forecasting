"""Assemble benchmarks/retrieval_v0/RESULTS_V0.md from the narrative template, $J_V0/out/tables.md and narrative blocks.

Usage: v0_build_report.py <template.md> <out.md> [narrative.md]
Markers ``{{T1}}`` … are replaced by blocks ``### T1 …`` of tables.md; ``{{SUMMARY}}`` … by blocks ``### SUMMARY`` of
the narrative file (upper-case names keep blank lines).
"""
import os
import re
import sys
from pathlib import Path

V0 = Path(os.environ["J_V0"])
template = Path(sys.argv[1]).read_text(encoding="utf-8")
out_path = Path(sys.argv[2])
narrative = Path(sys.argv[3]).read_text(encoding="utf-8") if len(sys.argv) > 3 else ""
source = (V0 / "out" / "tables.md").read_text(encoding="utf-8") + "\n" + narrative
blocks: dict[str, list[str]] = {}
cur = None
for line in source.splitlines():
    m = re.match(r"^### (\w+)( |$)", line)
    if m:
        cur = m.group(1)
        blocks[cur] = []
        continue
    if cur:
        blocks[cur].append(line)


def block(key: str) -> str:
    rows = blocks.get(key, [])
    if key.isupper():
        while rows and not rows[-1].strip():
            rows = rows[:-1]
        while rows and not rows[0].strip():
            rows = rows[1:]
    else:
        rows = [r for r in rows if r.strip()]
    return "\n".join(rows) if rows else "_(нет данных)_"


text = template
for _ in range(3):                                  # blocks may contain markers of other blocks
    text = re.sub(r"\{\{(\w+)\}\}", lambda m: block(m.group(1)), text)
missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", template)) - set(blocks))
out_path.write_text(text, encoding="utf-8")
print("written", out_path.name, "missing blocks:", missing)
