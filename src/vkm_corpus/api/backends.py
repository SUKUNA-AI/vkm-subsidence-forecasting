"""Dependencies of the API behind small interfaces (real adapters here; deterministic fakes in the tests).

* :class:`SearchBackend` — OpenSearch through agent E (``vkm_corpus.search.query.search``): candidate IDs only;
* :class:`HybridBackend` — BM25 + dense k-NN (vectors alias) + RX580 query encoder, RRF with a stage trace;
* :class:`GraphBackend` — Neo4j through agent E (``graph_state``, read templates): IDs and structure only;
* :class:`RerankBackend` — ``vkm-rerank-gateway`` on EDGE through agent F's ``RerankClient``;
* :class:`ControlPlane` — PostgreSQL jobs through ``vkm_corpus.ops.jobs`` (plan-first, H-12);
* :class:`ArtifactBlobs` — content-addressed bytes under ``$VKM_DATA_ROOT/artifacts`` (read-only; sha verified).

Every adapter maps its failures to :class:`ApiFailure` (``DEPENDENCY_UNAVAILABLE`` / ``DEPENDENCY_TIMEOUT`` /
``DEPENDENCY_ERROR``); secrets and addresses never appear in messages.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.ids import grammar


# ---------------------------------------------------------------------------------------------------- interfaces
class SearchBackend(Protocol):
    def search(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...


class GraphBackend(Protocol):
    def state(self) -> dict[str, Any]: ...

    def page_neighbors(self, page_id: str) -> dict[str, Any] | None: ...

    def citations(self, work_id: str, direction: str) -> list[dict[str, Any]]: ...


class RerankBackend(Protocol):
    async def rerank_text(self, query: str, candidates: list[tuple[str, str]], top_n: int | None,
                          request_id: str | None, truncate_to_tokens: int | None) -> dict[str, Any]: ...

    async def rerank_visual(self, query: str, candidates: list[tuple[str, bytes]], top_n: int | None,
                            request_id: str | None) -> dict[str, Any]: ...

    async def status(self) -> dict[str, Any]: ...


class ControlPlane(Protocol):
    def request_plan(self, kind: str, requested_by: str, *, source_id: str | None, page_id: str | None,
                     request: dict[str, Any]) -> int: ...

    def job(self, job_id: int) -> dict[str, Any] | None: ...

    def active_jobs(self, kind: str) -> list[dict[str, Any]]: ...

    def confirm(self, job_id: int, plan_sha256: str, confirmed_by: str) -> dict[str, Any]: ...

    def cancel(self, job_id: int, by: str, note: str) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------------------------------- OpenSearch
class OpenSearchBackend:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._client: Any = None

    def _connect(self) -> Any:
        if self._client is None:
            from vkm_corpus.search.client import connect

            try:
                self._client = connect(self.settings)
            except Exception as exc:  # noqa: BLE001 - reported without the address
                raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"search index not reachable ({type(exc).__name__})",
                                 stage="opensearch", tool="opensearch") from exc
        return self._client

    def search(self, request: dict[str, Any]) -> dict[str, Any]:
        from vkm_corpus.search.query import SearchRequest, SearchRequestError, search

        try:
            response = search(self._connect(), SearchRequest(**request), self.settings.opensearch_index_prefix)
        except SearchRequestError as exc:
            status = "DEPENDENCY_ERROR" if exc.code == "E_SEARCH_FAILED" else "INVALID_ARGUMENT"
            raise ApiFailure(status, exc.message, stage="opensearch", tool="opensearch",
                             details={"search_code": exc.code}) from exc
        except ApiFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"search failed ({type(exc).__name__})", stage="opensearch",
                             tool="opensearch") from exc
        return response.as_dict()

    def late_rerank(self, query: str, targets: list[dict[str, str]]) -> Any:
        """mLateOn MaxSim of given targets on the RX580 token store (``rerank_text`` since the EDGE text reranker was
        retired, 29.09): a page over its units except BIB_ENTRY, an object over its own unit."""
        from vkm_corpus.search.hybrid import HybridError

        try:
            return self._embed.late_scores(query, targets)
        except HybridError as exc:
            raise ApiFailure(exc.code, exc.message, stage=f"rerank_{exc.stage}", tool=exc.tool,
                             details=exc.details) from exc

    def status(self) -> dict[str, Any]:
        from vkm_corpus.search.indexer import status

        try:
            return status(self._connect(), self.settings.opensearch_index_prefix)
        except ApiFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"search status failed ({type(exc).__name__})",
                             stage="opensearch", tool="opensearch") from exc


class HybridBackend:
    """Hybrid search (``vkm_corpus.search.hybrid``): E's BM25 + the vectors alias + the RX580 query encoder (+ late
    interaction on request). The vectors build ``_meta`` is cached for 30 s; every failure maps to an
    :class:`ApiFailure` without addresses. ``VKM_HYBRID_LATE_DEFAULT`` (1/0) overrides the code default of the late
    stage for requests that do not say (operators switch it without a rebuild). ``VKM_HYBRID_VISUAL_ROUTE`` (1/0)
    switches the visual route (agent VIS: page-image channel for queries with picture words; off in code, on after the
    RX580 gate and the page-vector build), ``VKM_HYBRID_VISUAL_SEARCH`` = ``exact`` (default) | ``hnsw`` and
    ``VKM_HYBRID_VISUAL_EF_SEARCH`` tune its page-vector search; the page-vector ``_meta`` is cached like the vectors
    one."""

    META_TTL_S = 30.0

    def __init__(self, settings: Any, search: OpenSearchBackend | None = None, *, embed: Any = None,
                 late_default: bool | None = None, visual: Any = None) -> None:
        import os

        from vkm_corpus.search.hybrid import VISUAL_ROUTE_DEFAULT, EmbedClient, VisualRouteSettings

        self.settings = settings
        self._search = search or OpenSearchBackend(settings)
        self._embed = embed if embed is not None else EmbedClient(settings.embed_url, settings.embed_token)
        self._meta: tuple[float, dict[str, Any]] | None = None
        self._vmeta: tuple[float, dict[str, Any]] | None = None
        flags = {"1": True, "true": True, "on": True, "0": False, "false": False, "off": False}
        if late_default is None:
            raw = os.environ.get("VKM_HYBRID_LATE_DEFAULT", "").strip().lower()
            late_default = flags.get(raw)
        self.late_default = late_default
        if visual is None:
            enabled = flags.get(os.environ.get("VKM_HYBRID_VISUAL_ROUTE", "").strip().lower())
            mode = os.environ.get("VKM_HYBRID_VISUAL_SEARCH", "").strip().lower() or "exact"
            ef = os.environ.get("VKM_HYBRID_VISUAL_EF_SEARCH", "").strip()
            visual = VisualRouteSettings(enabled=VISUAL_ROUTE_DEFAULT if enabled is None else enabled,
                                         mode=mode if mode in ("exact", "hnsw") else "exact",
                                         ef_search=int(ef) if ef.isdigit() else None)
        self.visual = visual

    def _vectors_meta(self, client: Any) -> dict[str, Any]:
        import time

        from vkm_corpus.search.hybrid import vectors_meta

        now = time.monotonic()
        if self._meta is None or now - self._meta[0] > self.META_TTL_S:
            self._meta = (now, vectors_meta(client, self.settings.opensearch_index_prefix))
        return self._meta[1]

    def _visual_meta(self, client: Any) -> dict[str, Any]:
        import time

        from vkm_corpus.search.hybrid import visual_meta

        now = time.monotonic()
        if self._vmeta is None or now - self._vmeta[0] > self.META_TTL_S:
            self._vmeta = (now, visual_meta(client, self.settings.opensearch_index_prefix))
        return self._vmeta[1]

    def status(self) -> dict[str, Any]:
        """Query encoder health (RX580 retrieval service, with its late-interaction token store), the late default and
        the visual route switch; the vectors and page-vector builds are part of the OpenSearch status."""
        from vkm_corpus.search.hybrid import LATE_DEFAULT

        return {"query_encoder": self._embed.health(),
                "late_default": LATE_DEFAULT if self.late_default is None else self.late_default,
                "visual_route": {"enabled": bool(self.visual.enabled), "search": self.visual.mode,
                                 "ef_search": self.visual.ef_search}}

    def search(self, request: dict[str, Any]) -> dict[str, Any]:
        from vkm_corpus.search.hybrid import HybridError, HybridRequest, hybrid_search
        from vkm_corpus.search.query import SearchRequestError

        if request.get("late") is None and self.late_default is not None:
            request = {**request, "late": self.late_default}
        try:
            client = self._search._connect()
            meta = self._vectors_meta(client)
            return hybrid_search(client, self._embed, HybridRequest(**request),
                                 self.settings.opensearch_index_prefix, meta=meta, visual=self.visual,
                                 vmeta=self._visual_meta)
        except HybridError as exc:
            if exc.stage == "vectors":
                self._meta = None
            if exc.stage in ("visual", "visual_embed"):
                self._vmeta = None
            raise ApiFailure(exc.code, exc.message, stage=f"hybrid_{exc.stage}", tool=exc.tool,
                             details=exc.details) from exc
        except SearchRequestError as exc:
            status = "DEPENDENCY_ERROR" if exc.code == "E_SEARCH_FAILED" else "INVALID_ARGUMENT"
            raise ApiFailure(status, exc.message, stage="hybrid", tool="opensearch",
                             details={"search_code": exc.code}) from exc
        except ApiFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            self._meta = None
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"hybrid search failed ({type(exc).__name__})",
                             stage="hybrid", tool="opensearch") from exc


# ---------------------------------------------------------------------------------------------------- Neo4j
class Neo4jBackend:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._driver: Any = None

    def _connect(self) -> Any:
        if self._driver is None:
            from vkm_corpus.graph.client import connect

            try:
                self._driver = connect(self.settings)
            except Exception as exc:  # noqa: BLE001
                raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"graph not reachable ({type(exc).__name__})",
                                 stage="neo4j", tool="neo4j") from exc
        return self._driver

    def _read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        from vkm_corpus.graph.client import read

        try:
            return read(self._connect(), self.settings.neo4j_database, query, **params)
        except ApiFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"graph query failed ({type(exc).__name__})", stage="neo4j",
                             tool="neo4j") from exc

    def state(self) -> dict[str, Any]:
        from vkm_corpus.graph.runs import graph_state

        try:
            return graph_state(self._connect(), self.settings.neo4j_database)
        except ApiFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"graph state unavailable ({type(exc).__name__})",
                             stage="neo4j", tool="neo4j") from exc

    def page_neighbors(self, page_id: str) -> dict[str, Any] | None:
        from vkm_corpus.graph.cypher import read_neighbors
        from vkm_corpus.graph.schema import Namespace

        rows = self._read(read_neighbors(Namespace()), page_id=page_id)
        return rows[0] if rows else None

    def citations(self, work_id: str, direction: str) -> list[dict[str, Any]]:
        from vkm_corpus.graph.cypher import read_citations
        from vkm_corpus.graph.schema import Namespace

        return self._read(read_citations(Namespace(), "out" if direction == "cites" else "in"), work_id=work_id)

    # ---------------------------------------------------------------- NAV graph (agent G): the navigation layer in Neo4j
    # READ transactions with a timeout (vkm_corpus.graph.nav_query); IDs, short names and page IDs only.
    def _nav_read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        from vkm_corpus.graph.nav_query import READ_TIMEOUT_S, timed

        try:
            records, _, _ = self._connect().execute_query(timed(query, READ_TIMEOUT_S), parameters_=params,
                                                          database_=self.settings.neo4j_database, routing_="r")
        except ApiFailure:
            raise
        except Exception as exc:  # noqa: BLE001 — no address or credentials in the message
            name = type(exc).__name__
            timed_out = "Timeout" in name or "TimedOut" in str(getattr(exc, "code", ""))
            raise ApiFailure("DEPENDENCY_TIMEOUT" if timed_out else "DEPENDENCY_UNAVAILABLE",
                             f"NAV graph query failed ({name})", stage="nav_graph", tool="neo4j") from exc
        return [dict(r) for r in records]

    def nav_state(self) -> dict[str, Any]:
        from vkm_corpus.graph import nav_schema
        from vkm_corpus.graph.nav_query import cy_meta
        from vkm_corpus.graph.schema import Namespace

        rows = self._nav_read(cy_meta(Namespace()), id=nav_schema.META_ID)
        props = dict(rows[0].get("props") or {}) if rows else {}
        if not props:
            return {"state": "EMPTY"}
        status = props.get("status")
        return {"state": "READY" if status == "COMPLETE" else ("LOADING" if status == "LOADING" else "FAILED"),
                "snapshot_id": props.get("snapshot_id"), "run_id": props.get("run_id"),
                "finished_at": str(props.get("finished_at") or "") or None,
                "graph_schema_version": props.get("graph_schema_version")}

    def nav_find_terms(self, text: str, keys: list[str], norm: str, limit: int = 5) -> list[dict[str, Any]]:
        from vkm_corpus.graph.nav_query import cy_find_terms, cy_find_terms_by_name
        from vkm_corpus.graph.schema import Namespace

        rows = self._nav_read(cy_find_terms(Namespace()), text=text, keys=keys, limit=int(limit))
        if not rows and norm:
            rows = self._nav_read(cy_find_terms_by_name(Namespace()), norm=norm, limit=int(limit))
        return rows

    def nav_paths(self, a: str, b: str, rel_types: list[str], max_len: int, cap: int) -> list[dict[str, Any]]:
        """All shortest paths: the length first, then the paths (ranked in the database when they are short)."""
        from vkm_corpus.graph import nav_schema
        from vkm_corpus.graph.nav_query import cy_paths, cy_shortest_length
        from vkm_corpus.graph.schema import Namespace

        labels = list(nav_schema.PATH_LABELS)
        params = {"a": a, "b": b, "rel_types": rel_types, "labels": labels}
        found = self._nav_read(cy_shortest_length(Namespace(), rel_types, max_len, tuple(labels)), **params)
        if not found:
            return []
        length = int(found[0]["n"])
        return self._nav_read(cy_paths(Namespace(), rel_types, length, tuple(labels), ordered=length <= 2),
                              cap=int(cap), **params)

    def nav_neighbourhood(self, node_id: str, layer: str, per_type: int) -> dict[str, Any] | None:
        from vkm_corpus.graph.nav_query import cy_neighbourhood
        from vkm_corpus.graph.schema import Namespace

        rows = self._nav_read(cy_neighbourhood(Namespace(), layer), id=node_id, per_type=int(per_type))
        return rows[0] if rows else None

    def nav_neighbourhood_2(self, ids: list[tuple[str, str]], root: str, per_type: int) -> dict[str, Any]:
        from vkm_corpus.graph.nav_query import cy_neighbourhood_2
        from vkm_corpus.graph.schema import Namespace

        out: dict[str, Any] = {}
        for layer in sorted({layer for _i, layer in ids}):
            wanted = [i for i, lay in ids if lay == layer]
            for row in self._nav_read(cy_neighbourhood_2(Namespace(), layer), ids=wanted, root=root,
                                      per_type=int(per_type)):
                out[row["mid"]] = row.get("groups") or []
        return out
    # ---------------------------------------------------------------- end NAV graph (agent G)


# ---------------------------------------------------------------------------------------------------- rerank
def _rerank_failure(exc: Exception, kind: str) -> ApiFailure:
    status = getattr(exc, "status", None)
    body = getattr(exc, "error", None)
    code = getattr(body, "code", None)
    if status == 413:
        api_code = "PAYLOAD_TOO_LARGE"
    elif status in (400, 422):
        api_code = "INVALID_ARGUMENT"
    elif status == 504:
        api_code = "DEPENDENCY_TIMEOUT"
    elif status in (503, None):
        api_code = "DEPENDENCY_UNAVAILABLE"
    else:
        api_code = "DEPENDENCY_ERROR"
    return ApiFailure(api_code, f"{kind} reranker: {code or type(exc).__name__}", stage=f"rerank_{kind}",
                      tool="vkm-rerank-gateway", details={"gateway_status": status, "gateway_code": code})


class GatewayRerankBackend:
    """Agent F's async client; one instance per process."""

    def __init__(self, settings: Any) -> None:
        from vkm_corpus.retrieval.client import RerankClient

        if not settings.rerank_url:
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", "VKM_RERANK_URL is not configured", stage="rerank",
                             tool="vkm-rerank-gateway")
        self._client = RerankClient(settings.rerank_url, settings.rerank_token)

    async def _call(self, kind: str, coro: Any) -> dict[str, Any]:
        import httpx

        from vkm_corpus.retrieval.client import RerankClientError
        from vkm_corpus.retrieval.models import RerankLimitError

        try:
            response = await coro
        except RerankLimitError as exc:
            raise ApiFailure("PAYLOAD_TOO_LARGE", str(exc), stage=f"rerank_{kind}", tool="vkm-rerank-gateway") \
                from exc
        except RerankClientError as exc:
            raise _rerank_failure(exc, kind) from exc
        except httpx.TimeoutException as exc:
            raise ApiFailure("DEPENDENCY_TIMEOUT", f"{kind} reranker timed out", stage=f"rerank_{kind}",
                             tool="vkm-rerank-gateway") from exc
        except httpx.HTTPError as exc:
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"{kind} reranker not reachable ({type(exc).__name__})",
                             stage=f"rerank_{kind}", tool="vkm-rerank-gateway") from exc
        return response.model_dump(mode="json")

    async def rerank_text(self, query: str, candidates: list[tuple[str, str]], top_n: int | None,
                          request_id: str | None, truncate_to_tokens: int | None) -> dict[str, Any]:
        return await self._call("text", self._client.rerank_text(query, candidates, top_n, request_id,
                                                                  truncate_to_tokens))

    async def rerank_visual(self, query: str, candidates: list[tuple[str, bytes]], top_n: int | None,
                            request_id: str | None) -> dict[str, Any]:
        return await self._call("visual", self._client.rerank_visual(query, candidates, top_n, request_id))

    async def status(self) -> dict[str, Any]:
        return await self._call("status", self._client.status())


