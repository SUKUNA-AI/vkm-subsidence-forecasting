"""Code-owned model loading boundaries, never retrospective model attestation.

The text hook surrounds the *actual* loader and binds its serving runtime/model,
tokenizer and formatter. The child owner creates the inference child itself and
keeps the exact client, executable, process, listener and mapped resources bound.
It cannot attach to an existing process or qualify a nearby identity sidecar.

The current CareerOps and direct llama-server deployments do not call these
hooks. Their status is UNWIRED until the actual owner entrypoints are changed and
independently qualified. In particular, llama mtmd may copy mmproj tensors and
close/unmap its file: absent mmap evidence is CLOSED, never a hash/log fallback.
Mapped bytes do not prove GPU residency, functional parity or scientific admission.
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import logging
import os
import platform
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.native_files import NativeFileWatch
from vkm_corpus.update.owner_diagnostics import NULL as NULL_DIAGNOSTICS, classify
from vkm_corpus.update.remote_models import LoadedModelLease, NativeModelProof, own_process
from vkm_corpus.update.remote_retrieval import file_signature
from vkm_evidence.contracts import Sha256, StrictModel, record_hash

LOG = logging.getLogger(__name__)

class ModelOwnerStatus(StrictModel):
    schema_version: Literal["vkm-model-owner-status/1"] = "vkm-model-owner-status/1"
    kind: Literal["text", "visual"]
    state: Literal["UNWIRED", "BOUND", "CLOSED"]
    boundary: Literal["PYTHON_ACTUAL_RUNTIME", "OWNED_NATIVE_CHILD"]
    reason: str = Field(min_length=1, max_length=256)
    functional_qualification: Literal["NOT_RUN"] = "NOT_RUN"
    gpu_residency: Literal["NOT_PROVEN"] = "NOT_PROVEN"


class OwnedChildRecipe(StrictModel):
    """Actual owner configuration; paths are runtime-local, not exported evidence.

    This record is supplied by installed owner code. No executable, loader or
    command is selected from a corpus/generation manifest or an HTTP payload.
    Native hashes must cover every resource and loaded implementation file.
    """
    schema_version: Literal["vkm-owned-model-recipe/1"] = "vkm-owned-model-recipe/1"
    kind: Literal["visual"] = "visual"
    executable: str = Field(min_length=1, max_length=4096)
    arguments: tuple[str, ...] = Field(min_length=1, max_length=256)
    resources: dict[str, str]
    implementation_files: tuple[str, ...] = Field(min_length=1, max_length=256)
    expected_sha256: dict[str, Sha256]
    port: int = Field(ge=1, le=65535)
    startup_timeout_s: float = Field(default=180.0, gt=0, le=600)

    @model_validator(mode="after")
    def _inventory(self):
        if not {"weights", "tokenizer", "mmproj"} <= self.resources.keys():
            raise ValueError("owned visual model requires weights/tokenizer/mmproj")
        if len(self.resources) > 256 or any(not key or len(key) > 128 for key in self.resources):
            raise ValueError("bounded native resource roles required")
        paths = {self.executable, *self.resources.values(), *self.implementation_files}
        if any(not Path(p).is_absolute() for p in paths):
            raise ValueError("native owner requires explicit absolute runtime paths")
        if set(self.expected_sha256) != paths:
            raise ValueError("native owner hashes must cover the exact complete file inventory")
        if any(not value or "\0" in value or len(value) > 8192 for value in self.arguments):
            raise ValueError("bounded nonempty literal child arguments required")
        for aliases, expected in ((("--host",), "127.0.0.1"), (("--port",), str(self.port)),
                                 (("-m", "--model"), self.resources["weights"]),
                                 (("--mmproj",), self.resources["mmproj"])):
            positions = [i for i, arg in enumerate(self.arguments) if arg in aliases]
            if len(positions) != 1 or positions[0] + 1 >= len(self.arguments):
                raise ValueError("native child requires one explicit endpoint/model argument")
            if self.arguments[positions[0] + 1] != expected:
                raise ValueError("native child endpoint/model arguments differ from inventory")
        if any(a.startswith(("--host=", "--port=", "--model=", "--mmproj=")) for a in self.arguments):
            raise ValueError("alternate endpoint/model flags are forbidden")
        return self


class OwnedChildIdentity(StrictModel):
    schema_version: Literal["vkm-owned-model-identity/1"] = "vkm-owned-model-identity/1"
    model: NativeModelProof
    boundary: Literal["OWNED_LOAD_TIME_MMAP_AND_SOCKET"] = "OWNED_LOAD_TIME_MMAP_AND_SOCKET"
    recipe_sha256: Sha256
    process_sha256: Sha256
    functional_qualification: Literal["NOT_RUN"] = "NOT_RUN"
    gpu_residency: Literal["NOT_PROVEN"] = "NOT_PROVEN"


def unwired_owner(kind: Literal["text", "visual"]) -> ModelOwnerStatus:
    """Honest status of the current external entrypoints, not an identity proof."""
    return ModelOwnerStatus(kind=kind, state="UNWIRED",
        boundary="PYTHON_ACTUAL_RUNTIME" if kind == "text" else "OWNED_NATIVE_CHILD",
        reason="ACTUAL_TEXT_LOADER_NOT_HOOKED" if kind == "text" else "ACTUAL_LLAMA_LOAD_HOOK_NOT_BOUND")


class TextRuntimeOwner:
    """Hook for CareerOps JinaRerankerRuntime.load, in its actual serving process.

    runtime_loader must load from a complete immutable local resources/code
    inventory. It must not download or follow a mutable HF cache alias. The
    runtime install/getter must be those used by HTTP inference. Calling this
    after an already loaded runtime is not a supported integration.
    """
    @classmethod
    def load(cls, *, resources, implementation_files, dependency_packages, config,
             runtime_loader, serving_runtime_getter, install_runtime):
        obj = cls()
        obj._pending = None
        obj._invalid = False
        obj._closed = False
        obj._lock = threading.RLock()
        if serving_runtime_getter() is not None:
            raise ValueError("text load hook cannot attest an already installed serving runtime")

        def load_model():
            runtime = runtime_loader()
            model = getattr(runtime, "_model", None)
            tokenizer = getattr(runtime, "_tokenizer", None)
            formatter = getattr(runtime, "_prompt_formatter", None)
            if (model is None or tokenizer is None or not callable(formatter)
                    or getattr(model, "_tokenizer", None) is not tokenizer
                    or not callable(getattr(model, "rerank", None))):
                raise ValueError("actual text runtime/model/tokenizer/formatter not bound")
            inventory = {str(Path(path).absolute()) for path in implementation_files}
            runtime_module = importlib.import_module(type(runtime).__module__)
            runtime_source = getattr(runtime_module, "__file__", None)
            formatter_code = getattr(formatter, "__code__", None)
            if (runtime_source is None or str(Path(runtime_source).absolute()) not in inventory
                    or formatter_code is None or str(Path(formatter_code.co_filename).absolute()) not in inventory):
                raise ValueError("actual text runtime/formatter code absent from load-time inventory")
            obj._pending = runtime
            return model

        def install_model(model):
            runtime = obj._pending
            if runtime is None or runtime._model is not model:
                raise ValueError("text owner install does not address the loaded runtime")
            install_runtime(runtime)

        def serving_model():
            runtime = serving_runtime_getter()
            return getattr(runtime, "_model", None)

        obj.lease = LoadedModelLease.load(kind="text", resources=resources,
            implementation_files=implementation_files, dependency_packages=dependency_packages,
            config=config, loader=load_model, serving_getter=serving_model, install=install_model)
        obj.runtime = obj._pending
        obj.getter = serving_runtime_getter
        obj.tokenizer, obj.formatter = obj.runtime._tokenizer, obj.runtime._prompt_formatter
        obj._pending = None
        obj.observe()
        return obj

    def observe(self) -> NativeModelProof:
        with self._lock:
            if self._closed or self._invalid:
                raise ValueError("text model owner is closed or permanently invalid")
            try:
                runtime = self.getter()
                if (runtime is not self.runtime or runtime._tokenizer is not self.tokenizer
                        or runtime._prompt_formatter is not self.formatter
                        or getattr(runtime._model, "_tokenizer", None) is not self.tokenizer):
                    raise ValueError("actual text serving runtime/tokenizer/formatter changed")
                return self.lease.observe()
            except (OSError, ValueError):
                self._invalid = True
                raise

    @contextlib.contextmanager
    def serving(self):
        """Use around the owner's actual inference path; check both boundaries."""
        with self._lock:
            self.observe()
            try:
                yield self.runtime
            finally:
                self.observe()

    def close(self):
        with self._lock:
            self._closed = True
            self.lease.watch.close()


