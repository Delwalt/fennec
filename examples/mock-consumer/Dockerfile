ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10002 consumer \
    && useradd --uid 10002 --gid consumer --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin consumer
WORKDIR /app
COPY --chown=consumer:consumer server.py /app/server.py
USER 10002:10002
EXPOSE 8090/tcp
HEALTHCHECK --interval=10s --timeout=3s --start-period=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2)"]
ENTRYPOINT ["python", "/app/server.py"]
