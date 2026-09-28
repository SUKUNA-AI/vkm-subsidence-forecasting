"""Near-duplicate text across sources — NAV part ``duplicates`` (rule ``duplicates_v1``), no LLM.

``build(con, *, vectors=None, stats=None, **options)`` reads the canon behind ``con`` (DuckDB: ``canonical.pages``,
primary-layer ``canonical.blocks``, figure/table captions and bibliography pages for the unit rule,
``canonical.source_work_links`` + ``canonical.works`` for works and years) and returns three Arrow tables:

* ``dup_clusters`` — passages found in two or more sources: ``kind``, the primary source (earliest publication year;
  ties and missing years by the rules below), counts;
* ``dup_members`` — the passages themselves: embedding units ``vkm-units-v1`` of kind BLOCK_GROUP (consecutive
  primary blocks of one page, ids ``u1-…`` as in search and the vector store) with page, blocks and the overlap with
  the reference source of the cluster;
* ``source_overlap`` — pairs of sources that share passages, with the shares of their units.

Method (every number is a MODEL_CHOICE of ``DEFAULTS``, recorded in the manifest):

1. units shorter than ``min_norm_chars`` after normalisation, or mostly reference lists (REFERENCE_LIST blocks or
   numbered bibliographic lines), are skipped — shared citations are not shared text;
2. normalisation for overlap: NFKC, case fold, ё→е, only letters and digits kept (spaces and hyphenation vanish),
   Latin/Ukrainian look-alikes folded to Cyrillic (OCR mixes alphabets); shingles are character ``shingle_k``-grams
   of that stream, hashed to 40 bits (polynomial hash + splitmix64 — identical on CPU and GPU);
3. candidate pairs of units of different sources: MinHash-LSH (``minhash_perms`` permutations, bands of
   ``lsh_rows``) and, when ``vectors`` (dense unit vectors) are given, cosine kNN (``knn_k`` neighbours,
   cosine >= ``knn_min_cosine``); a vector whose ``text_hash`` differs from the unit text is stale and ignored;
4. a candidate is verified when the shingle sets overlap: containment ``|A∩B| / min(|A|,|B|) >= min_containment``
   and ``|A∩B| >= min_shared_grams`` (exact counts, no estimate);
5. clusters are connected components of verified pairs (cuGraph on the GPU, union-find on the CPU);
6. the shared text of a member is its sentences whose shingles are >= ``shared_sentence_min`` found in members of
   other sources; the cluster is *template* when >= ``template_cluster_share`` of the shared characters are template
   sentences (title pages, funding notes, licences, publication counts, OCR placeholders) and *abstract-like* when
   >= ``abstract_cluster_share`` of its members have an abstract marker in their shared text;
7. kind, first rule that applies: ``SAME_WORK_COPY`` — all member sources are copies of one work; ``BOILERPLATE`` —
   template text in >= ``boilerplate_min_works`` works, or in two works without a substantial pair; ``REPRINT`` —
   two member sources of different works share >= ``reprint_min_shared`` non-template passages (reprint, an article
   inside an issue or a collection, text reused in a later book, an author abstract and its dissertation);
   ``SHARED_ABSTRACT`` — abstract-like; ``PARTIAL_OVERLAP`` — an isolated shared passage (quotation, reused
   paragraph, a page shared by two files);
8. primary: the work with the earliest ``publication_year``; ties → container/derivative work types after original
   works (``SECONDARY_WORK_TYPES``) → the work whose passage is nearer the start of its source → ``work_id``; UNKNOWN
   (NULL) when a member year is missing; not applicable to BOILERPLATE. Inside the chosen work (and for
   SAME_WORK_COPY) the copy is the FULL_COPY link, then the anchor source of the work, then ``source_id``.
   ``primary_rule`` names the deciding rule. The reference source (for the similarity columns and for keeping one
   instance in statistics) is the primary when known, otherwise the first source by the same ordering.

The layer is DERIVED navigation (``AUTO_EXTRACTED_UNREVIEWED``): «primary» is a hint for ordering copies in a
dossier, never a claim of authorship or priority. Heavy steps (shingles, MinHash, kNN, pair overlap, components) run
on the GPU with CuPy/cuVS/cuGraph when importable (``backend="auto"``) and with numpy otherwise; both give the same
rows (integer hashing; cosines recomputed in float64 on the CPU).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations
from typing import Any, Sequence

import numpy as np
import pyarrow as pa

from vkm_corpus.navigation import ids as nav_ids

log = logging.getLogger(__name__)

RULE_VERSION = nav_ids.RULE_VERSIONS["duplicates"]
UNIT_KIND = "BLOCK_GROUP"
KINDS: tuple[str, ...] = ("SAME_WORK_COPY", "REPRINT", "SHARED_ABSTRACT", "PARTIAL_OVERLAP", "BOILERPLATE")
PRIMARY_RULES: tuple[str, ...] = ("EARLIEST_YEAR", "TIE_WORK_TYPE", "TIE_POSITION", "TIE_WORK_ID", "UNKNOWN_YEAR",
                                  "NOT_APPLICABLE", "SAME_WORK_FULL_COPY", "SAME_WORK_ANCHOR", "SAME_WORK_SOURCE_ID")
# on a tie of years these work types yield to original works: containers (issue, proceedings, index) and derivatives
SECONDARY_WORK_TYPES = frozenset({"JOURNAL_ISSUE", "PROCEEDINGS_VOLUME", "BIBLIOGRAPHIC_INDEX", "DISSERTATION_ABSTRACT",
                                  "PRESENTATION"})
LINK_RANK = {"FULL_COPY": 0, "PARTIAL_COPY": 1, "PART": 1, "FRONT_MATTER_ONLY": 2}
NOTE = ("DERIVED navigation layer (AUTO_EXTRACTED_UNREVIEWED): passages whose text overlaps across sources. "
        "'primary' is the earliest source by publication year (a hint for ordering copies, not a claim of authorship "
        "or priority); BOILERPLATE is template text with no original.")

DEFAULTS: dict[str, Any] = {
    "backend": "auto",              # "auto" | "gpu" | "cpu"
    "channels": ("lsh", "knn"),     # candidate generators; "knn" needs vectors
    "min_norm_chars": 150,          # letters+digits after normalisation; shorter units are skipped
    "max_reference_share": 0.5,     # units with >= this share of reference-list characters are skipped
    "shingle_k": 8,                 # character shingles of the normalised stream
    "minhash_perms": 96,
    "lsh_rows": 3,                  # 32 bands of 3 rows: P(candidate) ~0.99 at Jaccard 0.5, ~0.58 at 0.3
    "lsh_seed": 20260928,
    "lsh_max_bucket": 200,          # a larger bucket is linked as a star to its first unit
    "knn_k": 16,
    "knn_min_cosine": 0.70,
    "min_containment": 0.5,         # |A∩B| / min(|A|, |B|)
    "min_shared_grams": 100,        # |A∩B| (about 100 letters of shared text)
    "shared_sentence_min": 0.5,     # a sentence is shared when this share of its shingles occurs in other sources
    "template_cluster_share": 0.5,  # template when this share of the shared characters is in template sentences
    "abstract_cluster_share": 0.5,  # abstract-like when this share of members has an abstract marker in shared text
    "reprint_min_shared": 3,        # non-template passages shared by two sources of different works
    "boilerplate_min_works": 3,
    "gpu_mem_limit_gb": 6.0,        # CuPy memory pool cap (the GPU is shared)
}
MODEL_CHOICES: dict[str, str] = {
    "unit": "vkm-units-v1 BLOCK_GROUP units (consecutive primary blocks of one page, 900/1600 characters)",
    "unit_filter": "min_norm_chars; reference lists (REFERENCE_LIST blocks or numbered bibliographic lines) skipped",
    "normalisation": "NFKC, casefold, ё→е, letters+digits only, Latin/Ukrainian look-alikes folded to Cyrillic",
    "shingles": "character shingle_k-grams of the normalised stream, polynomial hash (base 0x100000001B3) + "
                "splitmix64, top 40 bits",
    "minhash_lsh": "minhash_perms splitmix64 permutations (seeded by lsh_seed), bands of lsh_rows rows",
    "knn": "inner product of L2-normalised dense unit vectors, knn_k neighbours, cosine >= knn_min_cosine",
    "verification": "containment |A∩B|/min(|A|,|B|) >= min_containment and |A∩B| >= min_shared_grams",
    "clusters": "connected components of verified pairs",
    "shared_text": "sentences of a member with >= shared_sentence_min of their shingles in members of other sources",
    "template_markers": "title pages (ministry, institution, «на правах рукописи», «диссертация на соискание»), "
                        "funding and acknowledgements, licences and copyright, publication counts, e-mail/phone "
                        "lines, OCR placeholder sentences",
    "abstract_markers": "«аннотация», «ключевые слова», «реферат», «резюме», «abstract», «keywords»",
    "primary": "earliest publication_year of the member works; ties: SECONDARY_WORK_TYPES after original works, then "
               "the work whose passage is nearer the start of its source, then work_id; UNKNOWN when a member year is "
               "missing; the copy of a work: FULL_COPY link, anchor source, source_id",
}

# ------------------------------------------------------------------------------------------------ text rules

_FOLD = str.maketrans({"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "k": "к", "m": "м",
                       "t": "т", "h": "н", "b": "в", "i": "и", "u": "и", "n": "п", "r": "г",
                       "і": "и", "ї": "и", "є": "е", "ґ": "г", "ў": "у"})
_NON_ALNUM = re.compile(r"[\W_]+")
_SENTENCES = re.compile(r"(?<=[.!?;])\s+|\n+")
_TEMPLATE_RE = re.compile("|".join([
    r"министерств\w* (?:науки|образования|высшего|просвещения)", r"федеральн\w* (?:государственн|агентств)\w*",
    r"образовательн\w* учреждени\w*", r"учреждени\w* (?:высшего|науки)", r"на правах рукописи",
    r"диссертаци\w* на соискани\w*", r"на соискани\w* ученой степени", r"автореферат\w* диссертаци\w*",
    r"научн\w* (?:руководител\w*|консультант\w*)", r"специальност\w* \d", r"\bвак\b", r"учен\w* (?:степен|звани)\w*",
    r"(?:кандидат|доктор)\w* [\w-]*(?:технических|геолого|физико|химических|экономических|географических)\w*"
    r"[\w-]* наук", r"\bпрофессор\w*", r"\bдоцент\w*", r"рудничн\w* аэрогазодинамик\w*",
    r"высш\w* аттестационн\w* комисси\w*", r"состоит из введения", r"изложен\w* на \d+ страниц\w*",
    r"из \d+ наименовани\w*",
    r"финансов\w* поддержк\w*", r"при поддержке", r"государственн\w* задани\w*", r"\bгрант\w*", r"\bниоктр\b",
    r"благодарност\w*", r"признательн\w*", r"\backnowledg\w*", r"\bfunding\b", r"\bsupported by\b",
    r"financial support", r"creative commons", r"open access", r"all rights reserved", r"\bisbn\b", r"\bудк\b",
    r"\bббк\b", r"\bcopyright\b", "©", r"textcircled", r"competing interests?", r"conflicts? of interest",
    r"конфликт\w* интерес\w*", r"\bdeclarations?\b", r"reprints and permissions", r"publisher.?s note",
    r"no part of this (?:publication|book)", r"\bpermission of the publisher", r"translated from",
    r"original article submitted", r"catalogue record", r"first published", r"printed in (?:the )?[a-z]+",
    r"university press", r"издан\w* в рамках", r"рецензент\w*", r"рекомендован\w* (?:в качестве|к изданию|к печати)",
    r"печатается по (?:решению|постановлению)", r"подписано в печать", r"\bтираж\b", r"усл\. ?печ\. ?л",
    r"опубликован\w* \d+",
    r"печатн\w* работ", r"рецензируем\w* (?:журнал|издани)\w*", r"апробаци\w* работ\w*", r"структура и объем",
    r"объем работы", r"\be-?mail\b", r"\bтел\.", r"image provided is too blurry", r"no text can be extracted",
    r"cannot provide any information about the content",
]))
_ABSTRACT_RE = re.compile(r"\bаннотаци\w*|ключев\w* слов\w*|\bреферат\b|\bрезюме\b|\babstract\b|\bkey ?words?\b")
_BIB_HEAD_RE = re.compile(r"^(?:список (?:использованн\w+ )?(?:литератур|источник)\w*|библиографическ\w+ список|"
                          r"литература|references|bibliography)\b")
_BIB_ENTRY_RE = re.compile(r"^(?:\[\d{1,3}\]|\d{1,3}[.)])\s*\S")
_BIB_YEAR_RE = re.compile(r"\b(?:1[89]|20)\d{2}\b")
_BIB_MARK_RE = re.compile(r"//|\s[–—-]\s|\bс\.\s?\d|\d\s?с\.|\bpp?\.\s?\d|\bvol\.|№|изд|\bdoi\b|:\s")


def norm_key(text: str | None) -> str:
    """The character stream compared across sources (see the module docstring, step 2)."""
    t = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return _NON_ALNUM.sub("", t).translate(_FOLD)


def _lower(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")).strip()


def split_sentences(text: str | None) -> list[str]:
    return [s for s in _SENTENCES.split(text or "") if s.strip()]


def is_template(sentence: str) -> bool:
    return bool(_TEMPLATE_RE.search(_lower(sentence)))


def has_abstract_marker(text: str | None) -> bool:
    return bool(_ABSTRACT_RE.search(_lower(text or "")))


def bibliography_share(text: str | None) -> float:
    """Share of the characters of ``text`` in lines that look like a reference list (a heading, or a numbered entry
    with a year and a bibliographic mark)."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    total = sum(len(ln) for ln in lines)
    if not total:
        return 0.0
    bib = 0
    for ln in lines:
        low = _lower(ln)
        if _BIB_HEAD_RE.match(low) or (_BIB_ENTRY_RE.match(low) and _BIB_YEAR_RE.search(low)
                                       and _BIB_MARK_RE.search(low)):
            bib += len(ln)
    return bib / total


