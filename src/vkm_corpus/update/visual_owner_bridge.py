"""Owned patched llama child, live load witness and its actual serving proxy.

No attach, sidecar, retrospective resource hash or GPU-residency claim. The
ordinary mmap owner remains separate. Synthetic children never export a native
production identity or open the bridge's external listener.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import logging
import os
import secrets
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, StrictInt, model_validator

from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.model_owner import OwnedChildModelOwner, OwnedChildRecipe
from vkm_corpus.update.native_files import NativeFileWatch
from vkm_corpus.update.owner_diagnostics import NULL as NULL_DIAGNOSTICS, StartupDiagnostics, classify
from vkm_corpus.update.remote_models import NativeModelProof
from vkm_corpus.update.remote_retrieval import file_signature
from vkm_corpus.update.service_identity import runtime_dependency_inventory
from vkm_corpus.update.visual_placement import OwnedTargetWeightPlacement, TargetWeightPlacement
from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash

UPSTREAM = "4da6337767f973e2b4d0797e5b323d77d8565e4a"
WITNESS_HEADER = "X-VKM-Witness-Authorization"
WITNESS_ENV = "VKM_OWNED_WITNESS_TOKEN"
WITNESS_LIMIT = 512 * 1024
LOG = logging.getLogger(__name__)
# Fixed client/native capabilities. No manifest-selected module/import list.
BRIDGE_MODULES = ("vkm_corpus", "vkm_corpus.update", "vkm_corpus.parquet", "vkm_corpus.contracts",
    "vkm_corpus.retrieval", "vkm_evidence", "vkm_world", "vkm_world.core",
    "vkm_corpus.update.visual_owner_bridge", "vkm_corpus.update.visual_placement", "vkm_corpus.update.model_owner",
    "vkm_corpus.update.remote_models", "vkm_corpus.update.remote_retrieval",
    "vkm_corpus.update.native_files", "vkm_corpus.update.service_identity",
    "vkm_corpus.update.owner_diagnostics",
    "vkm_corpus.parquet.atomic", "vkm_evidence.contracts", "vkm_corpus.contracts.access", "vkm_corpus.contracts.access_vocab",
    "vkm_corpus.contracts.vocab",
    "vkm_world.core.provenance", "vkm_corpus.retrieval.pins")
DEPENDENCIES = (("httpx>=0.28,<1", "httpx"), ("pydantic>=2.13.5,<3", "pydantic"),
                ("packaging>=26.3,<27", "packaging"))
ROUTES = {("GET", "/health"), ("GET", "/props"), ("POST", "/embedding"),
          ("POST", "/embeddings"), ("POST", "/v1/embeddings"), ("POST", "/tokenize")}


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError("duplicate owner protocol field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite owner field")))


def _read(path, limit):
    path = Path(path)
    before = file_signature(path)
    if before[2] > limit or path.stat().st_nlink != 1:
        raise ValueError("owner input is oversized or indirect")
    with path.open("rb") as stream: raw = stream.read(limit + 1)
    if len(raw) > limit or file_signature(path) != before:
        raise ValueError("owner input changed while reading")
    return raw


class HookedChildRecipe(OwnedChildRecipe):
    schema_version: Literal["vkm-owned-hook-child-recipe/1"] = "vkm-owned-hook-child-recipe/1"
    strategy: Literal["NATIVE_LOADED_WITNESS_V1"]
    scope: Literal["VISUAL_OWNER_PRODUCTION", "SYNTHETIC"]
    upstream_commit: Literal[UPSTREAM] = UPSTREAM

    @model_validator(mode="after")
    def _native(self):
        if self.resources["tokenizer"] != self.resources["weights"]:
            raise ValueError("native llama tokenizer is the actual GGUF vocab, not gateway tokenizer.json")
        if self.scope == "VISUAL_OWNER_PRODUCTION":
            from vkm_corpus.retrieval.pins import VISUAL
            if (self.expected_sha256[self.resources["weights"]] != VISUAL["weights_sha256"]
                    or self.expected_sha256[self.resources["mmproj"]] != VISUAL["mmproj_sha256"]
                    or Path(self.executable).name != "llama-server"):
                raise ValueError("production hook must preserve the pinned visual model")
            # The current owned profile, not an arbitrary launcher/router/LoRA.
            valued = {"-m", "--model", "--mmproj", "--host", "--port", "-ngl", "--fit", "-fa",
                "-c", "-np", "-b", "-ub", "--image-min-tokens", "--image-max-tokens",
                "--cache-ram", "-t", "--pooling"}
            flags = {"--embeddings", "--no-warmup", "--no-webui", "--metrics"}
            options, i = {}, 0
            while i < len(self.arguments):
                flag = self.arguments[i]
                if flag not in valued | flags or flag in options:
                    raise ValueError("unsupported or duplicate native owner argument")
                if flag in valued:
                    if i + 1 >= len(self.arguments): raise ValueError("missing native option value")
                    options[flag] = self.arguments[i + 1]; i += 2
                else: options[flag] = True; i += 1
            if options.get("--embeddings") is not True or options.get("--pooling") != "last":
                raise ValueError("visual owner preserves embeddings/last pooling")
        return self


class LoadedWitness(StrictModel):
    schema_version: Literal["vkm-native-loaded-witness/1"]
    upstream_commit: Literal[UPSTREAM]
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    epoch: int = Field(strict=True, ge=1, le=2**64-1)
    capture_before_load: bool = Field(strict=True)
    handles: dict[str, StrictInt]
    paths: dict[str, str]
    target_weight_placement: TargetWeightPlacement | None = None

    @model_validator(mode="after")
    def _complete(self):
        if (not self.capture_before_load or set(self.handles) != {"model", "context", "mmproj", "vocab"}
                or any(type(v) is not int or not 0 < v < 2**64 for v in self.handles.values())
                or set(self.paths) != {"weights", "tokenizer", "mmproj"}
                or any(not Path(p).is_absolute() or len(p) > 4096 for p in self.paths.values())):
            raise ValueError("incomplete live native handles/paths")
        if self.target_weight_placement is not None and (
                self.target_weight_placement.model_handle != self.handles["model"]
                or self.target_weight_placement.context_handle != self.handles["context"]):
            raise ValueError("target-weight placement belongs to another native lifetime")
        return self


class HookedChildIdentity(StrictModel):
    schema_version: Literal["vkm-owned-hook-child-identity/1"] = "vkm-owned-hook-child-identity/1"
    scope: Literal["VISUAL_OWNER_PRODUCTION", "SYNTHETIC"]
    model: NativeModelProof
    boundary: Literal["OWNED_NATIVE_LOAD_WITNESS_AND_SOCKET"] = "OWNED_NATIVE_LOAD_WITNESS_AND_SOCKET"
    recipe_sha256: Sha256
    process_sha256: Sha256
    witness_sha256: Sha256
    functional_qualification: Literal["NOT_RUN"] = "NOT_RUN"
    gpu_residency: Literal["NOT_PROVEN"] = "NOT_PROVEN"


def _response_bytes(response, limit):
    if response.headers.get("Content-Encoding", "identity") != "identity":
        raise ValueError("encoded native response is unsupported")
    body = bytearray()
    for chunk in response.iter_raw():
        body.extend(chunk)
        if len(body) > limit: raise ValueError("native response exceeds bound")
    return bytes(body)


WITNESS_GETTER_UNAVAILABLE = b'{"error":"loaded_witness_unavailable"}'


def _unavailable_kind(raw):
    """Closed class of a witness 503: owner getter vs server still loading.

    Matches only two fixed native bodies; anything else stays unclassified.
    The body is never stored or exported."""
    if raw == WITNESS_GETTER_UNAVAILABLE:
        return "WITNESS_GETTER_UNAVAILABLE"
    try:
        error = _json(raw).get("error")
        if isinstance(error, dict) and error.get("message") == "Loading model" and error.get("code") == 503:
            return "WITNESS_SERVER_LOADING"
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _mapped_implementation(pid):
    """The owned executable's actual mapped runtime, not a declared library list."""
    paths = set()
    raw = _read(Path("/proc") / str(pid) / "maps", 8*1024*1024)
    for line in raw.decode("utf-8").splitlines():
        row = line.split(maxsplit=5)
        if len(row) != 6 or not row[5].startswith("/"): continue
        path = row[5]
        if "x" in row[1] or ".so" in Path(path).name:
            if path.endswith(" (deleted)"): raise ValueError("native implementation mapping deleted")
            paths.add(path)
    if not paths: raise ValueError("actual mapped native implementation unavailable")
    return paths


