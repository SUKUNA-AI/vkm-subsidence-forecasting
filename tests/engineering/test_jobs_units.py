"""Units of the job layer without long-running processes: root validation, path jail, redaction and public-text
checks, checks and parameters, file locks, job_read (offset, grep, jail), job_list paging, publishing receipts."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vkm_jobs.checks import run_check
from vkm_jobs.errors import ToolFailure
from vkm_jobs.pools import FileLock, lock_is_held, pool_capacity
from vkm_jobs.redact import Redactor, public_text_problems
from vkm_jobs.roots import SimRoot, check_root, clean_relpath, repo_root, resolve_inside
from vkm_jobs.spec import clamp_timeout, new_job_id, validate_check, validate_param

from jobs_fakes import OK_APP, service, submit

ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------------------------------------ roots and jail
def test_sim_root_rules(tmp_path):
    assert check_root(tmp_path / "ok_root") is None
    assert "ASCII" in check_root(tmp_path / "корень")
    assert "spaces" in check_root(tmp_path / "with space")
    assert "absolute" in check_root(Path("relative/root"))
    assert repo_root() is not None and (repo_root() / "src" / "vkm_jobs").is_dir()
    assert "PUBLIC" in check_root(tmp_path / "pub" / "work" / "sim", {"VKM_PUBLIC_ROOT": str(tmp_path / "pub")})
    assert "VKM_RESOURCES_ROOT" in check_root(tmp_path / "res" / "sim", {"VKM_RESOURCES_ROOT": str(tmp_path / "res")})
    unset = SimRoot.from_env({"VKM_SIM_ROOT": "${VKM_SIM_ROOT}"})
    assert unset.path is None and "set VKM_SIM_ROOT" in unset.reason
    with pytest.raises(ToolFailure) as exc:
        unset.require()
    assert exc.value.code == "SIM_ROOT_UNAVAILABLE"
    root = SimRoot.at(tmp_path / "sim")
    assert root.require() == root.path and (root.path / "jobs").is_dir() and (root.path / "matlab/session").is_dir()
    assert root.logical(root.path / "jobs" / "x") == "<VKM_SIM_ROOT>/jobs/x"
    assert root.describe()["logical"] == "<VKM_SIM_ROOT>" and "free_gb" in root.describe()


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "C:/x", "c:\\x", "\\\\host\\share\\x", "a:b",
                                 "", "  ", "a/\x00"])
def test_clean_relpath_refuses_escapes(bad):
    with pytest.raises(ToolFailure) as exc:
        clean_relpath(bad)
    assert exc.value.code in ("PATH_OUTSIDE_ROOT", "INVALID_ARGUMENT")


def test_clean_relpath_normalises_and_resolve_inside(tmp_path):
    assert clean_relpath("logs\\stdout.log") == "logs/stdout.log"
    assert clean_relpath("./out//result.json") == "out/result.json"
    (tmp_path / "base" / "logs").mkdir(parents=True)
    assert resolve_inside(tmp_path / "base", "logs/x.log") == tmp_path / "base" / "logs" / "x.log"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "base" / "link"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))                  # junctions need no privilege
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolFailure) as exc:
        resolve_inside(tmp_path / "base", "link/x")
    assert exc.value.code == "PATH_OUTSIDE_ROOT"


# ------------------------------------------------------------------------------------------------ redaction
# negative fixtures are assembled at run time so that this file itself passes the hygiene and leakage scans
DRIVE_D = "D" + ":"
HOME_DIR = "/" + "home" + "/someone"
PRIVATE_IP = ".".join(["192", "168", "1", "7"])


def test_redactor_rewrites_roots_names_and_unknown_paths(tmp_path):
    sim = tmp_path / "sim"
    red = Redactor({"<VKM_SIM_ROOT>": str(sim), "<MATLAB_ROOT>": str(tmp_path / "ML")})
    text = (f"Error in {sim}\\jobs\\X\\in\\code\\f.m (line 3); "
            f"json {json.dumps(str(sim / 'jobs'))}; toolbox {tmp_path / 'ML' / 'toolbox' / 'stats'}; "
            f"other {DRIVE_D}\\elsewhere\\file.txt and {HOME_DIR}/x and {PRIVATE_IP}")
    out = red.text(text)
    assert "<VKM_SIM_ROOT>/jobs/X/in/code/f.m (line 3)" in out
    assert '"<VKM_SIM_ROOT>/jobs"' in out and "<MATLAB_ROOT>/toolbox/stats" in out
    assert f"{DRIVE_D}\\elsewhere" not in out and "<ABS_PATH>" in out and HOME_DIR not in out and "<IP>" in out
    assert str(tmp_path) not in out and red.text(out) == out                        # idempotent
    assert red.obj({"a": [str(sim)], str(sim): 1}) == {"a": ["<VKM_SIM_ROOT>"], "<VKM_SIM_ROOT>": 1}


def test_public_text_problems():
    assert public_text_problems('{"a": "<VKM_SIM_ROOT>/jobs/x", "v": "25.2.0.2998904"}') == []
    for bad in ("C" + ":\\Users\\x", DRIVE_D + "/data/x", HOME_DIR + "/x", "\\\\host\\share\\x",
                ".".join(["10", "1", "2", "3"]), "pass" + "word: 'hunter2hunter2'"):
        assert public_text_problems(bad), bad


@pytest.mark.parametrize("identity", ["runner", "root", "service", "private-audit-user"])
def test_static_hygiene_is_host_independent_but_runtime_values_are_redacted(monkeypatch, identity):
    import vkm_jobs.redact as module

    monkeypatch.setattr(module.getpass, "getuser", lambda: identity)
    monkeypatch.setattr(module.socket, "gethostname", lambda: "private-audit-host")
    assert public_text_problems("runner executes a service at the root") == []
    assert public_text_problems(identity, identifiers=[identity])
    red = Redactor(include_defaults=False)
    payload = {"runner": {"python": "3.13.5"}, "root": "logical", "service": "logical", "note": identity}
    assert red.obj(payload) == {"runner": {"python": "3.13.5"}, "root": "logical", "service": "logical",
                                "note": "<NAME>"}


def test_dynamic_identity_keys_are_redacted_and_collisions_block():
    red = Redactor(include_defaults=False, identifiers=["runner", "private-audit-host"])
    assert red.obj({"runner": {"python": "3.13.5"}, "meta": {"runner": "value"}}) == {
        "runner": {"python": "3.13.5"}, "meta": {"<NAME>": "value"}}
    for obj in ({"meta": {"runner": 1, "private-audit-host": 2}},
                {"meta": {"runner": 1, "<NAME>": 2}}):
        with pytest.raises(ToolFailure, match="merge distinct"):
            red.obj(obj)


def test_path_key_collision_cannot_drop_data(tmp_path):
    red = Redactor({"<VKM_WORK>": tmp_path}, include_defaults=False, identifiers=[])
    with pytest.raises(ToolFailure, match="merge distinct"):
        red.obj({str(tmp_path): 1, "<VKM_WORK>": 2})


# ------------------------------------------------------------------------------------------------ spec pieces
def test_job_ids_timeouts_checks_params():
    job_id = new_job_id("MATLAB")
    assert job_id.startswith("MATLAB-") and len(job_id.split("-")[2]) == 8
    with pytest.raises(ValueError):
        new_job_id("EXCEL")
    assert clamp_timeout("matlab", None) == 1800
    assert clamp_timeout("matlab", 10.2) == 11
    for bad in (0, -1, 24 * 3600 + 1, True, float("nan")):
        with pytest.raises(ToolFailure):
            clamp_timeout("matlab", bad)
    chk = validate_check({"kind": "json_value", "pointer": "/s", "expected": 136}, default_path="out/result.json")
    assert chk["path"] == "out/result.json" and chk["name"] == "json_value"
    assert validate_check({"kind": "number_close", "path": "a.json", "expected": 1.0})["rtol"] == 1e-9
    for bad in ({"kind": "nope"}, {"kind": "text_contains", "path": "x", "expected": 3},
                {"kind": "json_value", "path": "../x", "expected": 1}, {"kind": "exit_code", "expected": "0"},
                {"kind": "json_value", "path": "x.json", "pointer": "s", "expected": 1}):
        with pytest.raises(ToolFailure):
            validate_check(bad)
    ok = validate_param({"name": "E", "value": 2.5e9, "unit": "Pa", "status": "ENGINEERING_ASSUMPTION",
                         "source_ref": "toy"})
    assert ok["status"] == "ENGINEERING_ASSUMPTION" and ok["unit"] == "Pa"
    assert validate_param({"name": "q", "status": "UNKNOWN"})["value"] is None
    for bad in ({"name": "E", "value": 1.0}, {"name": "E", "value": 1.0, "status": "GUESS", "source_ref": "x"},
                {"name": "E", "value": 1.0, "status": "FACT"}, {"name": "E", "value": 1.0, "status": "UNKNOWN"},
                {"name": "1E", "value": 1.0, "status": "FACT", "source_ref": "src"}):
        with pytest.raises(ToolFailure):
            validate_param(bad)


def test_run_check_kinds(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "r.json").write_text(json.dumps({"a": {"b": [1, 2.5]}, "flag": True, "t": "x"}), "utf-8")
    (tmp_path / "log.txt").write_text("all good\n", "utf-8")

    def chk(**kw):
        return run_check(tmp_path, validate_check(kw), 0)["passed"]

    assert chk(kind="json_value", path="out/r.json", pointer="/a/b/1", expected=2.5)
    assert not chk(kind="json_value", path="out/r.json", pointer="/flag", expected=1)    # bool is not 1
    assert chk(kind="json_value", path="out/r.json", pointer="/a/b", expected=[1, 2.5])
    assert chk(kind="number_close", path="out/r.json", pointer="/a/b/0", expected=1.0005, rtol=1e-3)
    assert not chk(kind="number_close", path="out/r.json", pointer="/a/b/9", expected=1)
    assert chk(kind="file_exists", path="out/*.json") and not chk(kind="file_exists", path="out/none.json")
    assert chk(kind="text_contains", path="log.txt", expected="good")
    assert chk(kind="text_absent", path="log.txt", expected="ERROR")
    assert chk(kind="exit_code", expected=0)
    assert run_check(tmp_path, validate_check({"kind": "json_value", "path": "log.txt", "expected": 1}), 0)[
        "message"].startswith("not JSON")


# ------------------------------------------------------------------------------------------------ locks
def test_file_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / "locks" / "x.lock"
    a, b = FileLock(path), FileLock(path)
    assert a.try_acquire() and not b.try_acquire() and lock_is_held(path)
    a.release()
    assert not lock_is_held(path) and b.try_acquire()
    b.release()
    assert pool_capacity("ansys", {}) == 1 and pool_capacity("dpf", {}) == 2
    assert pool_capacity("ansys", {"VKM_POOL_CAPACITY_ANSYS": "2"}) == 2
    assert pool_capacity("unknown", {}) == 1


# ------------------------------------------------------------------------------------------------ read, list, publish
def test_read_offsets_grep_and_jail(tmp_path):
    jobs = service(tmp_path)
    ref, draft = submit(jobs, OK_APP, wait_s=60)
    job_id = ref["job_id"]
    whole = jobs.read(job_id, "logs/stdout.log")
    assert "line one" in whole["text"] and whole["eof"] is True
    first = jobs.read(job_id, "logs/stdout.log", max_chars=5)
    assert len(first["text"]) == 5 and first["eof"] is False
    rest = jobs.read(job_id, "logs/stdout.log", offset=first["next_offset"])
    assert first["text"] + rest["text"] == whole["text"]
    grep = jobs.read(job_id, "logs/stdout.log", grep="ERROR")
    assert grep["count"] == 1 and grep["matches"][0]["text"].startswith("ERROR")
    for bad, code in (("../job.json", "PATH_OUTSIDE_ROOT"), ("C:/x", "PATH_OUTSIDE_ROOT"), ("job.json",
                                                                                              "INVALID_ARGUMENT"),
                      ("out/none.txt", "NOT_FOUND")):
        with pytest.raises(ToolFailure) as exc:
            jobs.read(job_id, bad)
        assert exc.value.code == code, bad
    with pytest.raises(ToolFailure) as exc:
        jobs.read(job_id, "logs/stdout.log", grep="(")
    assert exc.value.code == "INVALID_ARGUMENT"
    (draft.dir / "out" / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert jobs.read(job_id, "out/blob.bin")["binary"] is True
    (draft.dir / "out" / "utf8.txt").write_text("ааа", encoding="utf-8")                  # 2-byte characters
    part = jobs.read(job_id, "out/utf8.txt", max_chars=3)
    assert part["text"] == "а" and part["next_offset"] == 2


def test_list_filters_and_cursor(tmp_path):
    jobs = service(tmp_path)
    ids = [submit(jobs, "print(1)", wait_s=60)[0]["job_id"] for _ in range(3)]
    failed = submit(jobs, "import sys; sys.exit(2)", wait_s=60)[0]["job_id"]
    page = jobs.list(limit=2)
    assert page["count"] == 2 and page["next_cursor"]
    page2 = jobs.list(limit=10, cursor=page["next_cursor"])
    seen = [j["job_id"] for j in page["jobs"] + page2["jobs"]]
    assert sorted(seen) == sorted(ids + [failed]) and len(set(seen)) == 4
    assert [j["job_id"] for j in jobs.list(status="FAILED")["jobs"]] == [failed]
    assert jobs.list(app="MATLAB")["count"] == 0
    with pytest.raises(ToolFailure):
        jobs.list(limit=0)
    with pytest.raises(ToolFailure) as exc:
        jobs.status("PY-20260101T000000Z-00000000")
    assert exc.value.code == "NOT_FOUND"


def test_publish_receipt_sanitised_and_no_overwrite(tmp_path):
    jobs = service(tmp_path)
    ref, _ = submit(jobs, OK_APP, wait_s=60)
    out = jobs.publish_receipt(ref["job_id"], "fake_smoke")
    assert out["path"] == "docs/engineering_tools/receipts/fake_smoke.json" and out["leakage_scan"] == "PASS"
    text = (tmp_path / "public" / out["path"]).read_text(encoding="utf-8")
    assert public_text_problems(text) == [] and str(tmp_path) not in text
    assert json.loads(text)["job_id"] == ref["job_id"]
    with pytest.raises(ToolFailure) as exc:
        jobs.publish_receipt(ref["job_id"], "fake_smoke")
    assert exc.value.code == "WOULD_OVERWRITE"
    assert jobs.publish_receipt(ref["job_id"], "fake_smoke", overwrite=True)["overwritten"] is True
    with pytest.raises(ToolFailure) as bad:
        jobs.publish_receipt(ref["job_id"], "Bad-Name")
    assert bad.value.code == "INVALID_ARGUMENT"


def test_public_tree_of_the_job_layer_has_no_machine_paths():
    for rel in ("src/vkm_jobs",):
        for path in (ROOT / rel).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert public_text_problems(text) == [], path.name
    assert os.sep in str(ROOT)                                             # sanity: the repo root was found
