# customer_snapshot

Prepares a staging table, loads the CRM customer extract (pipe-delimited .txt), merges it into the customer dimension, publishes a Parquet snapshot and a .txt contact list.

| | |
|---|---|
| Author(s) | P20001 |
| Reviewer(s) | — |
| JIRA/SN # | SN-3001 |
| Owner | crm.team@example.com |
| Team | CRM |
| Script | `dl_customer_snapshot.py` with `dl_customer_snapshot.yaml` and `dl_customer_snapshot_table_schema.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `07c8d16e908c` |
| ADC identity at generation | `pipeline.developer@example.com` |

## Run

```bash
py ./dl_customer_snapshot.py -cf ./dl_customer_snapshot.yaml -ts ./dl_customer_snapshot_table_schema.yaml lab      # lab (dev): your own gcloud credentials
py ./dl_customer_snapshot.py -cf ./dl_customer_snapshot.yaml -ts ./dl_customer_snapshot_table_schema.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `dl_customer_snapshot.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`, `sql_logger`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `acme-analytics-dev` | `acme-analytics-prod` | — |
| `write_project` | project | `acme-analytics-dev` | `acme-analytics-prod` | `merge_customer_dimension`, `publish_customer_snapshot`, `export_customer_contacts_txt`, `refresh_customer_counts` (SQL) |
| `crm_dataset` | dataset | `crm` | `crm` | `load_crm_customer_extract`, `merge_customer_dimension`, `publish_customer_snapshot`, `export_customer_contacts_txt`, `refresh_customer_counts` (SQL) |
| `crm_bucket` | bucket | `acme-crm-dropzone` | `acme-crm-dropzone-prod` | `load_crm_customer_extract`, `publish_customer_snapshot`, `export_customer_contacts_txt`, `copy_contacts_to_share` |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `dl_customer_snapshot.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `load_crm_customer_extract` | File ingestion (GCS → BigQuery) | Load the latest pipe-delimited CRM customer extract (.txt) into crm.customer_stg | stop |
| 2. | `merge_customer_dimension` | SQL (SCRIPT) | Upsert staged customers into crm.dim_customer and log the change count | stop |
| 3. | `publish_customer_snapshot` | Export (BigQuery → GCS) | Publish the full customer dimension as Parquet for downstream teams | log and continue |
| 4. | `export_customer_contacts_txt` | Export (BigQuery → GCS) | Write today's changed customer contacts as a pipe-delimited .txt file for the mailing vendor | stop |
| – | `refresh_customer_counts` | SQL (SELECT) | Rebuild the customer counts report table *(disabled)* | stop |
| 5. | `copy_contacts_to_share` | Copy files (gcloud storage) | Copy the contact files to the vendor share on the pipeline server | log and continue |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

None: every table this process reads is built by an earlier step of the same process.

## Target tables

Tables this process creates or changes (lab names).

| Table (lab) | Written by | How | Read afterwards by |
|---|---|---|---|
| `acme-analytics-dev.crm.customer_stg` | `load_crm_customer_extract` | staging: replaced by every file | `merge_customer_dimension` |
| `acme-analytics-dev.crm.customer_extract` | `load_crm_customer_extract` | TRUNCATE (first file of the run) + INSERT from the staging table | — |
| `acme-analytics-dev.crm.dim_customer` | `merge_customer_dimension` | MERGE | `publish_customer_snapshot`, `export_customer_contacts_txt` |

### Source files

| Step | Files (lab) | What |
|---|---|---|
| `load_crm_customer_extract` | `gs://acme-crm-dropzone/exports/customers/*customers_*` | TXT (delimited text) files loaded (name contains 'customers_') |
| `copy_contacts_to_share` | `gs://acme-crm-dropzone/outbound/contacts/customer_contacts_{date:%Y_%m_%d}-*.txt` | read by gcloud storage cp |

### Output files

| Step | Path (lab) | What |
|---|---|---|
| `load_crm_customer_extract` | `gs://acme-crm-dropzone/archive/` | archive: loaded files are moved here |
| `publish_customer_snapshot` | `gs://acme-crm-dropzone/snapshots/dim_customer/dim_customer_<%Y%m%d>-*.parquet` | export: Parquet, snappy |
| `export_customer_contacts_txt` | `gs://acme-crm-dropzone/outbound/contacts/customer_contacts_<%Y_%m_%d>-*.txt` | export: TXT (delimited text) |
| `copy_contacts_to_share` | `/data/outbound/vendor_share/` | files copied here (local) |

