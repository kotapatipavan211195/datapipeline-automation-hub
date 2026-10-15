# Runbook: running a generated pipeline script

Every bundle the Datapipeline Automation Hub generates follows the team's production template: a Python script, its
YAML with one block per environment (`gcp_projects.lab / value [/ qa]`) and, for a file ingestion, the table schema
YAML. It imports the team's shared modules and has no dependency on the hub. The examples use a script named
`dl_script`. Each bundle's own `README.md` has the same commands with its real names, plus its environments, its
dependency and target tables and every step in detail.

## 1. Install

On the pipeline servers and Composer, the team modules are already in `/data/data_eng/py_scripts/modules`
(`read_config_yaml`, `sql_logger`, `gsm_module`). The script puts `/data/py_scripts` and `/data/data_eng/py_scripts`
first on `sys.path` (the *Module paths* block), so nothing else is needed there beyond the Python packages:

```bash
pip install -r requirements.txt     # google-cloud-bigquery/storage, PyYAML (+ pandas, pyarrow, db-dtypes for an ingestion
                                    # or an Excel export, + openpyxl for Excel)
```

On a laptop, put a `modules/` folder (a copy of the team modules) next to the script, or run the script on a machine
that has `/data/...`. The hub's **Test run in lab** does this for you.

A **Copy files** step runs the Google Cloud CLI: `gcloud` must be installed and on the PATH of the machine that runs the
script (Composer workers have it).

## 2. Credentials and permissions

Like the team template, the script calls `google.auth.default()`:

- **lab (dev):** your own IAM, after `gcloud auth application-default login`. `project_id` is the project of your
  gcloud configuration (`gcloud config configurations list`). Jobs are billed there, and `sql_logger` writes there.
- **value (prod):** the service account of the Composer environment.

| The identity running the script needs | On |
|---|---|
| `roles/bigquery.jobUser` | the credentials' project (where the jobs run) |
| `roles/bigquery.dataViewer` | datasets of the **dependency** tables (README → *Dependencies*) |
| `roles/bigquery.dataEditor` | datasets of the **target** tables (README → *Target tables*), and `pipeline_audit` for `sql_logger` |
| `roles/storage.objectViewer` / `objectAdmin` | buckets it reads / archives / extracts to / copies to |
| `roles/secretmanager.secretAccessor` | each secret, in the environment's `gsm_project` |
| network access to the mail relay | when the process sends email |

## 3. Run it

```bash
py ./dl_script.py -cf ./dl_script.yaml lab                                     # lab (dev)
py ./dl_script.py -cf ./dl_script.yaml value                                   # value (prod)
py ./dl_script.py -cf ./dl_script.yaml -ts ./dl_script_table_schema.yaml lab   # with a file ingestion
```

The environment comes last and picks the YAML block (`gcp_projects.<environment>`); argparse also accepts it first.
`-cf` and `-ts` are paths relative to the working directory (or absolute). Their defaults are the bundle's file names.
The generated README and the hub's Generate page list these commands for every environment of the process.

What a run does:

1. Logs `Function main execution begin`, `Parms-> Environment: lab` and the read/write projects.
2. Starts `sql_logger.CaptureSQL(...)` (when the hub's sql_logger setting is on).
3. Reads `gcp_projects.<environment>` from the YAML, and the table definition for an ingestion.
4. Calls each step function in order. Every step logs `Function <name> execution begin` and
   `Function <name> execution end:  <seconds> seconds.`; SQL steps also log the target table's rows and columns.
5. Sends the success email (if configured), then logs the total: `Function main execution end:  <seconds> seconds.`

A failing step raises `RuntimeError`. The run then sends the failure email (if configured) and ends with the error.
The exit code is non-zero, so the orchestrator marks the task failed. A step marked *on failure: continue* is logged
and skipped over, and the run still ends as failed after the remaining steps.

## 4. Hand-off to the orchestrator

The orchestrator only needs the command line. In Airflow / Composer, for example:

```python
BashOperator(
    task_id="dl_script",
    bash_command="cd /data/data_eng/py_scripts/dl_script && python ./dl_script.py -cf ./dl_script.yaml value",
)
```

`sql_logger` records the Composer `DAG_ID` and `TASK_ID` environment variables with every SQL statement, when they
are set.

Dates in the SQL are relative to the day the script runs (`date.today()`), or fixed values per environment in the YAML
(for example `cutoff_date`). To process another date, change that YAML value. The hub does not generate a run-date
argument.

## 5. SQL log (sql_logger)

Every SQL statement the script runs is written to `<credentials' project>.pipeline_audit.pipeline_sql_log` with
`CaptureSQL.write_to_log(step, sql, 'success' | 'failure', job_id)`. Each row has the DAG/task ids, the script, the
step, its order, the SQL, the job id, the status, the environment and a log id shared by the whole run. If the log
table is not reachable, the first SQL step fails: *Validate all* in the hub checks the table in the hub's ADC project.
Whether scripts use sql_logger is a hub setting (`SF_SQL_LOGGER_ENABLED`).

## 6. File ingestion

Each run lists `gcs_read_bucket_path` and loads every file whose name contains `filename` (and has the format's
extension). Files under the archive folder are skipped. For each file:

1. pandas reads it, typed by the table schema YAML (and adds `file_batch_id`, `inserted_ts`, `updated_ts`,
   `file_name` when batch columns are on). A fixed-width file is split with `pandas.read_fwf` by the
   `column_widths` of the table schema YAML; spaces around each value are trimmed, and a line shorter than the layout
   leaves its last columns empty. An Excel file is read with `pandas.read_excel` from `gcs_config.sheet_name` (the
   first sheet when empty), with the same `header` / `skiprows`;
2. it is loaded into the staging table (replaced) and appended to `<table_name>_history`;
3. it is MERGEd into the target table (newest row per key wins) or inserted, depending on the step;
4. it is moved to the archive folder.

Empty files are archived and skipped. When no file arrives, the step emails the team ("There is no … file received
today."), logs a warning, or fails, as set in the hub. To load a file again, move it back from the archive folder.

## 7. Copy files

A copy step (or each job of a copy-files process) runs `gcloud storage cp | mv | rsync` (the successor of `gsutil`) with the sources and destination from its
YAML block. `{date:%Y%m%d}` in a path becomes today's date. gcloud's own progress lines go to the log; a non-zero exit
fails the step with gcloud's message. `mv` deletes the source files after copying them; `rsync` with *delete unmatched*
deletes destination files that are not in the source.

## 8. Google Secret Manager (gsm_module)

Secrets are read at the start of `main()` by `get_gsm_secret()` (the team's `gsm_module`), from the environment's
`gsm_project`, into the `SECRETS` dictionary: each holds the whole payload (JSON becomes a dict, e.g.
`{"api_credentials": [{"api": "…"}]}`), for example an API key or a source-system connection. The steps read the keys
they need; a custom step takes the payload keys from its YAML block (`secret_list_key`, `secret_value_key`), so when a
secret's layout changes you edit the YAML. Only the secret id and version are in the hub, the YAML and the code; values
are read at run time and never logged. An empty payload stops the run (`Could not retrieve secret … empty payload`).

```bash
gcloud secrets create pdw-conn --project <gsm_project> --data-file=-   # paste the JSON value, then Ctrl+D
```

## 9. Notification email

The email goes through the internal relay (host and port are hub settings, `SF_SMTP_HOST` / `SF_SMTP_PORT`: `smtp.example.com:25` with STARTTLS by
default; no login behind the VPN), from `email.sender` (the ADC identity at generation) to `email.receiver` of the
environment's block:

- on **success**, at the end of the run, with the files written by exports marked *attach* plus `email.attachment`;
- on **failure**, before the error ends the run. A failure to send the failure email is logged and does not hide the
  original error.

The subject and message come from the hub (`{status}`, `{script}`, `{process}`, `{environment}`, `{date}` are filled
in), followed by the team signature.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: No module named 'modules'` | The team modules are not on the machine: run on a pipeline server or put a `modules/` copy next to the script |
| `RuntimeError: [Errno 2] No such file or directory: 'dl_script.yaml'` (from `read_configuration`) | Wrong `-cf` path: give it relative to the working directory, or absolute |
| `RuntimeError: 'qa'` from `read_configuration` | The YAML has no block for that environment |
| `DefaultCredentialsError` at start-up | No ADC on the machine: `gcloud auth application-default login`, or attach a service account |
| `403 … Access Denied` on a query | Grant the runner the BigQuery roles from section 2 on that project/dataset |
| The first SQL step fails with `…pipeline_audit.pipeline_sql_log` not found | `sql_logger` writes in the credentials' project: create the table there, or run with the right gcloud project |
| `No valid files to process` | No file name contains `filename` in that folder yet, or the files were already archived |
| `gcloud CLI not found on PATH` | Install the Google Cloud CLI on that machine |
| `gcloud storage cp failed (exit code 1): … matched no objects` | The source matched no file: check the path and the `{date}` format |
| `PermissionDenied … secretmanager` | Grant `roles/secretmanager.secretAccessor` on the secret in `gsm_project` |
| `Error occured while sending the email` with `TimeoutError` / `ConnectionRefusedError` | The machine cannot reach the relay (off the VPN, or a firewall) |
| `Data-quality check(s) failed: …` | An *error* check failed; the log line before it shows the value and the expectation |
