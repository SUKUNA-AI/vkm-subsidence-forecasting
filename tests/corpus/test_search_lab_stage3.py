"""Unattended runner ``infra/core/lab_stage3.sh`` (late interaction for the whole corpus, agent L): dry run, a full pass
with concurrent encode clients, pack, hot-reload check and late smoke, the ENCODED status of an image without the pack
command, and failures — with a fake ``docker`` (canned JSON per step); no containers, no services."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "core" / "lab_stage3.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None or
                                shutil.which("python3") is None or shutil.which("flock") is None,
                                reason="needs bash, python3 and flock (the CORE host environment)")

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *"export-units"*) echo '{"snapshot_id": "SNAP-TEST", "count": 3, "status": "EXISTS", "units_sha256": "u",
    "dir": "/data/derived/embeddings/units/SNAP-TEST/vkm-units-v1-A", "text_rule": "vkm-units-v1/A"}' ;;
  *"curl -fsS"*)
    if [ -f "$FAKE_STATE/packed" ] && [ -z "${FAKE_NO_RELOAD:-}" ]; then
      echo '{"status": "ok", "models": [{"role": "late", "key": "mlateon", "quant": "Q8_0"}],
        "late_store": {"status": "READY", "pack_id": "SNAP-TEST-0123456789ab", "count": 3}}'
    else
      echo '{"status": "ok", "models": [{"role": "dense", "key": "jina-v5-nano-retrieval", "quant": "Q8_0"},
        {"role": "late", "key": "mlateon", "quant": "Q8_0"}], "late_store": {"status": "MISSING"}}'
    fi ;;
  *"python -c"*) echo 0 ;;
  *"embeddings.cli encode"*)
    if [ -n "${FAKE_ENCODE_FAIL:-}" ]; then echo "backend down" >&2; exit 1; fi
    echo '[{"role": "late", "key": "mlateon", "kind": "multivector", "embedded": 1,
      "dir": "/data/derived/embeddings/multivector/lightonai__mLateOn/r/s", "config_signature": "s",
      "plan": {"new": 1, "to_embed": 1, "unchanged": 0}, "validation": {"ok": true, "skipped": true}}]' ;;
  *"rx580-embed-worker pack --help"*) if [ -n "${FAKE_NO_PACK:-}" ]; then exit 2; fi; echo "usage: pack" ;;
  *"rx580-embed-worker pack"*)
    touch "$FAKE_STATE/packed"
    echo '{"status": "BUILT", "pack_id": "SNAP-TEST-0123456789ab", "snapshot_id": "SNAP-TEST", "count": 3,
      "total_tokens": 450, "bytes": 115200, "checks_64": {"ok": true}, "published": {"pack_id": "SNAP-TEST-0123456789ab"}}' ;;
  *"hybrid-smoke --help"*) echo "usage: hybrid-smoke [--late] [--late-candidates N]" ;;
  *"hybrid-smoke --api-url"*)
    echo '{"status": "PASS", "late": true, "latency": {"p50_ms": 420.0}, "queries": [{"query": "q", "hits": 3, "pass": true}]}' ;;
  *) echo "unexpected docker call: $*" >&2; exit 3 ;;
esac
"""


@pytest.fixture()
def host(tmp_path):
    compose, data, state = tmp_path / "compose", tmp_path / "data", tmp_path / "state"
    (data / "canonical").mkdir(parents=True)
    (data / "canonical" / "CURRENT").write_text("SNAP-TEST\n", encoding="utf-8")
    compose.mkdir()
    state.mkdir()
    (compose / ".env").write_text(f"VKM_DATA_ROOT_HOST={data}\n", encoding="utf-8")
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "VKM_COMPOSE_DIR": str(compose), "VKM_DOCKER": str(fake), "VKM_LAB3_POLL_S": "0",
           "VKM_LAB3_PROGRESS_S": "1", "FAKE_LOG": str(tmp_path / "docker.log"), "FAKE_STATE": str(state)}
    return {"env": env, "data": data, "log": tmp_path / "docker.log", "tmp": tmp_path}


