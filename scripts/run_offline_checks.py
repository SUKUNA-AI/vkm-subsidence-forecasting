#!/usr/bin/env python3
"""Run the same public CPU checks locally and in Actions, with explicit NOT_RUN accounting.

World tests form one Linux job; every other test suite, recursively, forms the other.
Windows additionally qualifies the explicitly listed synthetic filesystem and protocol suites.
Only declared external-runtime markers are deselected. Unexpected skips fail.
Build/install/import smoke uses temporary directories outside the checkout.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

EXTERNAL_MARKERS = ("services", "gpu", "desktop", "matlab", "ansys", "qgis_runtime", "native_source")
MARKER_EXPRESSION = " and ".join(f"not {marker}" for marker in EXTERNAL_MARKERS)
ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PATHS = ("tests/world", "tests/engineering", "tests/qgis", "tests/evidence", "tests/datasets",
                 "tests/corpus/test_nightly_summary.py",
                 "tests/corpus/test_nightly_dossiers.py", "tests/corpus/test_backup_manifest.py",
                 "tests/corpus/test_unique_inventory.py", "tests/corpus/test_remote_services.py",
                 "tests/corpus/test_remote_search.py", "tests/corpus/test_remote_retrieval_identity.py",
                 "tests/corpus/test_remote_graph.py", "tests/corpus/test_pack_policy.py",
                 "tests/corpus/test_native_serving.py", "tests/corpus/test_mcp_contract.py",
                 "tests/corpus/test_mcp_rejected_body.py", "tests/corpus/test_nav_incremental.py",
                 "tests/corpus/test_nav_sections.py", "tests/corpus/test_nav_store.py",
                 "tests/corpus/test_accounting_publication.py", "tests/corpus/test_runtime_publication.py",
                 "tests/corpus/test_publish_transfer.py", "tests/corpus/test_service_identity.py",
                 "tests/corpus/test_service_wheel.py", "tests/corpus/test_structural_fidelity.py",
                 "tests/corpus/test_model_owner.py", "tests/corpus/test_read_credentials.py",
                 "tests/corpus/test_text_owner_bridge.py", "tests/corpus/test_visual_owner_bridge.py",
                 "tests/corpus/test_native_tokenizer_binding.py", "tests/corpus/test_table_continuations.py",
                 "tests/corpus/test_locator_schema_compat.py", "tests/corpus/test_visual_native_header.py")
# Host prerequisites only. Missing locked CPU Python packages must fail, not become allowed NOT_RUN.
KNOWN_SKIPS = (
    ("tests/corpus/test_extract_djvu.py", "DjVuLibre not installed (NOT_RUN)"),
    ("tests/corpus/test_publish_transfer.py::test_publish_then_reconcile_moves_current", "rsync not installed: NOT_RUN"),
    ("tests/corpus/test_accounting_publication.py::test_actual_local_rsync_transfers_only_pinned_closure",
     "rsync not installed: NOT_RUN"),
    ("tests/corpus/test_figures_pdf.py", "local OCR helper (tesseract) not installed"),
    ("tests/corpus/test_nightly_scripts.py", "needs the CORE/EDGE host environment: bash, python3, flock, rsync, timeout, sha256sum"),
    ("tests/corpus/test_visual_native_header.py::test_original_native_lifecycle_header_compiles_and_fences",
     "C++17 compiler not installed (NOT_RUN)"),
)
# These primitives do not exist on Windows. Missing symlink/locking privileges
# remain failures; they must never be added to this operating-system allowance.
LINUX_RECEIVER_REASON = ("NOT_RUN: qualified production receiver requires Linux filesystem change-time semantics; "
                         "native Windows ChangeTime/reparse-handle qualification unavailable")
LINUX_RECEIVER_CASES = (
    "test_operator_receiver_starts_closed_without_durable_record_but_proves_metadata",
    "test_receiver_proof_observes_actual_bound_generation_and_native_process",
    "test_receiver_proof_rejects_mutation_and_gate_replacement",
    "test_selected_database_change_and_policy_change_close_admission",
    "test_data_change_with_unchanged_snapshot_metadata_is_unqualified[False]",
    "test_data_change_with_unchanged_snapshot_metadata_is_unqualified[True]",
    "test_same_size_rewrite_with_restored_mtime_invalidates_immutable_file",
    "test_atomic_replacement_with_same_bytes_size_and_mtime_requires_rebind",
    "test_wrong_file_hash_cannot_reuse_otherwise_qualified_acceptance",
    "test_memory_canon_is_not_a_qualified_production_file",
    "test_qualified_duckdb_must_be_an_ordinary_nonindirect_file[symlink]",
    "test_qualified_duckdb_must_be_an_ordinary_nonindirect_file[directory]",
    "test_qualified_duckdb_must_be_an_ordinary_nonindirect_file[fifo]",
    "test_full_database_hash_is_only_computed_once_at_bind",
    "test_mutation_during_full_hash_closes_startup",
    "test_replacement_at_startup_observer_boundary_never_installs_guard[during_observer]",
    "test_replacement_at_startup_observer_boundary_never_installs_guard[after_startup]",
    "test_prepared_database_is_not_served_database",
    "test_remote_service_cannot_be_omitted_from_generation[graph]",
    "test_remote_service_cannot_be_omitted_from_generation[search]",
    "test_remote_service_cannot_be_omitted_from_generation[hybrid]",
    "test_remote_service_cannot_be_omitted_from_generation[rerank]",
    "test_remote_service_cannot_be_omitted_from_generation[control]",
    "test_contexts_and_current_policy_are_required",
    "test_qualified_principal_permissions_cannot_change_under_old_acceptance",
    "test_token_rotation_preserves_roles_but_write_permission_invalidates_acceptance",
    "test_acceptance_cannot_validate_one_file_and_parse_replacement_bytes",
    "test_generic_pass_is_not_serving_qualification",
    "test_native_watch_closes_even_when_all_stat_fields_repeat",
)
LINUX_PACK_REASON = ("NOT_RUN: qualified loaded-pack receiver requires Linux change-time semantics; "
                     "native Windows qualification unavailable")
LINUX_PACK_CASES = (
    "test_loaded_pack_hashes_once_and_observes_actual_bound_bytes",
    "test_same_size_restored_mtime_byte_change_closes_pack_admission[before_bind]",
    "test_same_size_restored_mtime_byte_change_closes_pack_admission[after_bind]",
    "test_qualified_pointer_change_does_not_wait_for_legacy_reload_interval",
    "test_wrong_pinned_manifest_cannot_enable_qualified_pack",
    "test_pack_watch_closes_even_when_all_stat_fields_repeat",
)
POSIX_SHARED_GATE_REASON = "NOT_RUN: POSIX shared admission gate"
LINUX_RECEIVER_EXTRA_NODES = (
    "tests/evidence/test_challenger_policy_binding.py::test_effective_policy_rebinding_cannot_reuse_qualified_generation[path]",
    "tests/evidence/test_challenger_policy_binding.py::test_effective_policy_rebinding_cannot_reuse_qualified_generation[replace]",
    "tests/evidence/test_challenger_policy_binding.py::test_effective_policy_rebinding_cannot_reuse_qualified_generation[remove]",
    "tests/evidence/test_challenger_policy_binding.py::test_effective_policy_rebinding_cannot_reuse_qualified_generation[source_inventory]",
    "tests/evidence/test_challenger_canon_binding.py::test_native_canon_connection_cannot_borrow_another_qualified_path[backend]",
    "tests/evidence/test_challenger_canon_binding.py::test_native_canon_connection_cannot_borrow_another_qualified_path[connection]",
    "tests/evidence/test_challenger_nav_binding.py::test_packed_nav_payload_change_cannot_reuse_unchanged_origin_metadata[False]",
    "tests/evidence/test_challenger_nav_binding.py::test_packed_nav_payload_change_cannot_reuse_unchanged_origin_metadata[True]",
)
POSIX_SHARED_GATE_CASES = (
    "tests/evidence/test_durable_admission.py::test_native_rebind_cannot_publish_open_without_returned_proof",
    "tests/evidence/test_durable_admission.py::test_native_lock_watch_survives_owned_read_only_fences_and_repeated_writer",
    "tests/evidence/test_durable_admission.py::test_actual_writer_process_exit_does_not_reopen_unverified_replacement[MAINTENANCE_REMOVED]",
    "tests/evidence/test_durable_admission.py::test_actual_writer_process_exit_does_not_reopen_unverified_replacement[REBOUND:receiver]",
    "tests/evidence/test_durable_admission.py::test_actual_writer_process_exit_does_not_reopen_unverified_replacement[RECEIVERS_VERIFIED]",
    "tests/evidence/test_durable_admission.py::test_actual_writer_process_exit_does_not_reopen_unverified_replacement[ADMISSION_OPEN]",
    "tests/evidence/test_receiver_identity.py::test_mcp_client_response_holds_native_gate_after_upstream_has_finished",
    "tests/evidence/test_receiver_barrier.py::test_shared_gate_lease_covers_streamed_response_and_releases_after_cancellation",
    "tests/evidence/test_receiver_barrier.py::test_replaced_gate_inode_cannot_silently_rebind_existing_receiver[acquire]",
    "tests/evidence/test_receiver_barrier.py::test_replaced_gate_inode_cannot_silently_rebind_existing_receiver[rebind]",
    "tests/evidence/test_receiver_barrier.py::test_receiver_cannot_change_gate_while_a_request_is_active",
    "tests/evidence/test_deployment_lifecycle.py::test_late_or_replacement_receiver_stays_closed_through_failed_rebind_and_restore[receiver]",
    "tests/evidence/test_deployment_lifecycle.py::test_late_or_replacement_receiver_stays_closed_through_failed_rebind_and_restore[late-receiver]",
    "tests/evidence/test_deployment_lifecycle.py::test_unregistered_process_request_must_drain_before_any_selector_mutation",
    "tests/evidence/test_deployment_lifecycle.py::test_replaced_admission_inode_invalidates_writer_fence_before_mutation",
    "tests/evidence/test_deployment_lifecycle.py::test_rollback_rechecks_admission_before_each_selector_restore[duckdb]",
    "tests/evidence/test_deployment_lifecycle.py::test_rollback_rechecks_admission_before_each_selector_restore[document]",
)
WINDOWS_NOT_RUN = (
    ("tests/evidence/test_qualification_cli.py::test_fifo_input_is_rejected_before_open", "POSIX FIFO"),
    ("tests/evidence/test_qualification_cli.py::test_fsync_failure_never_acknowledges_pass[directory]",
     "directory durability explicitly NOT_QUALIFIED on Windows"),
) + tuple(("tests/evidence/test_production_serving.py::" + case, LINUX_RECEIVER_REASON)
          for case in LINUX_RECEIVER_CASES) + tuple(
              ("tests/corpus/test_remote_retrieval_identity.py::" + case, LINUX_PACK_REASON)
              for case in LINUX_PACK_CASES) + tuple(
                  (nodeid, POSIX_SHARED_GATE_REASON) for nodeid in POSIX_SHARED_GATE_CASES) + tuple(
                      (nodeid, LINUX_RECEIVER_REASON) for nodeid in LINUX_RECEIVER_EXTRA_NODES) + tuple(
    ("tests/evidence/test_closed_startup.py::" + case, LINUX_RECEIVER_REASON) for case in (
        "test_actual_closed_receiver_proves_metadata_but_never_serves_content",
        *("test_closed_receiver_metadata_rechecks_actual_bindings[" + case + "]" for case in
          ("policy_bytes", "canonical_bytes", "current_generation", "authority_owner")))) + tuple(
    ("tests/evidence/test_bootstrap_probes.py::" + case,
     "NOT_RUN: Linux native bootstrap flock and inotify qualification") for case in (
        "test_native_fds_are_borrowed_and_fake_transport_never_qualifies_production",
        *("test_actual_native_lease_loss_is_not_an_owned_writer[" + case + "]" for case in
          ("unlock_writer", "unlock_gate", "shared", "closed_fd", "replace_gate", "mutate_restore")))) + tuple(
    ("tests/corpus/test_model_owner.py::" + case,
     "NOT_RUN: actual owned child procfs/inotify qualification requires Linux") for case in (
        "test_actual_owned_child_socket_maps_and_client_qualify_without_inference",
        "test_mmproj_read_or_digest_without_mapping_is_never_loaded_proof",
        "test_foreign_ready_listener_and_saved_hash_cannot_qualify_child",
        "test_owned_file_hash_mismatch_prevents_any_spawn", "test_owned_hardlink_alias_is_unqualified",
        *("test_actual_owned_child_fences_each_replacement_and_load_time_event[" + case + "]" for case in
          ("client_object", "client_url", "client_closed", "weights", "mmproj", "recipe", "restore_bytes",
           "unmap-mmproj", "close-listener", "exit")))) + tuple(
    ("tests/corpus/test_visual_owner_bridge.py::" + case,
     "NOT_RUN: actual owned child procfs/inotify qualification requires Linux") for case in (
        "test_owned_live_witness_without_mmap_and_sanitized_identity",
        *("test_invalid_first_witness_never_qualifies[" + case + "]" for case in
          ("epoch", "nonce", "zero", "path", "capture", "duplicate", "oversized", "sleep")),
        *("test_changed_lifetime_or_owned_boundary_closes_permanently[" + case + "]" for case in
          ("epoch", "handle", "sleep", "bytes", "auth", "transport")),
        "test_foreign_listener_never_receives_private_challenge", "test_hook_file_watch_precedes_child_spawn",
        "test_response_buffer_discarded_when_native_epoch_changes_during_inference",
        "test_same_owned_client_proxy_routes_and_no_synthetic_native_identity",
        "test_factory_owns_child_and_recipe_token_watch_before_inference",
        "test_factory_refuses_omitted_actual_bridge_code_before_child"))


def test_files(root: Path, suite: str) -> list[Path]:
    config_file = root / "pyproject.toml"
    config = tomllib.loads(config_file.read_text(encoding="utf-8")) if config_file.is_file() else {}
    patterns = config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("python_files", ["test_*.py", "*_test.py"])
    if isinstance(patterns, str):
        patterns = patterns.split()
    files = sorted(path for path in (root / "tests").rglob("*.py")
                   if any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns))
    if suite == "windows-offline":
        return [path for path in files if any(path == root / rel or path.is_relative_to(root / rel)
                                              for rel in WINDOWS_PATHS)]
    return [path for path in files if (path.relative_to(root / "tests").parts[0] == "world")
            == (suite == "world-integrity")]


def allowed_skip(nodeid: str, reason: str, *, host_platform: str | None = None) -> bool:
    module = nodeid.split("::", 1)[0]
    reason = reason.removeprefix("Skipped: ")
    platform = sys.platform if host_platform is None else host_platform
    return ((nodeid, reason) in KNOWN_SKIPS or (module, reason) in KNOWN_SKIPS
            or (platform == "win32" and (nodeid, reason) in WINDOWS_NOT_RUN))


def test_status(stats: dict, exit_code: int) -> str:
    if (exit_code or not stats.get("selected") or not stats.get("passed") or stats.get("failed")
            or stats.get("collection_errors") or stats.get("unexpected_skips")
            or stats.get("xfailed") or stats.get("xpassed")):
        return "FAIL"
    return "PASS_WITH_NOT_RUN" if stats.get("not_run") or stats.get("skipped") or stats.get("deselected") else "PASS"


class Accounting:
    def __init__(self):
        self.stats = {"selected": 0, "passed": 0, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0,
                      "collection_errors": [], "not_run": [], "unexpected_skips": [], "deselected": []}

    def pytest_collection_finish(self, session):
        self.stats["selected"] = len(session.items)

    def pytest_deselected(self, items):
        self.stats["deselected"].extend({"nodeid": item.nodeid, "status": "NOT_RUN",
                                       "markers": [m.name for m in item.iter_markers() if m.name in EXTERNAL_MARKERS]}
                                      for item in items)

    def _skip(self, report, phase):
        reason = str(report.longrepr[2]) if isinstance(report.longrepr, tuple) else str(report.longrepr)
        record = {"nodeid": report.nodeid, "phase": phase, "status": "NOT_RUN",
                  "reason": reason.removeprefix("Skipped: ")}
        self.stats["not_run"].append(record)
        if not allowed_skip(report.nodeid, reason):
            self.stats["unexpected_skips"].append(record)

    def pytest_collectreport(self, report):
        if report.failed:
            self.stats["collection_errors"].append({"nodeid": report.nodeid, "detail": str(report.longrepr)})
        elif report.skipped:
            self._skip(report, "collection")

    def pytest_runtest_logreport(self, report):
        if hasattr(report, "wasxfail"):
            self.stats["xfailed" if report.skipped else "xpassed"] += 1
        elif report.failed:
            self.stats["failed"] += 1
        elif report.skipped:
            self.stats["skipped"] += 1
            self._skip(report, report.when)
        elif report.when == "call" and report.passed:
            self.stats["passed"] += 1


def isolate_bytecode(output: Path) -> Path:
    """Ignore existing timestamp/size .pyc files without removing any cache.

    DONTWRITEBYTECODE alone still reads a stale cache. Each run gets a fresh
    external namespace; inherited Python workers use the same empty namespace.
    """
    prefix = Path(tempfile.mkdtemp(prefix="bytecode-unwritten-", dir=output))
    sys.pycache_prefix = str(prefix)
    sys.dont_write_bytecode = True
    os.environ["PYTHONPYCACHEPREFIX"] = str(prefix)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    return prefix


def run_tests(root: Path, suite: str, output: Path) -> dict:
    isolate_bytecode(output)
    import pytest

    # Match `python -m pytest`: public benchmark helper modules live at the repository root.
    sys.path.insert(0, str(root))
    accounting = Accounting()
    files = test_files(root, suite)
    if not files:
        return {"status": "FAIL", "exit_code": 5, **accounting.stats}
    # Let pytest recurse with its own discovery configuration, including newly added suites.
    if suite == "windows-offline":
        paths = [root / rel for rel in WINDOWS_PATHS]
        if any(not path.exists() for path in paths):
            return {"status": "FAIL", "exit_code": 5, "missing_required_paths": [
                path.relative_to(root).as_posix() for path in paths if not path.exists()], **accounting.stats}
    else:
        paths = [root / "tests/world"] if suite == "world-integrity" else sorted(
            path for path in (root / "tests").iterdir() if path.name != "world"
            and (path.is_dir() or path in files))
    code = pytest.main(["-q", "-p", "no:cacheprovider", "--strict-markers", "--strict-config", "-m", MARKER_EXPRESSION,
                        "--junitxml", str(output / "pytest.xml"),
                        "--basetemp", str(output / "pytest-tmp"), *map(str, paths)], plugins=[accounting])
    return {"status": test_status(accounting.stats, int(code)), "exit_code": int(code),
            "test_files": [p.relative_to(root).as_posix() for p in files], **accounting.stats}


def integrity_status(report: dict) -> dict:
    gaps, errors = [], []
    manifest = next((check for check in report["checks"] if check["id"] == "catalogue_sync:manifest"), None)
    if manifest is None or manifest["status"] != "PASS":
        errors.append("catalogue_sync:manifest")
    for check in report["checks"]:
        if check["id"].startswith("frozen:anchor:tag:") and check["status"] == "SKIPPED_REF_UNAVAILABLE":
            gaps.append(check["id"])
        elif check["id"].startswith(("frozen:", "retired:")) and check["status"] != "PASS":
            errors.append(check["id"])
        elif check["status"] in ("SKIPPED", "SKIPPED_REF_UNAVAILABLE"):
            gaps.append(check["id"])
    status = "FAIL" if report["exit_code"] or errors else "PASS_WITH_NOT_RUN" if gaps else "PASS"
    return {"status": status, "object_failures": errors, "not_run": gaps}


def run_integrity(root: Path, output: Path) -> dict:
    spec = importlib.util.spec_from_file_location("verify_canonical_repository", root / "scripts/verify_canonical_repository.py")
    verifier = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = verifier
    spec.loader.exec_module(verifier)
    # These source comparisons require PRIVATE, which this runner never opens.
    groups = [group for group in verifier.GROUPS if group != "private_sources"]
    report = verifier.verify(root, groups=groups, use_env=False)
    (output / "canonical-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    return integrity_status(report)


def _run(argv: list[str], cwd: Path, log: Path) -> None:
    if argv[0] == sys.executable and sys.pycache_prefix:
        # -I ignores Python environment variables, so the installed-wheel smoke
        # must carry these flags explicitly as well.
        argv = [argv[0], "-B", "-X", "pycache_prefix=" + sys.pycache_prefix, *argv[1:]]
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(argv) + "\n")
        stream.flush()
        subprocess.run(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, check=True)


def package_smoke(root: Path, output: Path) -> dict:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    settings = config["tool"]["setuptools"]
    resources, empty = {}, []
    for package, patterns in settings.get("package-data", {}).items():
        base = root / "src" / package.replace(".", "/")
        for pattern in patterns:
            matches = sorted(path for path in base.glob(pattern) if path.is_file())
            if not matches:
                empty.append({"package": package, "pattern": pattern, "status": "NO_SOURCE_FILES"})
            for path in matches:
                resources[path.relative_to(root / "src").as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    includes = settings["packages"]["find"]["include"]
    packages = sorted(path.name for path in (root / "src").iterdir()
                      if (path / "__init__.py").is_file() and any(fnmatch.fnmatch(path.name, p) for p in includes))
    log = output / "package-smoke.log"
    with tempfile.TemporaryDirectory(prefix="vkm-offline-package-") as scratch:
        temp = Path(scratch)
        source = temp / "source"
        source.mkdir()
        shutil.copy2(root / "pyproject.toml", source / "pyproject.toml")
        shutil.copytree(root / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        wheel_dir = temp / "wheel"
        wheel_dir.mkdir()
        _run([sys.executable, "-c", "import setuptools.build_meta as b; b.build_wheel(" + repr(str(wheel_dir)) + ")"], source, log)
        wheel, = wheel_dir.glob("*.whl")
        installed = temp / "installed"
        if importlib.util.find_spec("pip") is not None:
            install = [sys.executable, "-m", "pip", "install"]
        elif uv := shutil.which("uv"):
            # Local uv-created environments may have no pip/ensurepip. No package is downloaded.
            install = [uv, "pip", "install", "--python", sys.executable]
        else:
            raise RuntimeError("wheel install needs existing pip or uv; neither is available")
        _run([*install, "--no-deps", "--no-index", "--target", str(installed), str(wheel)], temp, log)
        script = """import hashlib, importlib, json, pathlib, sys
