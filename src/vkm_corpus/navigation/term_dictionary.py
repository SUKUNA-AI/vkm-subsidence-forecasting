"""Bilingual term dictionary of the navigation layer (NAV §4, ``term_translations``) — no LLM.

Pairs of equivalent terms across languages (RU ↔ EN, and DE where the corpus has it) and inside one language
(synonyms, abbreviations). Every automatic pair carries the corpus evidence that proposed it (method, source, page,
block ids, a score); nothing is invented. Methods:

* ``PAREN_GLOSS`` — a term with its translation printed in parentheses («эффект ракурса (англ. foreshortening)»,
  «subsidence (оседание)»), re-read from the canonical blocks: the gloss must be a whole noun phrase of the other
  script, the term the noun phrase that ends right before the bracket (the one closest to the gloss by embedding
  similarity), the Latin side must look English (its words occur in English sources of the corpus) or German;
* ``SAME_AS_ABBREVIATION`` — N3's SAME_AS abbreviations («водозащитная толща (ВЗТ)», «ground penetrating radar
  (GPR)»): relation ABBREVIATION, full form first;
* ``KEYWORD_LISTS`` — the Russian and the English keyword lists of one article: lists paired inside a source,
  items aligned by embedding similarity with a positional prior (identical items are anchors, not pairs);
* ``ABSTRACT_PAIRS`` / ``BILINGUAL_CAPTIONS`` — the terms of a Russian abstract (figure caption) and of its English
  version: mutual best matches by embedding similarity inside the pair;
* ``SYMBOL_DEFINITIONS`` — one formula symbol defined in Russian and in English sources («ν — коэффициент Пуассона»,
  «ν is Poisson's ratio», N2 ``formula_symbols``), confirmed by embedding similarity;
* ``XLING_NEIGHBOURS`` — mutual cross-lingual nearest neighbours of term embeddings (vectors centred by language,
  CSLS against hubs) among terms discussed in several units;
* ``ORTHOGRAPHIC`` — spelling variants of one term («inter-chamber pillar» / «interchamber pillar», «levelling» /
  «leveling»);
* ``SYNONYM_PATTERN`` — «X (или Y)», «X, иначе Y», «X (or Y)», «X, also known as Y» with embedding confirmation
  (not «X, или Y»: a list of alternatives is not a synonym);
* ``SUBSTITUTION`` — terms that differ in one word where the same word swap recurs across several terms with high
  embedding similarity («камерная система разработки / отработки»), unless the two words are a contrast pair or are
  coordinated in the corpus («горизонтальные и вертикальные» are siblings, not synonyms): SYNONYM;
* ``PIVOT`` — two terms of one language that are both translations of one term of the other language (bilingual
  pivoting), similar to each other and neither contained in the other: SYNONYM;
* ``CURATED_SEED`` — the project's seed list (:mod:`term_dictionary_seeds`), checked by hand by the agent:
  ``REVIEWED_BY_AGENT`` (not by the user); a seed that the corpus methods also found keeps their evidence.

Term vectors are an input (``term_vectors``: the Parquet file of ``vkm-corpus nav term-vectors``: ``lang``, ``key``,
``text``, ``vector``; jina-v5-nano). Without them the embedding methods are skipped and the others fall back to rules
(positional alignment of keyword lists of equal length, the gloss closest in length). Keys are the lemma keys of the
concept graph (``concepts.phrase_keys``, the same morphology), so ``term_id`` is N3's id when the term is in ``terms``
(``in_terms_*``). The dataset is DERIVED navigation (``AUTO_EXTRACTED_UNREVIEWED``): a pair says «printed as each
other's translation / found equivalent by these methods», never a reviewed fact. All thresholds are MODEL_CHOICE,
listed in ``DEFAULTS`` and written into the build statistics.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

import numpy as np
import pyarrow as pa

from vkm_corpus.navigation import ids as nav_ids
from vkm_corpus.navigation.concepts import Morphology, _abbr_matches, _tokenize, analyze_text, phrase_keys

log = logging.getLogger(__name__)

RULE_VERSION = nav_ids.RULE_VERSIONS["translations"]
RELATIONS = ("TRANSLATION", "SYNONYM", "ABBREVIATION")
STATUS_AUTO = "AUTO_EXTRACTED_UNREVIEWED"
STATUS_SEED = "REVIEWED_BY_AGENT"
METHODS = ("CURATED_SEED", "PAREN_GLOSS", "SAME_AS_ABBREVIATION", "KEYWORD_LISTS", "ABSTRACT_PAIRS",
           "BILINGUAL_CAPTIONS", "SYMBOL_DEFINITIONS", "XLING_NEIGHBOURS", "ORTHOGRAPHIC", "SYNONYM_PATTERN",
           "SUBSTITUTION", "PIVOT")
LANG_ORDER = {"ru": 0, "en": 1, "de": 2}
METADATA_KEY = b"vkm_nav_translations"

DEFAULTS: dict[str, Any] = {
    "term_vectors": None,          # Parquet of `nav term-vectors` (lang, key, text, vector) or an Arrow table
    "paren_min_cos": 0.50,         # gloss ↔ term
    "kw_min_cos": 0.55,            # keyword items (any position)
    "kw_pos_min_cos": 0.40,        # keyword items at the same position of two lists of equal length
    "kw_list_min_quality": 0.34,   # share of aligned items for two lists to be each other's version
    "kw_max_page_gap": 40,
    "abs_min_cos": 0.72,           # abstract / caption terms (mutual best inside the pair)
    "abs_max_page_gap": 40,
    "sym_min_cos": 0.72,           # heads of the definitions of one symbol
    "nn_min_df": 3,                # XLING_NEIGHBOURS: terms seen in >= 3 units ...
    "nn_min_sources": 2,           # ... of >= 2 sources
    "nn_min_cos": 0.84,            # raw cosine of a mutual CSLS neighbour pair
    "nn_min_ccos": 0.55,           # cosine of the language-centred vectors
    "nn_topk": 10,                 # CSLS neighbourhood
    "pattern_min_cos": 0.80,
    "sub_min_df": 2,               # SUBSTITUTION: terms of >= 2 units ...
    "sub_pair_min_cos": 0.88,      # ... two terms differing in one word, this similar ...
    "sub_min_frames": 4,           # ... the same word swap in >= 4 term pairs ...
    "sub_mean_min_cos": 0.92,      # ... with this mean similarity ...
    "sub_max_coordinated": 1,      # ... and the two words coordinated («X и Y») at most this often
    "sub_word_min_cos": 0.85,      # the two words themselves as a pair
    "pivot_min_score": 0.9,        # PIVOT: both translation pairs at least this good ...
    "pivot_min_cos": 0.8,          # ... and the two synonyms this similar
    "max_examples": 5,
    "min_score": 0.0,              # pairs below this final score are dropped
}

_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-zÀ-ɏ]")
_DE_CHARS = frozenset("äöüßÄÖÜ")
_ROMANCE_END = re.compile(r"(zione|zioni|ità|ción|ciones|ização|ções|mente|eaux)$")
_ROMANCE_WORDS = frozenset("della delle dei degli dello il gli per con del los las el que des du et une sur dans pour "
                           "au aux una uno".split())
_GLOSS_MARKERS = frozenset("англ eng engl english англий нем германск лат от рус russ russian".split())
_KW_RE = {"ru": re.compile(r"(?i)ключевые\s+слова\s*[:.—–-]*\s*"),
          "en": re.compile(r"(?i)\bkey\s*-?\s*words?\s*[:.—–-]*\s*")}
_KW_STOP = re.compile(r"(?i)\b(введение|introduction|сведения об авторах|information about|для цитирования|"
                      r"for citation|"
                      r"doi\b|удк\b|udc\b|©|поступила|received|article history|благодарност|acknowledg)")
_CAP_EN = re.compile(r"(?i)\b(fig\.?|figure|table)\s*\d")
_CAP_RU = re.compile(r"(?i)^\s*(рис\.?|рисунок|таблица|табл\.)\s*\d")
_HYPHEN_BREAK = re.compile(r"(\w)-\s+(\w+)")


# ------------------------------------------------------------------------------------------------------ phrases
class Phrase(NamedTuple):
    key: str        # lemma key (concepts.phrase_keys; norm_text form)
    lang: str       # ru | en | de
    text: str       # display: the N3 lemma when the key is a term, else the printed phrase (lower case)


class Occ(NamedTuple):
    """One piece of evidence for a pair (``a`` and ``b`` in any order; the aggregation orients them)."""
    method: str
    relation: str
    a: Phrase
    b: Phrase
    source_id: str | None
    page_id: str | None
    block_id: str | None
    other_page_id: str | None
    other_block_id: str | None
    detail: str | None
    score: float


def script_lang(text: str, source_lang: str | None = None) -> str:
    """ru for Cyrillic text, de for Latin text with German letters or of a German source, else en."""
    nc = len(_CYR.findall(text))
    nl = len(_LAT.findall(text))
    if nc >= nl and nc:
        return "ru"
    if any(ch in _DE_CHARS for ch in text) or source_lang == "de":
        return "de"
    return "en"


def _norm_ws(text: str) -> str:
    return " ".join(str(text or "").split())


def _fix_hyphen_breaks(text: str) -> str:
    """«РАДАР- НАЯ» → «РАДАРНАЯ» (a word broken at a line end); «Coulomb- Mohr» → «Coulomb-Mohr»."""
    def join(m: re.Match) -> str:
        right = m.group(2)
        if right[:1].isupper() and not right.isupper():
            return f"{m.group(1)}-{right}"
        if len(right) <= 4 or right.islower():
            return f"{m.group(1)}{right}"
        return f"{m.group(1)}-{right}"
    return _HYPHEN_BREAK.sub(join, text)


def page_of_block(block_id: str | None) -> str | None:
    if not block_id:
        return None
    m = re.match(r"^(VKM-SRC-\d{3,}:[a-z]\d{4})", block_id)
    return m.group(1) if m else None


def _compress(key: str, lang: str) -> str:
    """Spelling-insensitive form of a key for ORTHOGRAPHIC variants."""
    k = key.replace(" ", "").replace("-", "")
    if lang == "en":
        k = re.sub(r"ll(ing|ed|er)", r"l\1", k)
        k = re.sub(r"our\b", "or", k)
        k = re.sub(r"tre\b", "ter", k)
        k = re.sub(r"is(e|ing|ed|ation)", r"iz\1", k)
        k = re.sub(r"ys(e|ing|ed)", r"yz\1", k)
    return k


_ARTICLES = re.compile(r"(?i)^(the|a|an)\s+")


# ------------------------------------------------------------------------------------------------------ context
@dataclass
class Context:
    morph: Morphology
    terms: dict[tuple[str, str], dict[str, Any]]                 # (lang, key) → terms row
    source_lang: dict[str, str]
    vocab: dict[str, frozenset[str]]                              # en/de: words of terms of such sources
    vectors: dict[tuple[str, str], np.ndarray] = field(default_factory=dict)
    phrases_seen: dict[tuple[str, str], str] = field(default_factory=dict)   # (lang, key) → text to encode
    surfaces: dict[tuple[str, str], str] = field(default_factory=dict)       # (lang, printed form) → N3 key
    _word_cache: dict[str, bool] = field(default_factory=dict)

    def phrase(self, text: str, lang: str | None = None, *, source_lang: str | None = None) -> Phrase | None:
        t = _norm_ws(_fix_hyphen_breaks(text)).strip(" .,;:—–-·•*\"'«»()[]")
        t = _ARTICLES.sub("", t)
        if not t or not re.search(r"[^\W\d_]{2,}", t):
            return None
        lang = lang or script_lang(t, source_lang)
        surface = " ".join(t.lower().replace("ё", "е").split())
        key = self.surfaces.get((lang, surface))           # a printed form of an N3 term: its key (in context the
        if key is None:                                    # analyser may parse a phrase better than in isolation)
            keys = phrase_keys(t, self.morph)
            if not keys:
                return None
            key = keys[0]
        row = self.terms.get((lang, key))
        text_out = row["lemma"] if row is not None else t.lower()
        self.phrases_seen.setdefault((lang, key), text_out)
        return Phrase(key, lang, text_out)

    def term_phrase(self, row: Mapping[str, Any]) -> Phrase:
        p = Phrase(row["lemma_key"], row["language"], row["lemma"])
        self.phrases_seen.setdefault((p.lang, p.key), p.text)
        return p

    def vec(self, p: Phrase) -> np.ndarray | None:
        return self.vectors.get((p.lang, p.key))

    def cos(self, a: Phrase, b: Phrase) -> float | None:
        va, vb = self.vec(a), self.vec(b)
        if va is None or vb is None:
            return None
        return float(np.dot(va, vb))

    def is_term(self, p: Phrase) -> bool:
        return (p.lang, p.key) in self.terms

    def plain_word(self, p: Phrase) -> bool:
        """False for a one-word Russian phrase that pymorphy3 does not know (an OCR fragment: «увелич») or whose
        best parse is a person's name (Surn/Name/Patr: transliterated names are not term translations)."""
        if p.lang != "ru" or " " in p.key or self.morph.name != "pymorphy3":
            return True
        hit = self._word_cache.get(p.key)
        if hit is None:
            m = self.morph._morph                                   # noqa: SLF001 — the shared analyser
            parses = m.parse(p.key)
            tag = parses[0].tag if parses else None
            hit = bool(parses) and m.word_is_known(p.key) and not (
                tag is not None and any(g in tag for g in ("Surn", "Name", "Patr")))
            self._word_cache[p.key] = hit
        return hit

    def looks_language(self, p: Phrase) -> bool:
        """A Latin phrase is kept as English (German) when at least half of its words occur in terms of English
        (German) sources of the corpus and it shows no Romance-language markers; Russian is always kept."""
        if p.lang == "ru":
            return True
        words = [w for w in re.findall(r"[^\W\d_]+", p.text.lower()) if len(w) > 1]
        if not words:
            return False
        if any(w in _ROMANCE_WORDS or _ROMANCE_END.search(w) for w in words):
            return False
        vocab = self.vocab.get(p.lang) or frozenset()
        if not vocab:
            return True
        return sum(1 for w in words if w in vocab) * 2 >= len(words)


