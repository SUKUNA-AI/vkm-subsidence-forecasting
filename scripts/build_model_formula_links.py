#!/usr/bin/env python3
"""Links of the formula-registry models (MM-*) to the formula objects of the corpus canon (``VKM-SRC-NNN:pNNNN:m<12hex>``).

Inputs (PRIVATE ``$VKM_RESOURCES_ROOT/11_evidence_vnext/``):
- ``canonical/FORMULAS/MATHEMATICAL_MODEL_REGISTRY.csv`` — the 279 models (``vn_ids``, ``source_ids``, ``locator``,
  ``equation_latex``);
- ``canonical/FORMULAS/RECORD_TO_MODEL_MAP.csv``, ``merged/vn_index.csv`` and ``sweep_raw/<dir>/records.jsonl`` —
  every evidence record of a model: source, ``pdf_page``, printed page, equation number and LaTeX;
- optional ``canonical/FORMULAS/MODEL_FORMULA_LINK_CHECKS.csv`` — the manual checks of disputed links
  (``link_id``, ``formula_id``, ``check`` CONFIRMED / REJECTED / UNSURE, ``check_note``).

The corpus is read through the read-only API (``VKM_API_URL``; token file ``VKM_API_TOKEN_FILE``, default
``~/.vkm/secrets/api_read_token``): ``GET /v1/source/{id}`` and ``/v1/source/{id}/pages``, ``POST /v1/objects/query``
(the formulas of the page windows), ``GET /v1/nav/formula/{id}`` (the printed equation number of the navigation
layer) and ``GET /v1/nav/shared_formulas`` (the group «the same written form in other books»). Requests are sequential;
every response is cached under ``work/model_formula_links/api_cache`` (git-ignored), ``--offline`` uses the cache only.

Rule ``model_formula_links_v1`` (thresholds: ``PARAMS``, MODEL_CHOICE):
1. Units: one per (model, source, equation number) from the model's evidence records — a record that names several
   numbers («формулы (3.1)-(3.5)») gives one unit per number; a record without a number gives one unit matched by its
   LaTeX only. Locator segments (``VKM-SRC-NNN p.N``) of sources without a record add units the same way; registry
   sources without any page (TEXTBOOK, EXT-SRC, a source without a record) are NOT_FOUND rows with the reason.
2. Page basis: the record's ``pdf_page`` is the physical page from 1, the canon's ``page_index`` too. A source shift
   is accepted only when proven: >= ``shift_min_votes`` units of the source agree on one non-zero offset (a
   strong form match or the canonical equation label at that offset) with a share >= ``shift_min_share``.
3. Candidates: every formula object of the pages ``pdf_page + shift ± 1`` of the source.
4. Measures: number — the NAV ``equation_number`` (or the canonical ``equation_label``) equals the unit number after
   normalisation; form — the registry form of the equation (the record's LaTeX, else the ``equation_plain`` segment
   of that number converted to LaTeX, else the registry LaTeX of a one-equation model) against the canonical tokens
   of NAV ``formula_keys`` (``canonical_tokens``) after folding Cyrillic / Latin homoglyphs (and OCR misreadings
   inside subscripts), ``tg``/``tan``, fractions read as slashes, primes, brackets and OCR wrappers dropped: max of
   the token ratio, the ratio of the base letters (OCR-garbled subscripts), the renamed-identifier (shape) ratio for
   long formulas and the in-order containment for split or merged objects, each weighted (``PARAMS``); a form of
   several equations is also compared part by part.
5. Status: ``EXACT`` — number equal and form >= ``exact_min_form``; ``NUMBER_MATCH`` — number equal, form lower (OCR)
   or no registry form of that number (reason ``NO_REGISTRY_FORM``); ``FORM_MATCH`` — form >= ``form_match_min``
   without an equal number; ``CANDIDATE`` — form >= ``candidate_min``; ``NOT_FOUND`` with the reason
   (``TEXTBOOK_FORM_NO_SOURCE_PAGE``, ``EXTERNAL_SOURCE_NOT_IN_CORPUS``, ``NO_RECORD_OR_LOCATOR_PAGE_FOR_SOURCE``,
   ``NO_CORPUS_RECORD``, ``NO_PAGE_IN_RECORD``, ``SOURCE_NOT_IN_CANON``, ``SOURCE_HAS_NO_FORMULA_OBJECTS``,
   ``RECORD_HAS_NO_EQUATION``, ``PAGE_NOT_IN_CANON``, ``NO_FORMULAS_ON_PAGES``, ``CANON_FORMULAS_WITHOUT_LATEX``,
   ``NUMBER_NOT_IN_CANON_NO_REGISTRY_FORM``, ``NO_MATCHING_FORMULA``). A manual check that CONFIRMS another
   candidate of the window picks it (``method`` ``+REVIEW_PICK``; a picked form below ``candidate_min`` is shown as
   CANDIDATE).

Output: ``canonical/FORMULAS/MODEL_FORMULA_LINKS.csv`` (one row per link, keyed by ``link_id`` — a hash of model,
source, equation number and, for an unnumbered record, its vn_ids and pages; IDs and numbers only — no LaTeX, no
quotes) and the receipt
``canonical/FORMULAS/receipts/model_formula_links_<date>.json`` (inputs with SHA-256, canonical snapshot, command,
counts, rule version). The review sheet with LaTeX for the disputed links goes to the git-ignored work directory.
Every row is AUTO_EXTRACTED_UNREVIEWED / DERIVATION: a navigation link «the model's equation is printed as this
formula object», never a check of the law.

Usage:  VKM_RESOURCES_ROOT=<PRIVATE> VKM_API_URL=<API> python scripts/build_model_formula_links.py [--offline]
        [--date YYYY-MM-DD] [--work work/model_formula_links]
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RULE_VERSION = "model_formula_links_v1"
PARAMS: dict[str, Any] = {
    "page_window": 1,            # candidates: pdf_page + shift ± page_window
    "shift_probe": 2,            # offsets tried when proving a source shift
    "shift_min_votes": 3,
    "shift_min_share": 0.6,
    "shift_vote_min_form": 0.85,
    "exact_min_form": 0.5,
    "form_match_min": 0.8,
    "candidate_min": 0.6,
    "weight_base": 0.95,         # base letters only (subscripts garbled by OCR)
    "weight_shape": 0.9,         # identifiers renamed in order (another notation)
    "shape_min_tokens": 12,
    "weight_containment": 0.85,  # one form inside the other in order (split or merged objects)
    "containment_min_tokens": 8,
    "nav_min_tokens": 4,         # NAV context is fetched for DISPLAY formulas, labelled ones and INLINE ones with a
                                 # relation sign and at least this many tokens
}
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
EPISTEMIC_STATUS = "DERIVATION"
STATUSES = ("EXACT", "NUMBER_MATCH", "FORM_MATCH", "CANDIDATE", "NOT_FOUND")
CHECKS = ("CONFIRMED", "REJECTED", "UNSURE")
COLUMNS = ("link_id", "model_id", "equation_ref", "source_id", "vn_ids", "page_id", "page_index", "page_label",
           "formula_id", "formula_kind", "equation_number_canon", "match_status", "number_match", "form_similarity",
           "page_offset", "page_basis", "method", "reason", "check", "check_note", "n_candidates", "shared_group_id",
           "shared_n_works", "canonical_snapshot_id", "rule_version", "review_status", "epistemic_status")
CHECK_COLUMNS = ("link_id", "model_id", "equation_ref", "source_id", "formula_id", "check", "check_note")
REG_REL = "canonical/FORMULAS/MATHEMATICAL_MODEL_REGISTRY.csv"
MAP_REL = "canonical/FORMULAS/RECORD_TO_MODEL_MAP.csv"
IDX_REL = "merged/vn_index.csv"
CHECKS_REL = "canonical/FORMULAS/MODEL_FORMULA_LINK_CHECKS.csv"
OUT_REL = "canonical/FORMULAS/MODEL_FORMULA_LINKS.csv"
RECEIPT_DIR_REL = "canonical/FORMULAS/receipts"
CODE_FILES = ("scripts/build_model_formula_links.py", "src/vkm_corpus/navigation/object_duplicates.py",
              "src/vkm_corpus/navigation/formulas.py")
REL_SIGNS = frozenset({"=", "≈", "≤", "≥", "<", ">", "≠", "≡", "~", "∝", "≃", "≅"})


# ================================================================================================ numbers and LaTeX
_NUM_FOLD = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", ",": ".", "–": "-", "—": "-", "−": "-"})
_PAREN_NUM = re.compile(r"\(\s*(\d{1,3}(?:\s*[.,]\s*\d{1,3}){0,2}\s*[a-zа-я]?)\s*(?:,[^)]*)?\)", re.IGNORECASE)
_RANGE = re.compile(r"\(\s*(\d{1,3}(?:[.,]\d{1,3}){0,2})\s*\)\s*[-–—]\s*\(\s*(\d{1,3}(?:[.,]\d{1,3}){0,2})\s*\)")


def norm_number(text: Any) -> str:
    """``(6.1)`` / ``6,1`` / ``( 3.2а )`` → ``6.1`` / ``3.2a``; empty when no number."""
    s = str(text or "").strip().lower().translate(_NUM_FOLD)
    s = re.sub(r"\s+", "", s).strip("()[]").rstrip(".")
    return s if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){0,2}[a-z]?", s) else ""


def _expand(a: str, b: str) -> list[str]:
    pa_, pb = a.split("."), b.split(".")
    if len(pa_) != len(pb) or pa_[:-1] != pb[:-1] or not (pa_[-1].isdigit() and pb[-1].isdigit()):
        return [a, b]
    lo, hi = int(pa_[-1]), int(pb[-1])
    if not 0 < hi - lo <= 30:
        return [a, b]
    return [".".join(pa_[:-1] + [str(k)]) for k in range(lo, hi + 1)]


def parse_numbers(*texts: Any) -> list[str]:
    """Equation numbers named in the texts in order: «(6.1)», «(3.1)-(3.5)» (expanded), «eq. (12)»; years and page
    numbers (an integer >= 200 without a dot) are not equation numbers."""
    out: list[str] = []
    for text in texts:
        t = str(text or "")
        for m in _RANGE.finditer(t):
            for n in _expand(norm_number(m.group(1)), norm_number(m.group(2))):
                if n and n not in out:
                    out.append(n)
        t = _RANGE.sub(" ", t)
        for m in _PAREN_NUM.finditer(t):
            n = norm_number(m.group(1))
            if n and ("." in n or int(re.sub(r"\D", "", n) or 0) < 200) and n not in out:
                out.append(n)
        if out:
            return out
    return out


_SOFT_SEP = re.compile(r",\s*(?=\\(?:[,;: ]|quad|qquad))|\\qquad|\\quad")


def split_latex(latex: str, soft: bool = False) -> list[str]:
    """Top-level parts of a LaTeX string that holds several equations (``;`` or ``\\\\`` outside braces); ``soft``
    also splits at ``\\quad`` and at a comma followed by spacing («a'=a+2Δ,\\; b'=b−2Δ»)."""
    parts, depth, cur, i = [], 0, [], 0
    s = latex or ""
    while i < len(s):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        m = _SOFT_SEP.match(s, i) if soft and depth == 0 else None
        if m:
            parts.append("".join(cur))
            cur = []
            i = m.end()
            continue
        if c == "\\" and i + 1 < len(s) and s[i + 1] in ",;:! ":     # a spacing command, not a separator
            cur.append(s[i:i + 2])
            i += 2
            continue
        if depth == 0 and (c == ";" or s.startswith("\\\\", i)):
            parts.append("".join(cur))
            cur = []
            i += 2 if c == "\\" else 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    parts = [re.sub(r"^(\\[,;:! ]|\s)+|(\\[,;:! ]|\s)+$", "", p) for p in parts]
    return [p for p in parts if p.strip()]


_PLAIN_TAIL = re.compile(r"\(\s*(\d{1,3}(?:\s*[.,]\s*\d{1,3}){0,2}\s*[a-zа-я]?)\s*(?:,[^)]*)?\)\s*$", re.IGNORECASE)


def parse_plain(text: str) -> dict[str, str]:
    """Number → plain-text form of the registry ``equation_plain`` («Q_ч = Q_г/(T_ф·t_c) (6.4); …»)."""
    out: dict[str, str] = {}
    for part in split_latex(text):
        m = _PLAIN_TAIL.search(part.strip())
        if m:
            n, body = norm_number(m.group(1)), part.strip()[:m.start()].strip()
            if body.endswith(("–", "-", "—")) or re.search(r"\(\s*\d[\d.,\s]*\)\s*[–—-]\s*$", body):
                continue                                   # «… (8.245)–(8.250)»: a range, not one equation
            if n and body and n not in out:
                out[n] = body
    return out


_HOMO = str.maketrans({"а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
                       "с": "c", "т": "t", "у": "y", "х": "x", "з": "3", "п": "n", "г": "r", "д": "d", "и": "u",
                       "ь": "b", "б": "6", "ч": "4", "ш": "w", "щ": "w", "ц": "u", "л": "jl", "я": "r", "ж": "x",
                       "ф": "φ", "ι": "i", "ο": "o", "ν": "v", "κ": "k", "ы": "bi"})
# inside subscripts only (Cyrillic abbreviations read by OCR as Greek or letter pairs): Г→Γ, П→Π, Л→JI, П→II
_HOMO_OCR = (("γ", "r"), ("π", "n"), ("ji", "jl"), ("ii", "n"))
_FUNC_FOLD = {"tan": "tg", "cot": "ctg", "arctan": "arctg", "arccot": "arcctg", "sinh": "sh", "cosh": "ch",
              "tanh": "th", "ln": "ln", "log": "lg"}
_OPENERS = ("open", "sup", "sub")
_BRACKETS = frozenset({"(", ")", "[", "]", "\\{", "\\}", "{", "}"})


def _fold(text: str) -> str:
    s = re.sub(r"[\s.{}\\]", "", unicodedata.normalize("NFKC", text).lower().translate(_HOMO))
    head, sep, sub = s.partition("_")
    for a, b in _HOMO_OCR:
        sub = sub.replace(a, b)
    return head + sep + sub


def _arg_end(toks: list[tuple[str, str]], i: int) -> int:
    """Index after the argument that starts at ``i``: a braced group, or one token with its scripts."""
    if i >= len(toks):
        return i
    if toks[i][0] in _OPENERS:
        depth = 0
        for k in range(i, len(toks)):
            if toks[k][0] in _OPENERS:
                depth += 1
            elif toks[k][0] == "close":
                depth -= 1
                if depth == 0:
                    return k + 1
        return len(toks)
    j = i + 1
    while j < len(toks) and toks[j][0] in ("sup", "sub"):
        j = _arg_end(toks, j)
    return j


def _linearize(toks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``\\frac A B`` → ``A / B`` (a printed fraction and an inline slash read the same)."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(toks):
        if toks[i][1] == "\\frac":
            e1 = _arg_end(toks, i + 1)
            e2 = _arg_end(toks, e1)
            out += _linearize(toks[i + 1:e1]) + [("op", "/")] + _linearize(toks[e1:e2])
            i = max(e2, i + 1)
            continue
        out.append(toks[i])
        i += 1
    return out


def _primes(toks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``a ^ {′ ′}`` → ``a′′`` (a prime written as a superscript and one written on the letter read the same)."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(toks):
        if toks[i][0] == "sup" and out and out[-1][0] == "id":
            j = i + 1
            while j < len(toks) and toks[j][0] == "op" and toks[j][1] and set(toks[j][1]) <= {"′"}:
                j += 1
            if j > i + 1 and j < len(toks) and toks[j][0] == "close":
                out[-1] = ("id", out[-1][1] + "".join(t for _, t in toks[i + 1:j]))
                i = j + 1
                continue
        out.append(toks[i])
        i += 1
    return out


_GREEK = ("alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|lambda|mu|nu|xi|pi|varpi|"
          "rho|varrho|sigma|varsigma|tau|upsilon|phi|varphi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|"
          "Upsilon|Phi|Psi|Omega")
_PRECLEAN = ((re.compile(r"\\up(" + _GREEK + r")(?![A-Za-z])"), r"\\\1"),     # \upsigma (OCR) → \sigma
             (re.compile(r"\\begin\{(?:array|matrix|aligned|align\*?|cases|split)\}(?:\{[^{}]*\})?"), " "),
             (re.compile(r"\\end\{(?:array|matrix|aligned|align\*?|cases|split)\}"), " "),
             (re.compile(r"\\operatorname\s*\{([^{}]*)\}"), r"\\mathrm{\1}"),
             (re.compile(r"\\(?:mathrm|text)\s*\{\s*-+\s*\}"), "-"))
_UNI_SUB = str.maketrans("₀₁₂₃₄₅₆₇₈₉ₐₑₒₓᵢⱼₖₘₙₚₛₜ", "0123456789aeoxijkmnpst")
_UNI_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")


def preclean(latex: str) -> str:
    """OCR and layout wrappers that carry no form: upright Greek, array environments, ``\\operatorname``."""
    s = latex or ""
    for rx, rep in _PRECLEAN:
        s = rx.sub(rep, s)
    return s


_ACCENTS = {"̄": "bar", "̅": "bar", "̇": "dot", "̂": "hat", "̃": "tilde", "⃗": "vec"}


def plain_to_latex(text: str) -> str:
    """Registry plain text → LaTeX-like input for the parser: ``K_гл`` → ``K_{гл}``, ``α₀`` → ``α_{0}``,
    ``ρ⁻¹`` → ``ρ^{-1}``, ``σ̄`` → ``\\bar{σ}``, ``tgα`` → ``tg α``, ``·`` dropped."""
    s = unicodedata.normalize("NFD", text or "")
    s = re.sub("([^\\W\\d_])([" + "".join(_ACCENTS) + "])", lambda m: "\\" + _ACCENTS[m.group(2)] + "{"
               + m.group(1) + "}", s)
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"([₀-₉ₐₑₒₓᵢⱼₖₘₙₚₛₜ]+)", lambda m: "_{" + m.group(1).translate(_UNI_SUB) + "}", s)
    s = re.sub(r"([⁰-⁹¹²³⁻⁺]+)", lambda m: "^{" + m.group(1).translate(_UNI_SUP) + "}", s)
    s = re.sub(r"(?<![\\\w])(arctg|ctg|tg|sin|cos|exp|ln|lg)(?=[^\W\d_])", r"\1 ", s)
    s = re.sub(r"_((?:[^\W_]|[.,](?=[^\W\d_]))+)", r"_{\1}", s)
    return s.replace("·", " ")


def form_tokens(latex: str | None) -> dict[str, Any]:
    """The folded token streams of a formula: full, base letters, shape (identifiers renamed); fractions read as
    slashes, brackets and braces dropped."""
    from vkm_corpus.navigation.object_duplicates import canonical_tokens

    toks = _primes(_linearize(canonical_tokens(preclean(latex or ""))))
    full, base, shape, names = [], [], [], {}
    for typ, t in toks:
        if typ in ("open", "close") or (typ == "op" and t in _BRACKETS):
            continue
        if typ == "id":
            f = _fold(t)
            full.append(f)
            base.append(f.split("_", 1)[0] or f)
            shape.append(names.setdefault(f, f"v{len(names) + 1}"))
        elif typ in ("func", "word"):
            f = _FUNC_FOLD.get(t.lower(), t.lower())
            full.append(f)
            base.append(f)
            shape.append(f)
        else:
            full.append(t)
            base.append(t)
            shape.append(t)
    return {"full": full, "base": base, "shape": shape, "relation": any(t in REL_SIGNS for t in full)}


def _ratio(a: list[str], b: list[str]) -> tuple[float, int]:
    if not a or not b:
        return 0.0, 0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    return 2.0 * matched / (len(a) + len(b)), matched


def form_similarity(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, str]:
    """Weighted best of the token measures (``PARAMS``) and the measure that gave it."""
    if not a["full"] or not b["full"]:
        return 0.0, "NO_TOKENS"
    if a["full"] == b["full"]:
        return 1.0, "KEY_EQUAL"
    r_full, m_full = _ratio(a["full"], b["full"])
    best = [(r_full, "TOKENS")]
    best.append((PARAMS["weight_base"] * _ratio(a["base"], b["base"])[0], "TOKENS_BASE"))
    shorter = min(len(a["full"]), len(b["full"]))
    if shorter >= PARAMS["shape_min_tokens"]:
        best.append((PARAMS["weight_shape"] * _ratio(a["shape"], b["shape"])[0], "SHAPE_RENAMED"))
    if shorter >= PARAMS["containment_min_tokens"]:
        m_base = _ratio(a["base"], b["base"])[1]
        best.append((PARAMS["weight_containment"] * max(m_full, m_base) / shorter, "CONTAINMENT"))
    score, method = max(best, key=lambda x: (x[0], x[1] == "TOKENS"))
    return round(min(score, 1.0), 3), method


# ================================================================================================ inputs
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def split_ids(value: Any) -> list[str]:
    return [x.strip() for x in str(value or "").split(";") if x.strip()]


_LOC_SEG = re.compile(r"((?:VKM|EXT)-SRC-\d{3})\s+p\.\s*(\d+)(?:\s*\(печ\.\s*([^)]*)\))?\s*(?:\[([^\]]*)\])?")


def parse_locator(locator: str) -> list[dict[str, Any]]:
    """Segments «VKM-SRC-014 p.151 (печ. 151) [формула (6.1)]» of a registry locator."""
    out = []
    for m in _LOC_SEG.finditer(locator or ""):
        out.append({"source_id": m.group(1), "pdf_page": int(m.group(2)), "printed_page": (m.group(3) or "").strip(),
                    "extra": m.group(4) or ""})
    return out


class Inputs:
    """The PRIVATE inputs actually read, with SHA-256 (logical paths relative to ``11_evidence_vnext``)."""

    def __init__(self, ev_root: Path) -> None:
        self.root = ev_root
        self.files: dict[str, str] = {}

    def path(self, rel: str) -> Path:
        p = self.root / rel
        if p.is_file():
            self.files.setdefault(rel, sha256_file(p))
        return p


def load_records(inp: Inputs, vn_ids: set[str]) -> dict[str, dict[str, Any]]:
    """vn_id → source, pdf_page, printed page, kind, locator text, equation number and LaTeX of the record."""
    mp = {r["vn_id"]: r for r in read_csv(inp.path(MAP_REL))}
    idx = {r["vn_id"]: r for r in read_csv(inp.path(IDX_REL))}
    by_dir: dict[str, dict[int, str]] = defaultdict(dict)
    for vn in vn_ids:
        if vn in idx:
            by_dir[idx[vn]["dir"]][int(idx[vn]["line"])] = vn
    raw: dict[str, dict[str, Any]] = {}
    for d in sorted(by_dir):
        rel = f"sweep_raw/{d}/records.jsonl"
        p = inp.path(rel)
        if not p.is_file():
            continue
        want = by_dir[d]
        with open(p, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if n in want and line.strip():
                    raw[want[n]] = json.loads(line)
    out: dict[str, dict[str, Any]] = {}
    for vn in sorted(vn_ids):
        rec, m, ix = raw.get(vn) or {}, mp.get(vn) or {}, idx.get(vn) or {}
        data = rec.get("data") or {}
        page = rec.get("pdf_page") or m.get("pdf_page") or ix.get("pdf_page")
        try:
            page = int(page) if page not in (None, "") else None
        except (TypeError, ValueError):
            page = None
        out[vn] = {"vn_id": vn, "source_id": rec.get("source_id") or m.get("source_id") or ix.get("source_id") or "",
                   "pdf_page": page, "printed_page": str(rec.get("printed_page") or ""),
                   "kind": rec.get("kind") or m.get("kind") or ix.get("kind") or "",
                   "locator_extra": rec.get("locator_extra") or m.get("locator_extra") or "",
                   "equation_number": str(data.get("equation_number") or ""),
                   "latex": str(data.get("equation_latex") or ""), "found": bool(rec)}
    return out


def build_units(models: list[dict[str, str]], records: dict[str, dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Matchable units and the NOT_FOUND rows of sources without a page (see the module docstring, step 1)."""
    units: list[dict[str, Any]] = []
    pageless: list[dict[str, Any]] = []
    for m in models:
        mid = m["model_id"]
        mine: dict[tuple, dict[str, Any]] = {}

        plain = parse_plain(m.get("equation_plain") or "")
        reg_latex = m.get("equation_latex") or ""
        single = len(plain) <= 1 and len(parse_numbers(m.get("equation_plain"))) <= 1

        def form_of(number: str, latex: str, basis: str) -> tuple[str, str]:
            """The registry form of one equation: the record's LaTeX, else the registry plain text of that number,
            else the registry LaTeX of a one-equation model."""
            if latex:
                return latex, basis
            if number and number in plain:
                return plain_to_latex(plain[number]), "REGISTRY_PLAIN"
            if single and reg_latex:
                return reg_latex, "REGISTRY_LATEX"
            return "", ""

        def add(source: str, number: str, page: int | None, latex: str, basis: str, vn: str, origin: str,
                kind: str) -> None:
            key = (source, number) if number else (source, "-", vn, page)
            latex, basis = form_of(number, latex, basis)
            u = mine.get(key)
            if u is None:
                u = mine[key] = {"model_id": mid, "equation_ref": number or "-", "source_id": source, "vn_ids": [],
                                 "pages": [], "latex": latex, "form_basis": basis, "origin": origin, "kinds": set()}
            if vn and vn not in u["vn_ids"]:
                u["vn_ids"].append(vn)
            if page is not None and page not in u["pages"]:
                u["pages"].append(page)
            if latex and (not u["latex"] or (u["form_basis"] != "RECORD_LATEX" and basis == "RECORD_LATEX")):
                u["latex"], u["form_basis"] = latex, basis
            if kind:
                u["kinds"].add(kind)

        for vn in split_ids(m.get("vn_ids")):
            r = records.get(vn)
            if r is None:
                continue
            nums = parse_numbers(r["equation_number"], r["locator_extra"])
            parts = split_latex(r["latex"]) if len(nums) > 1 else []
            for i, n in enumerate(nums):
                # a record naming several numbers: its LaTeX counts per equation only when it splits into as many
                lx = r["latex"] if len(nums) == 1 else parts[i] if len(parts) == len(nums) else ""
                add(r["source_id"], n, r["pdf_page"], lx, "RECORD_LATEX", vn, "RECORD", r["kind"])
            if not nums:
                add(r["source_id"], "", r["pdf_page"], r["latex"], "RECORD_LATEX", vn, "RECORD", r["kind"])
        covered = {u["source_id"] for u in mine.values()}
        for seg in parse_locator(m.get("locator") or ""):
            if seg["source_id"] in covered or not seg["source_id"].startswith("VKM-SRC-"):
                continue
            nums = parse_numbers(seg["extra"])
            for n in nums or [""]:
                add(seg["source_id"], n, seg["pdf_page"], "", "", "", "LOCATOR", "")
        covered = {u["source_id"] for u in mine.values()}
        units.extend(sorted(mine.values(), key=lambda u: (u["source_id"], _num_key(u["equation_ref"]),
                                                          min(u["pages"] or [0]), u["vn_ids"][:1])))
        sources = split_ids(m.get("source_ids"))
        for s in sources:
            if s in covered:
                continue
            reason = ("TEXTBOOK_FORM_NO_SOURCE_PAGE" if s.upper().startswith("TEXTBOOK") else
                      "EXTERNAL_SOURCE_NOT_IN_CORPUS" if s.startswith("EXT-") else
                      "NO_RECORD_OR_LOCATOR_PAGE_FOR_SOURCE")
            pageless.append({"model_id": mid, "source_id": s, "reason": reason})
        if not sources and not mine:
            pageless.append({"model_id": mid, "source_id": "-", "reason": "NO_CORPUS_RECORD"})
    for u in units:
        u["link_id"] = link_id(u["model_id"], u["source_id"], u["equation_ref"], u["vn_ids"], u["pages"])
    return units, pageless


