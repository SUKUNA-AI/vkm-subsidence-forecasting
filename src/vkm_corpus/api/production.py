"""Bind serving admission to the files actually selected by the API.

A prepared sidecar is not evidence of a running remote selector. The optional
typed native factory verifies the actual remote clients and loaded services;
missing observers close admission instead of certifying a DOCUMENT-only subset.
"""
from __future__ import annotations

from pathlib import Path
import ast
import importlib
import json
import hashlib
import platform
import stat

from vkm_corpus.update.generation import GenerationCoordinator, GenerationUnavailable
from vkm_corpus.update.runtime import observe_components, read_bound
from vkm_evidence.contracts import record_hash

SERVING_CHECKS = {"native_identity", "policy_enforcement", "generation_consistency", "full_mcp",
                  "shadow_acceptance", "failed_switch", "rollback"}


def serving_code_identity():
    """Hash actual installed code, not an environment's claim of its commit."""
    from vkm_corpus.parquet.atomic import sha256_of
    files, size = {}, 0
    for package in ("vkm_corpus", "vkm_evidence", "vkm_datasets", "vkm_world", "vkm_jobs"):
        root = Path(importlib.import_module(package).__file__).parent
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".sql", ".json"} or "__pycache__" in path.parts:
                continue
            if path.is_symlink() or not path.is_file():
                raise GenerationUnavailable("indirect serving code resource")
            size += path.stat().st_size
            if size > 32 * 1024 * 1024 or len(files) >= 8192:
                raise GenerationUnavailable("serving code inventory exceeds qualification limit")
            files[package + "/" + path.relative_to(root).as_posix()] = sha256_of(path)
    return record_hash(files)


def read_tool_names():
    # The decorators are the registered public contract; no service connection
    # or model inference is needed to enumerate it.
    import vkm_corpus.mcp.servers as servers
    source = ast.parse(Path(servers.__file__).read_text(encoding="utf-8"))
    func = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "build_read_server")
    names = set()
    for node in ast.walk(func):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "tool":
                    for kw in decorator.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            names.add(kw.value.value)
    if not names:
        raise GenerationUnavailable("empty serving MCP contract")
    return names


def serving_dependencies_identity():
    from vkm_corpus.update.service_identity import runtime_dependency_inventory
    # This production profile requires all native stores, not a caller-selected
    # subset. PEP508 extras are essential: psycopg itself can import successfully
    # with the binary driver missing or another implementation selected.
    inventory = runtime_dependency_inventory("API_ALL_STORES_V1", (
        ("numpy>=2.5.3,<3", "numpy"), ("pydantic>=2.13.5,<3", "pydantic"),
        ("pyarrow>=25.0.1,<26", "pyarrow.parquet"), ("duckdb>=1.5.5,<2", "duckdb"),
        ("fastapi>=0.141.1,<0.142", "fastapi"), ("uvicorn>=0.40", "uvicorn"),
        ("httpx>=0.28", "httpx"), ("mcp==2.2.0", "mcp"),
        ("pillow>=12.3", "PIL.Image"), ("pytz>=2025.2", "pytz"),
        ("neo4j>=6.3.1,<7", "neo4j"), ("opensearch-py>=3.2.0,<4", "opensearchpy"),
        ("psycopg[binary]>=3.3", "psycopg"), ("psycopg-binary>=3.3", "psycopg_binary.pq"),
        ("packaging>=26.3,<27", "packaging"),
        ("pymorphy3>=2.0.6,<3", "pymorphy3"),
        ("pymorphy3-dicts-ru>=2.4.417150", "pymorphy3_dicts_ru"),
        ("snowballstemmer>=3.1,<4", "snowballstemmer"),
    ), (("httptools", "httptools"), ("uvloop", "uvloop"), ("websockets", "websockets"),
        ("wsproto", "wsproto"), ("brotli", "brotli"), ("brotlicffi", "brotlicffi"),
        ("zstandard", "zstandard")))
    import psycopg.pq
    if psycopg.pq.__impl__ != "binary":
        raise GenerationUnavailable("API binary database driver profile is unavailable")
    return record_hash(inventory)


