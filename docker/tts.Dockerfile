# syntax=docker/dockerfile:1.7
# Text-to-speech for voice replies: Piper (GPL-3.0, separate program) + an OpenAI-style
# /v1/audio/speech gateway (docker/tts/server.py, Apache-2.0) returning OGG/Opus.
# The voice files come from models.items (engine: tts), selected through engines/tts.env.
ARG PYTHON_IMAGE=python:3.12.16-slim-bookworm
ARG PIPER_VERSION=1.8.0

FROM ${PYTHON_IMAGE} AS tts-build
ARG PIPER_VERSION
RUN apt-get update \
 && apt-get -y upgrade \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10005 --home-dir /app --shell /usr/sbin/nologin tts \
 && pip install --no-cache-dir "piper-tts[http]==${PIPER_VERSION}"
COPY docker/tts/server.py /app/server.py
ENV PYTHONUNBUFFERED=1 \
    TTS_VOICE=fr_FR-siwis-medium \
    TTS_MODELS_DIR=/models
USER tts
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["python", "/app/server.py"]
