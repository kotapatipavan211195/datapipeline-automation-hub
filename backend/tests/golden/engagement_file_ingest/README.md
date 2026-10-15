# engagement_file_ingest

Ingests the daily engagement files sent by the vendor: moves them from the vendor drop bucket to the landing folder, loads them to a staging table, keeps a history table, merges the newest record per client into the daily table and archives the files. Emails the team when no file arrives.

| | |
|---|---|
| Author(s) | P10000 |
| Reviewer(s) | P12345 |
| JIRA/SN # | SN-4321 |
| Owner | pipeline.developer@example.com |
| Team | Membership |
| Script | `mkt_engagement_ingest.py` with `mkt_engagement_ingest.yaml` and `mkt_engagement_ingest_table_schema.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `4da05c8ea964` |
| ADC identity at generation | `pipeline.developer@example.com` (also the email sender) |

## Run

```bash
py ./mkt_engagement_ingest.py -cf ./mkt_engagement_ingest.yaml -ts ./mkt_engagement_ingest_table_schema.yaml lab      # lab (dev): your own gcloud credentials
py ./mkt_engagement_ingest.py -cf ./mkt_engagement_ingest.yaml -ts ./mkt_engagement_ingest_table_schema.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `mkt_engagement_ingest.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`, `sql_logger`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `value-confidential` | `value-gcp` | — |
| `write_project` | project | `demo-analytics-dev` | `value-gcp` | `check_engagement_daily` (SQL) |
| `landing_bucket` | bucket | `demo_file_test_bucket` | `value-gcp-insights` | `move_vendor_files`, `ingest_engagement_files` |
| `landing_path` | other | `engagement_ingestion` | `inbound/engagement` | `move_vendor_files`, `ingest_engagement_files` |
| `archive_path` | other | `engagement_ingestion/Archive` | `Archive/inbound/engagement` | `ingest_engagement_files` |
| `vendor_bucket` | bucket | `demo_vendor_drop_test` | `value-gcp-vendor-drop` | `move_vendor_files` |
| `temporary_dataset` | dataset | `clubwide` | `discovery05` | `ingest_engagement_files` |
| `target_dataset` | dataset | `clubwide` | `strategic03` | `ingest_engagement_files` |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `mkt_engagement_ingest.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `move_vendor_files` | Copy files (gcloud storage) | Move today's engagement files from the vendor drop bucket to the landing folder | stop |
| 2. | `ingest_engagement_files` | File ingestion (GCS → BigQuery) | Load the engagement files into staging, history and the daily table, then archive them | stop |
| 3. | `check_engagement_daily` | Data quality | Every client appears once in the daily table | stop |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

None: every table this process reads is built by an earlier step of the same process.

## Target tables

Tables this process creates or changes (lab names).

| Table (lab) | Written by | How | Read afterwards by |
|---|---|---|---|
| `demo-analytics-dev.clubwide.engagement` | `ingest_engagement_files` | staging: replaced by every file | — |
| `demo-analytics-dev.clubwide.engagement_history` | `ingest_engagement_files` | history: every file appended | — |
| `demo-analytics-dev.clubwide.engagement_daily` | `ingest_engagement_files` | MERGE from the staging table (newest row per key) | `check_engagement_daily` |

### Source files

| Step | Files (lab) | What |
|---|---|---|
| `move_vendor_files` | `gs://demo_vendor_drop_test/outbound/Daily Engagement File_*.csv` | moved by gcloud storage mv |
| `ingest_engagement_files` | `gs://demo_file_test_bucket/engagement_ingestion/*Daily Engagement File_*` | CSV files loaded (name contains 'Daily Engagement File_') |

### Output files

| Step | Path (lab) | What |
|---|---|---|
| `move_vendor_files` | `gs://demo_file_test_bucket/engagement_ingestion/` | files moved here |
| `ingest_engagement_files` | `gs://demo_file_test_bucket/engagement_ingestion/Archive/` | archive: loaded files are moved here |

## Process steps in detail

### 1. `move_vendor_files` · Copy files (gcloud storage)

Move today's engagement files from the vendor drop bucket to the landing folder

| | |
|---|---|
| Direction | Bucket → bucket |
| Command | `gcloud storage mv --no-clobber gs://demo_vendor_drop_test/outbound/Daily Engagement File_*.csv gs://demo_file_test_bucket/engagement_ingestion/` |
| Config | `gcp_projects.<environment>.file_copy` |
| On failure | the run stops (later steps do not run) |

### 2. `ingest_engagement_files` · File ingestion (GCS → BigQuery)

Load the engagement files into staging, history and the daily table, then archive them

| | |
|---|---|
| Reads files | `gs://demo_file_test_bucket/engagement_ingestion/*Daily Engagement File_*`; when none arrive: send the no-file email |
| Format | CSV, separator `,`, header `0`, skiprows `None`; read with pandas, typed by `mkt_engagement_ingest_table_schema.yaml` (21 columns) |
| Writes | `demo-analytics-dev.clubwide.engagement` (staging: replaced by every file), `demo-analytics-dev.clubwide.engagement_history` (history: every file appended), `demo-analytics-dev.clubwide.engagement_daily` (MERGE from the staging table (newest row per key)) |
| Merge | keys `Client_Id`; newest `Created` wins; only rows where `upper(Campaign) NOT LIKE '%UAT%'` |
| Added columns | `file_batch_id`, `inserted_ts`, `updated_ts`, `file_name` |
| Processed files | moved to `gs://demo_file_test_bucket/engagement_ingestion/Archive` |
| Config | `gcp_projects.<environment>.gcs_config` |
| On failure | the run stops (later steps do not run) |

### 3. `check_engagement_daily` · Data quality

Every client appears once in the daily table

| | |
|---|---|
| Checks | 2 (1 stop the run when they fail, 1 only warn); see Data-quality checks below |
| Reads | `demo-analytics-dev.clubwide.engagement_daily` |
| Config | variables only (see Environments) |
| On failure | the run stops (later steps do not run) |

## Data-quality checks

A failed check with severity **error** stops the run; **warn** only logs a warning.

| Step | Check | Expectation | Severity |
|---|---|---|---|
| `check_engagement_daily` | unique(Client_Id) on engagement_daily | value == 0 | error |
| `check_engagement_daily` | not_null(Client_Id, Created) on engagement_daily | value == 0 | warn |

## SQL log

Every SQL statement the script runs is captured with the team's `sql_logger` module (`CaptureSQL.write_to_log`)
in `<credentials' project>.pipeline_audit.pipeline_sql_log`, with status `success` or `failure`, the BigQuery job id,
the environment and the Composer `DAG_ID` / `TASK_ID` when set.

## Notification email

Sent through the internal mail relay (no login) from `pipeline.developer@example.com` to the
`email.receiver` list of the environment's block, when a step fails.

## Copying files

Copy steps run the Google Cloud CLI (`gcloud storage cp | mv | rsync`, the successor of `gsutil`), so `gcloud` must be
installed and on the PATH of the machine that runs the script, logged in as the same identity.

## Files

| File | What |
|---|---|
| `mkt_engagement_ingest.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `mkt_engagement_ingest.yaml` | configuration: one block per environment under `gcp_projects` |
| `mkt_engagement_ingest_table_schema.yaml` | table definition for the file ingestion (`table_definition.table_name` / `table_schema`) |
| `requirements.txt` | Python packages |
