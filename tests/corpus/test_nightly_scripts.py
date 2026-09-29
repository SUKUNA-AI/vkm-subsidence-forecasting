"""The nightly jobs end to end on a synthetic host (agent OPS): the backup chain CORE manifest → EDGE copy with hard
links → sha256 verification → status push → restore of a subset; the forced command of the backup key; the 04:00
orchestrator with a fake ``docker`` (canned answers of every step, the real summary, dossier store and topic scorer).
Needs the CORE/EDGE host tools (bash, python3, flock, rsync, GNU coreutils): skipped on Windows. Nothing reaches a
real host, a container or a service."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "infra" / "core" / "nightly"
EDGE = ROOT / "infra" / "edge" / "backup"
BENCH = ROOT / "benchmarks" / "topic_v1"
HOST_TOOLS = ("bash", "python3", "flock", "rsync", "timeout", "sha256sum")
pytestmark = pytest.mark.skipif(sys.platform == "win32" or any(shutil.which(t) is None for t in HOST_TOOLS),
                                reason="needs the CORE/EDGE host environment: " + ", ".join(HOST_TOOLS))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(cmd, env, timeout=300, **kw):
    return subprocess.run(cmd, env={**os.environ, **env}, capture_output=True, text=True, timeout=timeout, **kw)


def _write(root: Path, rel: str, data) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


def make_core(tmp: Path) -> tuple[Path, Path]:
    """A data root and a compose directory whose .env names it."""
    data = tmp / "core" / "data"
    for rel, blob in {
        ".vkm_root.json": '{"root_kind": "CANONICAL"}',
        "canonical/CURRENT": "snap-20260929T193550Z-738eebee\n",
        "canonical/pages/source_id=VKM-SRC-001/run=RUN-1/part-00000.parquet": b"PAR1" + os.urandom(2048),
        "canonical/_commits/run=RUN-1/VKM-SRC-001__CMT-1.json": "{}",
        "artifacts/png/ab/cd/abcd.png": os.urandom(4096),
        "duckdb/vkm_corpus.duckdb": os.urandom(1024),
        "derived/navigation/CURRENT": "snap-20260929T193550Z-738eebee\n",
        "derived/embeddings/multivector/m/r/s/packs/CURRENT": '{"pack_id": "P1"}',
        "derived/embeddings/multivector/m/r/s/packs/P1/tokens.f16": os.urandom(512),
        "derived/embeddings/multivector/m/r/s/packs/P0/tokens.f16": os.urandom(512),
        "receipts/lab_stage3/latest.json": '{"status": "DONE"}',
        "tmp/x": "transient",
    }.items():
        _write(data, rel, blob)
    compose = tmp / "core" / "compose"
    compose.mkdir(parents=True)
    (compose / ".env").write_text(f"VKM_DATA_ROOT_HOST={data}\n", encoding="utf-8")
    (compose / "compose.yml").write_text("name: vkm-core\n", encoding="utf-8")
    return data, compose


# ---------------------------------------------------------------- backup chain
def backup_env(tmp: Path, data: Path) -> dict[str, str]:
    return {"VKM_BACKUP_ROOT": str(tmp / "edge" / "backup"), "VKM_BACKUP_SOURCE": f"{data}/",
            "VKM_BACKUP_STATUS_DEST": f"{data}/receipts/backup/edge/", "VKM_BACKUP_MIN_FREE_GB": "0",
            "VKM_BACKUP_REHASH_WEEKDAY": "0", "VKM_BACKUP_WAIT_MANIFEST_S": "0", "VKM_BACKUP_JOBS": "2",
            "VKM_BACKUP_ENV": str(tmp / "nonexistent.env"), "VKM_BACKUP_DU_TIMEOUT_S": "60"}


def prepare(compose: Path) -> subprocess.CompletedProcess:
    return _run(["bash", str(CORE / "backup_prepare.sh")], {"VKM_COMPOSE_DIR": str(compose),
                                                           "VKM_MANIFEST_TOOL": str(EDGE / "vkm_manifest.py"),
                                                           "VKM_BACKUP_REHASH_DAY": "0"})


def test_backup_chain_prepare_copy_verify_push_restore(tmp_path):
    data, compose = make_core(tmp_path)
    r = prepare(compose)
    assert r.returncode == 0, r.stdout + r.stderr
    latest = json.loads((data / "receipts/backup/source/LATEST.json").read_text(encoding="utf-8"))
    assert latest["files"] == 10 and latest["canonical_current"] == "snap-20260929T193550Z-738eebee"
    assert (data / "receipts/backup/edge").is_dir()
    env = backup_env(tmp_path, data)
    r = _run(["bash", str(EDGE / "vkm_backup.sh"), "--dry-run"], env)
    assert r.returncode == 0, r.stdout + r.stderr
    snaps = tmp_path / "edge" / "backup" / "snapshots"
    assert not snaps.exists() or not any(snaps.iterdir())
    r = _run(["bash", str(EDGE / "vkm_backup.sh")], env)
    assert r.returncode == 0, r.stdout + r.stderr
    status = json.loads((tmp_path / "edge/backup/status/latest.json").read_text(encoding="utf-8"))
    assert status["status"] == "DONE" and status["verdict"] == "PASS" and status["compare"]["equal"] == 10
    first = status["snapshot"]
    assert os.readlink(snaps / "latest") == first
    assert not (snaps / first / "tmp").exists() and not (snaps / first / "derived/embeddings/multivector/m/r/s/packs/P0").exists()
    pushed = json.loads((data / "receipts/backup/edge/latest.json").read_text(encoding="utf-8"))
    assert pushed["run_id"] == status["run_id"]
    # a second night: one new artifact; unchanged files are hard links to the first snapshot
    _write(data, "artifacts/png/ef/01/ef01.png", os.urandom(300))
    assert prepare(compose).returncode == 0
    r = _run(["bash", str(EDGE / "vkm_backup.sh")], env)
    assert r.returncode == 0, r.stdout + r.stderr
    status = json.loads((tmp_path / "edge/backup/status/latest.json").read_text(encoding="utf-8"))
    second = status["snapshot"]
    assert second != first and status["verdict"] == "PASS" and status["previous"] == first
    rel = "canonical/pages/source_id=VKM-SRC-001/run=RUN-1/part-00000.parquet"
    assert os.stat(snaps / first / rel).st_ino == os.stat(snaps / second / rel).st_ino
    assert status["transfer"]["files_transferred"] <= 3 and status["target_manifest"]["reused"] >= 9
    # restore a subset and verify it; then damage the store and let --verify-only find it
    rest = tmp_path / "restore"
    r = _run(["bash", str(EDGE / "vkm_restore.sh"), "--path", "receipts", "--path", "canonical", "--to", str(rest)],
             env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads((rest / ".vkm_restore/restore.json").read_text(encoding="utf-8"))["verdict"] == "PASS"
    assert (rest / rel).read_bytes() == (data / rel).read_bytes()
    assert _run(["bash", str(EDGE / "vkm_restore.sh"), "--to", str(rest)], env).returncode == 2   # not empty
    assert _run(["bash", str(EDGE / "vkm_restore.sh"), "--dry-run", "--path", "artifacts"], env).returncode == 0
    assert _run(["bash", str(EDGE / "vkm_restore.sh"), "--verify-only"], env).returncode == 0
    p = snaps / second / "artifacts/png/ab/cd/abcd.png"
    st = p.stat()
    blob = bytearray(p.read_bytes())
    blob[0] ^= 1
    p.write_bytes(bytes(blob))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _run(["bash", str(EDGE / "vkm_restore.sh"), "--verify-only"], env).returncode == 1


def test_backup_fails_without_a_fresh_manifest_and_keeps_nothing(tmp_path):
    data, _compose = make_core(tmp_path)
    env = backup_env(tmp_path, data)
    r = _run(["bash", str(EDGE / "vkm_backup.sh")], env)
    assert r.returncode == 1
    status = json.loads((tmp_path / "edge/backup/status/latest.json").read_text(encoding="utf-8"))
    assert status["status"] == "FAILED" and status["failed_step"] == "manifest" and "LATEST.json" in status["note"]


# ---------------------------------------------------------------- forced command of the backup key
def test_backup_gate_dispatch(tmp_path):
    data, compose = make_core(tmp_path)
    nightly = compose / "nightly"
    nightly.mkdir()
    shutil.copy(CORE / "vkm_backup_gate.sh", nightly / "vkm_backup_gate.sh")
    fake = tmp_path / "rrsync"
    fake.write_text('#!/bin/sh\necho "rrsync $*"\n', encoding="utf-8")
    fake.chmod(0o755)

    def gate(cmd):
        return _run(["sh", str(nightly / "vkm_backup_gate.sh")], {"SSH_ORIGINAL_COMMAND": cmd, "VKM_RRSYNC": str(fake)})

    r = gate("rsync --server --sender -logDtpre.iLsfxCIvu --files-from=- --from0 . /")
    assert r.returncode == 0 and r.stdout.strip() == f"rrsync -ro {data}"
    r = gate("rsync --server -logDtpre.iLsfxCIvu . /")
    assert r.returncode == 0 and r.stdout.strip() == f"rrsync -wo -no-del {data}/receipts/backup/edge"
    for bad in ("bash", "", "cat /etc/passwd", "sh -c 'rsync --server --sender . /'"):
        r = gate(bad)
        assert r.returncode == 1 and "only rsync" in r.stderr and not r.stdout
    r = _run(["sh", str(nightly / "vkm_backup_gate.sh"), "--dry-run"],
             {"SSH_ORIGINAL_COMMAND": "rsync --server -logDtpre.iLsfxCIvu . /", "VKM_RRSYNC": str(fake)})
    assert r.returncode == 0 and r.stdout.strip() == f"would exec: {fake} -wo -no-del {data}/receipts/backup/edge"
    (compose / ".env").write_text("", encoding="utf-8")
    assert gate("rsync --server --sender . /").returncode == 1


# ---------------------------------------------------------------- the 04:00 orchestrator with a fake docker
FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
out() { cat "$FAKE_OUT/$1"; }
all=("$@"); idx=-1
for ((i = 0; i < ${#all[@]}; i++)); do [ "${all[$i]}" = "vkm-nightly" ] && idx=$i; done
if [ "$idx" -ge 0 ] && [[ " $* " == *" run --rm -T "* ]]; then
  rest=("${all[@]:$((idx + 1))}")
  if [[ " $* " == *" --entrypoint python "* ]]; then            # the scorer: real topic_score.py, /data → host root
    mapped=()
    for a in "${rest[@]:1}"; do mapped+=("${a//\/data\//$FAKE_DATA/}"); done
    PYTHONPATH="$FAKE_SRC" exec python3 - "${mapped[@]}"
  fi
  case "${rest[*]}" in
    "canon validate"*) out canon.json ;;
    "duckdb status") out duckdb.json ;;
    "graph verify") out graph.json ;;
    "nav graph-verify --nav-dir /data/derived/navigation/"*) out nav.json ;;
    "search status") out search_status.json ;;
    "search smoke") out search.json ;;
    *) echo "unexpected vkm-nightly call: ${rest[*]}" >&2; exit 3 ;;
  esac
  exit 0
fi
case " $* " in
  *" ps --all --format json "*) out containers.jsonl ;;
  *" config --services "*) printf '%s\n' api mcp neo4j opensearch rx580-retrieval vkm-nightly ;;
  *" exec -T "*" rx580-retrieval curl "*) out rx580.json ;;
  *" exec -T "*" api vkm-corpus search hybrid-smoke "*"--late"*) out hybrid.json ;;
  *" exec -T api python -c "*|*" exec -T mcp python -c "*) exit 0 ;;
  *" exec -T -e VKM_NIGHTLY_MARK="*" mcp python - --deadline-s "*)
      cat > /dev/null; [ -z "${FAKE_SLEEP_MCP:-}" ] || sleep "$FAKE_SLEEP_MCP"; out mcp.json ;;
  *" exec -T -e VKM_NIGHTLY_MARK="*" api python - --budget "*) cat > "$FAKE_OUT/dossiers.bundle"; out dossiers.jsonl ;;
  *" exec -T -e VKM_NIGHTLY_MARK="*" api python - --systems hybrid_late --outlines seen "*)
      cat > "$FAKE_OUT/harness.bundle"; out topic_runs.jsonl ;;
  " ps -q "*|" ps -aq "*|" rm -f "*) : ;;
  *) echo "unexpected docker call: $*" >&2; exit 3 ;;
esac
"""


