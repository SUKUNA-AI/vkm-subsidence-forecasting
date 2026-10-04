"""CI must reject empty, entirely skipped, and broken collections."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("offline_checks", ROOT / "scripts/run_offline_checks.py")
CHECKS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKS
SPEC.loader.exec_module(CHECKS)


def test_recursive_selection_is_complete_disjoint_and_discovers_new_suites(tmp_path):
    files = ["world/test_a.py", "corpus/deeper/test_b.py", "engineering/test_c.py", "future/deeper/newname_test.py"]
    for file in files:
        path = tmp_path / "tests" / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_ok(): pass\n")
    world = set(CHECKS.test_files(tmp_path, "world-integrity"))
    corpus = set(CHECKS.test_files(tmp_path, "corpus-offline"))
    assert not world & corpus
    assert world | corpus == set((tmp_path / "tests").rglob("*.py"))
    assert tmp_path / "tests/future/deeper/newname_test.py" in corpus


def test_configured_python_files_are_honoured(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n')
    (tmp_path / "tests/future").mkdir(parents=True)
    target = tmp_path / "tests/future/check_custom.py"
    target.write_text("def test_ok(): pass\n")
    assert CHECKS.test_files(tmp_path, "corpus-offline") == [target]


@pytest.mark.parametrize("stats", [
    {"selected": 0, "passed": 0},
    {"selected": 2, "passed": 0, "skipped": 2},
    {"selected": 2, "passed": 1, "collection_errors": ["bad import"]},
    {"selected": 2, "passed": 1, "failed": 1},
    {"selected": 2, "passed": 1, "unexpected_skips": ["new skip"]},
    {"selected": 2, "passed": 1, "xfailed": 1},
    {"selected": 2, "passed": 1, "xpassed": 1},
])
def test_false_green_stats_fail(stats):
    assert CHECKS.test_status(stats, 0) == "FAIL"


def test_known_missing_host_tool_is_not_counted_as_pass():
    stats = {"selected": 2, "passed": 1, "skipped": 1,
             "not_run": [{"nodeid": "tests/corpus/test_extract_djvu.py", "reason": "DjVuLibre not installed (NOT_RUN)"}]}
    assert CHECKS.test_status(stats, 0) == "PASS_WITH_NOT_RUN"


def test_external_deselection_is_not_counted_as_pass():
    stats = {"selected": 1, "passed": 1, "deselected": [{"nodeid": "test_gpu", "status": "NOT_RUN"}]}
    assert CHECKS.test_status(stats, 0) == "PASS_WITH_NOT_RUN"


def test_skip_allowance_is_module_and_reason_specific():
    assert CHECKS.allowed_skip("tests/corpus/test_extract_djvu.py", "DjVuLibre not installed (NOT_RUN)")
    assert not CHECKS.allowed_skip("tests/corpus/test_figures_raster.py", "could not import 'cv2': No module named 'cv2'")
    assert not CHECKS.allowed_skip("tests/world/test_foundation.py", "could not import 'cv2': No module named 'cv2'")
    assert not CHECKS.allowed_skip("tests/corpus/test_figures_raster.py", "unrelated new reason")
    missing = "could not import 'cv2': No module named 'cv2'"
    assert not CHECKS.allowed_skip("tests/corpus/test_figures_pdf.py::test_glyph_outline_date_labels_are_read", missing)
    assert not CHECKS.allowed_skip("tests/corpus/test_figures_pdf.py::test_unrelated_new_check", missing)


@pytest.mark.parametrize("nodeid,reason", CHECKS.WINDOWS_NOT_RUN)
def test_posix_specific_skips_are_only_allowed_on_windows(nodeid, reason):
    assert CHECKS.allowed_skip(nodeid, reason, host_platform="win32")
    assert not CHECKS.allowed_skip(nodeid, reason, host_platform="linux")
    assert not CHECKS.allowed_skip(nodeid, "new unrelated reason", host_platform="win32")
    assert CHECKS.test_status({"selected": 2, "passed": 1, "skipped": 1,
                              "not_run": [{"nodeid": nodeid, "reason": reason}]}, 0) == "PASS_WITH_NOT_RUN"


def test_missing_windows_symlink_privilege_is_still_a_failure():
    assert not CHECKS.allowed_skip("tests/qgis/test_unit.py::test_symlink_escape_if_supported",
                                   "Symlink privilege unavailable", host_platform="win32")
    assert not CHECKS.allowed_skip("tests/corpus/test_backup_manifest.py::test_symlinks_are_never_followed",
                                   "symlinks are not permitted here", host_platform="win32")


def test_windows_selection_is_explicit_and_does_not_include_live_corpus(tmp_path):
    for rel in ("tests/world/test_validation.py", "tests/engineering/test_jobs_units.py", "tests/qgis/test_unit.py",
                "tests/corpus/test_nightly_summary.py", "tests/corpus/test_search_live.py",
                "tests/evidence/test_production_evidence.py", "tests/datasets/test_native.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_ok(): pass\n", encoding="utf-8")
    selected = {p.relative_to(tmp_path).as_posix() for p in CHECKS.test_files(tmp_path, "windows-offline")}
    assert len(selected) == 6 and "tests/corpus/test_search_live.py" not in selected


def test_actual_new_fast_suites_are_mandatory_in_linux_and_windows_selection():
    for suite in ("corpus-offline", "windows-offline"):
        selected = {p.relative_to(ROOT).as_posix() for p in CHECKS.test_files(ROOT, suite)}
        assert {"tests/evidence/test_production_evidence.py", "tests/datasets/test_native.py",
                "tests/evidence/test_shadow_acceptance.py", "tests/evidence/test_deployment_lifecycle.py",
                "tests/evidence/test_semantic_extraction.py", "tests/evidence/test_scientific_context_history.py",
                "tests/evidence/test_phase1_migration.py", "tests/corpus/test_remote_graph.py",
                "tests/corpus/test_pack_policy.py", "tests/corpus/test_native_serving.py",
                "tests/corpus/test_mcp_contract.py", "tests/corpus/test_mcp_rejected_body.py",
                "tests/corpus/test_remote_services.py", "tests/corpus/test_remote_search.py",
                "tests/corpus/test_remote_retrieval_identity.py", "tests/corpus/test_nav_incremental.py",
                "tests/corpus/test_nav_sections.py", "tests/corpus/test_nav_store.py",
                "tests/corpus/test_accounting_publication.py", "tests/corpus/test_runtime_publication.py",
                "tests/corpus/test_publish_transfer.py", "tests/corpus/test_service_identity.py",
                "tests/corpus/test_service_wheel.py", "tests/corpus/test_structural_fidelity.py",
                "tests/corpus/test_text_owner_bridge.py", "tests/corpus/test_visual_owner_bridge.py",
                "tests/corpus/test_native_tokenizer_binding.py", "tests/corpus/test_table_continuations.py",
                "tests/corpus/test_locator_schema_compat.py", "tests/corpus/test_visual_native_header.py"} <= selected
    world = set(CHECKS.test_files(ROOT, "world-integrity"))
    assert world == set((ROOT / "tests/world").rglob("test_*.py"))


def test_rsync_capability_does_not_exempt_other_publication_or_symlink_failures():
    reason = "rsync not installed: NOT_RUN"
    for module, name in (
            ("test_publish_transfer", "test_publish_then_reconcile_moves_current"),
            ("test_accounting_publication", "test_actual_local_rsync_transfers_only_pinned_closure")):
        path = f"tests/corpus/{module}.py"
        assert CHECKS.allowed_skip(f"{path}::{name}", reason)
        assert not CHECKS.allowed_skip(path, reason)
        assert not CHECKS.allowed_skip(f"{path}::test_other", reason)
        assert not CHECKS.allowed_skip(f"{path}::{name}", "symlink privilege unavailable")


def test_linux_capability_allowance_never_covers_neighboring_or_new_cases():
    node = "tests/corpus/test_remote_retrieval_identity.py::test_pack_watch_closes_even_when_all_stat_fields_repeat"
    assert CHECKS.allowed_skip(node, CHECKS.LINUX_PACK_REASON, host_platform="win32")
    assert not CHECKS.allowed_skip(node + "[new]", CHECKS.LINUX_PACK_REASON, host_platform="win32")
    assert not CHECKS.allowed_skip("tests/corpus/test_remote_retrieval_identity.py", CHECKS.LINUX_PACK_REASON,
                                  host_platform="win32")
    assert not CHECKS.allowed_skip("tests/corpus/test_remote_services.py::test_new_case", CHECKS.LINUX_PACK_REASON,
                                  host_platform="win32")


def test_fresh_bytecode_namespace_ignores_stale_same_size_timestamp_cache(tmp_path):
    import json
    import subprocess

    script = r'''
import importlib, importlib.util, json, os, pathlib, py_compile, subprocess, sys
root=pathlib.Path(sys.argv[2]); source=root/'cache_probe.py'
source.write_text("VALUE='old'\n", encoding='utf-8')
sys.pycache_prefix=None
stamp=source.stat()
py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
source.write_text("VALUE='new'\n", encoding='utf-8')
os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
sys.path.insert(0, str(root)); sys.dont_write_bytecode=True
assert importlib.import_module('cache_probe').VALUE=='old'
del sys.modules['cache_probe']
spec=importlib.util.spec_from_file_location('runner',sys.argv[1]); runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
prefix=runner.isolate_bytecode(root)
assert importlib.import_module('cache_probe').VALUE=='new'
child="import sys;sys.path.insert(0,sys.argv[1]);import cache_probe;assert cache_probe.VALUE=='new';assert sys.dont_write_bytecode"
subprocess.run([sys.executable,'-c',child,str(root)],check=True)
runner._run([sys.executable,'-I','-c',child,str(root)],root,root/'isolated.log')
assert not list(prefix.rglob('*.pyc'))
assert list((root/'__pycache__').glob('*.pyc')), 'original cache must remain untouched'
print(json.dumps({'status':'PASS','existing_cache_preserved':True,'fresh_namespace_empty':True}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT / "scripts/run_offline_checks.py"), str(tmp_path)],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["status"] == "PASS"


def test_vendor_gis_rehearsal_has_explicit_external_marker():
    import ast

    tree = ast.parse((ROOT / "tests/datasets/test_dataset_runtime.py").read_text(encoding="utf-8"))
    test = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == "test_native_runtime_rehearsal")
    assert any(ast.unparse(decorator) == "pytest.mark.qgis_runtime" for decorator in test.decorator_list)
    assert "not qgis_runtime" in CHECKS.MARKER_EXPRESSION


def test_frozen_object_missing_fails_even_when_verifier_is_nonblocking():
    report = {"exit_code": 0, "checks": [
        {"id": "catalogue_sync:manifest", "status": "PASS", "details": {}},
        {"id": "frozen:X@1234567", "status": "SKIPPED_REF_UNAVAILABLE", "details": {}},
    ]}
    assert CHECKS.integrity_status(report)["status"] == "FAIL"


def test_missing_tag_is_reported_without_creating_it():
    report = {"exit_code": 0, "checks": [
        {"id": "catalogue_sync:manifest", "status": "PASS", "details": {}},
        {"id": "frozen:X@1234567", "status": "PASS", "details": {}},
        {"id": "frozen:anchor:tag:frozen/x", "status": "SKIPPED_REF_UNAVAILABLE", "details": {}},
    ]}
    result = CHECKS.integrity_status(report)
    assert result["status"] == "PASS_WITH_NOT_RUN"
    assert result["not_run"] == ["frozen:anchor:tag:frozen/x"]


def test_public_manifest_must_run_and_pass():
    assert CHECKS.integrity_status({"exit_code": 0, "checks": []})["status"] == "FAIL"


@pytest.mark.parametrize("source,expected", [
    ("def test_ok(): pass\n", "PASS"),
    ("import pytest\ndef test_skipped(): pytest.skip('new unexplained skip')\n", "FAIL"),
    ("raise RuntimeError('broken collection')\n", "FAIL"),
    ("import pytest\n@pytest.mark.unregistered\ndef test_ok(): pass\n", "FAIL"),
])
def test_real_pytest_run_accounts_for_collection_and_skips(tmp_path, source, expected):
    import json
    import subprocess

    (tmp_path / "tests/world").mkdir(parents=True)
    (tmp_path / "tests/world/test_probe.py").write_text(source)
    output = tmp_path / "output"
    output.mkdir()
    script = "import importlib.util,json,pathlib,sys; s=importlib.util.spec_from_file_location('checks',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ACCOUNTING='+json.dumps(m.run_tests(pathlib.Path(sys.argv[2]),'world-integrity',pathlib.Path(sys.argv[3]))))"
    result = subprocess.run([sys.executable, "-c", script, str(ROOT / "scripts/run_offline_checks.py"),
                             str(tmp_path), str(output)], cwd=tmp_path, text=True, capture_output=True, check=True)
    report = json.loads(result.stdout.split("ACCOUNTING=", 1)[1])
    assert report["status"] == expected
    assert (output / "pytest.xml").is_file()
    if "broken collection" in source:
        assert report["collection_errors"]
    if "pytest.skip" in source:
        assert report["passed"] == 0 and report["unexpected_skips"]
