"""Data-free tests of vkm_world.core.io and vkm_world.validation (guards, designs, access, metrics)."""
import hashlib
import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from vkm_world.chronology.events import Event, EventType, known_at
from vkm_world.core.io import (PathPolicyError, artifact_inventory, resolve_repo_path, snapshot_paths,
                               write_csv_atomic, write_json_atomic, write_text_atomic)
from vkm_world.core.provenance import EpistemicStatus as S, Provenance, SourceRef, TemporalSupport
from vkm_world.validation.access import (CandidateFreezeError, RepeatedTestAccessError, authorize_test_access,
                                         build_candidate_record, claim_test_access, finalize_test_access,
                                         freeze_candidate, verify_candidate_artifacts, verify_candidate_record)
from vkm_world.validation.leakage import (LeakageViolation, assert_available_at, assert_disjoint_sample_sets,
                                          assert_feature_fields_safe, assert_planned_target,
                                          assert_positive_horizon, assert_time_alignment, availability_reason,
                                          find_forbidden_split_api_usage, forbidden_field_reason, planned_target)
from vkm_world.validation.metrics import (conformal_quantile, interval_coverage, interval_score, normal_crps,
                                          normal_nll, point_metrics, weighted_interval_score)
from vkm_world.validation.splits import (FoldAssignment, SealedTestError, UnsafeSplitError, assert_forward_only,
                                         leave_one_borehole_out, leave_one_line_out, reject_plain_kfold,
                                         reject_random_train_test_split, rolling_origin_assignments,
                                         sample_id_list_sha256, validate_splitter_name)

ROOT = Path(__file__).resolve().parents[2]
FACT = Provenance(status=S.FACT, sources=(SourceRef(source_id="VKM-SRC-012", pdf_page=40),))
H = "a" * 64  # placeholder sha256 values for data-free records


# ================================================================ core.io
@pytest.mark.parametrize("bad", ["/etc/passwd", "E:\\data\\x.csv", "C:/x.csv", "E:x.csv", "\\\\server\\share\\x",
                                 "../x.csv", "a/../../x.csv", "a\\b.csv", ""])
def test_resolve_repo_path_rejects_absolute_windows_and_escape(tmp_path, bad):
    with pytest.raises(PathPolicyError):
        resolve_repo_path(tmp_path, bad)


