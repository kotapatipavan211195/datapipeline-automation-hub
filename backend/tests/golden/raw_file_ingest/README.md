# member_payments_raw

Loads the daily member payment files as they are (raw staging table plus history, no target table), then builds the payment summary from this run's rows. The email lists every file and its row count.

| | |
|---|---|
| Author(s) | P10000 |
| Reviewer(s) | — |
| JIRA/SN # | DE-2310 |
| Owner | pipeline.developer@example.com |
| Team | Membership |
| Script | `dl_member_payments_raw.py` with `dl_member_payments_raw.yaml` and `dl_member_payments_raw_table_schema.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `620957a675b7` |
| ADC identity at generation | `pipeline.developer@example.com` (also the email sender) |

## Run

```bash
py ./dl_member_payments_raw.py -cf ./dl_member_payments_raw.yaml -ts ./dl_member_payments_raw_table_schema.yaml lab      # lab (dev): your own gcloud credentials
py ./dl_member_payments_raw.py -cf ./dl_member_payments_raw.yaml -ts ./dl_member_payments_raw_table_schema.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `dl_member_payments_raw.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`, `sql_logger`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `demo-analytics-dev` | `value-gcp` | — |
| `write_project` | project | `demo-analytics-dev` | `value-gcp` | `build_payment_summary` (SQL) |
| `landing_bucket` | bucket | `demo_file_test_bucket` | `value-gcp-insights` | `ingest_member_payments` |
| `raw_dataset` | dataset | `membership_raw_lab` | `membership_raw` | `ingest_member_payments`, `build_payment_summary` (SQL) |
| `mart_dataset` | dataset | `membership_lab` | `membership` | `build_payment_summary` (SQL) |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `dl_member_payments_raw.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `ingest_member_payments` | File ingestion (GCS → BigQuery) | Load today's member payment files into the raw staging table and the history table, then archive them | stop |
| 2. | `build_payment_summary` | SQL (DML) | Add this run's payments to the daily payment summary | stop |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

None: every table this process reads is built by an earlier step of the same process.

## Target tables

Tables this process creates or changes (lab names).

| Table (lab) | Written by | How | Read afterwards by |
|---|---|---|---|
| `demo-analytics-dev.membership_raw_lab.member_payments_raw` | `ingest_member_payments` | staging: this run's files (raw rows for the next steps) | `build_payment_summary` |
| `demo-analytics-dev.membership_raw_lab.member_payments_raw_history` | `ingest_member_payments` | history: every file appended | — |
| `demo-analytics-dev.membership_lab.payment_summary` | `build_payment_summary` | INSERT | — |

### Source files

| Step | Files (lab) | What |
|---|---|---|
| `ingest_member_payments` | `gs://demo_file_test_bucket/inbound/payments/*Member Payments_*` | CSV files loaded (name contains 'Member Payments_') |

### Output files

| Step | Path (lab) | What |
|---|---|---|
| `ingest_member_payments` | `gs://demo_file_test_bucket/inbound/payments/Archive/` | archive: loaded files are moved here |

## Process steps in detail

### 1. `ingest_member_payments` · File ingestion (GCS → BigQuery)

Load today's member payment files into the raw staging table and the history table, then archive them

| | |
|---|---|
| Reads files | `gs://demo_file_test_bucket/inbound/payments/*Member Payments_*`; when none arrive: log a warning |
| Format | CSV, separator `\|`, header `0`, skiprows `None`; read with pandas, typed by `dl_member_payments_raw_table_schema.yaml` (4 columns) |
| Writes | `demo-analytics-dev.membership_raw_lab.member_payments_raw` (staging: this run's files (raw rows for the next steps)), `demo-analytics-dev.membership_raw_lab.member_payments_raw_history` (history: every file appended) |
| Raw rows | kept in `demo-analytics-dev.membership_raw_lab.member_payments_raw` for the next steps: the run's first file empties it and every file of the run is added (emptied when no file arrives) |
| Added columns | `file_batch_id`, `inserted_ts`, `updated_ts`, `file_name` |
| Processed files | moved to `gs://demo_file_test_bucket/inbound/payments/Archive` |
| Config | `gcp_projects.<environment>.gcs_config` |
| On failure | the run stops (later steps do not run) |

### 2. `build_payment_summary` · SQL (DML)

Add this run's payments to the daily payment summary

| | |
|---|---|
| Statement | DML |
| Reads | `demo-analytics-dev.membership_raw_lab.member_payments_raw` |
| Writes | `demo-analytics-dev.membership_lab.payment_summary` (INSERT) |
| Config | variables only (see Environments) |
| On failure | the run stops (later steps do not run) |

## SQL log

Every SQL statement the script runs is captured with the team's `sql_logger` module (`CaptureSQL.write_to_log`)
in `<credentials' project>.pipeline_audit.pipeline_sql_log`, with status `success` or `failure`, the BigQuery job id,
the environment and the Composer `DAG_ID` / `TASK_ID` when set.

## Notification email

Sent through the internal mail relay (no login) from `pipeline.developer@example.com` to the
`email.receiver` list of the environment's block, when the run succeeds and when a step fails.
 The body lists the files the run loaded and exported: file name, rows, and the table or gs:// path.
## Files

| File | What |
|---|---|
| `dl_member_payments_raw.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `dl_member_payments_raw.yaml` | configuration: one block per environment under `gcp_projects` |
| `dl_member_payments_raw_table_schema.yaml` | table definition for the file ingestion (`table_definition.table_name` / `table_schema`) |
| `requirements.txt` | Python packages |
