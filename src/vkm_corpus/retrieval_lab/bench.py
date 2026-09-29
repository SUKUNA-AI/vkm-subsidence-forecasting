"""Benchmark files of ``benchmarks/retrieval_v0``: queries, graded qrels, hard negatives, splits.

Everything is public-safe: queries are own formulations (never source text), qrels and hard negatives carry IDs and
grades only. Vocabularies are plain tuples of strings (StrEnum lives only in ``vkm_corpus.contracts``, H-01).

Files:

* ``queries.jsonl`` — one query per line (:class:`Query`);
* ``qrels.tsv`` — ``query_id, level, doc_id, grade, status, basis, evidence_ids`` (header line);
* ``hard_negatives.tsv`` — ``query_id, level, doc_id, negative_type, basis, status`` (each also in qrels with grade 0);
* ``splits.json`` — ``{"method", "salt", "fractions", "assignments": {query_id: train|dev|test}}``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from vkm_corpus.ids.grammar import OBJECT_ID, PAGE_ID

# ---------------------------------------------------------------- closed vocabularies (task §13, §14, §15, §16, §22)
# text-track categories (§13) → short code used in query IDs
CATEGORY_CODES: dict[str, str] = {
    "exact_entity": "ENT", "lexical": "LEX", "ru_semantic": "RSEM", "ru_technical": "RTECH", "ru_to_en": "RUEN",
    "en_to_ru": "ENRU", "terminology_mismatch": "TERM", "salt_mechanics": "SALT", "creep_rheology": "CREEP",
    "damage": "DMG", "geodesy": "GEOD", "mine_surveying": "MSURV", "hydrogeology": "HYDRO", "insar": "INSAR",
    "inverse_problems": "INV", "mathematics": "MATH", "bibliography": "BIB", "vkm_specific": "VKM",
    "skru1_specific": "SKRU", "formula_method": "FORM", "long_context": "LONG",
}
CATEGORIES: tuple[str, ...] = tuple(CATEGORY_CODES)
# visual-track categories (§14)
VISUAL_CATEGORY_CODES: dict[str, str] = {
    "mine_field_map": "FIELD", "block_panel_layout": "BLOCK", "geological_section": "GSECT",
    "hydrogeological_section": "HSECT", "tectonic_map": "TECT", "subsidence_graph": "SGRAPH",
    "subsidence_map": "SMAP", "insar_map": "INSAR", "chamber_pillar_scheme": "PILLAR", "seam_workings_plan": "SEAM",
    "properties_table": "PROPT", "radargram": "GPR", "microseismic_map": "MSEIS", "leveling_line": "LEVEL",
    "borehole_column": "BHOLE", "other_visual": "OTHER",
}
VISUAL_CATEGORIES: tuple[str, ...] = tuple(VISUAL_CATEGORY_CODES)
SLICES: tuple[str, ...] = ("russian", "ru_to_en", "en_to_ru", "vkm", "skru1", "physics", "math", "bibliography",
                           "long_context")
TRACKS: tuple[str, ...] = ("text", "visual")
LANGS: tuple[str, ...] = ("ru", "en")
TARGET_LANGS: tuple[str, ...] = ("ru", "en", "any")
UNIT_KINDS: tuple[str, ...] = ("BLOCK_GROUP", "FIGURE", "TABLE", "FORMULA", "BIB_ENTRY", "PAGE")
GRADES: tuple[int, ...] = (0, 1, 2, 3)
LEVELS: tuple[str, ...] = ("PAGE", "OBJECT")
QREL_STATUSES: tuple[str, ...] = ("VERIFIED", "CANDIDATE", "NEEDS_REVIEW")
QREL_BASES: tuple[str, ...] = ("EVIDENCE_VNEXT", "SKRU1_OBJECT_INDEX", "SOURCE_METADATA", "PAGE_INSPECTION",
                               "POOL_JUDGMENT")
NEGATIVE_TYPES: tuple[str, ...] = ("SAME_TOPIC_OTHER_OBJECT", "SAME_AUTHOR_OTHER_WORK", "SAME_WORD_OTHER_PROCESS",
                                   "GENERAL_TEXTBOOK_NOT_SITE", "SIMILAR_GRAPH_OTHER_VARIABLE")
SPLITS: tuple[str, ...] = ("train", "dev", "test")
SPLIT_FRACTIONS: dict[str, float] = {"train": 0.3, "dev": 0.2, "test": 0.5}
SPLIT_SALT = "vkm-retrieval-v0"
QRELS_COLUMNS: tuple[str, ...] = ("query_id", "level", "doc_id", "grade", "status", "basis", "evidence_ids")
HN_COLUMNS: tuple[str, ...] = ("query_id", "level", "doc_id", "negative_type", "basis", "status")
QUERY_KEYS: frozenset[str] = frozenset({"query_id", "track", "text", "lang", "target_lang", "category", "slices",
                                        "intent", "expected_kinds", "version"})
# keys that may never appear in public benchmark files (CP-04, leakage guard)
FORBIDDEN_KEYS: frozenset[str] = frozenset({"quote", "verbatim_quote", "ocr_text", "page_text", "full_text"})
MAX_QUERY_WORDS = 120            # long-context queries are paragraphs, not documents
_QUERY_ID = re.compile(r"^(?P<track>[TV])-(?P<code>[A-Z0-9]{2,6})-(?P<n>[0-9]{3})$")
_PAGE = re.compile(PAGE_ID)
_OBJECT = re.compile(OBJECT_ID)
_EVIDENCE_ID = re.compile(r"^EV-VN-S[0-9]{3}-[0-9]{4,5}$")


class BenchmarkError(ValueError):
    """A benchmark file violates its schema."""


@dataclass(frozen=True)
class Query:
    query_id: str
    track: str
    text: str
    lang: str
    target_lang: str
    category: str
    slices: tuple[str, ...] = ()
    intent: str = ""
    expected_kinds: tuple[str, ...] = ()
    version: int = 1

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Query":
        unknown = set(obj) - QUERY_KEYS
        if unknown:
            raise BenchmarkError(f"{obj.get('query_id')}: unknown keys {sorted(unknown)}")
        return cls(query_id=obj["query_id"], track=obj["track"], text=obj["text"], lang=obj["lang"],
                   target_lang=obj.get("target_lang", "any"), category=obj["category"],
                   slices=tuple(obj.get("slices") or ()), intent=obj.get("intent", ""),
                   expected_kinds=tuple(obj.get("expected_kinds") or ()), version=int(obj.get("version", 1)))

    def as_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["slices"], d["expected_kinds"] = list(self.slices), list(self.expected_kinds)
        return d


@dataclass(frozen=True)
class Qrel:
    query_id: str
    level: str
    doc_id: str
    grade: int
    status: str
    basis: str
    evidence_ids: tuple[str, ...] = ()

    def row(self) -> dict[str, str]:
        return {"query_id": self.query_id, "level": self.level, "doc_id": self.doc_id, "grade": str(self.grade),
                "status": self.status, "basis": self.basis, "evidence_ids": ";".join(self.evidence_ids)}


@dataclass(frozen=True)
class HardNegative:
    query_id: str
    level: str
    doc_id: str
    negative_type: str
    basis: str
    status: str


@dataclass
class Benchmark:
    queries: list[Query]
    qrels: list[Qrel]
    hard_negatives: list[HardNegative] = field(default_factory=list)
    splits: dict[str, str] = field(default_factory=dict)
    root: Path | None = None

    def query(self, query_id: str) -> Query:
        for q in self.queries:
            if q.query_id == query_id:
                return q
        raise KeyError(query_id)

    def select(self, *, track: str | None = None, split: str | None = None,
               categories: Iterable[str] | None = None) -> list[Query]:
        cats = set(categories) if categories is not None else None
        return [q for q in self.queries if (track is None or q.track == track)
                and (split is None or self.splits.get(q.query_id) == split)
                and (cats is None or q.category in cats)]

    def judgments(self, *, level: str = "PAGE", statuses: Iterable[str] = ("VERIFIED",),
                  treat_needs_review_as: int | None = None) -> dict[str, dict[str, int]]:
        """query_id → {doc_id: grade} of the judged pairs (default: VERIFIED only). With ``treat_needs_review_as``
        NEEDS_REVIEW pairs enter with that grade (sensitivity run)."""
        wanted = set(statuses)
        out: dict[str, dict[str, int]] = defaultdict(dict)
        for r in self.qrels:
            if r.level != level:
                continue
            if r.status in wanted:
                out[r.query_id][r.doc_id] = r.grade
            elif r.status == "NEEDS_REVIEW" and treat_needs_review_as is not None:
                out[r.query_id].setdefault(r.doc_id, treat_needs_review_as)
        return dict(out)

    def hard_negative_ids(self, level: str = "PAGE") -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for h in self.hard_negatives:
            if h.level == level and h.status == "VERIFIED":
                out[h.query_id].add(h.doc_id)
        return dict(out)

    def stats(self) -> dict[str, Any]:
        by_track = Counter(q.track for q in self.queries)
        by_cat = Counter(q.category for q in self.queries)
        by_status = Counter((r.level, r.status) for r in self.qrels)
        grades = Counter(r.grade for r in self.qrels if r.status == "VERIFIED")
        judged = self.judgments(level="PAGE")
        with_rel = sum(1 for q in self.queries if any(g >= 2 for g in judged.get(q.query_id, {}).values()))
        return {"queries": dict(by_track), "categories": dict(sorted(by_cat.items())),
                "qrels": {f"{lvl}/{st}": n for (lvl, st), n in sorted(by_status.items())},
                "verified_grades": {str(g): grades.get(g, 0) for g in GRADES},
                "queries_with_verified_relevant_page": with_rel, "hard_negatives": len(self.hard_negatives),
                "splits": dict(Counter(self.splits.values()))}


# ---------------------------------------------------------------- reading
def _read_tsv(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        if tuple(reader.fieldnames or ()) != columns:
            raise BenchmarkError(f"{path.name}: header {reader.fieldnames} != {list(columns)}")
        return [dict(r) for r in reader]


def load_queries(path: Path) -> list[Query]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(Query.from_json(json.loads(line)))
        except (KeyError, json.JSONDecodeError) as exc:
            raise BenchmarkError(f"{path.name}:{n}: {exc}") from exc
    return out


def load_qrels(path: Path) -> list[Qrel]:
    out = []
    for r in _read_tsv(path, QRELS_COLUMNS):
        out.append(Qrel(r["query_id"], r["level"], r["doc_id"], int(r["grade"]), r["status"], r["basis"],
                        tuple(x for x in (r.get("evidence_ids") or "").split(";") if x)))
    return out


def load_hard_negatives(path: Path) -> list[HardNegative]:
    return [HardNegative(**r) for r in _read_tsv(path, HN_COLUMNS)] if path.is_file() else []


def load_benchmark(root: str | Path) -> Benchmark:
    root = Path(root)
    splits_path = root / "splits.json"
    splits = json.loads(splits_path.read_text(encoding="utf-8"))["assignments"] if splits_path.is_file() else {}
    qrels_path = root / "qrels.tsv"
    return Benchmark(load_queries(root / "queries.jsonl"), load_qrels(qrels_path) if qrels_path.is_file() else [],
                     load_hard_negatives(root / "hard_negatives.tsv"), splits, root)


# ---------------------------------------------------------------- writing
def write_queries(path: Path, queries: Iterable[Query]) -> None:
    lines = [json.dumps(q.as_json(), ensure_ascii=False, sort_keys=False) for q in queries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_qrels(path: Path, qrels: Iterable[Qrel]) -> None:
    rows = sorted((r.row() for r in qrels), key=lambda r: (r["query_id"], r["level"], -int(r["grade"]), r["doc_id"]))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=QRELS_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def write_hard_negatives(path: Path, items: Iterable[HardNegative]) -> None:
    rows = sorted((asdict(h) for h in items), key=lambda r: (r["query_id"], r["doc_id"]))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HN_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- splits (§24, §59)
def _split_key(query_id: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}|{query_id}".encode("utf-8")).hexdigest()


def make_splits(queries: Iterable[Query], fractions: dict[str, float] | None = None,
                salt: str = SPLIT_SALT) -> dict[str, str]:
    """Deterministic split stratified by (track, category): inside each stratum queries are ordered by
    sha256(salt|query_id); the first ⌊n·train⌋ go to train, the next ⌊n·dev⌋ to dev, the rest (rounding in favour of
    test) to test. Adding a query moves at most the queries of its own stratum."""
    fractions = fractions or SPLIT_FRACTIONS
    if set(fractions) != set(SPLITS) or not math.isclose(sum(fractions.values()), 1.0):
        raise BenchmarkError(f"fractions must cover {SPLITS} and sum to 1")
    strata: dict[tuple[str, str], list[str]] = defaultdict(list)
    for q in queries:
        strata[(q.track, q.category)].append(q.query_id)
    out: dict[str, str] = {}
    for key in sorted(strata):
        ids = sorted(strata[key], key=lambda i: _split_key(i, salt))
        n = len(ids)
        n_train = math.floor(n * fractions["train"])
        n_dev = math.floor(n * fractions["dev"])
        for i, qid in enumerate(ids):
            out[qid] = "train" if i < n_train else ("dev" if i < n_train + n_dev else "test")
    return dict(sorted(out.items()))


def splits_document(assignments: dict[str, str], fractions: dict[str, float] | None = None,
                    salt: str = SPLIT_SALT) -> dict[str, Any]:
    return {"method": "stratified by (track, category); order sha256(salt|query_id); floor for train/dev",
            "salt": salt, "fractions": fractions or SPLIT_FRACTIONS, "assignments": assignments}


# ---------------------------------------------------------------- validation
def _json_keys(obj: Any) -> set[str]:
    keys: set[str] = set()
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            keys |= {str(k).lower() for k in cur}
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return keys


def validate_doc_id(level: str, doc_id: str) -> bool:
    return bool(_PAGE.match(doc_id)) if level == "PAGE" else bool(_OBJECT.match(doc_id))


def validate_benchmark(bench: Benchmark, *, min_text: int = 0, min_visual: int = 0) -> list[str]:
    """Schema and consistency problems (empty list = valid)."""
    problems: list[str] = []
    seen: set[str] = set()
    for q in bench.queries:
        m = _QUERY_ID.match(q.query_id)
        if not m:
            problems.append(f"{q.query_id}: bad query_id")
            continue
        if q.query_id in seen:
            problems.append(f"{q.query_id}: duplicate query_id")
        seen.add(q.query_id)
        if q.track not in TRACKS or (m["track"] == "T") != (q.track == "text"):
            problems.append(f"{q.query_id}: track {q.track!r} does not match the ID")
        codes = CATEGORY_CODES if q.track == "text" else VISUAL_CATEGORY_CODES
        if q.category not in codes:
            problems.append(f"{q.query_id}: unknown category {q.category!r} for track {q.track}")
        elif codes[q.category] != m["code"]:
            problems.append(f"{q.query_id}: code {m['code']} != {codes[q.category]} of {q.category}")
        if q.lang not in LANGS or q.target_lang not in TARGET_LANGS:
            problems.append(f"{q.query_id}: bad lang/target_lang {q.lang}/{q.target_lang}")
        bad_slices = set(q.slices) - set(SLICES)
        if bad_slices:
            problems.append(f"{q.query_id}: unknown slices {sorted(bad_slices)}")
        bad_kinds = set(q.expected_kinds) - set(UNIT_KINDS)
        if bad_kinds:
            problems.append(f"{q.query_id}: unknown expected_kinds {sorted(bad_kinds)}")
        words = len(q.text.split())
        if not q.text.strip() or words > MAX_QUERY_WORDS:
            problems.append(f"{q.query_id}: query text empty or longer than {MAX_QUERY_WORDS} words")
        if q.category == "ru_to_en" and not (q.lang == "ru" and q.target_lang == "en"):
            problems.append(f"{q.query_id}: ru_to_en must be lang=ru, target_lang=en")
        if q.category == "en_to_ru" and not (q.lang == "en" and q.target_lang == "ru"):
            problems.append(f"{q.query_id}: en_to_ru must be lang=en, target_lang=ru")
        if _json_keys(q.as_json()) & FORBIDDEN_KEYS:
            problems.append(f"{q.query_id}: forbidden key")
    pairs: set[tuple[str, str, str]] = set()
    for r in bench.qrels:
        where = f"qrels {r.query_id} {r.doc_id}"
        if r.query_id not in seen:
            problems.append(f"{where}: unknown query")
        if r.level not in LEVELS or not validate_doc_id(r.level, r.doc_id):
            problems.append(f"{where}: bad level/doc_id")
        if r.grade not in GRADES:
            problems.append(f"{where}: grade {r.grade} not in 0..3")
        if r.status not in QREL_STATUSES:
            problems.append(f"{where}: status {r.status}")
        if not r.basis or any(b not in QREL_BASES for b in r.basis.split("+")):
            problems.append(f"{where}: basis {r.basis}")
        if any(not _EVIDENCE_ID.match(e) for e in r.evidence_ids):
            problems.append(f"{where}: bad evidence id")
        key = (r.query_id, r.level, r.doc_id)
        if key in pairs:
            problems.append(f"{where}: duplicate pair")
        pairs.add(key)
    grade_of = {(r.query_id, r.level, r.doc_id): r.grade for r in bench.qrels}
    for h in bench.hard_negatives:
        where = f"hard negative {h.query_id} {h.doc_id}"
        if h.negative_type not in NEGATIVE_TYPES or h.status not in QREL_STATUSES:
            problems.append(f"{where}: bad type/status")
        if grade_of.get((h.query_id, h.level, h.doc_id)) != 0:
            problems.append(f"{where}: must also be in qrels with grade 0")
    if bench.splits:
        missing = seen - set(bench.splits)
        extra = set(bench.splits) - seen
        if missing or extra:
            problems.append(f"splits: missing {sorted(missing)[:5]} extra {sorted(extra)[:5]}")
        if set(bench.splits.values()) - set(SPLITS):
            problems.append("splits: unknown split name")
    n_text = sum(1 for q in bench.queries if q.track == "text")
    n_visual = sum(1 for q in bench.queries if q.track == "visual")
    if n_text < min_text or n_visual < min_visual:
        problems.append(f"too few queries: text {n_text} < {min_text} or visual {n_visual} < {min_visual}")
    return problems


# ------------------------------------------------------------------ pooled labels (V1: separate label source, §12)
POOLED_COLUMNS: tuple[str, ...] = ("query_id", "level", "doc_id", "grade", "status", "basis", "label_source",
                                   "pooled_from", "rationale")
# one source per labelling round; a later round labels only pages without any earlier label (V2: blind packets)
POOLED_LABEL_SOURCES: tuple[str, ...] = ("LLM_AGENT_V1", "LLM_AGENT_V2")
MAX_RATIONALE_CHARS = 160            # a one-line reason in own words; never a quote
_POOLED_FROM = re.compile(r"^[A-Za-z0-9_]+@[0-9]{1,3}(,[A-Za-z0-9_]+@[0-9]{1,3})*$")


def load_pooled_qrels(path: str | Path) -> list[dict[str, str]]:
    """Rows of a pooled-labels file (header checked). Pooled labels never mix with ``qrels.tsv``: they keep their own
    ``label_source`` and status CANDIDATE; a VERIFIED label always wins where both exist."""
    with Path(path).open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        if tuple(reader.fieldnames or ()) != POOLED_COLUMNS:
            raise BenchmarkError(f"{Path(path).name}: header {reader.fieldnames} != {POOLED_COLUMNS}")
        return list(reader)


def pooled_judgments(rows: Iterable[dict[str, str]], level: str = "PAGE") -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        if r["level"] == level:
            out[r["query_id"]][r["doc_id"]] = int(r["grade"])
    return dict(out)


def merge_judgments(verified: dict[str, dict[str, int]],
                    pooled: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """VERIFIED ∪ pooled; a VERIFIED grade always wins."""
    out = {q: dict(j) for q, j in verified.items()}
    for q, j in pooled.items():
        tgt = out.setdefault(q, {})
        for d, g in j.items():
            tgt.setdefault(d, g)
    return out


def pooled_round_overlaps(earlier: list[dict[str, str]], later: list[dict[str, str]]) -> list[str]:
    """Pairs of a later labelling round that an earlier round already labelled (a round labels only new pages)."""
    source = {(r["query_id"], r["level"], r["doc_id"]): r["label_source"] for r in earlier}
    return [f"pooled {r['query_id']} {r['doc_id']}: already labelled by {source[key]}" for r in later
            for key in [(r["query_id"], r["level"], r["doc_id"])] if key in source]


def validate_pooled_qrels(rows: list[dict[str, str]], bench: Benchmark) -> list[str]:
    """Schema problems of pooled labels (empty list = valid): known query, PAGE id grammar, grade 0..3, status
    CANDIDATE, basis POOL_JUDGMENT, known label source, ``name@rank`` provenance, a short one-line rationale (own
    words; the public-hygiene and leakage tests guard against corpus text), no duplicate pair and no pair that already
    has a VERIFIED label."""
    problems: list[str] = []
    known = {q.query_id for q in bench.queries}
    verified = bench.judgments(level="PAGE", statuses=("VERIFIED",))
    seen: set[tuple[str, str]] = set()
    for r in rows:
        where = f"pooled {r.get('query_id')} {r.get('doc_id')}"
        if r["query_id"] not in known:
            problems.append(f"{where}: unknown query")
        if r["level"] != "PAGE" or not validate_doc_id("PAGE", r["doc_id"]):
            problems.append(f"{where}: bad level/doc_id")
        if not r["grade"].isdigit() or int(r["grade"]) not in GRADES:
            problems.append(f"{where}: grade {r['grade']!r} not in 0..3")
        if r["status"] != "CANDIDATE" or r["basis"] != "POOL_JUDGMENT":
            problems.append(f"{where}: status/basis {r['status']}/{r['basis']}")
        if r["label_source"] not in POOLED_LABEL_SOURCES:
            problems.append(f"{where}: label_source {r['label_source']!r}")
        if not _POOLED_FROM.match(r["pooled_from"] or ""):
            problems.append(f"{where}: pooled_from {r['pooled_from']!r}")
        why = r["rationale"] or ""
        if not why.strip() or len(why) > MAX_RATIONALE_CHARS or any(c in why for c in "\t\n\r"):
            problems.append(f"{where}: rationale must be one short line (≤ {MAX_RATIONALE_CHARS} chars)")
        key = (r["query_id"], r["doc_id"])
        if key in seen:
            problems.append(f"{where}: duplicate pair")
        seen.add(key)
        if r["doc_id"] in verified.get(r["query_id"], {}):
            problems.append(f"{where}: pair already has a VERIFIED label")
    return problems
