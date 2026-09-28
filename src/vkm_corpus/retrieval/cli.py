"""``vkm-corpus rerank …`` — gateway server, client calls, fixtures, parity and acceptance (agent F).

Subcommands:

* ``serve --host H [--host H2] --port 18084`` — run ``vkm-rerank-gateway`` (EDGE, inside its container);
* ``health`` / ``status`` — query a gateway (``VKM_RERANK_URL``, ``VKM_RERANK_TOKEN_FILE``);
* ``text --query Q --candidates file.jsonl`` / ``visual --query Q --image id=path …`` — one rerank call;
* ``fixtures --out DIR`` — deterministic synthetic images, queries and passages (manifest with sha256);
* ``parity reference|llama|compare`` — the blocking llama.cpp vs transformers gate;
* ``acceptance …`` — the 9-point acceptance run on EDGE (stdlib only, see ``acceptance.py``).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print(obj) -> None:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _serve(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval.gateway import serve

    return serve(args.host or ["127.0.0.1"], args.port, log_dir=args.log_dir)


def _client():
    from vkm_corpus.retrieval.client import SyncRerankClient

    return SyncRerankClient.from_settings()


def _health(args: argparse.Namespace) -> int:
    with _client() as c:
        _print(c.health())
    return 0


def _status(args: argparse.Namespace) -> int:
    with _client() as c:
        _print(c.status())
    return 0


def _text(args: argparse.Namespace) -> int:
    rows = [json.loads(line) for line in Path(args.candidates).read_text(encoding="utf-8").splitlines() if line.strip()]
    with _client() as c:
        _print(c.rerank_text(args.query, [(r["id"], r["text"]) for r in rows], top_n=args.top_n,
                             truncate_to_tokens=args.truncate_to_tokens))
    return 0


def _visual(args: argparse.Namespace) -> int:
    cands = []
    for spec in args.image:
        cid, _, path = spec.partition("=")
        if not path:
            raise SystemExit(f"--image expects id=path, got {spec!r}")
        cands.append((cid, Path(path).read_bytes()))
    with _client() as c:
        _print(c.rerank_visual(args.query, cands, top_n=args.top_n))
    return 0


def _fixtures(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval.fixtures import write_fixtures

    manifest = write_fixtures(Path(args.out), font=args.font, seed=args.seed)
    _print({"out": str(args.out), "font_file": manifest["font_file"], "font_sha256": manifest["font_sha256"],
            "images": {k: v["sha256"] for k, v in manifest["images"].items()}})
    return 0


def _parity_inputs(fixtures: Path, names: list[str] | None):
    from vkm_corpus.retrieval.images import normalize_image

    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
    names = names or ["map_scheme", "table", "plot", "section", "text_page", "blank"]
    norm = {n: normalize_image((fixtures / manifest["images"][n]["file"]).read_bytes()) for n in names}
    queries = {q["id"]: q["query"] for q in manifest["visual_queries"]}
    return norm, queries


def _images_meta(norm) -> dict:
    return {k: {"sha256": v.sha256, "pixel_sha256": v.pixel_sha256, "size_px": [v.width, v.height],
                "image_tokens": v.image_tokens} for k, v in norm.items()}


def _parity(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval import parity

    if args.parity_cmd == "reference":
        import torch
        import transformers

        norm, queries = _parity_inputs(Path(args.fixtures), args.names)
        scores = parity.reference_scores(args.model_dir, {k: v.png for k, v in norm.items()}, queries,
                                         device=args.device, dtype=args.dtype)
        out = {"side": "transformers", "model_dir": Path(args.model_dir).name, "dtype": args.dtype,
               "device": torch.cuda.get_device_name(0) if args.device.startswith("cuda") else args.device,
               "torch": torch.__version__, "transformers": transformers.__version__, "images": _images_meta(norm),
               "queries": queries, "scores": scores}
    elif args.parity_cmd == "llama":
        from vkm_corpus.retrieval.m0_head import M0Head

        norm, queries = _parity_inputs(Path(args.fixtures), args.names)
        head = M0Head.from_npz(args.head)
        scores, _ = parity.llama_scores(args.url, {k: v.png_base64() for k, v in norm.items()}, queries, head)
        out = {"side": "llama.cpp", "label": args.label, "head_sha256": head.sha256, "images": _images_meta(norm),
               "queries": queries, "scores": scores}
    else:
        ref = json.loads(Path(args.reference).read_text(encoding="utf-8"))
        cand = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
        if ref["images"] != cand["images"]:
            raise SystemExit("normalised images differ between reference and candidate")
        max_diff = parity.LAYOUT_MAX_ABS_DIFF if args.layout else parity.PARITY_MAX_ABS_DIFF
        out = parity.compare(ref["scores"], cand["scores"], max_abs_diff=max_diff,
                             spearman_min=0.999 if args.layout else parity.PARITY_SPEARMAN_MIN)
        out["reference"] = {k: ref.get(k) for k in ("side", "label", "dtype", "device", "torch", "transformers")}
        out["candidate"] = {k: cand.get(k) for k in ("side", "label", "head_sha256")}
        out["images"] = ref["images"]
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _print({k: out[k] for k in ("verdict", "max_abs_diff", "min_spearman") if k in out} or {"written": args.out})
    return 0 if out.get("verdict", "PASS") == "PASS" else 1


def _acceptance(args: argparse.Namespace) -> int:
    from vkm_corpus.retrieval import acceptance

    return acceptance.main(args.rest)


def register(subparsers) -> None:
    p = subparsers.add_parser("rerank", help="EDGE rerank gateway: serve, call, fixtures, parity, acceptance")
    sub = p.add_subparsers(dest="rerank_cmd", metavar="<cmd>")

    s = sub.add_parser("serve", help="run vkm-rerank-gateway")
    s.add_argument("--host", action="append", help="bind address (repeatable)")
    s.add_argument("--port", type=int, default=18084)
    s.add_argument("--log-dir", default=None)
    s.set_defaults(func=_serve)

    sub.add_parser("health", help="GET /health of the gateway").set_defaults(func=_health)
    sub.add_parser("status", help="GET /status of the gateway").set_defaults(func=_status)

    t = sub.add_parser("text", help="POST /v1/rerank/text")
    t.add_argument("--query", required=True)
    t.add_argument("--candidates", required=True, help="JSONL with {id, text}")
    t.add_argument("--top-n", type=int, default=None)
    t.add_argument("--truncate-to-tokens", type=int, default=None)
    t.set_defaults(func=_text)

    v = sub.add_parser("visual", help="POST /v1/rerank/visual")
    v.add_argument("--query", required=True)
    v.add_argument("--image", action="append", required=True, help="id=path (repeatable, ≤ 8)")
    v.add_argument("--top-n", type=int, default=None)
    v.set_defaults(func=_visual)

    f = sub.add_parser("fixtures", help="write synthetic rerank fixtures")
    f.add_argument("--out", required=True)
    f.add_argument("--font", default=None)
    f.add_argument("--seed", type=int, default=20260928)
    f.set_defaults(func=_fixtures)

    par = sub.add_parser("parity", help="llama.cpp vs transformers parity of the visual reranker")
    psub = par.add_subparsers(dest="parity_cmd", required=True)
    r = psub.add_parser("reference")
    r.add_argument("--fixtures", required=True)
    r.add_argument("--model-dir", required=True)
    r.add_argument("--device", default="cuda")
    r.add_argument("--dtype", default="bfloat16")
    r.add_argument("--names", nargs="*", default=None)
    r.add_argument("--out", required=True)
    ll = psub.add_parser("llama")
    ll.add_argument("--fixtures", required=True)
    ll.add_argument("--url", required=True)
    ll.add_argument("--head", required=True)
    ll.add_argument("--label", default="llama.cpp")
    ll.add_argument("--names", nargs="*", default=None)
    ll.add_argument("--out", required=True)
    c = psub.add_parser("compare")
    c.add_argument("--reference", required=True)
    c.add_argument("--candidate", required=True)
    c.add_argument("--layout", action="store_true", help="layout parity: |Δ| ≤ 1e-3")
    c.add_argument("--out", required=True)
    par.set_defaults(func=_parity)

    a = sub.add_parser("acceptance", help="9-point acceptance on EDGE (see acceptance.py --help)")
    a.add_argument("rest", nargs=argparse.REMAINDER)
    a.set_defaults(func=_acceptance)


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(prog="vkm-corpus")
    register(parser.add_subparsers(dest="group"))
    ns = parser.parse_args()
    sys.exit(ns.func(ns))
