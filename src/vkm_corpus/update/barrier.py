"""Admission and drain of one explicitly bound receiver process.

The lease spans the complete ASGI response, including streaming. Production
receivers additionally hold the same shared kernel lock, so a replacement
process cannot enter during a selector transaction.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
import threading
import time
import uuid


class BarrierUnavailable(RuntimeError):
    pass


class RequestLease:
    def __init__(self, barrier: "ReceiverBarrier", token: str, fd: int | None = None):
        self._barrier, self._token, self._released = barrier, token, False
        self._fd = fd
        self._lock = threading.Lock()

    def release(self):
        with self._lock:
            if not self._released:
                self._barrier._same_process()
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                self._barrier._release(self._token)
                self._released = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class ReceiverBarrier:
    def __init__(self, receiver_id: str, *, gate_path: Path | None = None):
        if not receiver_id or len(receiver_id) > 120 or not all(c.isalnum() or c in "_-" for c in receiver_id):
            raise ValueError("invalid receiver identity")
        self.receiver_id, self.pid = receiver_id, os.getpid()
        self._condition = threading.Condition()
        self._active: set[str] = set()
        self._owner: str | None = None
        self.gate_path: Path | None = None
        self._gate_identity: tuple[int, int] | None = None
        if gate_path is not None:
            self.bind_gate(gate_path)

    def bind_gate(self, path: Path):
        """All production receivers share this kernel lock, including replacements."""
        self._same_process()
        if os.name != "posix":
            raise BarrierUnavailable("native cross-process admission requires POSIX flock")
        selected = Path(path).absolute()
        with self._condition:
            if self._active or (self.gate_path is not None and self.gate_path != selected):
                raise BarrierUnavailable("cannot replace a bound or active receiver gate")
            if any(p.is_symlink() for p in (selected, *selected.parents)):
                raise BarrierUnavailable("indirect admission gate")
            selected.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(selected, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                current = selected.stat(follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or identity != (current.st_dev, current.st_ino):
                    raise BarrierUnavailable("admission gate changed during binding")
                if self._gate_identity is not None and identity != self._gate_identity:
                    raise BarrierUnavailable("bound admission gate was replaced")
            finally:
                os.close(fd)
            self.gate_path = selected
            self._gate_identity = identity

    def _shared_gate(self):
        if self.gate_path is None:
            return None
        import fcntl
        path = self.gate_path
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise BarrierUnavailable("indirect admission gate")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if (not stat.S_ISREG(os.fstat(fd).st_mode)
                    or (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != self._gate_identity
                    or (path.stat().st_dev, path.stat().st_ino) != self._gate_identity):
                raise BarrierUnavailable("admission gate inode replaced")
            return fd
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise BarrierUnavailable("shared receiver gate is draining") from exc
        except BaseException:
            os.close(fd)
            raise

    def _same_process(self):
        if os.getpid() != self.pid:
            raise BarrierUnavailable("receiver barrier was inherited by another process")

    def acquire(self) -> RequestLease:
        self._same_process()
        with self._condition:
            if self._owner is not None:
                raise BarrierUnavailable("receiver admission is paused")
            fd = self._shared_gate()
            token = uuid.uuid4().hex
            self._active.add(token)
            return RequestLease(self, token, fd)

    def _release(self, token):
        self._same_process()
        with self._condition:
            if token not in self._active:
                raise BarrierUnavailable("unknown receiver request lease")
            self._active.remove(token)
            self._condition.notify_all()

    def pause(self, owner: str):
        self._same_process()
        if not owner:
            raise ValueError("maintenance owner required")
        with self._condition:
            if self._owner not in {None, owner}:
                raise BarrierUnavailable("another maintenance owner controls receiver")
            self._owner = owner

    def drain(self, owner: str, timeout_seconds: float):
        self._same_process()
        if timeout_seconds <= 0:
            raise ValueError("positive drain deadline required")
        end = time.monotonic() + timeout_seconds
        with self._condition:
            if self._owner != owner:
                raise BarrierUnavailable("receiver maintenance owner differs")
            while self._active:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise BarrierUnavailable("active receiver requests did not drain")
                self._condition.wait(remaining)

    def resume(self, owner: str):
        self._same_process()
        with self._condition:
            if self._owner != owner or self._active:
                raise BarrierUnavailable("receiver cannot resume under this lease")
            self._owner = None
            self._condition.notify_all()

    def status(self):
        self._same_process()
        with self._condition:
            gate_available = True
            if self.gate_path is not None:
                try:
                    fd = self._shared_gate()
                    os.close(fd)
                except (BarrierUnavailable, OSError):
                    gate_available = False
            return {"receiver_id": self.receiver_id, "pid": self.pid,
                    "scope": "CROSS_PROCESS" if self.gate_path is not None else "SINGLE_PROCESS",
                    "paused": self._owner is not None,
                    "admission_open": self._owner is None and gate_available,
                    "active_requests": len(self._active)}


class AdmissionBarrierMiddleware:
    """Pure ASGI middleware: call_next alone does not cover streamed bodies."""
    def __init__(self, app, *, provider):
        self.app, self.provider = app, provider

    async def __call__(self, scope, receive, send):
        barrier = self.provider()
        if scope["type"] != "http" or scope.get("path") in {"/v1/health", "/v1/status"} or barrier is None:
            await self.app(scope, receive, send)
            return
        try:
            lease = barrier.acquire()
        except BarrierUnavailable:
            from starlette.responses import JSONResponse
            from vkm_corpus.api.envelope import ApiError, ApiResponse, Meta
            request_id = uuid.uuid4().hex[:16]
            response = JSONResponse(ApiResponse(ok=False, meta=Meta(request_id=request_id),
                error=ApiError(code="DEPENDENCY_UNAVAILABLE", message="receiver is draining",
                               log_ref=request_id)).model_dump(mode="json"), status_code=503,
                headers={"X-Request-Id": request_id})
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            lease.release()
