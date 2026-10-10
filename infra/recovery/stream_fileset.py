"""Bounded immutable-version recovery; no archive, plaintext upload or snapshot claim.

This module does not execute on import. A caller must separately approve exact
source metadata, tool hashes, recipient, namespace and budgets. Identity bytes
are only read by an explicitly invoked age process, never by this module.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import struct
import subprocess
import sys
import threading
import time

_SPEC = importlib.util.spec_from_file_location("vkm_stream_frozen_inventory", Path(__file__).with_name("frozen_inventory.py"))
F = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = F
_SPEC.loader.exec_module(F)
R = F.R
CHUNK = 64 * 1024
MAX_FILES = 64
MAX_PLAIN = 256 * 1024**2
MAX_HEADER = 128 * 1024
MAX_CIPHER = MAX_PLAIN + 2 * 1024**2
MAGIC = b"VKMSTRM1"
END = b"VKMEND01"
SHA = re.compile(r"^[0-9a-f]{64}$")
IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class StreamError(ValueError):
    """Fixed diagnostics only: do not expose source paths, payloads or keys."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def unique_json(raw):
    def pairs(entries):
        value = {}
        for key, item in entries:
            if key in value: raise StreamError("DUPLICATE_FIELD")
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(StreamError("NONFINITE_FIELD")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StreamError("INVALID_HEADER") from exc


class Bounds:
    def __init__(self, *, max_seconds, max_bytes, min_free_bytes=0, clock=time.monotonic):
        if (type(max_seconds) is not int or not 1 <= max_seconds <= 300
                or type(max_bytes) is not int or not 1 <= max_bytes <= 8 * MAX_CIPHER
                or type(min_free_bytes) is not int or min_free_bytes < 0):
            raise StreamError("EXPLICIT_BOUNDS_REQUIRED")
        self.clock, self.started = clock, clock()
        self.max_seconds, self.max_bytes, self.min_free_bytes, self.used = max_seconds, max_bytes, min_free_bytes, 0

    def check(self, size=0, *, disk=None):
        if self.clock() - self.started > self.max_seconds: raise StreamError("DEADLINE_EXCEEDED")
        if type(size) is not int or size < 0 or self.used + size > self.max_bytes:
            raise StreamError("BYTE_BUDGET_EXCEEDED")
        if disk is not None:
            import shutil
            if shutil.disk_usage(disk).free < self.min_free_bytes: raise StreamError("DISK_RESERVE_EXCEEDED")
        self.used += size


def validate_header(header):
    if (not isinstance(header, dict) or set(header) != {"schema", "manifest_sha256", "batch_id", "files"}
            or header["schema"] != "vkm-stream-immutable-fileset/1"
            or not isinstance(header["manifest_sha256"], str) or not SHA.fullmatch(header["manifest_sha256"])
            or not isinstance(header["batch_id"], str) or not NAMESPACE.fullmatch(header["batch_id"])
            or not isinstance(header["files"], list) or not 1 <= len(header["files"]) <= MAX_FILES):
        raise StreamError("INVALID_HEADER")
    ids, total = set(), 0
    for row in header["files"]:
        if (not isinstance(row, dict) or set(row) != {"logical_id", "size", "sha256"}
                or not isinstance(row["logical_id"], str) or not IDENT.fullmatch(row["logical_id"])
                or type(row["size"]) is not int or not 0 <= row["size"] <= MAX_PLAIN
                or not isinstance(row["sha256"], str) or not SHA.fullmatch(row["sha256"])):
            raise StreamError("INVALID_FILE_DECLARATION")
        if row["logical_id"].casefold() in ids: raise StreamError("DUPLICATE_LOGICAL_ID")
        ids.add(row["logical_id"].casefold()); total += row["size"]
    if total > MAX_PLAIN or len(canonical(header)) > MAX_HEADER: raise StreamError("BATCH_LIMIT")
    return header


def plan_batches(manifest, sizes, *, manifest_sha256):
    """Metadata only. Sizes are reviewed declarations, not fresh source reads."""
    R.validate_manifest(manifest)
    if not isinstance(manifest_sha256, str) or not SHA.fullmatch(manifest_sha256):
        raise StreamError("EXACT_MANIFEST_HASH_REQUIRED")
    if not isinstance(sizes, dict) or set(sizes) != {i["logical_id"] for i in manifest["items"]}:
        raise StreamError("EXACT_SIZE_DECLARATIONS_REQUIRED")
    batches, rows, total = [], [], 0
    for item in manifest["items"]:
        size = sizes[item["logical_id"]]
        if item["mutable"] or item["copies"] or not SHA.fullmatch(item.get("expected_sha256", "")):
            raise StreamError("IMMUTABLE_EXPECTED_VERSIONS_REQUIRED")
        if type(size) is not int or not 0 <= size <= MAX_PLAIN: raise StreamError("SINGLE_FILE_LIMIT")
        if rows and (len(rows) == MAX_FILES or total + size > MAX_PLAIN):
            batches.append(rows); rows, total = [], 0
        rows.append({"logical_id": item["logical_id"], "size": size, "sha256": item["expected_sha256"]}); total += size
    if rows: batches.append(rows)
    return [validate_header({"schema": "vkm-stream-immutable-fileset/1", "manifest_sha256": manifest_sha256,
        "batch_id": "batch-%04d" % index, "files": rows}) for index, rows in enumerate(batches, 1)]


def _write_all(sink, raw, bounds):
    view = memoryview(raw)
    while view:
        bounds.check()
        count = sink.write(view)
        if type(count) is not int or not 0 < count <= len(view): raise StreamError("SHORT_WRITE")
        bounds.check(count); view = view[count:]


def _read_exact(source, size, bounds):
    if size > MAX_HEADER: raise StreamError("HEADER_LIMIT")
    parts, remaining = [], size
    while remaining:
        bounds.check(); raw = source.read(min(CHUNK, remaining))
        if not isinstance(raw, bytes) or not raw or len(raw) > remaining: raise StreamError("TRUNCATED_STREAM")
        bounds.check(len(raw)); parts.append(raw); remaining -= len(raw)
    return b"".join(parts)


def _source_chunks(pinned, row):
    """Narrow adapter to the frozen retained-handle contract; never read_pinned()."""
    if type(pinned) is not F.PinnedFileSet or pinned._closed or row["logical_id"] not in pinned._handles:
        raise StreamError("PINNED_HANDLE_REQUIRED")
    file, path, stamp, native, definition = pinned._handles[row["logical_id"]]
    if (definition["mutable"] or definition.get("expected_sha256") != row["sha256"]
            or stamp["size"] != row["size"]): raise StreamError("SOURCE_DECLARATION_DIFFERS")
    def fence():
        pinned._deadline()
        if (R._stamp(os.fstat(file.fileno())) != stamp or F._change_stamp(file) != native
                or R._stamp(R._metadata(path)) != stamp): raise StreamError("PINNED_SOURCE_CHANGED")
    fence(); file.seek(0); remaining = row["size"]; digest = hashlib.sha256()
    while remaining:
        fence(); raw = pinned.budget.read(file, min(CHUNK, remaining))
        if not raw: raise StreamError("PINNED_SOURCE_CHANGED")
        digest.update(raw); remaining -= len(raw); yield raw
    if pinned.budget.read(file, 1) or digest.hexdigest() != row["sha256"]:
        raise StreamError("PINNED_SOURCE_CHANGED")
    fence()


def write_payload(pinned, sink, header, bounds):
    validate_header(header)
    wanted = [{"logical_id": i["logical_id"], "size": i["size"], "sha256": i["sha256"]}
        for i in pinned.capture["files"]]
    # Frozen inventory uses its own canonical ID ordering. Transport ordering
    # is separately pinned by the approved header, never inferred from handles.
    if ({i["logical_id"]: i for i in wanted} != {i["logical_id"]: i for i in header["files"]}
            or len(wanted) != len(header["files"])):
        raise StreamError("CAPTURE_DIFFERS_FROM_APPROVED_BATCH")
    raw = canonical(header)
    _write_all(sink, MAGIC + struct.pack(">I", len(raw)) + raw, bounds)
    for row in header["files"]:
        for chunk in _source_chunks(pinned, row): _write_all(sink, chunk, bounds)
    _write_all(sink, END, bounds)
    return {"header_sha256": hashlib.sha256(raw).hexdigest(), "file_count": len(wanted),
        "plain_bytes": sum(i["size"] for i in wanted), "status": "STREAM_WRITTEN_NOT_BACKUP_QUALIFIED"}


def _direct(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise StreamError("INDIRECT_OUTPUT")
    return path


def _new_file(path):
    path = Path(path).absolute(); _direct(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    return os.fdopen(fd, "wb", buffering=0)


def private_directory(path):
    """Exclusive local staging: protected user/SYSTEM DACL on Windows.

    All children inherit this DACL. No shell, icacls command, key read or shared
    user name. POSIX uses exact 0700. This guards local staging permissions; it
    does not claim protection from the same user or an administrator.
    """
    path = Path(path).absolute(); _direct(path.parent)
    if os.name != "nt":
        path.mkdir(mode=0o700)
        if stat.S_IMODE(path.stat().st_mode) != 0o700: raise StreamError("PRIVATE_DIRECTORY_DIFFERS")
        return path
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    class Attributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]; kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(Attributes)]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    token = wintypes.HANDLE(); sid = wintypes.LPWSTR(); descriptor = ctypes.c_void_p()
    actual = ctypes.c_void_p(); wanted_text = wintypes.LPWSTR(); actual_text = wintypes.LPWSTR()
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise StreamError("PRIVATE_TOKEN_UNAVAILABLE")
        length = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not 0 < length.value <= 4096: raise StreamError("PRIVATE_TOKEN_LIMIT")
        buffer = ctypes.create_string_buffer(length.value)
        if not security.GetTokenInformation(token, 1, buffer, length.value, ctypes.byref(length)):
            raise StreamError("PRIVATE_TOKEN_UNAVAILABLE")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not security.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid)):
            raise StreamError("PRIVATE_TOKEN_UNAVAILABLE")
        sddl = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;" + sid.value + ")"
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise StreamError("PRIVATE_DESCRIPTOR_UNAVAILABLE")
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise StreamError("PRIVATE_EXCLUSIVE_DIRECTORY_FAILED")
        _direct(path)
        if security.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(actual)):
            raise StreamError("PRIVATE_DESCRIPTOR_UNAVAILABLE")
        for pointer, output in [(descriptor, wanted_text), (actual, actual_text)]:
            if not security.ConvertSecurityDescriptorToStringSecurityDescriptorW(pointer, 1, 4, ctypes.byref(output), None):
                raise StreamError("PRIVATE_DESCRIPTOR_UNAVAILABLE")
        if actual_text.value != wanted_text.value: raise StreamError("PRIVATE_DIRECTORY_DIFFERS")
    finally:
        if token: kernel.CloseHandle(token)
        for pointer in (sid, descriptor, actual, wanted_text, actual_text):
            if pointer: kernel.LocalFree(pointer)
    return path


