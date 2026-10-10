"""Operator-pinned synthetic qualification; never real corpus processing."""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from vkm_evidence import cli
from vkm_evidence.contracts import OriginSlice, canonical_bytes, record_hash
from vkm_evidence.qualification import (FrozenPlan, FrozenRegistration, GoldSet, GoldStratum,
    MatchAdjudication, MatchStratum, PredictionSet, StratumKey, StratumPlan, StratumResult,
    Threshold, qualification_gate)


T = datetime(2026, 10, 1, tzinfo=timezone.utc)
SHA = "a" * 64
METRICS = ("DISCOVERY_RECALL", "DISCOVERY_PRECISION", "CLASSIFICATION_ACCURACY")
SECRET = "RAW_PRIVATE_SYNTHETIC_VALUE_DO_NOT_LOG"


def write(path, value):
    raw = canonical_bytes(value) + b"\n"
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def artifacts(tmp_path):
    key = StratumKey(source_id="synthetic-source", source_sha256=SHA, format="DOCX", quality="CLEAN", object_kind="TABLE")
    objects = ({"object_id": "gold-object", "unit_id": "unit", "locator": SECRET, "classification": "TABLE"},)
    gold = GoldSet(population="SYNTHETIC", authors=("annotator",), reviewers=("original-reviewer",),
        strata=(GoldStratum(stratum_id="native", key=key, unit_ids=("unit",), status="COMPLETE", objects=objects,
            origins=(OriginSlice(origin_id="evaluation", independence_basis="synthetic original", verified=True),),
            original_review="VERIFIED_FROM_ORIGINAL", review_receipt_sha256=SHA),))
    plan = FrozenPlan(plan_id="synthetic-plan", population="SYNTHETIC", purpose="INDEPENDENT_EXTRACTION_VALIDATION",
        gold_sha256=record_hash(gold), extractor_identity_sha256=SHA, producer_actors=("extractor",),
        tuning_state="NO_TUNING_ATTESTED", tuning_declaration_sha256=SHA, confidence=.95,
        strata=(StratumPlan(stratum_id="native", key=key, unit_ids=("unit",), design="POSITIVE_OBJECTS",
            sampling_basis="synthetic explicit fixture", thresholds=tuple(Threshold(metric=m, minimum=1,
                minimum_denominator=1, decision_statistic="POINT") for m in METRICS)),))
    prediction = PredictionSet(plan_sha256=plan.sha256, extractor_identity_sha256=SHA, producer_actors=("extractor",),
        started_at=T + timedelta(seconds=1), strata=(StratumResult(stratum_id="native", key=key, unit_ids=("unit",),
            status="COMPLETE", objects=({**objects[0], "object_id": "prediction-object"},)),))
    adjudication = MatchAdjudication(plan_sha256=plan.sha256, gold_sha256=record_hash(gold),
        prediction_sha256=record_hash(prediction), reviewers=("matching-reviewer",), reviewed_at=T + timedelta(seconds=2),
        strata=(MatchStratum(stratum_id="native", status="COMPLETE", matches=(
            {"gold_object_id": "gold-object", "prediction_object_id": "prediction-object"},)),))
    receipt = cli.TrustedQualificationRegistration(schema_version="vkm-qualification-preregistration-receipt/1",
        plan_sha256=plan.sha256, gold_sha256=record_hash(gold), registered_at=T, registrar="registrar")
    receipt_path = tmp_path / "trusted-receipt.json"
    receipt_sha = write(receipt_path, receipt)
    registration = FrozenRegistration(plan_sha256=plan.sha256, gold_sha256=record_hash(gold),
        registered_at=T, registrar="registrar", durable_receipt_sha256=receipt_sha)
    values = {"plan": plan, "gold": gold, "predictions": prediction, "adjudication": adjudication,
              "registration": registration}
    argv = ["qualification"]
    for name, value in values.items():
        path = tmp_path / (name + ".json")
        digest = write(path, value)
        argv += ["--" + name, str(path), "--" + name + "-sha256", digest]
    output = tmp_path / "report.json"
    argv += ["--trusted-registration-receipt", str(receipt_path), "--trusted-registration-sha256", receipt_sha,
             "--trusted-registrar", "registrar", "--output", str(output)]
    return {"argv": argv, "values": values, "receipt": receipt, "root": tmp_path, "output": output}


def replace(argv, option, value):
    args = list(argv)
    args[args.index(option) + 1] = str(value)
    return args


def omit(argv, *options):
    args = list(argv)
    for option in options:
        index = args.index(option)
        del args[index:index + 2]
    return args


def result(capsys):
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    return json.loads(captured.out)


