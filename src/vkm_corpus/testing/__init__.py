"""Synthetic test data for all agents (never source text): valid row factories and a synthetic canonical root.

* ``vkm_corpus.testing.rows`` — ``make_page()``, ``make_block()``, … minimal valid rows;
* ``vkm_corpus.testing.synthetic`` — ``synthetic_canon(root, ...)``: a small valid CANONICAL root (several source
  formats, pages, two text layers, figure, table, formula, bibliography with a foreign page, 013/022 skipped by the
  register, a Work group, run markers, snapshot + ``CURRENT``) and optionally the materialised DuckDB file.
"""
from __future__ import annotations

from typing import Any

__all__ = ["synthetic_canon", "SyntheticCanon"]


def __getattr__(name: str) -> Any:
    if name in ("synthetic_canon", "SyntheticCanon", "build_synthetic_corpus"):
        from vkm_corpus.testing import synthetic

        return getattr(synthetic, name)
    raise AttributeError(name)