def restore_payload(source, destination, approved_header, bounds):
    """Stage exact logical IDs. No commit until age authenticates its final block."""
    validate_header(approved_header)
    if _read_exact(source, len(MAGIC), bounds) != MAGIC: raise StreamError("PROTOCOL_DIFFERS")
    size = struct.unpack(">I", _read_exact(source, 4, bounds))[0]
    if not 0 < size <= MAX_HEADER: raise StreamError("HEADER_LIMIT")
    raw = _read_exact(source, size, bounds)
    parsed = validate_header(unique_json(raw))
    if parsed != approved_header or raw != canonical(approved_header): raise StreamError("APPROVED_HEADER_DIFFERS")
    destination = Path(destination).absolute(); _direct(destination.parent)
    destination.mkdir(mode=0o700)  # exclusive attempt; retain partial data on failure
    _direct(destination); files = []
    for row in parsed["files"]:
        remaining, digest = row["size"], hashlib.sha256()
        with _new_file(destination / ("item-" + row["logical_id"] + ".bytes")) as sink:
            while remaining:
                bounds.check(disk=destination); block = source.read(min(CHUNK, remaining))
                if not isinstance(block, bytes) or not block or len(block) > remaining:
                    raise StreamError("TRUNCATED_STREAM")
                digest.update(block); _write_all(sink, block, bounds); remaining -= len(block)
            sink.flush(); os.fsync(sink.fileno())
        if digest.hexdigest() != row["sha256"]: raise StreamError("RESTORED_DIGEST_DIFFERS")
        files.append(dict(row))
    if _read_exact(source, len(END), bounds) != END: raise StreamError("TRUNCATED_STREAM")
    bounds.check(); extra = source.read(1)
    if extra != b"": raise StreamError("EXTRA_STREAM_BYTES")
    return {"schema": "vkm-stream-restore-stage/1", "status": "PROTOCOL_BYTES_VERIFIED_AUTHENTICATION_PENDING",
        "header_sha256": hashlib.sha256(raw).hexdigest(), "files": files}


