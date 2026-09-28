# llama.cpp llama-server for the VKM visual reranker (jina-reranker-m0) on EDGE: GTX 1650 Mobile (TU117, sm_75),
# driver with CUDA 13.2. Decision CP-18 (в): built on EDGE inside the CUDA 13.2 devel image, not on the host.
# Pins: base images by digest (build args, values in the build receipt), llama.cpp by full commit.
# Backends are loadable modules (GGML_BACKEND_DL): libggml-cuda for sm_75 and all x86 CPU variants, so the CPU part
# of the partially offloaded model picks the best variant for the host (Zen 2 → haswell-class) at run time.
# patches/*.patch are applied in name order (D-F1 «skip lm_head with --embeddings», allowed by CP-18; sha256 of every
# patch is written to /app/PATCHES_SHA256 and into the deployment receipt).
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:13.2.1-devel-ubuntu24.04@sha256:44a9504c6dfb50b1241464241b02a93871928f373de6f5a644cf5fe9f080aa63
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:13.2.1-runtime-ubuntu24.04@sha256:4c17c2e7474ca1d470428b25e87e95756f51e7da3b663859894a03bc5a7ebdae

FROM ${CUDA_DEVEL_IMAGE} AS build
ARG LLAMA_CPP_COMMIT=4da6337767f973e2b4d0797e5b323d77d8565e4a
ARG CUDA_ARCHITECTURES=75
# EDGE is a laptop (6C/12T, thermal limit): at most 6 compile jobs at lowered priority (coordinator, 28.09).
# Prefer building on WORKSTATION and moving the image with `docker save | ssh edge docker load`.
ARG BUILD_JOBS=6
RUN apt-get update \
 && apt-get install -y --no-install-recommends git cmake ninja-build build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q . \
 && git remote add origin https://github.com/ggml-org/llama.cpp.git \
 && git fetch -q --depth 1 origin "${LLAMA_CPP_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${LLAMA_CPP_COMMIT}"
COPY patches/ /patches/
RUN set -e; : > /PATCHES_SHA256; for p in /patches/*.patch; do [ -e "$p" ] || continue; \
      git apply --verbose "$p"; (cd /patches && sha256sum "$(basename "$p")") >> /PATCHES_SHA256; done; \
    git diff --stat
RUN cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DGGML_NATIVE=OFF -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES}" \
      -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON \
      -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=ON -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
 && nice -n 10 cmake --build build -j"${BUILD_JOBS}" --target llama-server test-backend-ops llama-bench
RUN mkdir -p /out \
 && cp build/bin/llama-server build/bin/test-backend-ops build/bin/llama-bench /out/ \
 && find build -name "*.so*" -exec cp -P {} /out/ \; \
 && git rev-parse HEAD > /out/LLAMA_CPP_COMMIT \
 && cp /PATCHES_SHA256 /out/PATCHES_SHA256 \
 && (cd /out && sha256sum llama-server test-backend-ops llama-bench *.so* > SHA256SUMS)

FROM ${CUDA_RUNTIME_IMAGE}
ARG LLAMA_CPP_COMMIT=4da6337767f973e2b4d0797e5b323d77d8565e4a
LABEL org.opencontainers.image.title="vkm llama-server (CUDA 13.2, sm_75)" \
      org.opencontainers.image.source="https://github.com/ggml-org/llama.cpp" \
      org.opencontainers.image.revision="${LLAMA_CPP_COMMIT}" \
      vkm.role="visual-rerank backend (jina-reranker-m0)"
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/ /app/
ENV LD_LIBRARY_PATH=/app
WORKDIR /app
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=5 \
  CMD curl -fsS http://127.0.0.1:18083/health || exit 1
ENTRYPOINT ["/app/llama-server"]
