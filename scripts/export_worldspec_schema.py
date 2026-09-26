"""Regenerate schemas/worldspec_vnext.schema.json from the pydantic WorldSpec model (deterministic)."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_world.worldspec.io import json_schema  # noqa: E402

out = ROOT / "schemas" / "worldspec_vnext.schema.json"
out.write_text(json_schema(), encoding="utf-8", newline="\n")
print(out.relative_to(ROOT))
