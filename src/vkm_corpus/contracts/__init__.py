"""The single contract of the VKM document layer (CP-16): vocabularies, row models, Arrow schemas, text rules.

Modules:

* ``vocab`` — every ``StrEnum`` of the platform (other packages only import them);
* ``fieldtypes`` — Annotated field types (explicit Arrow widths, ID/hash patterns);
* ``base`` / ``models`` — pydantic row models of all datasets (the schema's single source of truth);
* ``datasets`` — dataset registry (class, version, keys, partitions);
* ``arrow`` — Arrow schemas and canonical fingerprints derived from the models (pyarrow imported lazily);
* ``text_rules`` — ``normalize_text_v1``, ``page_text_v1``, ``rerank_text_v1`` (H-04);
* ``signatures`` — ``stage_signature``, ``call_signature``, ``pixel_sha256``, ``config_hash`` (H-05);
* ``site_scope`` — register scope → ``vkm_world`` Scope mapping (CP-07);
* ``builders`` — envelope contexts and ``build_row`` (content hash + validation);
* ``export`` — JSON Schema / Arrow / vocabulary export to ``schemas/corpus/``.

IDs live in ``vkm_corpus.ids``. Only ``vocab`` is imported eagerly; everything else on first attribute access.
"""
from __future__ import annotations

import importlib
from typing import Any

from vkm_corpus.contracts import vocab

SUBMODULES = ("vocab", "fieldtypes", "base", "models", "datasets", "arrow", "text_rules", "signatures", "site_scope",
              "builders", "export")

__all__ = list(SUBMODULES)


def __getattr__(name: str) -> Any:
    if name in SUBMODULES:
        return importlib.import_module(f"vkm_corpus.contracts.{name}")
    raise AttributeError(f"module 'vkm_corpus.contracts' has no attribute {name!r}")
