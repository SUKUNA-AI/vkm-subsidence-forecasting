"""Figures, tables and formulas repeated across sources — NAV part ``object_duplicates`` (rule
``object_duplicates_v1``), no LLM and no learned model.

The part ``duplicates`` (agent U) compares only text passages. This part compares the document objects of the canon —
figures (``canonical.figures`` + their image artifacts), tables (``canonical.tables`` cells) and formulas
(``canonical.formulas`` LaTeX) — across sources and writes four Arrow tables:

* ``object_dup_clusters`` — a group of objects of one type found in two or more sources: ``kind``, the primary source
  (earliest publication year; the rule that decided it), counts;
* ``object_dup_members`` — the objects of each group with their evidence against the reference object of the group
  (image hash distances, caption similarity, cell containment, formula match);
* ``formula_keys`` — the canonical form of every formula (``latex_key``, the renaming-invariant ``shape_key``,
  complexity, ``trivial``): the lookup table of ``shared_formulas(latex)``;
* ``figure_hashes`` — perceptual hashes of every figure image (a cache for the next build: rows are reused by artifact
  id and hash rule).

Method (every number is a MODEL_CHOICE of ``DEFAULTS``, recorded in the manifest):

1. **Figures.** The image of a figure is its page-render crop (``FIGURE_CROP``), else its native embedded image. Each
   image is decoded once (Pillow, CPU processes) to grey levels and reduced by exact bin averaging (numpy, no
   resampling library): pHash (32×32 → 8×8 DCT, median of the AC terms) of the whole crop; pHash and dHash (9×9
   gradients) of the content box (uniform margins trimmed) under the 8 rotations/mirrors of the square thumbnail —
   margins, scale and quarter turns do not change them. Candidate pairs of different sources come from an exact
   all-pairs Hamming scan. A pair is verified by image evidence and context, with agreeing content-box aspect ratios:
   ``IMAGE`` — distance <= ``fig_strong_max`` with a dHash that agrees; ``IMAGE_CAPTION`` — distance <=
   ``fig_medium_max`` and a similar caption or the same printed number; ``CAPTION_IMAGE`` — distance <=
   ``fig_weak_max`` and a nearly equal caption; ``CAPTION`` — a nearly equal caption while the images differ, only
   with context (the same printed number in two copies of one work, or pages beside a text passage that the two
   sources share — part ``duplicates``) and loosely alike images. Low-information images (blank, a few pixels of ink,
   icons under ``fig_min_side`` px) take part only when identical.
2. **Tables.** Cell text row by row (math spans as plain text, NFKC, case fold, letters and digits only, look-alike
   letters folded — decimal commas, points and spaces in numbers vanish), character ``table_shingle_k``-grams;
   candidates from an inverted index of shingles (shingles in more than ``table_max_df`` tables skipped) and of number
   windows. ``CELLS`` — exact containment ``|A∩B| / min(|A|,|B|)`` of the whole table and of the rows below the header,
   with the numbers agreeing when both tables carry them (the same row labels with other values are another table);
   ``NUMBERS`` — the same informative number windows in reading order (a translated or reworded table). Header
   similarity is reported.
3. **Formulas.** The LaTeX is read by the parser of the part ``formulas`` and written back in a canonical form
   (``latex_key``): Greek names and variants unified, fonts, spacing and ``\\left``/``\\right`` dropped, explicit
   multiplication signs dropped (implicit product), relation commands mapped to one sign, decimal commas to points,
   ``d``/``Δ`` before a symbol as operators, braces kept only where they carry structure, the trailing punctuation
   removed. ``shape_key`` renames the identifiers (a letter with its accents, primes and subscript) in order of first
   occurrence (``v1, v2 …``). *Trivial* formulas are not grouped: no relation sign, fewer than two identifiers or
   ``formula_min_tokens`` tokens, or notation without any operation (``i = 1, 2, …, n``, ``σ2 = σ3``). Groups: equal
   ``latex_key`` (``LATEX``); for *distinctive* formulas (``formula_shape_min_tokens`` tokens,
   ``formula_shape_min_ids`` identifiers, a structural operation) equal ``shape_key`` (``LATEX_RENAMED`` — the same law
   in another notation) unless the structure is generic (one source writes two different formulas with it; no
   symbol kept in place and no agreeing «где …» definitions) or the definitions contradict; ``LATEX_NEAR`` — OCR
   variants with the same printed number on pages that share a text passage.
4. **Assignment and clusters.** Figures and tables: for every object and every other source only the best verified
   partner is kept, and only when the choice is mutual (a panel «а» is not paired with the panel «б» of the same
   figure in a copy); clusters are connected components. Formulas: connected groups of the three links.
5. **Kind**, the first rule that applies: ``SAME_WORK_COPY`` — all member sources are copies of one work;
   ``BOILERPLATE`` — template objects (figures: caption-less logos, badges and ornaments — small, or on a first/last
   page — or caption-less images in ``fig_boilerplate_min_works`` works; tables: a header-only template in >= 3
   works); ``REDRAWN`` — only ``CAPTION`` evidence links different works; ``REPRINT`` — all member works belong to one
   *publication group*: works joined by reprinted text (``source_overlap`` of the part ``duplicates``: relation
   REPRINT or SHARED_ABSTRACT, or >= ``reprint_min_shared_text`` shared passages) or by curated relations
   (``work_relations`` ABSTRACT_OF / COMPANION_OF, ``source_relations`` CONTAINS_COPY_OF / DERIVED_FROM /
   SHARES_PAGES_WITH); otherwise the object is reused in independent publications: ``REUSED_FIGURE``,
   ``REUSED_TABLE``, ``SHARED_FORMULA`` («where is the same law used»).
6. **Primary** as in ``duplicates``: the work with the earliest ``publication_year``; ties → original works before
   containers and derivatives → the object nearer the start of its source → ``work_id``; UNKNOWN (NULL) when a member
   year is missing; not applicable to BOILERPLATE; the copy of a work: FULL_COPY link, anchor source, ``source_id``.

The layer is DERIVED navigation (``AUTO_EXTRACTED_UNREVIEWED``): «primary» orders copies in a dossier, it is never a
claim of authorship or priority; a formula group says where the same written form occurs, not that a law was
checked.
"""
from __future__ import annotations

import hashlib
import logging
import math
import multiprocessing
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from itertools import combinations
from typing import Any, Iterable, Sequence

import numpy as np
import pyarrow as pa

from vkm_corpus.navigation import duplicates as dup
from vkm_corpus.navigation import ids as nav_ids

log = logging.getLogger(__name__)

RULE_VERSION = nav_ids.RULE_VERSIONS["object_duplicates"]
HASH_RULE = "vkm-figure-hash-v1"
OBJECT_TYPES: tuple[str, ...] = ("FIGURE", "TABLE", "FORMULA")
KINDS: tuple[str, ...] = ("SAME_WORK_COPY", "REPRINT", "REUSED_FIGURE", "REUSED_TABLE", "SHARED_FORMULA", "REDRAWN",
                          "BOILERPLATE")
REUSED_KIND = {"FIGURE": "REUSED_FIGURE", "TABLE": "REUSED_TABLE", "FORMULA": "SHARED_FORMULA"}
MATCHES: tuple[str, ...] = ("REFERENCE", "IDENTICAL", "IMAGE", "IMAGE_CAPTION", "CAPTION_IMAGE", "CAPTION", "CELLS",
                            "NUMBERS", "LATEX", "LATEX_RENAMED", "LATEX_NEAR")
PRIMARY_RULES = dup.PRIMARY_RULES
D4_NAMES: tuple[str, ...] = ("ID", "ROT90", "ROT180", "ROT270", "TRANSPOSE", "FLIP_H", "ANTI_TRANSPOSE", "FLIP_V")
NOTE = ("DERIVED navigation layer (AUTO_EXTRACTED_UNREVIEWED): figures, tables and formulas that repeat across "
        "sources. 'primary' is the earliest source by publication year (a hint for ordering copies, not a claim of "
        "authorship or priority); a formula group shows where the same written form occurs, not a checked law; "
        "BOILERPLATE is template content with no original.")

