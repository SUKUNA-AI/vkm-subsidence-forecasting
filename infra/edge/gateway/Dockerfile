# vkm-rerank-gateway (FastAPI) on EDGE: contract vkm.rerank/1, token auth, per-backend limits (CP-18, H-13).
# Build context = this directory + pyproject.toml + complete src/ (infra/edge/build_images.sh).
# No GPU inside: the gateway only talks HTTP to the text service and to llama-server on loopback.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
FROM ${PYTHON_IMAGE} AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY src/ src/
RUN python -m pip install --no-cache-dir setuptools==78.1.0 \
 && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/vkm
COPY requirements.txt /opt/vkm/requirements.txt
COPY --from=build /wheels/ /opt/vkm/wheels/
RUN python -m pip install --no-cache-dir -r /opt/vkm/requirements.txt \
 && python -m pip install --no-deps /opt/vkm/wheels/*.whl \
 && python -m pip check \
 && python -m pip freeze > /opt/vkm/pip-freeze.txt
RUN python -I -c "import vkm_corpus.retrieval.gateway, vkm_corpus.retrieval.cli; from vkm_corpus.update.service_identity import RERANK, code_inventory, dependency_inventory; import json; from pathlib import Path; c=code_inventory(RERANK); d=dependency_inventory(RERANK); Path('/opt/vkm/service-identity.json').write_text(json.dumps({'code':c,'dependencies':d},sort_keys=True)); Path('/opt/vkm/SOURCE_SHA256SUMS').write_text(''.join(h+'  '+m+'\\n' for m,h in sorted(c['modules'].items())))"
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18084/health', timeout=4).read()"
ENTRYPOINT ["python", "-m", "vkm_corpus.cli", "rerank", "serve"]
