# Deploying the Datapipeline Automation Hub

One Cloud Run service hosts both the web UI and the REST API. The service runs as a dedicated **service account**. The
**same Google group** that may use that service account is the only group IAP lets in.

```
user (member of ALLOWED_GROUP) ──HTTPS──▶ IAP ──▶ Cloud Run "datapipeline-hub" (runs as datapipeline-hub-sa)
                                                     ├── Vertex AI Gemini        (SQL analysis, README text)
                                                     ├── BigQuery                (lab dry-runs + metadata; lab test runs on request)
                                                     ├── GCS                     (lab browse/sample; workspace bucket read/write)
                                                     └── Secret Manager          (secret metadata only, never values)

The hub only ever uses the **lab** (dev) values of a process. It never reads, dry-runs or runs the **value** (prod)
environment: those values are only written into the generated YAML.
```

## What the service account can do

| Role | Where | Why |
|---|---|---|
| `roles/aiplatform.user` | hub project | Gemini via Vertex AI |
| `roles/bigquery.jobUser` | hub + data projects | dry-run jobs (free, nothing executes) |
| `roles/bigquery.dataViewer` | data projects | dry-runs need to see the referenced tables; schema copy |
| `roles/storage.objectViewer` | data buckets | browse folders, count matching files, read sample files |
| `roles/storage.objectAdmin` | workspace bucket only | saved processes, drafts, generated versions |
| `roles/logging.logWriter` | hub project | structured app logs |
| `roles/secretmanager.secretAccessor` | the UI secret | session cookie signing |
| `roles/secretmanager.viewer` | `SECRET_PROJECTS` (default: the data projects) | **Check** on a secret and *Validate all*: does it exist, which version is `latest`, is it enabled. Viewer cannot read secret values |

Validation never writes anything and never reads a secret's value. Only a **lab test run**, which a user starts and
confirms, writes: it runs the generated script with the hub's credentials and the lab values, exactly as a developer
would in lab. For test runs from Cloud Run, the service account also needs the lab write roles a developer has
(`roles/bigquery.dataEditor` on the lab datasets, `roles/storage.objectAdmin` on the lab buckets, and the
`pipeline_audit.pipeline_sql_log` table for `sql_logger`). Most teams run the hub locally for test runs instead
(`run_local.sh`), so the developer's own `gcloud auth application-default login` is used, as the team template expects
in lab.

The service account is the hub's **ADC identity**. The header shows it top right as `ADC: <email>`, and every
generated script, YAML and README records it as "ADC identity at generation". That way, anyone can tell whose
permissions a passing check reflects. On a laptop, ADC is whatever `gcloud auth application-default login` stored.

Generated scripts use `google.auth.default()`, like the team template: the developer's IAM in lab, the Composer
service account in value (prod).

## First deployment

Prerequisites:
- `gcloud` logged in as someone who can administer IAM, Cloud Run, IAP and Secret Manager in the project.
- An OAuth consent screen configured for the project. IAP needs one; set it up under APIs & Services → OAuth consent screen, as Internal.

```bash
cd script_automation
PROJECT_ID=acme-tools \
ALLOWED_GROUP=data-engineering@acme.com \
REGION=us-central1 \
DATA_PROJECTS="acme-analytics-dev acme-analytics-prod" \
DATA_BUCKETS="acme-landing-dev acme-exports-dev" \
COPY_BUCKETS="acme-landing-dev acme-vendor-drop-dev" \
SECRET_PROJECTS="acme-analytics-dev acme-analytics-prod" \
GEMINI_MODEL=gemini-2.5-pro \
./deploy/setup_gcp.sh
```

The script is idempotent and does the following:
1. Enables the APIs.
2. Creates the service account and grants the roles above.
3. Creates a versioned workspace bucket, and grants read/write (`roles/storage.objectAdmin`) on the `COPY_BUCKETS`.
4. Stores a random UI secret in Secret Manager.
5. Builds the image.
6. Deploys Cloud Run with `--no-allow-unauthenticated --iap --session-affinity`.
7. Grants the group IAP access.

It then prints the URL.

**Already running the earlier ScriptForge deployment?** The defaults are now named `datapipeline-hub*`. To update the
existing service in place instead of creating new resources, export the old names first:
`SERVICE=scriptforge SA_NAME=scriptforge-sa AR_REPO=scriptforge WORKSPACE_BUCKET=<project>-scriptforge-workspace`.
The UI secret is now called `datapipeline-hub-ui-secret`; the script creates it if it is missing. All `SF_*`
environment variables are unchanged.

