"""Qualification uses declared synthetic fixtures, never real corpus quality claims."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vkm_evidence.contracts import OriginSlice, record_hash
from vkm_evidence.qualification import (EXACT, EvalObject, FrozenPlan, FrozenRegistration, GoldSet, GoldStratum,
    GridField, MatchAdjudication, MatchStratum, NumericField, PredictionSet, StratumKey, StratumPlan, StratumResult,
    Threshold, evaluate_qualification, wilson_interval)
from vkm_evidence.qualification import qualification_gate

SHA = "a" * 64
T = datetime(2026, 10, 1, tzinfo=timezone.utc)
BASE_METRICS = ("DISCOVERY_RECALL", "DISCOVERY_PRECISION", "CLASSIFICATION_ACCURACY")


def origin(name="evaluation", members=()):
    return OriginSlice(origin_id=name, members=members, independence_basis="synthetic reviewed primary",
                       verified=True)


def obj(object_id="gold-1", **kwargs):
    return EvalObject(object_id=object_id, unit_id="unit-1", locator="original:xpath:1", classification="TABLE", **kwargs)


def bundle(*, gold_objects=None, predicted_objects=None, metrics=BASE_METRICS, matches=None,
           confidence=.95, design="POSITIVE_OBJECTS", purpose="INDEPENDENT_EXTRACTION_VALIDATION",
           gold_origins=None, tuning_origins=None, minimum=1, statistic="POINT"):
    key = StratumKey(source_id="synthetic-001", source_sha256=SHA, format="PDF_NATIVE", quality="CLEAN",
                     object_kind="TABLE")
    gobjects = (obj(),) if gold_objects is None else gold_objects
    pobjects = (obj("prediction-1"),) if predicted_objects is None else predicted_objects
    gold = GoldSet(population="SYNTHETIC", authors=("gold-author",), reviewers=("gold-reviewer",),
        strata=(GoldStratum(stratum_id="native-table", key=key, unit_ids=("unit-1",), status="COMPLETE",
            objects=gobjects, origins=gold_origins or (origin(),), original_review="VERIFIED_FROM_ORIGINAL",
            review_receipt_sha256=SHA),))
    plan = FrozenPlan(plan_id="synthetic-plan", population="SYNTHETIC", purpose=purpose,
        gold_sha256=record_hash(gold), extractor_identity_sha256=SHA, producer_actors=("extractor",),
        tuning_actors=("tuner",), tuning_state="DECLARED", tuning_origins=tuning_origins or (origin("training"),),
        confidence=confidence, strata=(StratumPlan(stratum_id="native-table", key=key, unit_ids=("unit-1",),
            design=design, sampling_basis="synthetic explicit unit",
            thresholds=tuple(Threshold(metric=m, minimum=minimum, minimum_denominator=1,
                                        decision_statistic=statistic) for m in metrics)),))
    prediction = PredictionSet(plan_sha256=plan.sha256, extractor_identity_sha256=SHA, producer_actors=("extractor",),
        started_at=T + timedelta(seconds=1), strata=(StratumResult(stratum_id="native-table", key=key,
            unit_ids=("unit-1",), status="COMPLETE", objects=pobjects),))
    matched = ({"gold_object_id": "gold-1", "prediction_object_id": "prediction-1"},) if matches is None else matches
    review = MatchAdjudication(plan_sha256=plan.sha256, gold_sha256=record_hash(gold),
        prediction_sha256=record_hash(prediction), reviewers=("matcher",), reviewed_at=T + timedelta(seconds=2),
        strata=(MatchStratum(stratum_id="native-table", status="COMPLETE", matches=matched),))
    registration = FrozenRegistration(plan_sha256=plan.sha256, gold_sha256=record_hash(gold),
        registered_at=T, registrar="registrar", durable_receipt_sha256=SHA)
    return plan, gold, prediction, review, registration


def evaluate(values, verifier=lambda _: True):
    return evaluate_qualification(*values, verify_registration=verifier)


def rebind(plan, gold, prediction, review, registration):
    """A NEW synthetic preregistration, not mutation permitted by the evaluator."""
    plan = plan.model_copy(update={"gold_sha256": record_hash(gold)})
    prediction = prediction.model_copy(update={"plan_sha256": plan.sha256})
    review = review.model_copy(update={"plan_sha256": plan.sha256, "gold_sha256": record_hash(gold),
                                     "prediction_sha256": record_hash(prediction)})
    registration = registration.model_copy(update={"plan_sha256": plan.sha256, "gold_sha256": record_hash(gold)})
    return plan, gold, prediction, review, registration


def test_perfect_synthetic_is_only_synthetic_not_scientific_admission():
    report = evaluate(bundle())
    assert report["status"] == "PASS" and report["qualification"] == "SYNTHETIC_ONLY"
    assert report["scientific_admission"] == report["field_validation"] == "NOT_ESTABLISHED"
    assert report["strata"]["native-table"]["metrics"]["DISCOVERY_RECALL"]["denominator"] == 1
    digest = report.pop("report_sha256")
    assert digest == record_hash(report)


def test_discovery_false_positives_misses_and_conditional_classification_are_separate():
    gold = (obj(), obj("gold-2"))
    predicted = (obj("prediction-1").model_copy(update={"classification": "FIGURE"}), obj("prediction-extra"))
    report = evaluate(bundle(gold_objects=gold, predicted_objects=predicted))
    m = report["strata"]["native-table"]["metrics"]
    assert (m["DISCOVERY_RECALL"]["numerator"], m["DISCOVERY_RECALL"]["denominator"]) == (1, 2)
    assert (m["DISCOVERY_PRECISION"]["numerator"], m["DISCOVERY_PRECISION"]["denominator"]) == (1, 2)
    assert (m["CLASSIFICATION_ACCURACY"]["numerator"], m["CLASSIFICATION_ACCURACY"]["denominator"]) == (0, 1)
    assert m["CLASSIFICATION_ACCURACY"]["unmatched_gold"] == 1
    assert report["status"] == "BLOCKED"


def fields():
    return (
        {"metric": "NUMERIC_EXACT", "key": "number", "literal": "1,20", "state": "VALUE", "decimal_value": "1.20", "unit": None},
        {"metric": "GRID_EXACT", "key": "grid", "rows": 1, "columns": 2,
         "cells": ({"row": 0, "col": 0, "text": "a", "state": "VALUE"}, {"row": 0, "col": 1, "text": "", "state": "EMPTY"})},
        {"metric": "FORMULA_EXACT", "key": "equation", "representation": "LATEX", "literal": "x^2", "bindings": (("x", "distance", "figure-1"),)},
        {"metric": "ENTITY_EXACT", "key": "identity", "entity_id": "zone-7", "scope": "mine-A", "state": "RESOLVED"},
        {"metric": "TEMPORAL_EXACT", "key": "history", "support": {"event_date": "2020-01-01", "available_from": "2021-01-01", "precision": "year"}},
    )


def test_all_five_exact_domains_are_independent_and_do_not_leak_raw_values_into_report():
    expected = obj(fields=fields())
    changed = list(fields())
    changed[0] = {**changed[0], "unit": "m"}  # UNKNOWN must not become metre
    changed[1] = {**changed[1], "cells": ({"row": 0, "col": 0, "text": "a", "colspan": 2, "state": "VALUE"},)}
    changed[2] = {**changed[2], "literal": "x^3"}
    changed[3] = {**changed[3], "scope": "mine-B"}
    changed[4] = {**changed[4], "support": {"event_date": "2020-01-01", "available_from": "2020-01-01", "precision": "year"}}
    values = bundle(gold_objects=(expected,), predicted_objects=(obj("prediction-1", fields=changed),), metrics=BASE_METRICS + EXACT)
    report = evaluate(values)
    measured = report["strata"]["native-table"]["metrics"]
    assert measured["DISCOVERY_RECALL"]["point"] == 1
    for metric in EXACT:
        assert (measured[metric]["numerator"], measured[metric]["denominator"], measured[metric]["incorrect"]) == (0, 1, 1)
    assert report["status"] == "BLOCKED"
    assert "1,20" not in str(report) and "mine-B" not in str(report) and "x^3" not in str(report)


def test_missing_and_hallucinated_fields_penalize_exact_metrics():
    numeric = fields()[0]
    expected = obj(fields=(numeric,))
    actual = obj("prediction-1", fields=({**numeric, "key": "hallucinated"},))
    report = evaluate(bundle(gold_objects=(expected,), predicted_objects=(actual,), metrics=BASE_METRICS + ("NUMERIC_EXACT",)))
    metric = report["strata"]["native-table"]["metrics"]["NUMERIC_EXACT"]
    assert (metric["numerator"], metric["denominator"], metric["missing"], metric["unexpected"]) == (0, 2, 1, 1)


def test_zero_denominator_and_missing_execution_never_pass():
    report = evaluate(bundle(predicted_objects=(), matches=()))
    assert report["status"] == "BLOCKED"
    m = report["strata"]["native-table"]["metrics"]["DISCOVERY_PRECISION"]
    assert m["denominator"] == 0 and m["point"] is None and m["status"] == "NOT_RUN"
    values = list(bundle())
    values[2] = None
    report = evaluate(values)
    assert report["status"] == "NOT_RUN" and report["qualification"] == "NOT_QUALIFIED"
    assert all(g["status"] == "NOT_RUN" for g in report["strata"]["native-table"]["gates"])


@pytest.mark.parametrize("missing_index", [1, 2, 3])
def test_missing_required_stratum_blocks_without_reducing_denominator(missing_index):
    values = list(bundle())
    values[missing_index] = values[missing_index].model_copy(update={"strata": ()})
    report = evaluate(values)
    assert report["status"] == "BLOCKED" and "native-table:STRATUM_NOT_RUN" in report["reasons"]


@pytest.mark.parametrize("role,actor", [("authors", "Extractor"), ("reviewers", "TUNER")])
def test_gold_reviewers_and_authors_cannot_be_tuning_or_extractor_actors(role, actor):
    values = list(bundle())
    values[1] = values[1].model_copy(update={role: (actor,)})
    report = evaluate(rebind(*values))
    assert "EVALUATOR_PRODUCER_ACTOR_OVERLAP" in report["reasons"] and report["status"] == "BLOCKED"


def test_match_reviewer_cannot_be_extractor():
    values = list(bundle())
    values[3] = values[3].model_copy(update={"reviewers": ("extractor",)})
    assert "EVALUATOR_PRODUCER_ACTOR_OVERLAP" in evaluate(values)["reasons"]


def test_registration_is_before_prediction_and_verified_no_default_trust():
    values = bundle()
    assert evaluate(values, None)["status"] == "BLOCKED"
    changed = values[-1].model_copy(update={"registered_at": T + timedelta(seconds=1)})
    report = evaluate((*values[:-1], changed))
    assert "PLAN_NOT_FROZEN_BEFORE_PREDICTION" in report["reasons"]
    def verifier(_):
        raise OSError("unavailable external registration")
    assert "PREREGISTRATION_NOT_VERIFIED" in evaluate(values, verifier)["reasons"]


@pytest.mark.parametrize("target", ["gold", "prediction", "plan"])
def test_frozen_artifact_change_is_not_requalification(target):
    values = list(bundle())
    if target == "gold":
        values[1] = values[1].model_copy(update={"authors": ("new-author",)})
    elif target == "prediction":
        values[2] = values[2].model_copy(update={"started_at": T + timedelta(seconds=3)})
    else:
        s = values[0].strata[0]
        relaxed = tuple(t.model_copy(update={"minimum": 0}) for t in s.thresholds)
        values[0] = values[0].model_copy(update={"strata": (s.model_copy(update={"thresholds": relaxed}),)})
    assert evaluate(values)["status"] == "BLOCKED"


@pytest.mark.parametrize("field,value", [("source_sha256", "b" * 64), ("format", "PDF_SCAN"), ("quality", "DEGRADED"),
                                        ("object_kind", "FORMULA")])
def test_source_version_quality_and_unit_identity_cannot_be_swapped(field, value):
    values = list(bundle())
    row = values[2].strata[0]
    values[2] = values[2].model_copy(update={"strata": (row.model_copy(update={"key": row.key.model_copy(update={field: value})}),)})
    report = evaluate(rebind(*values))
    assert "native-table:STRATUM_SOURCE_VERSION_OR_DENOMINATOR_CHANGED" in report["reasons"]


def test_primary_shared_disjoint_members_are_not_independent_validation():
    kwargs = {"gold_origins": (origin("survey", ("point-2",)),), "tuning_origins": (origin("survey", ("point-1",)),)}
    report = evaluate(bundle(**kwargs))
    assert report["strata"]["native-table"]["primary_lineage"] == "DISJOINT_SHARED_ORIGIN"
    assert report["status"] == "BLOCKED"
    assert evaluate(bundle(**kwargs, purpose="SHARED_ORIGIN_HOLDOUT"))["status"] == "PASS"
    overlap = {"gold_origins": (origin("survey", ("point-1",)),), "tuning_origins": (origin("survey"),)}
    assert evaluate(bundle(**overlap, purpose="SHARED_ORIGIN_HOLDOUT"))["status"] == "BLOCKED"
    report = evaluate(bundle(**overlap, purpose="TRANSFER_CONSISTENCY"))
    assert report["status"] == "PASS" and report["purpose"] == "TRANSFER_CONSISTENCY"


def test_unknown_primary_origin_is_not_independent():
    report = evaluate(bundle(gold_origins=(origin().model_copy(update={"verified": False}),)))
    assert report["status"] == "BLOCKED"
    assert report["strata"]["native-table"]["primary_lineage"] == "UNKNOWN"


def test_wilson_interval_zero_and_known_all_success_example():
    assert wilson_interval(0, 0, .95) is None
    lower, upper = wilson_interval(100, 100, .95)
    assert lower == pytest.approx(.9630065017930143) and upper == pytest.approx(1)
    assert wilson_interval(0, 100, .95)[1] == pytest.approx(1 - lower)
    report = evaluate(bundle(statistic="WILSON_LOWER", minimum=.9))
    assert report["status"] == "BLOCKED"  # one perfect sample is weak evidence
    for args in ((1, 0, .95), (-1, 1, .95), (1, 1, 1), (True, 1, .95)):
        with pytest.raises(ValueError):
            wilson_interval(*args)


def test_negative_units_use_explicit_absence_metric_instead_of_nan_precision_pass():
    kwargs = {"gold_objects": (), "predicted_objects": (), "matches": (), "metrics": ("EMPTY_UNIT_ACCURACY",),
              "design": "NEGATIVE_UNITS"}
    assert evaluate(bundle(**kwargs))["status"] == "PASS"
    kwargs["predicted_objects"] = (obj("hallucination"),)
    assert evaluate(bundle(**kwargs))["status"] == "BLOCKED"


def test_exact_metric_cannot_be_silently_removed_from_frozen_thresholds():
    values = bundle(gold_objects=(obj(fields=(fields()[0],)),), predicted_objects=(obj("prediction-1", fields=(fields()[0],)),))
    assert "native-table:EXACT_METRIC_WITHOUT_FROZEN_THRESHOLD:NUMERIC_EXACT" in evaluate(values)["reasons"]


def test_matching_is_one_to_one_and_cannot_borrow_another_original_unit():
    with pytest.raises(ValueError, match="duplicate matched"):
        MatchStratum(stratum_id="s", status="COMPLETE", matches=(
            {"gold_object_id": "a", "prediction_object_id": "b"}, {"gold_object_id": "c", "prediction_object_id": "b"}))
    values = list(bundle())
    a = values[3].strata[0]
    a = a.model_copy(update={"matches": (a.matches[0].model_copy(update={"gold_object_id": "absent"}),)})
    values[3] = values[3].model_copy(update={"strata": (a,)})
    assert "native-table:INVALID_ONE_TO_ONE_ADJUDICATION" in evaluate(values)["reasons"]


def test_grid_preserves_empty_cells_and_rejects_overlap_holes_or_fabricated_zero():
    with pytest.raises(ValueError, match="empty and merged"):
        GridField(key="grid", rows=1, columns=2, cells=({"row": 0, "col": 0, "text": "x", "state": "VALUE"},))
    with pytest.raises(ValueError, match="overlap"):
        GridField(key="grid", rows=1, columns=2, cells=(
            {"row": 0, "col": 0, "text": "x", "state": "VALUE"}, {"row": 0, "col": 0, "text": "", "state": "EMPTY"}))
    for value in ("NaN", "Infinity", "1,2"):
        with pytest.raises(ValueError):
            NumericField(key="x", literal=value, state="VALUE", decimal_value=value)
    with pytest.raises(ValueError):
        NumericField(key="x", literal="", state="EMPTY", decimal_value="0")


def test_grid_span_check_is_bounded_by_description_not_expanded_coordinate_count():
    grid = GridField(key="synthetic-huge-span", rows=10**9, columns=257,
        cells=({"row": 0, "col": 0, "rowspan": 10**9, "colspan": 257, "text": "", "state": "EMPTY"},))
    assert len(grid.cells) == 1


def test_failed_and_not_run_stratum_cannot_be_ignored_by_macro_average():
    values = list(bundle())
    original = values[2].strata[0]
    values[2] = values[2].model_copy(update={"strata": (original.model_copy(update={"status": "FAILED", "reason": "synthetic parser failure"}),)})
    report = evaluate(rebind(*values))
    assert report["status"] == "BLOCKED"
    assert all(g["status"] == "NOT_RUN" for g in report["strata"]["native-table"]["gates"])


def test_synthetic_gold_cannot_qualify_corpus_or_claim_unmeasured_fields():
    values = list(bundle())
    values[0] = values[0].model_copy(update={"population": "CORPUS"})
    report = evaluate(rebind(*values))
    assert "POPULATION_MISMATCH" in report["reasons"]
    assert report["qualification"] == "NOT_QUALIFIED"
    report = evaluate(bundle())
    assert report["qualified_scope"] == {"native-table": list(BASE_METRICS)}


def test_exact_decimal_spelling_and_precision_are_not_silently_normalized():
    expected = fields()[0]
    actual = {**expected, "decimal_value": "1.2"}
    report = evaluate(bundle(gold_objects=(obj(fields=(expected,)),),
        predicted_objects=(obj("prediction-1", fields=(actual,)),), metrics=BASE_METRICS + ("NUMERIC_EXACT",)))
    assert report["strata"]["native-table"]["metrics"]["NUMERIC_EXACT"]["incorrect"] == 1


def test_production_gate_rejects_synthetic_pass_and_requires_explicit_frozen_scope():
    values = bundle()
    report = evaluate(values)
    prediction_sha = record_hash(values[2])
    assert qualification_gate(report, values[0], prediction_sha) is False
    assert qualification_gate(report, values[0], prediction_sha, require_corpus=False) is True
    assert qualification_gate(report, values[0], prediction_sha, require_corpus=False,
        required_scope={"native-table": ("NUMERIC_EXACT",)}) is False
    assert qualification_gate(report, values[0], "d" * 64, require_corpus=False) is False


def test_gate_checks_metric_status_and_recomputes_statistics_even_with_rehashed_report():
    values = bundle()
    report = evaluate(values)
    report["strata"]["native-table"]["metrics"]["DISCOVERY_RECALL"]["status"] = "NOT_RUN"
    report["report_sha256"] = record_hash({k: v for k, v in report.items() if k != "report_sha256"})
    assert not qualification_gate(report, values[0], record_hash(values[2]), require_corpus=False)
    report = evaluate(values)
    report["strata"]["native-table"]["metrics"]["DISCOVERY_RECALL"]["wilson"] = [1, 1]
    report["report_sha256"] = record_hash({k: v for k, v in report.items() if k != "report_sha256"})
    assert not qualification_gate(report, values[0], record_hash(values[2]), require_corpus=False)


def test_runtime_parses_bound_plan_prediction_and_keeps_require_corpus(tmp_path, monkeypatch):
    import hashlib
    from vkm_corpus.update.runtime import BoundFile, QualityGateBinding, RuntimeConfig, UpdateRuntime
    from vkm_evidence.contracts import canonical_bytes
    values = bundle()
    report = evaluate(values)
    def save(name, value):
        data = canonical_bytes(value)
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return BoundFile(path=str(path), sha256=hashlib.sha256(data).hexdigest())
    plan_ref, prediction_ref = save("plan.json", values[0]), save("prediction.json", values[2])
    report_ref = save("report.json", report)
    save("receipts/" + report_ref.sha256 + ".json", report)
    policy = save("policy.json", {})
    rt = UpdateRuntime(RuntimeConfig(runtime_root=str(tmp_path / "runtime"), originals_root=str(tmp_path / "originals"),
        policy=policy, qualification_root=str(tmp_path / "receipts"), expected_commit="b" * 40,
        memory_budget_gib=3, memory_reserve_gib=1, worker_memory_gib=2, min_free_disk_gib=1,
        artifacts={"plan": plan_ref, "prediction": prediction_ref},
        quality_gates={report_ref.sha256: QualityGateBinding(plan_artifact="plan", prediction_artifact="prediction",
            required_scope={"native-table": BASE_METRICS})}))
    calls = []
    original_gate = qualification_gate
    def observed_gate(report, plan, prediction_sha, **kwargs):
        calls.append((plan.sha256, prediction_sha, kwargs))
        return original_gate(report, plan, prediction_sha, **kwargs)
    monkeypatch.setattr("vkm_evidence.qualification.qualification_gate", observed_gate)
    assert rt._gate(report_ref.sha256) is False  # complete SYNTHETIC still not CORPUS
    assert calls == [(values[0].sha256, record_hash(values[2]), {"require_corpus": True,
        "required_scope": {"native-table": BASE_METRICS}})]
    (tmp_path / "prediction.json").write_text("{}")
    assert rt._gate(report_ref.sha256) is False and len(calls) == 1


def test_easy_stratum_cannot_hide_failed_degraded_source_version():
    values = list(bundle())
    key = values[0].strata[0].key.model_copy(update={"source_sha256": "d" * 64, "format": "PDF_SCAN", "quality": "DEGRADED"})
    scope = values[0].strata[0].model_copy(update={"stratum_id": "degraded", "key": key})
    gs = values[1].strata[0].model_copy(update={"stratum_id": "degraded", "key": key})
    ps = values[2].strata[0].model_copy(update={"stratum_id": "degraded", "key": key,
        "objects": (obj("prediction-1").model_copy(update={"classification": "WRONG"}),)})
    ms = values[3].strata[0].model_copy(update={"stratum_id": "degraded"})
    values[0] = values[0].model_copy(update={"strata": (*values[0].strata, scope)})
    values[1] = values[1].model_copy(update={"strata": (*values[1].strata, gs)})
    values[2] = values[2].model_copy(update={"strata": (*values[2].strata, ps)})
    values[3] = values[3].model_copy(update={"strata": (*values[3].strata, ms)})
    values = rebind(*values)
    original_gold_sha = record_hash(values[1])
    first = evaluate(values)
    assert first["strata"]["native-table"]["status"] == "PASS"
    assert first["strata"]["degraded"]["status"] == "BLOCKED"
    assert first["status"] == "BLOCKED" and first == evaluate(values)
    assert record_hash(values[1]) == original_gold_sha
