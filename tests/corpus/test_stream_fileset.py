"""Synthetic protocol/guards only: never originals, age keys, SSH or GPU."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[2] / "infra/recovery/stream_fileset.py"
_SPEC = importlib.util.spec_from_file_location("stream_recovery_test", _PATH)
S = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = S
_SPEC.loader.exec_module(S)


def manifest(items):
    return {"schema": S.R.SCHEMA, "roots": [{"root_id": "source", "location_env": "VKM_SYNTHETIC_SOURCE_ROOT",
        "failure_domain": "SYNTHETIC", "role": "ORIGINAL"}], "items": items}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"; root.mkdir()
    data = {"document-a": b"SYNTHETIC original data\n" * 17000, "document-empty": b"", "document-b": b"SYNTHETIC other bytes"}
    items = []
    for index, (logical, raw) in enumerate(data.items()):
        path = "file-%d.bytes" % index; (root / path).write_bytes(raw)
        items.append({"logical_id": logical, "classification": "CANONICAL", "source": {"root_id": "source", "path": path},
            "mutable": False, "copies": [], "expected_sha256": hashlib.sha256(raw).hexdigest()})
    declared = manifest(items)
    header = S.plan_batches(declared, {key: len(raw) for key, raw in data.items()},
        manifest_sha256=hashlib.sha256(S.canonical(declared)).hexdigest())[0]
    return root, declared, header, data


def pin(source):
    root, declared, _, data = source
    return S.F.PinnedFileSet(declared, byte_budget=6 * (sum(map(len, data.values())) + len(data)) + 10000,
        max_seconds=300, environ={"VKM_SYNTHETIC_SOURCE_ROOT": str(root)})


def bounds(*, maximum=8 * S.MAX_CIPHER):
    return S.Bounds(max_seconds=300, max_bytes=maximum)


def payload(source):
    sink = io.BytesIO()
    with pin(source) as pinned:
        result = S.write_payload(pinned, sink, source[2], bounds())
        assert result["status"] == "STREAM_WRITTEN_NOT_BACKUP_QUALIFIED"
        pinned.recheck()
    return sink.getvalue()


def test_real_retained_handles_stream_and_restore_all_bytes_with_empty_file(source, tmp_path, monkeypatch):
    root, declared, header, data = source
    reads = []
    original_read = S.R.ByteBudget.read
    def read(budget, file, count):
        reads.append(count); return original_read(budget, file, count)
    with pin(source) as pinned:
        monkeypatch.setattr(S.R.ByteBudget, "read", read)
        monkeypatch.setattr(pinned, "read_pinned", lambda _: pytest.fail("whole-file read is forbidden"))
        sink = io.BytesIO(); S.write_payload(pinned, sink, header, bounds())
        # Stream calls stay <=64KiB. Frozen initial/final hash passes use their
        # existing 1MiB bounded implementation; do not confuse the two scopes.
        assert max(reads) <= S.CHUNK
        result = S.restore_payload(io.BytesIO(sink.getvalue()), tmp_path / "restored", header, bounds())
        assert result["status"] == "PROTOCOL_BYTES_VERIFIED_AUTHENTICATION_PENDING"
        for row in result["files"]:
            assert (tmp_path / "restored" / ("item-" + row["logical_id"] + ".bytes")).read_bytes() == data[row["logical_id"]]
        assert not (tmp_path / "restored" / "COMMITTED").exists()
        pinned.recheck()


class ShortSink(io.BytesIO):
    def write(self, block): return super().write(block[:3])


def test_short_writes_are_completed_without_data_loss(source):
    reference = payload(source)
    with pin(source) as pinned:
        sink = ShortSink(); S.write_payload(pinned, sink, source[2], bounds())
        assert sink.getvalue() == reference


@pytest.mark.parametrize("count", [0, None, -1, 100])
def test_invalid_write_result_fails_closed(count):
    with pytest.raises(S.StreamError, match="SHORT_WRITE"):
        S._write_all(SimpleNamespace(write=lambda _: count), b"abc", bounds())


@pytest.mark.parametrize("cut", [0, 5, 10, 20, -1, -9, -30])
def test_truncated_protocol_never_becomes_committed(source, tmp_path, cut):
    raw = payload(source)
    with pytest.raises(S.StreamError):
        S.restore_payload(io.BytesIO(raw[:cut]), tmp_path / "restored", source[2], bounds())
    assert not (tmp_path / "restored" / "COMMITTED").exists()


def test_extra_bytes_and_corrupted_body_are_rejected(source, tmp_path):
    raw = payload(source)
    with pytest.raises(S.StreamError, match="EXTRA_STREAM"):
        S.restore_payload(io.BytesIO(raw + b"x"), tmp_path / "extra", source[2], bounds())
    start = len(S.MAGIC) + 4 + len(S.canonical(source[2]))
    raw = raw[:start] + bytes([raw[start] ^ 1]) + raw[start + 1:]
    with pytest.raises(S.StreamError, match="RESTORED_DIGEST"):
        S.restore_payload(io.BytesIO(raw), tmp_path / "corrupt", source[2], bounds())


def test_stream_rehash_rejects_wrong_read_even_with_unchanged_file_metadata(source, monkeypatch):
    with pin(source) as pinned:
        original_read = pinned.budget.read
        def corrupt(file, count):
            raw = original_read(file, count)
            return bytes([raw[0] ^ 1]) + raw[1:] if raw and count > 1 else raw
        monkeypatch.setattr(pinned.budget, "read", corrupt)
        with pytest.raises(S.StreamError, match="PINNED_SOURCE_CHANGED"):
            S.write_payload(pinned, io.BytesIO(), source[2], bounds())


@pytest.mark.parametrize("logical", ["../escape", "a/b", "a\\b", "a:stream", ".", "", "CON/target"])
def test_payload_paths_are_never_interpreted_as_output_locators(source, logical):
    header = json.loads(S.canonical(source[2])); header["files"][0]["logical_id"] = logical
    with pytest.raises(S.StreamError, match="INVALID_FILE"):
        S.validate_header(header)


def test_case_aliases_and_duplicate_json_rejected(source):
    header = json.loads(S.canonical(source[2])); header["files"][1]["logical_id"] = header["files"][0]["logical_id"].upper()
    with pytest.raises(S.StreamError, match="DUPLICATE_LOGICAL"):
        S.validate_header(header)
    for raw in [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"files":[{"x":1,"x":2}]}']:
        with pytest.raises(S.StreamError): S.unique_json(raw)


def test_changed_approved_header_rejected_before_output_creation(source, tmp_path):
    header = json.loads(S.canonical(source[2])); header["files"][0]["sha256"] = "a" * 64
    with pytest.raises(S.StreamError, match="APPROVED_HEADER"):
        S.restore_payload(io.BytesIO(payload(source)), tmp_path / "restored", header, bounds())
    assert not (tmp_path / "restored").exists()


def test_exclusive_restore_never_overwrites_previous_attempt(source, tmp_path):
    destination = tmp_path / "restored"
    S.restore_payload(io.BytesIO(payload(source)), destination, source[2], bounds())
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    with pytest.raises(FileExistsError): S.restore_payload(io.BytesIO(payload(source)), destination, source[2], bounds())
    assert before == {p.name: p.read_bytes() for p in destination.iterdir()}


def test_explicit_deadline_and_byte_budget_apply_to_stream_io(source):
    now = [0.0]
    deadline = S.Bounds(max_seconds=1, max_bytes=1024, clock=lambda: now[0]); now[0] = 2.0
    with pytest.raises(S.StreamError, match="DEADLINE"): S._write_all(io.BytesIO(), b"x", deadline)
    with pin(source) as pinned, pytest.raises(S.StreamError, match="BYTE_BUDGET"):
        S.write_payload(pinned, io.BytesIO(), source[2], S.Bounds(max_seconds=300, max_bytes=10))


def test_header_size_is_bounded_before_allocation(source, tmp_path):
    raw = S.MAGIC + struct.pack(">I", S.MAX_HEADER + 1)
    with pytest.raises(S.StreamError, match="HEADER_LIMIT"):
        S.restore_payload(io.BytesIO(raw), tmp_path / "restored", source[2], bounds())


def test_disk_reserve_is_checked_without_any_tool_or_payload_read(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(S.StreamError, match="DISK_RESERVE"):
        S.Bounds(max_seconds=1, max_bytes=1, min_free_bytes=1).check(disk=tmp_path)


def test_pinned_tool_requires_actual_exact_software_bytes(tmp_path):
    tool = tmp_path / "synthetic-software-never-executed"; tool.write_bytes(b"SYNTHETIC SOFTWARE")
    digest = hashlib.sha256(tool.read_bytes()).hexdigest()
    with S.pinned_tool(tool, digest) as (_, fence): fence()
    with pytest.raises(S.StreamError, match="PINNED_TOOL_CHANGED"):
        with S.pinned_tool(tool, "f" * 64): pytest.fail("incorrect binary must never be usable")


def test_watchdog_kills_actual_bounded_synthetic_blocked_command():
    process = subprocess.Popen([sys.executable, "-B", "-c", "import time;time.sleep(10)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with pytest.raises(S.StreamError, match="DEADLINE"):
        with S._ProcessGuard(process, S.Bounds(max_seconds=1, max_bytes=1)) as guard: guard.finish()
    assert process.poll() is not None


def test_watchdog_stops_actual_synthetic_disk_output_overflow(tmp_path):
    with (tmp_path / "synthetic-output").open("xb", buffering=0) as output:
        process = subprocess.Popen([sys.executable, "-B", "-c",
            "import sys,time;sys.stdout.buffer.write(b'x'*4096);sys.stdout.buffer.flush();time.sleep(10)"],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL)
        with pytest.raises(S.StreamError, match="PROCESS_OUTPUT_LIMIT"):
            with S._ProcessGuard(process, S.Bounds(max_seconds=2, max_bytes=1), output=output, maximum=1024) as guard:
                guard.finish()
        assert process.poll() is not None


def test_metadata_only_batching_has_exact_size_and_handle_boundaries():
    items = [{"logical_id": "source-%03d" % i, "classification": "CANONICAL", "source": {"root_id": "source", "path": "file-%d" % i},
        "mutable": False, "copies": [], "expected_sha256": "a" * 64} for i in range(65)]
    declared = manifest(items); sizes = {i["logical_id"]: 1 for i in items}
    assert [len(b["files"]) for b in S.plan_batches(declared, sizes, manifest_sha256="b" * 64)] == [64, 1]
    sizes[items[0]["logical_id"]] = S.MAX_PLAIN
    assert [len(b["files"]) for b in S.plan_batches(declared, sizes, manifest_sha256="b" * 64)] == [1, 64]
    sizes[items[0]["logical_id"]] += 1
    with pytest.raises(S.StreamError, match="SINGLE_FILE_LIMIT"): S.plan_batches(declared, sizes, manifest_sha256="b" * 64)


@pytest.mark.parametrize("field,value", [("mutable", True), ("expected_sha256", ""), ("copies", [{"root_id": "source", "path": "copy"}])])
def test_unpinned_or_mutable_versions_do_not_enter_recovery_batches(source, field, value):
    declared = json.loads(S.canonical(source[1])); declared["items"][0][field] = value
    with pytest.raises(ValueError):
        S.plan_batches(declared, {k: len(v) for k, v in source[3].items()}, manifest_sha256="a" * 64)


def request():
    return {"action": "copy", "target_root": "/srv/vkm-edge/recovery-unique", "namespace": "synthetic-immutable-batch-01",
        "cipher": {"size": 12, "sha256": "a" * 64}, "intent_sha256": "b" * 64,
        "stream_header": {"schema": "vkm-stream-immutable-fileset/1", "manifest_sha256": "b" * 64,
            "batch_id": "synthetic-01", "files": [{"logical_id": "synthetic-original", "size": 1, "sha256": "a" * 64}]},
        "python_version": tuple(sys.version_info[:3]), "min_free_bytes": 0,
        "expected_uid": 1000, "expected_gid": 1000, "expected_device": 65025,
        "expected_root_inode": 18874477, "expected_parent_inode": 18874441,
        "timeout_seconds": 180, "memory_limit_bytes": 256 * 1024**2,
        "python_executable": {"path": "/usr/bin/python3.13", "sha256": "a" * 64,
            "size": 6812336, "device": 65025, "inode": 3673610, "uid": 0, "mode": 0o755}}


def test_remote_program_is_fixed_and_metadata_is_bound():
    body, digest = S.remote_program(**request())
    assert "O_NOFOLLOW" in body and "O_EXCL" in body and "os.fsync" in body
    assert body.index("'manifest.json',raw") < body.index("'COMMITTED',marker")
    assert "expected['size']" in body and "sys.version_info[:3]" in body and len(digest) == 64
    assert "stream_header" in body
    altered = request(); altered["intent_sha256"] = "c" * 64
    assert S.remote_program(**altered)[1] != digest
    for field in ["expected_uid", "expected_gid", "expected_device", "expected_root_inode", "expected_parent_inode", "timeout_seconds"]:
        altered = request(); altered[field] += 1 if field != "timeout_seconds" else -1
        assert S.remote_program(**altered)[1] != digest
    altered = request(); altered["stream_header"]["files"][0]["sha256"] = "f" * 64
    assert S.remote_program(**altered)[1] != digest


@pytest.mark.parametrize("field,value", [("namespace", "../../escape"), ("namespace", "unsafe;sh"),
    ("target_root", "/srv/vkm-edge/recovery-unique-other"), ("target_root", "/tmp"),
    ("python_version", (3, 13)), ("intent_sha256", "x"), ("expected_uid", True),
    ("expected_gid", -1), ("expected_device", 0), ("expected_root_inode", None),
    ("expected_parent_inode", 0), ("timeout_seconds", 0), ("memory_limit_bytes", 1024)])
def test_remote_roots_namespace_and_version_are_closed(field, value):
    altered = request(); altered[field] = value
    with pytest.raises(S.StreamError): S.remote_program(**altered)


def test_transport_does_not_accept_arbitrary_program_or_action_before_tool_launch(monkeypatch):
    monkeypatch.setattr(S.subprocess, "Popen", lambda *a, **k: pytest.fail("must reject before any command"))
    altered = request() | {"program": S.REMOTE_SOURCE + "\nrun();malicious()"}
    with pytest.raises(S.StreamError):
        S.transfer_cipher(None, ssh_executable="not-used", ssh_sha256="a" * 64, request=altered, bounds=bounds())
    with pytest.raises(S.StreamError):
        S.transfer_cipher(None, ssh_executable="not-used", ssh_sha256="a" * 64, request=request() | {"action": "restore"}, bounds=bounds())


def test_decrypt_exit_failure_never_reports_authenticated_restore(source, tmp_path, monkeypatch):
    cipher = tmp_path / "synthetic-cipher"; cipher.write_bytes(b"SYNTHETIC_NOT_AGE")
    identity = tmp_path / "synthetic-identity"; identity.write_bytes(b"SYNTHETIC_NOT_A_KEY")
    @contextmanager
    def tool(*args): yield "synthetic-tool-never-launched", lambda: None
    monkeypatch.setattr(S, "pinned_tool", tool)
    class FailedAuthentication:
        stdout = io.BytesIO(payload(source)); returncode = 1
        def poll(self): return self.returncode
        def wait(self, timeout): return self.returncode
        def kill(self): self.returncode = -1
    monkeypatch.setattr(S.subprocess, "Popen", lambda *a, **k: FailedAuthentication())
    with pytest.raises(S.StreamError, match="EXTERNAL_COMMAND_FAILED"):
        S.decrypt_restore(cipher, tmp_path / "restore", source[2], age_executable="not-used", age_sha256="a" * 64,
            identity_path=identity, bounds=bounds())
    assert not (tmp_path / "restore" / "COMMITTED").exists()


POSIX = pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: FD-relative Linux durability/alias tests")


def remote_expected(root, data):
    info = root.stat(); parent = root.parent.stat()
    return {"schema": "vkm-stream-cipher-backup/1", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "intent_sha256": "b" * 64, "namespace": "synthetic-01", "min_free_bytes": 0,
        "root_uid": info.st_uid, "root_gid": info.st_gid, "root_device": info.st_dev,
        "root_inode": info.st_ino, "parent_inode": parent.st_ino,
        "timeout_seconds": 10, "memory_limit_bytes": 256 * 1024**2,
        "python_executable": local_python(),
        "scope": "IMMUTABLE_EXPECTED_VERSIONS_NOT_ATOMIC_CURRENT_GENERATION"}


def local_python():
    path = Path("/usr/bin/python3").resolve(); info = path.stat()
    assert info.st_size <= 64 * 1024**2
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": info.st_size,
        "device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "mode": info.st_mode & 0o7777}


def remote_body(action, root, expected):
    version = tuple(json.loads(subprocess.check_output(["/usr/bin/python3", "-I", "-c",
        "import json,sys;print(json.dumps(list(sys.version_info[:3])))"], timeout=5)))
    return S.REMOTE_SOURCE + "\nrun(" + ",".join(repr(v) for v in (action, str(root), "synthetic-01", expected, version)) + ")\n"


def remote_root(tmp_path):
    tmp_path.chmod(0o755)
    root = tmp_path / "owned"; root.mkdir(mode=0o700); root.chmod(0o700)
    return root


@POSIX
def test_actual_remote_fd_relative_cipher_copy_and_separate_restore_are_exact(tmp_path):
    # Execute only trusted static helper on synthetic local files, never SSH.
    root = remote_root(tmp_path)
    data = b"SYNTHETIC CIPHERTEXT NOT AGE" * 5000
    expected = remote_expected(root, data)
    body = remote_body("copy", root, expected)
    copied = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", body], input=data, capture_output=True, timeout=10)
    assert copied.returncode == 0 and copied.stdout == hashlib.sha256(S.canonical(expected)).hexdigest().encode() + b"\n"
    assert (root / "synthetic-01/cipher.age").read_bytes() == data
    repeated = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", body], input=data, capture_output=True, timeout=10)
    assert repeated.returncode != 0 and (root / "synthetic-01/cipher.age").read_bytes() == data
    body = body.replace("\nrun('copy',", "\nrun('restore',")
    restored = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", body], capture_output=True, timeout=10)
    assert restored.returncode == 0 and restored.stdout == data
    # A replaced leaf cannot be accepted via its symlink even with correct bytes.
    (root / "synthetic-01").chmod(0o700)
    leaf = root / "synthetic-01/cipher.age"; leaf.rename(root / "original-cipher"); leaf.symlink_to(root / "original-cipher")
    (root / "synthetic-01").chmod(0o500)
    failed = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", body], capture_output=True, timeout=10)
    assert failed.returncode != 0


@POSIX
@pytest.mark.parametrize("change", ["truncated", "extra", "digest"])
def test_actual_remote_failed_copy_never_writes_committed(tmp_path, change):
    root = remote_root(tmp_path); data = b"SYNTHETIC-CIPHER"
    expected = remote_expected(root, data)
    supplied = data[:-1] if change == "truncated" else data + b"x" if change == "extra" else b"X" + data[1:]
    body = remote_body("copy", root, expected)
    failed = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", body], input=supplied, capture_output=True, timeout=10)
    assert failed.returncode != 0 and not (root / "synthetic-01/COMMITTED").exists()


@POSIX
@pytest.mark.parametrize("field", ["root_uid", "root_gid", "root_device", "root_inode", "parent_inode"])
def test_remote_wrong_owned_identity_rejected_before_namespace_creation(tmp_path, field):
    root = remote_root(tmp_path); data = b"SYNTHETIC CIPHER"
    expected = remote_expected(root, data); expected[field] += 1
    failed = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", remote_body("copy", root, expected)],
        input=data, capture_output=True, timeout=10)
    assert failed.returncode != 0 and not (root / "synthetic-01").exists()


@POSIX
@pytest.mark.parametrize("field", ["sha256", "path", "inode", "uid"])
def test_remote_wrong_actual_python_binary_rejected_before_namespace_creation(tmp_path, field):
    root = remote_root(tmp_path); data = b"SYNTHETIC CIPHER"; expected = remote_expected(root, data)
    if field == "sha256": expected["python_executable"][field] = "f" * 64
    elif field == "path": expected["python_executable"][field] = "/usr/bin/python3.99"
    else: expected["python_executable"][field] += 1
    failed = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", remote_body("copy", root, expected)],
        input=data, capture_output=True, timeout=10)
    assert failed.returncode != 0 and not (root / "synthetic-01").exists()


@pytest.mark.parametrize("field,value", [("path", "/tmp/python3.13"), ("sha256", "x"), ("size", 70 * 1024**2),
    ("uid", 1000), ("mode", 0o777), ("inode", True)])
def test_remote_python_binary_contract_rejects_unpinned_or_writable_fields(field, value):
    altered = request(); altered["python_executable"][field] = value
    with pytest.raises(S.StreamError, match="EXACT_REMOTE_PYTHON_BINARY_REQUIRED"):
        S.remote_program(**altered)


@POSIX
@pytest.mark.parametrize("node", ["root", "parent", "namespace", "cipher"])
def test_remote_private_permissions_fail_closed_on_copy_and_restore(tmp_path, node):
    root = remote_root(tmp_path); data = b"SYNTHETIC CIPHER"; expected = remote_expected(root, data)
    if node in {"root", "parent"}:
        (root if node == "root" else root.parent).chmod(0o777)
        failed = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", remote_body("copy", root, expected)],
            input=data, capture_output=True, timeout=10)
        assert failed.returncode != 0 and not (root / "synthetic-01").exists()
    else:
        copied = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", remote_body("copy", root, expected)],
            input=data, capture_output=True, timeout=10)
        assert copied.returncode == 0
        path = root / "synthetic-01" if node == "namespace" else root / "synthetic-01/cipher.age"
        path.chmod(0o755 if node == "namespace" else 0o644)
        failed = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", remote_body("restore", root, expected)],
            capture_output=True, timeout=10)
        assert failed.returncode != 0 and failed.stdout == b""


@POSIX
@pytest.mark.parametrize("operation", ["read", "write"])
def test_remote_alarm_terminates_actual_blocked_pipe_without_caller_kill(tmp_path, operation):
    import signal
    root = remote_root(tmp_path); data = b"SYNTHETIC" * 130000; expected = remote_expected(root, data)
    expected["timeout_seconds"] = 1
    if operation == "write":
        copied = subprocess.run(["/usr/bin/python3", "-I", "-B", "-c", remote_body("copy", root, expected)],
            input=data, capture_output=True, timeout=5)
        assert copied.returncode == 0
    process = subprocess.Popen(["/usr/bin/python3", "-I", "-B", "-c", remote_body("copy" if operation == "read" else "restore", root, expected)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        # Keep stdin open without bytes, or leave stdout unread. No caller kill.
        assert process.wait(timeout=5) == -signal.SIGALRM
        if operation == "read": assert not (root / "synthetic-01/COMMITTED").exists()
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=5)
        process.stdin.close(); process.stdout.close()


WINDOWS = pytest.mark.skipif(os.name != "nt", reason="NOT_RUN: suspended Windows Job memory-cap qualification")


@WINDOWS
def test_actual_windows_job_cap_is_applied_before_execute_and_bounds_allocation(tmp_path):
    output_path = tmp_path / "synthetic-job-output"
    code = "import sys\ntry: x=bytearray(96*1024**2)\nexcept MemoryError: sys.stdout.write('MEMORY_BOUNDED')\nelse: sys.stdout.write('UNBOUNDED')"
    with output_path.open("xb", buffering=0) as output:
        process = S._spawn_bounded([sys.executable, "-B", "-c", code], memory_limit_bytes=64 * 1024**2,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, close_fds=True)
        assert process.memory_limit_bytes == 64 * 1024**2
        with S._ProcessGuard(process, S.Bounds(max_seconds=5, max_bytes=100), output=output, maximum=100) as guard:
            guard.finish()
        assert process.job is None
    assert output_path.read_bytes() == b"MEMORY_BOUNDED"


@WINDOWS
def test_failed_job_assignment_never_resumes_actual_child(tmp_path, monkeypatch):
    from vkm_jobs import procs as api
    marker = tmp_path / "must-not-run"
    monkeypatch.setattr(api._k32, "AssignProcessToJobObject", lambda *a: 0)
    with pytest.raises(S.StreamError, match="PROCESS_JOB_ASSIGN_FAILED"):
        S._spawn_bounded([sys.executable, "-B", "-c", "from pathlib import Path;import sys;Path(sys.argv[1]).write_text('BAD')", str(marker)],
            memory_limit_bytes=64 * 1024**2, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
    assert not marker.exists()


def test_private_staging_is_exclusive_and_child_can_be_created(tmp_path):
    path = S.private_directory(tmp_path / "synthetic-private")
    child = path / "child"; child.mkdir(mode=0o700)
    (child / "synthetic").write_bytes(b"SYNTHETIC")
    assert (child / "synthetic").read_bytes() == b"SYNTHETIC"
    with pytest.raises((S.StreamError, FileExistsError)):
        S.private_directory(path)


@pytest.mark.parametrize("limit", [True, 1, 2 * 1024**3])
def test_invalid_memory_cap_never_launches_command(limit, monkeypatch):
    monkeypatch.setattr(S.subprocess, "Popen", lambda *a, **k: pytest.fail("must reject before launch"))
    with pytest.raises(S.StreamError, match="NATIVE_MEMORY_CAP_REQUIRED"):
        S._spawn_bounded(["unused"], memory_limit_bytes=limit)