def test_no_inputs_not_run_and_help_is_not_a_pass(capsys):
    assert cli.main(["qualification"]) == 2
    assert result(capsys)["status"] == "NOT_RUN"
    with pytest.raises(SystemExit) as exit:
        cli.main(["qualification", "--help"])
    assert exit.value.code == 0
    assert "NOT_RUN" in capsys.readouterr().out


def test_pinned_synthetic_success_is_not_production_and_retry_is_identical(artifacts, capsys):
    args = artifacts["argv"]
    assert cli.main(args) == 0
    summary = result(capsys)
    raw = artifacts["output"].read_bytes()
    report = json.loads(raw)
    assert summary["status"] == "PASS" and summary["qualification"] == "SYNTHETIC_ONLY"
    assert summary["production_ready"] is False
    assert summary["report_file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["operator_verification"]["registration_trust"] == "OPERATOR_PINNED_RECEIPT"
    assert not qualification_gate(report, artifacts["values"]["plan"], record_hash(artifacts["values"]["predictions"]))
    assert qualification_gate(report, artifacts["values"]["plan"], record_hash(artifacts["values"]["predictions"]), require_corpus=False)
    assert cli.main(args) == 0
    assert result(capsys) == summary and artifacts["output"].read_bytes() == raw


def test_absent_trusted_preregistration_is_blocked(artifacts, capsys):
    args = omit(artifacts["argv"], "--trusted-registration-receipt", "--trusted-registration-sha256", "--trusted-registrar")
    assert cli.main(args) == 2
    assert result(capsys)["status"] == "BLOCKED"
    report = json.loads(artifacts["output"].read_bytes())
    assert "PREREGISTRATION_NOT_VERIFIED" in report["reasons"]
    assert report["operator_verification"]["registration_trust"] == "NOT_VERIFIED"


def test_absent_registration_model_is_blocked_even_with_trusted_receipt(artifacts, capsys):
    args = omit(artifacts["argv"], "--registration", "--registration-sha256")
    assert cli.main(args) == 2
    assert result(capsys)["status"] == "BLOCKED"
    assert "PREREGISTRATION_BINDING_MISSING" in json.loads(artifacts["output"].read_bytes())["reasons"]


def test_report_output_required_before_success(artifacts, capsys):
    assert cli.main(omit(artifacts["argv"], "--output")) == 1
    assert result(capsys)["status"] == "FAILED"
    assert not artifacts["output"].exists()


@pytest.mark.parametrize("missing", ["gold", "predictions", "adjudication"])
def test_missing_evaluation_artifacts_return_not_run(artifacts, capsys, missing):
    assert cli.main(omit(artifacts["argv"], "--" + missing, "--" + missing + "-sha256")) == 2
    assert result(capsys)["status"] == "NOT_RUN"


@pytest.mark.parametrize("option", ["--plan-sha256", "--gold-sha256", "--predictions-sha256",
                                    "--adjudication-sha256", "--registration-sha256", "--trusted-registration-sha256"])
def test_changed_byte_pin_never_publishes(artifacts, capsys, option):
    assert cli.main(replace(artifacts["argv"], option, "f" * 64)) == 1
    assert result(capsys)["status"] == "FAILED"
    assert not artifacts["output"].exists()


def test_changed_gold_cannot_be_accepted_by_repinning_its_file(artifacts, capsys):
    gold = artifacts["values"]["gold"].model_copy(update={"authors": ("changed-annotator",)})
    path = artifacts["root"] / "gold.json"
    assert cli.main(replace(artifacts["argv"], "--gold-sha256", write(path, gold))) == 2
    assert result(capsys)["status"] == "BLOCKED"
    assert "FROZEN_GOLD_MISSING_OR_CHANGED" in json.loads(artifacts["output"].read_bytes())["reasons"]


@pytest.mark.parametrize("field,value", [("registrar", "impostor"), ("registered_at", T + timedelta(seconds=5)),
                                         ("plan_sha256", "b" * 64), ("gold_sha256", "b" * 64)])
def test_receipt_must_bind_exact_plan_gold_time_registrar(artifacts, capsys, field, value):
    path = artifacts["root"] / "trusted-receipt.json"
    receipt = artifacts["receipt"].model_copy(update={field: value})
    receipt_sha = write(path, receipt)
    registration = artifacts["values"]["registration"].model_copy(update={"durable_receipt_sha256": receipt_sha})
    reg_sha = write(artifacts["root"] / "registration.json", registration)
    args = replace(replace(artifacts["argv"], "--trusted-registration-sha256", receipt_sha), "--registration-sha256", reg_sha)
    assert cli.main(args) == 2
    assert result(capsys)["status"] == "BLOCKED"
    assert "PREREGISTRATION_NOT_VERIFIED" in json.loads(artifacts["output"].read_bytes())["reasons"]


def test_registrar_requires_separate_operator_approval(artifacts, capsys):
    assert cli.main(replace(artifacts["argv"], "--trusted-registrar", "unapproved")) == 2
    assert result(capsys)["registration_trust"] == "NOT_VERIFIED"


def test_same_output_cannot_be_replaced_with_different_report(artifacts, capsys):
    assert cli.main(artifacts["argv"]) == 0
    result(capsys)
    before = artifacts["output"].read_bytes()
    assert cli.main(replace(artifacts["argv"], "--trusted-registrar", "other")) == 1
    assert result(capsys)["status"] == "FAILED"
    assert artifacts["output"].read_bytes() == before


def test_failed_extraction_is_not_pass(artifacts, capsys):
    prediction = artifacts["values"]["predictions"]
    failed = prediction.strata[0].model_copy(update={"status": "FAILED", "reason": SECRET})
    prediction = prediction.model_copy(update={"strata": (failed,)})
    digest = write(artifacts["root"] / "predictions.json", prediction)
    assert cli.main(replace(artifacts["argv"], "--predictions-sha256", digest)) == 2
    assert result(capsys)["status"] == "BLOCKED"
    assert SECRET not in artifacts["output"].read_text(encoding="utf-8")


@pytest.mark.parametrize("payload", [b'{"plan_id":"' + SECRET.encode() + b'"}',
    b'{"population":"SYNTHETIC","population":"CORPUS"}', b'{"confidence": NaN}'])
def test_bad_json_errors_do_not_echo_payload(artifacts, capsys, payload):
    (artifacts["root"] / "plan.json").write_bytes(payload)
    args = replace(artifacts["argv"], "--plan-sha256", hashlib.sha256(payload).hexdigest())
    assert cli.main(args) == 1
    assert result(capsys)["status"] == "FAILED"
    assert not artifacts["output"].exists()


def test_oversized_input_and_unpinned_input_are_rejected(artifacts, capsys, monkeypatch):
    assert cli.main(omit(artifacts["argv"], "--plan-sha256")) == 1
    result(capsys)
    monkeypatch.setattr(cli, "QUALIFICATION_MAX_BYTES", 32)
    assert cli.main(artifacts["argv"]) == 1
    assert result(capsys)["status"] == "FAILED"
    assert not artifacts["output"].exists()


@pytest.mark.parametrize("parent", [False, True])
def test_symlink_file_or_parent_is_rejected(artifacts, capsys, parent):
    link = artifacts["root"] / "indirect"
    try:
        link.symlink_to(artifacts["root"] if parent else artifacts["root"] / "plan.json", target_is_directory=parent)
    except OSError:
        pytest.skip("host cannot create synthetic symlinks")
    target = link / "plan.json" if parent else link
    assert cli.main(replace(artifacts["argv"], "--plan", target)) == 1
    assert result(capsys)["status"] == "FAILED"


def test_symlink_output_is_not_followed(artifacts, capsys):
    target = artifacts["root"] / "untouched.json"
    target.write_bytes(b"existing user bytes")
    try:
        artifacts["output"].symlink_to(target)
    except OSError:
        pytest.skip("host cannot create synthetic symlinks")
    assert cli.main(artifacts["argv"]) == 1
    assert result(capsys)["status"] == "FAILED"
    assert target.read_bytes() == b"existing user bytes"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
def test_fifo_input_is_rejected_before_open(artifacts, capsys):
    fifo = artifacts["root"] / "fifo"
    os.mkfifo(fifo)
    assert cli.main(replace(artifacts["argv"], "--plan", fifo)) == 1
    assert result(capsys)["status"] == "FAILED"


@pytest.mark.parametrize("when", ["file", "directory"])
def test_fsync_failure_never_acknowledges_pass(artifacts, capsys, monkeypatch, when):
    if when == "directory" and os.name == "nt":
        pytest.skip("directory durability explicitly NOT_QUALIFIED on Windows")
    def fail(*_):
        raise OSError("synthetic fsync failure")
    if when == "file":
        monkeypatch.setattr(cli.os, "fsync", fail)
    else:
        monkeypatch.setattr(cli, "_qualification_sync_directory", fail)
    assert cli.main(artifacts["argv"]) == 1
    assert result(capsys)["status"] == "FAILED"


def test_concurrent_different_reports_have_exactly_one_winner(tmp_path):
    target = tmp_path / "immutable.json"
    def publish(raw):
        try:
            cli._qualification_write_report(target, raw)
            return raw
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (b'{"a":1}', b'{"a":2}')))
    assert sum(r is not None for r in results) == 1
    assert target.read_bytes() in [r for r in results if r is not None]
