"""Native identity of owned Linux retrieval children; no encode/model calls.

An external llama-server, missing mmap evidence, changed process, or a file whose
load-time signature was not captured is unqualified. Identity is not model
quality or parity qualification. The caller must bind this before reader admission.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
from pathlib import Path

from vkm_corpus.parquet.atomic import sha256_of
from vkm_evidence.contracts import record_hash


def file_signature(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("indirect native model resource")
    s = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(s.st_mode):
        raise ValueError("native model resource is not an ordinary file")
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def capture_load_files(paths):
    """Legacy loading stays available; missing capture can never qualify later."""
    try:
        return {str(Path(p).absolute()): file_signature(p) for p in paths}
    except (OSError, ValueError):
        return None


def process_identity(model):
    if platform.system() != "Linux" or not model.alive() or model.pid is None:
        raise ValueError("native owned model identity requires a live Linux child")
    proc = Path("/proc") / str(model.pid)
    # comm may contain spaces/parentheses; fields after the final ')' start at field 3.
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    started = int(fields[19])
    command = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    command = [os.fsdecode(x) for x in command]
    if getattr(model.client, "host", None) != "127.0.0.1" or getattr(model.client, "port", None) != model.slot.port:
        raise ValueError("actual client does not address the owned loopback child")
    for flag, expected in (("--host", "127.0.0.1"), ("--port", str(model.slot.port))):
        positions = [i for i, value in enumerate(command) if value == flag]
        if len(positions) != 1 or positions[0] + 1 >= len(command) or command[positions[0] + 1] != expected:
            raise ValueError("child command endpoint differs from actual client")
    if (proc / "ns/net").stat().st_ino != Path("/proc/self/ns/net").stat().st_ino:
        raise ValueError("owned model is in a different network namespace")
    sockets = set()
    for fd in (proc / "fd").iterdir():
        try:
            link = os.readlink(fd)
        except FileNotFoundError:
            continue
        if link.startswith("socket:["):
            sockets.add(link[8:-1])
    address = f"0100007F:{model.slot.port:04X}"
    if not any(len(row) > 9 and row[1] == address and row[3] == "0A" and row[9] in sockets
               for row in (line.split() for line in (proc / "net/tcp").read_text().splitlines()[1:])):
        raise ValueError("actual client port is not owned by observed child")
    expected_model = str(Path(model.slot.gguf).resolve())
    positions = [i for i, v in enumerate(command) if v in {"-m", "--model"}]
    if len(positions) != 1 or positions[0] + 1 >= len(command) or str(Path(command[positions[0] + 1]).resolve()) != expected_model:
        raise ValueError("owned child command selects different model weights")
    expected_exe = Path(model.llama_server).resolve()
    if (proc / "exe").resolve() != expected_exe:
        raise ValueError("owned child executable differs")
    fs = Path(expected_model).stat()
    matching = False
    for line in (proc / "maps").read_text().splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) == 6 and parts[5] == expected_model:
            major, minor = (int(v, 16) for v in parts[3].split(":"))
            matching |= int(parts[4]) == fs.st_ino and (major, minor) == (os.major(fs.st_dev), os.minor(fs.st_dev))
    if not matching:
        raise ValueError("actual child weight mmap identity unavailable")
    return {"pid": model.pid, "start_ticks": started, "command_sha256": record_hash(command),
            "executable_signature": list(file_signature(expected_exe)),
            "endpoint_sha256": record_hash(model.slot.endpoint)}


class OwnedModelLease:
    def __init__(self, encoder, model):
        if encoder.backend is not model.client or encoder.slot != model.slot:
            raise ValueError("encoder does not use the observed owned model client")
        self.encoder, self.model = encoder, model
        load = getattr(model, "_load_files", None)
        encoded = getattr(encoder, "_load_files", None)
        if not load or not encoded:
            raise ValueError("native model/tokenizer load-time file bindings unavailable")
        self.watches = [getattr(owner, "_load_watch", None) for owner in (model, encoder)]
        if any(w is None for w in self.watches):
            raise ValueError("native model/tokenizer load-time mutation watches unavailable")
        self.files = {**load, **encoded}
        self._files_unchanged()
        self.process = process_identity(model)
        slot = encoder.slot
        hashes = {p: sha256_of(Path(p)) for p in self.files}
        gguf = hashes[str(Path(slot.gguf).absolute())]
        tokenizer = hashes[str((Path(slot.tokenizer_dir) / encoder.spec.tokenizer_file).absolute())]
        if gguf != slot.gguf_sha256 or tokenizer != encoder.qconfig.tokenizer_sha256:
            raise ValueError("native weights/tokenizer hashes differ from query signature")
        if slot.heads and hashes[str(Path(slot.heads).absolute())] != slot.heads_sha256:
            raise ValueError("native heads hash differs from loaded configuration")
        self.config_sha256 = record_hash(encoder.qconfig.as_dict())
        if encoder.signature != encoder.qconfig.signature():
            raise ValueError("loaded encoder signature differs from actual query configuration")
        self.identity = {"role": slot.role, "model_id": encoder.spec.model_id,
            "model_revision": encoder.spec.model_revision, "query_signature": encoder.signature,
            "query_config_sha256": self.config_sha256, "weights_sha256": gguf, "tokenizer_sha256": tokenizer,
            "resources_sha256": record_hash(sorted(hashes.values())), "process": self.process}
        self._files_unchanged()
        if process_identity(model) != self.process:
            raise ValueError("model restarted during qualification")

    def _files_unchanged(self):
        for watch in self.watches:
            watch.check()
        if any(file_signature(p) != signature for p, signature in self.files.items()):
            raise ValueError("model resource changed since loading")

    def observe(self):
        self._files_unchanged()
        if (process_identity(self.model) != self.process or self.encoder.backend is not self.model.client
                or record_hash(self.encoder.qconfig.as_dict()) != self.config_sha256
                or self.encoder.signature != self.identity["query_signature"]):
            raise ValueError("qualified model process/configuration changed")
        return json.loads(json.dumps(self.identity))


class RetrievalServiceLease:
    def __init__(self, encoders, residency, store):
        if residency is None or not encoders or set(encoders) != set(residency.processes):
            raise ValueError("all serving encoders require observed owned children")
        from vkm_corpus.update.service_identity import (RETRIEVAL, require_retrieval_profile,
            service_code_identity, service_dependencies_identity)
        require_retrieval_profile(encoders, residency)
        self.store = store
        self.encoders, self.residency = encoders, residency
        self.models = {role: OwnedModelLease(enc, residency.processes[role]) for role, enc in encoders.items()}
        self.pack = store.qualified_identity()
        from vkm_corpus.embeddings.pack import COMPATIBLE_FIELDS, compatibility
        if "late" not in encoders:
            raise ValueError("qualified late pack requires a bound late encoder")
        expected = {k: getattr(encoders["late"].qconfig, k) for k in COMPATIBLE_FIELDS}
        if compatibility(store.current().manifest, expected):
            raise ValueError("native loaded pack is incompatible with the actual late encoder")
        self.code = service_code_identity(RETRIEVAL)
        self.dependencies = service_dependencies_identity(RETRIEVAL)

    def observe(self):
        from vkm_corpus.update.service_identity import (RETRIEVAL, require_retrieval_profile,
            service_code_identity, service_dependencies_identity)
        require_retrieval_profile(self.encoders, self.residency)
        if service_code_identity(RETRIEVAL) != self.code or service_dependencies_identity(RETRIEVAL) != self.dependencies:
            raise ValueError("retrieval service code/dependencies changed")
        if (set(self.encoders) != set(self.models) or set(self.residency.processes) != set(self.models)
                or any(self.encoders[k] is not lease.encoder or self.residency.processes[k] is not lease.model
                       for k, lease in self.models.items())):
            raise ValueError("actual serving model objects changed")
        pack = self.store.qualified_identity()
        if pack != self.pack:
            raise ValueError("retrieval pack generation changed")
        models = {role: lease.observe() for role, lease in sorted(self.models.items())}
        return {"schema": "vkm-retrieval-native-identity/1", "status": "READY", "scope": "NATIVE_LOADED_IDENTITY",
                "code_sha256": self.code, "dependencies_sha256": self.dependencies,
                "pack": pack, "models": models, "functional_qualification": "NOT_RUN"}