## Process steps in detail

### 1. `load_crm_customer_extract` · File ingestion (GCS → BigQuery)

Load the latest pipe-delimited CRM customer extract (.txt) into crm.customer_stg

| | |
|---|---|
| Reads files | `gs://acme-crm-dropzone/exports/customers/*customers_*`; when none arrive: log a warning |
| Format | TXT (delimited text), separator `\|`, header `0`, skiprows `None`; read with pandas, typed by `dl_customer_snapshot_table_schema.yaml` (4 columns) |
| Writes | `acme-analytics-dev.crm.customer_stg` (staging: replaced by every file), `acme-analytics-dev.crm.customer_extract` (TRUNCATE (first file of the run) + INSERT from the staging table) |
| Added columns | `file_batch_id`, `inserted_ts`, `updated_ts`, `file_name` |
| Processed files | moved to `gs://acme-crm-dropzone/archive` |
| Config | `gcp_projects.<environment>.gcs_config` |
| On failure | the run stops (later steps do not run) |

### 2. `merge_customer_dimension` · SQL (SCRIPT)

Upsert staged customers into crm.dim_customer and log the change count

| | |
|---|---|
| Statement | SCRIPT |
| Reads | `acme-analytics-dev.crm.customer_stg` |
| Writes | `acme-analytics-dev.crm.dim_customer` (MERGE) |
| Config | variables only (see Environments) |
| On failure | the run stops (later steps do not run) |

### 3. `publish_customer_snapshot` · Export (BigQuery → GCS)

Publish the full customer dimension as Parquet for downstream teams

| | |
|---|---|
| Reads | `acme-analytics-dev.crm.dim_customer` |
| Writes files | `gs://acme-crm-dropzone/snapshots/dim_customer/dim_customer_<%Y%m%d>-*.parquet` (Parquet, snappy compressed) |
| Config | `gcp_projects.<environment>.ftp` |
| On failure | logged; the run continues with the next step and ends as failed |

### 4. `export_customer_contacts_txt` · Export (BigQuery → GCS)

Write today's changed customer contacts as a pipe-delimited .txt file for the mailing vendor

| | |
|---|---|
| Reads | `acme-analytics-dev.crm.dim_customer` |
| Writes files | `gs://acme-crm-dropzone/outbound/contacts/customer_contacts_<%Y_%m_%d>-*.txt` (TXT (delimited text), delimiter `\|`, with a header row) |
| Parameters | `@run_date` DATE: today's date |
| Config | `gcp_projects.<environment>.ftp_contacts` |
| On failure | the run stops (later steps do not run) |

### 5. `copy_contacts_to_share` · Copy files (gcloud storage)

Copy the contact files to the vendor share on the pipeline server

| | |
|---|---|
| Direction | Bucket → local folder (local = the machine that runs the script) |
| Command | `gcloud storage cp --no-clobber gs://acme-crm-dropzone/outbound/contacts/customer_contacts_{date:%Y_%m_%d}-*.txt /data/outbound/vendor_share/` |
| Config | `gcp_projects.<environment>.file_copy` |
| On failure | logged; the run continues with the next step and ends as failed |

Disabled steps (their calls are commented out in `main()`): `refresh_customer_counts`

## SQL log

Every SQL statement the script runs is captured with the team's `sql_logger` module (`CaptureSQL.write_to_log`)
in `<credentials' project>.pipeline_audit.pipeline_sql_log`, with status `success` or `failure`, the BigQuery job id,
the environment and the Composer `DAG_ID` / `TASK_ID` when set.

## Copying files

Copy steps run the Google Cloud CLI (`gcloud storage cp | mv | rsync`, the successor of `gsutil`), so `gcloud` must be
installed and on the PATH of the machine that runs the script, logged in as the same identity.

## Files

| File | What |
|---|---|
| `dl_customer_snapshot.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `dl_customer_snapshot.yaml` | configuration: one block per environment under `gcp_projects` |
| `dl_customer_snapshot_table_schema.yaml` | table definition for the file ingestion (`table_definition.table_name` / `table_schema`) |
| `requirements.txt` | Python packages |
