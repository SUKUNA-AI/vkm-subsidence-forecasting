#!/usr/bin/env python3
"""Pin an explicit recovery file set without inventing a filesystem snapshot.

The caller retains the handles through copy/restore. Windows handles deny ordinary
write/delete opens; POSIX has no equivalent writer exclusion. Neither mode claims
an atomic directory snapshot, protection from existing mmap writers, or uniqueness.
No output contains file contents or machine locators.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


_SPEC = importlib.util.spec_from_file_location("vkm_recovery_inventory", Path(__file__).with_name("unique_inventory.py"))
R = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = R
_SPEC.loader.exec_module(R)
MAX_OPEN_FILES = 64


def _change_stamp(file):
    """Native ChangeTime is distinct from Python's Windows creation-time ctime."""
    if os.name != "nt":
        return {"ctime_ns": os.fstat(file.fileno()).st_ctime_ns}
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("created", ctypes.c_longlong), ("accessed", ctypes.c_longlong),
                    ("written", ctypes.c_longlong), ("changed", ctypes.c_longlong),
                    ("attributes", wintypes.DWORD)]

    api = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    api.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.restype = wintypes.BOOL
    basic = Basic()
    if not api(msvcrt.get_osfhandle(file.fileno()), 0, ctypes.byref(basic), ctypes.sizeof(basic)):
        raise R.InventoryError("NATIVE_CHANGE_STAMP_UNAVAILABLE")
    return {"created": basic.created, "written": basic.written,
            "changed": basic.changed, "attributes": basic.attributes}


