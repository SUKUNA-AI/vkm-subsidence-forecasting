"""Unattended runner ``infra/core/lab_stage2.sh`` (retrieval lab stage 2): dry run, a full pass and a failing smoke with
a fake ``docker`` (canned JSON per step), receipts and the status line; no containers, no services."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "core" / "lab_stage2.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None or
                                shutil.which("python3") is None or shutil.which("flock") is None,
                                reason="needs bash, python3 and flock (the CORE host environment)")

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *"search status"*) echo '{"aliases": {"pages": {"built_from_snapshot_id": "SNAP-TEST"}}}' ;;
  *"export-units"*) echo '{"snapshot_id": "SNAP-TEST", "count": 3, "status": "WRITTEN", "units_sha256": "u",
    "dir": "/data/derived/embeddings/units/SNAP-TEST/vkm-units-v1-A", "text_rule": "vkm-units-v1/A"}' ;;
  *"curl -fsS"*) echo '{"status": "ok", "models": [{"role": "dense", "key": "granite-311m-r2", "quant": "Q8_0"},
    {"role": "late", "key": "mlateon", "quant": "Q8_0"}]}' ;;
  *"python -c"*) echo 0 ;;
  *"embeddings.cli encode"*) echo '[{"role": "dense", "key": "granite-311m-r2", "kind": "dense", "embedded": 3,
    "dir": "/data/derived/embeddings/dense/m/r/s", "config_signature": "s", "plan": {"new": 3},
    "validation": {"ok": true, "skipped": true}}]' ;;
  *"build-vectors"*) echo '{"status": "COMPLETE", "build_id": "b1", "index": "vkm-vectors-m1-b1"}' ;;
  *"hybrid-smoke"*)
    if [ -n "${FAKE_SMOKE_FAIL:-}" ]; then echo '{"status": "FAIL", "queries": []}'; exit 1; fi
    echo '{"status": "PASS", "queries": [{"query": "q", "hits": 3, "pass": true}]}' ;;
  *) echo "unexpected docker call: $*" >&2; exit 3 ;;
esac
"""


@pytest.fixture()
def host(tmp_path):
    compose, data = tmp_path / "compose", tmp_path / "data"
    (data / "canonical").mkdir(parents=True)
    (data / "canonical" / "CURRENT").write_text("SNAP-TEST\n", encoding="utf-8")
    compose.mkdir()
    (compose / ".env").write_text(f"VKM_DATA_ROOT_HOST={data}\n", encoding="utf-8")
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "VKM_COMPOSE_DIR": str(compose), "VKM_DOCKER": str(fake), "VKM_LAB2_POLL_S": "0",
           "FAKE_LOG": str(tmp_path / "docker.log")}
    return {"env": env, "data": data, "log": tmp_path / "docker.log"}


def _run(host, *args, **env):
    return subprocess.run(["bash", str(SCRIPT), *args], env={**host["env"], **env}, capture_output=True, text=True,
                          timeout=120)


def test_dry_run_plans_without_containers(host):
    r = _run(host, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    out = host["data"] / "receipts" / "lab_stage2"
    assert " DRY_RUN " in (out / "STATUS").read_text(encoding="utf-8")
    assert json.loads((out / "latest.json").read_text(encoding="utf-8"))["status"] == "DRY_RUN"
    assert not host["log"].exists()                                          # no docker call at all
    assert "build-vectors" in r.stdout and "--skip-validate" in r.stdout


def test_full_pass_writes_the_receipt_and_the_status_line(host):
    r = _run(host)
    assert r.returncode == 0, r.stdout + r.stderr
    out = host["data"] / "receipts" / "lab_stage2"
    status = (out / "STATUS").read_text(encoding="utf-8")
    assert " DONE " in status and "snapshot=SNAP-TEST" in status and "smoke=PASS" in status
    receipt = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "DONE" and receipt["failed_step"] is None
    assert receipt["units"]["count"] == 3 and receipt["encode"]["embedded"] == 3
    assert receipt["vectors"]["status"] == "COMPLETE" and receipt["smoke"]["status"] == "PASS"
    calls = host["log"].read_text(encoding="utf-8")
    assert "exec -T rx580-retrieval python -m vkm_corpus.embeddings.cli encode" in calls
    assert "--docs /data/derived/embeddings/units/SNAP-TEST/vkm-units-v1-A/docs.jsonl" in calls
    assert "--skip-validate" in calls and "--roles dense" in calls
    assert "build-vectors --embeddings /data/receipts/lab_stage2/" in calls and "--snapshot SNAP-TEST" in calls
    assert "--skip-if-current" in calls and "hybrid-smoke --api-url http://127.0.0.1:8000" in calls
    assert "--query ползучесть каменной соли" in calls
    run_dirs = [p for p in out.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1 and (run_dirs[0] / "run.log").is_file()


def test_failed_smoke_or_wrong_model_is_a_failed_run(host):
    r = _run(host, FAKE_SMOKE_FAIL="1")
    assert r.returncode != 0
    status = (host["data"] / "receipts" / "lab_stage2" / "STATUS").read_text(encoding="utf-8")
    assert " FAILED " in status and "step=smoke" in status and "smoke=FAIL" in status
    cfg = host["data"] / "lab.json"
    cfg.write_text(json.dumps({"expect_dense_key": "jina-v5-nano"}), encoding="utf-8")
    r = _run(host, "--config", str(cfg))
    assert r.returncode != 0
    status = (host["data"] / "receipts" / "lab_stage2" / "STATUS").read_text(encoding="utf-8")
    assert "step=encode" in status and "expected jina-v5-nano" in status
