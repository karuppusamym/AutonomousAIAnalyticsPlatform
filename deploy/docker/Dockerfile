# AnalystOS application image (API, Temporal worker, scheduler). ADR-0025: the extras decide its weight.
#   lite API (default):   docker build --build-arg EXTRAS=reports,ml ...        core + PDF/XLSX/DOCX/PPTX reports + ML
#   standard API:         --build-arg EXTRAS=reports,temporal,ml                  + the Temporal client
#   worker / scheduler:   --build-arg EXTRAS=standard                             + ml, graph (every extra)
# The ML stack (scikit-learn, statsmodels) is in the API image too: ML experiments train in the lite API and are
# checked there before they are dispatched (compose default; ADR-0022's isolated compute-ml pool is opt-in). Built
# without it (EXTRAS=reports) the driver model, experiments and Holt-Winters forecasts report "unavailable".
# The ServiceNow demo mock has its own small image (Dockerfile.mock).
#
# P7-17: the package is installed from a non-editable wheel, so modules arrive byte-compiled (an editable install
# under PYTHONDONTWRITEBYTECODE recompiled every module on every start) and the runtime data the code reads
# (config, migrations, packs, connector evidence) travels inside it (hatch_build.py, core/config.py DATA_ROOT).

# Optional build secret `pipcert` (both stages): CA bundle for TLS-inspecting proxies.
FROM python:3.11-slim AS wheel
WORKDIR /build
COPY pyproject.toml README.md alembic.ini hatch_build.py ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY packs ./packs
COPY docs/60-delivery/evidence ./docs/60-delivery/evidence
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    pip wheel --no-cache-dir --no-deps --wheel-dir /dist .

FROM python:3.11-slim
ARG EXTRAS=reports,ml
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Operators select a models file by path (Helm: ANALYSTOS_MODELS_CONFIG=/app/config/models*.yaml); the same
# files are the wheel's defaults. var/ (artifacts, uploads, builds) is written below the working directory.
COPY config ./config
# The base image's pip/setuptools/wheel lag behind fixed CVEs (setuptools vendors jaraco.context and wheel), and
# the Grype gate in supply-chain.yml fails on them; upgrade them before installing the app.
RUN --mount=type=secret,id=pipcert,required=false \
    --mount=type=bind,from=wheel,source=/dist,target=/dist \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    whl=$(ls /dist/analystos-*.whl); if [ -n "$EXTRAS" ]; then target="$whl[$EXTRAS]"; else target="$whl"; fi; \
    pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir "$target" && useradd --create-home --uid 10001 --gid 0 analystos     && mkdir -p /app/var && chown -R 10001:0 /app/var && chmod -R g=u /app/var
# Numeric user (runAsNonRoot can verify it); group 0 owns the writable paths, so OpenShift's arbitrary UID
# (restricted-v2 SCC, always in group 0) can write them too (S12 §1).
USER 10001
EXPOSE 8000
CMD ["uvicorn", "analystos.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
