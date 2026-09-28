# vkm-rerank-gateway (FastAPI) on EDGE: contract vkm.rerank/1, token auth, per-backend limits (CP-18, H-13).
# Build context = this directory + src/vkm_corpus copied next to it (infra/edge/build_images.sh does it).
# No GPU inside: the gateway only talks HTTP to the text service and to llama-server on loopback.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/vkm
COPY requirements.txt /opt/vkm/requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/vkm/requirements.txt \
 && python -m pip freeze > /opt/vkm/pip-freeze.txt
COPY src/vkm_corpus /opt/vkm/src/vkm_corpus
ENV PYTHONPATH=/opt/vkm/src
RUN python -c "import vkm_corpus.retrieval.gateway, vkm_corpus.retrieval.cli" \
 && find /opt/vkm/src -name "*.py" -print0 | sort -z | xargs -0 sha256sum > /opt/vkm/SOURCE_SHA256SUMS
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18084/health', timeout=4).read()"
ENTRYPOINT ["python", "-m", "vkm_corpus.cli", "rerank", "serve"]