# ------------------------------------------------------------------------------------------------ canon rows


def _table_exists(con: Any, schema: str, name: str) -> bool:
    try:
        return bool(con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = ? "
                                "AND table_name = ?", [schema, name]).fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


def _rows(con: Any, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, list(params or []))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _optional_rows(con: Any, table: str, cols: str) -> list[dict[str, Any]]:
    if not _table_exists(con, "canonical", table):
        return []
    have = {r["column_name"] for r in _rows(con, "SELECT column_name FROM information_schema.columns "
                                                 "WHERE table_schema = 'canonical' AND table_name = ?", [table])}
    keep = [c.strip() for c in cols.split(",") if c.strip() in have]
    return _rows(con, f'SELECT {", ".join(keep)} FROM canonical."{table}"') if keep else []


def load_units(con: Any) -> list[dict[str, Any]]:
    """BLOCK_GROUP units of rule ``vkm-units-v1`` over the canon (the same ids and texts as the embedding units)."""
    from vkm_corpus.retrieval_lab.units import build_units  # noqa: PLC0415

    pages = _rows(con, "SELECT page_id, source_id, page_index FROM canonical.pages")
    blocks = _rows(con, """
        SELECT object_id, source_id, page_id, block_type, reading_order, is_primary_layer, normalized_text
        FROM canonical.blocks
        WHERE coalesce(is_primary_layer, true) AND normalized_text IS NOT NULL AND length(trim(normalized_text)) > 0""")
    figures = _optional_rows(con, "figures", "object_id, page_id, caption_block_id")
    tables = _optional_rows(con, "tables", "object_id, page_id, caption_block_id")
    bib = _optional_rows(con, "bibliography_entries", "object_id, page_id")
    btype = {b["object_id"]: (b["block_type"], len(b["normalized_text"] or "")) for b in blocks}
    pidx = {p["page_id"]: int(p["page_index"] or 0) for p in pages}
    out = []
    for u in build_units(pages, blocks, figures, tables, (), bib):
        if u.kind != UNIT_KIND:
            continue
        chars: Counter = Counter()
        for oid in u.object_ids:
            t, n = btype.get(oid, ("UNKNOWN", 0))
            chars[t] += n
        ref = max(chars.get("REFERENCE_LIST", 0) / max(1, sum(chars.values())), bibliography_share(u.text))
        out.append({"unit_id": u.unit_id, "source_id": u.source_id, "page_id": u.page_id,
                    "page_index": pidx.get(u.page_id, 0), "block_ids": list(u.object_ids), "text": u.text,
                    "reference_share": ref})
    return out


