# syntax=docker/dockerfile:1.7
# whisper.cpp speech-to-text server (CPU), OpenAI-style endpoint /v1/audio/transcriptions.
# The model file comes from models.items (engine: whisper) and is selected through
# engines/whisper.env, rendered by `make engines`.
ARG WHISPER_CPP_REF=v1.9.4
ARG WHISPER_CPP_COMMIT=927cfce34f31707e17f2bff35c349632fb9e2c3a

FROM ubuntu:24.04 AS build
ARG WHISPER_CPP_REF
ARG WHISPER_CPP_COMMIT
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential cmake \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${WHISPER_CPP_REF} https://github.com/ggml-org/whisper.cpp.git . \
 && test "$(git rev-parse HEAD)" = "${WHISPER_CPP_COMMIT}" \
 || (echo "whisper.cpp ${WHISPER_CPP_REF} is not commit ${WHISPER_CPP_COMMIT}" >&2; exit 1)
RUN cmake -B build \
      -DGGML_NATIVE=OFF \
      -DGGML_BACKEND_DL=ON \
      -DGGML_CPU_ALL_VARIANTS=ON \
      -DWHISPER_BUILD_TESTS=OFF \
      -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build --config Release -j"$(nproc)" --target whisper-server \
 && mkdir -p /out/lib \
 && cp build/bin/whisper-server /out/ \
 && find build -name "*.so*" -exec cp -P {} /out/lib/ \;

FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10004 whisper \
 && useradd --system --uid 10004 --gid whisper --home-dir /app --shell /usr/sbin/nologin whisper
COPY --from=build /out/whisper-server /app/whisper-server
COPY --from=build /out/lib/ /app/
ENV LD_LIBRARY_PATH=/app \
    WHISPER_MODEL=ggml-small.bin \
    WHISPER_LANGUAGE=fr \
    WHISPER_THREADS=4
USER whisper
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
# --convert: ffmpeg turns Telegram's OGG/Opus voice notes into WAV
ENTRYPOINT ["/bin/sh", "-c", "exec /app/whisper-server --host 0.0.0.0 --port 8080 --convert --tmp-dir /tmp --inference-path /v1/audio/transcriptions -m \"/models/${WHISPER_MODEL}\" -l \"${WHISPER_LANGUAGE}\" -t \"${WHISPER_THREADS}\" \"$@\"", "--"]
