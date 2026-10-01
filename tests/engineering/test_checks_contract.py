"""Expected-result checks fail closed and keep diagnostics valid JSON, without live solvers."""
from __future__ import annotations

import json

import pytest

from vkm_ansys import checks as legacy
from vkm_jobs.checks import json_pointer, run_check
from vkm_jobs.errors import ToolFailure
from vkm_jobs.spec import validate_check


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("kind", ["number_close", "json_value"])
def test_spec_rejects_nonfinite_expected(value, kind):
    raw = {"name": "check", "kind": kind, "path": "r.json", "expected": value}
    with pytest.raises(ToolFailure, match="finite"):
        validate_check(raw)
    with pytest.raises(ValueError, match="finite"):
        legacy.validate(raw)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0, True])
@pytest.mark.parametrize("tolerance", ["rtol", "atol"])
def test_spec_rejects_invalid_tolerances(value, tolerance):
    raw = {"name": "check", "kind": "number_close", "path": "r.json", "expected": 1, tolerance: value}
    with pytest.raises(ToolFailure):
        validate_check(raw)
    with pytest.raises(ValueError):
        legacy.validate(raw)


def test_spec_rejects_nested_nonfinite_expected():
    with pytest.raises(ToolFailure, match="finite"):
        validate_check({"kind": "json_value", "path": "r.json", "expected": {"a": [float("inf")]}})


def _evaluate(api, tmp_path, raw):
    return run_check(tmp_path, raw, 0) if api == "jobs" else legacy.evaluate({"name": "check", **raw}, tmp_path, 0)


