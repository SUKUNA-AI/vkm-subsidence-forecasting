"""Durable selector transactions with explicit receiver drain and crash recovery.

Adapters are registered by operator-owned code, never deserialized commands.
The persisted intent contains exact previous bindings before any mutation.
Physical power-loss and remote qualification remain separate runtime gates.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable, Literal

from pydantic import Field, model_validator

from vkm_corpus.parquet.atomic import create_exclusive, write_bytes, _fsync_dir
from vkm_corpus.update.barrier import BarrierUnavailable, ReceiverBarrier
from vkm_corpus.update.admission import (AdmissionState, AdmissionUnavailable, STATE_FILE,
                                          VERIFIED_PHASE, require_open)
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.generation import GenerationCoordinator, GenerationUnavailable
from vkm_evidence.contracts import Identifier, Sha256, StrictModel, canonical_bytes, record_hash

DRILL_CHECKS = {"admission_closed", "drained", "partial_apply_failed",
                "restored_exact_previous", "rollback_admission"}


def components_sha256(manifest):
    return record_hash([c.model_dump(mode="json") for c in sorted(manifest.components, key=lambda c: c.component)])


def services_sha256(manifest):
    return record_hash([s.model_dump(mode="json") for s in sorted(manifest.services, key=lambda s: s.service)])


class DeploymentProfile(StrictModel):
    scope: Literal["SYNTHETIC", "SHADOW_PRODUCTION", "PRODUCTION_SWITCH"]
    code_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    code_tree_sha256: Sha256
    dependencies_sha256: Sha256
    access_config_sha256: Sha256
    policy_sha256: Sha256
    deployment_profile_sha256: Sha256
    receiver_ids: tuple[Identifier, ...] = Field(min_length=1)
    adapter_ids: tuple[Identifier, ...] = Field(min_length=1)
    isolation_attestation_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _qualified(self):
        if len(set(self.receiver_ids)) != len(self.receiver_ids) or len(set(self.adapter_ids)) != len(self.adapter_ids):
            raise ValueError("duplicate deployment bindings")
        if self.scope == "SHADOW_PRODUCTION" and self.isolation_attestation_sha256 is None:
            raise ValueError("shadow endpoint isolation must be independently attested")
        return self


@dataclass(frozen=True)
class SelectorAdapter:
    adapter_id: str
    capture: Callable[[], dict]
    apply: Callable[[GenerationManifest, str], None]
    restore: Callable[[dict, str], None]


@dataclass(frozen=True)
class ReceiverControl:
    barrier: ReceiverBarrier
    rebind: Callable[[GenerationManifest], object]


class DurableDeployment:
    def __init__(self, root: Path, *, adapters: tuple[SelectorAdapter, ...],
                 receivers: tuple[ReceiverControl, ...], observer: Callable[[], dict],
                 identity_provider: Callable[[], DeploymentProfile],
                 qualification: Callable[[GenerationManifest], None],
                 candidate_probe: Callable[[GenerationManifest], None],
                 service_observer: Callable[[], dict] | None = None,
                 drain_timeout_seconds: float = 30, fault: Callable[[str], None] | None = None):
        self.root = Path(root).absolute()
        self.adapters = {a.adapter_id: a for a in adapters}
        self.receivers = {r.barrier.receiver_id: r for r in receivers}
        if len(self.adapters) != len(adapters) or len(self.receivers) != len(receivers):
            raise ValueError("duplicate registered deployment control")
        self.observer, self.identity_provider = observer, identity_provider
        self.service_observer = service_observer
        self.qualification, self.candidate_probe = qualification, candidate_probe
        if drain_timeout_seconds <= 0:
            raise ValueError("bounded drain deadline required")
        self.timeout, self.fault = drain_timeout_seconds, fault
        self._writer_fd = None
        self._gate_fd = None
        self._lock_watch = None
        for receiver in self.receivers.values():
            receiver.barrier.admission_root = self.root
        if os.name == "posix":
            for receiver in self.receivers.values():
                receiver.barrier.bind_gate(self.root / "admission.lock")
        self.coordinator = GenerationCoordinator(self.root)

    def _path(self, relative):
        path = self.root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise GenerationUnavailable("unsafe deployment path")
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise GenerationUnavailable("indirect deployment root or selector")
        return path

    @contextmanager
    def _writer(self):
        if self._writer_fd is not None:
            raise GenerationUnavailable("deployment writer is busy")
        self._path("writer.lock").parent.mkdir(parents=True, exist_ok=True)
        path = self._path("writer.lock")
        if os.name == "posix":
            try:
                created = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(created)
            except FileExistsError:
                pass
            # flock needs no write descriptor. CLOSE_WRITE on our own ordinary
            # reads would otherwise invalidate the immutable lock-file watch.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        else:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise GenerationUnavailable("deployment writer requires an ordinary file")
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (path.stat().st_dev, path.stat().st_ino):
                raise GenerationUnavailable("deployment writer inode changed")
            self._writer_fd = fd
            if self._profile().scope != "SYNTHETIC":
                if self._lock_watch is None:
                    from vkm_corpus.update.native_files import NativeFileWatch
                    self._lock_watch = NativeFileWatch((path, self._path("admission.lock")))
                self._lock_watch.check()
            yield
        except (BlockingIOError, PermissionError) as exc:
            raise GenerationUnavailable("deployment writer is busy") from exc
        finally:
            if self._writer_fd == fd:
                if self._gate_fd is not None:
                    os.close(self._gate_fd)
                    self._gate_fd = None
                self._writer_fd = None
            os.close(fd)  # the kernel also releases this lock after process death

    def _profile(self):
        profile = self.identity_provider()
        if not isinstance(profile, DeploymentProfile):
            raise GenerationUnavailable("typed actual deployment profile required")
        if set(profile.receiver_ids) != set(self.receivers) or set(profile.adapter_ids) != set(self.adapters):
            raise GenerationUnavailable("deployment omits a registered receiver or selector adapter")
        if profile.scope != "SYNTHETIC" and os.name != "posix":
            raise GenerationUnavailable("native deployment durability is not qualified on this OS")
        if self.fault is not None and profile.scope == "PRODUCTION_SWITCH":
            raise GenerationUnavailable("fault injection is forbidden for serving selectors")
        return profile

    def _read(self, relative):
        path = self._path(relative)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise GenerationUnavailable("deployment record exceeds bound")
        return json.loads(path.read_bytes())

    def _current(self):
        path = self._path("CURRENT")
        if not path.exists():
            return None
        return self.coordinator._load_head(path.read_text(encoding="ascii").strip())

    def _load_plan(self, key):
        plan = self._read("requests/" + key + ".json")
        expected = {"request_id", "candidate_sha256", "profile", "payload_sha256", "previous",
                    "bindings", "native_before_sha256", "plan_sha256"}
        if set(plan) != expected or self._request_key(plan["request_id"]) != key:
            raise GenerationUnavailable("deployment intent ownership differs")
        profile = DeploymentProfile.model_validate(plan["profile"])
        if (set(plan["bindings"]) != set(profile.adapter_ids)
                or record_hash({k: v for k, v in plan.items() if k != "plan_sha256"}) != plan["plan_sha256"]
                or record_hash({k: plan[k] for k in ("request_id", "candidate_sha256", "profile")}) != plan["payload_sha256"]):
            raise GenerationUnavailable("deployment intent bytes or native binding identities differ")
        if plan["previous"]:
            GenerationManifest.model_validate(plan["previous"])
        events = self.history(plan["request_id"])
        if events and (events[0]["phase"] != "PREPARED"
                       or events[0]["detail"].get("plan_sha256") != plan["plan_sha256"]):
            raise GenerationUnavailable("journal does not attest the original rollback intent")
        return plan

    def _request_key(self, request_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", request_id):
            raise ValueError("invalid deployment request identity")
        return record_hash(request_id)

    @staticmethod
    def _snapshot(value):
        if not isinstance(value, dict):
            raise GenerationUnavailable("selector adapter returned no native binding snapshot")
        data = canonical_bytes(value)
        if len(data) > 2 * 1024 * 1024:
            raise GenerationUnavailable("native selector snapshot exceeds bound")
        return json.loads(data)

    def _bindings(self):
        return {k: self._snapshot(a.capture()) for k, a in sorted(self.adapters.items())}

    def _native(self):
        return {"components": self.observer(),
                "services": self.service_observer() if self.service_observer else {}}

    def _verify(self, manifest, native=None):
        native = self._native() if native is None else native
        self.coordinator.verify(manifest, native["components"], native["services"])
        return native

    def plan(self, candidate: GenerationManifest, request_id: str):
        profile = self._profile()
        if candidate.code_commit != profile.code_commit or candidate.policy_sha256 != profile.policy_sha256:
            raise GenerationUnavailable("candidate code or policy differs from actual profile")
        self.qualification(candidate)
        key = self._request_key(request_id)
        payload = {"request_id": request_id, "candidate_sha256": candidate.sha256,
                   "profile": profile.model_dump(mode="json")}
        request = self._path("requests/" + key + ".json")
        if request.exists():
            old = self._load_plan(key)
            if old.get("payload_sha256") != record_hash(payload):
                raise GenerationUnavailable("request ID already names different deployment content")
            if not self.history(request_id):
                self._revalidate_previous(old)
            return old
        if self._path("MAINTENANCE").exists():
            raise GenerationUnavailable("another unresolved maintenance transaction exists")
        previous = self._current()
        observed = self._native()
        if previous:
            self._verify(previous, observed)
        elif observed["components"] or observed["services"]:
            raise GenerationUnavailable("initial deployment has unregistered serving components")
        body = {**payload, "payload_sha256": record_hash(payload),
                "previous": previous.model_dump(mode="json") if previous else None,
                "bindings": self._bindings(), "native_before_sha256": record_hash(observed)}
        return {**body, "plan_sha256": record_hash(body)}

    def _revalidate_previous(self, plan):
        current = self._current()
        expected = GenerationManifest.model_validate(plan["previous"]) if plan["previous"] else None
        if ((current.sha256 if current else None) != (expected.sha256 if expected else None)
                or record_hash(self._native()) != plan["native_before_sha256"]
                or self._bindings() != plan["bindings"]):
            raise GenerationUnavailable("orphan deployment intent has stale previous selectors")

    def _event(self, key, phase, *, detail=None):
        head = self._path("journals/" + key + ".head")
        parent = head.read_text(encoding="ascii").strip() if head.exists() else None
        body = {"schema": "vkm-deployment-event/1", "request_key": key,
                "parent_sha256": parent, "phase": phase, "detail": detail or {}}
        data = canonical_bytes(body)
        sha = hashlib.sha256(data).hexdigest()
        write_bytes(self._path("tmp"), self._path("journals/" + sha + ".json"), data)
        write_bytes(self._path("tmp"), head, (sha + "\n").encode("ascii"), overwrite=True)
        return sha

    def history(self, request_id):
        key = self._request_key(request_id)
        head = self._path("journals/" + key + ".head")
        current = head.read_text(encoding="ascii").strip() if head.exists() else None
        events, visited = [], set()
        while current:
            if not re.fullmatch("[0-9a-f]{64}", current) or current in visited or len(visited) >= 256:
                raise GenerationUnavailable("invalid deployment journal chain")
            data = self._path("journals/" + current + ".json").read_bytes()
            event = json.loads(data)
            if hashlib.sha256(data).hexdigest() != current or event.get("request_key") != key:
                raise GenerationUnavailable("deployment journal ownership or bytes differ")
            events.append({**event, "journal_sha256": current})
            visited.add(current)
            current = event["parent_sha256"]
        return list(reversed(events))

    def _pause(self, key):
        flag = self._path("MAINTENANCE")
        if flag.exists():
            if flag.read_text(encoding="ascii") != key:
                raise GenerationUnavailable("another transaction owns maintenance")
        elif not create_exclusive(flag, key):
            raise GenerationUnavailable("maintenance owner changed")
        _fsync_dir(flag.parent)
        self._publish_admission(AdmissionState(status="CLOSED", request_key=key))
        for receiver in self.receivers.values():
            receiver.barrier.pause(key)
        self._event(key, "ADMISSION_CLOSED")
        self._fault("ADMISSION_CLOSED")
        for receiver in self.receivers.values():
            receiver.barrier.drain(key, self.timeout)
        if os.name == "posix" and self._gate_fd is None:
            import fcntl
            path = self._path("admission.lock")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            deadline = time.monotonic() + self.timeout
            try:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise GenerationUnavailable("cross-process requests did not drain")
                        time.sleep(min(.01, remaining))
                if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (path.stat().st_dev, path.stat().st_ino):
                    raise GenerationUnavailable("cross-process admission gate changed")
                self._gate_fd = fd
            except BaseException:
                os.close(fd)
                raise
        self._event(key, "DRAINED")

    def writer_fence(self, request_id):
        """Native remote adapters call this immediately before selector mutations."""
        key = self._request_key(request_id)
        if self._writer_fd is None:
            raise GenerationUnavailable("selector mutation requires the live exclusive writer lease")
        if self._lock_watch is not None:
            self._lock_watch.check()
        if os.name == "posix" and self._gate_fd is None:
            raise GenerationUnavailable("cross-process admission gate is not drained")
        self._require_gate()
        current_stat = self._path("writer.lock").stat()
        owned = os.fstat(self._writer_fd)
        if (owned.st_dev, owned.st_ino) != (current_stat.st_dev, current_stat.st_ino):
            raise GenerationUnavailable("deployment writer inode changed")
        plan = self._load_plan(key)
        if self._profile().model_dump(mode="json") != plan["profile"]:
            raise GenerationUnavailable("actual code/dependencies/policy/profile changed")
        if self._path("MAINTENANCE").read_text(encoding="ascii") != key:
            raise GenerationUnavailable("maintenance lease lost")
        from vkm_corpus.update.admission import read_state
        admission = read_state(self.root)
        if admission.status != "CLOSED" or admission.request_key != key:
            raise GenerationUnavailable("durable admission closure lost")
        for receiver in self.receivers.values():
            state = receiver.barrier.status()
            if not state["paused"] or state["active_requests"]:
                raise GenerationUnavailable("receiver is not exclusively drained")

    def _require_gate(self):
        if os.name != "posix":
            return
        if self._gate_fd is None:
            raise GenerationUnavailable("cross-process admission gate is not drained")
        owned = os.fstat(self._gate_fd)
        current = self._path("admission.lock").stat(follow_symlinks=False)
        if (owned.st_dev, owned.st_ino) != (current.st_dev, current.st_ino):
            raise GenerationUnavailable("admission gate inode changed")
        if any(r.barrier._gate_identity != (owned.st_dev, owned.st_ino) for r in self.receivers.values()):
            raise GenerationUnavailable("receiver uses another admission gate")

    def _select_current(self, manifest):
        write_bytes(self._path("tmp"), self._path(manifest.sha256 + ".json"), canonical_bytes(manifest))
        write_bytes(self._path("tmp"), self._path("CURRENT"), (manifest.sha256 + "\n").encode("ascii"), overwrite=True)

    def _publish_admission(self, state):
        write_bytes(self._path("tmp"), self._path(STATE_FILE), canonical_bytes(state), overwrite=True)

    def _publication_fence(self, key, manifest):
        """Final approved identity fence after MAINTENANCE has been removed."""
        if self._writer_fd is None:
            raise GenerationUnavailable("admission publication requires the writer lease")
        self._require_gate()
        if self._lock_watch is not None:
            self._lock_watch.check()
        owned = os.fstat(self._writer_fd)
        current = self._path("writer.lock").stat(follow_symlinks=False)
        if (owned.st_dev, owned.st_ino) != (current.st_dev, current.st_ino):
            raise GenerationUnavailable("admission writer inode changed")
        plan = self._load_plan(key)
        if self._profile().model_dump(mode="json") != plan["profile"]:
            raise GenerationUnavailable("approved deployment profile changed before OPEN")
        previous = GenerationManifest.model_validate(plan["previous"]) if plan["previous"] else None
        if (manifest.sha256 not in {plan["candidate_sha256"], previous.sha256 if previous else None}
                or self._current() != manifest or self._path("MAINTENANCE").exists()):
            raise GenerationUnavailable("admission generation or maintenance boundary changed")
        from vkm_corpus.update.admission import read_state
        admission = read_state(self.root)
        if admission.status != "CLOSED" or admission.request_key != key:
            raise GenerationUnavailable("durable closure changed before admission")

    def _committed_open(self, key, manifest):
        try:
            state = require_open(self.root)
            expected = manifest if isinstance(manifest, str) else manifest.sha256
            return state.request_key == key and state.generation_sha256 == expected
        except AdmissionUnavailable:
            return False

    def _finish_committed(self, key, plan, candidate, events):
        # OPEN is the durable commit point. A lost ACK (including death before
        # FINISHED) is reconciliation, never another apply/restart/rollback.
        self.qualification(candidate)
        self._verify(candidate)
        self._resume_local(key)
        if events[-1]["phase"] != "FINISHED":
            self._event(key, "FINISHED")
        return self._result(key, plan, "PASS")

    def _resume_local(self, key):
        for receiver in self.receivers.values():
            if receiver.barrier._owner not in {None, key}:
                raise GenerationUnavailable("another owner paused the committed receiver")
            if receiver.barrier._owner == key:
                receiver.barrier.resume(key)

    def _open(self, key, manifest):
        self._require_gate()
        self._verify(manifest)
        flag = self._path("MAINTENANCE")
        if flag.read_text(encoding="ascii") != key:
            raise GenerationUnavailable("maintenance owner differs before rebind")
        # CURRENT must be observable for native startup/metadata proofs. Durable
        # CLOSED survives a controller death after MAINTENANCE is removed.
        flag.unlink()
        _fsync_dir(flag.parent)
        self._fault("MAINTENANCE_REMOVED")
        proofs = {}
        for receiver in self.receivers.values():
            proof = receiver.rebind(manifest)
            if self._profile().scope != "SYNTHETIC" and not isinstance(proof, dict):
                raise GenerationUnavailable("native receiver rebind omitted its proof")
            proofs[receiver.barrier.receiver_id] = record_hash(proof)
            self._fault("REBOUND:" + receiver.barrier.receiver_id)
        self._publication_fence(key, manifest)
        native = self._verify(manifest)
        verified = self._event(key, VERIFIED_PHASE, detail={"generation_sha256": manifest.sha256,
            "native_sha256": record_hash(native), "receiver_proofs": proofs})
        self._fault("RECEIVERS_VERIFIED")
        if record_hash(self._verify(manifest)) != record_hash(native):
            raise GenerationUnavailable("native serving changed before durable OPEN")
        self._publication_fence(key, manifest)
        self._publish_admission(AdmissionState(status="OPEN", request_key=key,
            generation_sha256=manifest.sha256, verified_event_sha256=verified))
        self._fault("ADMISSION_OPEN")
        for receiver in self.receivers.values():
            receiver.barrier.resume(key)

    def _restore(self, key, plan):
        self._pause(key)  # covers exceptions after any receivers resumed
        self.writer_fence(plan["request_id"])
        for name, adapter in reversed(list(sorted(self.adapters.items()))):
            self._event(key, "RESTORING", detail={"adapter_id": name})
            self.writer_fence(plan["request_id"])
            adapter.restore(plan["bindings"][name], plan["request_id"])
        previous = GenerationManifest.model_validate(plan["previous"]) if plan["previous"] else None
        if previous is None:
            self._event(key, "BOOTSTRAP_CLOSED")
            raise GenerationUnavailable("initial deployment failed; service remains closed")
        self._verify(previous)
        if self._bindings() != plan["bindings"]:
            raise GenerationUnavailable("restored selectors differ from exact previous bindings")
        self.writer_fence(plan["request_id"])
        self._select_current(previous)
        self._event(key, "RESTORED")
        self._open(key, previous)
        return previous

    def _fault(self, phase):
        if self.fault:
            self.fault(phase)

    def switch(self, candidate, request_id, confirmation):
        with self._writer():
            plan = self.plan(candidate, request_id)
            if confirmation != plan["plan_sha256"]:
                raise GenerationUnavailable("fresh deployment plan confirmation required")
            key = self._request_key(request_id)
            request = self._path("requests/" + key + ".json")
            events = self.history(request_id)
            if events:
                current = self._current()
                if current is not None and current.sha256 == candidate.sha256 and self._committed_open(key, candidate):
                    return self._finish_committed(key, plan, candidate, events)
                raise GenerationUnavailable("interrupted deployment requires explicit recovery plan")
            write_bytes(self._path("tmp"), request, canonical_bytes(plan))
            write_bytes(self._path("tmp"), self._path(candidate.sha256 + ".json"), canonical_bytes(candidate))
            self._revalidate_previous(plan)
            self._event(key, "PREPARED", detail={"plan_sha256": plan["plan_sha256"]})
            try:
                self._pause(key)
                self._fault("DRAINED")
                for name, adapter in sorted(self.adapters.items()):
                    self.writer_fence(request_id)
                    self._event(key, "APPLYING", detail={"adapter_id": name})
                    adapter.apply(candidate, request_id)
                    self._fault("APPLIED:" + name)  # native side effect can precede its ACK/event
                    self._event(key, "APPLIED", detail={"adapter_id": name})
                self._verify(candidate)
                self.candidate_probe(candidate)  # isolated probe, independent of MAINTENANCE
                self.writer_fence(request_id)
                self._event(key, "VERIFIED")
                self._select_current(candidate)
                self._fault("CURRENT_WRITTEN")
                self._event(key, "COMMITTED")
                self._open(key, candidate)
                self._event(key, "FINISHED")
            except Exception:
                if self._committed_open(key, candidate):
                    raise  # durable OPEN was committed; retry reconciles the ACK
                # A process kill/BaseException leaves the intent and maintenance
                # for recover(); a normal exception restores the exact snapshot.
                self._restore(key, plan)
                self._event(key, "FAILED_RESTORED")
                raise
            # This is after the durable commit. Failure to deliver the answer is
            # resolved by identical retry, never by rolling back served data.
            self._fault("ACK")
            return self._result(key, plan, "PASS")

    def recovery_plan(self, request_id, *, mode: Literal["RESTORE_PREVIOUS", "COMPLETE_CANDIDATE"]):
        key = self._request_key(request_id)
        plan = self._load_plan(key)
        events = self.history(request_id)
        if not events or events[-1]["phase"] in {"FINISHED", "FAILED_RESTORED", "RECOVERED_PREVIOUS"}:
            raise GenerationUnavailable("no interrupted deployment to recover")
        # RESTORE_PREVIOUS must not depend on readable candidate artifacts.
        # The trusted intent digest suffices to detect a committed candidate;
        # only COMPLETE_CANDIDATE loads that candidate manifest.
        if self._committed_open(key, plan["candidate_sha256"]):
            raise GenerationUnavailable("candidate is already admitted; retry the original switch to reconcile its ACK")
        if mode not in {"RESTORE_PREVIOUS", "COMPLETE_CANDIDATE"}:
            raise ValueError("explicit recovery mode required")
        body = {"request_id": request_id, "mode": mode, "journal_sha256": events[-1]["journal_sha256"],
                "profile": self._profile().model_dump(mode="json"), "bindings": self._bindings(),
                "plan_sha256": plan["plan_sha256"]}
        return {**body, "recovery_sha256": record_hash(body)}

    def recover(self, request_id, confirmation, *, mode="RESTORE_PREVIOUS"):
        with self._writer():
            key = self._request_key(request_id)
            plan = self._load_plan(key)
            events = self.history(request_id)
            terminal = "RECOVERED_PREVIOUS" if mode == "RESTORE_PREVIOUS" else "FINISHED"
            if (events and events[-1]["phase"] == terminal
                    and events[-1]["detail"].get("recovery_sha256") == confirmation
                    and events[-1]["detail"].get("mode") == mode):
                if self._profile().model_dump(mode="json") != plan["profile"]:
                    raise GenerationUnavailable("committed recovery profile changed")
                selected = (GenerationManifest.model_validate(plan["previous"]) if mode == "RESTORE_PREVIOUS"
                            else self.coordinator._load_head(plan["candidate_sha256"]))
                if not self._committed_open(key, selected):
                    raise GenerationUnavailable("committed recovery is no longer admitted")
                self._verify(selected)
                self._resume_local(key)
                result = self._result(key, plan, "RESTORED" if mode == "RESTORE_PREVIOUS" else "PASS")
                return {**result, "generation_sha256": selected.sha256}
            recovery = self.recovery_plan(request_id, mode=mode)
            if recovery["recovery_sha256"] != confirmation:
                raise GenerationUnavailable("fresh recovery confirmation required")
            if recovery["profile"] != plan["profile"]:
                raise GenerationUnavailable("deployment profile changed; automatic recovery is unqualified")
            if mode == "RESTORE_PREVIOUS":
                previous = GenerationManifest.model_validate(plan["previous"]) if plan["previous"] else None
                if previous is not None and self._committed_open(key, previous):
                    self._verify(previous)
                    self._resume_local(key)
                else:
                    previous = self._restore(key, plan)
                self._event(key, "RECOVERED_PREVIOUS", detail={"recovery_sha256": confirmation, "mode": mode})
                self._fault("RECOVERY_ACK")
                return {**self._result(key, plan, "RESTORED"), "generation_sha256": previous.sha256}
            candidate = self.coordinator._load_head(plan["candidate_sha256"])
            self._pause(key)
            self._verify(candidate)
            self.qualification(candidate)
            self.candidate_probe(candidate)
            self.writer_fence(request_id)
            self._select_current(candidate)
            self._event(key, "COMMITTED")
            self._open(key, candidate)
            self._event(key, "FINISHED", detail={"recovery_sha256": confirmation, "mode": mode})
            self._fault("RECOVERY_ACK")
            return self._result(key, plan, "PASS")

    def _result(self, key, plan, status):
        events = self.history(plan["request_id"])
        return {"schema": "vkm-deployment-result/1", "status": status,
                "scope": plan["profile"]["scope"], "request_id": plan["request_id"],
                "generation_sha256": plan["candidate_sha256"], "plan_sha256": plan["plan_sha256"],
                "journal_receipts": [e["journal_sha256"] for e in events]}

    def rehearse_failure(self, candidate, request_id, *, fail_after_adapter: str):
        profile = self._profile()
        if profile.scope == "PRODUCTION_SWITCH" or self.fault is not None:
            raise GenerationUnavailable("fault drill requires isolated selectors and no other injection")
        if fail_after_adapter not in self.adapters:
            raise ValueError("unknown drill adapter")
        plan = self.plan(candidate, request_id)
        if plan["previous"] is None:
            raise GenerationUnavailable("restore drill requires exact previous generation")
        original_leases = [r.barrier.acquire() for r in self.receivers.values()]
        def inject(phase):
            if phase == "ADMISSION_CLOSED":
                for receiver in self.receivers.values():
                    try:
                        leaked = receiver.barrier.acquire()
                    except BarrierUnavailable:
                        pass
                    else:
                        leaked.release()
                        raise GenerationUnavailable("paused receiver admitted a drill request")
                for lease in original_leases:
                    lease.release()
            if phase == "APPLIED:" + fail_after_adapter:
                self.writer_fence(request_id)
                native_partial = record_hash(self._native())
                if native_partial == plan["native_before_sha256"] and self._bindings() == plan["bindings"]:
                    raise GenerationUnavailable("drill adapter did not mutate an actual selector")
                self._event(self._request_key(request_id), "FAULT_INJECTED",
                            detail={"native_partial_sha256": native_partial,
                                    "bindings_partial_sha256": record_hash(self._bindings())})
                raise RuntimeError("isolated selector failure injection")
        self.fault = inject
        try:
            try:
                self.switch(candidate, request_id, plan["plan_sha256"])
            except RuntimeError:
                pass
            else:
                raise GenerationUnavailable("requested failure was not reached")
        finally:
            self.fault = None
            for lease in original_leases:
                lease.release()
        events = self.history(request_id)
        if events[-1]["phase"] != "FAILED_RESTORED" or "FAULT_INJECTED" not in {e["phase"] for e in events}:
            raise GenerationUnavailable("failed switch was not restored")
        native_after = record_hash(self._native())
        if native_after != plan["native_before_sha256"] or self._bindings() != plan["bindings"]:
            raise GenerationUnavailable("drill did not restore the exact previous generation")
        if any(r.barrier.status()["paused"] for r in self.receivers.values()):
            raise GenerationUnavailable("rollback receiver admission did not reopen")
        body = {"schema": "vkm-deployment-drill/1", "status": "PASS", **profile.model_dump(mode="json"),
                "candidate_components_sha256": components_sha256(candidate),
                "candidate_services_sha256": services_sha256(candidate),
                "previous_components_sha256": components_sha256(GenerationManifest.model_validate(plan["previous"])),
                "previous_services_sha256": services_sha256(GenerationManifest.model_validate(plan["previous"])),
                "native_before_sha256": plan["native_before_sha256"], "native_after_restore_sha256": native_after,
                "bindings_before_sha256": record_hash(plan["bindings"]),
                "bindings_after_restore_sha256": record_hash(self._bindings()),
                "transitions": [{"phase": e["phase"], "journal_sha256": e["journal_sha256"]} for e in events],
                "journal_receipts": [e["journal_sha256"] for e in events], "checks": dict.fromkeys(DRILL_CHECKS, "PASS")}
        data = canonical_bytes(body)
        sha = hashlib.sha256(data).hexdigest()
        write_bytes(self._path("tmp"), self._path("drills/" + sha + ".json"), data)
        return body
