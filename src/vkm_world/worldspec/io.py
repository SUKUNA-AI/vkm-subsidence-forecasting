"""Deterministic serialisation, content hashing and JSON-Schema export of a WorldSpec."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .model import WorldSpec


def to_json(world: WorldSpec) -> str:
    """Canonical JSON: sorted keys, UTF-8, no volatile fields → identical bytes for identical content."""
    return json.dumps(world.model_dump(mode="json", exclude_none=True), ensure_ascii=False, sort_keys=True,
                      indent=1) + "\n"


def content_hash(world: WorldSpec) -> str:
    return hashlib.sha256(to_json(world).encode("utf-8")).hexdigest()


def save(world: WorldSpec, path: str | Path) -> str:
    text = to_json(world)
    Path(path).write_text(text, encoding="utf-8", newline="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(path: str | Path) -> WorldSpec:
    return WorldSpec.model_validate_json(Path(path).read_text(encoding="utf-8"))


def json_schema() -> str:
    schema = WorldSpec.model_json_schema()
    schema["$id"] = "https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/schemas/worldspec_vnext.schema.json"
    schema["title"] = "VKM/SKRU-1 WorldSpec vNext"
    return json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