def hash_file(path, bounds, *, maximum=MAX_CIPHER):
    digest, total = hashlib.sha256(), 0
    with R._open_regular(Path(path).absolute()) as file:
        before = R._stamp(os.fstat(file.fileno()))
        while True:
            bounds.check(); block = file.read(CHUNK)
            if not block: break
            total += len(block)
            if total > maximum: raise StreamError("FILE_LIMIT")
            bounds.check(len(block)); digest.update(block)
        if R._stamp(os.fstat(file.fileno())) != before: raise StreamError("FILE_CHANGED")
    return {"size": total, "sha256": digest.hexdigest()}


@contextmanager
def pinned_tool(executable, expected_sha256):
    """Retain the exact executable handle; never resolve an executable from PATH."""
    if not isinstance(expected_sha256, str) or not SHA.fullmatch(expected_sha256):
        raise StreamError("EXACT_TOOL_HASH_REQUIRED")
    executable = Path(executable).absolute()
    with R._open_regular(executable) as file:
        before = R._stamp(os.fstat(file.fileno())); native = F._change_stamp(file)
        digest = hashlib.sha256(); total = 0
        for block in iter(lambda: file.read(CHUNK), b""):
            total += len(block)
            if total > 64 * 1024**2: raise StreamError("TOOL_LIMIT")
            digest.update(block)
        def fence():
            if (R._stamp(os.fstat(file.fileno())) != before or F._change_stamp(file) != native
                    or R._stamp(R._metadata(executable)) != before
                    or digest.hexdigest() != expected_sha256): raise StreamError("PINNED_TOOL_CHANGED")
        fence(); yield str(executable), fence; fence()