class OwnedHookChildOwner(OwnedChildModelOwner):
    @classmethod
    def start(cls, recipe: HookedChildRecipe, **kwargs):
        if type(recipe) is not HookedChildRecipe:
            raise ValueError("explicit native hook recipe required; no mmap downgrade")
        if WITNESS_ENV in kwargs.get("environment", {}):
            raise ValueError("witness token must be generated by the owning parent")
        return super().start(recipe, **kwargs)

    def _spawn_child(self):
        self._witness_token = secrets.token_hex(32)
        self.environment[WITNESS_ENV] = self._witness_token
        self.client.headers[WITNESS_HEADER] = "Bearer " + self._witness_token
        self._client_headers = dict(self.client.headers)
        self._client_transport = self.client._transport
        self._environment_sha = record_hash(self.environment)
        self._witness = None
        self._client_fence()
        return super()._spawn_child()

    def _client_fence(self):
        super()._client_fence()
        if (type(self.client) is not httpx.Client or self.client.follow_redirects
                or self.client.trust_env or type(self.client._transport) is not httpx.HTTPTransport
                or self.client.event_hooks.get("request") or self.client.event_hooks.get("response")
                or self.client._mounts):
            raise ValueError("native hook requires the actual direct bounded HTTP client")
        if hasattr(self, "_client_headers") and (dict(self.client.headers) != self._client_headers
                or self.client._transport is not self._client_transport
                or record_hash(self.environment) != self._environment_sha):
            raise ValueError("owned client authorization/transport/environment changed")

    def _loaded_resource_fence(self):
        if self.recipe.scope == "VISUAL_OWNER_PRODUCTION":
            self._fence_step = "MAPPED_IMPLEMENTATION"
            mapped = _mapped_implementation(self.pid)
            outside = mapped - {self.recipe.executable, *self.recipe.implementation_files}
            if outside:
                # Basenames only, for the private diagnostic receipt.
                record = getattr(self, "_diagnostics", None)
                if record is not None: record.unexpected_mapping(Path(p).name for p in outside)
                raise ValueError("actual native loaded library absent from pre-spawn inventory")
        nonce = secrets.token_hex(32)
        self._fence_step = "WITNESS_HTTP"
        try:
            with self.client.stream("GET", "/__vkm_loaded_witness", params={"nonce": nonce}, timeout=1.0) as response:
                if response.status_code != 200:
                    self._fence_step, self._witness_status = "WITNESS_STATUS", response.status_code
                    try:
                        kind = _unavailable_kind(_response_bytes(response, 4096))
                    except (httpx.HTTPError, ValueError):
                        kind = None
                    if kind is not None: self._fence_step = kind
                    raise ValueError("loaded native witness unavailable")
                self._fence_step = "WITNESS_BODY"
                raw = _response_bytes(response, WITNESS_LIMIT)
                self._fence_step = "WITNESS_SCHEMA"
                witness = LoadedWitness.model_validate(_json(raw))
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            self._fence_cause = classify(exc)
            raise ValueError("loaded native witness unavailable") from None
        self._fence_step = "WITNESS_BINDING"
        if (witness.nonce != nonce or witness.epoch != 1 or witness.paths !=
                {role: self.recipe.resources[role] for role in ("weights", "tokenizer", "mmproj")}):
            raise ValueError("native load epoch/resource/challenge differs")
        self._fence_step = "PLACEMENT_PRESENT"
        if self.recipe.scope == "VISUAL_OWNER_PRODUCTION" and witness.target_weight_placement is None:
            raise ValueError("actual target-weight placement unavailable")
        self._fence_step = "WITNESS_LIFETIME"
        core = witness.model_dump(exclude={"nonce"})
        if self._witness is None: self._witness = core
        elif core != self._witness: raise ValueError("native loaded handles/lifetime changed")

    def observe(self):
        with self._lock:
            if self._closed or self._invalid: raise ValueError("native hook owner is closed or permanently invalid")
            try:
                self._file_fence(); self._client_fence()
                if self._process_fence() != self.process:
                    raise ValueError("owned native child process/listener changed")
                self._file_fence()
                proof = self.proof.model_copy(update={"tokenizer_binding": "EMBEDDED_WEIGHTS_VOCAB", "instance_sha256": record_hash({
                    "process": self.process, "witness": self._witness})}, deep=True)
                return HookedChildIdentity(scope=self.recipe.scope, model=proof,
                    recipe_sha256=self.recipe_hash, process_sha256=record_hash(self.process),
                    witness_sha256=record_hash(self._witness))
            except (OSError, ValueError):
                self._invalid = True
                raise


