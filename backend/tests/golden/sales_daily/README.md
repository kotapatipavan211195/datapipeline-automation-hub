# sales_daily

Loads the daily store sales CSV, builds the daily sales mart, exports it for the finance team and checks quality.

| | |
|---|---|
| Author(s) | P10001 |
| Reviewer(s) | P10002 |
| JIRA/SN # | DE-2001 |
| Owner | data.engineer@example.com |
| Team | Retail Analytics |
| Script | `sales_daily.py` with `sales_daily.yaml` and `sales_daily_table_schema.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `03c8b10a9d4e` |
| ADC identity at generation | `pipeline.developer@example.com` (also the email sender) |

## Run

```bash
py ./sales_daily.py -cf ./sales_daily.yaml -ts ./sales_daily_table_schema.yaml lab      # lab (dev): your own gcloud credentials
py ./sales_daily.py -cf ./sales_daily.yaml -ts ./sales_daily_table_schema.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `sales_daily.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`, `sql_logger`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `acme-analytics-dev` | `acme-analytics-prod` | — |
| `write_project` | project | `acme-analytics-dev` | `acme-analytics-prod` | `build_sales_daily_agg`, `export_sales_daily_to_finance`, `check_sales_daily_quality` (SQL) |
| `raw_dataset` | dataset | `raw_sales` | `raw_sales` | `ingest_store_sales_files`, `build_sales_daily_agg`, `check_sales_daily_quality` (SQL) |
| `mart_dataset` | dataset | `sales_mart` | `sales_mart` | `build_sales_daily_agg`, `export_sales_daily_to_finance`, `check_sales_daily_quality` (SQL) |
| `landing_bucket` | bucket | `acme-landing-dev` | `acme-landing-prod` | `ingest_store_sales_files` |
| `export_bucket` | bucket | `acme-exports-dev` | `acme-exports-prod` | `export_sales_daily_to_finance` |
| `reference_project` | project | `acme-reference-prod` | `acme-reference-prod` | `build_sales_daily_agg` (SQL) |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `sales_daily.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `ingest_store_sales_files` | File ingestion (GCS → BigQuery) | Load the store sales CSV files for the run date into raw_sales.store_sales | stop |
| 2. | `build_sales_daily_agg` | SQL (MERGE) | MERGE the day's raw sales into sales_mart.sales_daily (one row per store per day) | stop |
| 3. | `export_sales_daily_to_finance` | Export (BigQuery → GCS) | Export the day's sales mart rows to the finance bucket as gzip CSV | stop |
| 4. | `check_sales_daily_quality` | Data quality | Row count, key columns not null, one row per store per day | stop |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

| Table (lab) | Read by | Template |
|---|---|---|
| `acme-reference-prod.reference.stores` | `build_sales_daily_agg` | `{reference_project}.reference.stores` |

## Target tables

Tables this process creates or changes (lab names).

| Table (lab) | Written by | How | Read afterwards by |
|---|---|---|---|
| `acme-analytics-dev.raw_sales.store_sales_stg` | `ingest_store_sales_files` | staging: replaced by every file | — |
| `acme-analytics-dev.raw_sales.store_sales_stg_history` | `ingest_store_sales_files` | history: every file appended | — |
| `acme-analytics-dev.raw_sales.store_sales` | `ingest_store_sales_files` | MERGE from the staging table (newest row per key) | `build_sales_daily_agg`, `check_sales_daily_quality` |
| `acme-analytics-dev.sales_mart.sales_daily` | `build_sales_daily_agg` | MERGE | `export_sales_daily_to_finance`, `check_sales_daily_quality` |

### Source files

| Step | Files (lab) | What |
|---|---|---|
| `ingest_store_sales_files` | `gs://acme-landing-dev/sales/incoming/*store_sales_*` | CSV files loaded (name contains 'store_sales_') |

### Output files

| Step | Path (lab) | What |
|---|---|---|
| `ingest_store_sales_files` | `gs://acme-landing-dev/sales/archive/` | archive: loaded files are moved here |
| `export_sales_daily_to_finance` | `gs://acme-exports-dev/finance/sales_daily/sales_daily_<%Y%m%d>-*.csv.gz` | export: CSV, gzip |

## Process steps in detail