class _ProcessGuard:
    """A watchdog kills blocked pipes/commands; output is disk or bounded streaming."""
    def __init__(self, process, bounds, *, output=None, maximum=MAX_CIPHER, disk=None):
        self.process, self.bounds, self.output, self.maximum, self.disk = process, bounds, output, maximum, disk
        self.stop, self.failure = threading.Event(), None
        self.thread = threading.Thread(target=self._run, daemon=True)
    def _run(self):
        while not self.stop.wait(0.05):
            try:
                self.bounds.check(disk=self.disk)
                if self.output is not None and os.fstat(self.output.fileno()).st_size > self.maximum:
                    raise StreamError("PROCESS_OUTPUT_LIMIT")
            except BaseException as error:
                self.failure = error; self.process.kill(); return
    def __enter__(self): self.thread.start(); return self
    def finish(self):
        try:
            self.process.wait(timeout=max(0.05, self.bounds.max_seconds - (self.bounds.clock() - self.bounds.started)))
        except subprocess.TimeoutExpired:
            raise StreamError("DEADLINE_EXCEEDED") from None
        if self.failure: raise self.failure
        self.bounds.check(disk=self.disk)
        if self.process.returncode: raise StreamError("EXTERNAL_COMMAND_FAILED")
        if self.output is not None and os.fstat(self.output.fileno()).st_size > self.maximum:
            raise StreamError("PROCESS_OUTPUT_LIMIT")
    def __exit__(self, *exc):
        if self.process.poll() is None:
            self.process.kill(); self.process.wait(timeout=5)
        self.stop.set(); self.thread.join(timeout=2)
        if self.thread.is_alive(): raise StreamError("WATCHDOG_DID_NOT_STOP")
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(self.process, name, None)
            if stream is not None:
                try: stream.close()
                except (OSError, ValueError): pass
        close_resources = getattr(self.process, "close_resources", None)
        if close_resources is not None: close_resources()


class _JobProcess:
    """No breakaway: the exact suspended process and descendants share the cap."""
    def __init__(self, process, job, api, memory_limit_bytes):
        self.process, self.job, self.api = process, job, api
        self.memory_limit_bytes = memory_limit_bytes
    def __getattr__(self, name): return getattr(self.process, name)
    def kill(self):
        if self.job is not None and not self.api._k32.TerminateJobObject(self.job, self.api.KILL_EXIT_CODE):
            raise StreamError("PROCESS_JOB_TERMINATION_FAILED")
    def close_resources(self):
        if self.job is not None:
            self.api._k32.CloseHandle(self.job); self.job = None


