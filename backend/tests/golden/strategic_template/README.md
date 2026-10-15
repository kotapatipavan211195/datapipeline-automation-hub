# strategic_template

Builds the prod_script_test table from the online quote conversion model scores, extracts the active members table to the FTP bucket as a dated .txt file and emails it to the team.

| | |
|---|---|
| Author(s) | P10000 |
| Reviewer(s) | — |
| JIRA/SN # | DE-1234 |
| Owner | pipeline.developer@example.com |
| Team | Data Research & Analytics |
| Script | `dl_strategic_pipeline_template.py` with `dl_strategic_pipeline_template.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `97c2f17446f6` |
| ADC identity at generation | `pipeline.developer@example.com` (also the email sender) |

## Run

```bash
py ./dl_strategic_pipeline_template.py -cf ./dl_strategic_pipeline_template.yaml lab      # lab (dev): your own gcloud credentials
py ./dl_strategic_pipeline_template.py -cf ./dl_strategic_pipeline_template.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `dl_strategic_pipeline_template.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`, `sql_logger`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `value-confidential` | `value-confidential` | — |
| `write_project` | project | `demo-analytics-dev` | `value-gcp` | `build_prod_script_test`, `extract_data_file_csv` (SQL) |
| `w_model_scores_dataset` | dataset | `lab_w_model_scores` | `w_model_scores` | `build_prod_script_test` (SQL) |
| `strategic_dataset` | dataset | `lab_strategic` | `strategic` | `build_prod_script_test` (SQL) |
| `membership_dataset` | dataset | `lab_membership` | `membership` | `extract_data_file_csv` (SQL) |
| `ftp_bucket` | bucket | `demo_file_test_bucket` | `value-gcp-insights` | `extract_data_file_csv` |
| `ftp_path` | other | `outbound/test` | `outbound/rpm` | `extract_data_file_csv` |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `dl_strategic_pipeline_template.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `build_prod_script_test` | SQL (DDL) | Rebuild prod_script_test from the latest online quote conversion predictions | stop |
| 2. | `extract_data_file_csv` | Export (BigQuery → GCS) | Extract the active members table to the FTP bucket as a dated comma-delimited .txt file | stop |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

| Table (lab) | Read by | Template |
|---|---|---|
| `demo-analytics-dev.lab_w_model_scores.prediction_ins_auto_online_quote_conversion` | `build_prod_script_test` | `{write_project}.{w_model_scores_dataset}.prediction_ins_auto_online_quote_conversion` |
| `demo-analytics-dev.lab_membership.ms_acq_dm_active_members` | `extract_data_file_csv` | `{write_project}.{membership_dataset}.ms_acq_dm_active_members` |

## Target tables

Tables this process creates or changes (lab names).

| Table (lab) | Written by | How | Read afterwards by |
|---|---|---|---|
| `demo-analytics-dev.lab_strategic.prod_script_test` | `build_prod_script_test` | CREATE OR REPLACE TABLE | — |

### Output files

| Step | Path (lab) | What |
|---|---|---|
| `extract_data_file_csv` | `gs://demo_file_test_bucket/outbound/test/ms_acq_dm_active_members_<%Y_%m_%d>.txt` | export: TXT (delimited text) |

## Process steps in detail

### 1. `build_prod_script_test` · SQL (DDL)

Rebuild prod_script_test from the latest online quote conversion predictions

| | |
|---|---|
| Statement | DDL |
| Reads | `demo-analytics-dev.lab_w_model_scores.prediction_ins_auto_online_quote_conversion` |
| Writes | `demo-analytics-dev.lab_strategic.prod_script_test` (CREATE OR REPLACE TABLE) |
| Parameters | `@score_start` DATE: first day of this month |
| Config | variables only (see Environments) |
| On failure | the run stops (later steps do not run) |

### 2. `extract_data_file_csv` · Export (BigQuery → GCS)

Extract the active members table to the FTP bucket as a dated comma-delimited .txt file

| | |
|---|---|
| Reads | `demo-analytics-dev.lab_membership.ms_acq_dm_active_members` |
| Writes files | `gs://demo_file_test_bucket/outbound/test/ms_acq_dm_active_members_<%Y_%m_%d>.txt` (TXT (delimited text), delimiter `,`, with a header row) |
| Email | the file is attached to the success email |
| Config | `gcp_projects.<environment>.ftp` |
| On failure | the run stops (later steps do not run) |

## SQL log

Every SQL statement the script runs is captured with the team's `sql_logger` module (`CaptureSQL.write_to_log`)
in `<credentials' project>.pipeline_audit.pipeline_sql_log`, with status `success` or `failure`, the BigQuery job id,
the environment and the Composer `DAG_ID` / `TASK_ID` when set.

## Notification email

Sent through the internal mail relay (no login) from `pipeline.developer@example.com` to the
`email.receiver` list of the environment's block, when the run succeeds and when a step fails.
Attachments: the `email.attachment` paths plus the files written by `extract_data_file_csv`.
## Files

| File | What |
|---|---|
| `dl_strategic_pipeline_template.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `dl_strategic_pipeline_template.yaml` | configuration: one block per environment under `gcp_projects` |
| `requirements.txt` | Python packages |
