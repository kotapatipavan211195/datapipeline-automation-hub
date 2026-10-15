# vendor_file_copy

Moves the daily vendor files from the SFTP drop bucket to the landing folder and copies the outbound extract to the vendor's pickup folder.

| | |
|---|---|
| Author(s) | P10000 |
| Reviewer(s) | — |
| JIRA/SN # | DE-2211 |
| Owner | pipeline.developer@example.com |
| Team | Membership |
| Script | `dl_vendor_file_copy.py` with `dl_vendor_file_copy.yaml` |
| Generated | Datapipeline Automation Hub 3.4.0 on 2026-01-01 · spec `f345712f3fb9` |
| ADC identity at generation | `pipeline.developer@example.com` (also the email sender) |

## Run

```bash
py ./dl_vendor_file_copy.py -cf ./dl_vendor_file_copy.yaml lab      # lab (dev): your own gcloud credentials
py ./dl_vendor_file_copy.py -cf ./dl_vendor_file_copy.yaml value    # value (prod): the Composer service account
```

The environment comes last and picks the block of `dl_vendor_file_copy.yaml` (`gcp_projects.<environment>`). Credentials come from
`google.auth.default()`: your gcloud login in lab (`gcloud auth application-default login`), the service account on
Composer. The script imports the team's shared modules (`read_config_yaml`) from
`/data/py_scripts` / `/data/data_eng/py_scripts`.

A failed step raises an error, so the process exits with a non-zero code and the orchestrator marks the task failed.
Every step logs `Function <name> execution begin` / `execution end: <seconds> seconds.`, and `main` logs the total.

## Environments

| Variable | Group | lab (dev) | value (prod) | Used in |
|---|---|---|---|---|
| `read_project` | project | `demo-analytics-dev` | `value-gcp` | — |
| `write_project` | project | `demo-analytics-dev` | `value-gcp` | — |
| `vendor_bucket` | bucket | `demo_vendor_drop_test` | `value-gcp-vendor-drop` | `move_vendor_files`, `copy_outbound_extract` |
| `landing_bucket` | bucket | `demo_file_test_bucket` | `value-gcp-insights` | `move_vendor_files` |
| `ftp_bucket` | bucket | `demo_ftp_test_bucket` | `value-gcp-ftp` | `copy_outbound_extract` |

The hub only ever checks and test-runs **lab**; the **value** (prod) values are written into the YAML but never
used by the hub.

## What it does

`main()` runs these steps in order. Each step is a plain function in `dl_vendor_file_copy.py`.

| # | Function | Type | What it does | On failure |
|---|---|---|---|---|
| 1. | `move_vendor_files` | Copy files (gcloud storage) | Move today's vendor files from the drop bucket to the landing folder | stop |
| 2. | `copy_outbound_extract` | Copy files (gcloud storage) | Copy today's outbound extract to the vendor pickup folder | stop |

## Dependencies (source tables)

Tables this process reads but does not build itself (lab names). They must exist, and be up to date, before a run.

None: every table this process reads is built by an earlier step of the same process.

## Target tables

Tables this process creates or changes (lab names).

None: this process does not write BigQuery tables.

### Source files

| Step | Files (lab) | What |
|---|---|---|
| `move_vendor_files` | `gs://demo_vendor_drop_test/outbound/Daily Vendor File_*.csv` | moved by gcloud storage mv |
| `copy_outbound_extract` | `gs://demo_ftp_test_bucket/outbound/member_extract_{date:%Y_%m_%d}.txt` | read by gcloud storage cp |

### Output files

| Step | Path (lab) | What |
|---|---|---|
| `move_vendor_files` | `gs://demo_file_test_bucket/inbound/vendor/` | files moved here |
| `copy_outbound_extract` | `gs://demo_vendor_drop_test/inbound/` | files copied here |

## Process steps in detail

### 1. `move_vendor_files` · Copy files (gcloud storage)

Move today's vendor files from the drop bucket to the landing folder

| | |
|---|---|
| Direction | Bucket → bucket |
| Command | `gcloud storage mv --no-clobber gs://demo_vendor_drop_test/outbound/Daily Vendor File_*.csv gs://demo_file_test_bucket/inbound/vendor/` |
| Config | `gcp_projects.<environment>.file_copy` |
| On failure | the run stops (later steps do not run) |

### 2. `copy_outbound_extract` · Copy files (gcloud storage)

Copy today's outbound extract to the vendor pickup folder

| | |
|---|---|
| Direction | Bucket → bucket |
| Command | `gcloud storage cp gs://demo_ftp_test_bucket/outbound/member_extract_{date:%Y_%m_%d}.txt gs://demo_vendor_drop_test/inbound/` |
| Config | `gcp_projects.<environment>.file_copy_2` |
| On failure | the run stops (later steps do not run) |

## Notification email

Sent through the internal mail relay (no login) from `pipeline.developer@example.com` to the
`email.receiver` list of the environment's block, when a step fails.

## Copying files

Copy steps run the Google Cloud CLI (`gcloud storage cp | mv | rsync`, the successor of `gsutil`), so `gcloud` must be
installed and on the PATH of the machine that runs the script, logged in as the same identity.

## Files

| File | What |
|---|---|
| `dl_vendor_file_copy.py` | the pipeline (prod template: header, module imports, one function per step, `main(args)`) |
| `dl_vendor_file_copy.yaml` | configuration: one block per environment under `gcp_projects` |
| `requirements.txt` | Python packages |
