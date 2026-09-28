"""Public receipt of benchmark V2 → ``docs/corpus_platform/receipts/retrieval_v2.json`` (IDs, numbers, hashes and
statuses only; no corpus text, no host paths or addresses). Inputs: ``$V2_WORK/out/{results_v2_full,serving_v2}.json``,
``$V2_WORK/{vec,vis}/*/meta.json``, ``$V2_WORK/pool/pool.json``, the V2 files of the repository."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

WORK = Path(os.environ["V2_WORK"])
REPO = Path(__file__).resolve().parents[3]
V2_DIR = REPO / "benchmarks/retrieval_v2"
OUT = REPO / "docs/corpus_platform/receipts/retrieval_v2.json"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    res = json.load(open(WORK / "out" / "results_v2_full.json", encoding="utf-8"))
    serv = json.load(open(WORK / "out" / "serving_v2.json", encoding="utf-8")) if \
        (WORK / "out" / "serving_v2.json").is_file() else {}
    models = {}
    for sub in ("vec", "vis"):
        for d in sorted((WORK / sub).iterdir()):
            f = d / "meta.json"
            if not f.is_file():
                continue
            m = json.loads(f.read_text(encoding="utf-8"))
            keep = {k: m.get(k) for k in ("model_id", "revision", "license", "precision", "attention", "n_units",
                                          "n_pages", "ids_sha256", "dim", "doc_prefix", "query_prefix", "doc_prompt",
                                          "query_prompt", "doc_max_tokens", "query_max_tokens", "load_s", "encode_s",
                                          "units_per_s", "tokens", "tokens_per_s", "pages_per_s", "queries_s",
                                          "peak_vram_mib", "finished_at") if m.get(k) is not None}
            keep["weights"] = {"receipt": (m.get("weights") or {}).get("receipt"),
                               "complete_and_verified": (m.get("weights") or {}).get("complete_and_verified"),
                               "files_sha256": (m.get("weights") or {}).get("files")}
            keep["env"] = m.get("env")
            for extra in ("serving_q8.json", "rx580_calibration.json"):
                if (d / extra).is_file():
                    keep[extra.split(".")[0]] = json.loads((d / extra).read_text(encoding="utf-8"))
            models[d.name] = keep
    decision = res["decision"]
    summary = {}
    for label in ("verified", "verified+pooled"):
        for track in ("text", "visual"):
            tr = res["sets"][label][track]
            summary[f"{label}/{track}"] = {
                n: {"status": r.get("status"), **{k: round(r["overall"][k], 4) for k in
                                                  ("ndcg@10", "recall@50", "mrr@10", "judged@10")}}
                for n, r in tr["systems"].items() if "overall" in r}
    pool = json.load(open(WORK / "pool" / "pool.json", encoding="utf-8"))["stats"] if \
        (WORK / "pool" / "pool.json").is_file() else {}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    receipt = {
        "schema": "vkm.retrieval_benchmark.v2.receipt/1", "agent": "V2", "date": "2026-09-28",
        "branch": "claude/agent-v2-embeddings-gpu-2026-09-28", "code_commit_at_receipt": commit,
        "preregistration": {"file": "benchmarks/retrieval_v2/PREREGISTRATION.md",
                            "sha256": (V2_DIR / "PREREGISTRATION.sha256").read_text(encoding="utf-8").split()[0],
                            "commit": "e36fe19"},
        "snapshot": res["snapshot"], "dense_units_snapshot": res["dense_units_snapshot"],
        "check0_reproduces_v1": res["check0"], "coverage": res["coverage"],
        "labels": {"pooled": res["pooled_labels"], "pool": pool,
                   "qrels_v2_pooled_sha256": sha(V2_DIR / "qrels_v2_pooled.tsv")
                   if (V2_DIR / "qrels_v2_pooled.tsv").is_file() else None},
        "hardware": {"encode": "WORKSTATION RTX 5070 Ti 16 GB (shared), bf16, SDPA",
                     "serving_target": "CORE RX580 8 GiB (llama.cpp Vulkan); EDGE GTX 1650 is full"},
        "models": models, "summary_metrics": summary,
        "decision": {"rule": decision["rule"], "dense_choice": decision["dense_choice"],
                     "visual_choice": decision["visual_choice"],
                     "dense": {k: {kk: v[kk] for kk in ("license", "delta_V_text", "p_V_text", "holm_p", "delta_P_text",
                                                         "p_P_text", "delta_V_visual", "delta_P_visual", "criteria",
                                                         "recommended_candidate")} for k, v in decision["dense"].items()},
                     "visual": decision["visual"]},
        "serving": serv,
        "results_file_sha256": sha(V2_DIR / "results_v2.json") if (V2_DIR / "results_v2.json").is_file() else None,
        "not_deployed": "nothing was deployed to CORE or EDGE; no service was changed",
    }
    OUT.write_text(json.dumps(receipt, indent=1, ensure_ascii=False, default=float) + "\n", encoding="utf-8",
                   newline="\n")
    print("written", OUT.relative_to(REPO), OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
