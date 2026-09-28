"""OpenSearch connection (``opensearch-py`` 3.x, imported lazily). The server runs without the security plugin and
listens on CORE loopback only (DN-E12); clients on other hosts use an SSH tunnel."""
from __future__ import annotations

from typing import Any

from vkm_corpus.config import Settings
from vkm_corpus.graph.common import ProjectionError


def connect(settings: Settings | None = None, *, url: str | None = None, timeout: int = 120) -> Any:
    from opensearchpy import OpenSearch
    from opensearchpy.exceptions import ConnectionError as OSConnectionError

    url = url or (settings.opensearch_url if settings else None)
    if not url:
        raise ProjectionError("E_NO_SERVICE", "VKM_OPENSEARCH_URL is not set", stage="connect")
    client = OpenSearch(hosts=[url], http_compress=True, timeout=timeout, max_retries=3, retry_on_timeout=True)
    try:
        client.info()
    except OSConnectionError as exc:
        raise ProjectionError("E_SERVER_UNAVAILABLE", "OpenSearch is not reachable", stage="connect",
                              retryable=True) from exc
    return client


def server_info(client: Any) -> dict[str, Any]:
    info = client.info()
    version = info.get("version", {})
    return {"distribution": version.get("distribution", "opensearch"), "version": version.get("number"),
            "build_hash": version.get("build_hash"), "lucene_version": version.get("lucene_version")}