DEFAULTS: dict[str, Any] = {
    "object_types": ("FIGURE", "TABLE", "FORMULA"),
    "workers": 0,                       # processes for image hashing; 0 = min(16, CPU count)
    "hash_cache": None,                 # a figure_hashes.parquet of any earlier build (rows reused by artifact id)
    "max_image_pixels": 16_000_000,     # larger images are reduced by an integer factor while decoding
    # figures: hashes and verification (Hamming distances of 64-bit hashes)
    "fig_ink_delta": 32,                # grey-level difference from the border median that counts as ink
    "fig_min_std": 6.0,                 # grey-level std of the 32×32 thumbnail below it: low information
    "fig_min_ink": 0.005,               # share of ink pixels below it: low information
    "fig_candidate_max": 12,
    "fig_strong_max": 4,                # IMAGE: distance <= this ...
    "fig_strong_dhash_max": 14,         # ... and a dHash that agrees
    "fig_medium_max": 8,                # IMAGE_CAPTION: distance <= this, dHash <= fig_medium_dhash_max and context
    "fig_medium_dhash_max": 20,
    "fig_weak_max": 12,                 # CAPTION_IMAGE: distance <= this and caption similarity >= fig_caption_strong
    "fig_caption_k": 5,                 # character shingles of the caption body (the printed number removed)
    "fig_caption_min_shingles": 12,
    "fig_caption_support": 0.5,         # caption similarity that supports an IMAGE_CAPTION pair
    "fig_caption_strong": 0.8,
    "fig_caption_max_df": 200,          # caption shingles in more captions are not indexed for caption pairs
    "fig_caption_context": True,        # CAPTION pairs: equal caption + context, the image not matched
    "aligned_slack_pages": 1,           # a figure may sit one page away from its reprinted paragraph
    "fig_caption_max_distance": 24,     # CAPTION pairs: the images still loosely alike (unrelated images: ~32 ± 4)
    "fig_min_side": 150,                # px: smaller images (icons, legend swatches) match only when identical
    "fig_transform_min_side": 300,      # px: a rotation/mirror is considered only for images this large
    "fig_max_aspect_ratio": 1.3,        # content-box aspect ratios of an image pair (after the transform) agree
    "fig_boilerplate_small": 200,       # px: a caption-less image this small is template-like
    "fig_boilerplate_max_side": 400,    # px: ... or not larger and on the first or last page of its source
    "fig_boilerplate_min_works": 3,     # caption-less, >= half template-like, in this many works: BOILERPLATE
    # tables
    "table_shingle_k": 8,
    "table_header_k": 5,
    "table_max_df": 50,
    "table_min_containment": 0.5,
    "table_min_shared": 40,
    "table_body_min_shingles": 40,
    "table_min_body_containment": 0.5,
    "table_header_only_containment": 0.9,
    "table_number_k": 3,                # numeric channel: windows of 3 consecutive numbers (digits only)
    "table_min_numbers": 6,             # informative numbers a table needs for the numeric channel
    "table_min_numbers_agree": 0.5,     # CELLS pairs whose numbers are known on both sides must agree this much
    "table_min_number_containment": 0.6,
    "table_min_number_shared": 5,
    # formulas
    "formula_min_tokens": 6,
    "formula_shape_min_tokens": 12,
    "formula_shape_min_ids": 3,
    "formula_renamed": True,
    "formula_near": True,               # LATEX_NEAR: the same printed number on pages sharing a text passage
    "formula_near_min_ratio": 0.6,      # token similarity (difflib ratio of canonical tokens) of a LATEX_NEAR pair
    # kinds
    "reprint_min_shared_text": 3,
}
MODEL_CHOICES: dict[str, str] = {
    "figure_image": "page-render crop (FIGURE_CROP), else the native embedded image; decoded with Pillow to grey "
                    "levels (alpha composited on white)",
    "figure_hashes": "exact bin-average reduction (numpy); pHash = 8×8 low-frequency DCT of a 32×32 thumbnail "
                     "against the median of its AC terms; dHash = horizontal gradients of a 9×9 thumbnail; "
                     "content box = rows/columns with >= 0.2 % ink (|grey - border median| > fig_ink_delta); the "
                     "content-box hashes are also taken under the 8 rotations/mirrors (D4) of the square thumbnail",
    "figure_distance": "min(pHash of the whole crop, pHash of the content box); a rotation or mirror only for two "
                       "images of >= fig_transform_min_side px when it gives a strong match (<= fig_strong_max) with "
                       "agreeing aspect ratios and its own dHash",
    "figure_verification": "the content-box aspect ratios agree within fig_max_aspect_ratio; IMAGE: d <= "
                           "fig_strong_max and dHash <= fig_strong_dhash_max; IMAGE_CAPTION: d <= fig_medium_max, "
                           "dHash <= fig_medium_dhash_max and (caption similarity >= fig_caption_support or the same "
                           "printed number); CAPTION_IMAGE: d <= fig_weak_max and caption similarity >= "
                           "fig_caption_strong; low-information images (grey-level std < fig_min_std, ink < "
                           "fig_min_ink, or max side < fig_min_side px) only when identical",
    "figure_boilerplate": "every member caption-less and template-like (max side < fig_boilerplate_small px, or <= "
                          "fig_boilerplate_max_side px on the first or last page of its source), or caption-less in "
                          ">= fig_boilerplate_min_works works with >= half template-like",
    "figure_caption": "caption body without the printed number, NFKC, case fold, letters and digits, look-alike "
                      "letters folded; shingles of fig_caption_k characters; similarity = |A∩B| / min(|A|, |B|)",
    "figure_caption_context": "CAPTION: caption similarity >= fig_caption_strong while the images do not match "
                              "closely, accepted only with context — two copies of one work with the same printed "
                              "number (unique in each source), or pages within aligned_slack_pages of pages sharing a "
                              "text passage (part duplicates) — and only while the images stay loosely alike (best "
                              "distance <= fig_caption_max_distance); between different works the cluster is REDRAWN",
    "assignment": "figures and tables: per object and other source the best verified partner (evidence tier, "
                  "distance, caption), kept only when mutual; clusters = connected components",
    "table_text": "cells row by row (math spans as plain text), NFKC, case fold, letters and digits only, look-alike "
                  "letters folded; header = the first row (or header_rows rows)",
    "table_verification": "CELLS: containment >= table_min_containment and >= table_min_shared shared shingles, the "
                          "rows below the header contained >= table_min_body_containment (a header-only table: "
                          "containment >= table_header_only_containment), and when both tables carry numbers their "
                          "number windows and their sets of informative numbers agree >= table_min_numbers_agree "
                          "(the same row labels with other values are another table); NUMBERS: the numbers of the "
                          "cells in reading order (digits only, so 16,42 = 16.42 = 1642 after a lost comma), windows of "
                          "table_number_k numbers with >= 2 informative numbers (a fraction or three significant "
                          "digits) that are not an arithmetic progression, containment >= "
                          "table_min_number_containment and >= table_min_number_shared shared windows (tables with >= "
                          "table_min_numbers informative numbers)",
    "formula_near": "LATEX_NEAR: formulas of two sources with the same printed number on pages that share a text "
                    "passage (part duplicates), both non-trivial, token ratio >= formula_near_min_ratio (OCR variants "
                    "of one formula in copies and reprints)",
    "formula_canonical": "parser of the part formulas; Greek variants unified; fonts, spacing, \\left/\\right, "
                         "\\tag and alignment dropped; \\cdot, \\times, * dropped (implicit product); relation "
                         "commands to signs; decimal comma to point; braces kept after \\frac, \\sqrt, \\binom and for "
                         "scripts; trailing punctuation removed",
    "formula_shape": "identifiers (letter + accents + primes + subscript) renamed v1, v2 … in order of first "
                     "occurrence; numbers, functions and operators kept",
    "formula_trivial": "no relation sign, fewer than two identifiers or formula_min_tokens tokens, or no operation "
                       "(notation, ranges, equalities of two symbols)",
    "formula_distinctive": ">= formula_shape_min_tokens tokens, >= formula_shape_min_ids identifiers and a "
                           "structural operation (fraction, power, root, function, sum, integral, derivative)",
    "formula_context": "LATEX_RENAMED links are dropped when the «где …» definitions (formula_symbols) of the two "
                       "forms share no word stem while both have >= 2 definitions, or when the forms keep no symbol "
                       "(coordinates and indices x, y, z, t, r, i, j, k, n, m, ξ, η, ζ, τ do not count) in the same "
                       "place and no definitions agree (a generic form: a polynomial, an additive split, a total "
                       "differential, a gradient); a structure that one source uses for two different canonical "
                       "forms is generic and never merged by renaming",
    "publication_groups": "works joined by text reprints (source_overlap REPRINT / SHARED_ABSTRACT or >= "
                          "reprint_min_shared_text shared passages) and curated relations (ABSTRACT_OF, "
                          "COMPANION_OF; CONTAINS_COPY_OF, DERIVED_FROM, SHARES_PAGES_WITH)",
    "primary": "as in duplicates_v1: earliest publication_year; ties: SECONDARY_WORK_TYPES after original works, then "
               "the object nearer the start of its source, then work_id; UNKNOWN when a member year is missing; the "
               "copy of a work: FULL_COPY link, anchor source, source_id",
}
REPRINT_WORK_RELATIONS = frozenset({"ABSTRACT_OF", "COMPANION_OF"})
REPRINT_SOURCE_RELATIONS = frozenset({"CONTAINS_COPY_OF", "DERIVED_FROM", "SHARES_PAGES_WITH"})
_TEXT_REPRINT_RELATIONS = frozenset({"REPRINT", "SHARED_ABSTRACT"})