class VisualOwnerRecipe(StrictModel):
    schema_version: Literal["vkm-visual-owner-bridge/1"]
    child: HookedChildRecipe
    environment: dict[str, str]
    identity_token_path: str
    identity_token_sha256: Sha256
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    request_timeout_s: float = Field(default=60, gt=0, le=180)
    max_request_bytes: int = Field(default=16*1024*1024, ge=1024, le=64*1024*1024)
    max_response_bytes: int = Field(default=16*1024*1024, ge=1024, le=64*1024*1024)
    max_clients: int = Field(default=4, ge=1, le=16)

    @model_validator(mode="after")
    def _owner(self):
        if self.port == self.child.port or not Path(self.identity_token_path).is_absolute():
            raise ValueError("distinct bridge listener and pinned identity token required")
        # No native model/endpoint override, downloads, preloads or Python startup hooks.
        if not set(self.environment) <= {"PATH", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES", "LANG", "LC_ALL"}:
            raise ValueError("unsupported child environment override")
        return self


class VisualOwnerBridge:
    def _check(self):
        if self.closed or self.invalid: raise ValueError("visual owner bridge closed")
        try:
            self.watch.check()
            if (file_signature(self.recipe_path) != self.recipe_signature or record_hash(self.recipe) != self.recipe_hash
                    or self.client is not self.owner.client or self.owner.recipe != self.recipe.child
                    or hashlib.sha256(self.token.encode("ascii")).hexdigest() != self.recipe.identity_token_sha256
                    or record_hash(runtime_dependency_inventory("VISUAL_OWNER_HTTP_V1", DEPENDENCIES)) != self.dependencies):
                raise ValueError("actual owner configuration/client/dependencies changed")
            self.owner.observe()
        except (OSError, ValueError):
            self.invalid = True
            raise

    def dispatch(self, method, path, headers, body=b""):
        """Bounded buffered adapter; never forwards a URL, auth, streaming or controls."""
        if (method, path) not in ROUTES | {("GET", "/identity"), ("GET", "/placement")}:
            return 404, canonical_bytes({"error": "not_found"})
        if path in {"/identity", "/placement"}:
            auth = [v for k, v in headers if k.lower() == "authorization"]
            if (len(auth) != 1 or len(auth[0]) > 512 or not hmac.compare_digest(
                    auth[0].encode("utf-8"), ("Bearer " + self.token).encode("ascii"))):
                return 401, canonical_bytes({"error": "unauthorized"})
        if len(body) > self.recipe.max_request_bytes:
            return 413, canonical_bytes({"error": "request_too_large"})
        if method == "GET" and body: return 400, canonical_bytes({"error": "invalid_request"})
        if method == "POST":
            try:
                data = _json(body)
                if not isinstance(data, dict) or data.get("stream", False) is not False:
                    raise ValueError("streaming request")
            except (ValueError, TypeError):
                return 400, canonical_bytes({"error": "invalid_request"})
        try:
            self._check()
            with self.owner.serving() as client:
                if path in {"/identity", "/placement"}:
                    identity = self.owner.observe()
                    if self.recipe.child.scope != "VISUAL_OWNER_PRODUCTION":
                        status, raw = 503, canonical_bytes({"error": "synthetic_owner_not_production"})
                    elif path == "/placement":
                        placement = TargetWeightPlacement.model_validate(self.owner._witness["target_weight_placement"])
                        status, raw = 200, canonical_bytes(OwnedTargetWeightPlacement(
                            instance_sha256=identity.model.instance_sha256,
                            process_sha256=identity.process_sha256,
                            witness_sha256=identity.witness_sha256,
                            placement_sha256=placement.sha256,
                            observation=placement.summary()))
                    else:
                        proof = identity.model.model_copy(update={"dependencies_sha256": self.dependencies,
                            "config_sha256": record_hash({"owner": identity.model.config_sha256,
                                "bridge": self.recipe_hash})})
                        status, raw = 200, canonical_bytes(proof)
                else:
                    with client.stream(method, path, content=body or None,
                            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                            timeout=self.recipe.request_timeout_s) as response:
                        status = response.status_code
                        raw = _response_bytes(response, self.recipe.max_response_bytes)
                        # Errors can contain paths/prompts. A failed model call
                        # has no safe response payload for this narrow adapter.
                        if status != 200: raw = canonical_bytes({"error": "native_request_failed"})
                        else: _json(raw)
            self._check()
            return status, raw
        except (OSError, ValueError, httpx.HTTPError):
            return 503, canonical_bytes({"error": "model_owner_unavailable"})

    def close(self):
        self.closed = True
        if getattr(self, "owner", None) is not None: self.owner.close()
        if getattr(self, "client", None) is not None: self.client.close()
        self.watch.close()


def create_visual_owner(recipe_path: Path, recipe_sha256: str, *, diagnostics=None, cancel=None):
    diag = NULL_DIAGNOSTICS if diagnostics is None else diagnostics
    diag.enter("RECIPE_IDENTITY")
    raw = _read(recipe_path, 1024*1024)
    if hashlib.sha256(raw).hexdigest() != recipe_sha256: raise ValueError("visual owner recipe changed")
    recipe = VisualOwnerRecipe.model_validate(_json(raw))
    obj = VisualOwnerBridge()
    obj.closed, obj.invalid, obj.owner, obj.client = False, False, None, None
    obj.recipe, obj.recipe_path, obj.recipe_hash = recipe, Path(recipe_path), record_hash(recipe)
    diag.enter("WATCHES")
    obj.watch = NativeFileWatch([recipe_path, recipe.identity_token_path, *recipe.child.expected_sha256])
    try:
        obj.recipe_signature = file_signature(recipe_path)
        if hashlib.sha256(_read(recipe_path, 1024*1024)).hexdigest() != recipe_sha256:
            raise ValueError("recipe changed before watch installation")
        diag.enter("DEPENDENCY_INVENTORY")
        inventory = set(recipe.child.implementation_files)
        if recipe.child.scope == "VISUAL_OWNER_PRODUCTION" and str(Path(sys.executable).resolve(strict=True)) not in inventory:
            raise ValueError("actual visual owner Python executable absent from inventory")
        for name in BRIDGE_MODULES:
            module = importlib.import_module(name)
            if str(Path(module.__file__).absolute()) not in inventory:
                raise ValueError("actual visual owner implementation absent from inventory")
        obj.dependencies = record_hash(runtime_dependency_inventory("VISUAL_OWNER_HTTP_V1", DEPENDENCIES))
        diag.enter("CREDENTIAL_READ")
        token = _read(recipe.identity_token_path, 256)
        if (hashlib.sha256(token).hexdigest() != recipe.identity_token_sha256 or not 32 <= len(token) <= 256
                or any(c < 33 or c > 126 for c in token)):
            raise ValueError("invalid pinned visual identity credential")
        obj.token = token.decode("ascii")
        diag.enter("CLIENT_SETUP")
        obj.client = httpx.Client(base_url=f"http://127.0.0.1:{recipe.child.port}", trust_env=False,
            follow_redirects=False, headers={"Accept-Encoding": "identity"},
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1), timeout=recipe.request_timeout_s)
        owner_options = {} if diagnostics is None else {"diagnostics": diagnostics}
        if cancel is not None: owner_options["cancel"] = cancel
        obj.owner = OwnedHookChildOwner.start(recipe.child, inference_client=obj.client,
            serving_client_getter=lambda: obj.client, environment=recipe.environment, **owner_options)
        diag.enter("BRIDGE_CHECK")
        obj._check()
        return obj
    except BaseException:
        try:
            obj.close()
        except BaseException as cleanup:  # never replaces the primary startup cause
            diag.cleanup_failure("BRIDGE_CLOSE", cleanup)
            LOG.error("visual_owner_cleanup_failed %s", classify(cleanup))
        raise