def _spawn_bounded(argv, *, memory_limit_bytes=None, **kwargs):
    """Windows native cap applied and read back before the child can execute.

    None preserves the separately qualified protocol-only tests. An execution
    intent must request the explicit cap; unsupported platforms fail closed.
    The existing Win32 ABI declarations are reused, never ProcessTree's fallback
    or its BREAKAWAY_OK policy. Its source must also be pinned by the executor.
    """
    if memory_limit_bytes is None: return subprocess.Popen(argv, **kwargs)
    if (type(memory_limit_bytes) is not int or not 64 * 1024**2 <= memory_limit_bytes <= 1024**3
            or os.name != "nt" or "creationflags" in kwargs):
        raise StreamError("NATIVE_MEMORY_CAP_REQUIRED")
    from vkm_jobs import procs as api
    import ctypes
    job = api._k32.CreateJobObjectW(None, None)
    if not job: raise StreamError("PROCESS_JOB_CREATE_FAILED")
    process = None
    try:
        info = api._EXTENDED()
        flags = api.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | 0x100 | 0x200
        info.BasicLimitInformation.LimitFlags = flags
        info.ProcessMemoryLimit = info.JobMemoryLimit = memory_limit_bytes
        if not api._k32.SetInformationJobObject(job, api._EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)):
            raise StreamError("PROCESS_JOB_LIMIT_FAILED")
        observed = api._EXTENDED()
        if (not api._k32.QueryInformationJobObject(job, api._EXTENDED_LIMIT, ctypes.byref(observed), ctypes.sizeof(observed), None)
                or observed.BasicLimitInformation.LimitFlags != flags
                or observed.ProcessMemoryLimit != memory_limit_bytes or observed.JobMemoryLimit != memory_limit_bytes):
            raise StreamError("PROCESS_JOB_LIMIT_DIFFERS")
        process = subprocess.Popen(argv, creationflags=api.CREATE_SUSPENDED | api.CREATE_NO_WINDOW,
            **kwargs)
        if not api._k32.AssignProcessToJobObject(job, int(process._handle)):
            raise StreamError("PROCESS_JOB_ASSIGN_FAILED")
        if api._resume_process(process.pid) != 1: raise StreamError("PROCESS_JOB_RESUME_FAILED")
        return _JobProcess(process, job, api, memory_limit_bytes)
    except BaseException:
        if process is not None:
            process.kill(); process.wait(timeout=5)
            for name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, name, None)
                if stream is not None: stream.close()
        api._k32.CloseHandle(job)
        raise


def encrypt_pinned(pinned, header, destination, *, age_executable, age_sha256, recipient, bounds, memory_limit_bytes=None):
    if not isinstance(recipient, str) or not re.fullmatch(r"age1[0-9a-z]{58}", recipient):
        raise StreamError("EXPLICIT_PUBLIC_RECIPIENT_REQUIRED")
    bounds.check(disk=Path(destination).parent)
    with pinned_tool(age_executable, age_sha256) as (executable, fence), _new_file(destination) as output:
        process = _spawn_bounded([executable, "-r", recipient], memory_limit_bytes=memory_limit_bytes, stdin=subprocess.PIPE, stdout=output,
            stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)
        with _ProcessGuard(process, bounds, output=output, disk=Path(destination).parent) as guard:
            fence(); result = write_payload(pinned, process.stdin, header, bounds)
            process.stdin.close(); guard.finish(); fence()
        output.flush(); os.fsync(output.fileno())
    result.update(cipher=hash_file(destination, bounds), status="ENCRYPTED_LOCAL_NOT_REMOTE_OR_RESTORE_QUALIFIED")
    pinned.recheck()
    return result


def decrypt_restore(ciphertext, destination, header, *, age_executable, age_sha256, identity_path, bounds, memory_limit_bytes=None):
    """Explicit execution only. Never inspect, log, transport or hash identity bytes."""
    identity_path = _direct(identity_path)
    with pinned_tool(age_executable, age_sha256) as (executable, fence), R._open_regular(Path(ciphertext).absolute()) as cipher:
        process = _spawn_bounded([executable, "-d", "-i", str(identity_path)], memory_limit_bytes=memory_limit_bytes, stdin=cipher,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)
        with _ProcessGuard(process, bounds, disk=Path(destination).parent) as guard:
            fence(); result = restore_payload(process.stdout, destination, header, bounds)
            process.stdout.close(); guard.finish(); fence()
    for row in result["files"]:
        observed = hash_file(Path(destination) / ("item-" + row["logical_id"] + ".bytes"), bounds, maximum=MAX_PLAIN)
        if observed != {"size": row["size"], "sha256": row["sha256"]}: raise StreamError("RESTORED_DISK_DIFFERS")
    result["status"] = "AUTHENTICATED_RESTORE_BYTES_VERIFIED_NOT_FINAL_SOURCE_GUARDED"
    # Caller keeps PinnedFileSet open, performs final recheck(), then publishes
    # its receipt. No synthetic/protocol-only path can create this age result.
    return result


