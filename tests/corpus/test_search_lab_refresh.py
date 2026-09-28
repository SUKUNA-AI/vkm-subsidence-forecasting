"""Unattended refresh job ``infra/core/lab_refresh.sh`` (dense stage 2 + late stage 3 on one snapshot, agent L): the
snapshot is a parameter (CURRENT must be it, optionally after a bounded wait), the pack worker's image must be the
serving image, and the verdict comes from the stage receipts — DONE only when both stages are DONE on the requested
snapshot and the unit count, the dense index count and the late pack count are equal; a stale stage receipt never
counts. The stages are stubs that write canned receipts, ``docker`` is a fake; no containers, no services."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "core" / "lab_refresh.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None or
                                shutil.which("python3") is None or shutil.which("flock") is None,
                                reason="needs bash, python3 and flock (the CORE host environment)")

STAGE = r"""#!/usr/bin/env bash
set -euo pipefail
name="$(basename "$0" .sh)"
echo "$name $*" >> "$FAKE_LOG"
[ "${1:-}" = "--dry-run" ] && { echo "$name: plan"; exit 0; }
[ -n "${FAKE_FAIL:-}" ] && [ "$FAKE_FAIL" = "$name" ] && { echo "$name failed" >&2; exit 1; }
python3 - "$name" <<'PY'
import datetime, json, os, sys
name = sys.argv[1]
data = os.environ["FAKE_DATA"]
run_id = f"{name}-run"
out = os.path.join(data, "receipts", name)
os.makedirs(os.path.join(out, run_id), exist_ok=True)
now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
units, snap = {"count": 5, "status": "EXISTS"}, os.environ.get("FAKE_SNAP", "SNAP-TEST")
if name == "lab_stage2":
    with open(os.path.join(out, run_id, "build-vectors.json"), "w", encoding="utf-8") as fh:
        json.dump({"status": "COMPLETE", "build_id": "b1", "index": "vkm-vectors-b1",
                   "checks": {"count": int(os.environ.get("FAKE_DENSE", "5")), "expected": 5}}, fh)
    receipt = {"run_id": run_id, "status": "DONE", "snapshot_id": snap, "finished_at": now, "units": units}
else:
    pack = {"status": "BUILT", "pack_id": "SNAP-TEST-0123456789ab", "count": int(os.environ.get("FAKE_PACK", "5"))}
    receipt = {"run_id": run_id, "status": os.environ.get("FAKE_S3_STATUS", "DONE"), "snapshot_id": snap,
               "finished_at": now, "units": units, "pack": pack}
with open(os.path.join(out, "latest.json"), "w", encoding="utf-8") as fh:
    json.dump(receipt, fh)
PY
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *" ps -q rx580-retrieval"*) [ -n "${FAKE_NO_SERVICE:-}" ] || echo "cid0123" ;;
  "inspect --format"*"cid0123") echo "${FAKE_SVC_IMAGE:-vkm-rx580-retrieval:new}" ;;
  *) echo "unexpected docker call: $*" >&2; exit 3 ;;
