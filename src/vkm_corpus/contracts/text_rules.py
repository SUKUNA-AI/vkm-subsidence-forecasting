"""Versioned text rules (H-04): the one definition of page text and of rerank text.

* ``normalize_text_v1`` — canonical whitespace/Unicode clean-up of one text fragment (idempotent). Agent C applies it
  (after its own repairs such as CP1251 re-mapping) to produce ``blocks.normalized_text``.
* ``page_text_v1`` — page text = primary-layer blocks only, in reading order, running heads / footers / page numbers
  excluded, blocks separated by a blank line; computes ``normalized_text``, ``text_sha256`` and ``char_count`` of
  ``pages`` in one function.
* ``rerank_text_v1`` — the text a reranker sees for an object id. The SQL view ``rerank_text`` (duckdb/sql) implements
  the same rule and a test keeps both equal; E, F and G read only that view.

Changing any behaviour here requires a new rule id (``page_text_v2``...), never an edit of v1.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from vkm_corpus.contracts.vocab import BlockType, FormulaRawFormat, ObjectKind, TextRule

PAGE_TEXT_RULE = TextRule.PAGE_TEXT_V1.value
RERANK_TEXT_RULE = TextRule.RERANK_TEXT_V1.value

# block types left out of the page text (running heads, footers, printed page numbers)
EXCLUDED_BLOCK_TYPES_V1: frozenset[str] = frozenset({BlockType.PAGE_HEADER, BlockType.PAGE_FOOTER,
                                                     BlockType.PAGE_NUMBER})
BLOCK_SEPARATOR_V1 = "\n\n"

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st",
              "ﬆ": "st"}
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_SOFT_HYPHEN_EOL = re.compile("­[ \t]*\n")
_EOL_HYPHEN = re.compile(r"(?<=[^\W\d_])[-‐][ \t]*\n[ \t]*(?=[a-zа-яё])")
_SPACES = re.compile(r"\s+")


def normalize_text_v1(text: str | None) -> str:
    """NFC; CR/LF unified; soft hyphens, zero-width characters and C0/C1 controls removed; ligatures split; an
    end-of-line hyphen between a letter and a lower-case letter joins the word; all whitespace (including line
    breaks) collapses to one space; stripped. Letters, case, ``ё`` and U+FFFD are kept."""
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    s = unicodedata.normalize("NFC", s)
    s = _SOFT_HYPHEN_EOL.sub("", s).replace("­", "")
    s = s.translate(_ZERO_WIDTH)
    for lig, rep in _LIGATURES.items():
        s = s.replace(lig, rep)
    s = _EOL_HYPHEN.sub("", s)
    s = "".join(ch if (ch in "\n\t" or unicodedata.category(ch) != "Cc") else " " for ch in s)
    return _SPACES.sub(" ", s).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PageText:
    normalized_text: str | None
    text_sha256: str | None
    char_count: int
    rule: str
    block_ids: tuple[str, ...]


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)


def page_text_v1(blocks: Iterable[Any]) -> PageText:
    """Page text from the blocks of ONE page (mappings or row objects with ``object_id``, ``block_type``,
    ``reading_order``, ``is_primary_layer``, ``normalized_text``). Non-primary blocks are ignored."""
    chosen = []
    for b in blocks:
        if not _get(b, "is_primary_layer") or str(_get(b, "block_type")) in EXCLUDED_BLOCK_TYPES_V1:
            continue
        text = normalize_text_v1(_get(b, "normalized_text"))
        if text:
            chosen.append((int(_get(b, "reading_order")), str(_get(b, "object_id")), text))
    chosen.sort()
    if not chosen:
        return PageText(None, None, 0, PAGE_TEXT_RULE, ())
    page = BLOCK_SEPARATOR_V1.join(t for _, _, t in chosen)
    return PageText(page, sha256_text(page), len(page), PAGE_TEXT_RULE, tuple(oid for _, oid, _ in chosen))


def _join(sep: str, *parts: Any) -> str | None:
    kept = [str(p) for p in parts if p is not None and str(p) != ""]
    return sep.join(kept) if kept else None


def rerank_text_v1(object_kind: str, row: Any) -> str | None:
    """Text of an object for reranking (candidate passage). ``None`` when the object has no text.

    PAGE, BLOCK, BIBLIOGRAPHY_ENTRY: ``normalized_text``; FIGURE: label + normalised caption; TABLE: label, normalised
    caption and table text on separate lines; FORMULA: equation label + normalised LaTeX (else the raw output unless
    the formula is IMAGE_ONLY)."""
    kind = ObjectKind(object_kind)
    if kind in (ObjectKind.PAGE, ObjectKind.BLOCK, ObjectKind.BIBLIOGRAPHY_ENTRY):
        return _join("", _get(row, "normalized_text"))
    if kind == ObjectKind.FIGURE:
        return _join(" ", _get(row, "figure_label"), _get(row, "caption_normalized"))
    if kind == ObjectKind.TABLE:
        return _join("\n", _get(row, "table_label"), _get(row, "caption_normalized"), _get(row, "normalized_text"))
    if kind == ObjectKind.FORMULA:
        latex = _get(row, "normalized_latex")
        if latex is None and str(_get(row, "raw_format")) != FormulaRawFormat.IMAGE_ONLY:
            latex = _get(row, "raw_output")
        return _join(" ", _get(row, "equation_label"), latex)
    raise ValueError(f"no rerank text for {kind}")