def serve_visual_owner(bridge, stop, *, diagnostics=None, on_listening=None):
    if bridge.recipe.child.scope != "VISUAL_OWNER_PRODUCTION":
        raise ValueError("synthetic bridge cannot open an external serving listener")
    class Handler(BaseHTTPRequestHandler):
        timeout = bridge.recipe.request_timeout_s
        def log_message(self, *args): LOG.info("visual_owner_http_request")
        def _run(self):
            lengths = self.headers.get_all("Content-Length") or []
            if (self.headers.get_all("Transfer-Encoding") or len(lengths) > 1
                    or (lengths and (not lengths[0].isascii() or not lengths[0].isdigit()))):
                status, raw = 400, canonical_bytes({"error": "invalid_request"})
            else:
                size = int(lengths[0]) if lengths else 0
                if size > bridge.recipe.max_request_bytes:
                    status, raw = 413, canonical_bytes({"error": "request_too_large"})
                else:
                    body = self.rfile.read(size)
                    if len(body) != size:
                        status, raw = 400, canonical_bytes({"error": "invalid_request"})
                    else:
                        status, raw = bridge.dispatch(self.command, self.path, list(self.headers.items()), body)
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        do_GET = do_POST = _run
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        def process_request(self, channel, address):
            if not slots.acquire(blocking=False): self.shutdown_request(channel); return
            try: super().process_request(channel, address)
            except BaseException: slots.release(); raise
        def process_request_thread(self, channel, address):
            try: super().process_request_thread(channel, address)
            finally: slots.release()
        def handle_error(self, *args): LOG.error("visual_owner_http_unavailable")
    slots = threading.BoundedSemaphore(bridge.recipe.max_clients)
    diag = NULL_DIAGNOSTICS if diagnostics is None else diagnostics
    diag.enter("LISTENER")
    with Server((bridge.recipe.host, bridge.recipe.port), Handler) as server:
        server.timeout = 0.5
        diag.enter("SERVING")
        diag.ready()
        if on_listening is not None: on_listening()
        while not stop.is_set(): server.handle_request()


