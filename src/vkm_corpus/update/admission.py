"""Durable content admission, independent of the controller's kernel lease.

The trusted selector writer is the only publisher. Receivers check this record
under their shared gate lease; losing the writer process cannot turn CLOSED into
OPEN. This is an operational boundary, never scientific use admission.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Literal

from pydantic import Field, model_validator

from vkm_evidence.contracts import Identifier, Sha256, StrictModel

STATE_FILE = "ADMISSION.json"
VERIFIED_PHASE = "RECEIVERS_VERIFIED"


class AdmissionUnavailable(RuntimeError):
    pass


class AdmissionState(StrictModel):
    schema_version: Literal["vkm-durable-admission/1"] = "vkm-durable-admission/1"
    status: Literal["CLOSED", "OPEN"]
    request_key: Sha256
    generation_sha256: Sha256 | None = None
    verified_event_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _boundary(self):
        if self.status == "OPEN":
            if self.generation_sha256 is None or self.verified_event_sha256 is None:
                raise ValueError("OPEN requires a verified generation event")
        elif self.generation_sha256 is not None or self.verified_event_sha256 is not None:
            raise ValueError("CLOSED cannot attest an admitted generation")
        return self


class ReceiverVerification(StrictModel):
    generation_sha256: Sha256
    native_sha256: Sha256
    receiver_proofs: dict[Identifier, Sha256] = Field(min_length=1)


def _ordinary_bytes(path: Path, limit: int) -> bytes:
    """Fresh bounded, nonindirect bytes, including same-inode write detection."""
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise AdmissionUnavailable("indirect durable admission record")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit
                or (os.name == "posix" and before.st_mode & 0o022)):
            raise AdmissionUnavailable("unqualified durable admission record")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        # CPython/Windows fstat ChangeTime and path stat creation time differ.
        # Native deployment is Linux-only; portable synthetic checks compare
        # fd ChangeTime before/after, not two different Windows time concepts.
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                              s.st_ctime_ns if os.name == "posix" else None)
        if (any(p.is_symlink() for p in (path, *path.parents))
                or len(data) > limit or identity(before) != identity(after) or identity(after) != identity(current)
                or before.st_ctime_ns != after.st_ctime_ns):
            raise AdmissionUnavailable("durable admission bytes changed during inspection")
        return data
    finally:
        os.close(fd)


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AdmissionUnavailable("duplicate admission JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def read_state(root: Path) -> AdmissionState:
    try:
        return AdmissionState.model_validate(_json(_ordinary_bytes(Path(root) / STATE_FILE, 4096)))
    except (OSError, ValueError) as exc:
        raise AdmissionUnavailable("durable admission record is unavailable") from exc


def require_open(root: Path) -> AdmissionState:
    """Call while holding the shared admission gate, or the writer's EX lease."""
    root = Path(root)
    state = read_state(root)
    if state.status != "OPEN" or (root / "MAINTENANCE").exists():
        raise AdmissionUnavailable("durable receiver admission is closed")
    try:
        current = _ordinary_bytes(root / "CURRENT", 65).decode("ascii").strip()
        data = _ordinary_bytes(root / "journals" / (state.verified_event_sha256 + ".json"), 65536)
        event = _json(data)
        detail = ReceiverVerification.model_validate(event.get("detail"))
        if (current != state.generation_sha256
                or hashlib.sha256(data).hexdigest() != state.verified_event_sha256
                or event.get("schema") != "vkm-deployment-event/1"
                or event.get("request_key") != state.request_key
                or event.get("phase") != VERIFIED_PHASE
                or detail.generation_sha256 != current
                or set(event) != {"schema", "request_key", "parent_sha256", "phase", "detail"}):
            raise AdmissionUnavailable("durable admission does not attest CURRENT and verified receivers")
        if read_state(root) != state or (root / "MAINTENANCE").exists():
            raise AdmissionUnavailable("durable admission changed during inspection")
        return state
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise AdmissionUnavailable("durable admission proof is unavailable") from exc
