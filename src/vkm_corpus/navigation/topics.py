"""Topics of the navigation layer NAV (``docs/corpus_platform/NAVIGATION_LAYER.md`` §7) — a RAPTOR-style topic tree
across sources, built without an LLM, plus the per-section aggregates of §2.

``build(con, *, sections, section_pages, terms=None, term_mentions=None, vectors=None, stats=None, **options)``
returns five Arrow tables (``None`` when there are no vectors or no sections — the part is then skipped):

* ``section_aggregates`` — per section of ``section_pages``: pages, units, central units (ids: unit, page, canonical
  objects), key terms, counts of formulas / figures / tables / bibliography entries;
* ``section_vectors`` — the L2-normalised mean of the unit vectors of a section (``n_units`` > 0 only);
* ``topics`` — nested topics, level 1 = fine … 3 = coarse: parent, sizes, sources, label terms, central sections,
  coherence (mean pairwise cosine of the member vectors);
* ``topic_members`` — topic ↔ section (every level) with the cosine to the topic centroid and the rank;
* ``topic_edges`` — neighbouring topics of one level: centroid cosine and the kNN links between their sections.

**Units and vectors.** Units are the embedding units of rule ``vkm-units-v1`` (``retrieval_lab.units``) recomputed
from the canon behind ``con``. A stored vector (``vectors`` = a directory of ``part-*.parquet`` of one embedding
config) is used only when its unit exists in this snapshot, its text hash equals the unit text (context variant A)
and its ``embedding_signature`` is consistent — vectors left over from older snapshots are dropped and counted.
``BIB_ENTRY`` units and text units made mostly of reference lists or tables of contents (``APPARATUS_BLOCKS``) never
enter a section vector. A unit belongs to the deepest section of its page (``section_pages``); on a page shared by
several sections, text units go to the section whose heading precedes most of their characters in reading order,
formulas / figures / tables by the block just above them; without anchors the unit counts for every section.

**Tree.** Level 1: exact cosine kNN graph of the section vectors → Leiden (modularity with a resolution) → topics
below a minimum size join their best-linked neighbour. Levels 2 and 3 repeat this over the centroids of the level
below (RAPTOR clusters summaries of the level below; here centroids stand in for summaries), so the levels are
nested by construction. Labels: c-TF-IDF of the N3 ``term_mentions`` of the members (a label must occur in ≥ 2 member
sections); without N3, word counts of the section texts (fallback, no lemmatiser). kNN, centroids and Leiden run on
the GPU (CuPy, cuGraph) when importable, otherwise numpy and a deterministic Louvain. Vectors are rounded to a dyadic
grid, so every sum is exact and order-free: both backends are bit-reproducible and give identical section vectors
and aggregates; the topics differ between Leiden (GPU) and Louvain (CPU) — the backend is in ``stats``, with the
parameters and the reasons of every MODEL_CHOICE (``MODEL_CHOICES``). Everything is DERIVED navigation
(``AUTO_EXTRACTED_UNREVIEWED``): a topic says «these sections read alike», never a physical claim.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pyarrow as pa

from vkm_corpus.navigation import ids as nav_ids

log = logging.getLogger(__name__)

RULE_VERSION = nav_ids.RULE_VERSIONS["topics"]
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
UNIT_KINDS: tuple[str, ...] = ("BLOCK_GROUP", "FIGURE", "TABLE", "FORMULA")   # BIB_ENTRY never
APPARATUS_BLOCKS: frozenset[str] = frozenset({"REFERENCE_LIST", "TABLE_OF_CONTENTS"})
N_LEVELS = 3
VECTOR_COLUMNS = ("object_id", "source_id", "page_id", "object_type", "text_hash", "model_id", "model_revision",
                  "config_hash", "embedding_signature", "dimension", "created_at", "vector")

DEFAULTS: dict[str, Any] = {
    # formula units are ~58 % of the units and repeat the text around them: half weight (tuned on the 28.09 canon)
    "kind_weights": {"BLOCK_GROUP": 1.0, "FIGURE": 1.0, "TABLE": 1.0, "FORMULA": 0.5},
    "max_apparatus_share": 0.5,      # a text unit with >= 50 % of its characters in reference lists / TOCs is dropped
    "check_text_hash": True,         # a vector must embed the current unit text (context variant A)
    "n_central_units": 5,
    "n_key_terms": 10,
    "language_centering": True,      # topic space: section vectors minus the mean vector of their language
    "min_language_sections": 20,     # a language with fewer sections is centred by the mean of all sections
    "knn_k": (16, 10, 8),            # exact cosine kNN per level (level 1: sections, 2–3: topic centroids)
    "knn_same_source": None,         # None: plain kNN; m: level-1 neighbours = k − m of other sources + m of the same
    "weight_power": 1.0,             # edge weight = cosine ** power (cosine <= 0 dropped)
    "resolution": (90.0, 20.0, 6.0),  # Leiden / Louvain modularity resolution per level (tuned on the 28.09 canon)
    "upper_graph": "centroid_knn",   # levels 2–3: kNN of the topic centroids | "aggregate": summed section links
    "min_topic_size": (3, 2, 2),     # members (level 1) or children (levels 2–3) below this are merged
    "n_labels": 8,
    "n_central_sections": 5,
    "max_central_per_source": 2,
    "edge_top_k": 8,                 # topic_edges: up to this many centroid neighbours per topic ...
    "edge_min_cosine": 0.6,          # ... with at least this cosine (in the topic space) ...
    "edge_min_k": 3,                 # ... but the nearest edge_min_k (cosine > 0) are always kept
    "seed": 42,
    "backend": "auto",               # "auto" | "gpu" | "cpu"
}

# MODEL_CHOICE records written to the stats of the part (manifest); values are those of ``DEFAULTS``
MODEL_CHOICES: dict[str, str] = {
    "unit_vectors": "a vector is used only if its unit (vkm-units-v1) is in this snapshot, its text hash equals the "
                    "unit text (context variant A) and its embedding_signature is consistent",
    "excluded_units": "BIB_ENTRY units; text units with >= max_apparatus_share of their characters in REFERENCE_LIST / "
                      "TABLE_OF_CONTENTS blocks; lone headings on such pages",
    "section_vector": "L2-normalised weighted mean of the unit vectors of the section's own pages (section_pages); "
                      "kind_weights: FORMULA 0.5 (58 % of the units, they repeat the text around them; best "
                      "term coherence per granularity on the 28.09 canon)",
    "shared_pages": "a page of several sections is split at the headings of the sections starting on it: text units by "
                    "the majority of their characters, formulas/figures/tables/bibliography entries by the block "
                    "just above them; without an anchor the unit counts for every section of the page",
    "topic_space": "the tree is built on the section vectors minus the mean vector of their language (ru / en by "
                   "Cyrillic vs Latin letters of the units, >= 2:1; else und), renormalised: RU→EN neighbours ×4, "
                   "mixed-language level-1 topics ×2.3 and higher same-language term coherence at every level at equal "
                   "granularity (28.09 canon); section_vectors stay the plain means",
    "graph": "exact cosine kNN, knn_k per level, plain (no quota of other sources: quotas lowered the term "
             "coherence of the topics, also across sources, on the 28.09 canon)",
    "communities": "GPU: cuGraph Leiden (float64 dyadic weights, random_state=seed — repeatable); CPU: deterministic "
                   "Louvain; modularity resolution per level; smaller than min_topic_size → merged into the "
                   "best-linked neighbour",
    "upper_levels": "levels 2–3: kNN graph of the topic centroids of the level below (RAPTOR analogue), nested by "
                    "construction",
    "labels": "c-TF-IDF of N3 term tf over the members × (0.5 + 0.5·share of members with the term); a label occurs "
              "in >= 2 member sections; nested terms skipped; fallback: word unigrams/bigrams of the text units",
    "central": "central units: closest to the section centroid; central sections: closest to the topic centroid, at "
               "most max_central_per_source per source",
    "topic_edges": "a topic's edge_min_k nearest centroids, plus up to edge_top_k with cosine >= edge_min_cosine "
                   "(topic space); n_links = level-1 kNN "
                   "links between their sections",
    "numerics": "unit/section vectors on a 2^-24 grid, kNN weights on a 2^-20 grid: float64 sums exact in any order",
}

# fallback labels (no N3): stop words of a naive tokenizer (lower case, ё → е); no lemmatiser, words of >= 3 letters
STOP_RU = frozenset("""
без более больше будет будут бы был была были было быть вам вас ваш весь во вот все всего всех вы где
да даже для до его ее ей если есть еще же за здесь из или им их как какой когда кто ли либо между меня мне
может можно мы на над надо наш не него нее нет ни них но ну об однако он она они оно от очень по под после
при про раз рис рисунок так также такой там те тем то того тоже той только том тот ту ты уже чем что чтобы эта
эти это этого этой этом этот эту является являются который которая которое которые которых котором которой
которым которого табл таблица стр см т.е др пр гл глава раздел данные данных случае случай результате результаты
результат этапе этих этим этими своих свой своей свои себя себе сам сама само сами каждый каждая каждое любой
весьма наиболее менее рядом около вместе через поэтому потому тогда затем кроме вследствие согласно
""".split())
STOP_EN = frozenset("""
the and for are but not you all any can had her was one our out has have been were will with this that from they
them their there these those than then into onto upon about above after again against also because before being below
between both does doing during each few further here how its itself just more most other over same should some such
very what when where which while who whom why would your yours also fig figure table tab eq equation section chapter
et al see shown given used using use based following however thus hence may might must shall
""".split())
_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_CYR = re.compile(r"[а-яёА-ЯЁ]+")
_LAT = re.compile(r"[a-zA-Z]+")


# ============================================================================================ helpers
VEC_BITS = 24      # unit and section vectors are rounded to multiples of 2^-24 (exact in float32) ...
WEIGHT_BITS = 20   # ... and kNN edge weights to multiples of 2^-20, so float64 sums are exact in any order:
#                    CPU and GPU give bit-identical centroids and cuGraph Leiden (float64 weights) repeats itself


def dyadic(x: np.ndarray, bits: int) -> np.ndarray:
    """``x`` rounded to multiples of ``2**-bits`` (same dtype)."""
    s = float(2 ** bits)
    return (np.round(np.asarray(x, dtype=np.float64) * s) / s).astype(np.asarray(x).dtype, copy=False)


def _now() -> float:
    return time.perf_counter()


def _ensure_cuda_path() -> None:
    """CuPy needs the CUDA headers: in a conda RAPIDS env they are under ``<env>/targets/x86_64-linux``."""
    if os.environ.get("CUDA_PATH"):
        return
    for base in {sys.prefix, sys.base_prefix}:
        cand = os.path.join(base, "targets", "x86_64-linux")
        if os.path.exists(os.path.join(cand, "include", "cuda_runtime.h")):
            os.environ["CUDA_PATH"] = cand
            return


def gpu_available() -> bool:
    _ensure_cuda_path()
    try:
        import cugraph  # noqa: F401, PLC0415
        import cupy  # noqa: PLC0415

        return cupy.cuda.runtime.getDeviceCount() > 0
    except Exception:  # noqa: BLE001 — no RAPIDS, no driver, no device
        return False


def _as_table(data: Any, con: Any = None) -> pa.Table | None:
    """An Arrow table from a table, a DuckDB relation, a table name in ``con`` or ``None``."""
    if data is None:
        return None
    if isinstance(data, pa.Table):
        return data
    if isinstance(data, str) and con is not None:
        return con.execute(f'SELECT * FROM "{data}"').to_arrow_table()
    if hasattr(data, "to_arrow_table"):
        return data.to_arrow_table()
    if hasattr(data, "arrow"):
        return data.arrow()
    raise TypeError(f"unsupported table input {type(data).__name__}")


def _normalise_rows(x: np.ndarray) -> np.ndarray:
    """Rows scaled to unit length (float64 on the CPU), rounded to the dyadic grid of ``VEC_BITS``, as float32."""
    x = np.asarray(x, dtype=np.float64)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return dyadic((x / n).astype(np.float32), VEC_BITS)


def letters(text: str) -> tuple[int, int]:
    """(Cyrillic, Latin) letter counts of a text."""
    t = text or ""
    return len(t) - len(_CYR.sub("", t)), len(t) - len(_LAT.sub("", t))


def language_of(cyr: int, lat: int) -> str:
    """ru / en by a 2:1 majority of Cyrillic or Latin letters, else und."""
    if cyr >= 2 * lat and cyr > 0:
        return "ru"
    if lat >= 2 * cyr and lat > 0:
        return "en"
    return "und"


def center_by_language(x: np.ndarray, langs: np.ndarray, min_sections: int) -> tuple[np.ndarray, dict[str, int]]:
    """Rows minus the mean row of their language (languages with < ``min_sections`` rows, and «und», use the mean
    of all rows), renormalised onto the dyadic grid. Means are float64 on the CPU (deterministic)."""
    x64 = np.asarray(x, dtype=np.float64)
    out = x64 - x64.mean(axis=0) if len(x64) else x64.copy()
    used: dict[str, int] = {}
    for lg in sorted(set(langs.tolist())):
        m = langs == lg
        if lg != "und" and int(m.sum()) >= min_sections:
            out[m] = x64[m] - x64[m].mean(axis=0)
            used[lg] = int(m.sum())
    return _normalise_rows(out), used


def _nested(a: str, b: str) -> bool:
    return f" {a} " in f" {b} " or f" {b} " in f" {a} "


def _diversify(items: Iterable[tuple[str, str]], n: int) -> list[tuple[str, str]]:
    """First ``n`` (id, key) items, skipping a key nested in (or containing) an already chosen key."""
    out: list[tuple[str, str]] = []
    for tid, key in items:
        if any(_nested(key, k) for _, k in out):
            continue
        out.append((tid, key))
        if len(out) >= n:
            break
    return out


# ============================================================================================ vectors
def read_vectors(path: str | Path) -> dict[str, Any]:
    """Unit vectors of one embedding config: ``<path>/part-*.parquet`` (+ ``config.json``).

    Returns ids, kinds, pages, sources, text hashes, the float32 matrix and ``info`` (config signature, model,
    dimension, row counts, rows dropped for an inconsistent ``embedding_signature`` or a duplicate id)."""
    import pyarrow.compute as pc  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    from vkm_corpus.embeddings.signature import embedding_signature  # noqa: PLC0415

    d = Path(path)
    files = sorted(d.glob("part-*.parquet"))
    if not files:
        raise FileNotFoundError(f"no part-*.parquet under {d.name}")
    config: dict[str, Any] = {}
    if (d / "config.json").is_file():
        config = json.loads((d / "config.json").read_text(encoding="utf-8"))
    tables = []
    for f in files:
        t = pq.read_table(f)
        cols = [c for c in VECTOR_COLUMNS if c in t.column_names]
        tables.append(t.select(cols))
    t = pa.concat_tables(tables, promote_options="default")
    info: dict[str, Any] = {"n_parts": len(files), "rows": t.num_rows, "dyadic_bits": VEC_BITS}
    for col in ("config_hash", "model_id", "model_revision", "dimension"):
        vals = sorted({v for v in t.column(col).to_pylist()}, key=str) if col in t.column_names else []
        if len(vals) != 1:
            raise ValueError(f"vectors mix {len(vals)} values of {col}: one embedding config per directory")
        info[col] = vals[0]
    sig = info["config_hash"]
    info["config_signature"] = config.get("config_signature")
    if info["config_signature"] not in (None, sig):
        raise ValueError("config.json signature differs from the config_hash of the vectors")
    cfg = config.get("config") or {}
    info["text_rule"] = cfg.get("text_rule")
    info["normalization"] = cfg.get("normalization")
    dim = int(info["dimension"])
    col = t.column("vector").combine_chunks()
    lengths = pc.list_value_length(col).to_numpy(zero_copy_only=False)
    ids = np.asarray(t.column("object_id").to_pylist(), dtype=object)
    hashes = np.asarray(t.column("text_hash").to_pylist(), dtype=object)
    esig = t.column("embedding_signature").to_pylist()
    ok = lengths == dim
    bad_sig = 0
    for i in range(len(ids)):
        if ok[i] and esig[i] != embedding_signature(ids[i], hashes[i], sig):
            ok[i] = False
            bad_sig += 1
    info["bad_dimension"] = int((lengths != dim).sum())
    info["bad_embedding_signature"] = bad_sig
    # duplicates (a re-embedded unit): keep the newest row
    created = t.column("created_at").to_pylist() if "created_at" in t.column_names else [None] * len(ids)
    best: dict[str, int] = {}
    for i in np.flatnonzero(ok):
        oid = ids[i]
        j = best.get(oid)
        if j is None or (created[i] is not None and (created[j] is None or created[i] > created[j])):
            best[oid] = int(i)
    keep = np.array(sorted(best.values()), dtype=np.int64)
    info["duplicates_dropped"] = int(ok.sum() - len(keep))
    flat = col.values.to_numpy(zero_copy_only=False).astype(np.float32, copy=False)
    offsets = col.offsets.to_numpy()
    starts = offsets[:-1][keep]
    if len(keep) and np.array_equal(offsets[:-1], offsets[0] + dim * np.arange(len(offsets) - 1)):
        mat = flat[offsets[0]:offsets[0] + dim * (len(offsets) - 1)].reshape(-1, dim)[keep]
    else:
        mat = np.empty((len(keep), dim), dtype=np.float32)
        for r, s in enumerate(starts):
            mat[r] = flat[s:s + dim]
    return {
        "ids": ids[keep], "kinds": np.asarray(t.column("object_type").to_pylist(), dtype=object)[keep],
        "pages": np.asarray(t.column("page_id").to_pylist(), dtype=object)[keep],
        "sources": np.asarray(t.column("source_id").to_pylist(), dtype=object)[keep],
        "hashes": hashes[keep], "matrix": _normalise_rows(mat), "info": info,
    }


# ============================================================================================ canon units
def canon_units(con: Any) -> dict[str, Any]:
    """Units of rule ``vkm-units-v1`` recomputed from the canon + block and object positions."""
    from vkm_corpus.retrieval_lab.canon import CanonReader  # noqa: PLC0415
    from vkm_corpus.retrieval_lab.units import UNIT_RULE, build_units  # noqa: PLC0415

    reader = CanonReader(con, "nav")
    rows: dict[str, list[dict[str, Any]]] = {}
    for name in ("pages", "blocks", "figures", "tables", "formulas", "bibliography"):
        try:
            rows[name] = getattr(reader, name)()
        except Exception:  # noqa: BLE001 — a canon without that table (tests, partial copies)
            if name in ("pages", "blocks"):
                raise
            rows[name] = []
    units = build_units(rows["pages"], rows["blocks"], rows["figures"], rows["tables"], rows["formulas"],
                        rows["bibliography"])
    blocks: dict[str, tuple] = {}
    page_blocks: dict[str, list[tuple[float, int]]] = defaultdict(list)   # page -> (bottom y, reading order)
    page_chars: Counter = Counter()
    page_app: Counter = Counter()
    for b in rows["blocks"]:
        if b.get("is_primary_layer") is False:
            continue
        ro = b.get("reading_order")
        y0, y1 = b.get("bbox_y0"), b.get("bbox_y1")
        btype, n = str(b.get("block_type")), len(b.get("normalized_text") or "")
        blocks[str(b["object_id"])] = (b["page_id"], ro, btype, n, y0, y1)
        page_chars[b["page_id"]] += n
        if btype in APPARATUS_BLOCKS:
            page_app[b["page_id"]] += n
        if ro is not None and y1 is not None:
            page_blocks[b["page_id"]].append((float(y1), int(ro)))
    for v in page_blocks.values():
        v.sort()
    page_apparatus = {p: page_app[p] / page_chars[p] for p in page_app if page_chars[p]}
    objects: dict[str, dict[str, tuple]] = {}
    for kind, name in (("FORMULA", "formulas"), ("FIGURE", "figures"), ("TABLE", "tables"),
                       ("BIB_ENTRY", "bibliography")):
        objects[kind] = {str(r["object_id"]): (r["page_id"], r.get("bbox_y0"), r.get("source_id"))
                         for r in rows[name]}
    return {"units": units, "blocks": blocks, "page_blocks": dict(page_blocks), "objects": objects,
            "page_apparatus": page_apparatus, "unit_rule": UNIT_RULE, "n_pages": len(rows["pages"])}


# ============================================================================================ units → sections
class _PageSplit:
    """Placement of the objects of one page shared by several sections (see the module docstring)."""

    def __init__(self, page_id: str, cands: list[int], heading_block: Sequence[str | None],
                 blocks: Mapping[str, tuple], page_blocks: Mapping[str, list[tuple[float, int]]]) -> None:
        anchors = []
        for s in cands:
            hb = heading_block[s]
            info = blocks.get(hb) if hb else None
            if info is not None and info[0] == page_id and info[1] is not None:
                anchors.append((int(info[1]), s))
        anchors.sort()
        self.cands = cands
        self.anchor_ro = [a for a, _ in anchors]
        self.anchor_sec = [s for _, s in anchors]
        anchored = set(self.anchor_sec)
        self.pre = [s for s in cands if s not in anchored] or self.anchor_sec[:1]
        self.bottoms = page_blocks.get(page_id, [])
        self.bottom_y = [y for y, _ in self.bottoms]

    def _seg(self, ro: int) -> int:
        return bisect_right(self.anchor_ro, ro) - 1

    def _targets(self, seg: int) -> list[int]:
        return self.pre if seg < 0 else [self.anchor_sec[seg]]

    def text_unit(self, object_ids: Sequence[str], blocks: Mapping[str, tuple]) -> tuple[list[int], bool]:
        if not self.anchor_ro:
            return self.cands, False
        weight: Counter = Counter()
        for oid in object_ids:
            info = blocks.get(oid)
            if info is None or info[1] is None:
                continue
            weight[self._seg(int(info[1]))] += max(1, info[3])
        if not weight:
            return self.cands, False
        seg = max(weight.items(), key=lambda kv: (kv[1], kv[0]))[0]   # most characters; tie → the later section
        return self._targets(seg), True

    def positioned(self, y0: float | None) -> tuple[list[int], bool]:
        """An object (formula, figure, table, bibliography entry) by the block just above its top edge."""
        if not self.anchor_ro or y0 is None:
            return self.cands, False
        i = bisect_right(self.bottom_y, float(y0) + 1.0) - 1
        if i < 0:
            return self._targets(-1), True
        ro = max(r for y, r in self.bottoms[: i + 1] if y >= self.bottoms[i][0] - 1e-9)
        return self._targets(self._seg(ro)), True


def assign_units(unit_rows: Sequence[tuple[str, str, Sequence[str]]], page_secs: Mapping[str, list[int]],
                 heading_block: Sequence[str | None], canon: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray,
                                                                                         dict[str, int]]:
    """(unit index, section index) pairs for units given as (kind, page_id, object_ids)."""
    blocks, page_blocks, objects = canon["blocks"], canon["page_blocks"], canon["objects"]
    splits: dict[str, _PageSplit] = {}
    au: list[int] = []
    asec: list[int] = []
    st = Counter({"outside_sections": 0, "on_shared_pages": 0, "placed_by_heading": 0, "shared_unplaced": 0})
    for ui, (kind, page_id, oids) in enumerate(unit_rows):
        cands = page_secs.get(page_id)
        if not cands:
            st["outside_sections"] += 1
            continue
        if len(cands) == 1:
            au.append(ui)
            asec.append(cands[0])
            continue
        sp = splits.get(page_id)
        if sp is None:
            sp = splits[page_id] = _PageSplit(page_id, cands, heading_block, blocks, page_blocks)
        if kind == "BLOCK_GROUP":
            targets, placed = sp.text_unit(oids, blocks)
        else:
            obj = objects.get(kind, {}).get(oids[0]) if oids else None
            targets, placed = sp.positioned(obj[1] if obj else None)
        st["on_shared_pages"] += 1
        st["placed_by_heading" if placed else "shared_unplaced"] += 1
        for s in targets:
            au.append(ui)
            asec.append(s)
    st["shared_pages"] = len(splits)
    return np.asarray(au, dtype=np.int64), np.asarray(asec, dtype=np.int64), dict(st)


def assign_objects(canon: Mapping[str, Any], page_secs: Mapping[str, list[int]], heading_block: Sequence[str | None],
                   n_sections: int) -> dict[str, np.ndarray]:
    """Counts of formulas / figures / tables / bibliography entries per section (shared pages split as units)."""
    splits: dict[str, _PageSplit] = {}
    out: dict[str, np.ndarray] = {}
    for kind, key in (("FORMULA", "n_formulas"), ("FIGURE", "n_figures"), ("TABLE", "n_tables"),
                      ("BIB_ENTRY", "n_bib_entries")):
        counts = np.zeros(n_sections, dtype=np.int64)
        for page_id, y0, _src in canon["objects"].get(kind, {}).values():
            cands = page_secs.get(page_id)
            if not cands:
                continue
            if len(cands) == 1:
                counts[cands[0]] += 1
                continue
            sp = splits.get(page_id)
            if sp is None:
                sp = splits[page_id] = _PageSplit(page_id, cands, heading_block, canon["blocks"],
                                                  canon["page_blocks"])
            for s in sp.positioned(y0)[0]:
                counts[s] += 1
        out[key] = counts
    return out


# ============================================================================================ backends
class Backend:
    """Linear algebra, kNN and communities: numpy + deterministic Louvain (CPU) or CuPy + cuGraph Leiden (GPU).

    Sums run over dyadic-rounded values in float64 (exact, order-free), so both backends are deterministic;
    they differ only by the community algorithm (and float32 kNN ties)."""

    def __init__(self, kind: str = "auto") -> None:
        if kind == "auto":
            kind = "gpu" if gpu_available() else "cpu"
        if kind == "gpu" and not gpu_available():
            raise RuntimeError("backend='gpu' but CuPy/cuGraph or a CUDA device is not available")
        self.kind = kind
        self.base_used = self.peak_used = 0
        if kind == "gpu":
            import cupy  # noqa: PLC0415

            self.xp = cupy
            self.base_used = self.peak_used = self._device_used()
        else:
            self.xp = np

    # ------------------------------------------------------------------ memory
    def _device_used(self) -> int:
        free, total = self.xp.cuda.runtime.memGetInfo()
        return int(total - free)

    def _mark(self) -> None:
        if self.kind == "gpu":
            self.xp.cuda.Device().synchronize()
            self.peak_used = max(self.peak_used, self._device_used())

    @property
    def peak_mib(self) -> float | None:
        """Peak device memory above the level at start (all processes of the device, pools included)."""
        return round((self.peak_used - self.base_used) / 2 ** 20, 1) if self.kind == "gpu" else None

    def to_numpy(self, x: Any) -> np.ndarray:
        return x.get() if self.kind == "gpu" else np.asarray(x)

    def free(self) -> None:
        if self.kind == "gpu":
            self._mark()
            self.xp.get_default_memory_pool().free_all_blocks()

    # ------------------------------------------------------------------ algebra
    def segment_sum(self, mat: np.ndarray, rows: np.ndarray, seg: np.ndarray, weights: np.ndarray,
                    n_seg: int) -> np.ndarray:
        """``out[s] = Σ weights[i] · mat[rows[i]]`` over ``seg[i] == s`` in float64. ``mat`` must hold dyadic
        values (:func:`dyadic`, ``VEC_BITS``) and ``weights`` dyadic numbers: the sums are then exact."""
        d = mat.shape[1]
        out = np.zeros((n_seg, d), dtype=np.float64)
        if len(rows) == 0:
            return out
        if self.kind == "gpu":
            xp = self.xp
            g = xp.zeros((n_seg, d), dtype=xp.float64)
            m = xp.asarray(mat)
            for lo in range(0, len(rows), 32768):
                r = xp.asarray(rows[lo:lo + 32768])
                w = xp.asarray(weights[lo:lo + 32768], dtype=xp.float64)[:, None]
                xp.add.at(g, xp.asarray(seg[lo:lo + 32768]), m[r].astype(xp.float64) * w)   # exact: dyadic
            self._mark()
            out = g.get()
            del m, g
            self.free()
            return out
        order = np.argsort(seg, kind="stable")
        s_sorted = seg[order]
        starts = np.r_[0, np.flatnonzero(np.diff(s_sorted)) + 1]
        for lo in range(0, len(starts), 4096):             # bounded temporaries
            st = starts[lo:lo + 4096]
            end = starts[lo + 4096] if lo + 4096 < len(starts) else len(order)
            idx = order[st[0]:end]
            block = mat[rows[idx]].astype(np.float64) * np.asarray(weights, np.float64)[idx, None]
            out[s_sorted[st]] = np.add.reduceat(block, st - st[0], axis=0)
        return out

    def row_dots(self, a: np.ndarray, ia: np.ndarray, b: np.ndarray, ib: np.ndarray) -> np.ndarray:
        """``Σ_k a[ia[i], k] · b[ib[i], k]`` for every i, in float64, rounded to multiples of 2^-30 (the backends
        then agree except on a value within 1e-16 of a rounding boundary)."""
        out = np.empty(len(ia), dtype=np.float64)
        if self.kind == "gpu":
            xp = self.xp
            ga, gb = xp.asarray(a), xp.asarray(b)
            for lo in range(0, len(ia), 32768):
                x = ga[xp.asarray(ia[lo:lo + 32768])].astype(xp.float64)
                y = gb[xp.asarray(ib[lo:lo + 32768])].astype(xp.float64)
                out[lo:lo + len(x)] = (x * y).sum(axis=1).get()
            self._mark()
            del ga, gb
            self.free()
            return dyadic(out, 30)
        for lo in range(0, len(ia), 32768):
            out[lo:lo + 32768] = np.einsum("ij,ij->i", a[ia[lo:lo + 32768]].astype(np.float64),
                                           b[ib[lo:lo + 32768]].astype(np.float64))
        return dyadic(out, 30)

    def knn(self, x: np.ndarray, groups: np.ndarray, k_other: int, k_same: int) -> tuple[np.ndarray, np.ndarray,
                                                                                        np.ndarray]:
        """Exact cosine kNN of unit rows: ``k_other`` neighbours with another group label and ``k_same`` with the
        same one (self excluded). Returns (src, dst, cosine) with src ≠ dst; ties go to the lower index."""
        n = x.shape[0]
        xp = self.xp
        gx = xp.asarray(x, dtype=xp.float32)
        gg = xp.asarray(groups)
        src_l, dst_l, sim_l = [], [], []
        block = max(1, min(n, 4096 if self.kind == "gpu" else 2048))
        for lo in range(0, n, block):
            hi = min(n, lo + block)
            sims = gx[lo:hi] @ gx.T                                  # (b, n)
            rows = xp.arange(lo, hi)
            same = gg[lo:hi, None] == gg[None, :]
            sims[xp.arange(hi - lo), rows] = -xp.inf                  # no self loops
            for k, mask_same in ((k_other, False), (k_same, True)):
                kk = min(k, n - 1)
                if kk <= 0:
                    continue
                s = sims.copy()
                s[same if not mask_same else ~same] = -xp.inf
                # deterministic top-k: sort by (-sim, index)
                part = xp.argsort(-s, axis=1, kind="stable")[:, :kk] if self.kind == "cpu" else \
                    xp.argsort(-s, axis=1)[:, :kk]
                vals = xp.take_along_axis(s, part, axis=1)
                ok = xp.isfinite(vals)
                r = xp.broadcast_to(rows[:, None], part.shape)
                src_l.append(self.to_numpy(r[ok]))
                dst_l.append(self.to_numpy(part[ok]))
                sim_l.append(self.to_numpy(vals[ok]).astype(np.float64))
            self._mark()
        del gx, gg
        self.free()
        if not src_l:
            return np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0)
        return (np.concatenate(src_l).astype(np.int64), np.concatenate(dst_l).astype(np.int64),
                np.concatenate(sim_l))

    def communities(self, n: int, src: np.ndarray, dst: np.ndarray, w: np.ndarray, *, resolution: float,
                    seed: int) -> np.ndarray:
        """Community of each node (0..c-1): cuGraph Leiden on the GPU, deterministic Louvain on the CPU."""
        if n == 0:
            return np.zeros(0, dtype=np.int64)
        if len(src) == 0:
            return np.arange(n, dtype=np.int64)
        if self.kind == "gpu":
            import cudf  # noqa: PLC0415
            import cugraph  # noqa: PLC0415

            g = cugraph.Graph(directed=False)
            g.from_cudf_edgelist(cudf.DataFrame({"src": src.astype(np.int32), "dst": dst.astype(np.int32),
                                                 "w": w.astype(np.float64)}),   # float64: repeatable
                                 source="src", destination="dst", edge_attr="w", renumber=False)
            parts, _ = cugraph.leiden(g, max_iter=100, resolution=float(resolution), random_state=int(seed))
            parts = parts.to_pandas()
            labels = np.arange(n, dtype=np.int64) + n          # isolated vertices keep their own community
            labels[parts["vertex"].to_numpy().astype(np.int64)] = parts["partition"].to_numpy().astype(np.int64)
            self._mark()
            return _renumber(labels)
        return louvain(n, src, dst, w, resolution=resolution)


def _renumber(labels: np.ndarray) -> np.ndarray:
    """Communities renumbered 0..c-1 by (size desc, smallest member)."""
    uniq, inv = np.unique(labels, return_inverse=True)
    sizes = np.bincount(inv)
    first = np.full(len(uniq), len(labels), dtype=np.int64)
    np.minimum.at(first, inv, np.arange(len(labels)))
    order = np.lexsort((first, -sizes))
    rank = np.empty(len(uniq), dtype=np.int64)
    rank[order] = np.arange(len(uniq))
    return rank[inv]


def _undirected(n: int, src: np.ndarray, dst: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray,
                                                                                    np.ndarray]:
    """Symmetric edge list a < b with the maximum weight of the two directions."""
    a = np.minimum(src, dst)
    b = np.maximum(src, dst)
    keep = a != b
    a, b, w = a[keep], b[keep], w[keep]
    if len(a) == 0:
        return a, b, w
    key = a * n + b
    order = np.lexsort((-w, key))
    key, a, b, w = key[order], a[order], b[order], w[order]
    first = np.r_[True, key[1:] != key[:-1]]
    return a[first], b[first], w[first]


def louvain(n: int, src: np.ndarray, dst: np.ndarray, w: np.ndarray, *, resolution: float = 1.0,
            max_passes: int = 100, tol: float = 1e-10) -> np.ndarray:
    """Deterministic Louvain (modularity with ``resolution``) on an undirected weighted graph — the CPU fallback of
    cuGraph Leiden. Nodes are visited in index order; ties keep the current community, then the lowest id."""
    a, b, ww = _undirected(n, np.asarray(src, np.int64), np.asarray(dst, np.int64), np.asarray(w, np.float64))
    membership = np.arange(n, dtype=np.int64)
    loops = np.zeros(n, dtype=np.float64)
    cn = n
    while True:
        deg = np.bincount(a, ww, cn) + np.bincount(b, ww, cn) + 2 * loops
        m2 = deg.sum()
        if m2 <= 0:
            break
        u = np.r_[a, b]
        v = np.r_[b, a]
        uw = np.r_[ww, ww]
        order = np.argsort(u, kind="stable")
        v, uw = v[order], uw[order]
        indptr = np.r_[0, np.cumsum(np.bincount(u, minlength=cn))]
        nbr = [v[indptr[i]:indptr[i + 1]].tolist() for i in range(cn)]
        nw = [uw[indptr[i]:indptr[i + 1]].tolist() for i in range(cn)]
        comm = list(range(cn))
        tot = deg.tolist()
        k = deg.tolist()
        moved_any = False
        for _ in range(max_passes):
            moved = False
            for i in range(cn):
                ci = comm[i]
                links: dict[int, float] = {}
                for j, wij in zip(nbr[i], nw[i]):
                    cj = comm[j]
                    links[cj] = links.get(cj, 0.0) + wij
                tot[ci] -= k[i]
                best = ci
                best_gain = links.get(ci, 0.0) - resolution * tot[ci] * k[i] / m2
                for c in sorted(links):
                    gain = links[c] - resolution * tot[c] * k[i] / m2
                    if gain > best_gain + tol:
                        best, best_gain = c, gain
                tot[best] += k[i]
                if best != ci:
                    comm[i] = best
                    moved = True
            if not moved:
                break
            moved_any = True
        if not moved_any:
            break
        carr = _renumber(np.asarray(comm, dtype=np.int64))
        membership = carr[membership]
        nc = int(carr.max()) + 1
        ca, cb = carr[a], carr[b]
        inner = ca == cb
        loops = np.bincount(carr, loops, nc) + np.bincount(ca[inner], ww[inner], nc)
        x, y = np.minimum(ca[~inner], cb[~inner]), np.maximum(ca[~inner], cb[~inner])
        key = x * nc + y
        uk, inv = np.unique(key, return_inverse=True)
        a, b, ww = uk // nc, uk % nc, np.bincount(inv, ww[~inner])
        cn = nc
    return _renumber(membership)


def merge_small(labels: np.ndarray, src: np.ndarray, dst: np.ndarray, w: np.ndarray, min_size: int) -> np.ndarray:
    """Communities smaller than ``min_size`` join the community they are linked to most strongly (by total edge
    weight; ties: the larger, then the lower id). Unlinked small communities stay."""
    labels = labels.copy()
    if min_size <= 1 or len(labels) == 0:
        return labels
    while True:
        sizes = np.bincount(labels)
        small = [int(c) for c in np.lexsort((np.arange(len(sizes)), sizes)) if 0 < sizes[c] < min_size]
        changed = False
        for c in small:
            sizes = np.bincount(labels, minlength=len(sizes))
            if sizes[c] == 0 or sizes[c] >= min_size:
                continue
            ls, ld = labels[src], labels[dst]
            out_a = (ls == c) & (ld != c)
            out_b = (ld == c) & (ls != c)
            tgt = np.r_[ld[out_a], ls[out_b]]
            if len(tgt) == 0:
                continue
            tw = np.bincount(tgt, np.r_[w[out_a], w[out_b]], len(sizes))
            best = max(np.flatnonzero(tw > 0), key=lambda t: (tw[t], sizes[t], -t))
            labels[labels == c] = best
            changed = True
        if not changed:
            break
    return _renumber(labels)


# ============================================================================================ terms
def _section_terms_n3(terms: pa.Table, term_mentions: pa.Table, sec_index: Mapping[str, int]) -> pa.Table:
    """(sec, term_id, key, lemma, tf, tfidf) summed over the N3 windows of each section."""
    import duckdb  # noqa: PLC0415

    db = duckdb.connect()
    try:
        db.register("tm_in", term_mentions.select(["term_id", "section_id", "tf", "tfidf"]))
        db.register("t_in", terms.select(["term_id", "lemma", "lemma_key"]))
        db.register("s_in", pa.table({"section_id": list(sec_index), "sec": list(sec_index.values())}))
        return db.execute("""
            SELECT s.sec, m.term_id, t.lemma_key AS key, t.lemma, m.tf, m.tfidf
            FROM (SELECT section_id, term_id, sum(tf)::BIGINT AS tf, sum(tfidf) AS tfidf FROM tm_in
                  WHERE section_id IS NOT NULL GROUP BY 1, 2) m
            JOIN s_in s USING (section_id) JOIN t_in t USING (term_id)
            ORDER BY s.sec, m.term_id""").to_arrow_table()
    finally:
        db.close()


def _stem(word: str) -> str:
    """Crude grouping key of a word (no lemmatiser): Russian and English endings stripped, at least 4 letters kept."""
    for suf in ("ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "иях", "ях", "ах", "ов", "ев", "ей", "ий",
                "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ом", "ем", "ам", "ям", "ию", "ия",
                "ть", "ы", "и", "а", "я", "о", "е", "у", "ю", "ь", "ies", "es", "s"):
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


def _section_terms_fallback(texts: Mapping[int, list[str]], n_sections: int) -> pa.Table:
    """Word unigrams / bigrams of the text units of each section → (sec, term_id, key, lemma, tf, tfidf)."""
    tf: dict[tuple[int, str], int] = Counter()
    surface: dict[str, Counter] = defaultdict(Counter)
    for sec, chunks in texts.items():
        for text in chunks:
            words = _WORD.findall((text or "").lower().replace("ё", "е"))
            keys: list[str | None] = []
            for wd in words:
                if wd in STOP_RU or wd in STOP_EN:
                    keys.append(None)
                    continue
                key = _stem(wd)
                surface[key][wd] += 1
                keys.append(key)
                tf[(sec, key)] += 1
            for i in range(len(words) - 1):
                if keys[i] and keys[i + 1]:
                    key = f"{keys[i]} {keys[i + 1]}"
                    surface[key][f"{words[i]} {words[i + 1]}"] += 1
                    tf[(sec, key)] += 1
    df = Counter(key for _, key in tf)
    n = max(1, n_sections)
    rows: dict[str, list] = {"sec": [], "term_id": [], "key": [], "lemma": [], "tf": [], "tfidf": []}
    for (sec, key), c in sorted(tf.items()):
        if df[key] < 2 and n > 1:
            continue
        rows["sec"].append(sec)
        rows["term_id"].append("WRD-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16])
        rows["key"].append(key)
        rows["lemma"].append(min(surface[key].items(), key=lambda kv: (-kv[1], kv[0]))[0])
        rows["tf"].append(c)
        rows["tfidf"].append(c * math.log(n / df[key]))
    return pa.table({"sec": pa.array(rows["sec"], pa.int64()), "term_id": pa.array(rows["term_id"], pa.string()),
                     "key": pa.array(rows["key"], pa.string()), "lemma": pa.array(rows["lemma"], pa.string()),
                     "tf": pa.array(rows["tf"], pa.int64()), "tfidf": pa.array(rows["tfidf"], pa.float64())})


def key_terms(st: pa.Table, n_sections: int, n: int) -> list[list[tuple[str, str, str]]]:
    """Top ``n`` (term_id, key, lemma) of every section by tf-idf (ties: tf, id), nested keys skipped."""
    import duckdb  # noqa: PLC0415

    out: list[list[tuple[str, str, str]]] = [[] for _ in range(n_sections)]
    if st.num_rows == 0 or n <= 0:
        return out
    db = duckdb.connect()
    try:
        db.register("st_in", st.select(["sec", "term_id", "key", "lemma", "tf", "tfidf"]))
        rows = db.execute("""
            SELECT sec, term_id, key, lemma FROM st_in
            QUALIFY row_number() OVER (PARTITION BY sec ORDER BY tfidf DESC, tf DESC, term_id) <= $lim
            ORDER BY sec, tfidf DESC, tf DESC, term_id""", {"lim": 4 * n}).fetchall()
    finally:
        db.close()
    by_sec: dict[int, list[tuple[str, str]]] = defaultdict(list)
    lemma: dict[str, str] = {}
    for sec, tid, key, lem in rows:
        by_sec[int(sec)].append((tid, key))
        lemma[tid] = lem
    for sec, cand in by_sec.items():
        out[sec] = [(t, k, lemma[t]) for t, k in _diversify(cand, n)]
    return out


def topic_labels(st: pa.Table, member_sec: np.ndarray, member_topic: np.ndarray, n_topics: int,
                 n: int) -> list[list[tuple[str, str, str]]]:
    """c-TF-IDF labels: ``tf_{t,c}/Σ_t tf_{t,c} · ln(1 + A/f_t) · (0.5 + 0.5·share of member sections with t)``; a
    label must occur in ≥ 2 member sections of a topic with ≥ 2 sections."""
    import duckdb  # noqa: PLC0415

    out: list[list[tuple[str, str, str]]] = [[] for _ in range(n_topics)]
    if st.num_rows == 0 or len(member_sec) == 0:
        return out
    db = duckdb.connect()
    try:
        db.register("st_in", st.select(["sec", "term_id", "key", "lemma", "tf"]))
        db.register("mb_in", pa.table({"sec": pa.array(member_sec, pa.int64()),
                                       "topic": pa.array(member_topic, pa.int64())}))
        rows = db.execute("""
            WITH sz AS (SELECT topic, count(*) AS n FROM mb_in GROUP BY 1),
                 tt AS (SELECT mb.topic, st.term_id, any_value(st.key) AS key, any_value(st.lemma) AS lemma,
                               sum(st.tf) AS tf, count(DISTINCT mb.sec) AS n_sec
                        FROM st_in st JOIN mb_in mb USING (sec) GROUP BY 1, 2),
                 tot AS (SELECT topic, sum(tf) AS total FROM tt GROUP BY 1),
                 ft AS (SELECT term_id, sum(tf) AS f FROM tt GROUP BY 1),
                 a AS (SELECT avg(total) AS avg_total FROM tot),
                 sc AS (SELECT tt.topic, tt.term_id, tt.key, tt.lemma,
                               tt.tf / tot.total * ln(1 + a.avg_total / ft.f) * (0.5 + 0.5 * tt.n_sec / sz.n) AS score
                        FROM tt JOIN tot USING (topic) JOIN ft USING (term_id) JOIN sz USING (topic), a
                        WHERE tt.n_sec >= least(2, sz.n))
            SELECT topic, term_id, key, lemma FROM sc
            QUALIFY row_number() OVER (PARTITION BY topic ORDER BY score DESC, term_id) <= $lim
            ORDER BY topic, score DESC, term_id""", {"lim": 4 * n}).fetchall()
    finally:
        db.close()
    by_topic: dict[int, list[tuple[str, str]]] = defaultdict(list)
    lemma: dict[str, str] = {}
    for topic, tid, key, lem in rows:
        by_topic[int(topic)].append((tid, key))
        lemma[tid] = lem
    for topic, cand in by_topic.items():
        out[topic] = [(t, k, lemma[t]) for t, k in _diversify(cand, n)]
    return out


# ============================================================================================ tree
def _level_graph(backend: Backend, x: np.ndarray, groups: np.ndarray, k_other: int, k_same: int,
                 power: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    s, d, sim = backend.knn(x, groups, k_other, k_same)
    keep = sim > 0
    s, d, sim = s[keep], d[keep], dyadic(sim[keep], WEIGHT_BITS)
    a, b, cos = _undirected(x.shape[0], s, d, sim)
    return a, b, (dyadic(cos ** power, WEIGHT_BITS) if power != 1.0 else cos)


def build_tree(backend: Backend, x: np.ndarray, sources: np.ndarray, p: Mapping[str, Any],
               timings: dict[str, float]) -> list[dict[str, Any]]:
    """Nested levels over the section vectors ``x`` (unit rows). Each level: ``topic`` of each node of the level
    below, ``sec_topic`` of each section, the graph of the level and the Leiden/Louvain parameters."""
    levels: list[dict[str, Any]] = []
    node_vec = x
    same = p.get("knn_same_source")
    node_groups = sources if same is not None else np.arange(x.shape[0])
    sec_node = np.arange(x.shape[0], dtype=np.int64)       # node of each section at the current level
    for li in range(N_LEVELS):
        t0 = _now()
        n_nodes = node_vec.shape[0]
        k = int(p["knn_k"][li])
        k_same = int(same) if (same is not None and li == 0) else 0
        if li > 0 and p.get("upper_graph", "centroid_knn") == "aggregate":
            pa_, pb, pw = levels[-1]["edges"]              # the graph of the level below, summed per topic pair
            prev = levels[-1]["topic"]
            ta, tb = prev[pa_], prev[pb]
            keep = ta != tb
            key = np.minimum(ta[keep], tb[keep]) * n_nodes + np.maximum(ta[keep], tb[keep])
            uk, inv = np.unique(key, return_inverse=True)
            size = np.bincount(prev, minlength=n_nodes).astype(np.float64)
            a, b = uk // n_nodes, uk % n_nodes
            w = np.bincount(inv, pw[keep]) / np.sqrt(size[a] * size[b])   # links per geometric mean of the sizes
        else:
            a, b, w = _level_graph(backend, node_vec, node_groups, k - k_same, k_same, float(p["weight_power"]))
        timings[f"level{li + 1}_knn"] = _now() - t0
        t0 = _now()
        raw = backend.communities(n_nodes, a, b, w, resolution=float(p["resolution"][li]), seed=int(p["seed"]))
        n_raw = int(raw.max()) + 1 if len(raw) else 0
        topic = merge_small(raw, a, b, w, int(p["min_topic_size"][li]))
        timings[f"level{li + 1}_communities"] = _now() - t0
        n_topics = int(topic.max()) + 1 if len(topic) else 0
        sec_topic = topic[sec_node]
        # centroids of the sections of each topic (equal weight per section) → the nodes of the next level
        cent = backend.segment_sum(x, np.arange(x.shape[0]), sec_topic, np.ones(x.shape[0], np.float32), n_topics)
        cent = _normalise_rows(cent)
        levels.append({"level": li + 1, "topic": topic, "sec_topic": sec_topic, "n_nodes": n_nodes,
                       "edges": (a, b, w), "n_raw": n_raw, "n_topics": n_topics, "centroids": cent})
        node_vec = cent
        node_groups = np.arange(n_topics)            # every topic is its own group: no same-group neighbours
        sec_node = sec_topic
        if n_topics <= 1:
            break
    return levels


# ============================================================================================ tables
SECTION_AGGREGATES_SCHEMA = pa.schema([
    ("section_id", pa.string()), ("source_id", pa.string()), ("language", pa.string()), ("n_pages", pa.int32()),
    ("n_units", pa.int32()),
    ("n_text_units", pa.int32()), ("central_unit_ids", pa.list_(pa.string())),
    ("central_page_ids", pa.list_(pa.string())), ("central_object_ids", pa.list_(pa.list_(pa.string()))),
    ("key_terms", pa.list_(pa.string())), ("key_term_ids", pa.list_(pa.string())),
    ("n_formulas", pa.int32()), ("n_figures", pa.int32()), ("n_tables", pa.int32()), ("n_bib_entries", pa.int32()),
    ("rule_version", pa.string()),
])
TOPICS_SCHEMA = pa.schema([
    ("topic_id", pa.string()), ("level", pa.int16()), ("parent_topic_id", pa.string()), ("n_children", pa.int32()),
    ("n_sections", pa.int32()), ("n_sources", pa.int32()), ("source_ids", pa.list_(pa.string())),
    ("label_terms", pa.list_(pa.string())), ("label_term_ids", pa.list_(pa.string())),
    ("central_section_ids", pa.list_(pa.string())), ("coherence", pa.float64()), ("rule_version", pa.string()),
])
TOPIC_MEMBERS_SCHEMA = pa.schema([
    ("topic_id", pa.string()), ("section_id", pa.string()), ("level", pa.int16()), ("source_id", pa.string()),
    ("similarity", pa.float64()), ("rank", pa.int32()), ("rule_version", pa.string()),
])
TOPIC_EDGES_SCHEMA = pa.schema([
    ("topic_id_a", pa.string()), ("topic_id_b", pa.string()), ("level", pa.int16()), ("cosine", pa.float64()),
    ("n_links", pa.int32()), ("rule_version", pa.string()),
])


SECTION_VECTORS_SCHEMA = pa.schema([
    ("section_id", pa.string()), ("source_id", pa.string()), ("n_units", pa.int32()),
    ("vector", pa.list_(pa.float32())), ("rule_version", pa.string()),
])


def _central(order_key: np.ndarray, groups: np.ndarray, n: int) -> dict[int, list[int]]:
    """Positions of the first ``n`` rows of every group in ``order_key`` order (a lexsort result)."""
    out: dict[int, list[int]] = defaultdict(list)
    for i in order_key:
        g = int(groups[i])
        if len(out[g]) < n:
            out[g].append(int(i))
    return out


def _round(x: float) -> float:
    return float(np.floor(x * 1e6 + 0.5) / 1e6)


def build(con: Any, *, sections: Any = None, section_pages: Any = None, terms: Any = None,
          term_mentions: Any = None, vectors: Any = None, stats: dict[str, Any] | None = None,
          **options: Any) -> dict[str, pa.Table] | None:
    """Build the five topic datasets (module docstring). ``vectors`` — a directory of unit vectors (``nav build
    --vectors DIR``) or the dict of :func:`read_vectors`; ``sections`` / ``section_pages`` (N1) are required,
    ``terms`` / ``term_mentions`` (N3) are optional (labels then come from word counts). Options: ``DEFAULTS``;
    unknown keyword arguments (common arguments of ``vkm-corpus nav build``) are ignored."""
    stats = stats if stats is not None else {}
    p = {k: options.get(k, v) for k, v in DEFAULTS.items()}
    stats["rule_version"] = RULE_VERSION
    stats["params"] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.items()}
    sec_t = _as_table(sections, con)
    sp_t = _as_table(section_pages, con)
    if vectors is None or sec_t is None or sp_t is None:
        stats["skipped"] = ("no unit vectors (nav build --vectors DIR)" if vectors is None
                            else "sections / section_pages of N1 are required")
        return None
    timings: dict[str, float] = {}
    t_all = _now()

    # ---- vectors and units
    t0 = _now()
    vec = vectors if isinstance(vectors, Mapping) else read_vectors(vectors)
    timings["read_vectors"] = _now() - t0
    t0 = _now()
    canon = canon_units(con)
    timings["canon_units"] = _now() - t0
    t0 = _now()
    unit_by_id = {u.unit_id: u for u in canon["units"]}
    check_hash = bool(p["check_text_hash"]) and str(vec["info"].get("text_rule") or "vkm-units-v1/A").endswith("/A")
    blocks = canon["blocks"]
    weights_by_kind = dict(p["kind_weights"])
    counts = Counter()
    keep_rows: list[int] = []
    keep_units = []
    for i, oid in enumerate(vec["ids"]):
        u = unit_by_id.get(oid)
        if u is None:
            counts["stale_not_in_snapshot"] += 1
            continue
        if check_hash and u.text_sha256 != vec["hashes"][i]:
            counts["text_hash_mismatch"] += 1
            continue
        if u.kind not in UNIT_KINDS or weights_by_kind.get(u.kind, 0.0) <= 0:
            counts[f"excluded_kind_{u.kind}"] += 1
            continue
        if u.kind == "BLOCK_GROUP":
            tot = app = 0
            only_headings = True
            for b in u.object_ids:
                info = blocks.get(b)
                if info is None:
                    continue
                tot += info[3]
                app += info[3] if info[2] in APPARATUS_BLOCKS else 0
                only_headings = only_headings and info[2] in ("HEADING", "TITLE")
            if tot and app >= float(p["max_apparatus_share"]) * tot:
                counts["apparatus_text_units"] += 1
                continue
            # a lone heading («Список литературы») on a page of reference lists / TOC (its entries are no unit text)
            if only_headings and canon["page_apparatus"].get(u.page_id, 0.0) >= float(p["max_apparatus_share"]):
                counts["apparatus_page_headings"] += 1
                continue
        keep_rows.append(i)
        keep_units.append(u)
    vec_ids = set(vec["ids"].tolist())
    missing = Counter(u.kind for u in canon["units"] if u.unit_id not in vec_ids)
    stats["vectors"] = {**{k: (v if isinstance(v, (int, float, str, type(None))) else str(v))
                           for k, v in vec["info"].items()},
                        "text_hash_checked": check_hash, "used": len(keep_rows), **dict(counts),
                        "units_in_snapshot": len(canon["units"]),
                        "units_without_vector": {k: v for k, v in sorted(missing.items())},
                        "unit_rule": canon["unit_rule"]}
    del vec_ids
    timings["match_vectors"] = _now() - t0

    # ---- sections and assignment
    t0 = _now()
    sp_sections = sorted(set(sp_t.column("section_id").to_pylist()))
    sec_index = {s: i for i, s in enumerate(sp_sections)}
    n_sec = len(sp_sections)
    sec_rows = {r["section_id"]: r for r in sec_t.select(
        [c for c in ("section_id", "source_id", "heading_block_id", "ordinal") if c in sec_t.column_names]
    ).to_pylist()}
    sec_source = [sec_rows.get(s, {}).get("source_id") for s in sp_sections]
    heading = [sec_rows.get(s, {}).get("heading_block_id") for s in sp_sections]
    page_secs: dict[str, list[int]] = defaultdict(list)
    n_pages = np.zeros(n_sec, dtype=np.int64)
    for sid, pid in zip(sp_t.column("section_id").to_pylist(), sp_t.column("page_id").to_pylist()):
        si = sec_index[sid]
        if si not in page_secs[pid]:
            page_secs[pid].append(si)
            n_pages[si] += 1
    for pid in page_secs:
        page_secs[pid].sort(key=lambda si: (sec_rows.get(sp_sections[si], {}).get("ordinal") or 0, si))
    unit_rows = [(u.kind, u.page_id, u.object_ids) for u in keep_units]
    a_unit, a_sec, a_stats = assign_units(unit_rows, page_secs, heading, canon)
    obj_counts = assign_objects(canon, page_secs, heading, n_sec)
    stats["assignment"] = {**a_stats, "sections_in_section_pages": n_sec, "pages": len(page_secs),
                           "assignments": int(len(a_unit))}
    timings["assign"] = _now() - t0

    # ---- section vectors, central units
    backend = Backend(str(p["backend"]))
    stats["backend"] = backend.kind
    t0 = _now()
    mat = vec["matrix"] if vec["info"].get("dyadic_bits") == VEC_BITS else _normalise_rows(vec["matrix"])
    rows = np.asarray(keep_rows, dtype=np.int64)
    u_kind = np.asarray([u.kind for u in keep_units], dtype=object)
    w_unit = np.asarray([weights_by_kind.get(k, 1.0) for k in u_kind], dtype=np.float32)
    sums = backend.segment_sum(mat, rows[a_unit], a_sec, w_unit[a_unit], n_sec)
    n_units = np.bincount(a_sec, minlength=n_sec)
    n_text = np.bincount(a_sec[u_kind[a_unit] == "BLOCK_GROUP"], minlength=n_sec) if len(a_sec) else \
        np.zeros(n_sec, np.int64)
    has_vec = n_units > 0
    cent_all = _normalise_rows(sums)
    sims = backend.row_dots(mat, rows[a_unit], cent_all, a_sec)
    _, u_rank = np.unique(np.asarray([u.unit_id for u in keep_units], dtype=object), return_inverse=True)
    order = np.lexsort((u_rank[a_unit], -sims, a_sec)) if len(a_sec) else np.zeros(0, np.int64)
    central = _central(order, a_sec, int(p["n_central_units"]))
    timings["section_vectors"] = _now() - t0

    # ---- key terms
    t0 = _now()
    tm_t, terms_t = _as_table(term_mentions, con), _as_table(terms, con)
    if tm_t is not None and terms_t is not None and tm_t.num_rows:
        st = _section_terms_n3(terms_t, tm_t, sec_index)
        stats["labels"] = "N3 term_mentions (c-TF-IDF over lemmas)"
    else:
        texts: dict[int, list[str]] = defaultdict(list)
        for ui, si in zip(a_unit.tolist(), a_sec.tolist()):
            if keep_units[ui].kind == "BLOCK_GROUP":
                texts[si].append(keep_units[ui].text)
        st = _section_terms_fallback(texts, n_sec)
        stats["labels"] = "fallback: word counts of the section texts (no N3 term_mentions)"
    kt = key_terms(st, n_sec, int(p["n_key_terms"]))
    timings["key_terms"] = _now() - t0

    # language of each section: Cyrillic vs Latin letters of its text units (all units when it has none)
    cyr = np.zeros(n_sec, np.int64)
    lat = np.zeros(n_sec, np.int64)
    cyr_any = np.zeros(n_sec, np.int64)
    lat_any = np.zeros(n_sec, np.int64)
    ul = [letters(u.text) for u in keep_units]
    for ui, si in zip(a_unit.tolist(), a_sec.tolist()):
        c_, l_ = ul[ui]
        cyr_any[si] += c_
        lat_any[si] += l_
        if keep_units[ui].kind == "BLOCK_GROUP":
            cyr[si] += c_
            lat[si] += l_
    sec_lang = [language_of(int(cyr[i]), int(lat[i])) if cyr[i] + lat[i] else
                language_of(int(cyr_any[i]), int(lat_any[i])) for i in range(n_sec)]

    agg = {f.name: [] for f in SECTION_AGGREGATES_SCHEMA}
    for si, sid in enumerate(sp_sections):
        cu = [keep_units[a_unit[j]] for j in central.get(si, [])]
        agg["section_id"].append(sid)
        agg["source_id"].append(sec_source[si])
        agg["language"].append(sec_lang[si])
        agg["n_pages"].append(int(n_pages[si]))
        agg["n_units"].append(int(n_units[si]))
        agg["n_text_units"].append(int(n_text[si]))
        agg["central_unit_ids"].append([u.unit_id for u in cu])
        agg["central_page_ids"].append([u.page_id for u in cu])
        agg["central_object_ids"].append([list(u.object_ids) for u in cu])
        agg["key_terms"].append([lem for _, _, lem in kt[si]])
        agg["key_term_ids"].append([t for t, _, _ in kt[si]])
        for key in ("n_formulas", "n_figures", "n_tables", "n_bib_entries"):
            agg[key].append(int(obj_counts[key][si]))
        agg["rule_version"].append(RULE_VERSION)
    section_aggregates = pa.table(agg, schema=SECTION_AGGREGATES_SCHEMA)

    # ---- topic tree over the sections with a vector
    vsec = np.flatnonzero(has_vec)
    x = cent_all[vsec]
    x_src = np.asarray([sec_source[i] or "" for i in vsec], dtype=object)
    _, src_codes = np.unique(x_src, return_inverse=True)
    dim = int(mat.shape[1])
    offsets = pa.array(np.arange(len(vsec) + 1, dtype=np.int32) * dim)
    section_vectors = pa.table({
        "section_id": pa.array([sp_sections[i] for i in vsec], pa.string()),
        "source_id": pa.array(list(x_src), pa.string()),
        "n_units": pa.array(n_units[vsec].astype(np.int32)),
        "vector": pa.ListArray.from_arrays(offsets, pa.array(np.ascontiguousarray(x, dtype=np.float32).ravel())),
        "rule_version": pa.array([RULE_VERSION] * len(vsec), pa.string())}, schema=SECTION_VECTORS_SCHEMA)
    t0 = _now()
    x_lang = np.asarray([sec_lang[i] for i in vsec], dtype=object)
    if p["language_centering"] and len(vsec):
        xt, centred = center_by_language(x, x_lang, int(p["min_language_sections"]))
    else:
        xt, centred = x, {}
    stats["topic_space"] = {"language_centering": bool(p["language_centering"]), "centred_languages": centred,
                            "section_languages": dict(sorted(Counter(x_lang.tolist()).items()))}
    levels = build_tree(backend, xt, src_codes, p, timings) if len(vsec) else []
    timings["tree"] = _now() - t0

    # ---- topics, members, edges
    t0 = _now()
    sec_ids_v = [sp_sections[i] for i in vsec]
    topics_rows = {f.name: [] for f in TOPICS_SCHEMA}
    members_rows = {f.name: [] for f in TOPIC_MEMBERS_SCHEMA}
    edges_rows = {f.name: [] for f in TOPIC_EDGES_SCHEMA}
    topic_ids: list[list[str]] = []
    level_stats = []
    for lv in levels:
        members: dict[int, list[int]] = defaultdict(list)
        for i, t in enumerate(lv["sec_topic"].tolist()):
            members[t].append(i)
        ids = [nav_ids.topic_id(lv["level"], RULE_VERSION, [sec_ids_v[i] for i in members[t]])
               for t in range(lv["n_topics"])]
        topic_ids.append(ids)
    for li, lv in enumerate(levels):
        cent = lv["centroids"]
        sec_topic = lv["sec_topic"]
        sim = backend.row_dots(xt, np.arange(len(vsec)), cent, sec_topic)
        labels = topic_labels(st, vsec, sec_topic, lv["n_topics"], int(p["n_labels"]))
        order = np.lexsort((np.arange(len(vsec)), -sim, sec_topic))   # ids are sorted: index = id order
        by_topic: dict[int, list[int]] = defaultdict(list)
        for i in order:
            by_topic[int(sec_topic[i])].append(int(i))
        sums_t = backend.segment_sum(xt, np.arange(len(vsec)), sec_topic, np.ones(len(vsec), np.float32),
                                     lv["n_topics"])
        n_child = np.bincount(lv["topic"], minlength=lv["n_topics"]) if li > 0 else None
        sizes, n_srcs = [], []
        for t in range(lv["n_topics"]):
            mem = by_topic[t]
            n = len(mem)
            srcs = Counter(x_src[i] for i in mem)
            sizes.append(n)
            n_srcs.append(len(srcs))
            src_sorted = [s for s, _ in sorted(srcs.items(), key=lambda kv: (-kv[1], kv[0]))]
            per_src: Counter = Counter()
            cs: list[str] = []
            for i in mem:                                  # central sections: by cosine, at most k per source
                if per_src[x_src[i]] < int(p["max_central_per_source"]):
                    cs.append(sec_ids_v[i])
                    per_src[x_src[i]] += 1
                if len(cs) >= int(p["n_central_sections"]):
                    break
            ssum = sums_t[t].astype(np.float64)
            coh = float((ssum @ ssum - n) / (n * (n - 1))) if n > 1 else 1.0
            parent = topic_ids[li + 1][int(levels[li + 1]["topic"][t])] if li + 1 < len(levels) else None
            topics_rows["topic_id"].append(topic_ids[li][t])
            topics_rows["level"].append(lv["level"])
            topics_rows["parent_topic_id"].append(parent)
            topics_rows["n_children"].append(int(n_child[t]) if n_child is not None else 0)
            topics_rows["n_sections"].append(n)
            topics_rows["n_sources"].append(len(srcs))
            topics_rows["source_ids"].append(src_sorted)
            topics_rows["label_terms"].append([lem for _, _, lem in labels[t]])
            topics_rows["label_term_ids"].append([tid for tid, _, _ in labels[t]])
            topics_rows["central_section_ids"].append(cs)
            topics_rows["coherence"].append(_round(coh))
            topics_rows["rule_version"].append(RULE_VERSION)
            for rank, i in enumerate(mem, 1):
                members_rows["topic_id"].append(topic_ids[li][t])
                members_rows["section_id"].append(sec_ids_v[i])
                members_rows["level"].append(lv["level"])
                members_rows["source_id"].append(x_src[i])
                members_rows["similarity"].append(_round(float(sim[i])))
                members_rows["rank"].append(rank)
                members_rows["rule_version"].append(RULE_VERSION)
        # edges between topics of this level: centroid cosine + section-level kNN links between them
        a, b, _w = levels[0]["edges"]
        ta, tb = sec_topic[a], sec_topic[b]
        cross = ta != tb
        lk = Counter(zip(np.minimum(ta[cross], tb[cross]).tolist(), np.maximum(ta[cross], tb[cross]).tolist()))
        if lv["n_topics"] > 1:
            es, ed, ec = backend.knn(cent, np.arange(lv["n_topics"]), int(p["edge_top_k"]), 0)
            rank = np.zeros(len(es), dtype=np.int64)       # neighbours come per topic in descending cosine
            for j in range(1, len(es)):
                rank[j] = rank[j - 1] + 1 if es[j] == es[j - 1] else 0
            keep = (ec > 0) & ((rank < int(p["edge_min_k"])) | (ec >= float(p["edge_min_cosine"])))
            ea, eb, ecos = _undirected(lv["n_topics"], es[keep], ed[keep], ec[keep])
            for i, j, c in sorted(zip(ea.tolist(), eb.tolist(), ecos.tolist())):
                edges_rows["topic_id_a"].append(topic_ids[li][i])
                edges_rows["topic_id_b"].append(topic_ids[li][j])
                edges_rows["level"].append(lv["level"])
                edges_rows["cosine"].append(_round(c))
                edges_rows["n_links"].append(int(lk.get((i, j), 0)))
                edges_rows["rule_version"].append(RULE_VERSION)
        sz, ns = np.asarray(sizes), np.asarray(n_srcs)
        cohs = topics_rows["coherence"][len(topics_rows["coherence"]) - lv["n_topics"]:]
        level_stats.append({
            "level": lv["level"], "nodes": lv["n_nodes"], "graph_edges": int(len(lv["edges"][0])),
            "communities_raw": lv["n_raw"], "topics": lv["n_topics"],
            "size_quantiles": {q: int(np.quantile(sz, q / 100)) for q in (0, 10, 25, 50, 75, 90, 100)} if len(sz)
            else {},
            "topics_multi_source": int((ns >= 2).sum()),
            "share_topics_multi_source": _round(float((ns >= 2).mean())) if len(ns) else None,
            "sections_in_multi_source_topics": int(sz[ns >= 2].sum()),
            "mean_coherence": _round(float(np.mean(cohs))) if cohs else None,
            "resolution": float(p["resolution"][lv["level"] - 1]), "knn_k": int(p["knn_k"][lv["level"] - 1]),
        })
    timings["tables"] = _now() - t0
    topics = pa.table(topics_rows, schema=TOPICS_SCHEMA)
    topic_members = pa.table(members_rows, schema=TOPIC_MEMBERS_SCHEMA)
    topic_edges = pa.table(edges_rows, schema=TOPIC_EDGES_SCHEMA)
    backend.free()
    timings["total"] = _now() - t_all
    stats["counts"] = {
        "sections_total": sec_t.num_rows, "sections_in_section_pages": n_sec,
        "sections_with_vector": int(len(vsec)),
        "share_of_sections_with_vector": _round(len(vsec) / max(1, sec_t.num_rows)),
        "topics": topics.num_rows, "topic_members": topic_members.num_rows, "topic_edges": topic_edges.num_rows,
        "dimension": dim,
    }
    stats["levels"] = level_stats
    stats["model_choices"] = MODEL_CHOICES
    stats["gpu_peak_mib"] = backend.peak_mib
    stats["timings_s"] = {k: round(v, 3) for k, v in timings.items()}
    return {"section_aggregates": section_aggregates, "section_vectors": section_vectors, "topics": topics,
            "topic_members": topic_members, "topic_edges": topic_edges}
