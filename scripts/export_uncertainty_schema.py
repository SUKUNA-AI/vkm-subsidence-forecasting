"""Regenerate schemas/uncertainty_registry.schema.json and schemas/nroy_design.schema.json (deterministic)."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_world.uncertainty.registry import json_schema  # noqa: E402
from vkm_world.uncertainty.nroy import json_schema as nroy_schema  # noqa: E402

for name, text in (("uncertainty_registry.schema.json", json_schema()), ("nroy_design.schema.json", nroy_schema())):
    out = ROOT / "schemas" / name
    out.write_text(text, encoding="utf-8", newline="\n")
    print(out.relative_to(ROOT))