def require_navigation_runtime():
    """Inspect the cached morphology actually used by each qualified query.

    Optional fallback remains available to legacy/development readers. This
    all-store production profile must not accept a corrupt dictionary or a
    cached crude/stem-only reader under a pymorphy3 acceptance.
    """
    from vkm_corpus.navigation.concepts import Morphology
    for module in ("concepts_query", "expansion_query", "topics_query", "parameters_query", "term_dictionary_query"):
        try:
            reader = importlib.import_module("vkm_corpus.navigation." + module)._morph()
        except Exception as exc:
            raise GenerationUnavailable("qualified navigation morphology is unavailable") from exc
        if type(reader) is not Morphology or reader.name != "pymorphy3":
            raise GenerationUnavailable("qualified navigation morphology has degraded")


def serving_access_identity(api_config):
    """Bind effective principal permissions and token roles, without bearer bytes.

    Rotating a token for the same principal does not change the authorization
    contract. Granting a different class, execution location, target visibility
    or read/write role requires a new qualified acceptance.
    """
    return record_hash({"schema": "vkm-serving-access/1",
        "read_principals": sorted(set(api_config.read_tokens.values())),
        "write_principals": sorted(set(api_config.write_tokens.values())),
        "contexts": {label: context.model_dump(mode="json")
                     for label, context in sorted(api_config.access_contexts.items())}})


def require_serving_acceptance(runtime, manifest, api_config):
    from vkm_corpus.update.runtime import contained
    path = contained(Path(runtime.config.qualification_root).resolve(), manifest.acceptance_sha256 + ".json")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest.acceptance_sha256:
        raise GenerationUnavailable("serving acceptance bytes changed")
    proof = json.loads(data)
    duckdb_sha = proof.get("duckdb_file_sha256")
    components = [c.model_dump(mode="json") for c in sorted(manifest.components, key=lambda c: c.component)]
    services = [s.model_dump(mode="json") for s in sorted(manifest.services, key=lambda s: s.service)]
    if (proof.get("schema") != "vkm-serving-acceptance/1" or proof.get("scope") != "SHADOW_PRODUCTION"
            or proof.get("status") != "PASS" or proof.get("code_commit") != manifest.code_commit
            or proof.get("code_tree_sha256") != serving_code_identity()
            or proof.get("dependencies_sha256") != serving_dependencies_identity()
            or proof.get("access_config_sha256") != serving_access_identity(api_config)
            or proof.get("policy_sha256") != manifest.policy_sha256
            or proof.get("components_sha256") != record_hash(components)
            or proof.get("services_sha256") != record_hash(services)
            or not isinstance(duckdb_sha, str) or len(duckdb_sha) != 64
            or any(c not in "0123456789abcdef" for c in duckdb_sha)
            or proof.get("checks") != dict.fromkeys(SERVING_CHECKS, "PASS")
            or proof.get("tools") != dict.fromkeys(read_tool_names(), "PASS")):
        raise GenerationUnavailable("serving acceptance scope/code/components/MCP contract is unqualified")
    require_navigation_runtime()
    return proof


def _duckdb_file_signature(path: Path) -> tuple[int, int, int, int, int]:
    """Cheap lifetime check of an operator-owned immutable serving artifact.

    This is operational replacement/mutation detection, not protection against a
    privileged actor able to spoof filesystem identities or modify process RAM.
    """
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise GenerationUnavailable("indirect serving DuckDB file")
        value = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(value.st_mode):
            raise GenerationUnavailable("serving DuckDB is not an ordinary file")
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    except OSError as exc:
        raise GenerationUnavailable("serving DuckDB file is unavailable") from exc


def _selected_duckdb_path(deps) -> Path:
    value = getattr(deps.canon, "path", None)
    if value is None:
        raise GenerationUnavailable("qualified production requires a file-backed DuckDB")
    try:
        # Keep the selected path instead of resolve(): symlinks must be refused,
        # not silently converted to a seemingly ordinary target path.
        return Path(value).absolute()
    except (TypeError, ValueError) as exc:
        raise GenerationUnavailable("invalid serving DuckDB path") from exc