def _table_exists(con: Any, schema: str, name: str) -> bool:
    try:
        return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = ? "
                                "AND table_name = ?", [schema, name]).fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


def _canon(con: Any, name: str) -> str:
    return f"canonical.{name}" if _table_exists(con, "canonical", name) else name


def source_languages(con: Any) -> dict[str, str]:
    """Source → the first language of its primary work (canon ``works.languages``); empty without works."""
    try:
        rows = con.execute(f"SELECT l.source_id, w.languages FROM {_canon(con, 'source_work_links')} l "
                           f"JOIN {_canon(con, 'works')} w ON w.work_id = l.work_id WHERE l.is_primary").fetchall()
    except Exception:  # noqa: BLE001 - a canon without works (tests)
        return {}
    return {sid: langs[0] for sid, langs in rows if langs}


def _as_rows(table: Any, columns: Sequence[str] | None = None) -> list[dict[str, Any]]:
    if table is None:
        return []
    if isinstance(table, pa.Table):
        t = table.select([c for c in columns if c in table.schema.names]) if columns else table
        return t.to_pylist()
    if hasattr(table, "to_arrow_table"):
        return _as_rows(table.to_arrow_table(), columns)
    return list(table)


def load_term_vectors(source: Any) -> dict[tuple[str, str], np.ndarray]:
    """(lang, key) → L2-normalised float32 vector from the Parquet file / Arrow table of ``nav term-vectors``."""
    if source is None:
        return {}
    if isinstance(source, (str, bytes)) or hasattr(source, "__fspath__"):
        import pyarrow.parquet as pq

        source = pq.read_table(str(source))
    t = source if isinstance(source, pa.Table) else pa.table(source)
    langs = t.column("lang").to_pylist()
    keys = t.column("key").to_pylist()
    col = t.column("vector").combine_chunks()
    flat = np.asarray(col.flatten().to_numpy(zero_copy_only=False), dtype=np.float32)
    dim = len(flat) // max(1, t.num_rows)
    mat = flat.reshape(t.num_rows, dim)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.where(norms > 0, norms, 1.0)
    return {(lg, k): mat[i] for i, (lg, k) in enumerate(zip(langs, keys))}


def make_context(con: Any, *, terms: Any, term_mentions: Any = None, morph: Morphology | None = None,
                 vectors: Any = None) -> Context:
    morph = morph or Morphology()
    rows = _as_rows(terms, ["term_id", "lemma", "lemma_key", "language", "kind", "n_words", "df_units", "df_sources",
                            "morphology", "surface_forms"])
    built_with = {r.get("morphology") for r in rows if r.get("morphology")}
    if built_with and morph.name not in built_with:
        raise RuntimeError(f"terms were lemmatised with {sorted(built_with)}, this run has {morph.name}: the keys "
                           "would not match (install the extra `navigation`)")
    by_key = {(r["language"], r["lemma_key"]): r for r in rows}
    src_lang = source_languages(con)
    vocab: dict[str, set[str]] = {"en": set(), "de": set()}
    if term_mentions is not None and src_lang:
        term_lang = {r["term_id"]: r["language"] for r in rows}
        lemma_of = {r["term_id"]: r["lemma"] for r in rows}
        ment = _as_rows(term_mentions, ["term_id", "source_id"])
        seen: set[tuple[str, str]] = set()
        for m in ment:
            tid, sid = m["term_id"], m["source_id"]
            lang = src_lang.get(sid)
            if lang in vocab and term_lang.get(tid) == lang and (tid, lang) not in seen:
                seen.add((tid, lang))
                vocab[lang].update(w for w in re.findall(r"[^\W\d_]+", (lemma_of.get(tid) or "").lower()) if len(w) > 1)
    surfaces: dict[tuple[str, str], str] = {}
    for r in sorted(rows, key=lambda r: (-(r.get("df_units") or 0), r["lemma_key"])):   # the frequent term wins
        for s in [r["lemma"], *(r.get("surface_forms") or [])]:
            surfaces.setdefault((r["language"], " ".join(str(s).lower().replace("ё", "е").split())), r["lemma_key"])
    ctx = Context(morph=morph, terms=by_key, source_lang=src_lang,
                  vocab={k: frozenset(v) for k, v in vocab.items()}, surfaces=surfaces)
    if vectors is not None:
        ctx.vectors = vectors if isinstance(vectors, dict) else load_term_vectors(vectors)
    return ctx


# ------------------------------------------------------------------------------------------------------ blocks
_TEXT_TYPES = ("TEXT", "ABSTRACT", "CAPTION", "FOOTNOTE", "LIST_ITEM", "HEADING", "TITLE", "REFERENCE_LIST")