# Executed only by a separately approved SSH call. No input-selected path or
# executable; fixed cipher/manifest/marker names and FD-relative nofollow access.
REMOTE_SOURCE = r'''
import hashlib,json,os,resource,signal,stat,sys,time
def run(action,root,namespace,expected,python_version):
    if sys.version_info[:3]!=tuple(python_version): raise RuntimeError('REMOTE_PYTHON_DIFFERS')
    # Default SIGALRM terminates at the OS boundary, including blocked pipe IO.
    # A Python handler or loop polling alone can leave an orphan after SSH dies.
    signal.signal(signal.SIGALRM,signal.SIG_DFL);signal.alarm(expected['timeout_seconds'])
    memory=expected['memory_limit_bytes']
    resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
    if resource.getrlimit(resource.RLIMIT_AS)!=(memory,memory):raise RuntimeError('REMOTE_MEMORY_DIFFERS')
    if os.geteuid()!=expected['root_uid'] or os.getegid()!=expected['root_gid']:raise RuntimeError('REMOTE_OWNER_DIFFERS')
    python_expected=expected['python_executable']
    if os.readlink('/proc/self/exe')!=python_expected['path'] or os.path.realpath('/usr/bin/python3')!=python_expected['path']:raise RuntimeError('REMOTE_PYTHON_PATH_DIFFERS')
    python_fd=os.open('/proc/self/exe',os.O_RDONLY)
    def python_guard():
        info=os.fstat(python_fd)
        if (not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino,info.st_uid,stat.S_IMODE(info.st_mode),info.st_size)!=(python_expected['device'],python_expected['inode'],python_expected['uid'],python_expected['mode'],python_expected['size'])):raise RuntimeError('REMOTE_PYTHON_IDENTITY')
        before=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        os.lseek(python_fd,0,os.SEEK_SET);digest=hashlib.sha256();total=0
        while True:
            block=os.read(python_fd,65536)
            if not block:break
            total+=len(block)
            if total>python_expected['size']:raise RuntimeError('REMOTE_PYTHON_LIMIT')
            digest.update(block)
        after=os.fstat(python_fd);path=os.stat(python_expected['path'],follow_symlinks=False)
        if (digest.hexdigest()!=python_expected['sha256'] or before!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or (path.st_dev,path.st_ino)!=(after.st_dev,after.st_ino)):raise RuntimeError('REMOTE_PYTHON_CHANGED')
    python_guard()
    deadline=time.monotonic()+expected['timeout_seconds']
    def check():
        if time.monotonic()>deadline: raise RuntimeError('REMOTE_DEADLINE')
    def owned(handle,mode=None,regular=False):
        info=os.fstat(handle)
        if info.st_uid!=expected['root_uid'] or info.st_gid!=expected['root_gid'] or info.st_dev!=expected['root_device']:raise RuntimeError('REMOTE_ROOT_IDENTITY')
        if mode is not None and stat.S_IMODE(info.st_mode)!=mode:raise RuntimeError('REMOTE_PRIVATE_MODE')
        if regular and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1):raise RuntimeError('REMOTE_FILE_IDENTITY')
        if not regular and not stat.S_ISDIR(info.st_mode):raise RuntimeError('REMOTE_DIRECTORY_IDENTITY')
        return info
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    fd=os.open('/',flags)
    try:
        parts=root.strip('/').split('/')
        for index,part in enumerate(parts):
            check(); nxt=os.open(part,flags,dir_fd=fd);os.close(fd);fd=nxt
            if index==len(parts)-2:
                if owned(fd,0o755).st_ino!=expected['parent_inode']:raise RuntimeError('REMOTE_PARENT_INODE')
        owned(fd,0o700)
        if os.fstat(fd).st_ino!=expected['root_inode']:raise RuntimeError('REMOTE_ROOT_INODE')
        if action=='copy':os.mkdir(namespace,0o700,dir_fd=fd)
        target=os.open(namespace,flags,dir_fd=fd)
        try:
            owned(target,0o700 if action=='copy' else 0o500)
            raw=json.dumps(expected,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode('ascii')
            marker=hashlib.sha256(raw).hexdigest().encode('ascii')+b'\n'
            if action=='copy':
                if os.statvfs(target).f_bavail*os.statvfs(target).f_frsize<expected['size']+expected['min_free_bytes']:raise RuntimeError('REMOTE_DISK_RESERVE')
                cipher=os.open('cipher.age',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=target)
                digest=hashlib.sha256();remaining=expected['size']
                try:
                    owned(cipher,0o600,True)
                    while remaining:
                        check();block=sys.stdin.buffer.read(min(65536,remaining))
                        if not block:raise RuntimeError('TRUNCATED_CIPHER')
                        digest.update(block);remaining-=len(block);view=memoryview(block)
                        while view:
                            check();n=os.write(cipher,view)
                            if n<=0:raise RuntimeError('SHORT_WRITE')
                            view=view[n:]
                    if sys.stdin.buffer.read(1) or digest.hexdigest()!=expected['sha256']:raise RuntimeError('CIPHER_DIFFERS')
                    os.fchmod(cipher,0o400);os.fsync(cipher)
                finally:os.close(cipher)
                python_guard()
                for name,data in [('manifest.json',raw),('COMMITTED',marker)]:
                    out=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=target)
                    try:
                        owned(out,0o600,True)
                        view=memoryview(data)
                        while view:
                            n=os.write(out,view)
                            if n<=0:raise RuntimeError('SHORT_WRITE')
                            view=view[n:]
                        os.fchmod(out,0o400);os.fsync(out)
                    finally:os.close(out)
                    os.fsync(target)
                os.fchmod(target,0o500);os.fsync(target);os.fsync(fd)
                python_guard();sys.stdout.buffer.write(marker);sys.stdout.buffer.flush()
            else:
                for name,wanted in [('manifest.json',raw),('COMMITTED',marker)]:
                    source=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
                    try:
                        info=owned(source,0o400,True)
                        if info.st_size!=len(wanted) or os.read(source,len(wanted)+1)!=wanted:raise RuntimeError('NOT_COMMITTED')
                    finally:os.close(source)
                source=os.open('cipher.age',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
                try:
                    before=owned(source,0o400,True)
                    if before.st_size!=expected['size']:raise RuntimeError('CIPHER_IDENTITY')
                    remaining=expected['size'];digest=hashlib.sha256()
                    while remaining:
                        check();block=os.read(source,min(65536,remaining))
                        if not block:raise RuntimeError('TRUNCATED_CIPHER')
                        remaining-=len(block);digest.update(block);sys.stdout.buffer.write(block)
                    sys.stdout.buffer.flush();after=os.fstat(source)
                    if digest.hexdigest()!=expected['sha256'] or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise RuntimeError('CIPHER_CHANGED')
                    python_guard()
                finally:os.close(source)
        finally:os.close(target)
    finally:os.close(fd);os.close(python_fd);signal.alarm(0)
'''


