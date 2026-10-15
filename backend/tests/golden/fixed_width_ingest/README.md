# member_fixed_width_ingest

Loads the daily fixed-width member file from the mainframe extract: each line holds the member id, name, status, join date and balance at fixed positions. The rows go to a staging table, a history table and are merged into the member table by member id.

| | |
|---|---|
| Author(s) | P10000 |
| Reviewer(s) | — |
| JIRA/SN # | DE-2210 |
| Owner | pipeline.developer@example.com |
| Team | Membership |
| Script | `dl_member_fixed_width_ingest.py` with `dl_member_fixed_width_ingest.yaml` and `dl_member_fixed_width_ingest_table_schema.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `ce4cfa09248f` |
| ADC identity at generation | `pipeline.developer@example.com` |

## Run

```bash
py ./dl_member_fixed_width_ingest.py -cf ./dl_member_fixed_width_ingest.yaml -ts ./dl_member_fixed_width_ingest_table_schema.yaml lab      # lab (dev): your own gcloud credentials
py ./dl_member_fixed_width_ingest.py -cf ./dl_member_fixed_width_ingest.yaml -ts ./dl_member_fixed_width_ingest_table_schema.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `dl_member_fixed_width_ingest.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`, `sql_logger`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `demo-analytics-dev` | `value-gcp` | — |
| `write_project` | project | `demo-analytics-dev` | `value-gcp` | — |
| `landing_bucket` | bucket | `demo_file_test_bucket` | `value-gcp-insights` | `ingest_member_extract` |
| `landing_path` | other | `member_extract` | `inbound/member_extract` | `ingest_member_extract` |
| `archive_path` | other | `member_extract/Archive` | `Archive/inbound/member_extract` | `ingest_member_extract` |
| `temporary_dataset` | dataset | `clubwide` | `discovery05` | `ingest_member_extract` |
| `target_dataset` | dataset | `clubwide` | `membership01` | `ingest_member_extract` |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `dl_member_fixed_width_ingest.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `ingest_member_extract` | File ingestion (GCS → BigQuery) | Load the fixed-width member extract into staging, history and the member table, then archive it | stop |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

None: every table this process reads is built by an earlier step of the same process.

## Target tables

Tables this process creates or changes (lab names).

| Table (lab) | Written by | How | Read afterwards by |
|---|---|---|---|
| `demo-analytics-dev.clubwide.member_extract` | `ingest_member_extract` | staging: replaced by every file | — |
| `demo-analytics-dev.clubwide.member_extract_history` | `ingest_member_extract` | history: every file appended | — |
| `demo-analytics-dev.clubwide.member_extract_daily` | `ingest_member_extract` | MERGE from the staging table (newest row per key) | — |

### Source files

| Step | Files (lab) | What |
|---|---|---|
| `ingest_member_extract` | `gs://demo_file_test_bucket/member_extract/*MBR_EXTRACT_*` | TXT (fixed width) files loaded (name contains 'MBR_EXTRACT_') |

### Output files

| Step | Path (lab) | What |
|---|---|---|
| `ingest_member_extract` | `gs://demo_file_test_bucket/member_extract/Archive/` | archive: loaded files are moved here |

## Process steps in detail

### 1. `ingest_member_extract` · File ingestion (GCS → BigQuery)

Load the fixed-width member extract into staging, history and the member table, then archive it

| | |
|---|---|
| Reads files | `gs://demo_file_test_bucket/member_extract/*MBR_EXTRACT_*`; when none arrive: log a warning |
| Format | TXT (fixed width), record length 69 characters (column lengths in `dl_member_fixed_width_ingest_table_schema.yaml`), header `None`, skiprows `None`; read with pandas, typed by `dl_member_fixed_width_ingest_table_schema.yaml` (6 columns) |
| Writes | `demo-analytics-dev.clubwide.member_extract` (staging: replaced by every file), `demo-analytics-dev.clubwide.member_extract_history` (history: every file appended), `demo-analytics-dev.clubwide.member_extract_daily` (MERGE from the staging table (newest row per key)) |
| Merge | keys `member_id` |
| Added columns | `file_batch_id`, `inserted_ts`, `updated_ts`, `file_name` |
| Processed files | moved to `gs://demo_file_test_bucket/member_extract/Archive` |
| Config | `gcp_projects.<environment>.gcs_config` |
| On failure | the run stops (later steps do not run) |

## SQL log

Every SQL statement the script runs is captured with the team's `sql_logger` module (`CaptureSQL.write_to_log`)
in `<credentials' project>.pipeline_audit.pipeline_sql_log`, with status `success` or `failure`, the BigQuery job id,
the environment and the Composer `DAG_ID` / `TASK_ID` when set.

## Files

| File | What |
|---|---|
| `dl_member_fixed_width_ingest.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `dl_member_fixed_width_ingest.yaml` | configuration: one block per environment under `gcp_projects` |
| `dl_member_fixed_width_ingest_table_schema.yaml` | table definition for the file ingestion (`table_definition.table_name` / `table_schema`) |
| `requirements.txt` | Python packages |
