"""Fetch and verify the pinned model snapshots of the Corpus Platform (infra/models/model_pins.json).

    python scripts/fetch_models.py fetch  --models-dir <VKM_MODELS_DIR> [--key glm-ocr ...]
    python scripts/fetch_models.py verify --models-dir <VKM_MODELS_DIR> [--key ...]

``fetch`` downloads each pinned revision into ``<models-dir>/<local_dir>``, compares every LFS file with the sha256
published by the hub for that revision and writes a receipt ``<models-dir>/receipts/models_<UTC>.json``. ``verify``
works offline: it recomputes the sha256 of every LFS file recorded by the newest receipt of each model. Paths in
receipts are relative to the models directory. Needs ``huggingface_hub`` (extra ``models``). On the workstation the
hub is reachable from WSL; if Xet transfers fail, set ``HF_HUB_DISABLE_XET=1``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

PINS_DEFAULT = Path(__file__).resolve().parents[1] / "infra" / "models" / "model_pins.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pins(path: Path, keys: list[str]) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    pins = json.loads(raw)["models"]
    if keys:
        unknown = sorted(set(keys) - {p["key"] for p in pins})
        if unknown:
            raise SystemExit(f"unknown model keys: {unknown}")
        pins = [p for p in pins if p["key"] in keys]
    return pins, hashlib.sha256(raw).hexdigest()


def fetch(pins: list[dict], pins_sha: str, models_dir: Path) -> int:
    from huggingface_hub import HfApi, snapshot_download

    api = HfApi()
    receipt = {"receipt_kind": "MODEL_FETCH", "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "pins_sha256": pins_sha, "models": []}
    all_ok = True
    for pin in pins:
        local = models_dir / pin["local_dir"]
        t0 = time.time()
        snapshot_download(repo_id=pin["repo_id"], revision=pin["revision"], local_dir=str(local))
        info = api.model_info(pin["repo_id"], revision=pin["revision"], files_metadata=True)
        files, ok = [], info.sha == pin["revision"]
        for s in info.siblings:
            p = local / s.rfilename
            entry = {"path": s.rfilename, "size": s.size, "present": p.exists()}
            if s.lfs:
                entry["lfs_sha256"] = s.lfs.sha256
                entry["local_sha256"] = sha256(p) if p.exists() else None
                entry["match"] = entry["local_sha256"] == s.lfs.sha256
                ok &= bool(entry["match"])
            files.append(entry)
        all_ok &= ok
        receipt["models"].append({"key": pin["key"], "repo_id": pin["repo_id"], "revision": pin["revision"],
                                  "resolved_sha": info.sha, "local_dir": pin["local_dir"],
                                  "seconds": round(time.time() - t0, 1), "all_lfs_match": ok, "files": files})
        print(f"{pin['key']}: revision {info.sha} lfs_match={ok} files={len(files)}")
    out = models_dir / "receipts" / f"models_{receipt['created_utc'].replace(':', '').replace('-', '')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"receipt: receipts/{out.name}  status: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


def latest_records(models_dir: Path) -> dict[str, dict]:
    """Newest fetch record per revision directory, from all receipts (including the older top-level ones)."""
    receipts = sorted((models_dir / "receipts").glob("models_*.json")) if (models_dir / "receipts").is_dir() else []
    receipts = sorted(models_dir.glob("MODELS_RECEIPT*.json")) + receipts
    found: dict[str, dict] = {}
    for r in receipts:
        for m in json.loads(r.read_text(encoding="utf-8")).get("models", []):
            found[m["local_dir"]] = m
    return found


def verify(pins: list[dict], models_dir: Path) -> int:
    records = latest_records(models_dir)
    all_ok = True
    for pin in pins:
        rec = records.get(pin["local_dir"])
        if rec is None:
            print(f"{pin['key']}: NOT_FETCHED (no receipt for {pin['local_dir']})")
            all_ok = False
            continue
        bad = []
        for f in rec["files"]:
            if "lfs_sha256" not in f:
                continue
            p = models_dir / pin["local_dir"] / f["path"]
            if not p.exists() or sha256(p) != f["lfs_sha256"]:
                bad.append(f["path"])
        all_ok &= not bad
        print(f"{pin['key']}: {'PASS' if not bad else 'FAIL ' + ', '.join(bad)}")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["fetch", "verify"])
    ap.add_argument("--models-dir", required=True, type=Path)
    ap.add_argument("--pins", type=Path, default=PINS_DEFAULT)
    ap.add_argument("--key", action="append", default=[], help="model key from the pins file (repeatable)")
    args = ap.parse_args(argv)
    pins, pins_sha = load_pins(args.pins, args.key)
    if args.action == "fetch":
        return fetch(pins, pins_sha, args.models_dir)
    return verify(pins, args.models_dir)


if __name__ == "__main__":
    sys.exit(main())
