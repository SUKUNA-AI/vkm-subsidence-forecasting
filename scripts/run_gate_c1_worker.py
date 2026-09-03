#!/usr/bin/env python
"""Execute one frozen Gate C1 outer job inside gate_c_torch."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.data_contracts import discover_project_root
from skru1.gate_c1_worker import run_outer_job


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-job", required=True)
    args = parser.parse_args()
    root = discover_project_root()
    path = Path(args.runtime_job).resolve()
    try:
        path.relative_to((root / "work" / "gate_c1" / "runtime_jobs").resolve())
    except ValueError as exc:
        parser.error(f"runtime job must be under work/gate_c1/runtime_jobs: {exc}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        result = run_outer_job(root, payload)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL_PROTOCOL" if exc.__class__.__name__ == "ContractViolation" else "REJECTED_MODEL_EXECUTION",
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(limit=12),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    # On the frozen Windows/CUDA stack, PyTorch may raise NTSTATUS C0000409
    # during interpreter-level CUDA destructor teardown *after* every artifact
    # has been atomically committed.  The isolated worker owns no reusable
    # process state, so flush streams and terminate without running those
    # process-global destructors.  Model/training failures still return 2 from
    # main() before this hard exit.
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