def _pid_record(pid: int) -> dict:
    root = Path("/proc") / str(pid)
    fields = (root / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    return {"pid": pid, "parent_pid": int(fields[1]), "start_ticks": int(fields[19]),
            "state": fields[0], "command": (root / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")}


def _mapped_resources(pid: int, resources: dict[str, str], signatures: dict):
    """No logs, file handles alone, or current digests substitute for mappings."""
    matches = set()
    for line in (Path("/proc") / str(pid) / "maps").read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) != 6:
            continue
        for role in ("weights", "mmproj"):
            path = resources[role]
            if parts[5] != path:
                continue
            major, minor = (int(x, 16) for x in parts[3].split(":"))
            signature = signatures[path]
            if (int(parts[4]) == signature[1]
                    and (major, minor) == (os.major(signature[0]), os.minor(signature[0]))
                    and "r" in parts[1]):
                matches.add(role)
    if matches != {"weights", "mmproj"}:
        raise ValueError("actual child weights/mmproj mmap identity unavailable; native load hook required")


def _listener_record(pid: int, port: int) -> dict:
    root = Path("/proc") / str(pid)
    if (root / "ns/net").stat().st_ino != Path("/proc/self/ns/net").stat().st_ino:
        raise ValueError("owned inference child network namespace differs")
    sockets = set()
    for fd in (root / "fd").iterdir():
        try:
            link = os.readlink(fd)
        except FileNotFoundError:
            continue
        if link.startswith("socket:["):
            sockets.add(link[8:-1])
    address = f"0100007F:{port:04X}"
    listeners = [row[9] for row in (line.split() for line in (root / "net/tcp").read_text().splitlines()[1:])
                 if len(row) > 9 and row[1] == address and row[3] == "0A" and row[9] in sockets]
    if len(listeners) != 1:
        raise ValueError("actual inference loopback socket is not uniquely owned by observed child")
    return {"network_namespace": (root / "ns/net").stat().st_ino, "socket_inode": listeners[0],
            "address": "127.0.0.1", "port": port}


class OwnedChildModelOwner:
    """Parent owns a new native child and the exact inference client for its life.

    There is deliberately no attach(pid), readyz-proof or load-time receipt
    import API. A replacement must go through a new bounded start, watch, proof
    and serving qualification. Do not expose start/stop to agents or HTTP.
    """
    @classmethod
    def start(cls, recipe: OwnedChildRecipe, *, inference_client, serving_client_getter,
              environment: dict[str, str], diagnostics=None, cancel=None):
        if platform.system() != "Linux":
            raise ValueError("native child owner requires Linux procfs and local mutation watches")
        diag = NULL_DIAGNOSTICS if diagnostics is None else diagnostics

        def cancelled():
            # Explicit stop request (e.g. SIGTERM flag): a signal that lands in a
            # blocked socket read is otherwise converted into a retried fence error.
            if cancel is not None and cancel():
                diag.fence_failure(None, "InterruptedError")
                raise InterruptedError("owner startup cancelled")
        obj = cls()
        obj.recipe = recipe.model_copy(deep=True)
        obj.recipe_hash = record_hash(obj.recipe)
        obj.client, obj.getter = inference_client, serving_client_getter
        obj.owner_process = own_process()
        obj._lock = threading.RLock()
        obj.proc = None
        obj._closed = False
        obj._invalid = False
        obj._watch = None
        # Closed diagnosis cursor: which startup-proof fence step failed and the
        # allowlisted class of its inner cause. Never exception text or paths.
        obj._fence_step = None
        obj._fence_cause = None
        obj._witness_status = None
        obj._diagnostics = diagnostics
        obj._client_fence()
        files = sorted(recipe.expected_sha256)
        try:
            diag.enter("FILE_INVENTORY")
            obj._watch = NativeFileWatch(files)
            obj.signatures = {path: file_signature(path) for path in files}
            if any(Path(path).stat(follow_symlinks=False).st_nlink != 1 for path in files):
                raise ValueError("native owner resource hardlink aliases forbidden")
            obj.hashes = {path: sha256_of(Path(path)) for path in files}
            if obj.hashes != recipe.expected_sha256:
                raise ValueError("native owner file bytes differ from exact recipe")
            obj._file_fence()
            obj.environment = dict(environment)
            if any(not isinstance(k, str) or not isinstance(v, str) or "\0" in k + v
                   for k, v in obj.environment.items()):
                raise ValueError("explicit bounded child environment required")
            if len(obj.environment) > 256 or sum(len(k) + len(v) for k, v in obj.environment.items()) > 65536:
                raise ValueError("native child environment exceeds limit")
            # The configured identity excludes any per-lifetime credential a hook
            # adds at spawn (e.g. the witness token); that lives in the process.
            obj.configured_environment = dict(obj.environment)
            # No inherited process environment, shell, executable fallback or restart.
            diag.enter("SPAWN")
            obj.proc = obj._spawn_child()
            obj.pid = obj.proc.pid
            diag.spawned()
            diag.enter("NATIVE_LOAD_PROOF")
            deadline = time.monotonic() + recipe.startup_timeout_s
            last_error = None
            while time.monotonic() < deadline:
                obj._fence_step = obj._fence_cause = obj._witness_status = None
                cancelled()
                try:
                    obj.process = obj._process_fence()
                    obj._fence_step = "FILE_LEASE"
                    obj._file_fence()
                    obj._fence_step = "CLIENT_FENCE"
                    obj._client_fence()
                    break
                except (ValueError, FileNotFoundError, ProcessLookupError) as exc:
                    last_error = exc
                    diag.fence_failure(obj._fence_step, obj._fence_cause or classify(exc),
                                       http_status=obj._witness_status)
                    code = obj.proc.poll()
                    if code is not None:
                        diag.child_exit(code)
                        raise ValueError("owned model child exited during loading") from exc
                    obj._fence_step = None
                    cancelled()
                    try:
                        time.sleep(min(0.025, max(0, deadline - time.monotonic())))
                    except BaseException as interrupted:  # signal during the pause: no fence step
                        diag.fence_failure(None, interrupted)
                        raise
                except BaseException as exc:
                    # Not retried: attribute the fatal class to the step it came from.
                    diag.fence_failure(obj._fence_step, obj._fence_cause or classify(exc),
                                       http_status=obj._witness_status)
                    raise
            else:
                code = obj.proc.poll()
                diag.deadline(child_alive=code is None, exit_code=code)
                raise ValueError("bounded native child load proof unavailable") from last_error
            diag.enter("PROOF_IDENTITY")
            obj.proof = NativeModelProof(kind="visual", instance_sha256=record_hash(obj.process),
                code_sha256=record_hash({path: obj.hashes[path] for path in
                    sorted({recipe.executable, *recipe.implementation_files})}),
                dependencies_sha256=record_hash({"python": platform.python_version(),
                    "pydantic": importlib.metadata.version("pydantic"),
                    "native_implementation": [obj.hashes[path] for path in recipe.implementation_files]}),
                config_sha256=record_hash({"recipe": obj.recipe_hash, "environment": obj.configured_environment}),
                resources={role: obj.hashes[path] for role, path in recipe.resources.items()})
            obj.observe()
            return obj
        except BaseException:
            # A secondary cleanup failure is recorded, never raised over the cause.
            try:
                obj.close()
            except BaseException as cleanup:
                diag.cleanup_failure("OWNER_CLOSE", cleanup)
                # Fixed text and allowlisted class only, also without a recorder.
                LOG.error("owned_model_child_cleanup_failed %s", classify(cleanup))
            raise

    def _spawn_child(self):
        return subprocess.Popen([self.recipe.executable, *self.recipe.arguments], env=self.environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)

    def _loaded_resource_fence(self):
        # Default boundary remains actual mmap; only an explicit hook subclass
        # can use the native load lifecycle protocol instead.
        self._fence_step = "MAPPED_RESOURCES"
        _mapped_resources(self.pid, self.recipe.resources, self.signatures)

    def _client_fence(self):
        if self.getter() is not self.client:
            raise ValueError("observed inference client is not the actual serving client")
        parsed = urlsplit(str(getattr(self.client, "base_url", "")))
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != self.recipe.port
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("actual inference client does not address the owned child endpoint")
        if getattr(self.client, "is_closed", False):
            raise ValueError("actual inference client is closed")

    def _file_fence(self):
        self._watch.check()
        if (record_hash(self.recipe) != self.recipe_hash or own_process() != self.owner_process
                or any(file_signature(path) != before for path, before in self.signatures.items())):
            raise ValueError("native owner resources/configuration/parent changed")

    def _process_fence(self):
        self._fence_step = "CHILD_PROCESS"
        if self.proc is None or self.proc.pid != self.pid or self.proc.poll() is not None:
            raise ValueError("owned inference child died or was replaced")
        record = _pid_record(self.pid)
        if (record["parent_pid"] != os.getpid() or record["state"] in {"Z", "X"}
                or record["command"] != [os.fsencode(self.recipe.executable),
                    *(os.fsencode(a) for a in self.recipe.arguments)]):
            raise ValueError("actual child ownership/command differs from load recipe")
        self._fence_step = "CHILD_EXECUTABLE"
        executable = Path("/proc") / str(self.pid) / "exe"
        expected = self.signatures[self.recipe.executable]
        observed = executable.stat()
        if (executable.resolve() != Path(self.recipe.executable)
                or (observed.st_dev, observed.st_ino) != expected[:2]):
            raise ValueError("actual child executable identity differs from load inventory")
        self._fence_step = "LISTENER"
        listener = _listener_record(self.pid, self.recipe.port)
        # Prove the recipient before a hook sends its private challenge.
        self._loaded_resource_fence()
        self._fence_step = "PROCESS_STABILITY"
        after = _pid_record(self.pid)
        if after != record:
            # R/S state changes are ordinary scheduler activity, not identity.
            if {k: v for k, v in after.items() if k != "state"} != {k: v for k, v in record.items() if k != "state"}:
                raise ValueError("owned child process changed during proof")
        return {"pid": record["pid"], "parent_pid": record["parent_pid"],
                "start_ticks": record["start_ticks"], "command_sha256": record_hash(self.recipe.arguments),
                "executable_sha256": self.hashes[self.recipe.executable], "listener": listener}

    def observe(self) -> OwnedChildIdentity:
        with self._lock:
            if self._closed or self._invalid:
                raise ValueError("native model owner is closed or permanently invalid")
            try:
                self._file_fence()
                self._client_fence()
                if self._process_fence() != self.process:
                    raise ValueError("owned native model process/listener changed")
                self._file_fence()
                return OwnedChildIdentity(model=self.proof.model_copy(deep=True),
                    recipe_sha256=self.recipe_hash, process_sha256=record_hash(self.process))
            except (OSError, ValueError):
                self._invalid = True
                raise

    @contextlib.contextmanager
    def serving(self):
        """The actual owner must use this around the bound client's inference."""
        with self._lock:
            self.observe()
            try:
                yield self.client
            finally:
                self.observe()

    def close(self):
        self._closed = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            # Only our direct child/session is signalled, never a supplied PID.
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2)
        if self._watch is not None:
            self._watch.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
