"""TERM_DICTIONARY_V1 stage A (NAV venv with pymorphy3): the local data root and the formulations.

1. A local data root ``$TD_WORK/root``: the NAV build of the snapshot + ``term_translations`` packed into
   ``derived/navigation/<snapshot>/nav.duckdb`` (files linked, not copied), the canonical DuckDB, and the catalogue
   pack of this PUBLIC checkout (the VKM core tier of the dossier).
2. For every topic query of ``topic_v1`` and both arms (translate off / on): the dossier's formulations and its core
   source list, computed by ``api.topic.DossierBuilder`` itself (concept, dictionary) — retrieval is not called here.
3. For every text query of the retrieval set: ``translate_query`` (the expansion of H1).

Environment: ``TD_WORK`` (work dir, outside git), ``TD_NAV_BUILD`` (NAV build dir of the snapshot),
``TD_DICT`` (``term_translations.parquet``), ``TD_CANON`` (canonical DuckDB of the snapshot), ``TD_COMMIT`` (PUBLIC
commit of the catalogues). Writes ``$TD_WORK/stage_a.json``.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from vkm_corpus.api import topic  # noqa: E402
from vkm_corpus.api.canon import CanonStore  # noqa: E402
from vkm_corpus.catalogues import pack as cpack  # noqa: E402
from vkm_corpus.catalogues.store import CatalogueStore  # noqa: E402
from vkm_corpus.navigation import store as nav_store  # noqa: E402
from vkm_corpus.retrieval_lab import bench as B  # noqa: E402

WORK = Path(os.environ["TD_WORK"])
NAV_BUILD = Path(os.environ["TD_NAV_BUILD"])
DICT = Path(os.environ["TD_DICT"])
CANON = Path(os.environ["TD_CANON"])
COMMIT = os.environ.get("TD_COMMIT")


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def data_root() -> tuple[Path, str]:
    import duckdb

    con = duckdb.connect(str(CANON), read_only=True)
    snap = con.execute("SELECT snapshot_id FROM meta.snapshot").fetchone()[0]
    con.close()
    root = WORK / "root"
    nav_dir = root / "derived" / "navigation" / snap
    nav_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(NAV_BUILD.glob("*.parquet")):
        link = nav_dir / f.name
        if not link.exists():
            link.symlink_to(f)
    link = nav_dir / "term_translations.parquet"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(DICT)
    if (NAV_BUILD / "manifest.json").exists():
        (nav_dir / "manifest.json").write_text((NAV_BUILD / "manifest.json").read_text(encoding="utf-8"),
                                               encoding="utf-8")
    packed = nav_store.pack(nav_dir)
    nav_store.publish(root, snap)
    db = root / "duckdb"
    db.mkdir(exist_ok=True)
    canon_link = db / "vkm_corpus.duckdb"
    if not canon_link.exists():
        canon_link.symlink_to(CANON)
    cat = cpack.pack(REPO, WORK / "catpack", commit=COMMIT, allow_dirty=True)
    log("nav pack", packed["tables"].get("term_translations"), "catalogues", cat["pack_id"], cat["working_tree"])
    return root, snap


class Recorder:
    """Retrieval stand-in: the dossier's searches are recorded, never run (formulations do not depend on them)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []

    def search(self, query, *, source_ids=None, limit=50):
        self.calls.append((query, tuple(source_ids) if source_ids else None))
        return {"units": []}


def formulations(root: Path, snap: str, topics: list[dict]) -> dict:
    canon = CanonStore(root / "duckdb" / "vkm_corpus.duckdb", current_snapshot=lambda: snap)
    nav = nav_store.NavStore(root, canonical_db=root / "duckdb" / "vkm_corpus.duckdb")
    cats = CatalogueStore(db_path=WORK / "catpack" / "catalogues.duckdb")
    out: dict = {"queries": {}, "core_sources": None}
    shared: dict = {}                                   # core tier and section index, computed once
    for t in topics:
        for q in t["queries"]:
            for arm, translate in (("D0", False), ("D1", True)):
                rec = Recorder()
                b = topic.DossierBuilder(canon, nav, cats, rec, cache=shared)
                st = topic._State(req=topic.TopicRequest(query=q["text"], translate=translate),
                                  stems=topic.query_stems(q["text"]))
                b._nav_ready(st)
                cat_ok = b._catalogues_ready(st)
                st.core = b._core_sources(st, cat_ok)
                b._concept(st)
                f = b._formulations(st)
                out["queries"].setdefault(q["query_id"], {"topic_id": t["topic_id"]})[arm] = {
                    "formulations": f, "translation": st.inputs.get("translation"),
                    "warnings": sorted({w["code"] for w in st.warnings})}
                if out["core_sources"] is None:
                    out["core_sources"] = sorted(st.core)
                    out["core_tier"] = st.inputs.get("core_tier")
    return out


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    root, snap = data_root()
    topics = [json.loads(line) for line in open(REPO / "benchmarks/topic_v1/topic_set_v1.jsonl", encoding="utf-8")
              if line.strip()]
    t0 = time.time()
    dossier = formulations(root, snap, topics)
    log("formulations", len(dossier["queries"]), round(time.time() - t0, 1), "s")
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    nav = nav_store.NavStore(root, canonical_db=root / "duckdb" / "vkm_corpus.duckdb")
    translations = {}
    for q in bench.queries:
        if q.track != "text":
            continue
        r = nav.run("translate_query", q.text)
        translations[q.query_id] = {"text": q.text, "translation": r.get("translation"),
                                    "source_language": r.get("source_language"),
                                    "target_language": r.get("target_language"), "coverage": r.get("coverage"),
                                    "pair_ids": [x["pair_id"] for x in r.get("terms") or []]}
    applied = sum(1 for v in translations.values() if v["translation"])
    log("translations", applied, "of", len(translations))
    out = {"snapshot_id": snap, "dictionary": DICT.name, "dossier": dossier, "retrieval_translations": translations}
    (WORK / "stage_a.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