**Notes**
- **Gemini model:** set `GEMINI_MODEL` (`SF_GEMINI_MODEL`) to the newest Gemini model available in your Vertex region. Model availability changes over time.
- **IAP flavour:** the script uses Cloud Run's built-in IAP (`gcloud run deploy --iap`).
  - The JWT audience the app checks is then `/projects/<PROJECT_NUMBER>/locations/<REGION>/services/<SERVICE>`.
  - If your organisation uses an external HTTPS load balancer with IAP on a serverless NEG instead, set `SF_IAP_AUDIENCE` to `/projects/<PROJECT_NUMBER>/global/backendServices/<BACKEND_SERVICE_ID>`.
  - Check the exact audience in the IAP console under "Signed header JWT audience".
- **Optional second check:** set `SF_ALLOWED_GROUP=<group email>` to have the app confirm group membership through the Cloud Identity Groups API. This also requires granting the service account Groups Reader.
- **Session affinity:** the UI keeps each open browser tab's state in memory, so a user should stay on one instance.
  - Session affinity does this, and drafts are autosaved to the bucket every couple of seconds.
  - If an instance is recycled, the user just reloads the page.

## Later deployments

```bash
gcloud builds submit --config deploy/cloudbuild.yaml \
  --substitutions=_REGION=us-central1,_REPO=datapipeline-hub,_SERVICE=datapipeline-hub .
```

This runs the test suite, builds the image and deploys a new revision. The service account, environment variables, IAP
and session affinity carry over.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SF_AUTH_MODE` | `iap` in the container, `local` otherwise | `local` trusts `SF_DEV_USER_EMAIL` (laptops/tests only) |
| `SF_IAP_AUDIENCE` | — | required in `iap` mode |
| `SF_ALLOWED_GROUP` | — | optional Cloud Identity membership check |
| `SF_GCP_PROJECT` | — | project for Vertex AI (Gemini is off when unset) |
| `SF_VERTEX_LOCATION` | `us-central1` | Vertex AI region |
| `SF_GEMINI_MODEL` / `SF_GEMINI_FAST_MODEL` | `gemini-2.5-pro` / `gemini-2.5-flash` | analysis / README models |
| `SF_GEMINI_ENABLED` | `true` | turn AI off and use only the rule-based parameterization |
| `SF_GEMINI_RPM` | `30` | per-instance rate limit |
| `SF_WORKSPACE_BUCKET` | — | GCS workspace (local folder `output/workspace` when unset) |
| `SF_SMOKE_RUN_ENABLED` | `true` | run each generated script offline (fake Google clients) before handing it out |
| `SF_GCP_CHECKS_ENABLED` | `true` | BigQuery dry-runs, GCS and Secret Manager checks during validation |
| `SF_ADC_LOOKUP_ENABLED` | `true` | look up the ADC identity's email for the header and the generated files |
| `SF_MAX_UPLOAD_MB` / `SF_SAMPLE_READ_BYTES` | `50` / `2 MiB` | sample-file limits |
| `SF_UI_STORAGE_SECRET` | random | session cookie secret (from Secret Manager in Cloud Run) |
| `SF_LOG_FORMAT` | `json` | `text` for local development |
| `SF_COPY_TOOL_ENABLED` | `true` | the home page's Copy files tool (copy / move between buckets from the browser) |
| `SF_COPY_TOOL_BUCKETS` | — | comma-separated **lab** buckets (or patterns such as `acme-*-dev`) the deployed tool may use; set from `COPY_BUCKETS` by `setup_gcp.sh`. Empty = the tool is off on a deployed hub |
| `SF_COPY_TOOL_SCOPE` | `auto` | `auto`: a local hub uses any bucket your own credentials reach, a deployed (IAP) hub only `SF_COPY_TOOL_BUCKETS`; `any` / `allowlist` force one |
| `SF_COPY_TOOL_MAX_FILES` / `SF_COPY_TOOL_WORKERS` | `5000` / `8` | files per copy job / files copied at the same time |
| `SF_COPY_TOOL_MAX_UPLOAD_MB` | `30` | largest file uploaded from a computer (Cloud Run caps a request at 32 MiB; a local hub can go higher) |
| `SF_COPY_TOOL_MAX_DOWNLOAD_MB` | `500` | largest download to a computer (one file, or one ZIP of several). Downloads wait on the hub's disk for an hour, and on Cloud Run that disk is memory: keep this well under the service's memory (2 GiB) |

**Generated-code standards** (the same for every process; not shown in the UI):

| Variable | Default | Meaning |
|---|---|---|
| `SF_ENVIRONMENTS` | `["lab","value","qa"]` | the script's `environment` argument choices |
| `SF_MODULE_PATHS` | `["/data/py_scripts","/data/data_eng/py_scripts"]` | `sys.path` entries for the team modules |
| `SF_SQL_LOGGER_ENABLED` | `true` | capture every SQL with `sql_logger.CaptureSQL` (the audit) |
| `SF_SQL_LOGGER_TABLE` | `pipeline_audit.pipeline_sql_log` | where sql_logger writes (validation checks it in the ADC project) |
| `SF_SMTP_HOST` / `SF_SMTP_PORT` / `SF_SMTP_STARTTLS` | `smtp.example.com` / `25` / `true` | internal mail relay (no login) |
| `SF_EMAIL_SIGNATURE` | "Regards, The Data Engineering Team …" | appended to every notification |
| `SF_BQ_LOCATION` | `US` | `location=` of extract jobs |

**Lab test runs:**

| Variable | Default | Meaning |
|---|---|---|
| `SF_TEST_RUN_ENABLED` | `true` | allow the **Test run in lab** button |
| `SF_TEST_RUN_TIMEOUT_SECONDS` | `1800` | a run is stopped after this |
| `SF_TEST_RUN_MAX_CONCURRENT` | `2` | test runs at the same time per instance |
| `SF_TEST_RUN_PYTHON` | the hub's Python | interpreter for test runs (needs google-cloud-bigquery/storage, pandas, PyYAML; openpyxl for Excel) |
| `SF_MODULES_DIR` | the hub's copies | folder with the team's real `modules/` (read_config_yaml, sql_logger, gsm_module) |

## Verify a deployment

1. Open the URL as a group member. You should see the Processes page, with your email in the header and
   `ADC: datapipeline-hub-sa@<project>.iam.gserviceaccount.com` next to it.
2. Open it from an account outside the group. IAP should answer with "You don't have access".
3. Call `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" <URL>/api/health` without going through IAP. It should be rejected.
4. Create a process from the *SQL → FTP extract* template on a throw-away lab table, then click **Validate all**,
   **Test run in lab** and **Generate**, and check that the lab test and the offline smoke run pass. (Generate stays
   locked until the lab test passes, unless test runs are turned off with `SF_TEST_RUN_ENABLED=false`.)
