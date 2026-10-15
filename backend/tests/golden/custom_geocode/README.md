# member_geocode

Refreshes the active member addresses and geocodes them with the maps API (key from Secret Manager) into the member geocode table.

| | |
|---|---|
| Author(s) | P10000 |
| Reviewer(s) | — |
| JIRA/SN # | DE-2300 |
| Owner | pipeline.developer@example.com |
| Team | Membership |
| Script | `dl_member_geocode.py` with `dl_member_geocode.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `77664746566f` |
| ADC identity at generation | `pipeline.developer@example.com` |

## Run

```bash
py ./dl_member_geocode.py -cf ./dl_member_geocode.yaml lab      # lab (dev): your own gcloud credentials
py ./dl_member_geocode.py -cf ./dl_member_geocode.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `dl_member_geocode.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`, `sql_logger`, `gsm_module`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `demo-analytics-dev` | `value-gcp` | `refresh_member_addresses` (SQL) |
| `write_project` | project | `demo-analytics-dev` | `value-gcp` | `refresh_member_addresses`, `geocode_member_addresses` (SQL) |
| `gsm_project` | project | `demo-analytics-dev` | `value-gcp` | — |
| `member_dataset` | dataset | `membership_lab` | `membership` | `refresh_member_addresses`, `geocode_member_addresses` (SQL) |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `dl_member_geocode.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `refresh_member_addresses` | SQL (DDL) | Rebuild the table of active member addresses to geocode | stop |
| 2. | `geocode_member_addresses` | Custom code | Geocode the member addresses with the maps API and store latitude/longitude | stop |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

| Table (lab) | Read by | Template |
|---|---|---|
| `demo-analytics-dev.membership.members` | `refresh_member_addresses` | `{read_project}.membership.members` |

## Target tables

Tables this process creates or changes (lab names).

| Table (lab) | Written by | How | Read afterwards by |
|---|---|---|---|
| `demo-analytics-dev.membership_lab.member_addresses` | `refresh_member_addresses` | CREATE OR REPLACE TABLE | — |

## Process steps in detail

### 1. `refresh_member_addresses` · SQL (DDL)

Rebuild the table of active member addresses to geocode

| | |
|---|---|
| Statement | DDL |
| Reads | `demo-analytics-dev.membership.members` |
| Writes | `demo-analytics-dev.membership_lab.member_addresses` (CREATE OR REPLACE TABLE) |
| Config | variables only (see Environments) |
| On failure | the run stops (later steps do not run) |

### 2. `geocode_member_addresses` · Custom code

Geocode the member addresses with the maps API and store latitude/longitude

| | |
|---|---|
| Custom code | written with Gemini (gemini-2.5-pro) from: Take the API key from the secret geocode_api_key (the 'api' value of the first 'api_credentials' entry), geocode every address of address_table with the maps geocoding API and write member_id, latitude, longitude and geocoded_ts into result_table. |
| Settings | `secret_list_key`, `secret_value_key`, `geocode_url`, `address_table`, `result_table` |
| Secrets | `geocode_api_key` |
| Imports | `import requests` |
| Review | the hub only checks that this code compiles and passes ruff; the lab test run is what proves it works |
| Config | `gcp_projects.<environment>.geocode_config` |
| On failure | the run stops (later steps do not run) |

## SQL log

Every SQL statement the script runs is captured with the team's `sql_logger` module (`CaptureSQL.write_to_log`)
in `<credentials' project>.pipeline_audit.pipeline_sql_log`, with status `success` or `failure`, the BigQuery job id,
the environment and the Composer `DAG_ID` / `TASK_ID` when set.

## Secrets (Google Secret Manager)

Read at the start of `main()` by `get_gsm_secret()` with the team's `gsm_module`, from the environment's
`gsm_project`. `SECRETS['<name>']` holds the whole payload: a dict for JSON (e.g.
`{"api_credentials": [{"api": "…"}]}`), else the text. The steps pick what they need from it; the payload keys they
read (e.g. `secret_list_key: api_credentials`, `secret_value_key: api`) are settings in the YAML, so a change of the
secret's layout is a YAML change. The identity running the script needs `roles/secretmanager.secretAccessor` on them.

| Name | Secret id | Version | What it holds |
|---|---|---|---|
| `geocode_api_key` | `maps-geocode-api-key` | latest | maps geocoding API key: JSON {"api_credentials": [{"api": "<key>"}]} |

## Files

| File | What |
|---|---|
| `dl_member_geocode.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `dl_member_geocode.yaml` | configuration: one block per environment under `gcp_projects` |
| `requirements.txt` | Python packages |
