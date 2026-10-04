# This is a NEW isolated owner image recipe. No image was built in this task.
# The existing Python-3.12 CareerOps image deliberately fails its first gate.
ARG QUALIFIED_BASE_IMAGE
FROM ${QUALIFIED_BASE_IMAGE}
ARG QUALIFIED_BASE_IMAGE
RUN python -c "import re,os; assert re.fullmatch(r'[A-Za-z0-9._/:+-]+@sha256:[0-9a-f]{64}', os.environ.get('QUALIFIED_BASE_IMAGE', ''))" \
    && python -c "import platform,torch,importlib.metadata as m; assert platform.python_version()=='3.13.13'; assert m.version('torch') in ('2.14.0','2.14.0+cu132'); assert torch.version.cuda=='13.2'; assert m.version('transformers')=='4.57.3'; assert m.version('pydantic')=='2.13.5'"
# Build context is independently verified by TextOwnerImagePlan. It contains
# exact prepared wheels and a fully hashed dependency lock, never raw models.
COPY wheels/ /opt/owner-wheels/
COPY dependencies.lock.txt /opt/owner-wheels/
RUN python -m pip install --no-index --find-links=/opt/owner-wheels --require-hashes -r /opt/owner-wheels/dependencies.lock.txt \
    && python -m pip install --no-index --no-deps /opt/owner-wheels/vkm_world-0.1.0-py3-none-any.whl /opt/owner-wheels/careerops_reranker_native-0.1.0-py3-none-any.whl \
    && python -m pip check
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "vkm_corpus.update.text_owner_bridge"]
