# Datapipeline Automation Hub — one container: REST API (/api) + NiceGUI web UI (/), behind IAP on Cloud Run.
# Build from the repository root:  docker build -f deploy/Dockerfile -t datapipeline-hub .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Google Cloud CLI: generated "Copy files" steps run `gcloud storage cp|mv|rsync`, also during lab test runs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
       > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
    && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY backend/requirements.txt /srv/requirements.txt
RUN pip install -r /srv/requirements.txt

COPY backend/ /srv/backend/
COPY samples/ /srv/samples/

RUN useradd --create-home --uid 10001 app && chown -R app /srv
USER app
WORKDIR /srv/backend

# Production defaults; everything else comes from Cloud Run env vars (see deploy/README_DEPLOY.md).
ENV SF_AUTH_MODE=iap \
    SF_LOG_FORMAT=json \
    PORT=8080

# One worker per instance: the UI keeps per-browser state in memory (Cloud Run session affinity routes a user back).
CMD exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 75
