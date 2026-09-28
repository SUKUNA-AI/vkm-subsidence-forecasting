"""Text analysis of the VKM search index (``vkm_text``, ``vkm_exact``, ``vkm_latex``) — a versioned MODEL_CHOICE of
retrieval, not scientific data (DN-E14). Any change bumps ``ANALYSIS_VERSION`` and means a new index build.

``vkm_text`` handles mixed Russian/English text in one field: ё→е, invisible characters and super/subscript digits
folded; Russian and English stop words; Snowball-Russian, then Porter-English (each leaves the other script alone).

Protected terms. Snowball-Russian strips «-ит» as a verb ending, so the nominative «сильвинит» becomes «сильвин» — the
name of a *different* mineral — and so does the prepositional «(в) сильвините» («-ите» is a verb ending too). A
``keyword_marker`` on the lemma alone does not fix the prepositional case. Therefore:

* ``vkm_protected_terms`` (``keyword_marker``) keeps the lemmas unstemmed;
* ``vkm_protected_forms`` (``stemmer_override``) maps every generated case form (masculine hard-stem paradigm) to its
  lemma, so all forms of «сильвинит» meet in one token and never collide with «сильвин».
"""
from __future__ import annotations

import hashlib
import json

ANALYSIS_VERSION = "vkm-analysis/1"

# minerals and rocks of salt/potash geology whose names end in «-ит» (plus «сильвин», kept distinct from «сильвинит»)
PROTECTED_LEMMAS: tuple[str, ...] = (
    "сильвинит", "сильвин", "карналлит", "карналит", "галит", "ангидрит", "доломит", "кизерит", "каинит", "бишофит",
    "полигалит", "лангбейнит", "галопелит", "аргиллит", "алевролит", "эпсомит", "глауберит", "кварцит", "эвапорит",
)
# masculine hard-stem noun paradigm: nom, gen, dat, instr, prep (sg) + nom, gen, dat, instr, prep (pl)
_CASE_ENDINGS: tuple[str, ...] = ("", "а", "у", "ом", "е", "ы", "ов", "ам", "ами", "ах")

SUPSUB_FOLD: tuple[str, ...] = (
    "⁰ => 0", "¹ => 1", "² => 2", "³ => 3", "⁴ => 4", "⁵ => 5", "⁶ => 6", "⁷ => 7", "⁸ => 8", "⁹ => 9", "⁻ => -",
    "⁺ => +", "₀ => 0", "₁ => 1", "₂ => 2", "₃ => 3", "₄ => 4", "₅ => 5", "₆ => 6", "₇ => 7", "₈ => 8", "₉ => 9",
)
LATEX_TOKEN_PATTERN = r"(\\[A-Za-z]+|\p{L}+|[0-9]+(?:\.[0-9]+)?)"


def protected_forms() -> dict[str, str]:
    """form → lemma for every protected lemma (lemma → itself included)."""
    out: dict[str, str] = {}
    for lemma in PROTECTED_LEMMAS:
        for ending in _CASE_ENDINGS:
            form = lemma + ending
            if form in out and out[form] != lemma:
                raise ValueError(f"ambiguous protected form {form}: {out[form]} vs {lemma}")
            out[form] = lemma
    return out


def analysis_settings() -> dict:
    forms = protected_forms()
    text_char_filters = ["vkm_invisible_strip", "vkm_supsub_fold", "vkm_yo_fold"]
    return {
        "char_filter": {
            "vkm_invisible_strip": {"type": "pattern_replace",
                                    "pattern": "[\\u00AD\\u200B-\\u200D\\u2060\\uFEFF]", "replacement": ""},
            "vkm_supsub_fold": {"type": "mapping", "mappings": list(SUPSUB_FOLD)},
            "vkm_yo_fold": {"type": "mapping", "mappings": ["ё => е", "Ё => Е"]},
        },
        "filter": {
            "vkm_ru_stop": {"type": "stop", "stopwords": "_russian_"},
            "vkm_en_stop": {"type": "stop", "stopwords": "_english_"},
            "vkm_protected_terms": {"type": "keyword_marker", "keywords": list(PROTECTED_LEMMAS)},
            "vkm_protected_forms": {"type": "stemmer_override",
                                    "rules": [f"{form} => {lemma}" for form, lemma in sorted(forms.items())
                                              if form != lemma]},
            "vkm_ru_stem": {"type": "stemmer", "language": "russian"},
            "vkm_en_possessive": {"type": "stemmer", "language": "possessive_english"},
            "vkm_en_stem": {"type": "stemmer", "language": "english"},
        },
        "tokenizer": {
            "vkm_latex_tokenizer": {"type": "pattern", "pattern": LATEX_TOKEN_PATTERN, "group": 1},
        },
        "normalizer": {
            "vkm_keyword_norm": {"type": "custom", "char_filter": ["vkm_yo_fold"], "filter": ["lowercase"]},
        },
        "analyzer": {
            "vkm_text": {"type": "custom", "char_filter": text_char_filters, "tokenizer": "standard",
                         "filter": ["vkm_en_possessive", "lowercase", "vkm_ru_stop", "vkm_en_stop",
                                    "vkm_protected_terms", "vkm_protected_forms", "vkm_ru_stem", "vkm_en_stem"]},
            "vkm_exact": {"type": "custom", "char_filter": text_char_filters, "tokenizer": "standard",
                          "filter": ["lowercase"]},
            "vkm_latex": {"type": "custom", "tokenizer": "vkm_latex_tokenizer"},
        },
    }


def analysis_sha256() -> str:
    return hashlib.sha256(json.dumps(analysis_settings(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# corpus-independent analyzer expectations (build check S4): analyzer, text → expected tokens
ANALYZER_EXPECTATIONS: tuple[tuple[str, str, list[str]], ...] = (
    ("vkm_text", "оседание оседания оседаний", ["оседан", "оседан", "оседан"]),
    ("vkm_text", "ползучесть ползучести", ["ползучест", "ползучест"]),
    ("vkm_text", "расчётная расчетная", ["расчетн", "расчетн"]),
    ("vkm_text", "сильвинит сильвинита в сильвините сильвинитами", ["сильвинит"] * 4),
    ("vkm_text", "сильвин сильвина сильвине", ["сильвин"] * 3),
    ("vkm_text", "карналлит карналлите галит галите", ["карналлит", "карналлит", "галит", "галит"]),
    ("vkm_text", "subsidence creeping backfilling", ["subsid", "creep", "backfil"]),
    ("vkm_text", "σ₂ м³ soft­hyphen", ["σ2", "м3", "softhyphen"]),
    ("vkm_exact", "Ёмкость Сильвините", ["емкость", "сильвините"]),
    ("vkm_latex", r"\dot{\varepsilon} = A \sigma^{n}, E_{1.5}",
     ["\\dot", "\\varepsilon", "A", "\\sigma", "n", "E", "1.5"]),
)