def load_sources(con: Any) -> dict[str, dict[str, Any]]:
    """Per source: its work (primary link), link type, the work's year, type and anchor source, page range."""
    out: dict[str, dict[str, Any]] = {}
    for r in _rows(con, "SELECT source_id, min(page_index) AS first, max(page_index) AS last FROM canonical.pages "
                        "GROUP BY source_id"):
        out[r["source_id"]] = {"first": int(r["first"] or 0), "last": int(r["last"] or 0), "work_id": None,
                               "link_type": None, "year": None, "work_type": None, "anchor": None}
    if not (_table_exists(con, "canonical", "source_work_links") and _table_exists(con, "canonical", "works")):
        return out
    rows = _rows(con, """
        SELECT l.source_id, l.work_id, l.link_type, w.publication_year AS year, w.work_type,
               w.anchor_source_id AS anchor
        FROM canonical.source_work_links l LEFT JOIN canonical.works w ON w.work_id = l.work_id
        WHERE l.is_primary AND l.work_id IS NOT NULL AND coalesce(l.curation_status, '') <> 'REJECTED'
        ORDER BY l.source_id, l.work_id""")
    for r in rows:
        s = out.setdefault(r["source_id"], {"first": 0, "last": 0})
        if s.get("work_id") is None:
            s.update({"work_id": r["work_id"], "link_type": r["link_type"],
                      "year": int(r["year"]) if r["year"] is not None else None, "work_type": r["work_type"],
                      "anchor": r["anchor"]})
    return out


# ------------------------------------------------------------------------------------------------ backends


def _gpu_modules() -> Any:
    """CuPy when importable and a device is present (CUDA headers of a conda RAPIDS env are pointed at first)."""
    if not os.environ.get("CUDA_PATH"):
        for base in {sys.prefix, sys.base_prefix}:
            cand = os.path.join(base, "targets", "x86_64-linux")
            if os.path.exists(os.path.join(cand, "include", "cuda_runtime.h")):
                os.environ["CUDA_PATH"] = cand
                break
    try:
        import cupy as cp  # noqa: PLC0415

        if cp.cuda.runtime.getDeviceCount() < 1:
            return None
        return cp
    except Exception:  # noqa: BLE001 — no CuPy, no driver, no device
        return None


_POLY = 0x100000001B3
_MASK40 = (1 << 40) - 1


def _mix(x: Any, xp: Any) -> Any:
    """splitmix64 finalizer (uint64 arrays wrap on overflow)."""
    x = x ^ (x >> xp.uint64(30))
    x = x * xp.uint64(0xBF58476D1CE4E5B9)
    x = x ^ (x >> xp.uint64(27))
    x = x * xp.uint64(0x94D049BB133111EB)
    return x ^ (x >> xp.uint64(31))


def shingle_keys(norms: Sequence[str], k: int, xp: Any = np) -> tuple[Any, Any, Any]:
    """Unique 40-bit shingle keys of each normalised string: (keys ordered by unit then key, starts, counts)."""
    n = len(norms)
    lens = np.fromiter((len(s) for s in norms), dtype=np.int64, count=n)
    codes_np = np.frombuffer("".join(norms).encode("utf-32-le"), dtype=np.uint32)
    total = int(codes_np.size)
    if total < k:
        z = xp.zeros(n, dtype=xp.int64)
        return xp.zeros(0, dtype=xp.uint64), z, z.copy()
    with np.errstate(over="ignore"):
        codes = xp.asarray(codes_np).astype(xp.uint64)
        m = total - k + 1
        h = codes[:m].copy()
        for j in range(1, k):
            h = h * xp.uint64(_POLY) + codes[j:m + j]
        h = _mix(h, xp)
    starts = np.cumsum(lens) - lens
    seg = xp.repeat(xp.arange(n, dtype=xp.int64), xp.asarray(lens))[:m]
    ok = (xp.arange(m, dtype=xp.int64) + k) <= xp.asarray(starts + lens)[seg]
    packed = (seg[ok].astype(xp.uint64) << xp.uint64(40)) | (h[ok] >> xp.uint64(24))
    packed = xp.unique(packed)
    useg = (packed >> xp.uint64(40)).astype(xp.int64)
    keys = packed & xp.uint64(_MASK40)
    counts = xp.bincount(useg, minlength=n).astype(xp.int64)
    return keys, xp.cumsum(counts) - counts, counts