esac
"""


@pytest.fixture()
def host(tmp_path):
    compose, data = tmp_path / "compose", tmp_path / "data"
    (data / "canonical").mkdir(parents=True)
    (data / "canonical" / "CURRENT").write_text("SNAP-TEST\n", encoding="utf-8")
    compose.mkdir()
    shutil.copy(SCRIPT, compose / "lab_refresh.sh")
    for name in ("lab_stage2.sh", "lab_stage3.sh"):
        (compose / name).write_text(STAGE, encoding="utf-8")
        (compose / name).chmod(0o755)
    (compose / ".env").write_text(f"VKM_DATA_ROOT_HOST={data}\nVKM_RX580_IMAGE=vkm-rx580-retrieval:new\n",
                                  encoding="utf-8")
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "VKM_COMPOSE_DIR": str(compose), "VKM_DOCKER": str(fake), "VKM_REFRESH_POLL_S": "1",
           "FAKE_LOG": str(tmp_path / "stages.log"), "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
           "FAKE_DATA": str(data)}
    for k in ("VKM_DATA_ROOT_HOST", "VKM_RX580_IMAGE"):
        env.pop(k, None)
    return {"env": env, "data": data, "script": compose / "lab_refresh.sh", "log": tmp_path / "stages.log"}


def _cmd(host, *args):
    return ["bash", str(host["script"]), *args]


def _run(host, *args, **env):
    return subprocess.run(_cmd(host, *args), env={**host["env"], **env}, capture_output=True, text=True, timeout=60)


def _stages(host):
    return host["log"].read_text(encoding="utf-8").splitlines() if host["log"].exists() else []


def _receipt(host):
    out = host["data"] / "receipts" / "lab_refresh"
    runs = sorted(p for p in out.iterdir() if p.is_dir())
    return json.loads((runs[-1] / "receipt.json").read_text(encoding="utf-8")), (out / "STATUS").read_text("utf-8")


def test_dry_run_plans_both_stages(host):
    r = _run(host, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _stages(host) == ["lab_stage2 --dry-run", "lab_stage3 --dry-run"]
    receipt, status = _receipt(host)
    assert receipt["status"] == "DRY_RUN" and " DRY_RUN " in status
    assert "rx580-retrieval runs vkm-rx580-retrieval:new" in r.stdout


def test_snapshot_is_required(host):
    r = _run(host)
    assert r.returncode == 2 and "--snapshot" in r.stderr
    assert _stages(host) == []


def test_both_stages_done_on_the_snapshot_and_counts_equal(host):
    r = _run(host, "--snapshot", "SNAP-TEST")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _stages(host) == ["lab_stage2 ", "lab_stage3 "]
    receipt, status = _receipt(host)
    assert receipt["status"] == "DONE" and receipt["counts_equal"] is True and receipt["failed_stage"] is None
    assert receipt["snapshot_requested"] == receipt["snapshot_id"] == "SNAP-TEST"
    assert receipt["counts"] == {"units_stage2": 5, "units_stage3": 5, "dense_index": 5, "late_pack": 5}
    assert receipt["dense_build"]["build_id"] == "b1" and receipt["late_pack"]["pack_id"] == "SNAP-TEST-0123456789ab"
    assert " DONE snapshot=SNAP-TEST " in status and "dense_index=5" in status and "equal=True" in status


@pytest.mark.parametrize("env", [{"FAKE_PACK": "4"}, {"FAKE_DENSE": "6"}, {"FAKE_S3_STATUS": "ENCODED"},
                                 {"FAKE_SNAP": "SNAP-OTHER"}])
def test_count_mismatch_partial_stage_or_other_snapshot_is_incomplete(host, env):
    r = _run(host, "--snapshot", "SNAP-TEST", **env)
    assert r.returncode != 0
    receipt, status = _receipt(host)
    assert receipt["status"] == "INCOMPLETE" and " INCOMPLETE " in status and receipt["note"]


@pytest.mark.parametrize("env,note", [({"FAKE_SVC_IMAGE": "vkm-rx580-retrieval:old"}, "up -d --no-deps"),
                                      ({"FAKE_NO_SERVICE": "1"}, "not running")])
def test_serving_image_must_be_the_pack_worker_image(host, env, note):
    r = _run(host, "--snapshot", "SNAP-TEST", **env)
    assert r.returncode != 0
    assert _stages(host) == []
    receipt, status = _receipt(host)
    assert receipt["status"] == "FAILED" and receipt["failed_stage"] == "check" and note in receipt["note"]


def test_other_current_fails_without_wait_and_is_adopted_after_a_bounded_wait(host):
    r = _run(host, "--snapshot", "SNAP-NEXT")
    assert r.returncode != 0 and _stages(host) == []
    receipt, _status = _receipt(host)
    assert receipt["status"] == "FAILED" and "CURRENT is SNAP-TEST, not SNAP-NEXT" in receipt["note"]
    time.sleep(1.1)                                   # run ids have a one-second resolution
    proc = subprocess.Popen(_cmd(host, "--snapshot", "SNAP-NEXT", "--wait-s", "30"),
                            env={**host["env"], "FAKE_SNAP": "SNAP-NEXT"}, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)
    assert proc.poll() is None and _stages(host) == []                    # still waiting for the snapshot
    (host["data"] / "canonical" / "CURRENT").write_text("SNAP-NEXT\n", encoding="utf-8")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    receipt, _status = _receipt(host)
    assert receipt["status"] == "DONE" and receipt["snapshot_id"] == "SNAP-NEXT"


def test_failed_stage_stops_the_job_and_stale_receipts_do_not_count(host):
    out2 = host["data"] / "receipts" / "lab_stage2"
    out2.mkdir(parents=True)
    (out2 / "latest.json").write_text(json.dumps({"run_id": "old", "status": "DONE", "units": {"count": 5},
                                                  "finished_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
    r = _run(host, "--snapshot", "SNAP-TEST", FAKE_FAIL="lab_stage2")
    assert r.returncode != 0
    assert _stages(host) == ["lab_stage2 "]                              # stage 3 never ran
    receipt, status = _receipt(host)
    assert receipt["status"] == "FAILED" and receipt["failed_stage"] == "stage2"
    assert receipt["stage2"]["run_id"] is None and receipt["counts"]["units_stage2"] is None
    assert " FAILED stage=stage2 " in status