5. In Cloud Logging, filter on `resource.type="cloud_run_revision" jsonPayload.logger="dahub.http"`. You should see request logs carrying the user email and request ID.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No IAP identity on the request" | The request bypassed IAP, or IAP is off. Re-run the IAP steps and open the IAP URL. |
| "Your sign-in could not be verified" | `SF_IAP_AUDIENCE` doesn't match. Copy the audience from the IAP console. |
| Dry run says *Access Denied* | Grant the service account `roles/bigquery.dataViewer` and `roles/bigquery.jobUser` on that project, or add the project to `DATA_PROJECTS`. |
| "Could not list gs://…" in validation | Grant the service account `roles/storage.objectViewer` on that bucket, or add it to `DATA_BUCKETS`. |
| Gemini errors / quota | The app falls back to rule-based parameterization automatically. Check `SF_GEMINI_MODEL` and the quota in the region. |
| Header shows an amber `ADC: not configured` | No usable Application Default Credentials. Locally: `gcloud auth application-default login`. On Cloud Run: check the service account. |
| A secret **Check** says *Permission denied* | Grant the service account `roles/secretmanager.viewer` on that project, or add it to `SECRET_PROJECTS`. The scripts themselves need `roles/secretmanager.secretAccessor`. |
| A lab test run stops halfway on Cloud Run | The service must run with `--no-cpu-throttling` (the setup script sets it), so the background run keeps CPU. |
| A copy step fails in a test run with "gcloud CLI not found" | The image installs the Google Cloud CLI; locally, install it and make sure `gcloud` is on the PATH. |
| Generate fails with *offline smoke run … Project was not passed and could not be determined*, the traceback in `site-packages\google\cloud\bigquery` | The hub's Python pre-loaded the real `google` package (legacy `*-nspkg.pth` files of older google-cloud packages in a shared venv). Fixed in 3.2: the smoke run now drops it and uses its fake clients. If it still says *could not load the fake Google clients*, run the hub from its own venv (`run_local.bat` makes one). |
