# Phase 00 — verify the self-contained bundle

1. Read AGENTS.md.
2. Run `python scripts/verify_inputs.py --root .`.
3. Confirm all five bootstrap artifacts and eleven primary sources are `PASS`.
4. Reject any stale absolute path in configs or scripts.
5. Produce `artifacts/status/input_verification.json` and stop on any mismatch.