@pytest.mark.parametrize("api", ["jobs", "legacy"])
@pytest.mark.parametrize("kind", ["json_value", "number_close"])
@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_nonfinite_actual_fails_without_publishing_value(tmp_path, api, kind, invalid):
    (tmp_path / "r.json").write_text('{"v": ' + invalid + '}', encoding="utf-8")
    result = _evaluate(api, tmp_path, {"kind": kind, "path": "r.json", "pointer": "/v", "expected": 1})
    assert result["passed"] is False and result["actual"] is None
    assert "finite" in result.get("message", result.get("error", ""))
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("api", ["jobs", "legacy"])
def test_recursive_nonfinite_actual_cannot_pass_structural_comparison(tmp_path, api):
    (tmp_path / "r.json").write_text('{"v": {"a": [Infinity]}}', encoding="utf-8")
    result = _evaluate(api, tmp_path, {"kind": "json_value", "path": "r.json", "pointer": "/v", "expected": {"a": [1]}})
    assert result["passed"] is False and result["actual"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("api", ["jobs", "legacy"])
@pytest.mark.parametrize("field,value", [("expected", float("inf")), ("expected", {"a": [float("nan")]}),
                                        ("rtol", float("inf")), ("atol", float("inf")), ("rtol", -1.0)])
def test_direct_calls_cannot_bypass_validation_or_publish_nonfinite_metadata(tmp_path, api, field, value):
    (tmp_path / "r.json").write_text('{"v": 1}', encoding="utf-8")
    raw = {"kind": "json_value", "path": "r.json", "pointer": "/v", "expected": 100, "atol": 200}
    raw[field] = value
    result = _evaluate(api, tmp_path, raw)
    assert result["passed"] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("api", ["jobs", "legacy"])
@pytest.mark.parametrize("actual,expected,rtol,atol", [(1e308, -1e308, 2.0, 0.0),
                                                     (1e308, 1e308, 1e308, 0.0),
                                                     (1e308, 1e308, 1.0, 1e308)])
def test_comparison_arithmetic_overflow_never_passes(tmp_path, api, actual, expected, rtol, atol):
    (tmp_path / "r.json").write_text(json.dumps({"v": actual}), encoding="utf-8")
    result = _evaluate(api, tmp_path, {"kind": "number_close", "path": "r.json", "pointer": "/v",
                                     "expected": expected, "rtol": rtol, "atol": atol})
    assert result["passed"] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("api", ["jobs", "legacy"])
@pytest.mark.parametrize("actual,expected", [(True, 1), ([True], [1]), ({"a": [False]}, {"a": [0]})])
def test_boolean_and_integer_are_distinct_recursively(tmp_path, api, actual, expected):
    (tmp_path / "r.json").write_text(json.dumps({"v": actual}), encoding="utf-8")
    assert not _evaluate(api, tmp_path, {"kind": "json_value", "path": "r.json", "pointer": "/v", "expected": expected})["passed"]


@pytest.mark.parametrize("api", ["jobs", "legacy"])
@pytest.mark.parametrize("actual", [True, "1"])
def test_numeric_actual_does_not_coerce_booleans_or_strings(tmp_path, api, actual):
    (tmp_path / "r.json").write_text(json.dumps({"v": actual}), encoding="utf-8")
    assert not _evaluate(api, tmp_path, {"kind": "number_close", "path": "r.json", "pointer": "/v", "expected": 1})["passed"]


@pytest.mark.parametrize("api", ["jobs", "legacy"])
def test_zero_tolerance_does_not_round_distinct_integers_to_float(tmp_path, api):
    (tmp_path / "r.json").write_text(json.dumps({"v": 2**53 + 1}), encoding="utf-8")
    assert not _evaluate(api, tmp_path, {"kind": "number_close", "path": "r.json", "pointer": "/v",
                                        "expected": 2**53, "rtol": 0, "atol": 0})["passed"]


@pytest.mark.parametrize("api", ["jobs", "legacy"])
@pytest.mark.parametrize("actual,expected,atol,passed,delta", [
    (2**53 + 1, float(2**53), 0.5, False, 1),
    (float(2**53), 2**53 + 1, 0.5, False, 1),
    (2**53 + 1, float(2**53), 1, True, 1),
    (float(2**53), 2**53 + 1, 1, True, 1),
    (2.5, 2, 0.5, True, 0.5),
    (2, 2.5, 0.5, True, 0.5),
])
def test_mixed_numeric_tolerance_keeps_exact_difference(tmp_path, api, actual, expected, atol, passed, delta):
    (tmp_path / "r.json").write_text(json.dumps({"v": actual}), encoding="utf-8")
    result = _evaluate(api, tmp_path, {"kind": "number_close", "path": "r.json", "pointer": "/v",
                                     "expected": expected, "rtol": 0, "atol": atol})
    assert result["passed"] is passed
    if api == "legacy":
        assert result["abs_error"] == delta
        assert result["rel_error"] == delta / abs(expected)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("actual,expected", [(2**53 + 1, float(2**53)), (float(2**53), 2**53 + 1)])
def test_structural_numeric_tolerance_does_not_round_mixed_values(tmp_path, actual, expected):
    (tmp_path / "r.json").write_text(json.dumps({"v": [actual]}), encoding="utf-8")
    result = run_check(tmp_path, {"kind": "json_value", "path": "r.json", "pointer": "/v",
                                 "expected": [expected], "rtol": 0, "atol": 0.5}, 0)
    assert not result["passed"]


@pytest.mark.parametrize("api", ["jobs", "legacy"])
def test_relative_tolerance_bound_is_not_rounded_up_to_acceptance(tmp_path, api):
    expected = 2**53 + 3
    (tmp_path / "r.json").write_text(json.dumps({"v": expected + 1}), encoding="utf-8")
    # The exact limit is 1 - 9/2**106; float multiplication rounds it up to 1.
    result = _evaluate(api, tmp_path, {"kind": "number_close", "path": "r.json", "pointer": "/v",
                                     "expected": expected, "rtol": (2**53 - 3) / 2**106})
    assert result["passed"] is False
    if api == "legacy":
        assert result["abs_error"] == 1


@pytest.mark.parametrize("actual,expected,passed", [(90, 100, True), (100, 90, False)])
def test_shared_asymmetric_tolerance_contract_is_preserved(tmp_path, actual, expected, passed):
    (tmp_path / "r.json").write_text(json.dumps({"v": actual}), encoding="utf-8")
    result = run_check(tmp_path, {"kind": "number_close", "path": "r.json", "pointer": "/v",
                                 "expected": expected, "rtol": 0.11}, 0)
    assert result["passed"] is passed


@pytest.mark.parametrize("api", ["jobs", "legacy"])
@pytest.mark.parametrize("pointer", ["/a/-1", "/a/01", "/a/+1", "/a/١", "/a/-", "/a/2", "/bad~2key"])
def test_invalid_json_pointer_cannot_select_a_value(tmp_path, api, pointer):
    (tmp_path / "r.json").write_text('{"a": [7,9], "bad~2key": 9}', encoding="utf-8")
    assert not _evaluate(api, tmp_path, {"kind": "json_value", "path": "r.json", "pointer": pointer, "expected": 9})["passed"]


@pytest.mark.parametrize("api", ["jobs", "legacy"])
@pytest.mark.parametrize("pointer,expected", [("//v", 2), ("/a~1b/~0/0", 7), ("/literal~01", 8)])
def test_json_pointer_preserves_empty_tokens_and_decodes_escapes_once(tmp_path, api, pointer, expected):
    doc = {"": {"v": 2}, "v": 1, "a/b": {"~": [7]}, "literal~1": 8}
    (tmp_path / "r.json").write_text(json.dumps(doc), encoding="utf-8")
    assert _evaluate(api, tmp_path, {"kind": "json_value", "path": "r.json", "pointer": pointer, "expected": expected})["passed"]
    assert json_pointer(doc, pointer) == expected


def test_dictionary_numeric_keys_are_not_array_indexes():
    assert json_pointer({"a": {"01": 1, "-1": 2, "١": 3}}, "/a/01") == 1
    assert json_pointer({"a": {"01": 1, "-1": 2, "١": 3}}, "/a/-1") == 2
    assert json_pointer({"a": {"01": 1, "-1": 2, "١": 3}}, "/a/١") == 3


@pytest.mark.parametrize("api", ["jobs", "legacy"])
def test_slash_whole_document_alias_remains_compatible(tmp_path, api):
    doc = {"": "not the whole document", "v": 1}
    (tmp_path / "r.json").write_text(json.dumps(doc), encoding="utf-8")
    assert _evaluate(api, tmp_path, {"kind": "json_value", "path": "r.json", "pointer": "/", "expected": doc})["passed"]
    assert json_pointer(doc, "/") == doc[""]  # low-level RFC lookup remains separate from the evaluator alias


def test_legacy_api_keeps_regex_negative_existence_and_exit_defaults(tmp_path):
    (tmp_path / "log").write_text("aXb", encoding="utf-8")
    raw = {"name": "regex", "kind": "text_contains", "path": "log", "expected": "a.b"}
    assert legacy.validate(raw) is raw
    assert legacy.evaluate(raw, tmp_path)["passed"]
    assert not run_check(tmp_path, raw, 0)["passed"]
    checks = [{"name": "absent", "kind": "file_exists", "path": "missing", "expected": False},
              {"name": "exit", "kind": "exit_code"}]
    assert all(result["passed"] for result in legacy.evaluate_all(checks, tmp_path, 0))
    assert not run_check(tmp_path, checks[0], 0)["passed"]


def test_legacy_numeric_defaults_and_error_outputs_are_preserved(tmp_path):
    (tmp_path / "r.json").write_text('{"v": 2.5}', encoding="utf-8")
    raw = {"name": "number", "kind": "number_close", "path": "r.json", "pointer": "/v", "expected": 2}
    result = legacy.evaluate(raw, tmp_path)
    assert not result["passed"] and result["abs_error"] == 0.5 and result["rel_error"] == 0.25
    assert legacy.evaluate({**raw, "atol": 0.5}, tmp_path)["passed"]
    (tmp_path / "r.json").write_text('{"v": 1.0000000005}', encoding="utf-8")
    raw["expected"] = 1
    assert run_check(tmp_path, raw, 0)["passed"] and not legacy.evaluate(raw, tmp_path)["passed"]
    raw.update(kind="json_value", rtol=1)
    assert not legacy.evaluate(raw, tmp_path)["passed"]  # legacy json_value remains exact


@pytest.mark.parametrize("actual,expected", [(1e308, -1e308), (1.0, 5e-324), (10**400 + 1, 10**400)])
def test_legacy_unrepresentable_error_diagnostic_is_not_published(tmp_path, actual, expected):
    (tmp_path / "r.json").write_text(json.dumps({"v": actual}), encoding="utf-8")
    result = legacy.evaluate({"name": "error", "kind": "number_close", "path": "r.json", "pointer": "/v",
                              "expected": expected}, tmp_path)
    assert not result["passed"] and "diagnostic" in result
    json.dumps(result, allow_nan=False)


def test_legacy_json_actual_retains_its_original_shape(tmp_path):
    doc = {"v": ["finite" * 100]}
    (tmp_path / "r.json").write_text(json.dumps(doc), encoding="utf-8")
    result = legacy.evaluate({"name": "long", "kind": "json_value", "path": "r.json", "expected": doc}, tmp_path)
    assert result["passed"] and result["actual"] == doc


def test_runner_receipt_with_nonfinite_output_is_failed_and_strict_json(tmp_path):
    from jobs_fakes import service, submit

    jobs = service(tmp_path)
    ref, draft = submit(jobs, "import pathlib; pathlib.Path('../out/result.json').write_text('{\"v\": Infinity}')",
                        wait_s=60, checks=[{"name": "finite", "kind": "number_close", "path": "out/result.json",
                                            "pointer": "/v", "expected": 1}])
    assert ref["status"] == "CHECK_FAILED"
    data = (draft.dir / "receipt.json").read_text(encoding="utf-8")
    receipt = json.loads(data)
    assert receipt["checks"][0]["actual"] is None and not receipt["checks"][0]["passed"]
    json.dumps(receipt, allow_nan=False)


@pytest.mark.parametrize("malformed", [{"kind": "number_close", "path": "out/result.json", "expected": float("inf")}, []])
def test_runner_keeps_rejected_saved_check_evidence(tmp_path, malformed):
    from jobs_fakes import service, submit

    jobs = service(tmp_path)
    spawn = jobs._spawner

    def mutate_saved_check(argv, cwd, env, log):
        saved = cwd / "job.json"
        spec = json.loads(saved.read_text(encoding="utf-8"))
        spec["checks"] = [malformed]
        saved.write_text(json.dumps(spec), encoding="utf-8")
        return spawn(argv, cwd, env, log)

    jobs._spawner = mutate_saved_check
    ref, draft = submit(jobs, "import pathlib; pathlib.Path('../out/result.json').write_text('1')", wait_s=60)
    assert ref["status"] == "CHECK_FAILED"
    receipt = json.loads((draft.dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["checks_passed"] is False and len(receipt["checks"]) == 1
    result = receipt["checks"][0]
    assert isinstance(result["name"], str) and result["name"]
    assert result["actual"] is None and result["passed"] is False and "INVALID_ARGUMENT" in result["message"]
    json.dumps(receipt, allow_nan=False)


@pytest.mark.parametrize("actual,expected", [(2**53 + 1, float(2**53)), (float(2**53), 2**53 + 1)])
def test_runner_receipt_rejects_mixed_numeric_precision_loss(tmp_path, actual, expected):
    from jobs_fakes import service, submit

    jobs = service(tmp_path)
    output = json.dumps({"v": actual})
    code = f"import pathlib; pathlib.Path('../out/result.json').write_text({output!r})"
    ref, draft = submit(jobs, code, wait_s=60, checks=[{"name": "precision", "kind": "number_close",
                        "path": "out/result.json", "pointer": "/v", "expected": expected, "rtol": 0, "atol": 0.5}])
    assert ref["status"] == "CHECK_FAILED"
    receipt = json.loads((draft.dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["checks_passed"] is False and receipt["checks"][0]["passed"] is False
    json.dumps(receipt, allow_nan=False)