### 1. `ingest_store_sales_files` · File ingestion (GCS → BigQuery)

Load the store sales CSV files for the run date into raw_sales.store_sales

| | |
|---|---|
| Reads files | `gs://acme-landing-dev/sales/incoming/*store_sales_*`; when none arrive: send the no-file email |
| Format | CSV, separator `,`, header `0`, skiprows `None`; read with pandas, typed by `sales_daily_table_schema.yaml` (7 columns) |
| Writes | `acme-analytics-dev.raw_sales.store_sales_stg` (staging: replaced by every file), `acme-analytics-dev.raw_sales.store_sales_stg_history` (history: every file appended), `acme-analytics-dev.raw_sales.store_sales` (MERGE from the staging table (newest row per key)) |
| Merge | keys `store_id`, `sku`, `sold_at` |
| Added columns | `file_batch_id`, `inserted_ts`, `updated_ts`, `file_name` |
| Processed files | moved to `gs://acme-landing-dev/sales/archive` |
| Config | `gcp_projects.<environment>.gcs_config` |
| On failure | the run stops (later steps do not run) |

### 2. `build_sales_daily_agg` · SQL (MERGE)

MERGE the day's raw sales into sales_mart.sales_daily (one row per store per day)

| | |
|---|---|
| Statement | MERGE |
| Reads | `acme-reference-prod.reference.stores`, `acme-analytics-dev.raw_sales.store_sales` |
| Writes | `acme-analytics-dev.sales_mart.sales_daily` (MERGE) |
| Parameters | `@run_date` DATE: today -1 day(s); `@min_amount` NUMERIC: 'min_amount' in the YAML (default 0.01) |
| Config | variables only (see Environments) |
| On failure | the run stops (later steps do not run) |

### 3. `export_sales_daily_to_finance` · Export (BigQuery → GCS)

Export the day's sales mart rows to the finance bucket as gzip CSV

| | |
|---|---|
| Reads | `acme-analytics-dev.sales_mart.sales_daily` |
| Writes files | `gs://acme-exports-dev/finance/sales_daily/sales_daily_<%Y%m%d>-*.csv.gz` (CSV, gzip compressed, delimiter `,`, with a header row) |
| Parameters | `@run_date` DATE: today -1 day(s) |
| Email | the file is attached to the success email |
| Config | `gcp_projects.<environment>.ftp` |
| On failure | the run stops (later steps do not run) |

### 4. `check_sales_daily_quality` · Data quality

Row count, key columns not null, one row per store per day

| | |
|---|---|
| Checks | 4 (3 stop the run when they fail, 1 only warn); see Data-quality checks below |
| Reads | `acme-analytics-dev.raw_sales.store_sales`, `acme-analytics-dev.sales_mart.sales_daily` |
| Config | variables only (see Environments) |
| On failure | the run stops (later steps do not run) |

## Data-quality checks

A failed check with severity **error** stops the run; **warn** only logs a warning.

| Step | Check | Expectation | Severity |
|---|---|---|---|
| `check_sales_daily_quality` | row_count_min on sales_daily | value >= 1 | error |
| `check_sales_daily_quality` | not_null(store_id, total_amount) on sales_daily | value == 0 | error |
| `check_sales_daily_quality` | unique(store_id, sale_date) on sales_daily | value == 0 | error |
| `check_sales_daily_quality` | freshness(sold_at) on store_sales | value <= 48 | warn |

## SQL log

Every SQL statement the script runs is captured with the team's `sql_logger` module (`CaptureSQL.write_to_log`)
in `<credentials' project>.pipeline_audit.pipeline_sql_log`, with status `success` or `failure`, the BigQuery job id,
the environment and the Composer `DAG_ID` / `TASK_ID` when set.

## Notification email

Sent through the internal mail relay (no login) from `pipeline.developer@example.com` to the
`email.receiver` list of the environment's block, when the run succeeds and when a step fails.
Attachments: the `email.attachment` paths plus the files written by `export_sales_daily_to_finance`.
## Files

| File | What |
|---|---|
| `sales_daily.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `sales_daily.yaml` | configuration: one block per environment under `gcp_projects` |
| `sales_daily_table_schema.yaml` | table definition for the file ingestion (`table_definition.table_name` / `table_schema`) |
| `requirements.txt` | Python packages |
