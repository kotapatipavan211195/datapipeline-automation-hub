#!/usr/bin/env bash
# One-time setup: Datapipeline Automation Hub on Cloud Run, protected by IAP, running as a dedicated service account.
#
#   PROJECT_ID=my-proj ALLOWED_GROUP=data-eng@example.com ./deploy/setup_gcp.sh
#
# Review the CONFIG block first. Every step is idempotent, so the script can be re-run.
# Run it from the repository root with an account that can administer IAM, Cloud Run and IAP in PROJECT_ID.
set -euo pipefail

# ------------------------------------------------------------------ CONFIG
PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (project that hosts the Datapipeline Automation Hub)}"
ALLOWED_GROUP="${ALLOWED_GROUP:?set ALLOWED_GROUP (Google group allowed to use the hub and the service account)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-datapipeline-hub}"
SA_NAME="${SA_NAME:-datapipeline-hub-sa}"
WORKSPACE_BUCKET="${WORKSPACE_BUCKET:-${PROJECT_ID}-datapipeline-hub-ws}"
DATA_PROJECTS="${DATA_PROJECTS:-$PROJECT_ID}"   # space-separated: projects whose tables the wizard dry-runs / inspects
DATA_BUCKETS="${DATA_BUCKETS:-}"                 # space-separated: buckets the wizard may browse and sample (read-only)
COPY_BUCKETS="${COPY_BUCKETS:-}"                 # space-separated: LAB buckets the Copy files tool may copy/move between (read/write; never prod)
SECRET_PROJECTS="${SECRET_PROJECTS:-$DATA_PROJECTS}"  # space-separated: projects whose Secret Manager metadata the wizard checks
GEMINI_LOCATION="${GEMINI_LOCATION:-us-central1}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-pro}"   # use the newest Gemini model available in GEMINI_LOCATION
AR_REPO="${AR_REPO:-datapipeline-hub}"
# ------------------------------------------------------------------

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE}:$(date +%Y%m%d-%H%M%S)"
AUDIENCE="/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE}"

step() { printf '\n==> %s\n' "$*"; }

step "Enabling APIs"
gcloud services enable run.googleapis.com iap.googleapis.com aiplatform.googleapis.com bigquery.googleapis.com \
  storage.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com \
  logging.googleapis.com --project "$PROJECT_ID"

step "Runtime service account ${SA_EMAIL}"
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" --display-name "Datapipeline Automation Hub runtime"

step "Project roles: Gemini, BigQuery jobs (dry-runs), logging"
for role in roles/aiplatform.user roles/bigquery.jobUser roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${SA_EMAIL}" --role "$role" \
    --condition=None --quiet >/dev/null
done

step "Read-only BigQuery access on data projects (dry-runs need to see the referenced tables)"
for p in $DATA_PROJECTS; do
  for role in roles/bigquery.jobUser roles/bigquery.dataViewer; do
    gcloud projects add-iam-policy-binding "$p" --member "serviceAccount:${SA_EMAIL}" --role "$role" \
      --condition=None --quiet >/dev/null
  done
done

step "Secret Manager Viewer on secret projects (metadata only: the hub never reads secret values)"
for p in $SECRET_PROJECTS; do
  gcloud projects add-iam-policy-binding "$p" --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.viewer \
    --condition=None --quiet >/dev/null
done

step "Workspace bucket gs://${WORKSPACE_BUCKET} (versioned)"
gcloud storage buckets describe "gs://${WORKSPACE_BUCKET}" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://${WORKSPACE_BUCKET}" --project "$PROJECT_ID" --location "$REGION" \
       --uniform-bucket-level-access
gcloud storage buckets update "gs://${WORKSPACE_BUCKET}" --versioning >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${WORKSPACE_BUCKET}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/storage.objectAdmin >/dev/null

