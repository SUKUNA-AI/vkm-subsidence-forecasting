"""Deterministic export of the contracts to ``schemas/corpus/`` (committed; a test compares it with the code).

    python -m vkm_corpus.contracts.export            # write
    python -m vkm_corpus.contracts.export --check    # exit 1 if the committed files differ from the code

Files: ``<dataset>.schema.json`` (JSON Schema of the row model), ``arrow_schemas.json`` (Arrow fields, nullability,
fingerprints, keys), ``vocabularies.json`` (all closed enums), ``id_grammar.json``. Format: sorted keys, indent 1, LF,
UTF-8 (like ``vkm_world.worldspec.io.json_schema``). The JSON Schema depends on the pinned pydantic 2.13.5 /
pydantic-core 2.46.5 pair (CP-02).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vkm_corpus.contracts import arrow as arrow_mod
from vkm_corpus.contracts.datasets import DATASETS
from vkm_corpus.contracts.vocab import all_vocabularies
from vkm_corpus.ids import grammar

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "corpus"
BASE_ID = "https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/schemas/corpus/"
PINNED_PYDANTIC = ("2.13.5", "2.46.5")


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def dataset_json_schema(name: str) -> dict:
    spec = DATASETS[name]
    schema = spec.model.model_json_schema()
    schema["$id"] = f"{BASE_ID}{name}.schema.json"
    schema["title"] = f"VKM corpus dataset {name}"
    schema["x-vkm-dataset"] = name
    schema["x-vkm-dataset-class"] = spec.dataset_class.value
    schema["x-vkm-envelope-profile"] = spec.profile.value
    schema["x-vkm-schema-version"] = spec.version
    schema["x-vkm-schema-fingerprint"] = arrow_mod.schema_fingerprint(name)
    schema["x-vkm-primary-key"] = list(spec.primary_key)
    return schema


def arrow_schemas() -> dict:
    out = {}
    for name, spec in DATASETS.items():
        out[name] = {
            "dataset_class": spec.dataset_class.value,
            "envelope_profile": spec.profile.value,
            "schema_version": spec.version,
            "schema_fingerprint": arrow_mod.schema_fingerprint(name),
            "primary_key": list(spec.primary_key),
            "sort_key": list(spec.sort_key),
            "partition_keys": list(spec.partition_keys) if spec.stored else [],
            "fields": [{"name": f.name, "type": str(f.type), "nullable": f.nullable}
                       for f in arrow_mod.field_specs(spec.model)],
        }
    return {"datasets": out}


def vocabularies() -> dict:
    return {name: [m.value for m in enum] for name, enum in sorted(all_vocabularies().items())}


def id_grammar() -> dict:
    return {"patterns": dict(grammar.PATTERNS), "reserved": dict(grammar.RESERVED),
            "logical_roots": list(grammar.LOGICAL_ROOTS)}


def render_all() -> dict[str, str]:
    """File name → content of every exported file."""
    files = {f"{name}.schema.json": _dump(dataset_json_schema(name)) for name in DATASETS}
    files["arrow_schemas.json"] = _dump(arrow_schemas())
    files["vocabularies.json"] = _dump(vocabularies())
    files["id_grammar.json"] = _dump(id_grammar())
    return files


def write(target: Path = SCHEMA_DIR) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for fname, text in render_all().items():
        (target / fname).write_text(text, encoding="utf-8", newline="\n")
        written.append(fname)
    return written


def check(target: Path = SCHEMA_DIR) -> list[str]:
    """Names of files that differ from the code (missing, stale or unexpected)."""
    expected = render_all()
    problems = []
    for fname, text in expected.items():
        path = target / fname
        if not path.is_file():
            problems.append(f"missing {fname}")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"stale {fname}")
    if target.is_dir():
        for path in target.glob("*.json"):
            if path.name not in expected:
                problems.append(f"unexpected {path.name}")
    return sorted(problems)


def pydantic_is_pinned() -> bool:
    import pydantic
    import pydantic_core

    return (pydantic.VERSION, pydantic_core.__version__) == PINNED_PYDANTIC


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vkm_corpus.contracts.export")
    parser.add_argument("--check", action="store_true", help="compare committed files with the code")
    parser.add_argument("--target", type=Path, default=SCHEMA_DIR)
    args = parser.parse_args(argv)
    if not pydantic_is_pinned():
        print(f"warning: pydantic/pydantic-core are not the pinned pair {PINNED_PYDANTIC}; the JSON Schema may differ",
              file=sys.stderr)
    if args.check:
        problems = check(args.target)
        for p in problems:
            print(p)
        return 1 if problems else 0
    for fname in write(args.target):
        print(fname)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
