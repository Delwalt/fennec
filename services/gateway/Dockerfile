# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
RUN python -m pip install --no-cache-dir uv==0.7.12
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 fennec \
    && useradd --uid 10001 --gid fennec --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin fennec
WORKDIR /app
COPY --from=builder --chown=fennec:fennec /app/.venv /app/.venv
COPY --chown=fennec:fennec fennec_gateway /app/fennec_gateway
USER 10001:10001
EXPOSE 8080/tcp
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=2)"]
ENTRYPOINT ["uvicorn", "fennec_gateway.app:app"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
