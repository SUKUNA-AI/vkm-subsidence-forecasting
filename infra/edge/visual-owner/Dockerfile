# New owned GPU image. Existing services and their images are not modified.
# Build context contains independently checked offline inputs, not model files.
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:13.2.1-devel-ubuntu24.04@sha256:44a9504c6dfb50b1241464241b02a93871928f373de6f5a644cf5fe9f080aa63
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:13.2.1-runtime-ubuntu24.04@sha256:4c17c2e7474ca1d470428b25e87e95756f51e7da3b663859894a03bc5a7ebdae
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
FROM ${PYTHON_IMAGE} AS python

FROM ${CUDA_DEVEL_IMAGE} AS native
LABEL vkm.owner-qualification="20261004-native-owners" vkm.owner.stage="native-builder"
RUN test "$(cat /sys/fs/cgroup/memory.max)" = "8589934592" \
 && test "$(cat /sys/fs/cgroup/memory.swap.max)" = "0" \
 && test "$(cat /sys/fs/cgroup/cpu.max)" = "200000 100000" \
 && printf '%s\n' 'memory.max=8589934592' 'memory.swap.max=0' 'cpu.max=200000 100000' > /BUILD_CGROUP_LIMITS.txt
COPY builder-debs/ /debs/
RUN set -eu; cd /debs; sha256sum -c SHA256SUMS; \
    set -- /debs/*.deb; if [ -f "$1" ]; then dpkg -i "$@"; fi
COPY llama-source.tar /upstream-source.tar
RUN printf '%s  %s\n' 'eef03eca6f90860158bc1b237845a60492520270ab88b023f9f5cafefcb8d5f1' /upstream-source.tar | sha256sum -c - \
 && mkdir /src && tar -xf /upstream-source.tar -C /src
COPY patches/ /patches/
COPY vkm-loaded-lifetime.h /src/tools/server/vkm-loaded-lifetime.h
COPY vkm-weight-placement.h /src/tools/server/vkm-weight-placement.h
RUN cd /src && for patch in /patches/*.patch; do git apply "$patch"; done \
 && cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DGGML_NATIVE=OFF -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=75 \
      -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DLLAMA_CURL=OFF \
      -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
 && timeout 1800s nice -n 10 cmake --build build -j2 --target llama-server \
 && mkdir /out && cp build/bin/llama-server /BUILD_CGROUP_LIMITS.txt /out/ \
 && find build -name '*.so*' -exec cp -P '{}' /out/ ';' \
 && cd /out && sha256sum llama-server *.so* > SHA256SUMS

FROM ${CUDA_RUNTIME_IMAGE}
LABEL vkm.owner-qualification="20261004-native-owners" vkm.owner.stage="runtime-builder"
COPY runtime-debs/ /debs/
RUN set -eu; cd /debs; sha256sum -c SHA256SUMS; \
    set -- /debs/*.deb; if [ -f "$1" ]; then dpkg -i "$@"; fi
# Bookworm CPython runs on the newer Ubuntu libc only after the actual guard.
# No Ubuntu native binary is copied into the older Bookworm runtime.
COPY --from=python /usr/local/ /usr/local/
COPY --from=native /out/ /app/
ENV LD_LIBRARY_PATH=/usr/local/lib:/app:/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -I -c "import platform,ssl,bz2,lzma,sqlite3,ctypes,readline,uuid; assert platform.python_version()=='3.13.15'; assert ssl.OPENSSL_VERSION_INFO[0]==3; assert sqlite3.connect(':memory:').execute('select 1').fetchone()==(1,); assert bz2.decompress(bz2.compress(b'abi'))==b'abi'; assert lzma.decompress(lzma.compress(b'abi'))==b'abi'"
COPY wheelhouse/ /opt/owner-wheels/
COPY dependencies.lock.txt vkm_world-0.1.0-py3-none-any.whl /opt/owner-wheels/
COPY gpu_preflight.py /opt/vkm-gpu-preflight.py
RUN python -m pip install --no-index --find-links=/opt/owner-wheels --require-hashes -r /opt/owner-wheels/dependencies.lock.txt \
 && python -m pip install --no-index --no-deps /opt/owner-wheels/vkm_world-0.1.0-py3-none-any.whl \
 && python -m pip check \
 && python -I -c "import vkm_corpus.update.visual_owner_bridge; import numpy,pydantic,httpx; assert numpy.__version__=='2.5.3'; assert pydantic.__version__=='2.13.5'; assert httpx.__version__=='0.28.1'" \
 && python -m pip freeze > /opt/owner-wheels/installed-versions.txt
RUN ldd /app/llama-server > /app/LDD_SERVER.txt \
 && ! grep -F 'not found' /app/LDD_SERVER.txt \
 && /app/llama-server --version > /app/NATIVE_VERSION.txt
ARG PUBLIC_SOURCE_COMMIT
ARG BUILD_INPUTS_SHA256
LABEL vkm.owner.role="native-visual-gpu-owner" vkm.owner.qualification="NOT_RUN" \
      vkm.owner.public-source="${PUBLIC_SOURCE_COMMIT}" vkm.owner.inputs-sha256="${BUILD_INPUTS_SHA256}" \
      vkm.owner.llama-source="4da6337767f973e2b4d0797e5b323d77d8565e4a"
USER 10001:10001
ENTRYPOINT ["python", "/opt/vkm-gpu-preflight.py"]
