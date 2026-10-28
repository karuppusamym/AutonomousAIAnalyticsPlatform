# syntax=docker/dockerfile:1
# Superset 6.1.0 for AnalystOS, built on python:3.11-slim instead of derived from apache/superset:6.1.0.
# Why not the upstream image: it fails the Grype gate (supply-chain.yml: any fixable high/critical fails) on its
# Python 3.10 interpreter and on dependencies Superset itself pins below their fixes (overrides.txt). Here the
# interpreter and Debian packages are current, the dependency set is upstream's own (constraints.txt) with only
# the CVE-fixed packages raised, and the Superset release is the PyPI wheel (the built frontend is inside it).
# Layout matches the upstream image: venv /app/.venv, user superset (uid 1000), SUPERSET_HOME /app/superset_home.
# Optional build secret `pipcert` (both stages): a CA bundle for environments behind a TLS-inspecting proxy.
ARG SUPERSET_VERSION=6.1.0

FROM python:3.11-slim AS build
ARG SUPERSET_VERSION
COPY constraints.txt overrides.txt /tmp/superset/
# psycopg2: Superset's own metadata DB and the AnalystOS analytics connection are PostgreSQL.
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert SSL_CERT_FILE=/run/secrets/pipcert; fi; \
    pip install --no-cache-dir "uv==0.13.6" \
    && uv venv /app/.venv \
    && uv pip install --no-cache --python /app/.venv/bin/python \
        --constraints /tmp/superset/constraints.txt --overrides /tmp/superset/overrides.txt \
        "apache-superset==${SUPERSET_VERSION}" "apache-superset-core==0.1.0rc3" "psycopg2-binary==2.9.10"

FROM python:3.11-slim
ENV LANG=C.UTF-8 PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH \
    SUPERSET_HOME=/app/superset_home HOME=/app/superset_home SUPERSET_PORT=8088 SUPERSET_ENV=production \
    FLASK_APP="superset.app:create_app()"
# curl: the compose healthcheck. pip/setuptools/wheel of the base interpreter are upgraded for the same gate.
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip setuptools wheel \
    && useradd --uid 1000 --user-group --home-dir /app/superset_home --create-home --shell /bin/bash superset
COPY --from=build /app/.venv /app/.venv
WORKDIR /app
USER superset
EXPOSE 8088
CMD ["gunicorn", "--bind", "0.0.0.0:8088", "--workers", "2", "--timeout", "120", "superset.app:create_app()"]