def _native_canon_binding(canon, selected: Path):
    """Verify the connection used by queries, not only its declared path."""
    from vkm_corpus.api.canon import CanonStore
    if type(canon) is not CanonStore:
        raise GenerationUnavailable("actual canonical DuckDB backend required")
    from vkm_corpus.api.errors import ApiFailure
    try:
        databases = canon.query("SELECT path, readonly FROM duckdb_databases() WHERE NOT internal")
    except ApiFailure as exc:
        raise GenerationUnavailable("canonical native query binding is unavailable") from exc
    if (len(databases) != 1 or databases[0]["readonly"] is not True
            or not databases[0]["path"] or Path(databases[0]["path"]).absolute() != selected):
        raise GenerationUnavailable("canonical query connection differs from qualified read-only file")
    return canon._con


def _native_nav_binding(nav, canonical_path: Path, selected: Path):
    from vkm_corpus.navigation.store import NavStore, NavUnavailable
    if type(nav) is not NavStore or nav._functions:
        raise GenerationUnavailable("actual unmodified NAV query backend required")
    try:
        rows = nav.query("SELECT database_name, path, readonly FROM duckdb_databases() "
                         "WHERE NOT internal AND path IS NOT NULL")
        wanted = {"canon": canonical_path, "nav": selected}
        if (len(rows) != 2 or {r["database_name"] for r in rows} != set(wanted)
                or any(r["readonly"] is not True or Path(r["path"]).absolute() != wanted[r["database_name"]] for r in rows)):
            raise GenerationUnavailable("NAV query attachments differ from qualified read-only files")
        return nav._con
    except (NavUnavailable, OSError) as exc:
        raise GenerationUnavailable("native NAV query binding unavailable") from exc


class _ServingFileLease:
    def __init__(self, *watches):
        self.watches = watches

    def check(self):
        for watch in self.watches:
            watch.check()

    def close(self):
        for watch in self.watches:
            watch.close()


def _qualify_duckdb_file(path: Path, expected_sha256: str, *, capture_watch: bool = False):
    """One bounded hash proof; a production lifetime lease additionally watches.

    CandidateFence owns its own multi-file watch. Synthetic qualification can
    hash on any OS and does not certify native mutation detection.
    """
    from vkm_corpus.parquet.atomic import sha256_of
    from vkm_corpus.update.native_files import NativeFileWatch

    before = _duckdb_file_signature(path)
    watch = None
    try:
        if capture_watch:
            watch = NativeFileWatch((path,))
            watch.check()
        actual = sha256_of(path)
        if watch is not None:
            watch.check()
        if _duckdb_file_signature(path) != before or actual != expected_sha256:
            raise GenerationUnavailable("serving DuckDB bytes differ from qualified immutable artifact")
        return (before, actual, watch) if capture_watch else (before, actual)
    except (OSError, ValueError, GenerationUnavailable) as exc:
        if watch is not None:
            watch.close()
        raise GenerationUnavailable("cannot verify immutable serving DuckDB bytes and native mutation lease") from exc


def require_serving_profile(environ):
    from vkm_corpus.config import ConfigError
    profile = environ.get("VKM_API_PROFILE", "production")
    if profile not in {"production", "compatibility"}:
        raise ConfigError("unsupported VKM_API_PROFILE")
    if profile == "production" and any(not environ.get(name) for name in
        ("VKM_SOURCE_POLICY_FILE", "VKM_ACCESS_CONTEXT_FILE", "VKM_UPDATE_RUNTIME_FILE")):
        raise ConfigError("production serving requires policy, access contexts and qualified runtime configuration")
    return profile


