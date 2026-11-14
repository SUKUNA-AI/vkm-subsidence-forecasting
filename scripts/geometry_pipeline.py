"""Bounded source geometry format checks; explicit local inputs and runtimes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from vkm_geometry.transport import (assert_rows_equal, batch_rows, load_batch, read_dxf,
                                   read_wkb_jsonl, write_dxf, write_wkb_jsonl)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "qgis", "cad"))
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--rows", type=Path)
    parser.add_argument("--dxf", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--qgis-root", type=Path)
    args = parser.parse_args()
    result = {"status": "FAIL", "action": args.action,
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if args.output.exists() and any(args.output.iterdir()): parser.error("Use a fresh output directory")
    try:
        if args.action == "prepare":
            from vkm_geometry.transport import validate_batch
            raw = args.batch.read_bytes()
            input_sha = hashlib.sha256(raw).hexdigest()
            rows = batch_rows(validate_batch(json.loads(raw.decode("utf-8-sig"))))
            args.output.mkdir(parents=True, exist_ok=True)
            write_wkb_jsonl(rows, args.output / "source.wkb.jsonl")
            write_dxf(rows, args.output / "source.cad_yup.dxf")
            assert_rows_equal(rows, read_wkb_jsonl(args.output / "source.wkb.jsonl"))
            assert_rows_equal(rows, read_dxf(args.output / "source.cad_yup.dxf"))
            if hashlib.sha256(args.batch.read_bytes()).hexdigest() != input_sha:
                raise RuntimeError("Input batch changed during transport preparation")
            result.update(status="PASS", features=len(rows), batch_id=rows[0]["batch_id"],
                          input_batch_sha256=input_sha,
                          native_frame_preserved=True, cad_transform="explicit y reflection only; no geodetic registration")
        elif args.action == "qgis":
            from vkm_geometry.qgis_check import check_qgis
            result.update(check_qgis(read_wkb_jsonl(args.rows), args.output, public_root=args.public_root,
                                     private_root=args.private_root, qgis_root=args.qgis_root))
        else:
            from vkm_geometry.qgis_check import check_cad
            result.update(check_cad(read_wkb_jsonl(args.rows), args.dxf, args.output))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "pipeline_receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__": raise SystemExit(main())
