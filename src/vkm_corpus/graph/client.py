"""Neo4j connection for the projector and its readers (driver ``neo4j`` 6.x, imported lazily).

Credentials come from ``vkm_corpus.config`` (``VKM_NEO4J_USER`` and ``VKM_NEO4J_PASSWORD[_FILE]``); they are never
logged. A password file in the ``NEO4J_AUTH`` format (``<user>/<password>``, as mounted into the Neo4j container) is
accepted as is.
"""
from __future__ import annotations

from typing import Any

from vkm_corpus.config import Settings
from vkm_corpus.graph.common import ProjectionError


def split_auth(user: str, secret: str) -> tuple[str, str]:
    """``secret`` may be a bare password or ``<user>/<password>`` (NEO4J_AUTH file format)."""
    if secret.startswith(f"{user}/"):
        return user, secret[len(user) + 1:]
    return user, secret


def neo4j_auth(settings: Settings) -> tuple[str, str]:
    if not settings.neo4j_password:
        raise ProjectionError("E_NO_CREDENTIALS", "VKM_NEO4J_PASSWORD_FILE (or VKM_NEO4J_PASSWORD) is not set",
                              stage="connect")
    return split_auth(settings.neo4j_user, settings.neo4j_password)


def connect(settings: Settings | None = None, *, uri: str | None = None, auth: tuple[str, str] | None = None) -> Any:
    """Open and verify a driver. ``uri``/``auth`` override the settings (live tests)."""
    from neo4j import GraphDatabase
    from neo4j.exceptions import AuthError, ServiceUnavailable

    uri = uri or (settings.neo4j_uri if settings else None)
    if not uri:
        raise ProjectionError("E_NO_SERVICE", "VKM_NEO4J_URI is not set", stage="connect")
    if auth is None:
        if settings is None:
            raise ProjectionError("E_NO_CREDENTIALS", "no Neo4j credentials", stage="connect")
        auth = neo4j_auth(settings)
    # UNRECOGNIZED notifications ("label/property does not exist yet") are expected on an empty layer; keep the rest
    driver = GraphDatabase.driver(uri, auth=auth, connection_timeout=15, max_transaction_retry_time=60,
                                  notifications_disabled_classifications=["UNRECOGNIZED"])
    try:
        driver.verify_connectivity()
    except AuthError as exc:
        driver.close()
        raise ProjectionError("E_NO_CREDENTIALS", "Neo4j rejected the credentials", stage="connect") from exc
    except (ServiceUnavailable, OSError) as exc:
        driver.close()
        raise ProjectionError("E_SERVER_UNAVAILABLE", f"Neo4j is not reachable: {type(exc).__name__}",
                              stage="connect", retryable=True) from exc
    return driver


def server_info(driver: Any, database: str) -> dict[str, Any]:
    records, _, _ = driver.execute_query("CALL dbms.components() YIELD name, versions, edition "
                                         "RETURN name, versions, edition", database_=database, routing_="r")
    out = {"name": None, "version": None, "edition": None}
    for rec in records:
        if rec["name"] == "Neo4j Kernel":
            out = {"name": rec["name"], "version": (rec["versions"] or [None])[0], "edition": rec["edition"]}
    return out


def read(driver: Any, database: str, query: str, **params: Any) -> list[dict[str, Any]]:
    """Run a read query in a READ transaction (``routing_=READ``) and return plain dicts."""
    records, _, _ = driver.execute_query(query, parameters_=params, database_=database, routing_="r")
    return [dict(r) for r in records]


def write(driver: Any, database: str, query: str, timeout: float | None = 300.0, **params: Any) -> list[dict[str, Any]]:
    """Run a write in a managed transaction (retried by the driver on transient errors)."""
    from neo4j import Query

    records, _, _ = driver.execute_query(Query(query, timeout=timeout), parameters_=params, database_=database)
    return [dict(r) for r in records]


def run_autocommit(driver: Any, database: str, query: str, **params: Any) -> dict[str, Any]:
    """Auto-commit transaction (needed for ``CALL {…} IN TRANSACTIONS``); returns the summary counters."""
    with driver.session(database=database) as session:
        summary = session.run(query, params).consume()
    counters = summary.counters
    return {"nodes_deleted": counters.nodes_deleted, "relationships_deleted": counters.relationships_deleted,
            "nodes_created": counters.nodes_created}