def nightly_host(tmp: Path) -> dict:
    """Synthetic CORE for nightly_checks.sh: data root, compose dir, topic_v1 files, canned answers, fake docker."""
    summary_tests = _load("nightly_summary_tests", Path(__file__).with_name("test_nightly_summary.py"))
    dossier_tests = _load("nightly_dossier_tests", Path(__file__).with_name("test_nightly_dossiers.py"))
    topic_tests = _load("nightly_topic_tests", Path(__file__).with_name("test_nightly_topic_score.py"))
    data, compose = make_core(tmp)
    cur = summary_tests.CUR
    (data / "canonical/CURRENT").write_text(cur + "\n", encoding="utf-8")
    (data / "derived/navigation/CURRENT").write_text(cur + "\n", encoding="utf-8")
    (data / f"derived/navigation/{cur}").mkdir(parents=True)
    _write(data, "derived/catalogues/CURRENT", "99f0ee6c6e05\n")
    _write(data, f"derived/embeddings/units/{cur}/vkm-units-v1-A/units.json",
           json.dumps({"count": 205784, "snapshot_id": cur}))
    _write(data, "derived/embeddings/multivector/m/r/s/packs/CURRENT",
           json.dumps({"pack_id": f"{cur}-2b0a341cb07a", "count": 205784, "snapshot_id": cur}))
    green = summary_tests.green_outputs()
    now = dt.datetime.now(dt.timezone.utc)
    green["backup"]["edge"]["finished_at"] = (now - dt.timedelta(hours=1)).isoformat(timespec="seconds")
    _write(data, "receipts/backup/edge/latest.json", json.dumps(green["backup"]["edge"]))
    _write(data, "receipts/backup/source/LATEST.json", json.dumps(green["backup"]["source"]))
    outdir = tmp / "fake_out"
    outdir.mkdir()
    names = {"canon": "canon.json", "duckdb": "duckdb.json", "graph": "graph.json", "nav": "nav.json",
             "search_status": "search_status.json", "search": "search.json", "rx580": "rx580.json",
             "hybrid": "hybrid.json", "mcp": "mcp.json"}
    for key, fname in names.items():
        (outdir / fname).write_text(json.dumps(green[key], ensure_ascii=False), encoding="utf-8")
    (outdir / "containers.jsonl").write_text(green["containers"] + "\n", encoding="utf-8")
    (outdir / "dossiers.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                                     for x in dossier_tests.dossier_lines(pc02_ok=True)) + "\n",
                                           encoding="utf-8")
    shutil.copy(topic_tests.runs_file(tmp), outdir / "topic_runs.jsonl")
    topic = tmp / "topic_v1"
    topic.mkdir()
    for f in ("SHA256SUMS", "topic_set_v1.jsonl", "topic_queries_v1.tsv", "metrics_spec_v1.json",
              "page_mapping_v1.json", "results_v1.json"):
        shutil.copy(BENCH / f, topic / f)
    shutil.copy(BENCH / "scripts" / "harness_core.py", topic / "harness_core.py")
    docker = tmp / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    env = {"VKM_COMPOSE_DIR": str(compose), "VKM_DOCKER": str(docker), "VKM_NIGHTLY_TOPIC_DIR": str(topic),
           "VKM_NIGHTLY_WAIT_BUSY_S": "0", "VKM_NIGHTLY_POLL_S": "1", "VKM_NIGHTLY_DEEP_WEEKDAY": "0",
           "FAKE_OUT": str(outdir), "FAKE_DATA": str(data), "FAKE_LOG": str(tmp / "docker.log"),
           "FAKE_SRC": str(ROOT / "src")}
    return {"env": env, "data": data, "out": outdir, "log": tmp / "docker.log", "cur": cur}