def _blocks(con: Any, where: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    blocks, pages = _canon(con, "blocks"), _canon(con, "pages")
    types = ", ".join(f"'{t}'" for t in _TEXT_TYPES)
    sql = f"""SELECT b.object_id AS block_id, b.page_id, b.source_id, coalesce(p.page_index, 0)::INTEGER AS page_index,
                     coalesce(b.reading_order, 0)::INTEGER AS reading_order, b.block_type, b.normalized_text AS text
              FROM {blocks} b LEFT JOIN {pages} p ON p.page_id = b.page_id
              WHERE b.is_primary_layer AND b.block_type IN ({types}) AND b.normalized_text IS NOT NULL
                AND ({where})
              ORDER BY b.source_id, page_index, b.page_id, reading_order, b.object_id"""
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ------------------------------------------------------------------------------------------------------ PAREN_GLOSS
def _gloss_occurrences(ctx: Context, block: Mapping[str, Any]) -> list[tuple[list[Phrase], Phrase, Phrase | None]]:
    """(left options, gloss, abbreviation printed after the gloss) of one block: «X (Y)», «X (англ. Y)», «X (Y, Z)»
    where Y is a whole noun phrase in the other script and X ends right before the bracket."""
    text = block["text"] or ""
    res = analyze_text(text, ctx.morph, patterns=False)
    toks, scripts = _tokenize(text)
    n = len(toks)
    by_end: dict[int, list] = defaultdict(list)
    by_span: dict[tuple[int, int], list] = defaultdict(list)
    for c in res.cands:
        by_end[c.t1].append(c)
        by_span[(c.t0, c.t1)].append(c)
    src_lang = ctx.source_lang.get(block["source_id"])
    out = []
    for p, t in enumerate(toks):
        if t != "(" or p == 0:
            continue
        q = next((k for k in range(p + 1, min(n, p + 16)) if toks[k] in "()"), None)
        if q is None or toks[q] != ")":
            continue
        a = p + 1
        marker_lang = None
        while a < q and (toks[a].lower().rstrip(".") in _GLOSS_MARKERS or toks[a] in (".", ":")):
            low = toks[a].lower()
            if low.startswith("нем") or low.startswith("германск"):
                marker_lang = "de"
            elif low.startswith("англ") or low.startswith("eng"):
                marker_lang = "en"
            a += 1
        if a >= q:
            continue
        segs: list[tuple[int, int]] = []
        s0 = a
        for k in range(a, q + 1):
            if k == q or toks[k] in (",", ";"):
                if k > s0:
                    segs.append((s0, k - 1))
                s0 = k + 1
        if not segs:
            continue
        g0, g1 = segs[0]
        if any(scripts[k] not in ("cyr", "lat") for k in range(g0, g1 + 1)) or g1 - g0 > 5:
            continue
        g_script = scripts[g0]
        if any(scripts[k] != g_script for k in range(g0, g1 + 1)):
            continue
        inner = [c for c in by_span.get((g0, g1), []) if c.kind in ("NP", "ABBR")]
        if not inner:
            continue
        ic = max(inner, key=lambda c: (c.kind == "NP", c.n_words))
        if ic.kind == "ABBR":
            continue                      # abbreviations in brackets: N3's SAME_AS (SAME_AS_ABBREVIATION)
        left = [c for c in by_end.get(p - 1, []) if c.kind == "NP" and scripts[c.t0] != g_script]
        if not left:
            continue
        g_lang = "ru" if g_script == "cyr" else (marker_lang or script_lang(ic.surface, src_lang))
        gloss = ctx.phrase(" ".join(toks[g0:g1 + 1]), g_lang, source_lang=src_lang)
        if gloss is None:
            continue
        opts = []
        for c in sorted(left, key=lambda c: (-c.n_words, c.t0)):
            lang = "ru" if scripts[c.t0] == "cyr" else script_lang(c.surface, src_lang)
            ph = ctx.phrase(" ".join(toks[c.t0:c.t1 + 1]), lang, source_lang=src_lang)
            if ph is not None and ph not in opts:
                opts.append(ph)
        abbr = None
        for s, e in segs[1:]:
            if s == e and scripts[s] == g_script:
                cand = [c for c in by_span.get((s, e), []) if c.kind == "ABBR"]
                if cand and _abbr_matches(cand[0].surface, " ".join(toks[g0:g1 + 1])):
                    abbr = ctx.phrase(cand[0].surface, g_lang, source_lang=src_lang)
                    break
        if opts:
            out.append((opts, gloss, abbr))
    return out


def paren_candidates(con: Any, ctx: Context) -> list[tuple[dict[str, Any], list[Phrase], Phrase, Phrase | None]]:
    rows = _blocks(con, r"regexp_matches(b.normalized_text, '\([^()]{2,160}\)') AND "
                        r"regexp_matches(b.normalized_text, '[A-Za-z]{3}') AND "
                        r"regexp_matches(b.normalized_text, '[А-Яа-яЁё]{3}') AND b.block_type <> 'REFERENCE_LIST'")
    out = []
    for b in rows:
        for opts, gloss, abbr in _gloss_occurrences(ctx, b):
            out.append((b, opts, gloss, abbr))
    return out


def method_paren(con: Any, ctx: Context, p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    occs: list[Occ] = []
    rejected = Counter()
    for b, opts, gloss, abbr in paren_candidates(con, ctx):
        if not ctx.looks_language(gloss):
            rejected["gloss_language"] += 1
            continue
        best, best_cos, any_vec = None, None, False
        n_g = len(gloss.key.split())
        for o in opts:
            if not ctx.looks_language(o) or not ctx.plain_word(o):
                continue
            c = ctx.cos(o, gloss)
            if c is None:
                continue
            any_vec = True
            n_o = len(o.key.split())
            need = p["paren_min_cos"] + 0.15 * max(0, abs(n_o - n_g) - 1)
            if n_g == 1 and n_o >= 2:
                need = max(need, 0.7)                     # a one-word gloss of a phrase: a symbol, a name, a part
            if c >= need and (best_cos is None or c > best_cos):
                best, best_cos = o, c
        if best is None and ctx.vectors:
            rejected["low_cosine" if any_vec else "no_vector"] += 1
            continue
        if best is None and not ctx.vectors:
            best = min(opts, key=lambda o: (abs(len(o.key.split()) - n_g), -len(o.key.split())))
        if best is None:
            rejected["no_vector"] += 1
            continue
        if best.key == gloss.key and best.lang == gloss.lang:
            continue
        pid = b["page_id"]
        occs.append(Occ("PAREN_GLOSS", "TRANSLATION", best, gloss, b["source_id"], pid, b["block_id"], None, None,
                        None, best_cos if best_cos is not None else 0.5))
        if abbr is not None and abbr.key != gloss.key:
            occs.append(Occ("PAREN_GLOSS", "ABBREVIATION", gloss, abbr, b["source_id"], pid, b["block_id"], None,
                            None, None, 0.8))
    stats["paren_rejected"] = dict(rejected)
    return occs


# ------------------------------------------------------------------------------------------------------ SAME_AS
def method_same_as(ctx: Context, terms: Any, term_edges: Any, p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    """N3 SAME_AS edges: abbreviations (ABBREVIATION, full form first) and bracketed translations (re-checked like
    PAREN_GLOSS: English-looking gloss, embedding similarity)."""
    if term_edges is None:
        return []
    by_id = {r["term_id"]: r for r in _as_rows(terms, ["term_id", "lemma", "lemma_key", "language", "kind"])}
    edges = term_edges
    if isinstance(edges, pa.Table):
        import pyarrow.compute as pc

        edges = edges.filter(pc.equal(edges.column("kind"), "SAME_AS"))
    occs: list[Occ] = []
    rejected = Counter()
    for e in _as_rows(edges, ["kind", "src_term_id", "dst_term_id", "dst_ref", "weight", "n_sources", "examples",
                              "example_block_ids", "rule"]):
        if e.get("kind") != "SAME_AS":
            continue
        src = by_id.get(e["src_term_id"])
        if src is None:
            continue
        a = ctx.term_phrase(src)
        a_kind = src["kind"]
        dst = by_id.get(e["dst_term_id"]) if e.get("dst_term_id") else None
        if dst is not None:
            b, b_kind = ctx.term_phrase(dst), dst["kind"]
        else:
            lang, _, key = (e.get("dst_ref") or "").partition(":")
            if not key or lang not in LANG_ORDER:
                continue
            b = Phrase(key, lang, key)
            ctx.phrases_seen.setdefault((lang, key), key)
            b_kind = "ABBR" if re.fullmatch(r"[^\W\d_]{2,8}(-\d+)?", key) and len(key) <= 8 else "NP"
        blocks = list(e.get("example_block_ids") or [])
        pages = list(e.get("examples") or [])
        block = blocks[0] if blocks else None
        page = pages[0] if pages else page_of_block(block)
        source = page.split(":")[0] if page else None
        n_src = int(e.get("n_sources") or 1)
        if e.get("rule") == "paren_abbreviation":
            if a_kind == "ABBR" and b_kind != "ABBR":
                full, abbr = b, a
            elif b_kind == "ABBR" and a_kind != "ABBR":
                full, abbr = a, b
            else:
                rejected["abbr_shape"] += 1
                continue
            if full.lang == abbr.lang and not _abbr_matches(abbr.key.replace(" ", ""), full.text):
                if n_src < 2:
                    rejected["abbr_letters"] += 1
                    continue
            if full.lang != abbr.lang and n_src < 2:
                rejected["abbr_cross_single_source"] += 1
                continue
            occs.append(Occ("SAME_AS_ABBREVIATION", "ABBREVIATION", full, abbr, source, page, block, None, None,
                            f"n={int(e.get('weight') or 1)}", min(1.0, 0.5 + 0.1 * n_src)))
        elif e.get("rule") == "paren_translation":
            if a.lang == b.lang or a_kind == "ABBR" or b_kind == "ABBR":
                continue
            if not (ctx.looks_language(a) and ctx.looks_language(b)):
                rejected["translation_language"] += 1
                continue
            c = ctx.cos(a, b)
            if c is None or c < p["paren_min_cos"]:
                rejected["translation_cosine"] += 1
                continue
            occs.append(Occ("PAREN_GLOSS", "TRANSLATION", a, b, source, page, block, None, None, "n3_same_as", c))
    stats["same_as_rejected"] = dict(rejected)
    return occs


# ------------------------------------------------------------------------------------------------------ keyword lists
@dataclass
class KwList:
    source_id: str
    page_id: str
    page_index: int
    block_id: str
    reading_order: int
    lang: str
    items: list[Phrase]
    raw_items: list[str]


def _split_keywords(text: str) -> list[str]:
    stop = _KW_STOP.search(text)
    if stop:
        text = text[: stop.start()]
    m = re.search(r"\.\s+(?=[A-ZА-ЯЁ][a-zа-яё]+\s+[a-zа-яё]+\s+[a-zа-яё]+)", text)   # a sentence after the list
    if m:
        text = text[: m.start()]
    text = text.strip().rstrip(".")
    items = []
    for it in re.split(r"[;,]|\s[·•]\s", text):
        it = _norm_ws(it).strip(" .:—–-·•")
        if it and len(it) <= 80 and len(it.split()) <= 6 and re.search(r"[^\W\d_]{2}", it):
            items.append(it)
    return items


def keyword_lists(con: Any, ctx: Context) -> list[KwList]:
    rows = _blocks(con, r"regexp_matches(b.normalized_text, '(?i)(ключевые\s+слова|key\s*-?\s*words?)')")
    out: list[KwList] = []
    for b in rows:
        text = _fix_hyphen_breaks(b["text"] or "")
        for lang, rx in _KW_RE.items():
            m = rx.search(text)
            if not m:
                continue
            rest = text[m.end():]
            other = _KW_RE["en" if lang == "ru" else "ru"].search(rest)
            if other:
                rest = rest[: other.start()]
            raw = _split_keywords(rest)
            items, kept = [], []
            for it in raw:
                it_lang = script_lang(it, ctx.source_lang.get(b["source_id"]))
                ph = ctx.phrase(it, it_lang)
                if ph is not None:
                    items.append(ph)
                    kept.append(it)
            if len(items) >= 2:
                out.append(KwList(b["source_id"], b["page_id"], b["page_index"], b["block_id"], b["reading_order"],
                                  lang, items, kept))
    return out


def _align_items(ctx: Context, ru: Sequence[Phrase], en: Sequence[Phrase], p: Mapping[str, Any]
                 ) -> tuple[list[tuple[int, int, float]], int]:
    """Greedy one-to-one alignment of two keyword lists: identical items are anchors; the rest by cosine with a
    positional prior (same relative position +0.08); a pair needs ``kw_min_cos`` (``kw_pos_min_cos`` at the same
    position of lists of equal length). Without vectors: position only, for lists of equal length."""
    anchors = {(i, j) for i, a in enumerate(ru) for j, b in enumerate(en) if a.key == b.key and a.lang != "ru"}
    ai = {i for i, _ in anchors}
    aj = {j for _, j in anchors}
    ri = [i for i in range(len(ru)) if i not in ai and ru[i].lang != en[0].lang]
    ej = [j for j in range(len(en)) if j not in aj and en[j].lang != ru[0].lang]
    pairs: list[tuple[int, int, float]] = []
    if not ri or not ej:
        return pairs, len(anchors)
    same_len = len(ri) == len(ej)
    if not ctx.vectors:
        if same_len:
            pairs = [(i, j, 0.5) for i, j in zip(ri, ej)]
        return pairs, len(anchors)
    cand = []
    for x, i in enumerate(ri):
        for y, j in enumerate(ej):
            c = ctx.cos(ru[i], en[j])
            if c is None:
                continue
            pos = 1.0 - abs(x / max(1, len(ri) - 1) - y / max(1, len(ej) - 1)) if len(ri) > 1 and len(ej) > 1 else 1.0
            same_pos = same_len and x == y
            ok = c >= p["kw_min_cos"] or (same_pos and c >= p["kw_pos_min_cos"])
            if ok:
                cand.append((c + 0.08 * pos + (0.05 if same_pos else 0.0), c, i, j))
    used_i, used_j = set(), set()
    for _s, c, i, j in sorted(cand, key=lambda t: (-t[0], t[2], t[3])):
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        pairs.append((i, j, c))
    return sorted(pairs), len(anchors)


def method_keywords(con: Any, ctx: Context, p: Mapping[str, Any], stats: dict[str, Any]
                    ) -> tuple[list[Occ], list[tuple[KwList, KwList]]]:
    lists = keyword_lists(con, ctx)
    by_src: dict[str, list[KwList]] = defaultdict(list)
    for kl in lists:
        by_src[kl.source_id].append(kl)
    occs: list[Occ] = []
    paired: list[tuple[KwList, KwList]] = []
    for sid, kls in sorted(by_src.items()):
        ru = [k for k in kls if k.lang == "ru"]
        en = [k for k in kls if k.lang != "ru"]
        options = []
        for x in ru:
            for y in en:
                if abs(x.page_index - y.page_index) > p["kw_max_page_gap"]:
                    continue
                al, n_anchor = _align_items(ctx, x.items, y.items, p)
                q = (len(al) + n_anchor) / max(1, min(len(x.items), len(y.items)))
                options.append((q, -abs(x.page_index - y.page_index), x, y, al))
        used = set()
        for q, _gap, x, y, al in sorted(options, key=lambda o: (-o[0], -o[1], o[2].block_id, o[3].block_id)):
            if id(x) in used or id(y) in used:
                continue
            single = len(ru) == 1 and len(en) == 1
            if q < p["kw_list_min_quality"] and not (single and al):
                continue
            used.add(id(x))
            used.add(id(y))
            paired.append((x, y))
            for i, j, c in al:
                occs.append(Occ("KEYWORD_LISTS", "TRANSLATION", x.items[i], y.items[j], sid, x.page_id, x.block_id,
                                y.page_id, y.block_id, f"items {i + 1}/{len(x.items)} ↔ {j + 1}/{len(y.items)}", c))
    stats["keyword_lists"] = {"lists": len(lists), "ru": sum(1 for k in lists if k.lang == "ru"),
                              "en": sum(1 for k in lists if k.lang != "ru"), "paired": len(paired)}
    return occs, paired


# -------------------------------------------------------------------------------------------------- abstracts, captions
def _block_terms(ctx: Context, text: str, lang_hint: str) -> list[Phrase]:
    """Noun phrases of a text whose key is an N3 term (1–4 words), in order, without repeats."""
    res = analyze_text(text, ctx.morph, lang_hint=lang_hint, patterns=False)
    out, seen = [], set()
    for c in res.cands:
        if c.kind != "NP":
            continue
        lang = c.lang if c.lang in LANG_ORDER else lang_hint
        row = ctx.terms.get((lang, c.key))
        if row is None or (lang, c.key) in seen:
            continue
        seen.add((lang, c.key))
        out.append(ctx.term_phrase(row))
    return out


def _mutual_best(ctx: Context, left: Sequence[Phrase], right: Sequence[Phrase], min_cos: float
                 ) -> list[tuple[Phrase, Phrase, float]]:
    if not ctx.vectors or not left or not right:
        return []
    lv = [(x, ctx.vec(x)) for x in left]
    rv = [(y, ctx.vec(y)) for y in right]
    lv = [(x, v) for x, v in lv if v is not None]
    rv = [(y, v) for y, v in rv if v is not None]
    if not lv or not rv:
        return []
    L = np.stack([v for _, v in lv])
    R = np.stack([v for _, v in rv])
    S = L @ R.T
    bi = S.argmax(axis=1)
    bj = S.argmax(axis=0)
    out = []
    for i, j in enumerate(bi):
        if bj[j] != i:
            continue
        x, y = lv[i][0], rv[j][0]
        d = abs(len(x.key.split()) - len(y.key.split()))
        need = min_cos + (0.0, 0.06, 0.13)[d] if d <= 2 else 9.0     # a partial match needs more similarity
        if S[i, j] >= need and ctx.plain_word(x) and ctx.plain_word(y):
            out.append((x, y, float(S[i, j])))
    return out


def _abstract_blocks(con: Any, ctx: Context, kw_lists: Sequence[KwList]) -> dict[str, dict[str, list[dict]]]:
    """Abstract blocks per source and language: blocks that start with an abstract marker, and the text block just
    before each keyword list (same page, same language, ≥ 150 characters)."""
    # RE2 (DuckDB): \b is an ASCII word boundary, useless after Cyrillic words
    rows = _blocks(con, r"regexp_matches(b.normalized_text, '(?i)^\s*(аннотация|резюме|реферат|abstract|summary)"
                        r"([\s:.—–-]|$)') AND length(b.normalized_text) >= 150")
    out: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    seen: set[str] = set()
    for b in rows:
        lang = script_lang(b["text"][:400], ctx.source_lang.get(b["source_id"]))
        out[b["source_id"]][lang].append(b)
        seen.add(b["block_id"])
    if kw_lists:
        pages = sorted({k.page_id for k in kw_lists})
        near = _blocks(con, "b.page_id IN (SELECT unnest(?::VARCHAR[])) AND length(b.normalized_text) >= 150 "
                            "AND b.block_type IN ('TEXT', 'ABSTRACT')", [pages])
        by_page: dict[str, list[dict]] = defaultdict(list)
        for b in near:
            by_page[b["page_id"]].append(b)
        for k in kw_lists:
            prev = [b for b in by_page.get(k.page_id, []) if b["reading_order"] < k.reading_order
                    and b["block_id"] != k.block_id]
            prev = [b for b in prev if (script_lang(b["text"][:400]) == "ru") == (k.lang == "ru")]
            if prev:
                b = max(prev, key=lambda b: b["reading_order"])
                lang = script_lang(b["text"][:400], ctx.source_lang.get(b["source_id"]))
                if b["block_id"] not in seen:
                    seen.add(b["block_id"])
                    b = dict(b)
                    b["_kw_block"] = k.block_id
                    out[b["source_id"]][lang].append(b)
    return out


def method_abstracts(con: Any, ctx: Context, kw_pairs: Sequence[tuple[KwList, KwList]], kw_lists: Sequence[KwList],
                     p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    blocks = _abstract_blocks(con, ctx, kw_lists)
    kw_pair_of = {}
    for x, y in kw_pairs:
        kw_pair_of[x.block_id] = y.block_id
        kw_pair_of[y.block_id] = x.block_id
    occs: list[Occ] = []
    n_pairs = 0
    for sid, by_lang in sorted(blocks.items()):
        ru = by_lang.get("ru", [])
        others = [b for lg in ("en", "de") for b in by_lang.get(lg, [])]
        if not ru or not others:
            continue
        pairs = []
        # 1) abstracts attached to paired keyword lists
        for a in ru:
            kb = a.get("_kw_block")
            if kb and kb in kw_pair_of:
                partner = next((b for b in others if b.get("_kw_block") == kw_pair_of[kb]), None)
                if partner is not None:
                    pairs.append((a, partner))
        # 2) the rest: nearest by page, one-to-one
        used = {id(a) for a, _ in pairs} | {id(b) for _, b in pairs}
        cand = sorted(((abs(a["page_index"] - b["page_index"]), a["block_id"], b["block_id"], a, b)
                       for a in ru for b in others if id(a) not in used and id(b) not in used
                       and abs(a["page_index"] - b["page_index"]) <= p["abs_max_page_gap"]),
                      key=lambda t: t[:3])
        for _gap, _ia, _ib, a, b in cand:
            if id(a) in used or id(b) in used:
                continue
            used.add(id(a))
            used.add(id(b))
            pairs.append((a, b))
        for a, b in pairs:
            n_pairs += 1
            la = _block_terms(ctx, a["text"], "ru")
            lb = _block_terms(ctx, b["text"], script_lang(b["text"][:400], ctx.source_lang.get(sid)))
            for x, y, c in _mutual_best(ctx, la, lb, p["abs_min_cos"]):
                if x.lang == y.lang or not ctx.looks_language(y):
                    continue
                occs.append(Occ("ABSTRACT_PAIRS", "TRANSLATION", x, y, sid, a["page_id"], a["block_id"], b["page_id"],
                                b["block_id"], None, c))
    stats["abstract_pairs"] = n_pairs
    return occs


def method_captions(con: Any, ctx: Context, p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    rows = _blocks(con, r"b.block_type = 'CAPTION' AND regexp_matches(b.normalized_text, "
                        r"'(?i)(рис|таблица|табл|fig|figure|table)\.?\s*\d')")
    pairs = []
    by_page: dict[str, list[dict]] = defaultdict(list)
    for b in rows:
        by_page[b["page_id"]].append(b)
        t = b["text"]
        m = _CAP_EN.search(t)
        if m and _CYR.search(t[: m.start()]) and _CAP_RU.search(t) and m.start() > 10:
            pairs.append((b, t[: m.start()], t[m.start():]))
    for pid, bl in by_page.items():                       # «Рис. 3 …» then «Fig. 3 …» as two blocks
        bl = sorted(bl, key=lambda b: b["reading_order"])
        for x, y in zip(bl, bl[1:]):
            mx, my = _CAP_RU.search(x["text"]), re.match(r"(?i)^\s*(fig\.?|figure|table)\s*(\d+)", y["text"])
            nx = re.match(r"(?i)^\s*(?:рис\.?|рисунок|таблица|табл\.)\s*(\d+)", x["text"])
            if mx and my and nx and nx.group(1) == my.group(2) and not _CAP_EN.search(x["text"]):
                pairs.append((x, x["text"], y["text"]))
    occs: list[Occ] = []
    for b, ru_text, en_text in pairs:
        sid = b["source_id"]
        la = _block_terms(ctx, ru_text, "ru")
        lb = _block_terms(ctx, en_text, "en")
        for x, y, c in _mutual_best(ctx, la, lb, p["abs_min_cos"]):
            if x.lang == y.lang or not ctx.looks_language(y):
                continue
            occs.append(Occ("BILINGUAL_CAPTIONS", "TRANSLATION", x, y, sid, b["page_id"], b["block_id"], None, None,
                            None, c))
    stats["caption_pairs"] = len(pairs)
    return occs


# ------------------------------------------------------------------------------------------------------ symbols
def _definition_head(ctx: Context, definition: str, lang: str) -> Phrase | None:
    res = analyze_text(definition, ctx.morph, lang_hint=lang, patterns=False)
    toks, scripts = _tokenize(definition)
    first = next((i for i, s in enumerate(scripts) if s in ("cyr", "lat")), None)
    if first is None:
        return None
    heads = [c for c in res.cands if c.kind == "NP" and c.t0 == first and (c.lang if c.lang in LANG_ORDER else lang,
                                                                           c.key) in ctx.terms]
    if not heads:
        return None
    c = max(heads, key=lambda c: c.n_words)
    return ctx.term_phrase(ctx.terms[(c.lang if c.lang in LANG_ORDER else lang, c.key)])


def method_symbols(ctx: Context, formula_symbols: Any, p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    if formula_symbols is None or not ctx.vectors:
        stats["symbol_pairs"] = 0
        return []
    rows = [r for r in _as_rows(formula_symbols, ["formula_id", "source_id", "symbol", "symbol_key", "definition",
                                                  "definition_block_id"]) if r.get("definition")]
    heads: dict[str, dict[str, dict[tuple[str, str], list[dict]]]] = defaultdict(lambda: defaultdict(dict))
    cache: dict[tuple[str, str], Phrase | None] = {}
    for r in rows:
        d = _norm_ws(r["definition"])[:160]
        lang = script_lang(d, ctx.source_lang.get(r["source_id"]))
        k = (lang, d)
        if k not in cache:
            cache[k] = _definition_head(ctx, d, lang)
        h = cache[k]
        if h is None:
            continue
        heads[r["symbol_key"] or r["symbol"]][lang].setdefault((h.lang, h.key), []).append({**r, "_head": h})
    occs: list[Occ] = []
    n = 0
    for sym, by_lang in sorted(heads.items()):
        ru = by_lang.get("ru") or {}
        for lg in ("en", "de"):
            other = by_lang.get(lg) or {}
            for (_l1, _k1), ra in sorted(ru.items()):
                for (_l2, _k2), rb in sorted(other.items()):
                    x, y = ra[0]["_head"], rb[0]["_head"]
                    c = ctx.cos(x, y)
                    d = abs(len(x.key.split()) - len(y.key.split()))
                    need = p["sym_min_cos"] if d == 0 else (0.86 if d == 1 else 9.0)
                    if c is None or c < need or not ctx.looks_language(y) or not ctx.plain_word(x):
                        continue
                    n += 1
                    ea, eb = ra[0], rb[0]
                    occs.append(Occ("SYMBOL_DEFINITIONS", "TRANSLATION", x, y, ea["source_id"],
                                    page_of_block(ea.get("definition_block_id")), ea.get("definition_block_id"),
                                    page_of_block(eb.get("definition_block_id")), eb.get("definition_block_id"),
                                    f"symbol {sym}", c))
    stats["symbol_pairs"] = n
    return occs


# ---------------------------------------------------------------------------------------------------- XLING neighbours
def method_xling(ctx: Context, terms: Any, p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    """Mutual nearest neighbours across languages under CSLS on language-centred vectors of frequent terms."""
    if not ctx.vectors:
        return []
    rows = [r for r in _as_rows(terms, ["term_id", "lemma", "lemma_key", "language", "kind", "df_units", "df_sources",
                                        "n_words"])
            if r["kind"] == "NP" and r["df_units"] >= p["nn_min_df"] and r["df_sources"] >= p["nn_min_sources"]]
    groups: dict[str, list[tuple[Phrase, np.ndarray]]] = defaultdict(list)
    for r in rows:
        ph = Phrase(r["lemma_key"], r["language"], r["lemma"])
        v = ctx.vec(ph)
        if v is not None and ctx.looks_language(ph) and ctx.plain_word(ph):
            groups[ph.lang].append((ph, v))
    k = int(p["nn_topk"])
    occs: list[Occ] = []
    counts = {}
    for la, lb in (("ru", "en"), ("ru", "de"), ("en", "de")):
        A, B = groups.get(la) or [], groups.get(lb) or []
        if len(A) < 2 or len(B) < 2:
            continue
        MA = np.stack([v for _, v in A]).astype(np.float32)
        MB = np.stack([v for _, v in B]).astype(np.float32)
        CA = MA - MA.mean(axis=0, keepdims=True)
        CB = MB - MB.mean(axis=0, keepdims=True)
        CA /= np.maximum(np.linalg.norm(CA, axis=1, keepdims=True), 1e-9)
        CB /= np.maximum(np.linalg.norm(CB, axis=1, keepdims=True), 1e-9)
        kk = min(k, len(A), len(B))
        top_a = np.zeros((len(A), kk), dtype=np.int64)
        val_a = np.zeros((len(A), kk), dtype=np.float32)
        # pass 1: per-row top-k of A→B and the running top-k similarities of every B column (for rS) — blockwise
        topk_b_vals = np.full((len(B), kk), -np.inf, dtype=np.float32)
        step = 4096
        for s in range(0, len(A), step):
            S = CA[s:s + step] @ CB.T
            idx = np.argpartition(-S, kk - 1, axis=1)[:, :kk]
            vals = np.take_along_axis(S, idx, axis=1)
            order = np.argsort(-vals, axis=1, kind="stable")
            top_a[s:s + step] = np.take_along_axis(idx, order, axis=1)
            val_a[s:s + step] = np.take_along_axis(vals, order, axis=1)
            kb = min(kk, S.shape[0])
            cb = np.argpartition(-S, kb - 1, axis=0)[:kb, :]
            cv = np.take_along_axis(S, cb, axis=0).T                       # (len(B), ≤ kk)
            topk_b_vals = -np.sort(-np.concatenate([topk_b_vals, cv], axis=1), axis=1)[:, :kk]
        rA = val_a.mean(axis=1)
        rB = topk_b_vals.mean(axis=1)
        # pass 2: CSLS argmax for A over its top-k candidates; for B over all A (blockwise over B)
        best_a = np.full(len(A), -1, dtype=np.int64)
        best_a_csls = np.full(len(A), -np.inf, dtype=np.float32)
        for i in range(len(A)):
            cand = top_a[i]
            cs = 2 * val_a[i] - rA[i] - rB[cand]
            j = int(np.argmax(cs))
            best_a[i], best_a_csls[i] = cand[j], cs[j]
        best_b = np.full(len(B), -1, dtype=np.int64)
        for s in range(0, len(B), step):
            S = CB[s:s + step] @ CA.T                                      # (b, len(A))
            cs = 2 * S - rB[s:s + step, None] - rA[None, :]
            best_b[s:s + step] = np.argmax(cs, axis=1)
        n = 0
        for i in range(len(A)):
            j = int(best_a[i])
            if j < 0 or int(best_b[j]) != i:
                continue
            x, y = A[i][0], B[j][0]
            raw = float(np.dot(A[i][1], B[j][1]))
            ccos = float(np.dot(CA[i], CB[j]))
            if raw < p["nn_min_cos"] or ccos < p["nn_min_ccos"]:
                continue
            nx, ny = len(x.key.split()), len(y.key.split())
            if abs(nx - ny) > 1 and not (min(nx, ny) >= 2 and abs(nx - ny) <= 2):
                continue
            n += 1
            occs.append(Occ("XLING_NEIGHBOURS", "TRANSLATION", x, y, None, None, None, None, None,
                            f"csls={best_a_csls[i]:.3f} ccos={ccos:.3f}", raw))
        counts[f"{la}-{lb}"] = {"candidates_a": len(A), "candidates_b": len(B), "pairs": n}
    stats["xling"] = counts
    return occs


# ------------------------------------------------------------------------------------------------------ monolingual
def method_orthographic(ctx: Context, stats: dict[str, Any]) -> list[Occ]:
    groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for (lang, key), row in ctx.terms.items():
        groups[(lang, _compress(key, lang))].append((lang, key))
    occs: list[Occ] = []
    for (_lang, _c), members in sorted(groups.items()):
        if len(members) < 2 or len(members) > 6:
            continue
        members = sorted(members)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a = ctx.term_phrase(ctx.terms[members[i]])
                b = ctx.term_phrase(ctx.terms[members[j]])
                if len(_compress(a.key, a.lang)) < 5:
                    continue
                occs.append(Occ("ORTHOGRAPHIC", "SYNONYM", a, b, None, None, None, None, None, None, 1.0))
    stats["orthographic_pairs"] = len(occs)
    return occs


def method_patterns(con: Any, ctx: Context, p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    """SYNONYM_PATTERN: «X (или Y)», «X, иначе Y», «X (or Y)», «X, also known as Y», «X, also called Y» — both N3
    terms of one language, embedding similarity ``pattern_min_cos`` or more."""
    if not ctx.vectors:
        return []
    rows = _blocks(con, r"regexp_matches(b.normalized_text, '(?i)(\(\s*или\s|[(,]\s*иначе\s|\(\s*or\s|"
                        r"also known as|otherwise known as|also called|also termed)') "
                        r"AND b.block_type <> 'REFERENCE_LIST'")
    occs: list[Occ] = []
    for b in rows:
        text = b["text"] or ""
        res = analyze_text(text, ctx.morph, patterns=False)
        toks, _scripts = _tokenize(text)
        low = [t.lower() for t in toks]
        by_end: dict[int, list] = defaultdict(list)
        by_start: dict[int, list] = defaultdict(list)
        for c in res.cands:
            if c.kind == "NP":
                by_end[c.t1].append(c)
                by_start[c.t0].append(c)
        n = len(toks)
        for i, w in enumerate(low):
            y_start = None
            x_end = None
            closing = None
            if i >= 2 and ((w in ("или", "or") and toks[i - 1] == "(") or (w == "иначе" and toks[i - 1] in ("(", ","))):
                x_end, y_start = i - 2, i + 1           # «X (или Y)», «X (or Y)», «X, иначе Y,» — not «X, или Y»:
                closing = ")" if toks[i - 1] == "(" else ","    # a list of alternatives is not a synonym
            elif w == "as" and i >= 3 and " ".join(low[i - 2:i + 1]) in ("also known as", "otherwise known as"):
                x_end, y_start = i - 3 - (1 if toks[i - 3] == "," else 0), i + 1
                if toks[i - 3] == ",":
                    x_end = i - 4
            elif w in ("called", "termed") and i >= 2 and low[i - 1] == "also":
                x_end, y_start = i - 2 - (1 if toks[i - 2] == "," else 0), i + 1
            if y_start is None or x_end is None or x_end < 0:
                continue
            xs = [c for c in by_end.get(x_end, [])]
            ys = [c for c in by_start.get(y_start, [])]
            if closing is not None:
                ys = [c for c in ys if c.t1 + 1 < n and toks[c.t1 + 1] == closing]
            if not xs or not ys:
                continue
            best = None
            for cx in xs:
                for cy in ys:
                    if cx.lang != cy.lang:
                        continue
                    px = ctx.terms.get((cx.lang, cx.key))
                    py = ctx.terms.get((cy.lang, cy.key))
                    if px is None or py is None or cx.key == cy.key:
                        continue
                    a, bb = ctx.term_phrase(px), ctx.term_phrase(py)
                    c = ctx.cos(a, bb)
                    if c is not None and c >= p["pattern_min_cos"] and (best is None or c > best[2]):
                        best = (a, bb, c)
            if best is not None:
                occs.append(Occ("SYNONYM_PATTERN", "SYNONYM", best[0], best[1], b["source_id"], b["page_id"],
                                b["block_id"], None, None, w, best[2]))
    stats["pattern_occurrences"] = len(occs)
    return occs


# modifiers that name opposite or sibling notions: a substitution between two of them is never a synonym
_CONTRAST = frozenset("""
горизонтальный вертикальный продольный поперечный осевой боковой верхний нижний максимальный минимальный внутренний
внешний северный южный западный восточный положительный отрицательный прямой обратный первый второй третий
начальный конечный левый правый высокий низкий большой малый мелкий крупный длинный короткий ранний поздний старый
новый упругий пластический вязкий хрупкий сжатие растяжение нагрузка разгрузка подъем поднятие оседание
horizontal vertical longitudinal transverse lateral axial upper lower maximum minimum max min internal external inner
outer north south east west northern southern eastern western positive negative direct inverse first second third
initial final left right high low large small long short early late old new tensile compressive elastic plastic
viscous brittle compression tension loading unloading uplift subsidence
""".split())
# scale words: a specimen is not the rock mass (LAB ≠ MASSIF) — never merged by a word swap
_SCALE_WORDS = frozenset("образец порода массив керн проба sample specimen rock mass core".split())
_COORD_RE = r"([^\W\d_]{3,})\s+(?:и|или|либо|and|or)\s+([^\W\d_]{3,})"          # Python
_COORD_RE2 = r"(\pL{3,})\s+(?:и|или|либо|and|or)\s+(\pL{3,})"                     # DuckDB (RE2)


def _word_lemma(ctx: Context, word: str, lang: str) -> str:
    from vkm_corpus.navigation.concepts import _singular_en  # noqa: PLC0415

    w = word.lower().replace("ё", "е")
    if lang != "ru":
        return _singular_en(w)
    if ctx.morph.name == "pymorphy3":
        rw = ctx.morph.ru_word(w)
        if rw.noun is not None:
            return rw.noun[0]
        if rw.adj is not None:
            return rw.adj[0]
    return ctx.morph.ru_stem(w)


def coordination_counts(con: Any, ctx: Context, words: set[tuple[str, str]]) -> Counter:
    """How often two words (lemmas) are coordinated in the canon («горизонтальные и вертикальные», «tension or
    compression»): (lang, lemma_a, lemma_b), both orders counted under the sorted pair. Only ``words`` are kept."""
    blocks = _canon(con, "blocks")
    rows = con.execute(f"""
        SELECT lower(m) AS m, count(*) AS n FROM (
            SELECT unnest(regexp_extract_all(normalized_text, '{_COORD_RE2}')) AS m
            FROM {blocks} WHERE is_primary_layer AND block_type IN ('TEXT', 'ABSTRACT', 'CAPTION', 'LIST_ITEM'))
        GROUP BY 1""").fetchall()
    wanted = {(lg, w) for lg, w in words}
    out: Counter = Counter()
    rx = re.compile(_COORD_RE)
    for m, n in rows:
        g = rx.fullmatch(m)
        if not g:
            continue
        a, b = g.group(1), g.group(2)
        lang = "ru" if _CYR.search(a) else "en"
        la, lb = _word_lemma(ctx, a, lang), _word_lemma(ctx, b, lang)
        if la != lb and (lang, la) in wanted and (lang, lb) in wanted:
            out[(lang, *sorted((la, lb)))] += int(n)
    return out


def word_kind(ctx: Context, lang: str, w: str) -> str:
    """N (noun only) / A (adjective only) / NA / "" of a word — a noun is never swapped for an adjective
    («расчет/расчетный», «elasticity/elastic»)."""
    if lang == "ru":
        if ctx.morph.name != "pymorphy3":
            return ""
        rw = ctx.morph.ru_word(w)
        return ("N" if rw.noun is not None else "") + ("A" if rw.adj is not None else "")
    if re.search(r"(ity|ness|tion|sion|ment|ence|ance|ism|ogy|ics)$", w):
        return "N"
    return "A" if re.search(r"(ic|al|ive|ous|ent|ant|ible|able|ary)$", w) else ""


def pos_ok(ctx: Context, lang: str, a: str, b: str) -> bool:
    return {word_kind(ctx, lang, a), word_kind(ctx, lang, b)} != {"N", "A"}


def method_substitution(con: Any, ctx: Context, terms: Any, p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    """SUBSTITUTION: terms that differ in one word, where that word swap recurs across several terms («камерная
    система разработки / отработки», «глубина разработки / отработки») with high embedding similarity, and the two
    words are neither a contrast pair nor coordinated in the corpus («горизонтальные и вертикальные» are siblings,
    not synonyms)."""
    if not ctx.vectors:
        return []
    rows = [r for r in _as_rows(terms, ["lemma", "lemma_key", "language", "kind", "df_units", "n_words"])
            if r["kind"] == "NP" and 2 <= len(r["lemma_key"].split()) <= 4 and r["df_units"] >= p["sub_min_df"]]
    frames: dict[tuple[str, str, int], list[tuple[str, dict]]] = defaultdict(list)
    for r in rows:
        ws = r["lemma_key"].split()
        for i in range(len(ws)):
            frame = " ".join(ws[:i] + ["_"] + ws[i + 1:])
            frames[(r["language"], frame, len(ws))].append((ws[i], r))
    support: dict[tuple[str, str, str], list[tuple[dict, dict, float]]] = defaultdict(list)
    for (lang, _frame, _n), members in frames.items():
        if len(members) < 2 or len(members) > 40:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                (wa, ra), (wb, rb) = members[i], members[j]
                if wa == wb:
                    continue
                c = ctx.cos(ctx.term_phrase(ra), ctx.term_phrase(rb))
                if c is None or c < p["sub_pair_min_cos"]:
                    continue
                key = (lang, *sorted((wa, wb)))
                support[key].append((ra, rb, c))

    cand = {k: v for k, v in support.items() if len(v) >= p["sub_min_frames"]
            and not (k[1] in _CONTRAST and k[2] in _CONTRAST)
            and not (k[1] in _SCALE_WORDS and k[2] in _SCALE_WORDS) and pos_ok(ctx, *k)}
    coord = coordination_counts(con, ctx, {(k[0], w) for k in cand for w in k[1:]}) if cand else Counter()
    occs: list[Occ] = []
    kept = 0
    for (lang, wa, wb), lst in sorted(cand.items()):
        if coord.get((lang, wa, wb), 0) > p["sub_max_coordinated"]:
            continue
        mean = sum(c for _, _, c in lst) / len(lst)
        if mean < p["sub_mean_min_cos"]:
            continue
        kept += 1
        for ra, rb, c in lst:
            occs.append(Occ("SUBSTITUTION", "SYNONYM", ctx.term_phrase(ra), ctx.term_phrase(rb), None, None, None,
                            None, None, f"{wa} ↔ {wb} in {len(lst)} terms", c))
        a_row, b_row = ctx.terms.get((lang, wa)), ctx.terms.get((lang, wb))
        if a_row is not None and b_row is not None:
            c = ctx.cos(ctx.term_phrase(a_row), ctx.term_phrase(b_row))
            if c is not None and c >= p["sub_word_min_cos"]:
                occs.append(Occ("SUBSTITUTION", "SYNONYM", ctx.term_phrase(a_row), ctx.term_phrase(b_row), None, None,
                                None, None, None, f"word swap in {len(lst)} terms", c))
    stats["substitution"] = {"word_pairs_candidates": len(cand), "word_pairs_kept": kept, "occurrences": len(occs)}
    return occs


def pivot_synonyms(ctx: Context, table: pa.Table, p: Mapping[str, Any], stats: dict[str, Any]) -> list[Occ]:
    """PIVOT: two terms of one language that are both translations of one term of the other language (pairs with
    ``pivot_min_score`` or more, not seeds only), similar to each other and neither contained in the other («оседание
    земной поверхности» / «проседание земной поверхности» via «ground surface subsidence»)."""
    rows = [r for r in table.to_pylist() if r["relation"] == "TRANSLATION" and r["score"] >= p["pivot_min_score"]
            and r["methods"] != ["CURATED_SEED"]]
    by_side: dict[tuple[str, str, str], list[tuple[Phrase, float, str]]] = defaultdict(list)
    for r in rows:
        a = Phrase(r["key_a"], r["lang_a"], r["lemma_a"])
        b = Phrase(r["key_b"], r["lang_b"], r["lemma_b"])
        by_side[(b.lang, b.key, a.lang)].append((a, r["score"], b.text))
        by_side[(a.lang, a.key, b.lang)].append((b, r["score"], a.text))
    occs: list[Occ] = []
    for (_pl, _pk, _lang), members in sorted(by_side.items()):
        if len(members) < 2 or len(members) > 8:
            continue
        members = sorted(members, key=lambda m: (m[0].key, m[0].text))
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                x, sx, via = members[i]
                y, sy, _ = members[j]
                wx, wy = set(x.key.split()), set(y.key.split())
                if wx <= wy or wy <= wx:
                    continue                                  # broader / narrower, not a synonym
                if len(wx) == 1 and len(wy) == 1 and not pos_ok(ctx, x.lang, x.key, y.key):
                    continue
                if not (ctx.plain_word(x) and ctx.plain_word(y)):
                    continue
                c = ctx.cos(x, y)
                if c is None or c < p["pivot_min_cos"]:
                    continue
                occs.append(Occ("PIVOT", "SYNONYM", x, y, None, None, None, None, None, f"via {via}",
                                min(sx, sy)))
    stats["pivot_occurrences"] = len(occs)
    return occs


# ------------------------------------------------------------------------------------------------------ seeds
def seed_occurrences(ctx: Context, seeds: Any = None) -> list[Occ]:
    from vkm_corpus.navigation import term_dictionary_seeds as S  # noqa: PLC0415

    translations = S.TRANSLATIONS if seeds is None else seeds.get("translations", ())
    abbreviations = S.ABBREVIATIONS if seeds is None else seeds.get("abbreviations", ())
    synonyms = S.SYNONYMS if seeds is None else seeds.get("synonyms", ())
    occs: list[Occ] = []
    for row in translations:
        ru, en = row[0], row[1]
        de = row[2] if len(row) > 2 else None
        pr, pe = ctx.phrase(ru, "ru"), ctx.phrase(en, "en")
        if pr is not None and pe is not None:
            occs.append(Occ("CURATED_SEED", "TRANSLATION", pr, pe, None, None, None, None, None, None, 1.0))
        if de:
            pd = ctx.phrase(de, "de")
            if pd is not None and pr is not None:
                occs.append(Occ("CURATED_SEED", "TRANSLATION", pr, pd, None, None, None, None, None, None, 1.0))
            if pd is not None and pe is not None:
                occs.append(Occ("CURATED_SEED", "TRANSLATION", pe, pd, None, None, None, None, None, None, 1.0))
    for full, abbr, lang in abbreviations:
        pf, pa_ = ctx.phrase(full, lang), ctx.phrase(abbr, lang if not re.search("[A-Za-z]", abbr) else
                                                     script_lang(abbr))
        if pf is not None and pa_ is not None:
            occs.append(Occ("CURATED_SEED", "ABBREVIATION", pf, pa_, None, None, None, None, None, None, 1.0))
    for group in synonyms:
        lang, words = group[0], group[1:]
        ph = [x for x in (ctx.phrase(w, lang) for w in words) if x is not None]
        for i in range(len(ph)):
            for j in range(i + 1, len(ph)):
                if ph[i].key != ph[j].key:
                    occs.append(Occ("CURATED_SEED", "SYNONYM", ph[i], ph[j], None, None, None, None, None, None,
                                    1.0))
    return occs


# ------------------------------------------------------------------------------------------------------ aggregation
def pair_id(relation: str, a: Phrase, b: Phrase) -> str:
    return "TTR-" + hashlib.sha256(f"vkm-nav-term-pair-v1|{relation}|{a.lang}|{a.key}|{b.lang}|{b.key}"
                                   .encode("utf-8")).hexdigest()[:16]


def orient(relation: str, a: Phrase, b: Phrase) -> tuple[Phrase, Phrase]:
    if relation == "ABBREVIATION":
        return a, b                                          # producers put the full form first
    if a.lang != b.lang:
        return (a, b) if LANG_ORDER.get(a.lang, 9) < LANG_ORDER.get(b.lang, 9) else (b, a)
    return (a, b) if (a.key, a.text) <= (b.key, b.text) else (b, a)


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def method_confidence(method: str, best: float, n_sources: int, n_occ: int) -> float:
    """Estimated precision (0…1) of a pair proposed by ``method``, from its best local score (cosine) and support.
    Calibrated on the hand-checked sample of the first full build (receipt ``nav_term_dictionary.json``, strict
    precision per method and, for XLING_NEIGHBOURS, per cosine band); MODEL_CHOICE."""
    extra = 0.02 * max(0, min(n_sources, 4) - 1)
    if method == "CURATED_SEED":
        return 1.0
    if method == "ORTHOGRAPHIC":
        return 0.95
    if method == "KEYWORD_LISTS":
        return _clip(0.92 + 0.05 * _clip((best - 0.4) / 0.5) + extra)
    if method == "BILINGUAL_CAPTIONS":
        return _clip(0.9 + 0.05 * _clip((best - 0.72) / 0.2) + extra)
    if method == "SAME_AS_ABBREVIATION":
        return _clip(0.88 + extra)
    if method == "SYMBOL_DEFINITIONS":
        return _clip(0.85 + 0.05 * _clip((best - 0.72) / 0.2) + 0.02 * max(0, min(n_occ, 4) - 1))
    if method == "ABSTRACT_PAIRS":
        return _clip(0.8 + 0.1 * _clip((best - 0.72) / 0.23) + 0.02 * max(0, min(n_occ, 4) - 1))
    if method == "PIVOT":
        return _clip(0.65 + 0.1 * _clip((best - 0.9) / 0.1))
    if method == "XLING_NEIGHBOURS":
        return _clip(0.7 + 0.25 * _clip((best - 0.84) / 0.1))
    if method == "PAREN_GLOSS":
        return _clip(0.7 + 0.2 * _clip((best - 0.5) / 0.35) + extra)
    if method == "SUBSTITUTION":
        return _clip(0.6 + 0.1 * _clip((best - 0.88) / 0.1))
    if method == "SYNONYM_PATTERN":
        return 0.5
    return 0.5


EVIDENCE_TYPE = pa.struct([("method", pa.string()), ("source_id", pa.string()), ("page_id", pa.string()),
                           ("block_id", pa.string()), ("other_page_id", pa.string()),
                           ("other_block_id", pa.string()), ("detail", pa.string()), ("score", pa.float64())])
SCHEMA = pa.schema([
    ("pair_id", pa.string()), ("relation", pa.string()),
    ("term_id_a", pa.string()), ("term_id_b", pa.string()), ("lemma_a", pa.string()), ("lemma_b", pa.string()),
    ("key_a", pa.string()), ("key_b", pa.string()), ("lang_a", pa.string()), ("lang_b", pa.string()),
    ("in_terms_a", pa.bool_()), ("in_terms_b", pa.bool_()),
    ("methods", pa.list_(pa.string())), ("evidence", pa.list_(EVIDENCE_TYPE)),
    ("n_sources", pa.int32()), ("n_occurrences", pa.int32()), ("cosine", pa.float64()), ("score", pa.float64()),
    ("status", pa.string()), ("rule_version", pa.string()),
])


def aggregate(ctx: Context, occs: Iterable[Occ], *, max_examples: int = 5, min_score: float = 0.0) -> pa.Table:
    groups: dict[tuple[str, tuple[str, str], tuple[str, str]], dict[str, Any]] = {}
    for o in occs:
        a, b = orient(o.relation, o.a, o.b)
        if a.lang == b.lang and a.key == b.key:
            continue
        gk = (o.relation, (a.lang, a.key), (b.lang, b.key))
        g = groups.setdefault(gk, {"a": a, "b": b, "occ": []})
        g["occ"].append(o)
    rows: dict[str, list[Any]] = {f.name: [] for f in SCHEMA}
    for (relation, _ka, _kb), g in sorted(groups.items()):
        a, b = g["a"], g["b"]
        by_m: dict[str, list[Occ]] = defaultdict(list)
        for o in g["occ"]:
            by_m[o.method].append(o)
        conf = []
        evidence = []
        sources: set[str] = set()
        for m in sorted(by_m, key=lambda m: METHODS.index(m) if m in METHODS else 99):
            lst = by_m[m]
            srcs = {o.source_id for o in lst if o.source_id}
            sources |= srcs
            best = max(o.score for o in lst)
            conf.append(method_confidence(m, best, len(srcs), len(lst)))
            seen = set()
            for o in sorted(lst, key=lambda o: (-o.score, o.source_id or "", o.block_id or "", o.other_block_id or "")):
                sig = (o.source_id, o.block_id, o.other_block_id)
                if sig in seen:
                    continue
                seen.add(sig)
                evidence.append({"method": m, "source_id": o.source_id, "page_id": o.page_id, "block_id": o.block_id,
                                 "other_page_id": o.other_page_id, "other_block_id": o.other_block_id,
                                 "detail": o.detail, "score": round(float(o.score), 6)})
                if len([e for e in evidence if e["method"] == m]) >= max_examples:
                    break
        score = 1.0 - math.prod(1.0 - c for c in conf)
        if score < min_score:
            continue
        cos = ctx.cos(a, b)
        seed = "CURATED_SEED" in by_m
        rows["pair_id"].append(pair_id(relation, a, b))
        rows["relation"].append(relation)
        rows["term_id_a"].append(nav_ids.term_id(a.key))
        rows["term_id_b"].append(nav_ids.term_id(b.key))
        ra, rb = ctx.terms.get((a.lang, a.key)), ctx.terms.get((b.lang, b.key))
        rows["lemma_a"].append(ra["lemma"] if ra else a.text)
        rows["lemma_b"].append(rb["lemma"] if rb else b.text)
        rows["key_a"].append(a.key)
        rows["key_b"].append(b.key)
        rows["lang_a"].append(a.lang)
        rows["lang_b"].append(b.lang)
        rows["in_terms_a"].append(ra is not None)
        rows["in_terms_b"].append(rb is not None)
        rows["methods"].append(sorted(by_m, key=lambda m: METHODS.index(m) if m in METHODS else 99))
        rows["evidence"].append(evidence)
        rows["n_sources"].append(len(sources))
        rows["n_occurrences"].append(sum(len(v) for v in by_m.values()))
        rows["cosine"].append(None if cos is None else round(cos, 6))
        rows["score"].append(round(score, 6))
        rows["status"].append(STATUS_SEED if seed else STATUS_AUTO)
        rows["rule_version"].append(RULE_VERSION)
    t = pa.table(rows, schema=SCHEMA)
    return t.sort_by([("relation", "ascending"), ("lang_a", "ascending"), ("key_a", "ascending"),
                      ("lang_b", "ascending"), ("key_b", "ascending")])


# ------------------------------------------------------------------------------------------------------ build
def collect(con: Any, ctx: Context, *, terms: Any, term_edges: Any = None, formula_symbols: Any = None,
            seeds: Any = None, p: Mapping[str, Any] | None = None, stats: dict[str, Any] | None = None,
            use_seeds: bool = True) -> list[Occ]:
    """All evidence occurrences (the methods in order); also fills ``ctx.phrases_seen`` — the phrases a vector
    file must cover (``nav term-phrases``)."""
    p = {**DEFAULTS, **(p or {})}
    stats = stats if stats is not None else {}
    occs: list[Occ] = []
    for row in _as_rows(terms, ["lemma", "lemma_key", "language"]):
        ctx.phrases_seen.setdefault((row["language"], row["lemma_key"]), row["lemma"])
    per: dict[str, int] = {}

    def add(name: str, got: list[Occ]) -> None:
        per[name] = len(got)
        occs.extend(got)

    if use_seeds:
        add("CURATED_SEED", seed_occurrences(ctx, seeds))
    add("SAME_AS", method_same_as(ctx, terms, term_edges, p, stats))
    add("PAREN_GLOSS", method_paren(con, ctx, p, stats))
    kw, kw_pairs = method_keywords(con, ctx, p, stats)
    add("KEYWORD_LISTS", kw)
    add("ABSTRACT_PAIRS", method_abstracts(con, ctx, kw_pairs, [x for pr in kw_pairs for x in pr], p, stats))
    add("BILINGUAL_CAPTIONS", method_captions(con, ctx, p, stats))
    add("SYMBOL_DEFINITIONS", method_symbols(ctx, formula_symbols, p, stats))
    add("XLING_NEIGHBOURS", method_xling(ctx, terms, p, stats))
    add("ORTHOGRAPHIC", method_orthographic(ctx, stats))
    add("SYNONYM_PATTERN", method_patterns(con, ctx, p, stats))
    add("SUBSTITUTION", method_substitution(con, ctx, terms, p, stats))
    stats["occurrences"] = per
    return occs


def build(con: Any, *, terms: Any = None, term_edges: Any = None, term_mentions: Any = None,
          formula_symbols: Any = None, seeds: Any = None, stats: dict[str, Any] | None = None,
          **options: Any) -> dict[str, pa.Table] | None:
    """``term_translations`` from the canon behind ``con`` and the N2/N3 datasets of the same snapshot
    (``--inputs``). Options: ``DEFAULTS`` (``term_vectors`` — the Parquet of ``nav term-vectors``); other keyword
    arguments of ``vkm-corpus nav build`` are ignored. Returns None without ``terms`` (SKIPPED_NO_INPUT)."""
    if terms is None:
        return None
    p = {k: options.get(k, v) for k, v in DEFAULTS.items()}
    stats = stats if stats is not None else {}
    ctx = make_context(con, terms=terms, term_mentions=term_mentions, vectors=p["term_vectors"])
    occs = collect(con, ctx, terms=terms, term_edges=term_edges, formula_symbols=formula_symbols, seeds=seeds, p=p,
                   stats=stats)
    first = aggregate(ctx, occs, max_examples=int(p["max_examples"]), min_score=float(p["min_score"]))
    occs += pivot_synonyms(ctx, first, p, stats)                 # second pass: synonyms through a common translation
    table = aggregate(ctx, occs, max_examples=int(p["max_examples"]), min_score=float(p["min_score"]))
    rel = Counter(table.column("relation").to_pylist())
    by_method = Counter(m for ms in table.column("methods").to_pylist() for m in ms)
    vec = p["term_vectors"]
    vec_name = None if vec is None else (str(vec).replace("\\", "/").rsplit("/", 1)[-1]
                                         if isinstance(vec, (str, bytes)) or hasattr(vec, "__fspath__") else "<table>")
    stats.update({"rule_version": RULE_VERSION, "morphology": ctx.morph.name, "vectors": len(ctx.vectors),
                  "params": {k: (v if k != "term_vectors" else vec_name) for k, v in p.items()},
                  "pairs": table.num_rows, "by_relation": dict(rel), "by_method": dict(by_method),
                  "by_status": dict(Counter(table.column("status").to_pylist()))})
    log.info("term dictionary: %d pairs %s", table.num_rows, dict(rel))
    table = table.replace_schema_metadata({METADATA_KEY: json.dumps(stats, ensure_ascii=False,
                                                                    default=str).encode("utf-8")})
    return {"term_translations": table}


def build_info(table: pa.Table) -> dict[str, Any]:
    md = table.schema.metadata or {}
    return json.loads(md.get(METADATA_KEY, b"{}"))


def phrases_table(ctx: Context) -> pa.Table:
    """The phrases seen by :func:`collect` (lang, key, text) — the input of ``nav term-vectors``."""
    items = sorted(ctx.phrases_seen.items())
    return pa.table({"lang": [k[0] for k, _ in items], "key": [k[1] for k, _ in items],
                     "text": [t for _, t in items]})


__all__ = ["build", "collect", "aggregate", "make_context", "load_term_vectors", "phrases_table", "Phrase", "Occ",
           "Context", "DEFAULTS", "SCHEMA", "RULE_VERSION", "METHODS", "RELATIONS", "script_lang", "pair_id"]
