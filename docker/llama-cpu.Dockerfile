# syntax=docker/dockerfile:1.7
# llama.cpp (llama-server) for CPU, fronted by llama-swap (same API as the GPU engine).
ARG LLAMA_CPP_REF=b12000
ARG LLAMA_SWAP_VERSION=260

FROM ubuntu:24.04 AS build
ARG LLAMA_CPP_REF
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential cmake libcurl4-openssl-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${LLAMA_CPP_REF} https://github.com/ggml-org/llama.cpp.git .
# Portable build: every x86 CPU variant is compiled and the best one is picked at runtime
# (AVX2 on an i9-10850K), so the image also works on another host.
RUN cmake -B build \
      -DGGML_NATIVE=OFF \
      -DGGML_BACKEND_DL=ON \
      -DGGML_CPU_ALL_VARIANTS=ON \
      -DLLAMA_CURL=OFF \
      -DLLAMA_BUILD_TESTS=OFF \
      -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build --config Release -j"$(nproc)" --target llama-server \
 && mkdir -p /out/lib \
 && cp build/bin/llama-server /out/ \
 && find build -name "*.so*" -exec cp -P {} /out/lib/ \;

FROM ubuntu:24.04 AS swap
ARG LLAMA_SWAP_VERSION
ADD https://github.com/mostlygeek/llama-swap/releases/download/v${LLAMA_SWAP_VERSION}/llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz /tmp/ls.tgz
RUN tar -xzf /tmp/ls.tgz -C /tmp && install -m 0755 /tmp/llama-swap /llama-swap

FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10002 llama \
 && useradd --system --uid 10002 --gid llama --home-dir /app --shell /usr/sbin/nologin llama
COPY --from=build /out/llama-server /app/llama-server
COPY --from=build /out/lib/ /app/
COPY --from=swap /llama-swap /app/llama-swap
ENV LD_LIBRARY_PATH=/app
USER llama
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/app/llama-swap", "-listen", ":8080", "-config", "/config/llama-swap.yaml"]
