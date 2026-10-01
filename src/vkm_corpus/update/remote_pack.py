"""Identity of the bytes already loaded by a Linux retrieval PackStore.

Qualification hashes native files once. Subsequent observations use Linux
change-time leases; Windows/native reparse-handle qualification is unavailable.
Operator-owned immutable files and an exact read-only service profile are required.
"""
from __future__ import annotations

import hashlib
import json
import platform
import stat
from pathlib import Path

from vkm_corpus.parquet.atomic import sha256_of
from vkm_evidence.contracts import record_hash

PACK_FILES = ("pack.json", "tokens.f16", "index.parquet")


def file_signatures(directory):
    out = {}
    for name in PACK_FILES:
        s = (Path(directory) / name).stat()
        out[name] = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    return out


class QualifiedPackLease:
    def __init__(self, store):
        if platform.system() != "Linux":
            raise ValueError("qualified loaded-pack receiver requires Linux change-time semantics")
        self.store = store
        self.directory = Path(store.directory).absolute()
        self._watch = getattr(store, "_load_watch", None)
        if self._watch is None:
            raise ValueError("native load-time pack mutation watch unavailable")
        self._watch.check()
        self.signatures = file_signatures(self.directory)
        self._ordinary_files()
        if store._load_signatures != self.signatures:
            raise ValueError("pack files changed since this store was loaded")
        data = (self.directory / "pack.json").read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        manifest = json.loads(data)
        if store._load_manifest_sha256 != digest or manifest != store.manifest:
            raise ValueError("loaded manifest differs from native bytes")
        if set(manifest.get("files", {})) != {"tokens.f16", "index.parquet"}:
            raise ValueError("complete native pack file inventory required")
        hashes = {}
        for name, entry in manifest["files"].items():
            path = self.directory / name
            hashes[name] = sha256_of(path)
            if hashes[name] != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
                raise ValueError("loaded pack content differs from manifest")
        if file_signatures(self.directory) != self.signatures:
            raise ValueError("pack changed during qualification")
        self.identity = {"schema": "vkm-loaded-pack/1", "pack_id": manifest["pack_id"],
            "snapshot_id": manifest["snapshot_id"], "manifest_sha256": digest,
            "config_signature": manifest["config_signature"], "config_sha256": record_hash(manifest["config"]),
            "files": hashes, "count": len(store), "total_tokens": store.total}
        self.identity_sha256 = record_hash(self.identity)
        self._watch.check()

    def _ordinary_files(self):
        if any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
            raise ValueError("indirect pack root")
        for name in PACK_FILES:
            path = self.directory / name
            if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                raise ValueError("pack requires ordinary nonindirect files")

    def observe(self):
        self._watch.check()
        self._ordinary_files()
        if (Path(self.store.directory).absolute() != self.directory
                or file_signatures(self.directory) != self.signatures
                or record_hash(self.identity) != self.identity_sha256):
            raise ValueError("qualified loaded pack changed")
        return json.loads(json.dumps(self.identity))
