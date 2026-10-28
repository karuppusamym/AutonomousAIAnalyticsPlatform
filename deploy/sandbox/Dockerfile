# Sandbox image for ANALYSTOS_SANDBOX_ISOLATION=container (P4-02): the interpreter and the allow-listed
# numeric libraries only - no AnalystOS code, no credentials, no shell tools the harness needs. The
# harness is passed on the command line by the runner; every run is
#   docker run --rm --network none --read-only --tmpfs /scratch --user 65534 --cap-drop ALL
#              --security-opt no-new-privileges --memory/--memory-swap --cpus --pids-limit ...
# Build: docker compose --profile sandbox build sandbox   (tag analystos-sandbox:latest)
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Optional build secret `pipcert`: CA bundle for TLS-inspecting proxies. pip/setuptools/wheel are upgraded first:
# the base image's copies carry fixed CVEs the Grype gate (supply-chain.yml) fails on.
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir "numpy>=1.26,<3" "pandas>=2.2,<4" "scipy>=1.13,<2" "statsmodels>=0.14,<1" \
        "scikit-learn>=1.5,<2" "polars>=1.5,<2" \
    && rm -rf /root/.cache
USER 65534:65534
WORKDIR /scratch
ENTRYPOINT ["python3", "-I", "-B"]