target=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(target))
packages=json.loads(sys.argv[2]); resources=json.loads(sys.argv[3])
for name in packages:
    module=importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(target), name
for relative,wanted in resources.items():
    path=target/relative
    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==wanted, relative
print(json.dumps({'imported':packages,'resources_verified':len(resources)}))
"""
        _run([sys.executable, "-I", "-c", script, str(installed), json.dumps(packages), json.dumps(resources)], temp, log)
    return {"status": "PASS", "installation": "non-editable wheel outside checkout", "imported": packages,
            "resources": resources, "empty_declarations": empty}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("suite", choices=("world-integrity", "corpus-offline", "windows-offline"))
    parser.add_argument("--output", type=Path, required=True, help="artifact directory outside checkout")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error("--output must be outside the checkout")
    output.mkdir(parents=True, exist_ok=True)
    # Live configuration is intentionally absent. Synthetic tests set their own configuration.
    for key in list(os.environ):
        if key.startswith("VKM_"):
            del os.environ[key]
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    sys.dont_write_bytecode = True
    os.chdir(ROOT)
    report = {"suite": args.suite, "marker_expression": MARKER_EXPRESSION,
              "scope": "PUBLIC synthetic CPU; no PRIVATE, live services, solver licences, or GPU",
              "python": sys.version.split()[0]}
    tasks = [("tests", lambda: run_tests(ROOT, args.suite, output))]
    if args.suite == "world-integrity":
        tasks += [("integrity", lambda: run_integrity(ROOT, output)),
                  ("package", lambda: package_smoke(ROOT, output))]
    for name, task in tasks:
        try:
            report[name] = task()
        except Exception as exc:
            report[name] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    statuses = [report[name]["status"] for name, _ in tasks]
    report["status"] = "FAIL" if "FAIL" in statuses else "PASS_WITH_NOT_RUN" if "PASS_WITH_NOT_RUN" in statuses else "PASS"
    report["bytecode_cache"] = {"namespace": Path(sys.pycache_prefix).name,
                               "existing_source_caches": "IGNORED", "writes": "DISABLED"}
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "suite": args.suite, "summary": str(output / "summary.json")}))
    return int(report["status"] == "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