def _num_key(n: str) -> tuple:
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.]", n)) if n != "-" else ((2, ""),)


def link_id(model_id: str, source_id: str, ref: str, vn_ids: list[str], pages: list[int]) -> str:
    tail = "" if ref != "-" else "|" + ",".join(sorted(vn_ids)) + "|" + ",".join(str(p) for p in sorted(pages))
    return "MFL-" + hashlib.sha256(f"{model_id}|{source_id}|{ref}{tail}".encode("utf-8")).hexdigest()[:12]


# ================================================================================================ API
class Api:
    """Sequential read-only client with a response cache (one JSON file per request)."""

    def __init__(self, base: str | None, token_file: Path | None, cache: Path, offline: bool) -> None:
        self.base, self.cache, self.offline = (base or "").rstrip("/"), cache, offline
        self.cache.mkdir(parents=True, exist_ok=True)
        self.calls = self.hits = 0
        self.snapshots: Counter = Counter()
        self._client = None
        self._token = None
        if not offline:
            if not self.base:
                raise SystemExit("set VKM_API_URL (or use --offline with a filled cache)")
            tf = token_file or Path.home() / ".vkm" / "secrets" / "api_read_token"
            self._token = tf.read_text(encoding="utf-8").strip()

    def _key(self, method: str, path: str, body: Any) -> Path:
        h = hashlib.sha256(json.dumps([method, path, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return self.cache / f"{h[:32]}.json"

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
        key = self._key(method, path, body)
        if key.is_file():
            self.hits += 1
            got = json.loads(key.read_text(encoding="utf-8"))
        else:
            if self.offline:
                raise SystemExit(f"--offline: no cached response for {method} {path}")
            import httpx

            if self._client is None:
                self._client = httpx.Client(base_url=self.base, timeout=120,
                                            headers={"Authorization": f"Bearer {self._token}"})
            for attempt in range(4):
                try:
                    r = (self._client.get(path, params=body) if method == "GET" else
                         self._client.post(path, json=body))
                    if r.status_code >= 500 and attempt < 3:
                        time.sleep(2 * (attempt + 1))
                        continue
                    break
                except httpx.TransportError:
                    if attempt == 3:
                        raise
                    time.sleep(2 * (attempt + 1))
            self.calls += 1
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            got = {"status": r.status_code, "body": payload}
            if r.status_code < 500:
                tmp = key.with_suffix(".tmp")
                tmp.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, key)
        snap = ((got.get("body") or {}).get("meta") or {}).get("canonical_snapshot_id")
        if snap:
            self.snapshots[snap] += 1
        return got["status"], got.get("body") or {}


def fetch_source(api: Api, sid: str) -> dict[str, Any] | None:
    status, body = api.request("GET", f"/v1/source/{sid}")
    if status != 200:
        return None
    rec = (body.get("item") or {}).get("record") or {}
    ps = rec.get("processing_summary") or {}
    return {"page_count": int(ps.get("page_count") or ps.get("pages_total") or 0),
            "n_formulas": int(ps.get("n_formulas") or 0),
            "processing": ps.get("document_processing_status") or ""}


def _ranges(pages: Iterable[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for p in sorted(set(pages)):
        if out and p == out[-1][1] + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def fetch_pages(api: Api, sid: str, pages: set[int]) -> dict[int, str]:
    """page_index → page_label of the canonical pages (only the pages that exist)."""
    labels: dict[int, str] = {}
    for a, b in _ranges(pages):
        cursor = None
        while True:
            params: dict[str, Any] = {"from_page": a, "to_page": b, "limit": 500}
            if cursor:
                params["cursor"] = cursor
            status, body = api.request("GET", f"/v1/source/{sid}/pages", params)
            if status != 200:
                break
            for it in body.get("items") or []:
                env = it.get("envelope") or {}
                if env.get("page_index") is not None:
                    labels[int(env["page_index"])] = env.get("page_label") or ""
            cursor = body.get("next_cursor")
            if not cursor:
                break
    return labels


QUERY_LIMIT = 200


def fetch_formulas(api: Api, sid: str, pages: set[int], truncated: list[str]) -> dict[int, list[dict[str, Any]]]:
    """page_index → formula objects (id, kind, canonical label, LaTeX) of the pages.

    ``POST /v1/objects/query`` orders by object id and (API 0.1.0) gives no ``next_cursor`` for one kind, so a
    full answer (``QUERY_LIMIT`` items) may be cut anywhere: such a page range is halved until every answer is
    below the limit; a single page that still fills the limit is recorded in ``truncated``."""
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    todo = list(reversed(_ranges(pages)))
    while todo:
        a, b = todo.pop()
        body_: dict[str, Any] = {"kinds": ["FORMULA"], "source_ids": [sid], "page_from": a, "page_to": b,
                                 "limit": QUERY_LIMIT}
        status, body = api.request("POST", "/v1/objects/query", body_)
        if status != 200:
            raise SystemExit(f"objects query failed for {sid} {a}-{b}: HTTP {status}")
        items = body.get("items") or []
        if len(items) >= QUERY_LIMIT and a < b:
            mid = (a + b) // 2
            todo += [(mid + 1, b), (a, mid)]
            continue
        if len(items) >= QUERY_LIMIT:
            truncated.append(f"{sid}:p{a:04d}")
        for it in items:
            env, rec = it.get("envelope") or {}, it.get("record") or {}
            by_page[int(env.get("page_index") or 0)].append({
                "formula_id": env.get("object_id"), "page_id": env.get("page_id"),
                "page_index": int(env.get("page_index") or 0), "page_label": env.get("page_label") or "",
                "formula_kind": rec.get("formula_kind") or "", "label": rec.get("equation_label") or "",
                "latex": rec.get("normalized_latex") or "", "text_layer": env.get("text_layer") or ""})
    for p in by_page:
        by_page[p].sort(key=lambda f: f["formula_id"])
    return by_page


def fetch_nav(api: Api, fid: str) -> dict[str, Any]:
    status, body = api.request("GET", f"/v1/nav/formula/{fid}")
    rec = (body.get("item") or {}).get("record") or {} if status == 200 else {}
    where = " ".join((w.get("text") or "") for w in rec.get("where") or [])
    return {"equation_number": rec.get("equation_number") or "", "number_method": rec.get("number_method") or "",
            "where": re.sub(r"\s+", " ", where)[:300], "intro": re.sub(r"\s+", " ", (rec.get("intro") or {}).get(
                "text") or "")[:200], "ok": status == 200}


def fetch_shared(api: Api, fid: str) -> tuple[str, str]:
    status, body = api.request("GET", "/v1/nav/shared_formulas", {"ref": fid, "renamed": "true", "limit": 50})
    if status != 200:
        return "", ""
    rec = (body.get("item") or {}).get("record") or {}
    clusters = sorted(c.get("cluster_id") for c in rec.get("clusters") or [] if c.get("cluster_id"))
    n_works = rec.get("n_works")
    return ";".join(clusters), "" if n_works is None else str(n_works)


# ================================================================================================ matching
def unit_forms(latex: str, tok: Any) -> list[tuple[dict[str, Any], bool]] | None:
    """Token streams of a registry form: the whole and, when it holds several equations, each part."""
    if not latex:
        return None
    forms = [(tok(latex), False)]
    parts = split_latex(latex, soft=True)
    if len(parts) > 1:
        forms += [(tok(p), True) for p in parts]
    return forms


def best_similarity(forms: list[tuple[dict[str, Any], bool]], ft: dict[str, Any]) -> tuple[float, str]:
    best = (0.0, "NO_TOKENS")
    for t, part in forms:
        score, method = form_similarity(t, ft)
        if score > best[0]:
            best = (score, method + ("+PART" if part else ""))
    return best


def needs_nav(f: dict[str, Any], toks: dict[str, Any]) -> bool:
    if f["formula_kind"] == "DISPLAY" or f["label"]:
        return True
    return bool(toks["relation"]) and len(toks["full"]) >= PARAMS["nav_min_tokens"]


def prove_shifts(units: list[dict], formulas: dict[str, dict[int, list[dict]]], tok: Any) -> dict[str, dict]:
    """Per source: votes of strong form matches / canonical labels for each offset; the accepted shift."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for u in units:
        if not u["pages"] or u["source_id"] not in formulas:
            continue
        page = min(u["pages"])
        fs = formulas[u["source_id"]]
        ut = unit_forms(u["latex"], tok)
        best: dict[int, float] = {}
        label_hit: set[int] = set()
        for d in range(-PARAMS["shift_probe"], PARAMS["shift_probe"] + 1):
            for f in fs.get(page + d, []):
                if ut is not None:
                    best[d] = max(best.get(d, 0.0), best_similarity(ut, tok(f["latex"]))[0])
                if u["equation_ref"] != "-" and norm_number(f["label"]) == u["equation_ref"]:
                    label_hit.add(d)
        if len(label_hit) == 1:
            votes[u["source_id"]][next(iter(label_hit))] += 1
            continue
        if best:
            top = max(best.values())
            at = [d for d, v in best.items() if v == top]
            second = max([v for d, v in best.items() if d not in at] or [0.0])
            if top >= PARAMS["shift_vote_min_form"] and len(at) == 1 and top - second >= 0.05:
                votes[u["source_id"]][at[0]] += 1
    out = {}
    for sid, c in sorted(votes.items()):
        total = sum(c.values())
        d, n = max(c.items(), key=lambda x: (x[1], -abs(x[0])))
        shift = d if d != 0 and n >= PARAMS["shift_min_votes"] and n / total >= PARAMS["shift_min_share"] else 0
        out[sid] = {"votes": {str(k): v for k, v in sorted(c.items())}, "shift": shift}
    return out


def choose(u: dict[str, Any], cands: list[dict[str, Any]], checks: dict[str, tuple[str, str]]
           ) -> tuple[dict[str, Any] | None, str, str]:
    """Best candidate, status and method (see the module docstring, steps 4–5)."""
    def rank(c: dict[str, Any]) -> tuple:
        return (-c["form"], c["dist"], c["formula_kind"] != "DISPLAY", -c["n_tokens"], c["formula_id"])

    picked = [c for c in cands if checks.get(c["formula_id"], ("", ""))[0] == "CONFIRMED"]
    if picked:
        c = sorted(picked, key=rank)[0]
        st = status_of(c, u)
        return c, (st if st != "NOT_FOUND" else "CANDIDATE"), c["method"] + "+REVIEW_PICK"
    numbered = [c for c in cands if c["number_match"] == "Y"]
    if numbered:
        c = sorted(numbered, key=rank)[0]
        return c, status_of(c, u), c["method"]
    if cands:
        c = sorted(cands, key=rank)[0]
        st = status_of(c, u)
        if st != "NOT_FOUND":
            return c, st, c["method"]
    return None, "NOT_FOUND", ""


def status_of(c: dict[str, Any], u: dict[str, Any]) -> str:
    if c["number_match"] == "Y":
        return "EXACT" if u["latex"] and c["form"] >= PARAMS["exact_min_form"] else "NUMBER_MATCH"
    if c["form"] >= PARAMS["form_match_min"]:
        return "FORM_MATCH"
    if c["form"] >= PARAMS["candidate_min"]:
        return "CANDIDATE"
    return "NOT_FOUND"


def build(ev_root: Path, api: Api, work: Path, run_date: str, command: str) -> dict[str, Any]:
    from vkm_corpus.navigation.object_duplicates import canonical_formula  # noqa: F401  (import check)

    inp = Inputs(ev_root)
    models = read_csv(inp.path(REG_REL))
    vn_all = {vn for m in models for vn in split_ids(m.get("vn_ids"))}
    records = load_records(inp, vn_all)
    units, pageless = build_units(models, records)
    checks_path = inp.path(CHECKS_REL)
    checks: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    if checks_path.is_file():
        for r in read_csv(checks_path):
            if r.get("check") not in CHECKS:
                raise SystemExit(f"bad check value in {CHECKS_REL}: {r.get('check')!r}")
            checks[r["link_id"]][r["formula_id"] or "-"] = (r["check"], (r.get("check_note") or "").strip())

    tok_cache: dict[str, dict[str, Any]] = {}

    def tok(latex: str) -> dict[str, Any]:
        if latex not in tok_cache:
            tok_cache[latex] = form_tokens(latex)
        return tok_cache[latex]

    # ---- canon: sources, pages, formulas of the probe windows
    src_ids = sorted({u["source_id"] for u in units})
    sources = {sid: fetch_source(api, sid) for sid in src_ids}
    labels: dict[str, dict[int, str]] = {}
    formulas: dict[str, dict[int, list[dict]]] = {}
    truncated: list[str] = []
    for sid in src_ids:
        info = sources[sid]
        if info is None:
            continue
        probe = PARAMS["shift_probe"] + PARAMS["page_window"]
        want = {p + d for u in units if u["source_id"] == sid for p in u["pages"] for d in range(-probe, probe + 1)}
        want = {p for p in want if 1 <= p <= max(info["page_count"], 1)}
        labels[sid] = fetch_pages(api, sid, want)
        formulas[sid] = fetch_formulas(api, sid, set(labels[sid]), truncated)
    shifts = prove_shifts(units, formulas, tok)

    # ---- per unit: candidates of the window, NAV numbers, measures, choice
    rows: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    names = {m["model_id"]: m.get("name_ru") or "" for m in models}
    seen_cands: dict[str, set[str]] = {}
    snapshot = api.snapshots.most_common(1)[0][0] if api.snapshots else ""
    nav_cache: dict[str, dict[str, Any]] = {}
    for u in units:
        sid = u["source_id"]
        shift = (shifts.get(sid) or {}).get("shift", 0)
        base = {"model_id": u["model_id"], "equation_ref": u["equation_ref"], "source_id": sid,
                "vn_ids": ";".join(u["vn_ids"]), "link_id": u["link_id"],
                "page_basis": ("LOCATOR_PDF_PAGE" if u["origin"] == "LOCATOR" else "PDF_PAGE")
                + (f"_SHIFT{shift:+d}" if shift else "")}
        info = sources.get(sid)
        reason = ""
        anchor = min(u["pages"]) + shift if u["pages"] else None
        if anchor is not None:
            base.update(page_id=f"{sid}:p{anchor:04d}", page_index=anchor,
                        page_label=(labels.get(sid) or {}).get(anchor, ""))
        if not u["pages"]:
            reason = "NO_PAGE_IN_RECORD"
        elif info is None:
            reason = "SOURCE_NOT_IN_CANON"
        elif info["n_formulas"] == 0:
            reason = "SOURCE_HAS_NO_FORMULA_OBJECTS"
        elif u["equation_ref"] == "-" and not u["latex"]:
            reason = "RECORD_HAS_NO_EQUATION"
        window = sorted({p + shift + d for p in u["pages"] for d in range(-PARAMS["page_window"],
                                                                          PARAMS["page_window"] + 1)})
        if not reason:
            exist = [p for p in window if p in labels.get(sid, {})]
            if not exist:
                reason = "PAGE_NOT_IN_CANON"
            elif not any(formulas[sid].get(p) for p in exist):
                reason = "NO_FORMULAS_ON_PAGES"
        if reason:
            rows.append(dict(base, match_status="NOT_FOUND", reason=reason, n_candidates=0))
            continue
        ut = unit_forms(u["latex"], tok)
        cands = []
        for p in window:
            for f in formulas[sid].get(p, []):
                ft = tok(f["latex"])
                form, method = best_similarity(ut, ft) if ut is not None else (0.0, "NO_REGISTRY_FORM")
                if ut is not None:
                    method = f"{method}@{u['form_basis']}"
                number = ""
                if needs_nav(f, ft) or (ut is not None and form >= PARAMS["candidate_min"]):
                    if f["formula_id"] not in nav_cache:
                        nav_cache[f["formula_id"]] = fetch_nav(api, f["formula_id"])
                    number = norm_number(nav_cache[f["formula_id"]]["equation_number"])
                number = number or norm_number(f["label"])
                nm = "-" if u["equation_ref"] == "-" else ("Y" if number and number == u["equation_ref"] else "N")
                dist = min(abs(f["page_index"] - (q + shift)) for q in u["pages"])
                cands.append(dict(f, form=form, method=method, number=number, number_match=nm, dist=dist,
                                  n_tokens=len(ft["full"])))
        uchecks = checks.get(u["link_id"], {})
        seen_cands[u["link_id"]] = {x["formula_id"] for x in cands}
        c, status, method = choose(u, cands, uchecks)
        row = dict(base, n_candidates=len(cands))
        if c is None:
            why = ("CANON_FORMULAS_WITHOUT_LATEX" if not any(x["latex"].strip() for x in cands) else
                   "NUMBER_NOT_IN_CANON_NO_REGISTRY_FORM" if ut is None else "NO_MATCHING_FORMULA")
            row.update(match_status="NOT_FOUND", reason=why, number_match="-" if u["equation_ref"] == "-" else "N")
        else:
            nearest = min(u["pages"], key=lambda q: (abs(c["page_index"] - q), q))
            row.update(page_id=c["page_id"], page_index=c["page_index"], page_label=c["page_label"],
                       formula_id=c["formula_id"], formula_kind=c["formula_kind"],
                       equation_number_canon=c["number"], match_status=status, number_match=c["number_match"],
                       form_similarity="" if ut is None else f"{c['form']:.3f}",
                       page_offset=c["page_index"] - nearest, method=method)
            better = [x for x in cands if x is not c and x["form"] >= PARAMS["form_match_min"] and x["form"] > c["form"]]
            notes = []
            if ut is None:
                notes.append("NO_REGISTRY_FORM")
            if status == "FORM_MATCH" and c["number"] and u["equation_ref"] != "-":
                notes.append("CANON_NUMBER_DIFFERS")
            if status in ("FORM_MATCH", "CANDIDATE") and not c["number"] and u["equation_ref"] != "-":
                notes.append("NO_CANON_NUMBER")
            if better:
                notes.append("BETTER_FORM_ELSEWHERE")
            row["reason"] = ";".join(notes)
        ck = uchecks.get(row.get("formula_id") or "-")
        if ck:
            row["check"], row["check_note"] = ck
        rows.append(row)
        if status in ("NUMBER_MATCH", "FORM_MATCH", "CANDIDATE") or (status == "NOT_FOUND" and cands) or (
                status == "EXACT" and c is not None and c["form"] < 0.7):      # EXACT with a weak form: spot check
            alts = sorted(cands, key=lambda x: (x is not c, -x["form"], x["dist"], x["formula_id"]))[:4]
            review.append({"row": row, "unit": u, "chosen": c, "alts": alts, "name": names.get(u["model_id"], ""),
                           "nav": nav_cache.get(row.get("formula_id") or "", {})})
    for pl in pageless:
        rows.append({"model_id": pl["model_id"], "equation_ref": "-", "source_id": pl["source_id"], "vn_ids": "",
                     "link_id": link_id(pl["model_id"], pl["source_id"], "-", [], []), "match_status": "NOT_FOUND",
                     "reason": pl["reason"], "n_candidates": 0, "page_basis": "NONE"})

    # ---- shared formulas («the same record in other books») for the linked formulas
    for row in rows:
        if row.get("formula_id") and row["match_status"] != "NOT_FOUND":
            row["shared_group_id"], row["shared_n_works"] = fetch_shared(api, row["formula_id"])
    order = {m["model_id"]: i for i, m in enumerate(models)}
    rows.sort(key=lambda r: (order.get(r["model_id"], 10 ** 6), r["source_id"], _num_key(r["equation_ref"]),
                             r.get("page_index") or 0, r["link_id"]))
    for r in rows:
        r.update(canonical_snapshot_id=snapshot, rule_version=RULE_VERSION, review_status=REVIEW_STATUS,
                 epistemic_status=EPISTEMIC_STATUS)
        r.setdefault("check", "NOT_REVIEWED")
        if r["match_status"] == "NOT_FOUND" and r["check"] == "NOT_REVIEWED":
            r["check"] = ""
    # a check is stale when its link is gone or its formula is no longer a candidate of the link
    stale = sorted((lid, fid) for lid, d in checks.items() for fid in d
                   if lid not in seen_cands or (fid != "-" and fid not in seen_cands[lid]))

    dup = sorted(k for k, n in Counter(r["link_id"] for r in rows).items() if n > 1)
    if dup:
        raise SystemExit(f"duplicate link ids: {dup[:5]}")

    # ---- write
    out_path = ev_root / OUT_REL
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: "" if r.get(k) is None else r.get(k) for k in COLUMNS})
    data = buf.getvalue().encode("utf-8")
    tmp = out_path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out_path)
    write_review(work / "review_sheet.md", review)
    write_review_compact(work / "review_compact.tsv", review)

    by_status = Counter(r["match_status"] for r in rows)
    linked = [r for r in rows if r["match_status"] != "NOT_FOUND" and r.get("check") != "REJECTED"]
    counts = {
        "models_total": len(models),
        "models_with_link": len({r["model_id"] for r in linked}),
        "models_with_exact": len({r["model_id"] for r in rows if r["match_status"] == "EXACT"}),
        "models_without_link": sorted({m["model_id"] for m in models} - {r["model_id"] for r in linked}),
        "rows": len(rows), "units": len(units), "pageless_rows": len(pageless),
        "by_status": {s: by_status.get(s, 0) for s in STATUSES},
        "by_check": dict(sorted(Counter(r.get("check") or "-" for r in rows).items())),
        "by_status_and_check": dict(sorted(Counter(f"{r['match_status']}/{r.get('check') or '-'}"
                                                   for r in rows).items())),
        "not_found_by_reason": dict(sorted(Counter(r["reason"] for r in rows
                                                   if r["match_status"] == "NOT_FOUND").items())),
        "page_offset": dict(sorted(Counter(str(r.get("page_offset")) for r in rows
                                           if r["match_status"] != "NOT_FOUND").items())),
        "distinct_formulas_linked": len({r["formula_id"] for r in linked}),
        "with_shared_group": sum(1 for r in linked if r.get("shared_group_id")),
        "checks_stale": [f"{a}|{b}" for a, b in stale],
        "pages_truncated_by_query_limit": truncated,
        "review_sheet_rows": len(review),
    }
    receipt = {
        "rule_version": RULE_VERSION, "run_date": run_date, "command": command,
        "canonical_snapshot_id": snapshot, "snapshots_seen": dict(api.snapshots),
        "inputs": [{"path": f"PRIVATE:11_evidence_vnext/{rel}", "sha256": h} for rel, h in sorted(inp.files.items())],
        "code": [{"path": rel, "sha256": sha256_file(ROOT / rel)} for rel in CODE_FILES],
        "api": {"base": "VKM_API_URL (read-only)", "endpoints": ["GET /v1/source/{id}", "GET /v1/source/{id}/pages",
                                                                  "POST /v1/objects/query", "GET /v1/nav/formula/{id}",
                                                                  "GET /v1/nav/shared_formulas"],
                "calls": api.calls, "cache_hits": api.hits},
        "params": PARAMS, "source_shifts": shifts,
        "sources": {sid: (info or {"status": "NOT_IN_CANON"}) for sid, info in sorted(sources.items())},
        "output": {"path": f"PRIVATE:11_evidence_vnext/{OUT_REL}", "sha256": hashlib.sha256(data).hexdigest(),
                   "rows": len(rows), "columns": list(COLUMNS)},
        "counts": counts,
        "review_status": REVIEW_STATUS, "epistemic_status": EPISTEMIC_STATUS,
        "note": ("navigation links «the model's equation is printed as this formula object»: AUTO_EXTRACTED_"
                 "UNREVIEWED / DERIVATION; the agent check of the disputed rows (check, check_note) is not a human "
                 "review; never a check of the law, never evidence"),
    }
    rdir = ev_root / RECEIPT_DIR_REL
    rdir.mkdir(parents=True, exist_ok=True)
    rpath = rdir / f"model_formula_links_{run_date}.json"
    rpath.write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return receipt


def write_review(path: Path, review: list[dict[str, Any]]) -> None:
    """The disputed links with both LaTeX forms and the NAV context (git-ignored work file, PRIVATE content)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# model_formula_links — review sheet (work file, not published)", ""]
    for x in review:
        r, u = x["row"], x["unit"]
        lines.append(f"## {r['link_id']} {r['model_id']} ({r['equation_ref']}) {r['source_id']} "
                     f"pages {u['pages']} → {r['match_status']} {r.get('reason') or ''}")
        lines.append(f"- REG: `{u['latex']}`")
        nav = x.get("nav") or {}
        for a in x["alts"]:
            mark = "**" if a is x["chosen"] else ""
            lines.append(f"- {mark}{a['formula_id']}{mark} p{a['page_index']} {a['formula_kind']} {a['text_layer']} "
                         f"num={a['number'] or '-'} form={a['form']:.3f} ({a['method']}): `{a['latex'][:300]}`")
        if nav:
            lines.append(f"- where: {nav.get('where', '')[:250]}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_review_compact(path: Path, review: list[dict[str, Any]]) -> None:
    """One line per disputed link: the chosen formula and the best other one, both forms cut short (work file)."""
    def cut(text: Any, n: int) -> str:
        return re.sub(r"\s+", " ", str(text or ""))[:n]

    out = ["\t".join(("link_id", "status", "reason", "model", "name", "ref", "pages", "chosen", "num", "form",
                      "canon", "registry", "other", "where"))]
    for x in review:
        r, u, c = x["row"], x["unit"], x["chosen"]
        others = [a for a in x["alts"] if a is not c]
        o = others[0] if others else None
        out.append("\t".join((
            r["link_id"], r["match_status"], r.get("reason") or "", r["model_id"], cut(x.get("name"), 50),
            r["equation_ref"], ",".join(str(p) for p in u["pages"]),
            f"{c['formula_id'].split(':', 1)[1]} {c['formula_kind'][:1]}{c['text_layer'][:3]}" if c else "-",
            (c or {}).get("number") or "-", f"{c['form']:.2f}" if c else "-", cut((c or {}).get("latex"), 170),
            cut(u["latex"], 170),
            f"{o['formula_id'].split(':', 2)[-1]} n={o['number'] or '-'} f={o['form']:.2f} {cut(o['latex'], 90)}"
            if o else "", cut((x.get("nav") or {}).get("where"), 110))))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true", help="use the cached API responses only")
    ap.add_argument("--date", default=date.today().isoformat(), help="run date for the receipt name (YYYY-MM-DD)")
    ap.add_argument("--work", type=Path, default=ROOT / "work" / "model_formula_links",
                    help="git-ignored work directory (API cache, review sheet)")
    args = ap.parse_args()
    res = os.environ.get("VKM_RESOURCES_ROOT")
    if not res:
        print("set VKM_RESOURCES_ROOT to the private resources repository", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print("--date is YYYY-MM-DD", file=sys.stderr)
        return 2
    token = os.environ.get("VKM_API_TOKEN_FILE")
    api = Api(os.environ.get("VKM_API_URL"), Path(token) if token else None, args.work / "api_cache", args.offline)
    command = ("VKM_RESOURCES_ROOT=<PRIVATE> VKM_API_URL=<API> python scripts/build_model_formula_links.py"
               + (" --offline" if args.offline else "") + f" --date {args.date}")
    receipt = build(Path(res) / "11_evidence_vnext", api, args.work, args.date, command)
    c = receipt["counts"]
    print(json.dumps({"rows": c["rows"], "by_status": c["by_status"], "models_with_link": c["models_with_link"],
                      "not_found_by_reason": c["not_found_by_reason"], "api_calls": receipt["api"]["calls"],
                      "cache_hits": receipt["api"]["cache_hits"], "snapshot": receipt["canonical_snapshot_id"],
                      "shifts": {k: v["shift"] for k, v in receipt["source_shifts"].items() if v["shift"]}},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
