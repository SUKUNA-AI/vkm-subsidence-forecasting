# VKM RX580 retrieval backend: llama.cpp / ggml Vulkan (RADV) for AMD Polaris (gfx803), CORE only.
#
# Stages:
#   toolchain — Debian trixie + Vulkan SDK pieces (glslc, headers) + Mesa RADV; also the research/dev container;
#   build     — pinned llama.cpp commit + VKM patches (sha256 in patches/SHA256SUMS), Vulkan backend, static libs;
#   runtime   — Mesa RADV + llama-server / llama-embedding / llama-bench / test-backend-ops, no compilers.
#
# The GPU is reached through /dev/dri (render node) — no ROCm, no kernel changes. Build on CORE:
#   docker build -f infra/core/rx580/Dockerfile --target runtime -t vkm-rx580-llama:<commit12>-vk infra/core/rx580
# Base image is pinned by digest (Mesa comes from the same Debian release as the host: RADV 25.0.x).

ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a

FROM ${DEBIAN_IMAGE} AS toolchain
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build git ca-certificates pkg-config \
      glslc libvulkan-dev spirv-headers \
      mesa-vulkan-drivers libvulkan1 vulkan-tools \
      python3 python3-numpy \
 && rm -rf /var/lib/apt/lists/*

FROM toolchain AS build
ARG JOBS=8
ARG LLAMA_CPP_REPO=https://github.com/ggml-org/llama.cpp
ARG LLAMA_CPP_COMMIT=4da6337767f973e2b4d0797e5b323d77d8565e4a
WORKDIR /src/llama.cpp
RUN git init -q . && git remote add origin "${LLAMA_CPP_REPO}" \
 && git fetch -q --depth 1 origin "${LLAMA_CPP_COMMIT}" && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${LLAMA_CPP_COMMIT}"
COPY patches/ /patches/
RUN cd /patches && if [ -s SHA256SUMS ]; then sha256sum -c SHA256SUMS; fi \
 && cd /src/llama.cpp && for p in $(ls /patches/*.patch 2>/dev/null | sort); do git apply --verbose "$p"; done
# Zen 3 host CPU (AVX2/FMA/F16C); every layer runs on the GPU, the CPU only drives the graph.
RUN cmake -S . -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
      -DGGML_VULKAN=ON -DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DGGML_BMI2=ON \
      -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=ON -DLLAMA_BUILD_EXAMPLES=ON -DLLAMA_BUILD_SERVER=ON \
 && nice -n 10 cmake --build build -j"${JOBS}" --target llama-server llama-embedding llama-bench test-backend-ops llama-quantize \
 && git rev-parse HEAD > build/bin/LLAMA_CPP_COMMIT && git diff --stat > build/bin/LLAMA_CPP_PATCHES.txt

FROM ${DEBIAN_IMAGE} AS runtime
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends mesa-vulkan-drivers libvulkan1 vulkan-tools libgomp1 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/llama.cpp/build/bin/ /opt/llama/bin/
COPY --from=build /patches/ /opt/llama/patches/
ENV PATH=/opt/llama/bin:$PATH
# RADV only: load just the radeon ICD, so a missing /dev/dri never silently falls back to llvmpipe (CPU).
# Device-local VRAM only: with ReBAR-first placement a second model spills to GTT through the 256 MiB BAR heap.
ENV VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/radeon_icd.json \
    GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1
USER 65534:65534
ENTRYPOINT ["llama-server"]