def test_nightly_dry_run_checks_the_configuration(tmp_path):
    h = nightly_host(tmp_path)
    r = _run(["bash", str(CORE / "nightly_checks.sh"), "--dry-run"], h["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dossiers planned: 117 topics" in r.stdout and "MCP smoke plans 45 of 45" in r.stdout
    assert not (h["data"] / "receipts" / "nightly").exists()


def test_nightly_run_writes_the_summary_dossiers_and_scores(tmp_path):
    h = nightly_host(tmp_path)
    r = _run(["bash", str(CORE / "nightly_checks.sh")], h["env"], timeout=600)
    assert r.returncode in (0, 1), r.stdout + r.stderr                   # 1 only if the test disk is nearly full
    base = h["data"] / "receipts" / "nightly"
    date = (base / "LATEST").read_text(encoding="utf-8").strip()
    run = base / date
    steps = [json.loads(x) for x in (run / "steps.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [s["step"] for s in steps] == ["containers", "disk", "canon", "duckdb", "graph", "nav", "search_status",
                                          "search", "rx580", "hybrid", "vectors", "mcp", "dossiers", "topic_v1",
                                          "backup"]
    assert all(s["rc"] == 0 for s in steps), steps
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    status = {c["id"]: c["status"] for c in summary["checks"]}
    assert all(v == "PASS" for k, v in status.items() if k != "disk"), status
    assert len((run / "summary.md").read_text(encoding="utf-8").splitlines()) <= 25
    assert (base / "STATUS").read_text(encoding="utf-8").split()[1] == date
    ddir = h["data"] / "derived" / "dossiers"
    assert (ddir / "CURRENT").read_text(encoding="utf-8").strip() == h["cur"]
    assert (ddir / h["cur"] / "index.json").is_file() and (run / "dossiers_index.json").is_file()
    score = json.loads((run / "raw" / "topic_v1.out").read_text(encoding="utf-8"))
    assert score["n_queries"] == 351 and score["summary"]["success@10"] == 1.0
    assert (h["out"] / "harness.bundle").read_text(encoding="utf-8").startswith('SET_JSONL = r"""')
    log = h["log"].read_text(encoding="utf-8")
    assert "--profile nightly run --rm -T --name vkm-nightly-canon-" in log and "canon validate" in log
    # a second run of the same day keeps the first one aside and compares with it
    r = _run(["bash", str(CORE / "nightly_checks.sh"), "--only", "disk,backup"], h["env"], timeout=600)
    assert r.returncode in (0, 1), r.stdout + r.stderr
    assert any(p.name.startswith(f"{date}-rerun-") for p in base.iterdir())
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["prev"]["dir"].startswith(f"{date}-rerun-")
    assert {c["id"]: c["status"] for c in summary["checks"]}["graph"] == "SKIP"


def test_a_step_timeout_is_a_failure_and_the_run_goes_on(tmp_path):
    h = nightly_host(tmp_path)
    env = {**h["env"], "FAKE_SLEEP_MCP": "30", "VKM_NIGHTLY_TIMEOUT_MCP": "2"}
    r = _run(["bash", str(CORE / "nightly_checks.sh"), "--only", "mcp,backup"], env, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr                        # RED
    base = h["data"] / "receipts" / "nightly"
    run = base / (base / "LATEST").read_text(encoding="utf-8").strip()
    steps = {s["step"]: s for s in map(json.loads, (run / "steps.jsonl").read_text(encoding="utf-8").splitlines())}
    assert steps["mcp"]["rc"] == 124 and steps["backup"]["rc"] == 0 and steps["graph"]["skipped"]
    checks = {c["id"]: c for c in json.loads((run / "summary.json").read_text(encoding="utf-8"))["checks"]}
    assert checks["mcp"]["status"] == "FAIL" and "превышено время" in checks["mcp"]["detail"]
    log = h["log"].read_text(encoding="utf-8")
    assert "rm -f vkm-nightly-mcp-" in log and "exec -T mcp python -c" in log     # container and client stopped
    assert _run(["bash", str(CORE / "nightly_checks.sh"), "--only", "nope"], h["env"]).returncode == 2
    assert _run(["bash", str(CORE / "nightly_checks.sh")], {**h["env"], "VKM_NIGHTLY_TIMEOUT_MCP": "x"}).returncode == 2
