# Expected-result checks: finite values and compatibility

The shared job checks reject nonfinite expected numbers (including nested JSON),
nonfinite or negative tolerances, and nonfinite actual values. Evaluation validates
raw checks again, so callers and stored job specs cannot bypass the contract.
Overflow in the comparison arithmetic fails the check. Rejected nonfinite values
are reported as `actual: null` with a failure message; diagnostic metadata is also
safe to serialize as strict JSON. Booleans are distinct from integers throughout
structural comparisons; numeric checks do not coerce booleans or numeric strings.
Mixed integer/float differences and tolerance bounds use exact ratios, so a large
integer is not rounded before comparison. Legacy error fields use the same delta.
Rejected saved checks keep a fallback name and failed-check evidence in receipts.

JSON pointers retain empty tokens and decode `~0` / `~1` once. Array indexes must
use ASCII decimal digits without a sign or a leading zero. Invalid escapes fail.
The existing evaluator alias `pointer="/"` still means the whole document;
the low-level RFC lookup itself continues to address the empty dictionary key.

`vkm_ansys.checks` is a deprecated compatibility adapter. Its `validate`,
`evaluate`, and `evaluate_all` functions remain available. It preserves regular
expressions for text checks, `file_exists(expected=False)`, default exit code zero,
zero numeric tolerances, exact `json_value` comparisons, full finite JSON actual
values, and finite `abs_error` / `rel_error` fields. Unrepresentable error diagnostics
are null with an explicit message. New job code uses `vkm_jobs`, whose text checks
are literal substrings and whose `number_close` relative tolerance defaults to
`1e-9`. These intentional legacy differences have regression coverage in
[test_checks_contract.py](../../tests/engineering/test_checks_contract.py).

## Current Ansys jobs and replay

Mechanical, Workbench and optiSLang send their checks through
`ProductJob` and `JobDraft.spec()` to the shared `vkm_jobs` runner. For
manual evaluation of an existing ladder result, use the same evaluator.

```python
from pathlib import Path

from vkm_ansys import ladder
from vkm_jobs.checks import run_checks


def ladder_checks(job_dir: Path, step: str, parameters: dict, process_exit: int):
    return run_checks(job_dir, ladder.expected_checks(step, parameters), process_exit)
```

Ladder numeric checks declare both tolerances, including explicit zeros.
Ladder JSON checks carry no tolerances, so their comparisons are exact.

| Existing check | Current shared contract |
|---|---|
| Text search | `text_contains` and `text_absent` use literal substrings. Historical regex checks need an explicitly equivalent spec or replay on the pinned version. |
| File existence | `expected: false` requires absence. Omitting `expected` requires presence. The receipt records a boolean for a specific path and a match count for a glob. |
| Exit status | Supply the intended integer `expected` in the check spec, including `0` when required. The evaluator treats unavailable exit status as a failed check. |
| Numeric tolerance | Set both `rtol` and `atol` explicitly when migrating a check with historical zero defaults. |
| Exact JSON | Omit `rtol` and `atol`. The evaluator accepts `/` as a whole-document alias. Low-level `json_pointer(doc, "/")` addresses the empty key. |
| Diagnostics | Shared receipts provide `actual`, `passed` and a failure message where applicable. Use the pinned historical evaluator when a replay needs its full `actual` payload or the `abs_error` and `rel_error` fields. |

Replay earlier jobs with the commit recorded by that job or its archived
runtime. Keep its original spec, inputs and receipt together. A replay evaluates
those saved artifacts with their original runtime. Approval to remove the
adapter depends on verifying local configurations and saved job specs that still
call it, then confirming that their pinned replay remains usable.