def test_resolve_repo_path_rejects_symlink_escape(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir(), outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert resolve_repo_path(root, "a/b.csv") == (root / "a" / "b.csv").resolve()
    with pytest.raises(PathPolicyError, match="escapes"):
        resolve_repo_path(root, "link/x.csv")


def test_atomic_writers_inventory_and_snapshot(tmp_path):
    write_text_atomic(tmp_path, "out/a.txt", "строка\n")
    write_json_atomic(tmp_path, "out/b.json", {"z": 1, "a": date(2020, 1, 2), "p": Path("x/y")})
    write_csv_atomic(tmp_path, "out/c.csv", [{"id": "1", "v": 0.5}, {"id": "2", "v": None}], ["id", "v"])
    assert (tmp_path / "out/a.txt").read_bytes() == "строка\n".encode()
    assert (tmp_path / "out/b.json").read_text(encoding="utf-8") == \
        '{\n  "a": "2020-01-02",\n  "p": "x/y",\n  "z": 1\n}\n'
    assert (tmp_path / "out/c.csv").read_bytes() == b"id,v\n1,0.5\n2,\n"
    assert not list((tmp_path / "work").rglob("*.tmp"))              # temporaries only in work/, cleaned
    with pytest.raises(ValueError):
        write_csv_atomic(tmp_path, "out/d.csv", [{"id": 1, "extra": 2}], ["id"])
    with pytest.raises(PathPolicyError):
        write_text_atomic(tmp_path, tmp_path.parent / "escape.txt", "x")

    inv = artifact_inventory(tmp_path, ["out/c.csv", tmp_path / "out/a.txt", "out/c.csv"])
    assert [r["path"] for r in inv] == ["out/a.txt", "out/c.csv"]
    assert inv[0]["sha256"] == hashlib.sha256("строка\n".encode()).hexdigest()
    with pytest.raises(FileNotFoundError):
        artifact_inventory(tmp_path, ["out/missing.csv"])

    s1, s2 = snapshot_paths(tmp_path, ["out"]), snapshot_paths(tmp_path, ["out/"])
    assert s1 == s2 and s1["file_count"] == 3
    (tmp_path / "out/a.txt").write_text("changed\n", encoding="utf-8")
    assert snapshot_paths(tmp_path, ["out"])["snapshot_sha256"] != s1["snapshot_sha256"]
    with pytest.raises(FileNotFoundError):
        snapshot_paths(tmp_path, ["nope"])


# ================================================================ leakage: fields, time, sets, code
@pytest.mark.parametrize("field", ["true_rate_mm_y", "hidden_regime", "generator_tau", "private_note",
                                   "next_rate_mm_y", "future_load", "rate_target", "point_id", "sample_id",
                                   "current_campaign_id", "label_status", "missing_reason"])
def test_forbidden_estimator_fields(field):
    assert forbidden_field_reason(field)
    with pytest.raises(LeakageViolation):
        assert_feature_fields_safe(["last_rate_mm_y", field])


def test_allowed_and_planned_fields_pass():
    for field in ("last_rate_mm_y", "depth_m", "forecast_horizon_days", "target_campaign_type"):
        assert forbidden_field_reason(field) is None
    assert_feature_fields_safe(["last_rate_mm_y", "forecast_horizon_days"])
    with pytest.raises(LeakageViolation, match="explicitly forbidden"):
        assert_feature_fields_safe(["terminal_map_mm"], extra_forbidden={"terminal_map_mm"})


def test_time_alignment_requires_origin_before_target_and_positive_horizon():
    assert_time_alignment(["2020-01-01", date(2020, 2, 1)], ["2020-03-01", "2020-04-01"], [60, 60])
    for origins, targets, horizons in ((["2020-03-01"], ["2020-03-01"], None),    # target == origin
                                       (["2020-03-01"], ["2020-01-01"], None),    # target before origin
                                       ([None], ["2020-01-01"], None),            # unknown origin
                                       (["2020-01-01"], ["2020-03-01"], [59])):   # horizon mismatch
        with pytest.raises(LeakageViolation):
            assert_time_alignment(origins, targets, horizons)
    assert_positive_horizon([1, 30.0])
    for bad in ([0], [-5], [None], [float("nan")]):
        with pytest.raises(LeakageViolation):
            assert_positive_horizon(bad)


def test_cross_split_overlap_is_rejected():
    assert_disjoint_sample_sets({"train": ["a", "b"], "validation": ["c"], "test": ["d"]})
    with pytest.raises(LeakageViolation, match="train<->validation:1"):
        assert_disjoint_sample_sets({"train": ["a", "b"], "validation": ["b", "c"]})


def test_source_scanner_finds_forbidden_split_calls(tmp_path):
    src = tmp_path / "bad_model.py"
    src.write_text(
        "from sklearn.model_selection import train_test_split, KFold\n"
        "# KFold( in a comment is not a call\n"
        "doc = 'train_test_split(X, y, shuffle=True)'\n"
        "parts = train_test_split(X, y, shuffle=True)\n"
        "cv = sklearn.model_selection.KFold(5)\n"
        "ok = GroupKFold(5)\n", encoding="utf-8")
    found = find_forbidden_split_api_usage([src])
    assert {(f.line, f.api) for f in found} == {(4, "train_test_split"), (4, "shuffle=True"), (5, "KFold")}
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n", encoding="utf-8")
    assert [f.api for f in find_forbidden_split_api_usage([broken])] == ["unparseable"]


def test_new_code_has_no_forbidden_split_calls():
    sources = [*(ROOT / "src" / "vkm_world").rglob("*.py"), *(ROOT / "tests" / "world").rglob("*.py")]
    assert find_forbidden_split_api_usage(sources) == []


# ================================================================ availability at origin
ORIGIN = date(2020, 6, 1)


@pytest.mark.parametrize("support", [
    None,
    TemporalSupport(),                                               # availability unknown
    TemporalSupport(measurement_date=date(2019, 12, 1)),             # measured earlier, availability unknown
    TemporalSupport(publication_date=date(2019, 1, 1)),              # publication year is not availability
    TemporalSupport(measurement_date=date(2020, 5, 1), available_from=date(2020, 9, 1)),  # known future
])
def test_unknown_or_future_availability_is_unusable(support):
    if support is not None:
        assert support.usable_at(ORIGIN) is not True
    assert availability_reason(support, ORIGIN)
    with pytest.raises(LeakageViolation, match="unusable at origin"):
        assert_available_at({"x": support}, ORIGIN)


def test_known_availability_at_origin_is_usable():
    s = TemporalSupport(measurement_date=date(2020, 5, 1), available_from=ORIGIN)
    assert s.usable_at(ORIGIN) is True and availability_reason(s, ORIGIN) is None
    assert_available_at({"x": s}, ORIGIN)


def campaign(cid, measured, available=None):
    return Event(id=cid, event_type=EventType.MONITORING_CAMPAIGN, provenance=FACT,
                 time=TemporalSupport(measurement_date=measured, available_from=available))


def test_known_at_checks_availability_timestamp_not_measurement_date():
    c1 = campaign("C1", date(2020, 1, 1), date(2020, 1, 15))
    c2 = campaign("C2", date(2020, 5, 1), date(2020, 7, 1))   # measured before origin, processed after
    c3 = campaign("C3", date(2020, 4, 1))                     # availability unknown
    assert known_at([c1, c2, c3], ORIGIN) == [c1]
    assert known_at([c1, c2, c3], date(2020, 7, 1)) == [c1, c2]


def test_next_successful_observation_does_not_replace_missed_planned_target():
    plan = [campaign(c, d) for c, d in (("C1", date(2020, 1, 1)), ("C2", date(2020, 2, 1)),
                                        ("C3", date(2020, 3, 1)), ("C4", date(2020, 4, 1)))]
    schedule = [(e.id, e.start) for e in plan]
    observed = {"C1", "C2", "C4"}                             # C3 rejected after QC for this benchmark
    target = planned_target(date(2020, 2, 1), schedule, observed)
    assert target.campaign_id == "C3" and target.censored
    assert_planned_target(date(2020, 2, 1), "C3", schedule)
    with pytest.raises(LeakageViolation, match="not the planned epoch"):
        assert_planned_target(date(2020, 2, 1), "C4", schedule)
    two = planned_target(date(2020, 2, 1), schedule, observed, steps=2)
    assert two.campaign_id == "C4" and two.observed
    assert planned_target(date(2020, 4, 1), schedule, observed) is None
    with pytest.raises(LeakageViolation, match="duplicate"):
        planned_target(date(2020, 1, 1), schedule + [("C5", date(2020, 4, 1))], observed)


# ================================================================ splits
@pytest.mark.parametrize("function", [reject_random_train_test_split, reject_plain_kfold])
def test_unsafe_row_split_guards_raise(function):
    with pytest.raises(UnsafeSplitError):
        function([1, 2, 3], test_size=0.2)


@pytest.mark.parametrize("name", ["KFold", "train_test_split", "ShuffleSplit", "random split", "StratifiedKFold",
                                  "leave-one-out", "leave_one_campaign_out", "my_custom_split", ""])
def test_unsafe_splitter_names_raise(name):
    with pytest.raises(UnsafeSplitError):
        validate_splitter_name(name)


@pytest.mark.parametrize("name", ["rolling_origin", "Rolling-Origin", "leave_one_borehole_out",
                                  "leave-one-line-out", "GroupKFold"])
def test_allowed_splitter_names(name):
    assert validate_splitter_name(name)


def test_rolling_origin_is_forward_only_and_deterministic():
    origins = {f"s{k}": date(2020, k, 1) for k in range(1, 9)}
    targets = {f"s{k}": date(2020, k + 1, 1) for k in range(1, 9)}
    rows = rolling_origin_assignments(origins, targets, minimum_train_dates=2, maximum_folds=5)
    assert rows == rolling_origin_assignments(dict(reversed(origins.items())), targets,
                                              minimum_train_dates=2, maximum_folds=5)
    folds = sorted({r.fold_id for r in rows})
    assert len(folds) == 5
    for fold in folds:
        train = [r.sample_id for r in rows if r.fold_id == fold and r.role == "train"]
        val = [r.sample_id for r in rows if r.fold_id == fold and r.role == "validation"]
        assert max(targets[s] for s in train) <= min(origins[s] for s in val)
    leaky = [FoldAssignment("f", "train", "s5", "x"), FoldAssignment("f", "validation", "s4", "x")]
    with pytest.raises(UnsafeSplitError, match="not yet known"):
        assert_forward_only(leaky, origins, targets)          # s5's label (Jun) is unknown at s4's origin (Apr)
    with pytest.raises(SealedTestError):
        rolling_origin_assignments(origins, targets, minimum_train_dates=2, sealed={"s8"})


def test_leave_one_group_out_designs():
    boreholes = {"a": "BH-1", "b": "BH-1", "c": "BH-2", "d": "BH-3"}
    rows = leave_one_borehole_out(boreholes)
    assert {r.fold_id for r in rows} == {f"leave_one_borehole_out:BH-{i}" for i in (1, 2, 3)}
    for r in rows:
        assert (r.role == "validation") == (boreholes[r.sample_id] == r.held_out)
    assert len(leave_one_line_out({"a": "L1", "b": "L2"})) == 4
    with pytest.raises(ValueError, match="at least two"):
        leave_one_line_out({"a": "L1", "b": "L1"})
    with pytest.raises(ValueError, match="unknown"):
        leave_one_line_out({"a": "L1", "b": None})
    with pytest.raises(SealedTestError):
        leave_one_borehole_out(boreholes, sealed={"d"})


def test_sample_id_list_hash_is_ordered():
    assert sample_id_list_sha256(["a", "b"]) == hashlib.sha256(b"a\nb").hexdigest()
    assert sample_id_list_sha256(["a", "b"]) != sample_id_list_sha256(["b", "a"])


# ================================================================ frozen candidate and one-time test access
def candidate(**over):
    kw = dict(task="subsidence_rate", split_version="v1", model_id="baseline_last_rate", dataset_sha256=H,
              contract_hashes={"features": H, "targets": H}, manifest_hashes={"train": H, "validation": H},
              evaluation_spec_sha256=H, code_commit="d95344e", random_seed=0, environment_sha256=H)
    kw.update(over)
    return build_candidate_record(**kw)


def test_candidate_record_is_content_addressed_and_immutable(tmp_path):
    rec = candidate()
    assert rec == candidate() and rec["candidate_id"] != candidate(random_seed=1)["candidate_id"]
    with pytest.raises(CandidateFreezeError):
        candidate(dataset_sha256="not-a-hash")
    tampered = {**rec, "model_id": "other"}
    with pytest.raises(CandidateFreezeError, match="does not match"):
        verify_candidate_record(tampered)
    frozen = freeze_candidate(tmp_path, "records/candidate.json", rec)
    assert "frozen_at_utc" in frozen
    assert freeze_candidate(tmp_path, "records/candidate.json", rec) == frozen      # idempotent
    with pytest.raises(CandidateFreezeError, match="different frozen candidate"):
        freeze_candidate(tmp_path, "records/candidate.json", candidate(random_seed=1))
    art = tmp_path / "records" / "model.json"
    art.write_text("{}\n", encoding="utf-8")
    with_art = candidate(artifact_hashes={"records/model.json": hashlib.sha256(b"{}\n").hexdigest()})
    assert verify_candidate_artifacts(tmp_path, with_art) == []
    art.write_text("{\"changed\": 1}\n", encoding="utf-8")
    assert verify_candidate_artifacts(tmp_path, with_art)


def test_test_is_sealed_without_matching_frozen_candidate():
    rec = candidate()
    setup = dict(task="subsidence_rate", split_version="v1", contract_hashes={"targets": H, "features": H},
                 manifest_hashes={"train": H})
    assert authorize_test_access(rec, **setup) == rec["candidate_id"]
    with pytest.raises(SealedTestError):
        authorize_test_access(None, **setup)
    with pytest.raises(SealedTestError):
        authorize_test_access({**rec, "status": "draft"}, **setup)
    with pytest.raises(SealedTestError, match="contract"):
        authorize_test_access(rec, **{**setup, "contract_hashes": {"features": "b" * 64, "targets": H}})
    with pytest.raises(SealedTestError, match="train manifest"):
        authorize_test_access(rec, **{**setup, "manifest_hashes": {"train": "b" * 64}})


def test_test_access_ledger_is_one_time(tmp_path):
    rec = candidate()
    ledger = claim_test_access(tmp_path, "ledger/test_access.json", rec, test_sample_ids_sha256=H, test_rows=10)
    assert ledger["status"] == "opening" and ledger["candidate_id"] == rec["candidate_id"]
    with pytest.raises(RepeatedTestAccessError):
        claim_test_access(tmp_path, "ledger/test_access.json", rec, test_sample_ids_sha256=H, test_rows=10)
    with pytest.raises(ValueError):
        finalize_test_access(tmp_path, "ledger/test_access.json", status="retry")
    done = finalize_test_access(tmp_path, "ledger/test_access.json", status="failed_after_claim", error="crash")
    assert json.loads((tmp_path / "ledger/test_access.json").read_text(encoding="utf-8")) == done
    for call in (lambda: finalize_test_access(tmp_path, "ledger/test_access.json", status="consumed"),
                 lambda: claim_test_access(tmp_path, "ledger/test_access.json", rec, test_sample_ids_sha256=H,
                                           test_rows=10),
                 lambda: finalize_test_access(tmp_path, "ledger/never.json", status="consumed")):
        with pytest.raises(RepeatedTestAccessError):                 # a failed access is still spent
            call()


# ================================================================ metrics
def test_point_metrics_manual_fixture():
    m = point_metrics([1.0, 2.0, 4.0], [2.0, 2.0, 1.0])
    assert m["n"] == 3
    assert math.isclose(m["mae"], 4.0 / 3.0)
    assert math.isclose(m["rmse"], math.sqrt(10.0 / 3.0))
    assert math.isclose(m["bias"], -2.0 / 3.0)
    assert math.isclose(point_metrics([1.0, 2.0, 4.0], [2.0, 2.0, 1.0], [1.0, 2.0, 1.0])["mae"], 1.0)
    with pytest.raises(ValueError, match="non-finite"):
        point_metrics([1.0, float("nan")], [1.0, 1.0])


def test_interval_score_and_wis_manual_fixture():
    truth, lower, upper = np.array([0.0, 2.0, 5.0]), np.array([-1.0, 0.0, 0.0]), np.array([1.0, 1.0, 4.0])
    score = interval_score(truth, lower, upper, alpha=0.2)
    assert np.allclose(score, [2.0, 11.0, 14.0])
    wis = weighted_interval_score(truth, [0.0, 0.5, 2.0], {0.8: (lower, upper)})
    assert np.allclose(wis, (0.5 * np.array([0.0, 1.5, 3.0]) + 0.1 * score) / 1.5)
    assert math.isclose(interval_coverage(truth, lower, upper), 1.0 / 3.0)
    with pytest.raises(ValueError):
        interval_score([0.0], [1.0], [0.0], alpha=0.2)


def test_normal_crps_and_nll_closed_forms():
    at_truth = normal_crps([0.0], [0.0], [1.0])[0]
    assert math.isclose(at_truth, 2.0 / math.sqrt(2.0 * math.pi) - 1.0 / math.sqrt(math.pi))
    assert 0 <= at_truth < normal_crps([0.0], [3.0], [1.0])[0]
    assert math.isclose(normal_crps([5.0], [5.0], [2.0])[0], 2.0 * at_truth)      # scales with sigma
    assert math.isclose(normal_nll([0.0], [0.0], [1.0])[0], 0.5 * math.log(2.0 * math.pi))
    with pytest.raises(ValueError):
        normal_crps([0.0], [0.0], [0.0])


def test_finite_sample_conformal_quantile():
    scores = list(range(1, 10))                                  # n = 9
    assert conformal_quantile(scores, 0.9) == 9.0                # rank ceil(10 * 0.9) = 9
    assert conformal_quantile(scores, 0.5) == 5.0                # rank ceil(5.0) = 5
    assert conformal_quantile(scores[:5], 0.9) == math.inf       # rank 6 > n: not clipped to the maximum
    with pytest.raises(ValueError):
        conformal_quantile([], 0.9)