_MINHASH_SRC = r'''
extern "C" __global__ void vkm_minhash(const unsigned long long* keys, const long long* starts,
                                       const long long* counts, const unsigned long long* seeds, const int P,
                                       const long long nseg, unsigned long long* out) {
  long long t = (long long)blockIdx.x * blockDim.x + threadIdx.x;
  if (t >= nseg * P) return;
  long long s = t / P; int p = (int)(t % P);
  unsigned long long seed = seeds[p], m = 0xFFFFFFFFFFFFFFFFULL;
  long long a = starts[s], e = a + counts[s];
  for (long long i = a; i < e; ++i) {
    unsigned long long x = keys[i] ^ seed;
    x ^= x >> 30; x *= 0xBF58476D1CE4E5B9ULL; x ^= x >> 27; x *= 0x94D049BB133111EBULL; x ^= x >> 31;
    if (x < m) m = x;
  }
  out[t] = m;
}'''


def minhash(keys: Any, starts: Any, counts: Any, seeds: np.ndarray, xp: Any = np) -> np.ndarray:
    """Signatures [units × permutations]: min over a unit's keys of splitmix64(key XOR seed). Units need >= 1 key."""
    n, p = int(counts.shape[0]), len(seeds)
    if n == 0:
        return np.zeros((0, p), dtype=np.uint64)
    if int(counts.min()) < 1:
        raise ValueError("minhash needs at least one shingle per unit")
    if xp is np:
        sig = np.empty((n, p), dtype=np.uint64)
        with np.errstate(over="ignore"):
            for i, s in enumerate(seeds):
                sig[:, i] = np.minimum.reduceat(_mix(keys ^ np.uint64(s), np), starts)
        return sig
    kern = xp.RawKernel(_MINHASH_SRC, "vkm_minhash")
    out = xp.empty((n, p), dtype=xp.uint64)
    threads = 256
    kern(((n * p + threads - 1) // threads,), (threads,),
         (keys, starts.astype(xp.int64), counts.astype(xp.int64), xp.asarray(seeds, dtype=xp.uint64), np.int32(p),
          np.int64(n), out))
    return xp.asnumpy(out)


def lsh_pairs(sig: np.ndarray, src: np.ndarray, rows: int, max_bucket: int) -> set[tuple[int, int]]:
    """Pairs (i < j) of units of different sources that share a band key; a bucket larger than ``max_bucket`` is
    linked as a star to its first unit."""
    n, p = sig.shape
    pairs: set[tuple[int, int]] = set()
    if n < 2:
        return pairs
    with np.errstate(over="ignore"):
        for b in range(p // rows):
            key = sig[:, b * rows].copy()
            for j in range(1, rows):
                key = _mix(key ^ sig[:, b * rows + j], np)
            order = np.argsort(key, kind="stable")
            ks = key[order]
            bounds = np.concatenate([[0], np.flatnonzero(ks[1:] != ks[:-1]) + 1, [n]])
            for r in np.flatnonzero(np.diff(bounds) >= 2):
                g = np.sort(order[bounds[r]:bounds[r + 1]])
                if len(g) > max_bucket:
                    c = int(g[0])
                    pairs.update((c, int(x)) for x in g[1:] if src[x] != src[c])
                    continue
                gs = src[g]
                for ii in range(len(g) - 1):
                    for jj in range(ii + 1, len(g)):
                        if gs[ii] != gs[jj]:
                            pairs.add((int(g[ii]), int(g[jj])))
    return pairs


def knn(mat: np.ndarray, k: int, xp: Any = np) -> np.ndarray:
    """Indices [n × k] of the nearest rows by inner product (self excluded; -1 pads when n <= k)."""
    n = mat.shape[0]
    kk = min(k, max(n - 1, 0))
    out = np.full((n, k), -1, dtype=np.int64)
    if kk == 0:
        return out
    if xp is not np:
        from cuvs.neighbors import brute_force  # noqa: PLC0415

        dm = xp.asarray(mat)
        index = brute_force.build(dm, metric="inner_product")
        _, nn = brute_force.search(index, dm, kk + 1)
        nn = xp.asnumpy(xp.asarray(nn)).astype(np.int64)
        for i in range(n):
            row = [j for j in nn[i] if j != i and j >= 0][:kk]
            out[i, :len(row)] = row
        return out
    step = 1024
    for lo in range(0, n, step):
        hi = min(n, lo + step)
        s = mat[lo:hi] @ mat.T
        s[np.arange(hi - lo), np.arange(lo, hi)] = -np.inf
        part = np.argpartition(-s, kk - 1, axis=1)[:, :kk]
        vals = np.take_along_axis(s, part, axis=1)
        order = np.argsort(-vals, axis=1, kind="stable")
        out[lo:hi, :kk] = np.take_along_axis(part, order, axis=1)
    return out


def _ragged(keys: Any, starts: Any, counts: Any, units: Any, xp: Any) -> tuple[Any, Any]:
    lens = counts[units]
    offs = xp.cumsum(lens) - lens
    total = int(lens.sum()) if len(units) else 0
    owner = xp.repeat(xp.arange(len(units), dtype=xp.int64), lens)
    pos = xp.arange(total, dtype=xp.int64) - offs[owner] + starts[units][owner]
    return keys[pos], owner


def intersections(keys: Any, starts: Any, counts: Any, a: np.ndarray, b: np.ndarray, xp: Any = np,
                  budget: int = 40_000_000) -> np.ndarray:
    """|keys(a_i) ∩ keys(b_i)| for every pair (exact; the key sets of one unit are unique)."""
    out = np.zeros(len(a), dtype=np.int64)
    if not len(a):
        return out
    cnt = np.asarray(counts.get() if hasattr(counts, "get") else counts)
    vol = cnt[a] + cnt[b]
    lo = 0
    while lo < len(a):
        hi, acc = lo, 0
        while hi < len(a) and (hi == lo or acc + vol[hi] <= budget) and hi - lo < (1 << 23):
            acc += int(vol[hi])
            hi += 1
        ua, ub = xp.asarray(a[lo:hi]), xp.asarray(b[lo:hi])
        ka, oa = _ragged(keys, starts, counts, ua, xp)
        kb, ob = _ragged(keys, starts, counts, ub, xp)
        comb = xp.concatenate([(oa.astype(xp.uint64) << xp.uint64(40)) | ka,
                               (ob.astype(xp.uint64) << xp.uint64(40)) | kb])
        comb = xp.sort(comb)
        dup = comb[1:][comb[1:] == comb[:-1]]
        res = xp.bincount((dup >> xp.uint64(40)).astype(xp.int64), minlength=hi - lo)
        out[lo:hi] = res.get() if hasattr(res, "get") else res
        lo = hi
    return out


def components(n: int, a: np.ndarray, b: np.ndarray, gpu: bool = False) -> np.ndarray:
    """Component label (the smallest member index) of every vertex 0..n-1; isolated vertices label themselves."""
    label = np.arange(n, dtype=np.int64)
    if not len(a):
        return label
    if gpu:
        import cudf  # noqa: PLC0415
        import cugraph  # noqa: PLC0415

        g = cugraph.Graph(directed=False)
        g.from_cudf_edgelist(cudf.DataFrame({"src": a.astype(np.int32), "dst": b.astype(np.int32)}), source="src",
                             destination="dst")
        df = cugraph.connected_components(g).to_pandas()
        mins = df.groupby("labels")["vertex"].transform("min").to_numpy()
        label[df["vertex"].to_numpy()] = mins
        return label
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for x, y in zip(a.tolist(), b.tolist()):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)
    return np.array([find(i) for i in range(n)], dtype=np.int64)


# ------------------------------------------------------------------------------------------------ vectors


def load_vectors(path: str | os.PathLike, unit_ids: Sequence[str], texts: Sequence[str]
                 ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Dense vectors of the given units from a directory of Parquet parts (``object_id``, ``object_type``,
    ``text_hash``, ``vector``): (matrix [m × d] L2-normalised, unit positions [m] ascending, info). A vector whose
    ``text_hash`` is not the sha256 of the unit text is stale (another snapshot) and ignored."""
    import duckdb  # noqa: PLC0415

    d = os.fspath(path)
    parts = sorted(f for f in os.listdir(d) if f.endswith(".parquet")) if os.path.isdir(d) else []
    if not parts:
        raise FileNotFoundError("no Parquet parts of dense vectors in the given directory")
    info: dict[str, Any] = {"parts": len(parts)}
    fp = os.path.join(d, "config.json")
    if os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8") as fh:
                doc = json.load(fh)
            cfg = doc.get("config", {})
            info.update({"model_id": cfg.get("model_id"), "model_revision": cfg.get("model_revision"),
                         "text_rule": cfg.get("text_rule"), "config_signature": doc.get("config_signature")})
        except (OSError, ValueError, AttributeError):
            pass
    empty = (np.zeros((0, 0), dtype=np.float32), np.zeros(0, dtype=np.int64))
    pos = {u: i for i, u in enumerate(unit_ids)}
    db = duckdb.connect()
    try:
        files = [os.path.join(d, f) for f in parts]
        tbl = db.execute("SELECT object_id, text_hash, vector FROM read_parquet(?) WHERE object_type = ? "
                         "ORDER BY object_id", [files, UNIT_KIND]).to_arrow_table()
    finally:
        db.close()
    take, where, stale, seen = [], [], 0, set()
    for r, (o, h) in enumerate(zip(tbl.column("object_id").to_pylist(), tbl.column("text_hash").to_pylist())):
        i = pos.get(o)
        if i is None or i in seen:
            continue
        if h != hashlib.sha256(texts[i].encode("utf-8")).hexdigest():
            stale += 1
            continue
        seen.add(i)
        take.append(r)
        where.append(i)
    if not take:
        return (*empty, {**info, "matched": 0, "stale": stale})
    vec = tbl.column("vector").combine_chunks().take(pa.array(take, pa.int64()))
    lens = np.diff(vec.offsets.to_numpy())
    dim = int(lens[0])
    if not (lens == dim).all():
        raise ValueError("dense vectors of different dimensions")
    mat = np.asarray(vec.flatten().to_numpy(zero_copy_only=False), dtype=np.float32).reshape(-1, dim)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.where(norms > 0, norms, 1.0)
    order = np.argsort(np.asarray(where), kind="stable")
    return (np.ascontiguousarray(mat[order]), np.asarray(where, dtype=np.int64)[order],
            {**info, "matched": len(where), "stale": stale, "dimension": dim})


# ------------------------------------------------------------------------------------------------ build

CLUSTERS_SCHEMA = pa.schema([
    ("cluster_id", pa.string()), ("kind", pa.string()), ("n_members", pa.int32()), ("n_sources", pa.int32()),
    ("n_works", pa.int32()), ("source_ids", pa.list_(pa.string())), ("work_ids", pa.list_(pa.string())),
    ("primary_source_id", pa.string()), ("primary_work_id", pa.string()), ("primary_year", pa.int16()),
    ("primary_rule", pa.string()), ("reference_source_id", pa.string()), ("n_edges", pa.int32()),
    ("n_edges_lsh", pa.int32()), ("n_edges_knn", pa.int32()), ("min_edge_containment", pa.float64()),
    ("max_edge_jaccard", pa.float64()), ("template_share", pa.float64()), ("abstract_share", pa.float64()),
    ("rule_version", pa.string()),
])
MEMBERS_SCHEMA = pa.schema([
    ("cluster_id", pa.string()), ("unit_id", pa.string()), ("unit_kind", pa.string()), ("kind", pa.string()),
    ("source_id", pa.string()), ("work_id", pa.string()), ("year", pa.int16()), ("page_id", pa.string()),
    ("page_index", pa.int32()), ("block_ids", pa.list_(pa.string())), ("n_shingles", pa.int32()),
    ("shared_chars", pa.int32()), ("similarity", pa.float64()), ("containment", pa.float64()),
    ("cosine", pa.float64()), ("is_primary", pa.bool_()), ("is_reference", pa.bool_()), ("rule_version", pa.string()),
])
OVERLAP_SCHEMA = pa.schema([
    ("source_a", pa.string()), ("source_b", pa.string()), ("work_a", pa.string()), ("work_b", pa.string()),
    ("same_work", pa.bool_()), ("n_shared_units", pa.int32()), ("n_shared_content", pa.int32()),
    ("n_units_a", pa.int32()), ("n_units_b", pa.int32()), ("units_total_a", pa.int32()),
    ("units_total_b", pa.int32()), ("share_of_a", pa.float64()), ("share_of_b", pa.float64()),
    ("year_a", pa.int16()), ("year_b", pa.int16()), ("relation", pa.string()), ("rule_version", pa.string()),
])


def _backend(choice: str) -> Any:
    if choice == "cpu":
        return np
    cp = _gpu_modules()
    if cp is None:
        if choice == "gpu":
            raise RuntimeError("backend='gpu' but CuPy / a CUDA device is not available")
        log.info("duplicates: CuPy / a CUDA device is not available, numpy backend")
        return np
    return cp


def _cos(mat: np.ndarray, i: np.ndarray, j: np.ndarray) -> np.ndarray:
    if not len(i):
        return np.zeros(0, dtype=np.float64)
    return np.einsum("ij,ij->i", mat[i].astype(np.float64), mat[j].astype(np.float64))


def _shared_text(members: list[int], units: list[dict[str, Any]], kset: Any, k: int, min_share: float
                 ) -> dict[int, tuple[int, int, bool]]:
    """Per member: (shared characters, of them in template sentences, abstract marker in the shared text)."""
    by_src: dict[str, list[int]] = defaultdict(list)
    for v in members:
        by_src[units[v]["source_id"]].append(v)
    sents: list[tuple[int, str]] = [(v, s) for v in members for s in split_sentences(units[v]["text"])]
    skeys, sstarts, scounts = shingle_keys([norm_key(s) for _, s in sents], k, np)
    other: dict[str, np.ndarray] = {}
    for s in by_src:
        rest = [kset(v) for v in members if units[v]["source_id"] != s]
        other[s] = np.unique(np.concatenate(rest)) if rest else np.zeros(0, dtype=np.uint64)
    out = {v: [0, 0, False] for v in members}
    for i, (v, sent) in enumerate(sents):
        c = int(scounts[i])
        if not c:
            continue
        ks = skeys[sstarts[i]:sstarts[i] + c]
        if np.isin(ks, other[units[v]["source_id"]], assume_unique=True).sum() < min_share * c:
            continue
        rec = out[v]
        rec[0] += len(sent)
        if is_template(sent):
            rec[1] += len(sent)
        if has_abstract_marker(sent):
            rec[2] = True
    return {v: (a, b, c) for v, (a, b, c) in out.items()}


def build(con: Any, *, vectors: str | os.PathLike | None = None, stats: dict[str, Any] | None = None,
          **options: Any) -> dict[str, pa.Table]:
    """``dup_clusters``, ``dup_members`` and ``source_overlap`` of the snapshot behind ``con`` (DuckDB).

    ``vectors`` — a directory of dense unit vectors (Parquet parts of the embedding store) for the kNN channel;
    without it only MinHash-LSH proposes candidates. Options and defaults: ``DEFAULTS``; unknown keyword arguments
    (the common arguments of ``vkm-corpus nav build``) are ignored. ``stats`` receives counts, parameters, model
    choices and timings for the manifest.
    """
    p = {k: options.get(k, v) for k, v in DEFAULTS.items()}
    st = stats if stats is not None else {}
    t_all = time.monotonic()
    timings: dict[str, float] = {}

    def lap(name: str, t0: float) -> float:
        timings[name] = round(time.monotonic() - t0, 3)
        return time.monotonic()

    xp = _backend(p["backend"])
    gpu = xp is not np
    pool = None
    if gpu:
        pool = xp.get_default_memory_pool()
        if p["gpu_mem_limit_gb"]:
            pool.set_limit(size=int(float(p["gpu_mem_limit_gb"]) * (1 << 30)))
    peak = [0]

    def mark() -> None:
        if pool is not None:
            peak[0] = max(peak[0], int(pool.total_bytes()))

    k = int(p["shingle_k"])
    t = time.monotonic()
    units_all = load_units(con)
    sources = load_sources(con)
    t = lap("load_units", t)

    norms_all = [norm_key(u["text"]) for u in units_all]
    min_len = max(int(p["min_norm_chars"]), k)
    keep = [i for i, u in enumerate(units_all) if len(norms_all[i]) >= min_len
            and u["reference_share"] < float(p["max_reference_share"])]
    units = [units_all[i] for i in keep]
    norms = [norms_all[i] for i in keep]
    n = len(units)
    src_names = sorted({u["source_id"] for u in units})
    src_code = {s: i for i, s in enumerate(src_names)}
    src = np.array([src_code[u["source_id"]] for u in units], dtype=np.int64)
    t = lap("normalise", t)

    keys, starts, counts = shingle_keys(norms, k, xp)
    mark()
    t = lap("shingles", t)

    channels = set(p["channels"] or ())
    lsh: set[tuple[int, int]] = set()
    if "lsh" in channels and n > 1:
        seeds = np.random.default_rng(int(p["lsh_seed"])).integers(0, 2 ** 63, size=int(p["minhash_perms"]),
                                                                   dtype=np.uint64)
        sig = minhash(keys, starts, counts, seeds, xp)
        mark()
        t = lap("minhash", t)
        lsh = lsh_pairs(sig, src, int(p["lsh_rows"]), int(p["lsh_max_bucket"]))
        t = lap("lsh", t)

    vec_info: dict[str, Any] = {"used": False}
    mat = np.zeros((0, 0), dtype=np.float32)
    vpos = np.full(n, -1, dtype=np.int64)
    knn_pairs: set[tuple[int, int]] = set()
    if vectors is not None and n:
        mat, where, vec_info = load_vectors(vectors, [u["unit_id"] for u in units], [u["text"] for u in units])
        vec_info["used"] = True
        vpos[where] = np.arange(len(where))
        t = lap("load_vectors", t)
        if "knn" in channels and len(where) > 1:
            nn = knn(mat, int(p["knn_k"]), xp)
            mark()
            ii = np.repeat(np.arange(len(where)), nn.shape[1])
            jj = nn.reshape(-1)
            ok = jj >= 0
            ii, jj = ii[ok], jj[ok]
            ua, ub = where[ii], where[jj]
            cross = src[ua] != src[ub]
            ii, jj, ua, ub = ii[cross], jj[cross], ua[cross], ub[cross]
            good = _cos(mat, ii, jj) >= float(p["knn_min_cosine"])
            knn_pairs = {(int(min(x, y)), int(max(x, y))) for x, y in zip(ua[good], ub[good])}
            t = lap("knn", t)

    cand = sorted(lsh | knn_pairs)
    ca = np.array([c[0] for c in cand], dtype=np.int64)
    cb = np.array([c[1] for c in cand], dtype=np.int64)
    inter = intersections(keys, starts, counts, ca, cb, xp)
    mark()
    cnt = np.asarray(counts.get() if gpu else counts)
    la, lb = cnt[ca], cnt[cb]
    small = np.minimum(la, lb)
    union = la + lb - inter
    contain = np.divide(inter, small, out=np.zeros(len(ca)), where=small > 0)
    jacc = np.divide(inter, union, out=np.zeros(len(ca)), where=union > 0)
    ok = (contain >= float(p["min_containment"])) & (inter >= int(p["min_shared_grams"]))
    ea, eb, e_cont, e_jac = ca[ok], cb[ok], contain[ok], jacc[ok]
    e_lsh = np.array([(int(x), int(y)) in lsh for x, y in zip(ea, eb)], dtype=bool)
    e_knn = np.array([(int(x), int(y)) in knn_pairs for x, y in zip(ea, eb)], dtype=bool)
    t = lap("verify", t)

    label = components(n, ea, eb, gpu=gpu)
    t = lap("components", t)
    keys_h = np.asarray(keys.get() if gpu else keys)
    starts_h = np.asarray(starts.get() if gpu else starts)
    if gpu:
        mark()
        del keys, starts, counts
        pool.free_all_blocks()

    def kset(v: int) -> np.ndarray:
        return keys_h[starts_h[v]:starts_h[v] + cnt[v]]

    # ---- clusters: shared text, kinds, primary
    members_of: dict[int, list[int]] = defaultdict(list)
    for v in sorted(set(ea.tolist()) | set(eb.tolist())):
        members_of[int(label[v])].append(v)
    edges_of: dict[int, list[int]] = defaultdict(list)
    for e, v in enumerate(ea.tolist()):
        edges_of[int(label[v])].append(e)

    def work(s: str) -> str:
        return sources.get(s, {}).get("work_id") or f"(source){s}"

    def year(s: str | None) -> int | None:
        return sources.get(s, {}).get("year") if s else None

    def rel_pos(v: int) -> float:
        s = sources.get(units[v]["source_id"], {})
        first, last = s.get("first", 0), s.get("last", 0)
        return round((units[v]["page_index"] - first) / max(1, last - first), 4)

    def copy_key(s: str) -> tuple:
        info = sources.get(s, {})
        return (LINK_RANK.get(info.get("link_type"), 3), info.get("anchor") != s, s)

    shared_of: dict[int, tuple[int, int, bool]] = {}
    clusters = []
    for root, vs in members_of.items():
        sh = _shared_text(vs, units, kset, k, float(p["shared_sentence_min"]))
        shared_of.update(sh)
        tot = sum(a for a, _, _ in sh.values())
        tshare = sum(b for _, b, _ in sh.values()) / tot if tot else 0.0
        ashare = sum(1 for _, _, c in sh.values() if c) / len(vs)
        clusters.append({"root": root, "members": vs, "sources": sorted({units[v]["source_id"] for v in vs}),
                         "template": tshare >= float(p["template_cluster_share"]), "template_share": round(tshare, 4),
                         "abstract_share": round(ashare, 4)})
    shared, shared_content = Counter(), Counter()
    for c in clusters:
        for s, u in combinations(c["sources"], 2):
            shared[(s, u)] += 1
            if not c["template"]:
                shared_content[(s, u)] += 1

    for c in clusters:
        ss, vs = c["sources"], c["members"]
        by_work: dict[str, list[str]] = defaultdict(list)
        for s in ss:
            by_work[work(s)].append(s)
        c["works"] = sorted(by_work)
        cross = [(s, u) for s, u in combinations(ss, 2) if work(s) != work(u)]
        substantial = any(shared_content[x] >= int(p["reprint_min_shared"]) for x in cross)
        if len(by_work) == 1:
            kind = "SAME_WORK_COPY"
        elif c["template"] and (len(by_work) >= int(p["boilerplate_min_works"]) or not substantial):
            kind = "BOILERPLATE"
        elif substantial:
            kind = "REPRINT"
        elif c["abstract_share"] >= float(p["abstract_cluster_share"]):
            kind = "SHARED_ABSTRACT"
        else:
            kind = "PARTIAL_OVERLAP"
        c["kind"] = kind

        def work_key(w: str) -> tuple:
            y = year(by_work[w][0])
            wt = sources.get(by_work[w][0], {}).get("work_type")
            pos = min(rel_pos(v) for v in vs if work(units[v]["source_id"]) == w)
            return (y is None, y or 0, 1 if wt in SECONDARY_WORK_TYPES else 0, pos, w)

        works_ordered = sorted(by_work, key=work_key)
        copies = sorted(by_work[works_ordered[0]], key=copy_key)
        reference = copies[0]
        if kind == "SAME_WORK_COPY":
            k0, k1 = copy_key(copies[0]), copy_key(copies[1])
            rule = ("SAME_WORK_FULL_COPY" if k0[0] != k1[0] else "SAME_WORK_ANCHOR" if k0[1] != k1[1]
                    else "SAME_WORK_SOURCE_ID")
            primary = reference
        elif kind == "BOILERPLATE":
            primary, rule = None, "NOT_APPLICABLE"
        elif any(year(s) is None for s in ss):
            primary, rule = None, "UNKNOWN_YEAR"
        else:
            k0, k1 = work_key(works_ordered[0]), work_key(works_ordered[1])
            primary = reference
            rule = ("EARLIEST_YEAR" if k0[1] != k1[1] else "TIE_WORK_TYPE" if k0[2] != k1[2]
                    else "TIE_POSITION" if k0[3] != k1[3] else "TIE_WORK_ID")
        c["primary"], c["rule"], c["reference"] = primary, rule, reference
        c["cluster_id"] = nav_ids.dup_cluster_id(units[v]["unit_id"] for v in vs)

    # ---- member overlap with the reference source (exact shingle sets; cosines in float64 on the CPU)
    rows_m: dict[str, list] = {f.name: [] for f in MEMBERS_SCHEMA}
    rows_c: dict[str, list] = {f.name: [] for f in CLUSTERS_SCHEMA}
    for c in sorted(clusters, key=lambda c: c["cluster_id"]):
        vs = c["members"]
        ref_units = [v for v in vs if units[v]["source_id"] == c["reference"]]
        ref = np.unique(np.concatenate([kset(v) for v in ref_units]))
        ref_vec = np.array([int(vpos[v]) for v in ref_units if vpos[v] >= 0], dtype=np.int64)
        for v in sorted(vs, key=lambda v: (units[v]["source_id"], units[v]["page_index"], units[v]["unit_id"])):
            ks = kset(v)
            it = int(np.intersect1d(ks, ref, assume_unique=True).size)
            un = len(ks) + len(ref) - it
            cos = None
            if vpos[v] >= 0 and len(ref_vec):
                cos = round(float(_cos(mat, np.full(len(ref_vec), vpos[v]), ref_vec).max()), 6)
            s = units[v]["source_id"]
            rows_m["cluster_id"].append(c["cluster_id"])
            rows_m["unit_id"].append(units[v]["unit_id"])
            rows_m["unit_kind"].append(UNIT_KIND)
            rows_m["kind"].append(c["kind"])
            rows_m["source_id"].append(s)
            rows_m["work_id"].append(sources.get(s, {}).get("work_id"))
            rows_m["year"].append(year(s))
            rows_m["page_id"].append(units[v]["page_id"])
            rows_m["page_index"].append(units[v]["page_index"])
            rows_m["block_ids"].append(units[v]["block_ids"])
            rows_m["n_shingles"].append(int(len(ks)))
            rows_m["shared_chars"].append(int(shared_of[v][0]))
            rows_m["similarity"].append(round(it / un, 6) if un else 0.0)
            rows_m["containment"].append(round(it / len(ks), 6) if len(ks) else 0.0)
            rows_m["cosine"].append(cos)
            rows_m["is_primary"].append(s == c["primary"])
            rows_m["is_reference"].append(s == c["reference"])
            rows_m["rule_version"].append(RULE_VERSION)
        es = edges_of[c["root"]]
        rows_c["cluster_id"].append(c["cluster_id"])
        rows_c["kind"].append(c["kind"])
        rows_c["n_members"].append(len(vs))
        rows_c["n_sources"].append(len(c["sources"]))
        rows_c["n_works"].append(len(c["works"]))
        rows_c["source_ids"].append(c["sources"])
        rows_c["work_ids"].append([w for w in c["works"] if not w.startswith("(source)")])
        rows_c["primary_source_id"].append(c["primary"])
        rows_c["primary_work_id"].append(sources.get(c["primary"], {}).get("work_id") if c["primary"] else None)
        rows_c["primary_year"].append(year(c["primary"]))
        rows_c["primary_rule"].append(c["rule"])
        rows_c["reference_source_id"].append(c["reference"])
        rows_c["n_edges"].append(len(es))
        rows_c["n_edges_lsh"].append(int(e_lsh[es].sum()))
        rows_c["n_edges_knn"].append(int(e_knn[es].sum()))
        rows_c["min_edge_containment"].append(round(float(e_cont[es].min()), 6))
        rows_c["max_edge_jaccard"].append(round(float(e_jac[es].max()), 6))
        rows_c["template_share"].append(c["template_share"])
        rows_c["abstract_share"].append(c["abstract_share"])
        rows_c["rule_version"].append(RULE_VERSION)
    t = lap("clusters", t)

    # ---- source pairs
    units_per_source = Counter(u["source_id"] for u in units)
    pair_units: dict[tuple[str, str], tuple[set, set]] = defaultdict(lambda: (set(), set()))
    pair_kinds: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for c in clusters:
        by_src: dict[str, set] = defaultdict(set)
        for v in c["members"]:
            by_src[units[v]["source_id"]].add(v)
        for s, u in combinations(c["sources"], 2):
            pair_units[(s, u)][0].update(by_src[s])
            pair_units[(s, u)][1].update(by_src[u])
            pair_kinds[(s, u)][c["kind"]] += 1
    rows_o: dict[str, list] = {f.name: [] for f in OVERLAP_SCHEMA}
    for (s, u) in sorted(pair_units):
        na, nb = len(pair_units[(s, u)][0]), len(pair_units[(s, u)][1])
        ta, tb = units_per_source[s], units_per_source[u]
        kinds = pair_kinds[(s, u)]
        rows_o["source_a"].append(s)
        rows_o["source_b"].append(u)
        rows_o["work_a"].append(sources.get(s, {}).get("work_id"))
        rows_o["work_b"].append(sources.get(u, {}).get("work_id"))
        rows_o["same_work"].append(work(s) == work(u))
        rows_o["n_shared_units"].append(shared[(s, u)])
        rows_o["n_shared_content"].append(shared_content[(s, u)])
        rows_o["n_units_a"].append(na)
        rows_o["n_units_b"].append(nb)
        rows_o["units_total_a"].append(ta)
        rows_o["units_total_b"].append(tb)
        rows_o["share_of_a"].append(round(na / ta, 6) if ta else 0.0)
        rows_o["share_of_b"].append(round(nb / tb, 6) if tb else 0.0)
        rows_o["year_a"].append(year(s))
        rows_o["year_b"].append(year(u))
        rows_o["relation"].append(sorted(kinds, key=lambda x: (-kinds[x], KINDS.index(x)))[0])
        rows_o["rule_version"].append(RULE_VERSION)
    t = lap("source_overlap", t)

    dup_clusters = pa.table(rows_c, schema=CLUSTERS_SCHEMA)
    dup_members = pa.table(rows_m, schema=MEMBERS_SCHEMA)
    source_overlap = pa.table(rows_o, schema=OVERLAP_SCHEMA)
    timings["total"] = round(time.monotonic() - t_all, 3)
    kinds_c = Counter(rows_c["kind"])
    kinds_m = Counter(rows_m["kind"])
    st.update({
        "rule_version": RULE_VERSION, "unit_rule": "vkm-units-v1/" + UNIT_KIND,
        "backend": "gpu" if gpu else "cpu", "gpu_pool_peak_mb": round(peak[0] / (1 << 20), 1) if gpu else None,
        "layer_status": "DERIVED", "review_status": "AUTO_EXTRACTED_UNREVIEWED",
        "params": {kk: (sorted(v) if isinstance(v, (tuple, set, list)) else v) for kk, v in p.items()},
        "model_choices": {**{kk: "MODEL_CHOICE" for kk in DEFAULTS if kk not in ("backend", "gpu_mem_limit_gb")},
                          **MODEL_CHOICES},
        "secondary_work_types": sorted(SECONDARY_WORK_TYPES),
        "vectors": vec_info,
        "counts": {
            "units": len(units_all), "units_kept": n,
            "units_short": sum(1 for i in range(len(units_all)) if len(norms_all[i]) < min_len),
            "units_reference_list": sum(1 for i, u in enumerate(units_all) if len(norms_all[i]) >= min_len
                                        and u["reference_share"] >= float(p["max_reference_share"])),
            "sources_with_units": len(src_names), "shingles": int(cnt.sum()),
            "candidates_lsh": len(lsh), "candidates_knn": len(knn_pairs), "candidates": len(cand),
            "verified_pairs": int(ok.sum()), "verified_lsh_only": int((e_lsh & ~e_knn).sum()),
            "verified_knn_only": int((e_knn & ~e_lsh).sum()), "verified_both": int((e_lsh & e_knn).sum()),
            "clusters": dup_clusters.num_rows, "members": dup_members.num_rows,
            "clusters_by_kind": {kd: kinds_c.get(kd, 0) for kd in KINDS},
            "members_by_kind": {kd: kinds_m.get(kd, 0) for kd in KINDS},
            "primary_rules": dict(sorted(Counter(rows_c["primary_rule"]).items())),
            "source_pairs": source_overlap.num_rows,
            "source_pairs_by_relation": dict(sorted(Counter(rows_o["relation"]).items())),
        },
        "timings_s": timings,
    })
    return {"dup_clusters": dup_clusters, "dup_members": dup_members, "source_overlap": source_overlap}


def copy_block_ids(dup_members: Any, *, min_containment: float = 0.8) -> set[str]:
    """Blocks of passages that repeat text counted elsewhere: members outside the reference source of their cluster
    whose shingles are >= ``min_containment`` inside it. Dropping them from co-occurrence statistics keeps one instance
    of each duplicated passage (the reference: the primary source, or the first source by the same ordering when the
    primary is UNKNOWN or not applicable)."""
    if dup_members is None:
        return set()
    if isinstance(dup_members, pa.Table):
        tbl = dup_members
    elif hasattr(dup_members, "to_arrow_table"):
        tbl = dup_members.to_arrow_table()
    else:
        tbl = pa.Table.from_pylist(list(dup_members))
    out: set[str] = set()
    for ref, cont, blocks in zip(tbl.column("is_reference").to_pylist(), tbl.column("containment").to_pylist(),
                                 tbl.column("block_ids").to_pylist()):
        if not ref and cont is not None and cont >= min_containment:
            out.update(blocks or ())
    return out