def remote_program(action, *, target_root, namespace, cipher, stream_header, intent_sha256, python_version, min_free_bytes,
        expected_uid, expected_gid, expected_device, expected_root_inode, expected_parent_inode, timeout_seconds, memory_limit_bytes,
        python_executable):
    if action not in {"copy", "restore"} or not isinstance(namespace, str) or not NAMESPACE.fullmatch(namespace):
        raise StreamError("INVALID_REMOTE_ACTION")
    if target_root != "/srv/vkm-edge/recovery-unique":
        raise StreamError("OWNED_REMOTE_ROOT_REQUIRED")
    validate_header(stream_header)
    if (not isinstance(python_executable, dict) or set(python_executable) != {"path", "sha256", "size", "device", "inode", "uid", "mode"}
            or not isinstance(python_executable["path"], str) or not re.fullmatch(r"/usr/bin/python3\.\d{1,2}", python_executable["path"])
            or not isinstance(python_executable["sha256"], str) or not SHA.fullmatch(python_executable["sha256"])
            or any(type(python_executable[field]) is not int or python_executable[field] <= 0 for field in ("size", "device", "inode"))
            or python_executable["size"] > 64 * 1024**2 or type(python_executable["uid"]) is not int or python_executable["uid"] != 0
            or type(python_executable["mode"]) is not int or python_executable["mode"] != 0o755):
        raise StreamError("EXACT_REMOTE_PYTHON_BINARY_REQUIRED")
    if (not isinstance(cipher, dict) or set(cipher) != {"size", "sha256"}
            or type(cipher["size"]) is not int or not 1 <= cipher["size"] <= MAX_CIPHER
            or not isinstance(cipher["sha256"], str) or not SHA.fullmatch(cipher["sha256"])
            or not isinstance(intent_sha256, str) or not SHA.fullmatch(intent_sha256)
            or not isinstance(python_version, tuple) or len(python_version) != 3
            or any(type(v) is not int or v < 0 for v in python_version) or python_version[0] != 3
            or type(min_free_bytes) is not int or min_free_bytes < 0
            or type(expected_uid) is not int or expected_uid < 0
            or type(expected_gid) is not int or expected_gid < 0
            or type(expected_device) is not int or expected_device <= 0
            or type(expected_root_inode) is not int or expected_root_inode <= 0
            or type(expected_parent_inode) is not int or expected_parent_inode <= 0
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 180
            or type(memory_limit_bytes) is not int or memory_limit_bytes != 256 * 1024**2):
        raise StreamError("EXACT_REMOTE_CONTRACT_REQUIRED")
    expected = {"schema": "vkm-stream-cipher-backup/1", **cipher, "intent_sha256": intent_sha256,
        "stream_header": stream_header,
        "namespace": namespace, "min_free_bytes": min_free_bytes,
        "root_uid": expected_uid, "root_gid": expected_gid, "root_device": expected_device,
        "root_inode": expected_root_inode, "parent_inode": expected_parent_inode,
        "timeout_seconds": timeout_seconds, "memory_limit_bytes": memory_limit_bytes,
        "python_executable": python_executable,
        "scope": "IMMUTABLE_EXPECTED_VERSIONS_NOT_ATOMIC_CURRENT_GENERATION"}
    program = REMOTE_SOURCE + "\nrun(" + ",".join(repr(v) for v in (action, target_root, namespace, expected, python_version)) + ")\n"
    return program, hashlib.sha256(canonical(expected)).hexdigest()