def bind_generation_guard(deps, runtime, api_config, data_root: Path, *, _native=None):
    # The immutable-file lease depends on Linux change time, not Windows birth
    # time. Other OSes need qualified native change-time and non-reparse handles
    # before they can act as production receivers; do not silently fall back.
    if platform.system() != "Linux":
        raise GenerationUnavailable(
            "qualified production receiver requires Linux; native Windows "
            "ChangeTime/reparse-handle qualification unavailable"
        )
    from vkm_corpus.parquet.layout import CanonLayout
    from vkm_corpus.parquet.reader import current_snapshot_id

    from vkm_corpus.contracts.policy_store import SourcePolicyStore
    if (type(deps.access_policy) is not SourcePolicyStore
            or deps.access_policy.path.absolute() != Path(runtime.config.policy.path).absolute()):
        raise GenerationUnavailable("production source policy is not bound to serving")
    selected_policy = deps.access_policy
    policy_path, source_provider = selected_policy.path.absolute(), selected_policy.sources
    selected_backends = {name: getattr(deps, name) for name in ("canon", "nav", "evidence")}
    labels = set(api_config.read_tokens.values()) | set(api_config.write_tokens.values())
    if not labels or not labels.issubset(api_config.access_contexts):
        raise GenerationUnavailable("production principals lack access contexts")
    required = {"DOCUMENT", "DUCKDB"}
    if deps.nav is not None:
        required.add("NAV")
    if deps.evidence is not None:
        required.add("EVIDENCE")
    if deps.graph is not None:
        required.add("GRAPH")
    if deps.search is not None:
        required.add("SEARCH")
    if deps.hybrid is not None:
        required.update(("DENSE", "LATE", "VISUAL"))
    specs = {s.component: s for s in runtime.config.observations}
    # Remote adapters must observe live serving identities, not expected JSON.
    has_remote = bool(required - {"DOCUMENT", "DUCKDB", "NAV", "EVIDENCE"}
                      or any(getattr(deps, name, None) is not None for name in ("rerank", "control")))
    if has_remote and _native is None:
        raise GenerationUnavailable("qualified live graph/search/pack/rerank/control observers are required")
    if _native is not None:
        from vkm_corpus.update.serving import NativeServingBindings
        if (not isinstance(_native, NativeServingBindings) or _native.deps is not deps
                or runtime.config.native_serving != _native.profile_file or not has_remote):
            raise GenerationUnavailable("native observers are not bound to this actual API/profile")
    local_required = required - {"GRAPH", "SEARCH", "DENSE", "LATE", "VISUAL"}
    if (_native is not None and set(specs) & {"DENSE", "LATE", "VISUAL"}
            or not local_required.issubset(specs) or any(not specs[k].required for k in local_required)):
        raise GenerationUnavailable("served dependencies omitted from required observers")
    layout = CanonLayout(Path(data_root)).require("CANONICAL")
    coordinator = GenerationCoordinator(runtime.root / "served")

    def same_path(declared, actual):
        if declared is None or Path(declared).resolve() != Path(actual).resolve():
            raise GenerationUnavailable("observer does not inspect the API selected file")

    selected_duckdb = _selected_duckdb_path(deps)
    same_path(specs["DUCKDB"].runtime_database, selected_duckdb)
    native_connection, canonical_sources = None, ()

    def require_actual_bindings():
        if (deps.access_policy is not selected_policy or selected_policy.path.absolute() != policy_path
                or selected_policy.sources is not source_provider
                or any(getattr(deps, name) is not backend for name, backend in selected_backends.items())
                or _native_canon_binding(deps.canon, selected_duckdb) is not native_connection):
            raise GenerationUnavailable("qualified serving backend or authorization binding changed")
        if tuple(sorted(source_provider())) != canonical_sources:
            raise GenerationUnavailable("authorization omits or changes the actual canonical source inventory")
        if not set(canonical_sources).issubset(selected_policy.read()):
            raise GenerationUnavailable("actual canonical sources lack current policy")

    proof = require_serving_acceptance(runtime, coordinator.manifest(), api_config)
    signature, duckdb_sha256, canonical_lease = _qualify_duckdb_file(
        selected_duckdb, proof["duckdb_file_sha256"], capture_watch=True)
    selected_nav = None
    nav_signature, nav_sha256, nav_connection, nav_lease = None, None, None, None
    try:
        # Reject nonordinary/indirect files and install mutation watches before
        # opening a native database. In particular, opening a FIFO could block.
        native_connection = _native_canon_binding(deps.canon, selected_duckdb)
        canonical_sources = tuple(sorted(row["source_id"] for row in deps.canon.query("SELECT source_id FROM sources")))
        require_actual_bindings()
        if "NAV" in required:
            canonical_db, nav_db, _ = deps.nav._paths()
            same_path(canonical_db, selected_duckdb)
            same_path(specs["NAV"].runtime_database, nav_db)
            selected_nav = Path(nav_db).absolute()
            from pydantic import TypeAdapter
            from vkm_evidence.contracts import Sha256
            nav_sha256 = TypeAdapter(Sha256).validate_python(proof.get("nav_file_sha256"))
            nav_signature, _, nav_lease = _qualify_duckdb_file(selected_nav, nav_sha256, capture_watch=True)
            nav_connection = _native_nav_binding(deps.nav, selected_duckdb, selected_nav)
    except BaseException:
        canonical_lease.close()
        if nav_lease is not None:
            nav_lease.close()
        raise
    file_lease = _ServingFileLease(canonical_lease, *([nav_lease] if nav_lease is not None else []))

    def require_immutable_duckdb():
        try:
            file_lease.check()
        except (ValueError, OSError) as exc:
            raise GenerationUnavailable("qualified serving DuckDB mutation lease is invalid") from exc
        if (_selected_duckdb_path(deps) != selected_duckdb
                or _duckdb_file_signature(selected_duckdb) != signature):
            raise GenerationUnavailable("qualified serving DuckDB was changed or replaced; rebind is required")
        if selected_nav is not None:
            _, nav_db, _ = deps.nav._paths()
            if (Path(nav_db).absolute() != selected_nav or _duckdb_file_signature(selected_nav) != nav_signature
                    or _native_nav_binding(deps.nav, selected_duckdb, selected_nav) is not nav_connection):
                raise GenerationUnavailable("qualified packed NAV changed or query binding differs")

    def observer():
        require_immutable_duckdb()
        require_actual_bindings()
        read_bound(runtime.config.policy)  # a policy edit invalidates all old projections
        manifest = coordinator.manifest()
        components = {c.component: c for c in manifest.components if c.required}
        if not required.issubset(components) or manifest.policy_sha256 != runtime.config.policy.sha256:
            raise GenerationUnavailable("generation omits served dependencies or current policy")
        if manifest.code_commit != runtime.config.expected_commit:
            raise GenerationUnavailable("generation code/acceptance qualification is stale")
        proof = require_serving_acceptance(runtime, manifest, api_config)
        if proof["duckdb_file_sha256"] != duckdb_sha256:
            raise GenerationUnavailable("acceptance refers to another serving DuckDB artifact")
        if selected_nav is not None and proof.get("nav_file_sha256") != nav_sha256:
            raise GenerationUnavailable("acceptance refers to another packed NAV artifact")
        snapshot = current_snapshot_id(layout)
        if snapshot is None:
            raise GenerationUnavailable("canonical selector unavailable")
        canonical_manifest = layout.path(layout.snapshot_manifest(snapshot))
        for key in ("DOCUMENT", "DUCKDB"):
            same_path(specs[key].native_manifest, canonical_manifest)
        same_path(specs["DUCKDB"].runtime_database, deps.canon.path)
        if "NAV" in required:
            canonical_db, nav_db, _ = deps.nav._paths()
            same_path(canonical_db, deps.canon.path)
            same_path(specs["NAV"].runtime_database, nav_db)
            same_path(specs["NAV"].native_manifest, nav_db.parent / "manifest.json")
        result = observe_components(tuple(s for s in runtime.config.observations if s.component != "EVIDENCE"))
        if "EVIDENCE" in required:
            import json
            from vkm_corpus.parquet.atomic import sha256_of
            from vkm_corpus.update.contracts import ComponentIdentity
            journal = deps.evidence.journal
            revision = journal.revision
            journal.commits(revision)  # verify the parent-linked commit chain
            native = journal._path("commits", revision + ".json")
            spec = specs["EVIDENCE"]
            same_path(spec.native_manifest, native)
            if sha256_of(native) != revision:
                raise GenerationUnavailable("served evidence commit identity differs")
            binding = Path(spec.policy_binding)
            if binding.is_symlink():
                raise GenerationUnavailable("indirect evidence policy binding")
            policy = json.loads(binding.read_bytes())
            if policy.get("status") != "PASS" or policy.get("component_manifest_sha256") != revision:
                raise GenerationUnavailable("evidence policy binding unavailable")
            result["EVIDENCE"] = ComponentIdentity(component="EVIDENCE", revision=revision,
                manifest_sha256=revision, policy_sha256=policy["policy_sha256"],
                built_from=policy.get("built_from", {})).model_dump(mode="json")
        if _native is not None:
            result.update(_native.observe_components())
            _native.verify_document(result["DOCUMENT"])
        require_immutable_duckdb()
        return result

    try:
        if _native is None:
            runtime.require_startup(observer)
        else:
            manifest = coordinator.manifest()
            coordinator.verify(manifest, observer(), _native.services.identities)
            if coordinator.manifest().sha256 != manifest.sha256:
                raise GenerationUnavailable("generation changed during native startup")
        require_immutable_duckdb()  # replacement just after the startup observer returned
        from vkm_corpus.update.barrier import ReceiverBarrier
        barrier = getattr(deps, "admission_barrier", None)
        if barrier is None:
            barrier = ReceiverBarrier("api-receiver", gate_path=coordinator.root / "admission.lock")
        elif not isinstance(barrier, ReceiverBarrier):
            raise GenerationUnavailable("receiver admission barrier is unqualified")
        barrier.status()  # reject a barrier inherited by another process
        barrier.bind_gate(coordinator.root / "admission.lock")
    except BaseException:
        file_lease.close()
        raise
    previous_lease = getattr(deps, "serving_file_lease", None)
    deps.admission_barrier = barrier
    deps.serving_file_lease = file_lease
    if _native is None:
        deps.generation_guard = lambda: runtime.generation_status(observer)
    else:
        async def native_guard():
            import asyncio
            from vkm_corpus.api.errors import ApiFailure
            try:
                manifest = coordinator.manifest()
                services = await _native.observe_services()
                components = await asyncio.to_thread(observer)
                coordinator.verify(manifest, components, services)
                if coordinator.manifest().sha256 != manifest.sha256:
                    raise GenerationUnavailable("generation changed during native admission")
                require_immutable_duckdb()
                return {"status": "READY", "generation": manifest.sha256}
            except (OSError, ValueError, GenerationUnavailable, ApiFailure):
                return {"status": "UNAVAILABLE"}
        deps.generation_guard = native_guard
    if previous_lease is not None:
        previous_lease.close()
    deps.serving_profile = "production"
    return deps.generation_guard


async def bind_native_generation_guard(deps, runtime, api_config, data_root: Path):
    """ASGI startup: qualify the actual configured clients without nested loops."""
    import asyncio
    if platform.system() != "Linux":
        raise GenerationUnavailable("qualified production receiver requires Linux")
    if runtime.config.native_serving is None:
        # The local-only profile still qualifies its selected bytes and cannot
        # silently omit a remote dependency.
        return await asyncio.to_thread(bind_generation_guard, deps, runtime, api_config, data_root)
    from vkm_corpus.update.serving import NativeServingBindings
    native = await NativeServingBindings.bind(deps, runtime.config.native_serving)
    guard = await asyncio.to_thread(bind_generation_guard, deps, runtime, api_config, data_root, _native=native)
    if (await guard())["status"] != "READY":
        deps.serving_file_lease.close()
        raise GenerationUnavailable("native serving changed after startup qualification")
    return guard
