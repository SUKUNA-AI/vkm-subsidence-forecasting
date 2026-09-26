"""Fast tests of scripts/verify_canonical_repository.py (report structure, planted failures, git-object closures).

Planted host paths are assembled at run time so that this file itself carries none.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_canonical_repository.py"
REGISTRY = ROOT / "scripts" / "frozen_references.json"
STATUSES = {"PASS", "FAIL", "WARN", "SKIPPED", "SKIPPED_REF_UNAVAILABLE"}


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_canonical_repository", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module            # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


V = _load_verifier()


def _tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def _checks(report: dict) -> dict[str, dict]:
    return {c["id"]: c for c in report["checks"]}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@example.org",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _has_ref(ref: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          capture_output=True).returncode == 0


LEGACY_AVAILABLE = _has_ref("legacy") or _has_ref("origin/legacy")


# ---------------------------------------------------------------- report structure / never raises
def test_report_structure_is_json_and_complete(tmp_path):
    root = _tree(tmp_path, {"README.md": "[ok](docs/a.md) [dir](docs) [web](https://example.org/x) [top](#t)\n",
                            "docs/a.md": "# a\n", "cfg.yaml": "input: data/x.csv\n"})
    report = V.verify(root, groups=["markdown_links", "host_paths"], use_env=False)
    json.loads(json.dumps(report))
    assert list(report)[0] == "status"
    assert report["status"] == "PASS" and report["exit_code"] == 0 and report["blocking_failures"] == []
    assert report["guarantees"]["models_executed"] == 0 and report["guarantees"]["evaluator_truth_parsed"] is False
    for check in report["checks"]:
        assert set(check) == {"id", "group", "status", "blocking", "summary", "details"}
        assert check["status"] in STATUSES


def test_never_raises_on_missing_root_or_broken_registry(tmp_path):
    report = V.verify(tmp_path / "nope", registry_path=tmp_path / "missing.json", use_env=False)
    json.dumps(report)
    assert report["status"] == "FAIL" and report["exit_code"] == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    report = V.verify(tmp_path, registry_path=bad, groups=["registry", "frozen_references", "retired_references"],
                      use_env=False)
    checks = _checks(report)
    assert checks["registry:load"]["status"] == "FAIL" and report["exit_code"] == 1
    bad.write_text(json.dumps({"references": [{"id": "X", "path": "a.json", "sha256": "abc"}]}), encoding="utf-8")
    report = V.verify(tmp_path, registry_path=bad, groups=["registry", "frozen_references", "retired_references"],
                      use_env=False)
    checks = _checks(report)
    assert checks["registry:schema"]["status"] == "FAIL"
    assert checks["frozen_references:registry"]["status"] == "SKIPPED"
    assert not any(c["id"].endswith(":error") for c in report["checks"])


def test_cli_writes_report_and_returns_exit_code(tmp_path, capsys):
    root = _tree(tmp_path, {"README.md": "[gone](missing.md)\n"})
    code = V.main(["--root", str(root), "--only", "markdown_links", "--output"])
    stdout = json.loads(capsys.readouterr().out)
    written = json.loads((root / V.DEFAULT_OUTPUT).read_text(encoding="utf-8"))
    assert code == 1 and stdout["status"] == written["status"] == "FAIL"


# ---------------------------------------------------------------- planted failures
def test_planted_broken_link_fails(tmp_path):
    root = _tree(tmp_path, {
        "README.md": "[gone](docs/missing.md) [ok](README.md#x) `[span](nowhere.md)`\n"
                     "```\n[fenced](nowhere.md)\n```\n[ref]: docs/also_missing.md\n",
    })
    report = V.verify(root, groups=["markdown_links"], use_env=False)
    link = _checks(report)["markdown:links"]
    assert link["status"] == "FAIL" and link["blocking"] and report["exit_code"] == 1
    assert sorted(b["target"] for b in link["details"]["broken"]["items"]) == ["docs/also_missing.md",
                                                                             "docs/missing.md"]


def test_links_into_externalized_trees_are_nonblocking(tmp_path):
    root = _tree(tmp_path, {"docs/r.md": "[pdf](../inputs/sources/primary/x.pdf)\n"})
    report = V.verify(root, registry_path=REGISTRY, groups=["markdown_links"], use_env=False)
    checks = _checks(report)
    assert checks["markdown:links"]["status"] == "PASS"
    assert checks["markdown:offloaded_links"]["status"] == "WARN" and report["exit_code"] == 0


def test_planted_absolute_and_windows_paths_fail(tmp_path):
    home = "/" + "home" + "/someone/data.csv"
    drive = "D" + ":" + "\\" + "data\\x.csv"
    venv = "." + "\\" + ".venv" + "\\" + "Scripts" + "\\" + "python.exe run.py"
    root = _tree(tmp_path, {
        "configs/run.yaml": f"input: {home}\n",
        "scripts/run.py": f"P = r'{drive}'\n",
        "scripts/hint.json": json.dumps({"command": venv}) + "\n",
        "scripts/ok.py": "URL = 'https://example.org/home/x'\nP = 'data/x.csv'\n",
        "scripts/pragma.py": f"P = '{home}'  # host-path-ok: documented example\n",
        "tests/test_guard.py": f"BAD = '{home}'\n",
        "docs/reset_2026_09/run_kit/tool.py": f"P = '{home}'\n",
        "docs/note.md": f"{home} is fine in prose\n",
    })
    report = V.verify(root, groups=["host_paths"], use_env=False)
    check = _checks(report)["host_paths:code_config"]
    assert check["status"] == "FAIL" and report["exit_code"] == 1
    hits = {(h["file"], h["kind"]) for h in check["details"]["hits"]["items"]}
    assert hits == {("configs/run.yaml", "posix_host_dir"), ("scripts/run.py", "windows_drive"),
                    ("scripts/hint.json", "windows_venv")}
    assert check["details"]["hits_in_exemptions"] == {"docs/reset_2026_09/run_kit/": 1, "tests/": 1}


# ---------------------------------------------------------------- private register / public catalogue
def test_private_register_and_public_catalogue(tmp_path):
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{_sha(b'C')}\nsize 1\n".encode()
    res = _tree(tmp_path / "res", {"01_primary_sources/a.bin": b"A", "02_dissertations/c.bin": pointer})
    header = "resource_id,canonical_path,sha256,size_bytes,evidence_scope,migration_status\n"
    rows = [f"VKM-SRC-001,01_primary_sources/a.bin,{_sha(b'A')},1,VKM_regional,ADDED_BY_USER_EXACT",
            f"VKM-SRC-002,02_dissertations/c.bin,{_sha(b'C')},1,VKM_regional,ADDED_BY_USER_EXACT",
            f"VKM-SRC-003,08_data_archives/old.zip,{_sha(b'Z')},1,LEGACY_RETIRED,RETIRED_FROM_CURRENT_RESEARCH"]
    _tree(res, {"00_registry/SOURCE_REGISTER.csv": header + "\n".join(rows) + "\n"})
    pub = _tree(tmp_path / "pub", {"evidence/sources/source_catalogue.csv":
                                   f"source_id,title,sha256\nVKM-SRC-001,a,{_sha(b'A')}\nVKM-SRC-002,c,{_sha(b'X')}\n"
                                   f"VKM-SRC-999,z,{_sha(b'A')}\nVKM-SRC-003,old,\n"})
    report = V.verify(pub, resources_root=res, groups=["private_sources"], use_env=False)
    checks = _checks(report)
    assert checks["private:register"]["status"] == "PASS"
    files = checks["private:registered_files"]
    assert files["status"] == "WARN" and files["details"]["verified"] == 1        # WARN: one pointer not materialized
    pointer_entry = files["details"]["lfs_pointers_skipped"]["items"][0]
    assert pointer_entry["status"] == "SKIPPED_LFS_POINTER" and pointer_entry["pointer_oid_matches_register"]
    assert [a["id"] for a in files["details"]["absent_by_register_status"]] == ["VKM-SRC-003"]
    catalogue = checks["private:public_catalogues"]
    assert catalogue["status"] == "FAIL" and report["exit_code"] == 1
    assert sorted(p["id"] for p in catalogue["details"]["problems"]["items"]) == ["VKM-SRC-002", "VKM-SRC-999"]
    # without VKM_RESOURCES_ROOT the group is skipped, not failed
    report = V.verify(pub, groups=["private_sources"], use_env=False)
    assert _checks(report)["private:configured"]["status"] == "SKIPPED" and report["exit_code"] == 0


# ---------------------------------------------------------------- frozen references from git objects
def test_closures_from_git_objects_in_synthetic_repo(tmp_path):
    root = tmp_path / "repo"
    gen, a_csv, payload = b"print('gen')\n", b"x\n1\n", b"payload"
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{_sha(payload)}\nsize {len(payload)}\n"
    good = {"inputs": [{"path": "src/gen.py", "sha256": _sha(gen)}],
            "outputs": [{"path": "a.csv", "sha256": _sha(a_csv), "size_bytes": len(a_csv)},
                        {"path": "big.bin", "sha256": _sha(payload), "size_bytes": len(payload)}]}
    bad = {"outputs": [{"path": "../good/a.csv", "sha256": _sha(b"other")}]}
    inventory = f"relative_path,size_bytes,sha256\ndata/good/a.csv,{len(a_csv)},{_sha(a_csv)}\n"
    files = {"src/gen.py": gen, "data/good/a.csv": a_csv, "data/good/big.bin": pointer,
             "data/good/manifest.json": json.dumps(good), "data/bad/manifest.json": json.dumps(bad),
             "data/inventory.csv": inventory}
    _tree(root, files)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "frozen")
    head = _git(root, "rev-parse", "HEAD")

    def ref(rid, path, closure):
        return {"id": rid, "path": path, "sha256": _sha((root / path).read_bytes()), "commit": head,
                "verify_at": [head, "0" * 40], "closure": closure}

    manifest_rule = {"rule": "json_manifest", "sections": ["inputs", "outputs"], "relative_to_manifest_dir": ["outputs"]}
    registry = {"closure_rules": {"none": "", "json_manifest": "", "csv_inventory": ""},
                "nested_manifest_names": ["manifest.json"], "retired_prefixes": [], "externalized_prefixes": [],
                "anchors": [{"name": "frozen/test", "kind": "tag", "commit": head}],
                "references": [ref("GOOD", "data/good/manifest.json", manifest_rule),
                               ref("BAD", "data/bad/manifest.json", manifest_rule),
                               ref("INV", "data/inventory.csv", {"rule": "csv_inventory", "path_column": "relative_path"})]}
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    # the working tree no longer holds the bytes: verification uses git objects only
    for rel in files:
        (root / rel).unlink()
    report = V.verify(root, registry_path=registry_path, groups=["registry", "frozen_references"], use_env=False)
    checks = _checks(report)
    short = head[:7]
    assert checks["registry:schema"]["status"] == "PASS"
    assert checks[f"frozen:GOOD@{short}"]["status"] == "PASS"
    assert checks[f"frozen:GOOD@{short}"]["details"]["by_method"] == {"git_blob": 2, "lfs_oid": 1}
    assert checks[f"frozen:INV@{short}"]["status"] == "PASS"
    assert checks[f"frozen:BAD@{short}"]["status"] == "FAIL"
    assert checks[f"frozen:BAD@{short}"]["details"]["errors"]["items"][0]["kind"] == "hash_or_size"
    skipped = checks["frozen:GOOD@0000000"]
    assert skipped["status"] == "SKIPPED_REF_UNAVAILABLE" and not skipped["blocking"]
    assert "how_to_fix" in skipped["details"]
    assert checks["frozen:anchor:tag:frozen/test"]["status"] == "SKIPPED_REF_UNAVAILABLE"
    assert report["blocking_failures"] == [f"frozen:BAD@{short}"] and report["exit_code"] == 1


def test_frozen_registry_is_well_formed():
    report = V.verify(ROOT, registry_path=REGISTRY, groups=["registry"], use_env=False)
    assert report["status"] == "PASS", report["checks"]
    pins = {r["id"]: r["sha256"] for r in json.loads(REGISTRY.read_text(encoding="utf-8"))["references"]}
    assert pins["SKRU1_SCENARIO_SIMULATION_V2_1"].startswith("a268cd78")
    assert pins["SKRU1_SCENARIO_REPRESENTATION_V2_1_R1"].startswith("0e5501cb")
    assert pins["GATE_B3_T1_B3_V1"].startswith("127fa194")


@pytest.mark.skipif(not LEGACY_AVAILABLE, reason="branch 'legacy' not available locally (shallow clone?)")
def test_frozen_references_verify_against_legacy(tmp_path):
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for entry in registry["references"]:
        entry["verify_at"] = ["legacy"]
    registry["anchors"] = [a for a in registry["anchors"] if a["kind"] == "branch"]
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    report = V.verify(ROOT, registry_path=path, groups=["registry", "frozen_references", "retired_references"],
                      use_env=False)
    checks = _checks(report)
    for entry in registry["references"]:
        assert checks[f"frozen:{entry['id']}@legacy"]["status"] == "PASS", checks[f"frozen:{entry['id']}@legacy"]
    assert checks["retired:registry_consistency"]["status"] == "PASS"
    assert report["exit_code"] == 0
