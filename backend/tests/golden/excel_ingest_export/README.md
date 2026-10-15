# agent_roster_excel

Loads the weekly agent roster workbook (sheet Roster, two title rows above the header) sent by the sales office, merges it into the agent table by agent id, then sends the active agents per office back as an Excel workbook and attaches it to the success email.

| | |
|---|---|
| Author(s) | P10000 |
| Reviewer(s) | — |
| JIRA/SN # | DE-2301 |
| Owner | pipeline.developer@example.com |
| Team | Marketing |
| Script | `dl_agent_roster_excel.py` with `dl_agent_roster_excel.yaml` and `dl_agent_roster_excel_table_schema.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `7d9f08a6e4fe` |
| ADC identity at generation | `pipeline.developer@example.com` (also the email sender) |

## Run

```bash
py ./dl_agent_roster_excel.py -cf ./dl_agent_roster_excel.yaml -ts ./dl_agent_roster_excel_table_schema.yaml lab      # lab (dev): your own gcloud credentials
py ./dl_agent_roster_excel.py -cf ./dl_agent_roster_excel.yaml -ts ./dl_agent_roster_excel_table_schema.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `dl_agent_roster_excel.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`, `sql_logger`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `demo-analytics-dev` | `value-gcp` | — |
| `write_project` | project | `demo-analytics-dev` | `value-gcp` | `export_active_agents_xlsx`, `export_agent_table_xlsx` (SQL) |
| `landing_bucket` | bucket | `demo_file_test_bucket` | `value-gcp-insights` | `ingest_agent_roster` |
| `landing_path` | other | `agent_roster` | `inbound/agent_roster` | `ingest_agent_roster` |
| `archive_path` | other | `agent_roster/Archive` | `Archive/inbound/agent_roster` | `ingest_agent_roster` |
| `outbound_bucket` | bucket | `demo_file_test_bucket` | `value-gcp-ftp` | `export_active_agents_xlsx`, `export_agent_table_xlsx` |
| `temporary_dataset` | dataset | `clubwide` | `discovery05` | `ingest_agent_roster` |
| `target_dataset` | dataset | `clubwide` | `marketing01` | `ingest_agent_roster`, `export_active_agents_xlsx`, `export_agent_table_xlsx` (SQL) |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `dl_agent_roster_excel.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `ingest_agent_roster` | File ingestion (GCS → BigQuery) | Load the agent roster workbook into staging, history and the agent table, then archive it | stop |
| 2. | `export_active_agents_xlsx` | Export (BigQuery → GCS) | Send the active agents per office to the sales office as an Excel workbook | stop |
| 3. | `export_agent_table_xlsx` | Export (BigQuery → GCS) | Keep a full copy of the agent table as an Excel workbook | stop |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

None: every table this process reads is built by an earlier step of the same process.

## Target tables

Tables this process creates or changes (lab names).

| Table (lab) | Written by | How | Read afterwards by |
|---|---|---|---|
| `demo-analytics-dev.clubwide.agent_roster` | `ingest_agent_roster` | staging: replaced by every file | — |
| `demo-analytics-dev.clubwide.agent_roster_history` | `ingest_agent_roster` | history: every file appended | — |
| `demo-analytics-dev.clubwide.agent_roster_current` | `ingest_agent_roster` | MERGE from the staging table (newest row per key) | `export_active_agents_xlsx`, `export_agent_table_xlsx` |

### Source files

| Step | Files (lab) | What |
|---|---|---|
| `ingest_agent_roster` | `gs://demo_file_test_bucket/agent_roster/*Agent_Roster_*` | Excel (.xlsx) files loaded (name contains 'Agent_Roster_') |

### Output files

| Step | Path (lab) | What |
|---|---|---|
| `ingest_agent_roster` | `gs://demo_file_test_bucket/agent_roster/Archive/` | archive: loaded files are moved here |
| `export_active_agents_xlsx` | `gs://demo_file_test_bucket/outbound/agent_roster/active_agents_<%Y%m%d>.xlsx` | export: Excel (.xlsx) |
| `export_agent_table_xlsx` | `gs://demo_file_test_bucket/outbound/agent_roster/full/agent_roster_<%Y%m%d>.xlsx` | export: Excel (.xlsx) |

## Process steps in detail

### 1. `ingest_agent_roster` · File ingestion (GCS → BigQuery)

Load the agent roster workbook into staging, history and the agent table, then archive it

| | |
|---|---|
| Reads files | `gs://demo_file_test_bucket/agent_roster/*Agent_Roster_*`; when none arrive: log a warning |
| Format | Excel (.xlsx), sheet `Roster`, header `0`, skiprows `2`; read with pandas, typed by `dl_agent_roster_excel_table_schema.yaml` (7 columns) |
| Writes | `demo-analytics-dev.clubwide.agent_roster` (staging: replaced by every file), `demo-analytics-dev.clubwide.agent_roster_history` (history: every file appended), `demo-analytics-dev.clubwide.agent_roster_current` (MERGE from the staging table (newest row per key)) |
| Merge | keys `agent_id` |
| Added columns | `file_batch_id`, `inserted_ts`, `updated_ts`, `file_name` |
| Processed files | moved to `gs://demo_file_test_bucket/agent_roster/Archive` |
| Config | `gcp_projects.<environment>.gcs_config` |
| On failure | the run stops (later steps do not run) |

### 2. `export_active_agents_xlsx` · Export (BigQuery → GCS)

Send the active agents per office to the sales office as an Excel workbook

| | |
|---|---|
| Reads | `demo-analytics-dev.clubwide.agent_roster_current` |
| Writes files | `gs://demo_file_test_bucket/outbound/agent_roster/active_agents_<%Y%m%d>.xlsx` (Excel (.xlsx), sheet `Active agents`, with a header row; written with pandas, at most 1,048,575 rows) |
| Parameters | `@as_of` DATE: today's date |
| Email | the file is attached to the success email |
| Config | `gcp_projects.<environment>.ftp` |
| On failure | the run stops (later steps do not run) |

### 3. `export_agent_table_xlsx` · Export (BigQuery → GCS)

Keep a full copy of the agent table as an Excel workbook

| | |
|---|---|
| Reads | `demo-analytics-dev.clubwide.agent_roster_current` |
| Writes files | `gs://demo_file_test_bucket/outbound/agent_roster/full/agent_roster_<%Y%m%d>.xlsx` (Excel (.xlsx), sheet `Sheet1`, with a header row; written with pandas, at most 1,048,575 rows) |
| Config | `gcp_projects.<environment>.ftp_full` |
| On failure | the run stops (later steps do not run) |

## SQL log

Every SQL statement the script runs is captured with the team's `sql_logger` module (`CaptureSQL.write_to_log`)
in `<credentials' project>.pipeline_audit.pipeline_sql_log`, with status `success` or `failure`, the BigQuery job id,
the environment and the Composer `DAG_ID` / `TASK_ID` when set.

## Notification email

Sent through the internal mail relay (no login) from `pipeline.developer@example.com` to the
`email.receiver` list of the environment's block, when the run succeeds and when a step fails.
Attachments: the `email.attachment` paths plus the files written by `export_active_agents_xlsx`. The body lists the files the run loaded and exported: file name, rows, and the table or gs:// path.
## Files

| File | What |
|---|---|
| `dl_agent_roster_excel.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `dl_agent_roster_excel.yaml` | configuration: one block per environment under `gcp_projects` |
| `dl_agent_roster_excel_table_schema.yaml` | table definition for the file ingestion (`table_definition.table_name` / `table_schema`) |
| `requirements.txt` | Python packages |
