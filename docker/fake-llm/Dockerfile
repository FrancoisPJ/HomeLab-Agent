# syntax=docker/dockerfile:1.7
# Fake services (OpenAI-compatible LLM, Home Assistant, Telegram Bot API) for CI and the
# `dev` compose profile. Build from the repository root:
#   docker build -f docker/fake-llm/Dockerfile -t homelab-agent-fakes .
ARG PYTHON_IMAGE=python:3.12.16-slim-bookworm
ARG UV_IMAGE=astral/uv:0.11.32
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE}
COPY --from=uv /uv /bin/uv
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable \
 && useradd --system --uid 10003 fake
USER fake
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)" || exit 1
ENTRYPOINT ["homelab-agent", "fake"]
CMD ["llm", "--port", "8080"]
