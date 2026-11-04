# syntax=docker/dockerfile:1.7
# homelab-agent application image (gateway + orchestrator + router + web panel).
ARG PYTHON_IMAGE=python:3.12.16-slim-bookworm
ARG UV_IMAGE=astral/uv:0.11.32

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="homelab-agent" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/FrancoisPJ/HomeLab-Agent"
RUN groupadd --system --gid 10001 agent \
 && useradd --system --uid 10001 --gid agent --home-dir /app --shell /usr/sbin/nologin agent \
 && mkdir -p /data /config \
 && chown agent:agent /data
COPY --from=build --chown=root:root /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HLA_CONFIG=/config/config.yaml \
    HLA_WEB_BIND=0.0.0.0
USER agent
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4)" || exit 1
ENTRYPOINT ["homelab-agent"]
CMD ["run"]