class PinnedFileSet:
    """Bounded simultaneous handles plus two full hashes; use as a context manager."""

    def __init__(self, manifest, *, byte_budget, max_seconds, environ=None, mutable_policy="REJECT"):
        if type(max_seconds) is not int or not 1 <= max_seconds <= 300:
            raise R.InventoryError("TIME_BUDGET_REQUIRED")
        R.validate_manifest(manifest)
        if len(manifest["items"]) > MAX_OPEN_FILES:
            raise R.InventoryError("OPEN_HANDLE_LIMIT")
        if any(item["copies"] for item in manifest["items"]):
            raise R.InventoryError("SOURCE_ONLY_CAPTURE_REQUIRED")
        if mutable_policy not in {"REJECT", "GUARDED_WINDOWS_BYTES_ONLY"}:
            raise R.InventoryError("INVALID_MUTABLE_POLICY")
        if mutable_policy != "REJECT" and os.name != "nt":
            raise R.InventoryError("MUTABLE_WRITE_EXCLUSION_UNAVAILABLE")
        self.manifest = manifest
        self.environ = os.environ if environ is None else environ
        self.byte_budget = byte_budget
        self.max_seconds = max_seconds
        self.mutable_policy = mutable_policy
        self._stack = ExitStack()
        self._handles = {}
        self._closed = True

    def __enter__(self):
        self.started = time.monotonic()
        plan = R.plan(self.manifest, byte_budget=self.byte_budget, environ=self.environ)
        if plan["status"] != "READY":
            raise R.InventoryError(plan["blocker"])
        # The older inventory intentionally excludes mutable files from verify.
        # This explicit capture route still reserves both full reads up front.
        minimum = 2 * sum(row["source"].get("size_bytes", 0) + 1 for row in plan["items"])
        if minimum > self.byte_budget:
            raise R.InventoryError("BYTE_BUDGET_EXCEEDED")
        self.roots, bindings = R._resolve(self.manifest, self.environ)
        self.budget = R.ByteBudget(self.byte_budget)
        self._closed = False
        definitions = {i["logical_id"]: i for i in self.manifest["items"]}
        try:
            for row in plan["items"]:
                self._deadline()
                if row["source"]["status"] != "REGULAR":
                    raise R.InventoryError("SOURCE_NOT_AVAILABLE")
                definition = definitions[row["logical_id"]]
                if definition["mutable"] and self.mutable_policy == "REJECT":
                    raise R.InventoryError("MUTABLE_REQUIRES_SEPARATE_SNAPSHOT")
                path = self.roots[definition["source"]["root_id"]] / definition["source"]["path"]
                file = self._stack.enter_context(R._open_regular(path))
                stamp = R._stamp(os.fstat(file.fileno()))
                if stamp != row["source"]["identity"]:
                    raise R.InventoryError("CHANGED_AFTER_PLAN")
                self._handles[row["logical_id"]] = (file, path, stamp, _change_stamp(file), definition)
            first = self._hash_pass()
            second = self._hash_pass()
            if first != second:
                raise R.InventoryError("CONTENT_CHANGED_BETWEEN_PASSES")
            self.capture = {
                "schema": "vkm-pinned-recovery-fileset/1", "status": "EXPLICIT_BYTES_PINNED",
                "manifest_sha256": R._digest(self.manifest), "plan_sha256": plan["plan_sha256"],
                "root_bindings": bindings, "files": first, "file_count": len(first),
                "total_bytes": sum(i["size"] for i in first), "bytes_read": self.budget.used,
                "write_exclusion": "WINDOWS_HANDLE_WRITE_DELETE_DENIED" if os.name == "nt" else "NOT_PROVEN_POSIX",
                "snapshot_scope": "GUARDED_EXPLICIT_FILESET_NOT_ATOMIC_FILESYSTEM_SNAPSHOT",
                "existing_mmap_writer_exclusion": "NOT_PROVEN", "semantic_uniqueness": "UNKNOWN",
                "backup": "NOT_RUN", "restore": "NOT_RUN",
                "mutable_policy": self.mutable_policy,
                "mutable_sources": [i["logical_id"] for i in self.manifest["items"] if i["mutable"]],
            }
            self.capture["capture_sha256"] = R._digest(self.capture)
            return self
        except BaseException:
            self._stack.close()
            self._closed = True
            raise

    def _deadline(self):
        if time.monotonic() - self.started > self.max_seconds:
            raise R.InventoryError("TIME_BUDGET_EXCEEDED")

    def _hash_pass(self):
        results = []
        for ident, (file, path, stamp, native, definition) in self._handles.items():
            self._deadline()
            if R._stamp(os.fstat(file.fileno())) != stamp or _change_stamp(file) != native:
                raise R.InventoryError("PINNED_SOURCE_CHANGED")
            file.seek(0)
            remaining = stamp["size"]
            digest = hashlib.sha256()
            while remaining:
                self._deadline()
                block = self.budget.read(file, min(R.CHUNK, remaining))
                if not block:
                    raise R.InventoryError("PINNED_SOURCE_CHANGED")
                digest.update(block)
                remaining -= len(block)
            if self.budget.read(file, 1):
                raise R.InventoryError("PINNED_SOURCE_CHANGED")
            if (R._stamp(os.fstat(file.fileno())) != stamp or _change_stamp(file) != native
                    or R._stamp(R._metadata(path)) != stamp):
                raise R.InventoryError("PINNED_SOURCE_CHANGED")
            sha = digest.hexdigest()
            if definition.get("expected_sha256", sha) != sha:
                raise R.InventoryError("EXPECTED_HASH_MISMATCH")
            results.append({"logical_id": ident, "size": stamp["size"], "sha256": sha,
                            "source_identity": stamp, "native_change_stamp": native})
        return results

    def read_pinned(self, logical_id):
        """Read verified captured bytes for a bounded caller, never print them."""
        if self._closed or logical_id not in self._handles:
            raise R.InventoryError("PINNED_HANDLE_UNAVAILABLE")
        self._deadline()
        file, _, stamp, native, _ = self._handles[logical_id]
        if R._stamp(os.fstat(file.fileno())) != stamp or _change_stamp(file) != native:
            raise R.InventoryError("PINNED_SOURCE_CHANGED")
        file.seek(0)
        data = self.budget.read(file, stamp["size"])
        wanted = next(i for i in self.capture["files"] if i["logical_id"] == logical_id)
        if len(data) != stamp["size"] or hashlib.sha256(data).hexdigest() != wanted["sha256"]:
            raise R.InventoryError("PINNED_SOURCE_CHANGED")
        if R._stamp(os.fstat(file.fileno())) != stamp or _change_stamp(file) != native:
            raise R.InventoryError("PINNED_SOURCE_CHANGED")
        return data

    def recheck(self):
        if self._closed:
            raise R.InventoryError("PINNED_HANDLE_UNAVAILABLE")
        if self._hash_pass() != self.capture["files"]:
            raise R.InventoryError("PINNED_SOURCE_CHANGED")
        return {"status": "SOURCE_BYTES_AND_IDENTITIES_UNCHANGED", "capture_sha256": self.capture["capture_sha256"],
                "bytes_read": self.budget.used}

    def __exit__(self, *exc):
        self._stack.close()
        self._closed = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--byte-budget", required=True, type=int)
    parser.add_argument("--max-seconds", required=True, type=int)
    args = parser.parse_args()
    try:
        with Path(args.manifest).open("rb") as stream:
            raw = stream.read(R.MAX_MANIFEST_BYTES + 1)
        if len(raw) > R.MAX_MANIFEST_BYTES:
            raise R.InventoryError("MANIFEST_LIMIT")
        with PinnedFileSet(json.loads(raw), byte_budget=args.byte_budget, max_seconds=args.max_seconds) as pinned:
            print(json.dumps(pinned.capture, sort_keys=True))
    except (R.InventoryError, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": R._failure(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