step "Read-only access to data buckets"
for b in $DATA_BUCKETS; do
  gcloud storage buckets add-iam-policy-binding "gs://${b}" \
    --member "serviceAccount:${SA_EMAIL}" --role roles/storage.objectViewer >/dev/null
done

step "Read/write access to the Copy files tool's lab buckets"
for b in $COPY_BUCKETS; do
  gcloud storage buckets add-iam-policy-binding "gs://${b}" \
    --member "serviceAccount:${SA_EMAIL}" --role roles/storage.objectAdmin >/dev/null
done

step "Let ${ALLOWED_GROUP} run generated scripts as the service account (impersonation)"
for role in roles/iam.serviceAccountUser roles/iam.serviceAccountTokenCreator; do
  gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT_ID" \
    --member "group:${ALLOWED_GROUP}" --role "$role" >/dev/null
done

step "UI session secret in Secret Manager"
gcloud secrets describe datapipeline-hub-ui-secret --project "$PROJECT_ID" >/dev/null 2>&1 \
  || (openssl rand -base64 32 | tr -d '\n' | gcloud secrets create datapipeline-hub-ui-secret \
        --project "$PROJECT_ID" --replication-policy automatic --data-file=-)
gcloud secrets add-iam-policy-binding datapipeline-hub-ui-secret --project "$PROJECT_ID" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.secretAccessor >/dev/null

step "Artifact Registry repository ${AR_REPO}"
gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$AR_REPO" --repository-format docker --location "$REGION" --project "$PROJECT_ID"

step "Building ${IMAGE}"
gcloud builds submit . --project "$PROJECT_ID" --config deploy/cloudbuild-image.yaml --substitutions "_IMAGE=${IMAGE}"

# --no-cpu-throttling: lab test runs keep running in a background process after the click that started them.
step "Deploying Cloud Run service ${SERVICE} (IAP on, unauthenticated access off)"
gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
  --service-account "$SA_EMAIL" \
  --no-allow-unauthenticated \
  --iap \
  --session-affinity \
  --no-cpu-throttling \
  --timeout 3600 --cpu 2 --memory 2Gi --concurrency 40 --min-instances 0 --max-instances 3 \
  --set-env-vars "^|^SF_AUTH_MODE=iap|SF_IAP_AUDIENCE=${AUDIENCE}|SF_GCP_PROJECT=${PROJECT_ID}|SF_VERTEX_LOCATION=${GEMINI_LOCATION}|SF_GEMINI_MODEL=${GEMINI_MODEL}|SF_WORKSPACE_BUCKET=${WORKSPACE_BUCKET}|SF_COPY_TOOL_BUCKETS=$(echo ${COPY_BUCKETS} | tr ' ' ',')" \
  --set-secrets "SF_UI_STORAGE_SECRET=datapipeline-hub-ui-secret:latest"

step "IAP service agent may invoke the service"
gcloud beta services identity create --service=iap.googleapis.com --project "$PROJECT_ID" >/dev/null 2>&1 || true
gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" --role roles/run.invoker >/dev/null

step "IAP: only ${ALLOWED_GROUP} may open the Datapipeline Automation Hub"
gcloud iap web add-iam-policy-binding --project "$PROJECT_ID" --region "$REGION" \
  --resource-type cloud-run --service "$SERVICE" \
  --member "group:${ALLOWED_GROUP}" --role roles/iap.httpsResourceAccessor >/dev/null

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')"
cat <<DONE

Datapipeline Automation Hub is deployed: ${URL}
  - Only members of ${ALLOWED_GROUP} get past IAP; the app verifies the IAP JWT (audience ${AUDIENCE}).
  - It runs as ${SA_EMAIL}; group members can also run generated scripts as it (impersonation).
  - Workspace: gs://${WORKSPACE_BUCKET}
Later deployments: gcloud builds submit --config deploy/cloudbuild.yaml --substitutions=_REGION=${REGION},_REPO=${AR_REPO},_SERVICE=${SERVICE} .
DONE
