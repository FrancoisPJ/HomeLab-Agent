# syntax=docker/dockerfile:1.7
# llama.cpp (llama-server) for CPU, fronted by llama-swap (same API as the GPU engine).
ARG LLAMA_CPP_REF=b12000
# Commit of LLAMA_CPP_REF, verified after clone (tags can be moved).
ARG LLAMA_CPP_COMMIT=68d20b884c3878217de79eade0a249efde96ab38
ARG LLAMA_SWAP_VERSION=260
# From llama-swap_${LLAMA_SWAP_VERSION}_checksums.txt of the GitHub release.
# Known: v260..v269 embed golang.org/x/net 0.59 (HTTP/2 CVE, fixed in 0.60). Accepted risk:
# Go only serves HTTP/2 over TLS and llama-swap is reachable on the compose network only.
# See docs/troubleshooting.md. Bump when a release ships x/net >= 0.60.
ARG LLAMA_SWAP_SHA256=d856a908507560cbdc253300bcf49092c7ead3c85098687428b0c9d4832ff46d

FROM ubuntu:24.04 AS build
ARG LLAMA_CPP_REF
ARG LLAMA_CPP_COMMIT
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential cmake libcurl4-openssl-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${LLAMA_CPP_REF} https://github.com/ggml-org/llama.cpp.git . \
 && test "$(git rev-parse HEAD)" = "${LLAMA_CPP_COMMIT}" \
 || (echo "llama.cpp ${LLAMA_CPP_REF} is not commit ${LLAMA_CPP_COMMIT}" >&2; exit 1)
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
ARG LLAMA_SWAP_SHA256
ADD https://github.com/mostlygeek/llama-swap/releases/download/v${LLAMA_SWAP_VERSION}/llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz /tmp/ls.tgz
RUN echo "${LLAMA_SWAP_SHA256}  /tmp/ls.tgz" | sha256sum -c - \
 && tar -xzf /tmp/ls.tgz -C /tmp \
 && install -m 0755 /tmp/llama-swap /llama-swap

FROM ubuntu:24.04
RUN apt-get update \
 && apt-get -y upgrade \
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
