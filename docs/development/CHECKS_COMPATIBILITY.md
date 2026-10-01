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

`vkm_ansys.checks` is a deprecated compatibility adapter; its `validate`,
`evaluate`, and `evaluate_all` functions remain available. It preserves regular
expressions for text checks, `file_exists(expected=False)`, default exit code zero,
zero numeric tolerances, exact `json_value` comparisons, full finite JSON actual
values, and finite `abs_error` / `rel_error` fields. Unrepresentable error diagnostics
are null with an explicit message. New job code uses `vkm_jobs`, whose text checks
are literal substrings and whose `number_close` relative tolerance defaults to
`1e-9`. These intentional legacy differences have regression coverage in
[test_checks_contract.py](../../tests/engineering/test_checks_contract.py).
