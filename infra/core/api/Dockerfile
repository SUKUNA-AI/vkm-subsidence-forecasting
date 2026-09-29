# VKM API + VKM Corpus MCP image (CORE, compose project vkm-core; decisions CP-19, H-26).
#
# One image, four services of infra/core/compose.yml: `api` (`vkm-corpus api serve`), `mcp` (`mcp serve --kind read`),
# `mcp-admin` (`--kind admin`) and the one-shot `vkm-job` (`core reconcile`, `canon ...`). No secrets in the image:
# tokens, the Neo4j password and the PostgreSQL DSN are compose secrets read through *_FILE variables. The canonical
# root is mounted at /data (read-only for api/mcp). Compose runs every service as the owner of the data root
# (`user: ${VKM_DATA_UID}:${VKM_DATA_GID}`); the image's own non-root user (10001) is only the default for plain
# `docker run`.
#
# Build from the repository root (the build context is limited by Dockerfile.dockerignore next to this file):
#   docker build -f infra/core/api/Dockerfile -t vkm-corpus-api:0.1.0 .
# Base image pinned by digest: python:3.13-slim (3.13.15-slim-trixie, index digest resolved 2026-09-28).
ARG PYTHON_IMAGE=python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY pyproject.toml ./
COPY requirements/ requirements/
COPY src/ src/
# pydantic/pydantic-core stay at the WorldSpec pins (CP-02); a services lock is used when the coordinator adds it.
# From the `corpus` extra the API and the CORE jobs need only DuckDB, pyarrow, Pillow, psycopg and pytz (DuckDB
# returns TIMESTAMPTZ values through pytz) — not the extractors — so those are installed with the extra's bounds.
# The `navigation` extra (pymorphy3 + dictionaries) lemmatizes concept queries the same way the NAV build does.
RUN python -m venv /opt/vkm \
 && /opt/vkm/bin/pip install --upgrade pip \
 && CONSTRAINTS="-c requirements/worldspec.lock.txt" \
 && if [ -f requirements/corpus-services.lock.txt ]; then CONSTRAINTS="$CONSTRAINTS -c requirements/corpus-services.lock.txt"; fi \
 && /opt/vkm/bin/pip install $CONSTRAINTS ".[corpus-services,navigation]" \
      "duckdb>=1.5.5,<2" "pyarrow>=25.0.1,<26" "pillow>=12.3" "psycopg[binary]>=3.3" "pytz>=2025.2" \
 && /opt/vkm/bin/pip check \
 && /opt/vkm/bin/python -c "import pathlib, vkm_corpus; r = pathlib.Path(vkm_corpus.__file__).parent; \
missing = [g for g in ('duckdb/sql/*.sql', 'ops/schema.sql') if not list(r.glob(g))]; \
assert not missing, f'package data missing from the wheel: {missing} (pyproject [tool.setuptools.package-data])'"

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 vkm \
 && useradd --system --uid 10001 --gid vkm --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin vkm
COPY --from=build /opt/vkm /opt/vkm
ENV PATH=/opt/vkm/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    VKM_DATA_ROOT=/data VKM_DATA_ROLE=canonical
USER vkm:vkm
WORKDIR /tmp
EXPOSE 8000 8765 8766
ENTRYPOINT ["vkm-corpus"]
CMD ["api", "serve", "--host", "0.0.0.0", "--port", "8000"]