def transfer_cipher(ciphertext, *, ssh_executable, ssh_sha256, request, bounds, destination=None, memory_limit_bytes=None):
    """Fixed EDGE transport. Rebuild its program from validated fields internally."""
    if (not isinstance(request, dict) or set(request) != {"action", "target_root", "namespace", "cipher", "stream_header",
            "intent_sha256", "python_version", "min_free_bytes", "expected_uid", "expected_gid", "expected_device",
            "expected_root_inode", "expected_parent_inode", "timeout_seconds", "memory_limit_bytes", "python_executable"}
            or (destination is None) != (request.get("action") == "copy")):
        raise StreamError("EXACT_REMOTE_REQUEST_REQUIRED")
    program, manifest_sha = remote_program(**request)
    with pinned_tool(ssh_executable, ssh_sha256) as (executable, fence):
        argv = [executable, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "edge", "/usr/bin/python3", "-c", shlex.quote(program)]
        if len(subprocess.list2cmdline(argv)) > 30000: raise StreamError("REMOTE_COMMAND_LINE_LIMIT")
        if destination is None:
            with R._open_regular(Path(ciphertext).absolute()) as source:
                process = _spawn_bounded(argv, memory_limit_bytes=memory_limit_bytes, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                with _ProcessGuard(process, bounds) as guard:
                    fence(); receipt = _read_exact(process.stdout, 65, bounds)
                    if process.stdout.read(1) != b"": raise StreamError("REMOTE_RECEIPT_LIMIT")
                    process.stdout.close(); guard.finish(); fence()
                if receipt != manifest_sha.encode("ascii") + b"\n": raise StreamError("REMOTE_RECEIPT_DIFFERS")
                return {"status": "REMOTE_CIPHER_COMMIT_REPORTED_NOT_RESTORE_QUALIFIED", "manifest_sha256": receipt[:-1].decode("ascii")}
        with _new_file(destination) as output:
            process = _spawn_bounded(argv, memory_limit_bytes=memory_limit_bytes, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, bufsize=0)
            with _ProcessGuard(process, bounds, output=output, disk=Path(destination).parent) as guard:
                fence(); guard.finish(); fence()
            output.flush(); os.fsync(output.fileno())
        observed = hash_file(destination, bounds)
        if observed != request["cipher"]: raise StreamError("RESTORED_CIPHER_DIFFERS")
        return {"status": "REMOTE_CIPHER_RESTORED_NOT_DECRYPTED", **observed}
