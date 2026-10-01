"""Offline scientific-use and DRAFT contract checker. Stdout contains aggregate codes and hashes only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkm_world.validation.draft import DraftProtocol, check_draft_protocol  # noqa: E402
from vkm_world.validation.scientific import ReviewIndex, ScientificUseBinding, admit_scientific_use  # noqa: E402
from vkm_world.worldspec.model import WorldSpec  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", required=True)
    parser.add_argument("--binding")
    parser.add_argument("--review")
    parser.add_argument("--protocol")
    parser.add_argument("--admission", help="optional prior receipt: stale results are refused")
    args = parser.parse_args(argv)
    try:
        world = WorldSpec.model_validate_json(Path(args.world).read_text(encoding="utf-8"))
        binding = ScientificUseBinding.model_validate_json(Path(args.binding).read_text(encoding="utf-8")) if args.binding else None
        review = ReviewIndex.model_validate_json(Path(args.review).read_text(encoding="utf-8")) if args.review else None
        result = admit_scientific_use(world, binding, review)
        if args.protocol:
            protocol = DraftProtocol.model_validate_json(Path(args.protocol).read_text(encoding="utf-8"))
            prior = json.loads(Path(args.admission).read_text(encoding="utf-8")) if args.admission else result
            result = check_draft_protocol(world, binding, review, protocol, prior)
        elif args.admission:
            prior = json.loads(Path(args.admission).read_text(encoding="utf-8"))
            if result["status"] != "READY" or result != prior:
                result = {"status": "BLOCKED", "reasons": ["STALE_ADMISSION"]}
        safe = {key: result[key] for key in ("status", "readiness_scope", "protocol_status", "reasons", "reason_counts",
            "receipt_sha256", "protocol_sha256", "admission_sha256", "fold_count", "sample_count", "split_manifest_sha256") if key in result}
    except (OSError, ValueError, TypeError):
        safe = {"status": "BLOCKED", "reasons": ["INVALID_INPUT"]}
    print(json.dumps(safe, sort_keys=True, ensure_ascii=False))
    return 0 if safe["status"] in ("READY", "READY_DRAFT") else 2


if __name__ == "__main__":
    raise SystemExit(main())