def code_identity():
    """Hash of the installed owner/bridge/diagnostic sources actually imported."""
    from vkm_corpus.update import model_owner, owner_diagnostics
    return record_hash({name: sha256_of(Path(module.__file__)) for name, module in
                        (("visual_owner_bridge", sys.modules[__name__]), ("model_owner", model_owner),
                         ("owner_diagnostics", owner_diagnostics))})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Actual owned visual llama inference bridge")
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--recipe-sha256", required=True)
    parser.add_argument("--diagnostics-dir")
    args = parser.parse_args(argv)
    bridge = None
    identity = {"recipe_sha256": args.recipe_sha256}
    try:
        identity["code_sha256"] = code_identity()
    except Exception:
        pass
    diagnostics = StartupDiagnostics(kind="visual", identity=identity)
    failed = False
    try:
        bridge = create_visual_owner(Path(args.recipe), args.recipe_sha256, diagnostics=diagnostics)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: stop.set())
        serve_visual_owner(bridge, stop, diagnostics=diagnostics, on_listening=lambda: print(
            diagnostics.line("visual_owner_ready"), file=sys.stderr, flush=True))
        return 0
    except Exception as exc:
        failed = True
        diagnostics.fail(exc)
        return 2
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception as cleanup:
                diagnostics.cleanup_failure("BRIDGE_CLOSE", cleanup)
                if not failed: raise
        if failed:
            # After cleanup, so a secondary close failure is part of the same record.
            diagnostics.write_private(args.diagnostics_dir)
            print(diagnostics.line("visual_owner_startup_unavailable"), file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