# ---------------------------------------------------------------------------------------------------- control plane
class PgControlPlane:
    """``vkm_corpus.ops.jobs`` over short-lived connections (the API never needs PostgreSQL to read the canon)."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def _conn(self) -> Any:
        from vkm_corpus.ops import jobs

        try:
            return jobs.connect(self.settings)
        except Exception as exc:  # noqa: BLE001 - psycopg/ConfigError: no address or DSN in the message
            raise ApiFailure("DEPENDENCY_UNAVAILABLE", f"control plane not reachable ({type(exc).__name__})",
                             stage="control_plane", tool="postgres") from exc

    def _run(self, fn: Any, conflict: str = "PLAN_CHANGED") -> Any:
        from vkm_corpus.ops.jobs import JobStateError

        conn = self._conn()
        try:
            return fn(conn)
        except JobStateError as exc:
            raise ApiFailure(conflict, str(exc), stage="control_plane", tool="postgres") from exc
        except ApiFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ApiFailure("DEPENDENCY_ERROR", f"control plane error ({type(exc).__name__})",
                             stage="control_plane", tool="postgres") from exc
        finally:
            conn.close()

    def request_plan(self, kind: str, requested_by: str, *, source_id: str | None, page_id: str | None,
                     request: dict[str, Any]) -> int:
        from vkm_corpus.ops import jobs

        return self._run(lambda c: jobs.request_plan(c, kind, requested_by, source_id=source_id, page_id=page_id,
                                                     request=request))

    def job(self, job_id: int) -> dict[str, Any] | None:
        def fetch(conn: Any) -> dict[str, Any] | None:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ops.job WHERE job_id = %s", (job_id,))
                row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None

        return self._run(fetch)

    def active_jobs(self, kind: str) -> list[dict[str, Any]]:
        from vkm_corpus.ops.jobs import ACTIVE

        def fetch(conn: Any) -> list[dict[str, Any]]:
            with conn.cursor() as cur:
                cur.execute("SELECT job_id, kind, state, source_id, page_id, requested_at FROM ops.job "
                            "WHERE kind = %s AND state = ANY(%s) ORDER BY job_id", (kind, list(ACTIVE)))
                rows = cur.fetchall()
            conn.commit()
            return [dict(r) for r in rows]

        return self._run(fetch)

    def confirm(self, job_id: int, plan_sha256: str, confirmed_by: str) -> dict[str, Any]:
        from vkm_corpus.ops import jobs

        return dict(self._run(lambda c: jobs.confirm(c, job_id, plan_sha256, confirmed_by)))

    def cancel(self, job_id: int, by: str, note: str) -> dict[str, Any]:
        from vkm_corpus.ops import jobs

        def run(conn: Any) -> dict[str, Any]:
            jobs.cancel(conn, job_id, by, note)
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ops.job WHERE job_id = %s", (job_id,))
                row = cur.fetchone()
            conn.commit()
            return dict(row) if row else {"job_id": job_id, "state": "CANCELLED"}

        return self._run(run, conflict="JOB_STATE_CONFLICT")

    def status(self) -> dict[str, Any]:
        from vkm_corpus.ops import jobs

        out = self._run(jobs.status)
        # roles only (H-40): worker ids may carry host names
        out["workers"] = [{"host_role": w.get("host_role"), "kind": w.get("kind"), "state": w.get("state")}
                          for w in out.get("workers", [])]
        return out


# ---------------------------------------------------------------------------------------------------- blobs
class ArtifactBlobs:
    """Read-only access to stored artifact bytes; the content address is verified on every read."""

    def __init__(self, artifacts_root: Path) -> None:
        self.root = Path(artifacts_root)

    def read(self, artifact_id: str, storage_relpath: str | None, max_bytes: int) -> bytes:
        if not grammar.matches("artifact", artifact_id):
            raise ApiFailure("INVALID_ID", f"{artifact_id!r} is not an artifact id")
        if not storage_relpath:
            raise ApiFailure("ARTIFACT_NOT_MATERIALIZED", "the artifact is reproducible but its bytes are not stored",
                             object_id=artifact_id)
        try:
            grammar.check_relative_path(storage_relpath)
        except ValueError as exc:
            raise ApiFailure("INTERNAL", "invalid storage path in the artifact index", object_id=artifact_id) from exc
        path = self.root / PurePosixPath(storage_relpath)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ApiFailure("ARTIFACT_NOT_MATERIALIZED", "artifact bytes are missing from the data root",
                             object_id=artifact_id, stage="artifact_read") from exc
        if size > max_bytes:
            raise ApiFailure("PAYLOAD_TOO_LARGE", f"artifact is larger than {max_bytes} bytes", object_id=artifact_id,
                             details={"bytes": size})
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact_id[7:]:
            raise ApiFailure("ARTIFACT_HASH_MISMATCH", "stored bytes do not match the artifact id",
                             object_id=artifact_id, stage="artifact_read")
        return data
