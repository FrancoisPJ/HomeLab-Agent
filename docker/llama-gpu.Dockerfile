# syntax=docker/dockerfile:1.7
# llama.cpp (llama-server) built with CUDA, fronted by llama-swap for on-demand loading.
#
# The host driver is 535 → CUDA 12.2 maximum. Keep CUDA_VERSION <= what the driver supports
# (`nvidia-smi` shows "CUDA Version"). After a driver upgrade you may raise it, see
# docs/gpu-sharing.md.
ARG CUDA_VERSION=12.2.2
ARG UBUNTU_VERSION=22.04
ARG LLAMA_CPP_REF=b12000
ARG LLAMA_SWAP_VERSION=260

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS build
ARG LLAMA_CPP_REF
# 86 = Ampere consumer (RTX 30xx). Several: "75;86;89". Fewer archs = faster build.
ARG CUDA_ARCHITECTURES=86
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential python3-pip libcurl4-openssl-dev \
 && pip3 install --no-cache-dir "cmake==3.31.6" \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${LLAMA_CPP_REF} https://github.com/ggml-org/llama.cpp.git .
RUN cmake -B build \
      -DGGML_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCHITECTURES}" \
      -DGGML_NATIVE=OFF \
      -DLLAMA_CURL=OFF \
      -DLLAMA_BUILD_TESTS=OFF \
      -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
 && cmake --build build --config Release -j"$(nproc)" --target llama-server \
 && mkdir -p /out/lib \
 && cp build/bin/llama-server /out/ \
 && find build -name "*.so*" -exec cp -P {} /out/lib/ \;

FROM ubuntu:24.04 AS swap
ARG LLAMA_SWAP_VERSION
ADD https://github.com/mostlygeek/llama-swap/releases/download/v${LLAMA_SWAP_VERSION}/llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz /tmp/ls.tgz
RUN tar -xzf /tmp/ls.tgz -C /tmp && install -m 0755 /tmp/llama-swap /llama-swap

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10002 llama \
 && useradd --system --uid 10002 --gid llama --home-dir /app --shell /usr/sbin/nologin llama
COPY --from=build /out/llama-server /app/llama-server
COPY --from=build /out/lib/ /app/lib/
COPY --from=swap /llama-swap /app/llama-swap
ENV LD_LIBRARY_PATH=/app/lib
USER llama
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/app/llama-swap", "-listen", ":8080", "-config", "/config/llama-swap.yaml"]