# ================================================================================================ shared helpers
def _q(con: Any, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    return dup._rows(con, sql, params)


def _columns(con: Any, table: str) -> set[str]:
    return {r["column_name"] for r in _q(con, "SELECT column_name FROM information_schema.columns "
                                              "WHERE table_schema = 'canonical' AND table_name = ?", [table])}


def _select(con: Any, table: str, want: Sequence[str], where: str = "") -> list[dict[str, Any]]:
    """Rows of ``canonical.<table>`` with the wanted columns (absent columns come back as None)."""
    if not dup._table_exists(con, "canonical", table):
        return []
    have = _columns(con, table)
    cols = ", ".join(f't."{c}"' if c in have else f'NULL AS "{c}"' for c in want)
    return _q(con, f'SELECT {cols} FROM canonical."{table}" t {where}')


def _table_rows(obj: Any) -> list[dict[str, Any]]:
    if obj is None:
        return []
    if isinstance(obj, pa.Table):
        return obj.to_pylist()
    if hasattr(obj, "to_arrow_table"):
        return obj.to_arrow_table().to_pylist()
    if hasattr(obj, "to_pylist"):
        return obj.to_pylist()
    return list(obj)


def norm_key(text: str | None) -> str:
    """The character stream compared across sources (the rule of the part ``duplicates``)."""
    return dup.norm_key(text)


def shingle_set(text: str | None, k: int) -> frozenset[str]:
    s = norm_key(text)
    if len(s) < k:
        return frozenset([s]) if s else frozenset()
    return frozenset(s[i:i + k] for i in range(len(s) - k + 1))


def containment(a: frozenset | set, b: frozenset | set) -> float | None:
    if not a or not b:
        return None
    return len(a & b) / min(len(a), len(b))


_NUM_RX = re.compile(r"(\d+(?:\s*[.,\-–]\s*\d+)*)\s*([a-zа-я](?![a-zа-я]))?", re.I)
_LABEL_HEAD = re.compile(r"^\s*(?:рис(?:унок|унки)?|рис|fig(?:ure|s)?|черт(?:еж|\.)?|схема|граф(?:ик)?|табл(?:ица)?|"
                         r"table|tab)\b\.?\s*", re.I)


def label_number(label: str | None) -> str | None:
    """The printed number of a label or caption start: «Рис. 3.12.» → ``3.12``, «Table 10.5» → ``10.5``."""
    if not label:
        return None
    t = unicodedata.normalize("NFKC", label).strip()
    m = _LABEL_HEAD.match(t)
    body = t[m.end():] if m else t
    n = _NUM_RX.match(body)
    if not n:
        return None
    num = re.sub(r"\s+", "", n.group(1)).replace(",", ".").replace("–", "-")
    return num + (n.group(2) or "").lower()


def caption_body(caption: str | None) -> str:
    """The caption without its leading label («Рис. 3.1.», «Table 2 —»)."""
    t = unicodedata.normalize("NFKC", caption or "").strip()
    m = _LABEL_HEAD.match(t)
    if not m:
        return t
    rest = t[m.end():]
    n = _NUM_RX.match(rest)
    if n:
        rest = rest[n.end():]
    return re.sub(r"^[\s.:—–\-)]+", "", rest)


# ================================================================================================ sources, groups
class _UF:
    def __init__(self) -> None:
        self.p: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if str(rb) < str(ra):
                ra, rb = rb, ra
            self.p[rb] = ra


def publication_groups(con: Any, sources: dict[str, dict[str, Any]], source_overlap: Any = None,
                       min_shared_text: int = 3) -> tuple[dict[str, str], dict[str, Any]]:
    """Work → publication group (the smallest work id of the group) and the links used (counts)."""
    uf = _UF()

    def work(s: str) -> str:
        return (sources.get(s) or {}).get("work_id") or f"(source){s}"

    for s in sources:
        uf.find(work(s))
    info: dict[str, Any] = {"text_pairs": 0, "curated_work_relations": 0, "curated_source_relations": 0,
                            "text_relations": "UNAVAILABLE" if source_overlap is None else "USED"}
    for r in _table_rows(source_overlap):
        rel = r.get("relation")
        if rel in _TEXT_REPRINT_RELATIONS or int(r.get("n_shared_content") or 0) >= int(min_shared_text):
            wa, wb = work(r["source_a"]), work(r["source_b"])
            if wa != wb:
                uf.union(wa, wb)
                info["text_pairs"] += 1
    if dup._table_exists(con, "canonical", "work_relations"):
        cols = _columns(con, "work_relations")
        a = next((c for c in ("from_work_id", "work_id", "subject_work_id") if c in cols), None)
        b = next((c for c in ("to_work_id", "related_work_id", "object_work_id") if c in cols), None)
        if a and b and "relation" in cols:
            for r in _q(con, f'SELECT "{a}" AS a, "{b}" AS b, relation FROM canonical.work_relations'):
                if r["relation"] in REPRINT_WORK_RELATIONS and r["a"] and r["b"]:
                    uf.union(r["a"], r["b"])
                    info["curated_work_relations"] += 1
    if dup._table_exists(con, "canonical", "source_relations"):
        cols = _columns(con, "source_relations")
        a = next((c for c in ("from_source_id", "source_id", "subject_source_id") if c in cols), None)
        b = next((c for c in ("to_source_id", "related_source_id", "object_source_id") if c in cols), None)
        if a and b and "relation" in cols:
            for r in _q(con, f'SELECT "{a}" AS a, "{b}" AS b, relation FROM canonical.source_relations'):
                if r["relation"] in REPRINT_SOURCE_RELATIONS and r["a"] and r["b"]:
                    uf.union(work(r["a"]), work(r["b"]))
                    info["curated_source_relations"] += 1
    groups = {w: uf.find(w) for w in list(uf.p)}
    info["groups_with_several_works"] = sum(1 for n in Counter(groups.values()).values() if n > 1)
    return groups, info


def _page_index(con: Any) -> dict[str, int]:
    return {r["page_id"]: int(r["page_index"] or 0) for r in _q(con, "SELECT page_id, page_index FROM canonical.pages")}


# ================================================================================================ formulas
from vkm_corpus.navigation import formulas as _fm  # noqa: E402  (the LaTeX reader of the part formulas)

_OP_MAP: dict[str, str] = {
    "\\le": "≤", "\\leq": "≤", "\\leqslant": "≤", "\\ge": "≥", "\\geq": "≥", "\\geqslant": "≥", "\\ne": "≠",
    "\\neq": "≠", "\\approx": "≈", "\\simeq": "≃", "\\cong": "≅", "\\equiv": "≡", "\\sim": "~", "\\propto": "∝",
    "\\to": "→", "\\rightarrow": "→", "\\Rightarrow": "⇒", "\\infty": "∞", "\\partial": "∂", "\\nabla": "∇",
    "\\pm": "±", "\\mp": "∓", "\\sum": "∑", "\\prod": "∏", "\\int": "∫", "\\iint": "∬", "\\iiint": "∭",
    "\\oint": "∮", "\\sqrt": "√", "\\dfrac": "\\frac", "\\tfrac": "\\frac", "\\cfrac": "\\frac", "\\div": "÷",
    "\\circ": "°", "\\degree": "°", "\\prime": "′", "\\lbrace": "\\{", "\\rbrace": "\\}", "\\langle": "⟨",
    "\\rangle": "⟩", "\\lvert": "|", "\\rvert": "|", "\\vert": "|", "\\mid": "|", "\\|": "‖", "\\Vert": "‖",
    "\\lVert": "‖", "\\rVert": "‖", "\\ldots": "…", "\\cdots": "…", "\\dots": "…", "\\vdots": "…", "\\%": "%",
    "\\in": "∈", "\\lim": "lim", "−": "-", "–": "-", "—": "-", "∗": "*", "Σ": "∑", "\\colon": ":",
    "\\ll": "≪", "\\gg": "≫", "\\cap": "∩", "\\cup": "∪", "\\subset": "⊂", "\\forall": "∀", "\\exists": "∃",
}
_DROP_OPS = frozenset({"\\cdot", "\\times", "*", "·", "×", "\\ast", "∙", "\\bullet", "\\,", "\\;", "\\:", "\\!",
                       "\\ ", "\\quad", "\\qquad", "~", "\\enspace", "\\thinspace", "\\medspace", "\\thickspace",
                       "\\displaystyle", "\\textstyle", "\\scriptstyle", "\\limits", "\\nolimits", "\\begin", "\\end",
                       "&", "\\hline", "\\mathbb", "\\cdotp", "\\centerdot", "\\big", "\\middle", "\\nonumber"})
_ARG_CMDS = frozenset({"\\frac", "\\binom", "√", "\\sqrt", "\\overset", "\\underset", "\\stackrel", "\\overbrace",
                       "\\underbrace", "\\dbinom", "\\tbinom"})
_REL_SIGNS = frozenset({"=", "≈", "≤", "≥", "<", ">", "≠", "≡", "~", "∝", "≃", "≅"})
_STRUCT = frozenset({"\\frac", "√", "∑", "∏", "∫", "∬", "∭", "∮", "∂", "∇", "lim"})
_ARITH = frozenset({"+", "-", "/", "±", "∓", "÷"}) | _STRUCT
_LINE = frozenset({"\\\\", "\\cr", "\\newline"})
_TRAIL = frozenset({",", ".", ";", ":"})
# coordinates and indices anchor nothing: «∂W/∂x = g_x» and «∂g_x/∂x = G_xx» share only x, y, z
_WEAK_ANCHORS = frozenset({"x", "y", "z", "t", "r", "i", "j", "k", "n", "m", "ξ", "η", "ζ", "τ"})


def _is_digit_op(a: Any) -> bool:
    return a.kind == "op" and len(a.text) == 1 and a.text.isdigit()


def _is_decimal_sep(a: Any) -> bool:
    """``.``, ``,`` or a braced ``{,}`` (the Russian decimal comma in LaTeX)."""
    if a.sub or a.sup or a.primes:
        return False
    if a.kind == "op":
        return a.text in (".", ",")
    return a.kind == "grp" and len(a.kids or ()) == 1 and a.kids[0].kind == "op" and a.kids[0].text in (".", ",")


def _is_one_group(tokens: list[tuple[str, str]]) -> bool:
    """The tokens are exactly one braced group (``{ … }``)."""
    if len(tokens) < 2 or tokens[0][0] != "open" or tokens[-1][0] != "close":
        return False
    depth = 0
    for k, (typ, _) in enumerate(tokens):
        if typ in ("open", "sup", "sub"):
            depth += 1
        elif typ == "close":
            depth -= 1
            if depth == 0 and k != len(tokens) - 1:
                return False
    return True


def _scripts(a: Any, out: list[tuple[str, str]]) -> None:
    if a.sub:
        out.append(("sub", "_{"))
        _ser(a.sub, out)
        out.append(("close", "}"))
    if a.sup:
        out.append(("sup", "^{"))
        _ser(a.sup, out)
        out.append(("close", "}"))
    if a.primes:
        out.append(("op", "′" * a.primes))


def _ser(atoms: list[Any] | None, out: list[tuple[str, str]]) -> None:
    """Canonical tokens (type, text) of parsed atoms; types: id, num, op, func, word, open, close, sup, sub.

    Braces are kept only around groups of two or more tokens (``\\frac{ab}{c}`` → ``\\frac { a b } c``), so a
    brace-free argument and a braced one read the same (``\\frac ab`` = ``\\frac{a}{b}``)."""
    seq = [a for a in atoms or () if not (a.kind == "op" and (not a.text or a.text.isspace()))]
    i = 0
    while i < len(seq):
        a = seq[i]
        if _is_digit_op(a):                                 # a number: digits with decimal points or commas
            j, num = i, []
            while j < len(seq):
                c = seq[j]
                if _is_digit_op(c):
                    num.append(c.text)
                    j += 1
                    if c.sub or c.sup or c.primes:           # 10^{-3}: the script closes the number
                        break
                elif num and _is_decimal_sep(c) and j + 1 < len(seq) and _is_digit_op(seq[j + 1]):
                    num.append(".")
                    j += 1
                else:
                    break
            out.append(("num", "".join(num)))
            _scripts(seq[j - 1], out)
            i = j
            continue
        if a.kind == "grp":
            one = _fm._single_id(a.kids)
            if one is not None and (a.sub or a.sup or a.primes):   # {\sigma}_{1} = \sigma_{1}
                merged = _fm._Atom("id", one.text)
                merged.marks, merged.primes = one.marks, one.primes + a.primes
                merged.sub = (one.sub or []) + (a.sub or []) or None
                merged.sup = (one.sup or []) + (a.sup or []) or None
                _ser([merged], out)
                i += 1
                continue
            inner: list[tuple[str, str]] = []
            _ser(a.kids, inner)
            if (len(inner) > 1 or (inner and (a.sub or a.sup))) and not _is_one_group(inner):
                out.append(("open", "{"))
                out.extend(inner)
                out.append(("close", "}"))
            else:
                out.extend(inner)
            _scripts(a, out)
        elif a.kind == "id" and a.text in ("d", "Δ") and not (a.sub or a.sup or a.marks or a.primes) \
                and i + 1 < len(seq) and _fm._starts_with_id(seq[i + 1]):
            out.append(("op", a.text))                       # a differential or an increment: d x, Δ t
        elif a.kind == "id":
            name = a.text.translate(_fm._UNI_FOLD) + a.marks + "′" * a.primes
            if a.sub:
                sub: list[tuple[str, str]] = []
                _ser(a.sub, sub)
                name += "_{" + " ".join(t for _, t in sub) + "}"
            out.append(("id", name))
            if a.sup:
                out.append(("sup", "^{"))
                _ser(a.sup, out)
                out.append(("close", "}"))
        elif a.kind in ("func", "word"):
            txt = a.text if a.kind == "func" else a.text.lower()
            out.append((a.kind, _OP_MAP.get("\\" + txt, txt) if a.kind == "func" else txt))
            _scripts(a, out)
        else:                                               # an operator (possibly with limits or a power)
            t = _OP_MAP.get(a.text, a.text)
            if a.text in _LINE:
                out.append(("op", ";"))
            elif t not in _DROP_OPS and a.text not in _DROP_OPS:
                out.append(("op", t))
            _scripts(a, out)
        i += 1


def canonical_formula(latex: str | None) -> dict[str, Any]:
    """Canonical form of a LaTeX formula: ``latex_key``, ``shape_key`` (identifiers renamed), their hashes, the
    identifiers in order, token counts, ``trivial`` / ``distinctive`` flags (thresholds of ``DEFAULTS``)."""
    return _canonical(latex, DEFAULTS)


def _canonical(latex: str | None, p: dict[str, Any]) -> dict[str, Any]:
    atoms = _fm._LatexParser(latex or "").parse()
    toks: list[tuple[str, str]] = []
    _ser(atoms, toks)
    while toks and toks[-1][0] == "op" and toks[-1][1] in _TRAIL:
        toks.pop()
    while toks and toks[0][0] == "op" and toks[0][1] in _TRAIL:
        toks.pop(0)
    names: dict[str, str] = {}
    shape = []
    for typ, t in toks:
        if typ == "id":
            shape.append(names.setdefault(t, f"v{len(names) + 1}"))
        else:
            shape.append(t)
    latex_key = " ".join(t for _, t in toks)
    shape_key = " ".join(shape)
    real = [(typ, t) for typ, t in toks if typ not in ("open", "close")]
    n_tokens = len(real)
    has_rel = any(typ == "op" and t in _REL_SIGNS for typ, t in real)
    n_struct = sum(1 for typ, t in real if t in _STRUCT or typ in ("sup", "func"))
    ids = [t for typ, t in real if typ == "id"]
    arith = n_struct > 0 or any(typ == "op" and t in _ARITH for typ, t in real) or _implicit_product(real)
    trivial_reason = None
    if not real:
        trivial_reason = "EMPTY"
    elif not has_rel:
        trivial_reason = "NO_RELATION"
    elif len(names) < 2:
        trivial_reason = "FEW_IDENTIFIERS"
    elif n_tokens < int(p["formula_min_tokens"]):
        trivial_reason = "SHORT"
    elif not arith:
        trivial_reason = "NOTATION"
    distinctive = (trivial_reason is None and n_tokens >= int(p["formula_shape_min_tokens"])
                   and len(names) >= int(p["formula_shape_min_ids"]) and n_struct >= 1)
    return {"latex_key": latex_key, "shape_key": shape_key,
            "key_hash": hashlib.sha256(latex_key.encode("utf-8")).hexdigest()[:16],
            "shape_hash": hashlib.sha256(shape_key.encode("utf-8")).hexdigest()[:16],
            "identifiers": list(names), "n_tokens": n_tokens, "n_ids": len(names), "n_struct": n_struct,
            "has_relation": has_rel, "trivial": trivial_reason is not None, "trivial_reason": trivial_reason,
            "distinctive": distinctive, "n_occurrences_ids": len(ids)}


def _implicit_product(real: list[tuple[str, str]]) -> bool:
    """Two operands side by side (``E ε``, ``2 x``) — an implicit product."""
    operand = {"id", "num", "func"}
    return any(a[0] in operand and b[0] in ("id", "func") for a, b in zip(real, real[1:]))


# ================================================================================================ figures: hashing
_BITS = np.left_shift(np.uint64(1), np.arange(64, dtype=np.uint64))


def _dct_matrix(n: int = 32, k: int = 8) -> np.ndarray:
    i = np.arange(n)
    m = np.cos(np.pi * (2 * i[None, :] + 1) * np.arange(k)[:, None] / (2 * n))
    m[0] *= math.sqrt(1.0 / n)
    m[1:] *= math.sqrt(2.0 / n)
    return m


_DCT = _dct_matrix()


def _pack(bits: np.ndarray) -> int:
    b = np.asarray(bits, dtype=bool).reshape(-1)
    return int(np.bitwise_or.reduce(np.where(b, _BITS[:b.size], np.uint64(0)))) if b.size else 0


def bin_resize(a: np.ndarray, oh: int, ow: int) -> np.ndarray:
    """Exact bin averaging of a 2-D array to ``oh × ow`` (row/column bins of near-equal size; small inputs are first
    repeated) — deterministic and independent of any resampling library."""
    a = np.asarray(a)
    h, w = a.shape
    if h < oh or w < ow:
        a = np.repeat(np.repeat(a, math.ceil(oh / h), axis=0), math.ceil(ow / w), axis=1)
        h, w = a.shape
    rows = (np.arange(oh) * h) // oh
    cols = (np.arange(ow) * w) // ow
    s = np.add.reduceat(np.add.reduceat(a, rows, axis=0, dtype=np.float64), cols, axis=1)
    return s / np.outer(np.diff(np.append(rows, h)), np.diff(np.append(cols, w)))


def phash(thumb32: np.ndarray) -> int:
    d = (_DCT @ np.asarray(thumb32, dtype=np.float64) @ _DCT.T).reshape(-1)
    return _pack(d > np.median(d[1:]))


def dhash(thumb9: np.ndarray) -> int:
    t = np.asarray(thumb9, dtype=np.float64)[:8]
    return _pack(t[:, 1:] > t[:, :-1])


def d4(t: np.ndarray) -> list[np.ndarray]:
    """The 8 rotations/mirrors of a square array, in the order of ``D4_NAMES``."""
    r = [t, np.rot90(t, 1), np.rot90(t, 2), np.rot90(t, 3)]
    return r + [x.T for x in r]


def _small_view(g: np.ndarray, side: int = 512) -> tuple[np.ndarray, float]:
    h, w = g.shape
    sc = min(1.0, float(side) / max(h, w))
    if sc < 1.0:
        return bin_resize(g, max(1, round(h * sc)), max(1, round(w * sc))), sc
    return np.asarray(g, dtype=np.float64), 1.0


def _ink(small: np.ndarray, ink_delta: float) -> np.ndarray:
    border = np.concatenate([small[0], small[-1], small[:, 0], small[:, -1]])
    return np.abs(small - np.median(border)) > ink_delta


def content_box(g: np.ndarray, ink_delta: float = 32, line_share: float = 0.002) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of the rows/columns carrying ink (grey level differing from the border median), or None."""
    h, w = g.shape
    small, sc = _small_view(g)
    ink = _ink(small, ink_delta)
    rows = np.flatnonzero(ink.mean(axis=1) > line_share)
    cols = np.flatnonzero(ink.mean(axis=0) > line_share)
    if not len(rows) or not len(cols):
        return None
    y0, y1 = int(rows[0] / sc), min(h, int(math.ceil((rows[-1] + 1) / sc)))
    x0, x1 = int(cols[0] / sc), min(w, int(math.ceil((cols[-1] + 1) / sc)))
    return (x0, y0, x1, y1) if x1 - x0 >= 2 and y1 - y0 >= 2 else None


def image_hashes(gray: np.ndarray, ink_delta: float = 32) -> dict[str, Any]:
    """Hashes of a grey-level image (2-D array, 0–255): see the module docstring (figures). The image is never
    copied at full size in floating point (bin sums accumulate in float64 from the integer array)."""
    g = np.asarray(gray)
    h, w = g.shape
    box = content_box(g, ink_delta)
    full32 = bin_resize(g, 32, 32)
    t = g[box[1]:box[3], box[0]:box[2]] if box else g
    t32, t9 = bin_resize(t, 32, 32), bin_resize(t, 9, 9)
    return {"width": int(w), "height": int(h), "phash": phash(full32),
            "phash_trim": [phash(v) for v in d4(t32)], "dhash_trim": [dhash(v) for v in d4(t9)],
            "trim_width": int(box[2] - box[0]) if box else int(w), "trim_height": int(box[3] - box[1]) if box else int(h),
            "ink_share": round(float(_ink(_small_view(g)[0], ink_delta).mean()), 6),
            "thumb_std": round(float(t32.std()), 4)}


def decode_gray(path: str, max_pixels: int = 16_000_000) -> np.ndarray:
    """Grey levels (uint8) of an image file; alpha is composited on white; huge images are reduced by an integer
    factor. Needs Pillow."""
    from PIL import Image  # noqa: PLC0415

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        w, h = im.size
        if im.format == "JPEG" and w * h > max_pixels:
            im.draft("L", (w // 2, h // 2))
        im.load()
        if im.size[0] * im.size[1] > max_pixels:
            f = math.ceil(math.sqrt(im.size[0] * im.size[1] / max_pixels))
            im = im.reduce(f)
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            bg.alpha_composite(rgba)
            im = bg
        elif im.mode in ("I;16", "I;16B", "I;16L", "I"):
            arr = np.asarray(im, dtype=np.float64)
            top = arr.max() or 1.0
            return np.clip(arr * (255.0 / top), 0, 255).astype(np.uint8)
        return np.asarray(im.convert("L"), dtype=np.uint8)


def _hash_task(task: tuple[str, str, int, float]) -> tuple[str, dict[str, Any] | None, str | None]:
    """Worker: (artifact_id, path, max_pixels, ink_delta) → (artifact_id, hashes or None, error or None)."""
    aid, path, max_pixels, ink_delta = task
    try:
        return aid, image_hashes(decode_gray(path, max_pixels), ink_delta), None
    except Exception as exc:  # noqa: BLE001 — a broken or unsupported file is recorded, never fatal
        return aid, None, f"{type(exc).__name__}: {str(exc)[:160]}"


_REL_PATH = re.compile(r"^[a-z_]+/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.[A-Za-z0-9.]{1,12}$")


_POOL_MIN_TASKS = 64                                  # fewer images are hashed in the calling process


def _hash_images(tasks: list[tuple[str, str, int, float]], workers: int) -> dict[str, tuple[dict | None, str | None]]:
    out: dict[str, tuple[dict | None, str | None]] = {}
    if not tasks:
        return out
    if workers <= 1 or len(tasks) < _POOL_MIN_TASKS:
        for t in tasks:
            aid, res, err = _hash_task(t)
            out[aid] = (res, err)
        return out
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for aid, res, err in ex.map(_hash_task, tasks, chunksize=32):
            out[aid] = (res, err)
    return out


# ================================================================================================ figures: rows
FIG_HASH_SCHEMA = pa.schema([
    ("object_id", pa.string()), ("source_id", pa.string()), ("artifact_id", pa.string()),
    ("artifact_kind", pa.string()), ("status", pa.string()), ("error", pa.string()), ("width", pa.int32()),
    ("height", pa.int32()), ("trim_width", pa.int32()), ("trim_height", pa.int32()), ("ink_share", pa.float64()),
    ("thumb_std", pa.float64()), ("phash", pa.uint64()), ("phash_trim", pa.list_(pa.uint64())),
    ("dhash_trim", pa.list_(pa.uint64())), ("low_information", pa.bool_()), ("hash_rule", pa.string()),
    ("rule_version", pa.string()),
])


def load_figures(con: Any) -> list[dict[str, Any]]:
    rows = _select(con, "figures", ("object_id", "source_id", "page_id", "figure_label", "caption",
                                    "caption_normalized", "layout_class", "image_artifact_id",
                                    "embedded_image_artifact_id"), "ORDER BY object_id")
    return rows


def _artifact_index(con: Any, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    want = sorted({i for i in ids if i})
    if not want or not dup._table_exists(con, "canonical", "artifacts"):
        return {}
    have = _columns(con, "artifacts")
    cols = [c for c in ("artifact_kind", "storage_relpath", "media_type", "image_width_px", "image_height_px")
            if c in have]
    sel = ", ".join(f"any_value(a.{c}) AS {c}" for c in cols)
    name = "_vkm_objdup_artifact_ids"
    con.register(name, pa.table({"artifact_id": want}))
    try:
        rows = _q(con, f"SELECT a.artifact_id, {sel} FROM canonical.artifacts a JOIN {name} w "
                       f"ON w.artifact_id = a.artifact_id GROUP BY a.artifact_id")
    finally:
        con.unregister(name)
    return {r["artifact_id"]: r for r in rows}


def figure_hash_rows(con: Any, figures: list[dict[str, Any]], artifacts_root: str | None, p: dict[str, Any],
                     cache: Any = None, stats: dict[str, Any] | None = None) -> pa.Table:
    """``figure_hashes``: one row per figure (hashes of its image; cached rows reused by artifact id)."""
    st = stats if stats is not None else {}
    idx = _artifact_index(con, [f["image_artifact_id"] for f in figures]
                          + [f["embedded_image_artifact_id"] for f in figures])
    cached: dict[str, dict[str, Any]] = {}
    for r in _table_rows(cache):
        if r.get("hash_rule") == HASH_RULE and r.get("status") == "OK" and r.get("artifact_id"):
            cached[r["artifact_id"]] = r
    choice: dict[str, tuple[str | None, str | None]] = {}
    for f in figures:
        aid, kind = None, None
        for col, k in (("image_artifact_id", "FIGURE_CROP"), ("embedded_image_artifact_id", "EMBEDDED_IMAGE")):
            if f.get(col):
                aid, kind = f[col], (idx.get(f[col]) or {}).get("artifact_kind") or k
                break
        choice[f["object_id"]] = (aid, kind)
    need = sorted({a for a, _ in choice.values() if a and a not in cached})
    tasks, status_of = [], {}
    root = os.fspath(artifacts_root) if artifacts_root else None
    for aid in need:
        rel = (idx.get(aid) or {}).get("storage_relpath")
        if not rel or not _REL_PATH.match(rel):
            status_of[aid] = ("NO_STORED_FILE", None)
        elif root is None:
            status_of[aid] = ("NO_ARTIFACT_ROOT", None)
        else:
            path = os.path.join(root, *rel.split("/"))
            if not os.path.isfile(path):
                status_of[aid] = ("MISSING_FILE", None)
            else:
                tasks.append((aid, path, int(p["max_image_pixels"]), float(p["fig_ink_delta"])))
    workers = int(p["workers"]) or min(16, os.cpu_count() or 1)
    t0 = time.monotonic()
    fresh: dict[str, tuple[dict | None, str | None]] = {}
    if tasks:
        try:
            import PIL  # noqa: F401, PLC0415
        except ImportError:
            for t in tasks:
                status_of[t[0]] = ("NO_DECODER", "Pillow is not installed")
            tasks = []
        fresh = _hash_images(tasks, workers)
    st.update({"hash_seconds": round(time.monotonic() - t0, 3), "hash_workers": workers, "hashed": len(fresh),
               "cached": sum(1 for a, _ in choice.values() if a in cached)})
    rows: dict[str, list] = {f.name: [] for f in FIG_HASH_SCHEMA}
    for f in figures:
        aid, kind = choice[f["object_id"]]
        h, status, err = None, "NO_ARTIFACT", None
        if aid in cached:
            h, status = cached[aid], "OK"
        elif aid in fresh:
            h, err = fresh[aid]
            status = "OK" if h is not None else "DECODE_ERROR"
        elif aid in status_of:
            status, err = status_of[aid]
        low = None
        if h is not None:
            low = bool(float(h["thumb_std"]) < float(p["fig_min_std"]) or float(h["ink_share"]) < float(p["fig_min_ink"]))
        rows["object_id"].append(f["object_id"])
        rows["source_id"].append(f["source_id"])
        rows["artifact_id"].append(aid)
        rows["artifact_kind"].append(kind if aid else None)
        rows["status"].append(status)
        rows["error"].append(err)
        for c in ("width", "height", "trim_width", "trim_height", "ink_share", "thumb_std", "phash", "phash_trim",
                  "dhash_trim"):
            rows[c].append(None if h is None else h[c])
        rows["low_information"].append(low)
        rows["hash_rule"].append(HASH_RULE if h is not None else None)
        rows["rule_version"].append(RULE_VERSION)
    return pa.table(rows, schema=FIG_HASH_SCHEMA)


def _popcount(x: np.ndarray) -> np.ndarray:
    return np.bitwise_count(x).astype(np.int16)


def figure_candidates(ph: np.ndarray, trim: np.ndarray, src: np.ndarray, max_d: int, block: int = 256
                      ) -> list[tuple[int, int]]:
    """Pairs (i < j) of different sources whose best distance (whole, content box, content box under D4) is
    <= ``max_d`` — an exact scan of all pairs in blocks."""
    n = len(ph)
    out: list[tuple[int, int]] = []
    for lo in range(0, n, block):
        hi = min(n, lo + block)
        d = np.minimum(_popcount(ph[lo:hi, None] ^ ph[None, :]), _popcount(trim[lo:hi, 0, None] ^ trim[None, :, 0]))
        for g in range(1, 8):
            d = np.minimum(d, _popcount(trim[lo:hi, 0, None] ^ trim[None, :, g]))
        ii, jj = np.nonzero(d <= max_d)
        ii = ii + lo
        keep = (jj > ii) & (src[ii] != src[jj])
        out.extend(zip(ii[keep].tolist(), jj[keep].tolist()))
    return out


_SWAP_AXES = frozenset({1, 3, 4, 6})       # ROT90, ROT270, TRANSPOSE, ANTI_TRANSPOSE exchange width and height


class _FigArrays:
    """Hash arrays of the hashed figures: whole-crop pHash, content-box pHash/dHash under D4, content-box aspect
    ratio and the larger side of the image (px)."""

    def __init__(self, rows: list[dict[str, Any]], p: dict[str, Any]):
        n = len(rows)
        self.p = p
        self.ph = np.array([r["phash"] for r in rows], dtype=np.uint64)
        self.trim = np.array([r["phash_trim"] for r in rows], dtype=np.uint64).reshape(n, 8)
        self.dh = np.array([r["dhash_trim"] for r in rows], dtype=np.uint64).reshape(n, 8)
        self.ar = np.array([max(1, r["trim_width"] or 1) / max(1, r["trim_height"] or 1) for r in rows], dtype=float)
        self.side = np.array([max(int(r["width"] or 0), int(r["height"] or 0)) for r in rows], dtype=np.int64)

    def aspect_ok(self, i: int, j: int, g: int = 0) -> bool:
        a, b = self.ar[i], (1.0 / self.ar[j] if g in _SWAP_AXES else self.ar[j])
        return max(a, b) / max(1e-9, min(a, b)) <= float(self.p["fig_max_aspect_ratio"])

    def distance(self, i: int, j: int) -> tuple[int, int, str]:
        """(distance, dHash distance, transform) used for verification: identity (whole crop or content box) unless
        a rotation/mirror of two large images gives a strong match with agreeing aspect ratios and its own dHash."""
        p = self.p
        d_full = int(np.bitwise_count(self.ph[i] ^ self.ph[j]))
        d_trim = int(np.bitwise_count(self.trim[i, 0] ^ self.trim[j, 0]))
        best = (min(d_full, d_trim), int(np.bitwise_count(self.dh[i, 0] ^ self.dh[j, 0])), "ID")
        big = min(self.side[i], self.side[j]) >= int(p["fig_transform_min_side"])
        if best[0] > int(p["fig_strong_max"]) and big:
            for g in range(1, 8):
                dg = int(np.bitwise_count(self.trim[i, 0] ^ self.trim[j, g]))
                dhg = int(np.bitwise_count(self.dh[i, 0] ^ self.dh[j, g]))
                if (dg <= int(p["fig_strong_max"]) and dhg <= int(p["fig_strong_dhash_max"]) and dg < best[0]
                        and self.aspect_ok(i, j, g)):
                    best = (dg, dhg, D4_NAMES[g])
        return best

    def best_distance(self, i: int, j: int) -> tuple[int, int, str]:
        """The smallest distance over the whole crop, the content box and its 8 rotations/mirrors (for reporting)."""
        best = (int(np.bitwise_count(self.ph[i] ^ self.ph[j])), int(np.bitwise_count(self.dh[i, 0] ^ self.dh[j, 0])),
                "ID")
        for g in range(8):
            dg = int(np.bitwise_count(self.trim[i, 0] ^ self.trim[j, g]))
            if dg < best[0]:
                best = (dg, int(np.bitwise_count(self.dh[i, 0] ^ self.dh[j, g])), D4_NAMES[g])
        return best


# ================================================================================================ assignment
def mutual_best(edges: dict[tuple[int, int], tuple], src: Sequence[Any]) -> list[tuple[int, int]]:
    """Edges kept when each end is the other's best partner in the other's source (ties kept). ``edges`` maps
    (i, j) to a score tuple (lower is better)."""
    best: dict[tuple[int, Any], tuple] = {}
    for (i, j), s in edges.items():
        for a, b in ((i, j), (j, i)):
            k = (a, src[b])
            if k not in best or s < best[k]:
                best[k] = s
    return sorted((i, j) for (i, j), s in edges.items() if best[(i, src[j])] == s and best[(j, src[i])] == s)


def _components(n: int, edges: Iterable[tuple[int, int]]) -> list[list[int]]:
    uf = _UF()
    touched = set()
    for i, j in edges:
        uf.union(i, j)
        touched.update((i, j))
    comp: dict[int, list[int]] = defaultdict(list)
    for v in sorted(touched):
        comp[uf.find(v)].append(v)
    return [sorted(v) for v in comp.values()]


# ================================================================================================ tables
def _table_texts(t: dict[str, Any]) -> tuple[str, str]:
    """(header text, body text) of a table: cells row by row; header = header_rows rows or the first row."""
    cells = t.get("cells") or []
    if cells:
        by_row: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for c in cells:
            txt = _fm.to_plain_text(c.get("text") or "") if "$" in (c.get("text") or "") else (c.get("text") or "")
            by_row[int(c.get("row") or 0)].append((int(c.get("col") or 0), txt))
        rows = [" | ".join(x for _, x in sorted(v)) for _, v in sorted(by_row.items())]
        nh = int(t.get("header_rows") or 0) or 1
        nh = min(nh, max(1, len(rows) - 1)) if len(rows) > 1 else 1      # a single row is a header only
        return "\n".join(rows[:nh]), "\n".join(rows[nh:])
    text = t.get("normalized_text") or ""
    return "", _fm.to_plain_text(text) if "$" in text else text


def load_tables(con: Any) -> list[dict[str, Any]]:
    return _select(con, "tables", ("object_id", "source_id", "page_id", "table_label", "caption",
                                   "caption_normalized", "cells", "normalized_text", "header_rows", "n_rows",
                                   "n_cols"), "ORDER BY object_id")


def _shared_counts(keys: list[np.ndarray], src: np.ndarray, max_df: int) -> dict[tuple[int, int], int]:
    """Shared-key counts of all pairs of objects of different sources through an inverted index (keys in more than
    ``max_df`` objects are skipped)."""
    owner = np.concatenate([np.full(len(k), i, dtype=np.int64) for i, k in enumerate(keys)]) if keys else \
        np.zeros(0, dtype=np.int64)
    allk = np.concatenate(keys) if keys else np.zeros(0, dtype=np.uint64)
    order = np.argsort(allk, kind="stable")
    ks, ow = allk[order], owner[order]
    if not len(ks):
        return {}
    bounds = np.concatenate([[0], np.flatnonzero(ks[1:] != ks[:-1]) + 1, [len(ks)]])
    starts, sizes = bounds[:-1], np.diff(bounds)
    n = np.int64(len(keys))
    codes = []
    for s in range(2, int(max_df) + 1):                    # all groups of one size at once
        g = starts[sizes == s]
        if not len(g):
            continue
        mem = np.sort(ow[g[:, None] + np.arange(s)[None, :]], axis=1)
        for x, y in combinations(range(s), 2):
            a, b = mem[:, x], mem[:, y]
            ok = (a != b) & (src[a] != src[b])
            codes.append(a[ok] * n + b[ok])
    if not codes:
        return {}
    uniq, cnt = np.unique(np.concatenate(codes), return_counts=True)
    return {(int(k // n), int(k % n)): int(c) for k, c in zip(uniq, cnt)}


# ================================================================================================ clusters
CLUSTERS_SCHEMA = pa.schema([
    ("cluster_id", pa.string()), ("object_type", pa.string()), ("kind", pa.string()), ("match_basis", pa.string()),
    ("n_members", pa.int32()), ("n_sources", pa.int32()), ("n_works", pa.int32()), ("n_groups", pa.int32()),
    ("source_ids", pa.list_(pa.string())), ("work_ids", pa.list_(pa.string())),
    ("primary_source_id", pa.string()), ("primary_object_id", pa.string()), ("primary_work_id", pa.string()),
    ("primary_year", pa.int16()), ("primary_rule", pa.string()), ("reference_source_id", pa.string()),
    ("reference_object_id", pa.string()), ("label", pa.string()), ("key_hash", pa.string()),
    ("n_edges", pa.int32()), ("min_similarity", pa.float64()), ("template_share", pa.float64()),
    ("rule_version", pa.string()),
])
MEMBERS_SCHEMA = pa.schema([
    ("cluster_id", pa.string()), ("object_id", pa.string()), ("object_type", pa.string()), ("kind", pa.string()),
    ("source_id", pa.string()), ("work_id", pa.string()), ("year", pa.int16()), ("page_id", pa.string()),
    ("page_index", pa.int32()), ("label", pa.string()), ("match", pa.string()), ("similarity", pa.float64()),
    ("image_distance", pa.int16()), ("dhash_distance", pa.int16()), ("transform", pa.string()),
    ("caption_similarity", pa.float64()), ("cell_containment", pa.float64()), ("number_containment", pa.float64()),
    ("header_similarity", pa.float64()), ("equation_number", pa.string()), ("section_id", pa.string()),
    ("context", pa.string()), ("is_primary", pa.bool_()), ("is_reference", pa.bool_()),
    ("rule_version", pa.string()),
])
FORMULA_KEYS_SCHEMA = pa.schema([
    ("formula_id", pa.string()), ("source_id", pa.string()), ("work_id", pa.string()), ("year", pa.int16()),
    ("page_id", pa.string()), ("page_index", pa.int32()), ("formula_kind", pa.string()),
    ("equation_number", pa.string()), ("section_id", pa.string()), ("latex_key", pa.string()),
    ("shape_key", pa.string()), ("key_hash", pa.string()), ("shape_hash", pa.string()), ("n_tokens", pa.int32()),
    ("n_ids", pa.int32()), ("n_struct", pa.int32()), ("trivial", pa.bool_()), ("trivial_reason", pa.string()),
    ("distinctive", pa.bool_()), ("rule_version", pa.string()),
])


class _Ctx:
    """Sources, works, groups and the primary/kind rules shared by the three object types."""

    def __init__(self, sources: dict[str, dict[str, Any]], groups: dict[str, str]):
        self.sources = sources
        self.groups = groups

    def work(self, s: str) -> str:
        return (self.sources.get(s) or {}).get("work_id") or f"(source){s}"

    def year(self, s: str | None) -> int | None:
        return (self.sources.get(s) or {}).get("year") if s else None

    def group(self, s: str) -> str:
        w = self.work(s)
        return self.groups.get(w, w)

    def rel_pos(self, s: str, page_index: int | None) -> float:
        info = self.sources.get(s) or {}
        first, last = info.get("first", 0), info.get("last", 0)
        return round(((page_index or 0) - first) / max(1, last - first), 4)

    def copy_key(self, s: str) -> tuple:
        info = self.sources.get(s) or {}
        return (dup.LINK_RANK.get(info.get("link_type"), 3), info.get("anchor") != s, s)

    def primary(self, members: list[dict[str, Any]], kind: str) -> tuple[str | None, str, str]:
        """(primary source or None, rule, reference source) of a cluster (rules of ``duplicates_v1``)."""
        by_work: dict[str, list[str]] = defaultdict(list)
        for s in sorted({m["source_id"] for m in members}):
            by_work[self.work(s)].append(s)

        def work_key(w: str) -> tuple:
            y = self.year(by_work[w][0])
            wt = (self.sources.get(by_work[w][0]) or {}).get("work_type")
            pos = min(self.rel_pos(m["source_id"], m.get("page_index")) for m in members
                      if self.work(m["source_id"]) == w)
            return (y is None, y or 0, 1 if wt in dup.SECONDARY_WORK_TYPES else 0, pos, w)

        works = sorted(by_work, key=work_key)
        copies = sorted(by_work[works[0]], key=self.copy_key)
        reference = copies[0]
        if kind == "SAME_WORK_COPY":
            if len(copies) == 1:
                return reference, "SAME_WORK_SOURCE_ID", reference
            k0, k1 = self.copy_key(copies[0]), self.copy_key(copies[1])
            rule = ("SAME_WORK_FULL_COPY" if k0[0] != k1[0] else "SAME_WORK_ANCHOR" if k0[1] != k1[1]
                    else "SAME_WORK_SOURCE_ID")
            return reference, rule, reference
        if kind == "BOILERPLATE":
            return None, "NOT_APPLICABLE", reference
        if any(self.year(m["source_id"]) is None for m in members):
            return None, "UNKNOWN_YEAR", reference
        k0, k1 = work_key(works[0]), work_key(works[1])
        rule = ("EARLIEST_YEAR" if k0[1] != k1[1] else "TIE_WORK_TYPE" if k0[2] != k1[2]
                else "TIE_POSITION" if k0[3] != k1[3] else "TIE_WORK_ID")
        return reference, rule, reference

    def kind(self, object_type: str, members: list[dict[str, Any]], *, template: bool = False,
             redrawn: bool = False) -> str:
        works = {self.work(m["source_id"]) for m in members}
        if len(works) == 1:
            return "SAME_WORK_COPY"
        if template:
            return "BOILERPLATE"
        if redrawn:
            return "REDRAWN"
        if len({self.groups.get(w, w) for w in works}) == 1:
            return "REPRINT"
        return REUSED_KIND[object_type]


def _emit_cluster(ctx: _Ctx, object_type: str, members: list[dict[str, Any]], *, kind: str, match_basis: str,
                  n_edges: int, sim_to_ref: Any, rows_c: dict[str, list], rows_m: dict[str, list],
                  key_hash: str | None = None, template_share: float | None = None) -> str:
    """Append one cluster and its members; ``sim_to_ref(ref_member, member) -> dict`` gives the evidence columns."""
    primary, rule, reference = ctx.primary(members, kind)
    ordered = sorted(members, key=lambda m: (m["source_id"], m.get("page_index") or 0, m["object_id"]))
    ref_m = next(m for m in ordered if m["source_id"] == reference)
    prim_m = next((m for m in ordered if m["source_id"] == primary), None) if primary else None
    cid = nav_ids.object_dup_cluster_id(object_type, (m["object_id"] for m in members))
    sources = sorted({m["source_id"] for m in members})
    works = sorted({ctx.work(s) for s in sources})
    labels = {m.get("label") for m in members}
    sims = []
    for m in ordered:
        ev = {"match": "REFERENCE", "similarity": 1.0} if m is ref_m else sim_to_ref(ref_m, m)
        sims.append(ev.get("similarity"))
        s = m["source_id"]
        rows_m["cluster_id"].append(cid)
        rows_m["object_id"].append(m["object_id"])
        rows_m["object_type"].append(object_type)
        rows_m["kind"].append(kind)
        rows_m["source_id"].append(s)
        rows_m["work_id"].append((ctx.sources.get(s) or {}).get("work_id"))
        rows_m["year"].append(ctx.year(s))
        rows_m["page_id"].append(m.get("page_id"))
        rows_m["page_index"].append(m.get("page_index"))
        rows_m["label"].append(m.get("label"))
        for c in ("match", "similarity", "image_distance", "dhash_distance", "transform", "caption_similarity",
                  "cell_containment", "number_containment", "header_similarity", "context"):
            rows_m[c].append(ev.get(c))
        rows_m["equation_number"].append(m.get("equation_number"))
        rows_m["section_id"].append(m.get("section_id"))
        rows_m["is_primary"].append(bool(primary) and s == primary)
        rows_m["is_reference"].append(m is ref_m)
        rows_m["rule_version"].append(RULE_VERSION)
    rows_c["cluster_id"].append(cid)
    rows_c["object_type"].append(object_type)
    rows_c["kind"].append(kind)
    rows_c["match_basis"].append(match_basis)
    rows_c["n_members"].append(len(members))
    rows_c["n_sources"].append(len(sources))
    rows_c["n_works"].append(len(works))
    rows_c["n_groups"].append(len({ctx.group(s) for s in sources}))
    rows_c["source_ids"].append(sources)
    rows_c["work_ids"].append([w for w in works if not w.startswith("(source)")])
    rows_c["primary_source_id"].append(primary)
    rows_c["primary_object_id"].append(prim_m["object_id"] if prim_m else None)
    rows_c["primary_work_id"].append((ctx.sources.get(primary) or {}).get("work_id") if primary else None)
    rows_c["primary_year"].append(ctx.year(primary))
    rows_c["primary_rule"].append(rule)
    rows_c["reference_source_id"].append(reference)
    rows_c["reference_object_id"].append(ref_m["object_id"])
    rows_c["label"].append(next(iter(labels)) if len(labels) == 1 else None)
    rows_c["key_hash"].append(key_hash)
    rows_c["n_edges"].append(int(n_edges))
    real = [x for x in sims if x is not None]
    rows_c["min_similarity"].append(round(min(real), 6) if real else None)
    rows_c["template_share"].append(template_share)
    rows_c["rule_version"].append(RULE_VERSION)
    return cid


# ================================================================================================ build: figures
def _build_figures(con: Any, ctx: _Ctx, page_idx: dict[str, int], artifacts_root: str | None, p: dict[str, Any],
                   cache: Any, rows_c: dict[str, list], rows_m: dict[str, list], st: dict[str, Any],
                   aligned: "_Aligned | None" = None) -> pa.Table:
    t0 = time.monotonic()
    figs = load_figures(con)
    hst: dict[str, Any] = {}
    hashes = figure_hash_rows(con, figs, artifacts_root, p, cache, hst)
    hrows = hashes.to_pylist()
    st["hashing"] = {**hst, "status": dict(sorted(Counter(r["status"] for r in hrows).items())),
                     "low_information": sum(1 for r in hrows if r["low_information"])}
    t1 = time.monotonic()
    ok = [i for i, r in enumerate(hrows) if r["status"] == "OK"]
    objs = []
    for i in ok:
        f, h = figs[i], hrows[i]
        cap = f.get("caption_normalized") or f.get("caption") or ""
        body = caption_body(cap)
        side = max(int(h["width"] or 0), int(h["height"] or 0))
        objs.append({"object_id": f["object_id"], "source_id": f["source_id"], "page_id": f["page_id"],
                     "page_index": page_idx.get(f["page_id"]), "artifact_id": h["artifact_id"],
                     "label": label_number(f.get("figure_label")) or label_number(cap),
                     "caption_set": shingle_set(body, int(p["fig_caption_k"])), "caption_len": len(norm_key(body)),
                     "has_caption": bool(norm_key(body)), "side": side,
                     "low": bool(h["low_information"]) or side < int(p["fig_min_side"])})
    n = len(objs)
    src_names = sorted({o["source_id"] for o in objs})
    code = {s: i for i, s in enumerate(src_names)}
    src = np.array([code[o["source_id"]] for o in objs], dtype=np.int64)
    fa = _FigArrays([hrows[i] for i in ok], p)
    cand = figure_candidates(fa.ph, fa.trim, src, int(p["fig_candidate_max"])) if n > 1 else []
    t2 = time.monotonic()
    min_cap = int(p["fig_caption_min_shingles"])

    def cap_sim(a: dict, b: dict) -> float | None:
        if len(a["caption_set"]) < min_cap or len(b["caption_set"]) < min_cap:
            return None
        return containment(a["caption_set"], b["caption_set"])

    edges: dict[tuple[int, int], tuple] = {}
    ev_of: dict[tuple[int, int], dict[str, Any]] = {}
    tiers: Counter = Counter()
    for i, j in cand:
        a, b = objs[i], objs[j]
        d, ddh, tr = fa.distance(i, j)
        cs = cap_sim(a, b)
        same_label = bool(a["label"] and a["label"] == b["label"])
        identical = a["artifact_id"] == b["artifact_id"] or (d == 0 and ddh == 0 and tr == "ID")
        shape_ok = fa.aspect_ok(i, j, D4_NAMES.index(tr))
        tier = None
        if a["low"] or b["low"]:
            tier = "IDENTICAL" if identical else None
        elif identical:
            tier = "IDENTICAL"
        elif not shape_ok:
            tier = None
        elif d <= int(p["fig_strong_max"]) and ddh <= int(p["fig_strong_dhash_max"]):
            tier = "IMAGE"
        elif (d <= int(p["fig_medium_max"]) and ddh <= int(p["fig_medium_dhash_max"]) and tr == "ID"
              and ((cs is not None and cs >= float(p["fig_caption_support"])) or same_label)):
            tier = "IMAGE_CAPTION"
        elif d <= int(p["fig_weak_max"]) and tr == "ID" and cs is not None and cs >= float(p["fig_caption_strong"]):
            tier = "CAPTION_IMAGE"
        if tier is None:
            continue
        edges[(i, j)] = (MATCHES.index(tier), d, ddh, -(cs or 0.0))
        ev_of[(i, j)] = {"match": tier, "image_distance": d, "dhash_distance": ddh, "transform": tr,
                         "caption_similarity": None if cs is None else round(cs, 4)}
        tiers[tier] += 1
    caption_edges: dict[tuple[int, int], tuple] = {}
    if p["fig_caption_context"]:
        label_n = Counter((o["source_id"], o["label"]) for o in objs if o["label"])
        for (i, j), cs in _caption_pairs(objs, src, p, set(edges)).items():
            a, b = objs[i], objs[j]
            why = None
            if ctx.work(a["source_id"]) == ctx.work(b["source_id"]):
                if (a["label"] and a["label"] == b["label"] and label_n[(a["source_id"], a["label"])] == 1
                        and label_n[(b["source_id"], b["label"])] == 1):
                    why = "SAME_LABEL"
            if why is None and aligned is not None and aligned.near(a, b):
                why = "ALIGNED_TEXT"
            if why is None:
                continue
            d, ddh, tr = fa.distance(i, j)
            if d > int(p["fig_caption_max_distance"]):
                tiers["CAPTION_REJECTED_IMAGE_UNLIKE"] += 1
                continue
            caption_edges[(i, j)] = (MATCHES.index("CAPTION"), d, ddh, -cs)
            ev_of[(i, j)] = {"match": "CAPTION", "image_distance": d, "dhash_distance": ddh, "transform": tr,
                             "caption_similarity": round(cs, 4), "context": why}
            tiers["CAPTION/" + why] += 1
        edges.update(caption_edges)
    kept = mutual_best(edges, src)
    t3 = time.monotonic()
    comps = _components(n, kept)
    kept_set = set(kept)
    link_of: dict[int, tuple[tuple, tuple[int, int]]] = {}       # the best kept edge of every member
    for e in kept:
        for v in e:
            if v not in link_of or edges[e] < link_of[v][0]:
                link_of[v] = (edges[e], e)

    def sim_to_ref(r: dict, m: dict) -> dict[str, Any]:
        i, j = r["_i"], m["_i"]
        key = (min(i, j), max(i, j))
        d, ddh, tr = fa.best_distance(key[0], key[1])
        cs = cap_sim(r, m)
        ev = ev_of[key] if key in kept_set else ev_of[link_of[j][1]]
        sim = round(cs or 0.0, 4) if ev["match"] == "CAPTION" else max(0.0, 1.0 - d / 32.0)
        return {"match": ev["match"], "similarity": round(sim, 6), "image_distance": d, "dhash_distance": ddh,
                "transform": tr, "caption_similarity": None if cs is None else round(cs, 4),
                "context": ev.get("context")}

    for o_i, o in enumerate(objs):
        o["_i"] = o_i
    kinds: Counter = Counter()
    edges_by_comp: Counter = Counter()
    root_of = {}
    for c_i, comp in enumerate(comps):
        for v in comp:
            root_of[v] = c_i
    for i, j in kept:
        edges_by_comp[root_of[i]] += 1
    for c_i, comp in enumerate(comps):
        members = [objs[v] for v in comp]
        tl = [_template_like(o, ctx, p) for o in members]
        tshare = round(sum(tl) / len(tl), 4)
        works = {ctx.work(o["source_id"]) for o in members}
        captionless = all(not o["has_caption"] for o in members)
        template = captionless and (tshare == 1.0 or (len(works) >= int(p["fig_boilerplate_min_works"])
                                                      and tshare >= 0.5))
        comp_edges = [(i, j) for i, j in kept if root_of[i] == c_i]
        redrawn = bool(comp_edges) and all(ev_of[e]["match"] == "CAPTION" for e in comp_edges
                                           if ctx.work(objs[e[0]]["source_id"]) != ctx.work(objs[e[1]]["source_id"]))
        kind = ctx.kind("FIGURE", members, template=template, redrawn=redrawn)
        basis = Counter(ev_of[e]["match"] for e in comp_edges).most_common(1)[0][0]
        _emit_cluster(ctx, "FIGURE", members, kind=kind, match_basis=basis, n_edges=edges_by_comp[c_i],
                      sim_to_ref=sim_to_ref, rows_c=rows_c, rows_m=rows_m, template_share=tshare)
        kinds[kind] += 1
    st["counts"] = {"figures": len(figs), "hashed": n, "candidates": len(cand), "verified_pairs": len(edges),
                    "verified_by_tier": dict(sorted(tiers.items())), "caption_context_pairs": len(caption_edges),
                    "mutual_best_pairs": len(kept), "clusters": len(comps),
                    "clusters_by_kind": dict(sorted(kinds.items())), "members": sum(len(c) for c in comps),
                    "aligned_text": aligned is not None}
    st["timings_s"] = {"hash": round(t1 - t0, 3), "candidates": round(t2 - t1, 3), "verify": round(t3 - t2, 3),
                       "clusters": round(time.monotonic() - t3, 3)}
    return hashes


def _template_like(o: dict[str, Any], ctx: _Ctx, p: dict[str, Any]) -> bool:
    """A caption-less image that looks like a logo, badge or ornament: small (max side < ``fig_boilerplate_small``
    px), or not large (<= ``fig_boilerplate_max_side`` px) and on the first or last page of its source."""
    if o["has_caption"] or not o["side"]:
        return False
    if o["side"] < int(p["fig_boilerplate_small"]):
        return True
    if o["side"] > int(p["fig_boilerplate_max_side"]):
        return False
    info = ctx.sources.get(o["source_id"]) or {}
    pi = o.get("page_index")
    return pi is not None and (pi == info.get("first", 0) or pi == info.get("last", 0))


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def _caption_pairs(objs: list[dict[str, Any]], src: np.ndarray, p: dict[str, Any], taken: set
                   ) -> dict[tuple[int, int], float]:
    """Pairs (i < j) of figures of different sources with nearly equal captions (containment >=
    ``fig_caption_strong``, both >= ``fig_caption_min_shingles`` shingles) not already verified by the image."""
    idx = [i for i, o in enumerate(objs) if len(o["caption_set"]) >= int(p["fig_caption_min_shingles"])
           and not o["low"]]
    if len(idx) < 2:
        return {}
    keys = [np.array(sorted({_h64(s) for s in objs[i]["caption_set"]}), dtype=np.uint64) for i in idx]
    shared = _shared_counts(keys, src[idx], int(p["fig_caption_max_df"]))
    thr = float(p["fig_caption_strong"])
    out: dict[tuple[int, int], float] = {}
    for (a, b), cnt in shared.items():
        i, j = idx[a], idx[b]
        oi, oj = objs[i], objs[j]
        if cnt / min(len(oi["caption_set"]), len(oj["caption_set"])) < 0.5 * thr:
            continue                                        # a lower bound (frequent shingles are not indexed)
        cs = containment(oi["caption_set"], oj["caption_set"]) or 0.0
        key = (min(i, j), max(i, j))
        if cs >= thr and key not in taken:
            out[key] = cs
    return out


class _Aligned:
    """Pages of two sources that share a text passage (clusters of the part ``duplicates``), as (source, page index)
    pairs; ``near`` allows ``slack`` pages on each side (a figure beside the reprinted paragraph)."""

    def __init__(self, dup_members: Any, slack: int = 1):
        self.slack = int(slack)
        by_cluster: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for r in _table_rows(dup_members):
            if r.get("page_index") is not None:
                by_cluster[r["cluster_id"]].append((r["source_id"], int(r["page_index"])))
        self.pairs: set[tuple[str, int, str, int]] = set()
        for pages in by_cluster.values():
            for (sa, ia), (sb, ib) in combinations(sorted(set(pages)), 2):
                if sa != sb:
                    self.pairs.add((sa, ia, sb, ib))
                    self.pairs.add((sb, ib, sa, ia))

    def __len__(self) -> int:
        return len(self.pairs) // 2

    def near(self, a: dict[str, Any], b: dict[str, Any], slack: int | None = None) -> bool:
        if a.get("page_index") is None or b.get("page_index") is None:
            return False
        k = self.slack if slack is None else slack
        ia, ib = int(a["page_index"]), int(b["page_index"])
        return any((a["source_id"], ia + x, b["source_id"], ib + y) in self.pairs
                   for x in range(-k, k + 1) for y in range(-k, k + 1))


# ================================================================================================ build: tables
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def table_numbers(text: str) -> list[tuple[str, float, bool]]:
    """The numbers of a table text in reading order: (digits only — decimal commas, points and lost separators give
    the same token: 16,42 = 16.42 = 1642 —, the value as printed, informative). A number is informative when it has
    a fractional part or three significant digits (small integers — counts, row numbers, classes — are not)."""
    out = []
    for n in _NUMBER.findall(text or ""):
        digits = re.sub(r"\D", "", n)
        norm = n.replace(",", ".")
        try:
            value = float(norm) if norm.count(".") <= 1 else float(digits)
        except ValueError:
            value = float(digits or 0)
        frac = "." in norm and norm.count(".") == 1
        out.append((digits, value, frac or len(digits.lstrip("0")) >= 3))
    return out


def _number_keys(nums: list[tuple[str, float, bool]], k: int) -> np.ndarray:
    """Hashes of the informative ``k``-number windows: at least two informative numbers and not an arithmetic
    progression (0.1, 0.2, 0.3 or 1998, 1999, 2000 describe a grid, not data)."""
    keys = set()
    for i in range(len(nums) - k + 1):
        win = nums[i:i + k]
        if sum(1 for _, _, inf in win if inf) < 2:
            continue
        vals = [v for _, v, _ in win]
        steps = {round(b - a, 9) for a, b in zip(vals, vals[1:])}
        if len(steps) == 1:
            continue
        keys.add(_h64("|".join(d for d, _, _ in win)))
    return np.array(sorted(keys), dtype=np.uint64)


def _build_tables(con: Any, ctx: _Ctx, page_idx: dict[str, int], p: dict[str, Any], rows_c: dict[str, list],
                  rows_m: dict[str, list], st: dict[str, Any]) -> None:
    t0 = time.monotonic()
    tabs = load_tables(con)
    k, hk, nk = int(p["table_shingle_k"]), int(p["table_header_k"]), int(p["table_number_k"])
    objs, norms, bodies, nkeys = [], [], [], []
    for t in tabs:
        head, body = _table_texts(t)
        full = norm_key(head + "\n" + body)
        if len(full) < k:
            continue
        cap = t.get("caption_normalized") or t.get("caption") or ""
        nums = table_numbers(head + "\n" + body)
        n_inf = sum(1 for _, _, inf in nums if inf)
        objs.append({"object_id": t["object_id"], "source_id": t["source_id"], "page_id": t["page_id"],
                     "page_index": page_idx.get(t["page_id"]),
                     "label": label_number(t.get("table_label")) or label_number(cap),
                     "header_set": shingle_set(head, hk), "n_numbers": len(nums), "n_informative": n_inf,
                     "number_set": frozenset(d for d, _, inf in nums if inf)})
        norms.append(full)
        bodies.append(norm_key(body))
        nkeys.append(_number_keys(nums, nk) if n_inf >= int(p["table_min_numbers"]) else
                     np.zeros(0, dtype=np.uint64))
    n = len(objs)
    src_names = sorted({o["source_id"] for o in objs})
    code = {s: i for i, s in enumerate(src_names)}
    src = np.array([code[o["source_id"]] for o in objs], dtype=np.int64)
    empty = (np.zeros(0, np.uint64), np.zeros(0, np.int64), np.zeros(0, np.int64))
    keys, starts, counts = dup.shingle_keys(norms, k, np) if n else empty
    bkeys, bstarts, bcounts = dup.shingle_keys([b if len(b) >= k else "" for b in bodies], k, np) if n else empty
    per = [keys[starts[i]:starts[i] + counts[i]] for i in range(n)]
    shared = _shared_counts(per, src, int(p["table_max_df"])) if n > 1 else {}
    nshared = _shared_counts(nkeys, src, int(p["table_max_df"])) if n > 1 else {}
    t1 = time.monotonic()
    cand = sorted({pr for pr, c in shared.items()
                   if c / max(1, min(counts[pr[0]], counts[pr[1]])) >= 0.5 * float(p["table_min_containment"])}
                  | {pr for pr, c in nshared.items() if c >= int(p["table_min_number_shared"])})
    ca = np.array([c[0] for c in cand], dtype=np.int64)
    cb = np.array([c[1] for c in cand], dtype=np.int64)
    inter = dup.intersections(keys, starts, counts, ca, cb, np)
    binter = dup.intersections(bkeys, bstarts, bcounts, ca, cb, np)
    edges: dict[tuple[int, int], tuple] = {}
    ev_of: dict[tuple[int, int], dict[str, Any]] = {}
    body_min = int(p["table_body_min_shingles"])
    by_channel: Counter = Counter()

    def ncont(i: int, j: int) -> tuple[float | None, int]:
        a, b = nkeys[i], nkeys[j]
        if not len(a) or not len(b):
            return None, 0
        it = int(np.intersect1d(a, b, assume_unique=True).size)
        return it / min(len(a), len(b)), it

    for e, (i, j) in enumerate(cand):
        small = int(min(counts[i], counts[j]))
        cont = inter[e] / small if small else 0.0
        cells_ok = cont >= float(p["table_min_containment"]) and inter[e] >= int(p["table_min_shared"])
        bcont = None
        bsmall = int(min(bcounts[i], bcounts[j]))
        if cells_ok and bsmall > 0:                       # the rows below the header must agree too
            bcont = binter[e] / bsmall
            cells_ok = bcont >= float(p["table_min_body_containment"])
        elif cells_ok:                                    # a header-only table: nearly all of it
            cells_ok = cont >= float(p["table_header_only_containment"])
        nc, nshare = ncont(i, j)
        if cells_ok and nc is not None and nc < float(p["table_min_numbers_agree"]):
            cells_ok = False                              # the same labels with other numbers: another table
        sa, sb = objs[i]["number_set"], objs[j]["number_set"]
        if cells_ok and min(len(sa), len(sb)) >= 2 and \
                len(sa & sb) / min(len(sa), len(sb)) < float(p["table_min_numbers_agree"]):
            cells_ok = False                              # a short spec table of another machine
        numbers_ok = (nc is not None and nc >= float(p["table_min_number_containment"])
                      and nshare >= int(p["table_min_number_shared"]))
        if not (cells_ok or numbers_ok):
            continue
        hs = containment(objs[i]["header_set"], objs[j]["header_set"])
        edges[(i, j)] = (0, -round(max(cont if cells_ok else 0.0, nc if numbers_ok else 0.0), 6), -(bcont or 0.0))
        ev_of[(i, j)] = {"match": "CELLS" if cells_ok else "NUMBERS", "cell_containment": round(float(cont), 6),
                         "number_containment": None if nc is None else round(nc, 6),
                         "header_similarity": None if hs is None else round(hs, 4)}
        by_channel["CELLS" if cells_ok and not numbers_ok else "NUMBERS" if numbers_ok and not cells_ok
                   else "BOTH"] += 1
    kept = mutual_best(edges, src)
    comps = _components(n, kept)
    t2 = time.monotonic()

    def kset(i: int) -> np.ndarray:
        return keys[starts[i]:starts[i] + counts[i]]

    def sim_to_ref(r: dict, m: dict) -> dict[str, Any]:
        a, b = kset(r["_i"]), kset(m["_i"])
        it = int(np.intersect1d(a, b, assume_unique=True).size)
        cont = it / max(1, min(len(a), len(b)))
        hs = containment(r["header_set"], m["header_set"])
        nc, _ = ncont(r["_i"], m["_i"])
        key = (min(r["_i"], m["_i"]), max(r["_i"], m["_i"]))
        match = (ev_of.get(key) or {}).get("match") or ("CELLS" if cont >= float(p["table_min_containment"])
                                                        else "NUMBERS")
        return {"match": match, "similarity": round(max(cont, nc or 0.0), 6), "cell_containment": round(cont, 6),
                "number_containment": None if nc is None else round(nc, 6),
                "header_similarity": None if hs is None else round(hs, 4)}

    for o_i, o in enumerate(objs):
        o["_i"] = o_i
    root_of = {v: c_i for c_i, comp in enumerate(comps) for v in comp}
    edges_by_comp: dict[int, list] = defaultdict(list)
    for e in kept:
        edges_by_comp[root_of[e[0]]].append(e)
    kinds: Counter = Counter()
    for c_i, comp in enumerate(comps):
        members = [objs[v] for v in comp]
        # a template: bodies too small to compare (a header row only) in >= 3 works
        works = {ctx.work(o["source_id"]) for o in members}
        template = all(int(bcounts[v]) < body_min for v in comp) and len(works) >= 3
        kind = ctx.kind("TABLE", members, template=template)
        basis = Counter(ev_of[e]["match"] for e in edges_by_comp[c_i]).most_common(1)[0][0]
        _emit_cluster(ctx, "TABLE", members, kind=kind, match_basis=basis, n_edges=len(edges_by_comp[c_i]),
                      sim_to_ref=sim_to_ref, rows_c=rows_c, rows_m=rows_m, template_share=1.0 if template else 0.0)
        kinds[kind] += 1
    st["counts"] = {"tables": len(tabs), "compared": n, "with_numbers": sum(1 for x in nkeys if len(x)),
                    "candidates": len(cand), "verified_pairs": len(edges),
                    "verified_by_channel": dict(sorted(by_channel.items())), "mutual_best_pairs": len(kept),
                    "clusters": len(comps), "clusters_by_kind": dict(sorted(kinds.items())),
                    "members": sum(len(c) for c in comps)}
    st["timings_s"] = {"index": round(t1 - t0, 3), "verify": round(t2 - t1, 3),
                       "clusters": round(time.monotonic() - t2, 3)}


# ================================================================================================ build: formulas
def load_formulas(con: Any) -> list[dict[str, Any]]:
    return _select(con, "formulas", ("object_id", "source_id", "page_id", "formula_kind", "normalized_latex",
                                     "raw_output", "raw_format", "equation_label"), "ORDER BY object_id")


def _definitions(formula_symbols: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for r in _table_rows(formula_symbols):
        key = r.get("definition_key")
        if key and r.get("definition"):
            out[r["formula_id"]].add(key)
    return out


def definitions_agree(a: set[str], b: set[str]) -> str:
    """Context of a renamed match from the «где …» definitions of both formulas (word stems of ``definition_key``):
    ``DEFINITIONS_AGREE`` (a stem in common), ``DEFINITIONS_DISAGREE`` (none in common, >= 2 definitions each) or
    ``DEFINITIONS_UNKNOWN``."""
    if not a or not b:
        return "DEFINITIONS_UNKNOWN"
    wa = {w for k in a for w in k.split()}
    wb = {w for k in b for w in k.split()}
    if wa & wb:
        return "DEFINITIONS_AGREE"
    return "DEFINITIONS_DISAGREE" if len(a) >= 2 and len(b) >= 2 else "DEFINITIONS_UNKNOWN"


def token_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """difflib ratio of two canonical token lists (1.0 = equal)."""
    import difflib  # noqa: PLC0415

    return difflib.SequenceMatcher(None, list(a), list(b), autojunk=False).ratio()


def _build_formulas(con: Any, ctx: _Ctx, page_idx: dict[str, int], p: dict[str, Any], formula_context: Any,
                    formula_symbols: Any, rows_c: dict[str, list], rows_m: dict[str, list],
                    st: dict[str, Any], aligned: "_Aligned | None" = None) -> pa.Table:
    t0 = time.monotonic()
    fs = load_formulas(con)
    fctx = {r["formula_id"]: r for r in _table_rows(formula_context)}
    defs = _definitions(formula_symbols)
    rows_k: dict[str, list] = {f.name: [] for f in FORMULA_KEYS_SCHEMA}
    objs = []
    no_latex = 0
    reasons: Counter = Counter()
    for f in fs:
        latex = f.get("normalized_latex") or (f.get("raw_output") if (f.get("raw_format") or "LATEX") == "LATEX"
                                              else None)
        if not latex:
            no_latex += 1
            continue
        c = _canonical(latex, p)
        fc = fctx.get(f["object_id"]) or {}
        eq = fc.get("equation_number") or label_number(f.get("equation_label"))
        s = f["source_id"]
        reasons[c["trivial_reason"] or "KEPT"] += 1
        rows_k["formula_id"].append(f["object_id"])
        rows_k["source_id"].append(s)
        rows_k["work_id"].append((ctx.sources.get(s) or {}).get("work_id"))
        rows_k["year"].append(ctx.year(s))
        rows_k["page_id"].append(f["page_id"])
        rows_k["page_index"].append(page_idx.get(f["page_id"]))
        rows_k["formula_kind"].append(f.get("formula_kind"))
        rows_k["equation_number"].append(eq)
        rows_k["section_id"].append(fc.get("section_id"))
        for col in ("latex_key", "shape_key", "key_hash", "shape_hash", "n_tokens", "n_ids", "n_struct", "trivial",
                    "trivial_reason", "distinctive"):
            rows_k[col].append(c[col])
        rows_k["rule_version"].append(RULE_VERSION)
        if not c["trivial"]:
            objs.append({"object_id": f["object_id"], "source_id": s, "page_id": f["page_id"],
                         "page_index": page_idx.get(f["page_id"]), "label": eq, "equation_number": eq,
                         "section_id": fc.get("section_id"), "key": c["key_hash"], "shape": c["shape_hash"],
                         "distinctive": c["distinctive"], "tokens": c["latex_key"].split(" "),
                         "ids": c["identifiers"]})
    keys_tbl = pa.table(rows_k, schema=FORMULA_KEYS_SCHEMA)
    t1 = time.monotonic()
    uf = _UF()
    for i in range(len(objs)):
        uf.find(i)
    # 1. equal canonical form (LATEX)
    by_key: dict[str, list[int]] = defaultdict(list)
    by_shape: dict[str, list[int]] = defaultdict(list)
    for i, o in enumerate(objs):
        by_key[o["key"]].append(i)
        if o["distinctive"] and p["formula_renamed"]:
            by_shape[o["shape"]].append(i)
    for idx in by_key.values():
        for i in idx[1:]:
            uf.union(idx[0], i)
    # 2. equal renamed form of distinctive formulas (LATEX_RENAMED) unless the definitions contradict; a structure
    #    that one source uses for two different formulas (E = Δσ/Δε and D = Δσ/Δε) is generic and not merged
    links: list[tuple[int, int]] = []
    renamed_kept = renamed_dropped = renamed_unanchored = generic_shapes = 0
    for idx in by_shape.values():
        keys = sorted({objs[i]["key"] for i in idx})
        if len(keys) < 2:
            continue
        key_sources: Counter = Counter()
        for kk in keys:
            key_sources.update({objs[i]["source_id"] for i in idx if objs[i]["key"] == kk})
        if any(c > 1 for c in key_sources.values()):
            generic_shapes += 1
            continue
        heads = {kk: min((i for i in idx if objs[i]["key"] == kk), key=lambda i: objs[i]["object_id"]) for kk in keys}
        first = heads[keys[0]]
        for kk in keys[1:]:
            h = heads[kk]
            why = definitions_agree(defs.get(objs[first]["object_id"], set()), defs.get(objs[h]["object_id"], set()))
            if why == "DEFINITIONS_DISAGREE":
                renamed_dropped += 1
                continue
            anchored = any(x == y and x not in _WEAK_ANCHORS for x, y in zip(objs[first]["ids"], objs[h]["ids"]))
            if why != "DEFINITIONS_AGREE" and not anchored:
                renamed_unanchored += 1               # no symbol in common and no definitions: a generic form
                continue
            renamed_kept += 1
            links.append((first, h))
            uf.union(first, h)
    # 3. LATEX_NEAR: the same printed number on pages sharing a text passage (OCR variants in copies and reprints)
    near: dict[tuple[int, int], float] = {}
    if aligned is not None and p["formula_near"]:
        by_page: dict[tuple[str, int], list[int]] = defaultdict(list)
        for i, o in enumerate(objs):
            if o["equation_number"] and o["page_index"] is not None:
                by_page[(o["source_id"], int(o["page_index"]))].append(i)
        for (sa, ia, sb, ib) in sorted(aligned.pairs):
            if sa >= sb:
                continue
            for i in by_page.get((sa, ia), ()):
                for j in by_page.get((sb, ib), ()):
                    a, b = objs[i], objs[j]
                    if a["equation_number"] != b["equation_number"] or a["key"] == b["key"]:
                        continue
                    r = token_ratio(a["tokens"], b["tokens"])
                    if r >= float(p["formula_near_min_ratio"]):
                        near[(min(i, j), max(i, j))] = r
        for (i, j) in near:
            links.append((i, j))
            uf.union(i, j)
    near_nodes = {i for e in near for i in e}
    comp: dict[int, list[int]] = defaultdict(list)
    for i in range(len(objs)):
        comp[uf.find(i)].append(i)
    links_of: Counter = Counter(uf.find(i) for i, _ in links)
    kinds: Counter = Counter()
    n_clusters = 0
    for root, idx in sorted(comp.items(), key=lambda kv: objs[min(kv[1])]["object_id"]):
        if len({objs[i]["source_id"] for i in idx}) < 2:
            continue
        members = [objs[i] for i in idx]
        kind = ctx.kind("FORMULA", members)
        n_keys = len({m["key"] for m in members})
        basis = ("LATEX_NEAR" if any(i in near_nodes for i in idx) else "LATEX_RENAMED" if n_keys > 1 else "LATEX")

        def sim_to_ref(r: dict, m: dict) -> dict[str, Any]:
            if m["key"] == r["key"]:
                return {"match": "LATEX", "similarity": 1.0}
            if m["distinctive"] and r["distinctive"] and m["shape"] == r["shape"]:
                return {"match": "LATEX_RENAMED", "similarity": 1.0,
                        "context": definitions_agree(defs.get(r["object_id"], set()), defs.get(m["object_id"], set()))}
            return {"match": "LATEX_NEAR", "similarity": round(token_ratio(r["tokens"], m["tokens"]), 6),
                    "context": "ALIGNED_TEXT"}

        ref_key = sorted(members, key=lambda m: m["object_id"])[0]["key"]
        _emit_cluster(ctx, "FORMULA", members, kind=kind, match_basis=basis, n_edges=links_of[root],
                      sim_to_ref=sim_to_ref, rows_c=rows_c, rows_m=rows_m, key_hash=ref_key)
        kinds[kind] += 1
        n_clusters += 1
    st["counts"] = {"formulas": len(fs), "no_latex": no_latex, "canonical": keys_tbl.num_rows,
                    "not_grouped_trivial": dict(sorted((kk, v) for kk, v in reasons.items() if kk != "KEPT")),
                    "compared": len(objs), "distinctive": sum(1 for o in objs if o["distinctive"]),
                    "clusters": n_clusters, "clusters_by_kind": dict(sorted(kinds.items())),
                    "renamed_links_kept": renamed_kept, "renamed_links_dropped_by_definitions": renamed_dropped,
                    "renamed_links_dropped_unanchored": renamed_unanchored,
                    "generic_shapes_not_merged": generic_shapes, "near_pairs": len(near),
                    "aligned_text": aligned is not None}
    st["timings_s"] = {"canonical": round(t1 - t0, 3), "groups": round(time.monotonic() - t1, 3)}
    return keys_tbl


# ================================================================================================ build
def build(con: Any, *, artifacts: str | os.PathLike | None = None, source_overlap: Any = None,
          dup_members: Any = None, formula_context: Any = None, formula_symbols: Any = None,
          figure_hashes: Any = None, stats: dict[str, Any] | None = None, **options: Any) -> dict[str, pa.Table]:
    """``object_dup_clusters``, ``object_dup_members``, ``formula_keys`` and ``figure_hashes`` of the snapshot behind
    ``con`` (DuckDB).

    ``artifacts`` — the artifact root (``$VKM_DATA_ROOT/artifacts``; default: that path when ``VKM_DATA_ROOT`` is
    set) for the figure images; ``figure_hashes`` — an earlier build's hashes, reused by artifact id; ``source_overlap``
    and ``dup_members`` (part ``duplicates``: reprint relations and pages sharing a text passage) and
    ``formula_context`` / ``formula_symbols`` (part ``formulas``) — optional context of the same snapshot; without
    them the context channels are off and the manifest says so. Options and defaults: ``DEFAULTS``; unknown keyword
    arguments (the common arguments of ``vkm-corpus nav build``) are ignored. ``stats`` receives counts, parameters,
    model choices and timings.
    """
    p = {k: options.get(k, v) for k, v in DEFAULTS.items()}
    st = stats if stats is not None else {}
    t_all = time.monotonic()
    cache_info = {"from": "inputs" if figure_hashes is not None else None}
    if figure_hashes is None and p["hash_cache"]:
        import pyarrow.parquet as pq  # noqa: PLC0415

        figure_hashes = pq.read_table(os.fspath(p["hash_cache"]))
        cache_info = {"from": "hash_cache", "file": os.path.basename(os.fspath(p["hash_cache"]))}
    p["hash_cache"] = cache_info["file"] if cache_info.get("from") == "hash_cache" else None
    root = os.fspath(artifacts) if artifacts else None
    if root is None and os.environ.get("VKM_DATA_ROOT"):
        cand = os.path.join(os.environ["VKM_DATA_ROOT"], "artifacts")
        root = cand if os.path.isdir(cand) else None
    sources = dup.load_sources(con)
    groups, ginfo = publication_groups(con, sources, source_overlap, int(p["reprint_min_shared_text"]))
    ctx = _Ctx(sources, groups)
    page_idx = _page_index(con)
    aligned = _Aligned(dup_members, int(p["aligned_slack_pages"])) if dup_members is not None else None
    ginfo["aligned_page_pairs"] = len(aligned) if aligned is not None else "UNAVAILABLE"
    rows_c: dict[str, list] = {f.name: [] for f in CLUSTERS_SCHEMA}
    rows_m: dict[str, list] = {f.name: [] for f in MEMBERS_SCHEMA}
    types = [t for t in OBJECT_TYPES if t in set(p["object_types"] or ())]
    per_type: dict[str, Any] = {}
    fig_tbl = pa.table({f.name: [] for f in FIG_HASH_SCHEMA}, schema=FIG_HASH_SCHEMA)
    keys_tbl = pa.table({f.name: [] for f in FORMULA_KEYS_SCHEMA}, schema=FORMULA_KEYS_SCHEMA)
    if "FIGURE" in types:
        per_type["FIGURE"] = {"artifact_root": "given" if artifacts else ("VKM_DATA_ROOT" if root else None),
                              "hash_cache": cache_info}
        fig_tbl = _build_figures(con, ctx, page_idx, root, p, figure_hashes, rows_c, rows_m, per_type["FIGURE"],
                                 aligned)
    if "TABLE" in types:
        per_type["TABLE"] = {}
        _build_tables(con, ctx, page_idx, p, rows_c, rows_m, per_type["TABLE"])
    if "FORMULA" in types:
        per_type["FORMULA"] = {"formula_context": formula_context is not None,
                               "formula_symbols": formula_symbols is not None}
        keys_tbl = _build_formulas(con, ctx, page_idx, p, formula_context, formula_symbols, rows_c, rows_m,
                                   per_type["FORMULA"], aligned)
    clusters = pa.table(rows_c, schema=CLUSTERS_SCHEMA)
    members = pa.table(rows_m, schema=MEMBERS_SCHEMA)
    order = sorted(range(clusters.num_rows), key=lambda i: rows_c["cluster_id"][i])
    clusters = clusters.take(pa.array(order, pa.int64())) if order else clusters
    morder = sorted(range(members.num_rows), key=lambda i: (rows_m["cluster_id"][i], rows_m["source_id"][i],
                                                          rows_m["page_index"][i] or 0, rows_m["object_id"][i]))
    members = members.take(pa.array(morder, pa.int64())) if morder else members
    kinds = Counter(zip(rows_c["object_type"], rows_c["kind"]))
    st.update({
        "rule_version": RULE_VERSION, "hash_rule": HASH_RULE, "layer_status": "DERIVED",
        "review_status": "AUTO_EXTRACTED_UNREVIEWED",
        "params": {k: (sorted(v) if isinstance(v, (tuple, set, list)) else v) for k, v in p.items()},
        "model_choices": {**{k: "MODEL_CHOICE" for k in DEFAULTS
                             if k not in ("workers", "object_types", "hash_cache", "max_image_pixels")},
                          **MODEL_CHOICES},
        "secondary_work_types": sorted(dup.SECONDARY_WORK_TYPES),
        "publication_groups": ginfo,
        "object_types": per_type,
        "counts": {"clusters": clusters.num_rows, "members": members.num_rows,
                   "clusters_by_type_kind": {f"{t}/{k}": v for (t, k), v in sorted(kinds.items())},
                   "primary_rules": dict(sorted(Counter(rows_c["primary_rule"]).items())),
                   "formula_keys": keys_tbl.num_rows, "figure_hashes": fig_tbl.num_rows},
        "timings_s": {"total": round(time.monotonic() - t_all, 3)},
    })
    return {"object_dup_clusters": clusters, "object_dup_members": members, "formula_keys": keys_tbl,
            "figure_hashes": fig_tbl}