def _run(host, *args, **env):
    return subprocess.run(["bash", str(SCRIPT), *args], env={**host["env"], **env}, capture_output=True, text=True,
                          timeout=120)


def _out(host):
    return host["data"] / "receipts" / "lab_stage3"


def test_dry_run_plans_without_containers(host):
    r = _run(host, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert " DRY_RUN " in (_out(host) / "STATUS").read_text(encoding="utf-8")
    assert not host["log"].exists()
    assert "--roles late" in r.stdout and "OPENBLAS_NUM_THREADS=1" in r.stdout and "× 4 concurrent clients" in r.stdout
    assert "pack --config /config/rx580.json --data-root /data" in r.stdout and "--late" in r.stdout


def test_full_pass_with_clients_pack_reload_and_smoke(host):
    r = _run(host)
    assert r.returncode == 0, r.stdout + r.stderr
    status = (_out(host) / "STATUS").read_text(encoding="utf-8")
    assert " DONE " in status and "late_embedded=4" in status and "pack=BUILT:SNAP-TEST-0123456789ab" in status
    receipt = json.loads((_out(host) / "latest.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "DONE" and receipt["encode"]["clients"] == 4 and receipt["encode"]["embedded"] == 4
    assert receipt["encode"]["plan"]["to_embed"] == 4 and receipt["encode"]["dirs"] == 1
    assert receipt["pack"]["pack_id"] == "SNAP-TEST-0123456789ab" and receipt["reload"]["status"] == "ok"
    assert receipt["smoke"]["status"] == "PASS"
    calls = host["log"].read_text(encoding="utf-8")
    assert calls.count("rx580-retrieval python -m vkm_corpus.embeddings.cli encode") == 4 and "--roles late" in calls
    assert "--writer rx580L-" in calls and "-a1s3 " in calls and "OPENBLAS_NUM_THREADS=1" in calls
    assert "--docs /cache/lab3/lab3-" in calls and "/shard-3.jsonl" in calls
    assert "rx580-embed-worker pack --config /config/rx580.json --data-root /data --units " \
           "/data/derived/embeddings/units/SNAP-TEST/vkm-units-v1-A --text-rule vkm-units-v1/A --publish" in calls
    assert "hybrid-smoke --api-url http://127.0.0.1:8000 --late --late-candidates 100" in calls
    assert "rm -rf /cache/lab3/lab3-" in calls                               # shards removed at the end


def test_image_without_pack_command_ends_encoded(host):
    r = _run(host, FAKE_NO_PACK="1")
    assert r.returncode == 0, r.stdout + r.stderr
    status = (_out(host) / "STATUS").read_text(encoding="utf-8")
    assert " ENCODED " in status and "late_embedded=4" in status and "no 'embed pack' yet" in status
    assert json.loads((_out(host) / "latest.json").read_text(encoding="utf-8"))["failed_step"] is None


def test_missing_reload_or_wrong_model_fail_the_run(host):
    cfg = host["tmp"] / "lab.json"
    cfg.write_text(json.dumps({"clients": 2, "wait_reload_s": 0}), encoding="utf-8")
    r = _run(host, "--config", str(cfg), "--skip-encode", FAKE_NO_RELOAD="1")
    assert r.returncode != 0
    status = (_out(host) / "STATUS").read_text(encoding="utf-8")
    assert " FAILED " in status and "step=reload" in status and "multivector_dir" in status
    cfg.write_text(json.dumps({"clients": 2, "expect_late_key": "jina-colbert-v2"}), encoding="utf-8")
    r = _run(host, "--config", str(cfg))
    assert r.returncode != 0
    status = (_out(host) / "STATUS").read_text(encoding="utf-8")
    assert "step=encode" in status and "expected jina-colbert-v2" in status
