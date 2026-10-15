# Datapipeline Automation Hub

A web app for building BigQuery/GCS data pipelines **to the team's production standard**. You describe a process in a
guided wizard:
- the SQL to run, pasted as you run it today
- the files to ingest the team way (pandas → staging → history → MERGE into the target → archive), including
  fixed-width `.txt` files described by each column's length and Excel `.xlsx` workbooks
- the extracts to write to a bucket (a dated CSV/TXT/JSON/Parquet/Avro/Excel file, e.g. for the FTP bucket)
- files to copy or move between buckets and local folders (`gcloud storage`)
- data-quality checks, Google Secret Manager secrets (`gsm_module`) and a notification email

The hub then generates the script the way the team writes it by hand (header block, `sys.path` module imports,
`CONFIG` + logger, one function per step with begin/end timing, `main(args)`), its YAML with one block per environment
(`gcp_projects: lab / value [/ qa]`) and, for an ingestion, the table schema YAML. Your orchestrator runs it as:

```
py ./dl_script.py -cf ./dl_script.yaml lab        # lab (dev): your gcloud credentials
py ./dl_script.py -cf ./dl_script.yaml value      # value (prod): the Composer service account
```

The home page has four cards: **New pipeline** (the wizard below), **Copy files** (bucket → bucket, your
computer → bucket and bucket → your computer, right now from the browser; or a script that does it every day),
**Enhance a script** (bring an existing script and its YAML up to the standard: imported as a process when every step
maps to a hub step, otherwise rewritten in place by Gemini and checked by the hub; either way it keeps its author header
block and service-account keys give way to ADC) and **Find files** (search one or more
buckets for a file name or pattern, at any folder depth; a file found can start a new pipeline that ingests files like
it). The search box in the header finds processes from any page. Owners can delete their processes from the list.

When you paste SQL, the hub lists the tables it reads and writes and turns the hard-coded projects into
`${write_project}` / `${read_project}` (both `value-gcp` in prod by default) or, for any other project, a
`${<name>_project}` with the same name in every environment. Gemini (through `google-genai` on Vertex AI) can also parameterize datasets and
dates. Either way, the hub **proves the logic is unchanged** before anything is generated.

**Lab only.** The hub checks and runs **lab** (dev) only. It acts as if it had no access to prod: the **value** (prod)
values are written into the YAML and never used for a dry run, a listing or a run. Besides the BigQuery dry runs,
**Test run in lab** really runs the generated script in lab from the hub, with the lab values, and shows its log and
step timings.

**Documentation**

| | |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | the process, stage by stage: from pasting SQL to a lab-tested script |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | running a generated script: modules, environments, orchestrator hand-off, SQL log, secrets, email, copy, troubleshooting |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | every key of the generated YAML and of the table schema YAML |
| [deploy/README_DEPLOY.md](deploy/README_DEPLOY.md) | Cloud Run behind IAP, service account, permissions, hub settings |

Everything is Python: FastAPI for the REST API and NiceGUI for the UI, served from one container. No Node build is needed.

```
script_automation/
├── backend/
│   ├── app/
│   │   ├── models/        ProcessSpec v3 (environments, steps), migrate (v1/v2 → v3), schema + analysis models, rules
│   │   ├── services/      renderer (Jinja → black → guards → ruff → offline smoke run), smoke_run, test_runner (lab),
│   │   │                  sql_analyzer (project scan, Gemini, equivalence), sql_utils (sqlglot), lineage,
│   │   │                  schema_infer, validators (lab only), adc (ADC identity), workspace (GCS/local), gemini
│   │   ├── vendor/modules/ hub copies of the team modules (read_config_yaml, sql_logger, gsm_module) for test runs
│   │   ├── sandbox/       fake google package + sitecustomize for the offline smoke run
│   │   ├── api/           REST endpoints (/api/...)
│   │   ├── ui/            NiceGUI pages: dashboard + 6-stage wizard
│   │   ├── prompts/       versioned Gemini prompts
│   │   ├── settings.py    hub settings, incl. the generated-code standards (module paths, sql_logger, mail relay)
│   │   ├── main.py        create_app(): API + UI
│   │   └── cli.py         generate/validate a spec.json from the command line
│   ├── templates/         what gets generated (script partials, README, requirements)
│   └── tests/             engine, analyzer, lineage, validators, runner, API and UI tests (+ golden bundles)
├── docs/                  user guide, runbook, config reference
├── samples/               example specs (strategic_template, engagement_file_ingest, sales_daily, customer_snapshot)
└── deploy/                Dockerfile, Cloud Build, one-time GCP setup, deployment guide
```

## The wizard in short

1. **Basics**:
   - the process name and the **script file name** (`dl_script` → `dl_script.py` + `dl_script.yaml`)
   - the **script header**: Author(s) PIDs, Reviewer(s), JIRA/SN #, Updates lines (*Created on* is set by the first saved version)
   - **Environments**: `read_project` / `write_project` for lab and value, and an optional qa environment
   - There is no run date and no schedule: dates are relative to the day the script runs, and your orchestrator decides when it runs.
2. **Variables & secrets**:
   - datasets, buckets and folders with one value per environment
   - Google Secret Manager entries (id, `latest` or a pinned version) read with `gsm_module` from each environment's `gsm_project`;
     the script keeps each secret's whole payload, and custom steps read its keys (e.g. `api_credentials` → `api`) from their YAML settings
3. **Pipeline**: the steps (**Ingest files**, **Run SQL**, **Export to GCS**, **Copy files**, **Data quality**,
   **Custom process (AI)**) in the order `main()` calls them, with a live preview of `def main(args)` and an order check.
   A custom process is described in plain words (e.g. "connect to DB2 with the credentials in Secret Manager and load
   the members"); Gemini writes the code inside the team's standard step function, and the hub checks it.
4. **Step details**: one tab per step. Pasted (or attached `.sql` / `.txt`) SQL is scanned on the spot; long SQL folds
   away behind a summary bar. Step fields take `{variables}` and preview their lab and value values. Fixed-width
   ingestions take a length per column, pasted as a layout or typed, and can be checked against a sample file. Column
   types of a CSV, TXT, fixed-width or Excel sample can come from **BigQuery's own guess**: the hub loads up to 1,000
   sample rows into a temporary lab table with schema auto-detect, takes the types and deletes the table. An
   ingestion may skip the target table: the raw rows then stay in the staging table (this run's files) for the next
   SQL steps.
5. **Email**:
   - the sender is the ADC identity's email (read-only)
   - **To** per environment, as comma-separated addresses
   - when to send, the subject and the message
   - attachments: the files exports write, plus gs:// or local paths
   - optionally, a list of the files loaded and exported: file name, rows, and the table or gs:// path
   - The mail relay (host and port) is a hub setting, and there is no login.
6. **Review & test**, in this order (each step unlocks the next):
   - **Validate all**: dry-runs and checks in lab
   - **Test run in lab**: runs it for real in lab after a confirmation that lists what it will change, then shows a live log and per-step times
   - **Generate**: builds the script, YAML, README and requirements.txt, smoke-runs them offline and lists the run commands per environment (generate without a lab test only where a test run is impossible, or when you skip it with a reason for custom code the hub cannot run; the bundle and its README are then marked "not tested in lab")
   - then **Download ZIP** or **Save version** (the lab test run of exactly that version is saved with it)

Logging and auditing are not options any more. Every script logs the team way, and every SQL it runs is captured with
the team's `sql_logger` module. Both are hub settings in `backend/app/settings.py`.

The top-right corner of every page shows **`ADC: <email>`**: the Google identity the hub uses for Gemini, dry runs,
GCS and Secret Manager checks and lab test runs, and the email sender.

## What gets generated

```
dl_script/
├── dl_script.py                    the team template: header, imports, module paths, CONFIG + logger, step functions, main(args), argparse
├── dl_script.yaml                  gcp_projects: lab / value [/ qa]: projects, data_sets, step blocks (ftp, gcs_config, file_copy), email
├── dl_script_table_schema.yaml     only with a file ingestion: table_definition (table_name, table_schema)
├── requirements.txt
└── README.md                       run commands, environments, dependency and target tables, every step in detail, SQL log, email, secrets
```

The process definition (`spec.json`) is stored with every saved version and has its own download button; re-import it
with **Import spec.json / ZIP** on the home page.

Inside the script, `main(args)` reads the YAML block of the environment it was given and calls your steps in order:

```python
def main(args):

    logger.info(f'Function {main.__name__} execution begin')
    logger.info(f'Parms-> Environment: {args.environment}')
    ...
    capture = sql_logger.CaptureSQL(project_id, credentials, CONFIG["SCRIPT_NAME"], environment)

    # Get the context from config file.
    config_data = config.read_configuration(environment, config_file)
    write_project = config_data.get('write_project')
    data_sets = config_data.get('data_sets', {})

    try:
        # 1. Rebuild prod_script_test from the latest online quote conversion predictions
        build_prod_script_test(write_project, data_sets)

        # 2. Extract the active members table to the FTP bucket as a dated .txt file
        attachments.append(extract_data_file_csv(config_data))

        #email execution
        email(config_data, environment, "SUCCESS", attachments=attachments)
    except Exception:
        ...  # the failure email, then the error is raised again (non-zero exit code)
    finally:
        logger.info(f"Function {main.__name__} execution end:  {execution_time} seconds.")
```

The SQL lives in the step function as an f-string:
- Projects and datasets are plain locals read from the YAML (`` `{write_project}.{mart_dataset}.t` ``).
- Dates and other values are BigQuery query parameters (`@report_date`): relative to today, or a value per environment in the YAML.
- Literal braces in your SQL (for example regex `{3}` or JSON) are escaped automatically, and the round trip is verified.

## Test it in lab

**Test run in lab** (Review & test) runs `python ./<script>.py -cf ./<script>.yaml [-ts …] lab` from the hub. It runs:
- with the hub's credentials (your own when you run the hub locally);
- in a private folder holding a lab-only YAML whose email receivers are replaced by your address;
- next to the team modules: the real ones from `SF_MODULES_DIR`, otherwise the hub's copies.

The hub refuses to start a test run when a lab write location (write project, export, archive or copy destination) is
the same as prod's. The log streams into the page, each step's `execution end` time is shown, and saving the version
keeps the result.

## Local development

Start it with the launcher for your system. Python 3.10 or newer is the only prerequisite.

| | Start | Turn on Gemini |
|---|---|---|
| **macOS / Linux** | `./run_local.sh` | `SF_GCP_PROJECT=my-dev-project ./run_local.sh` |
| **Windows** | double-click `run_local.bat`, or run `run_local.bat` in a terminal (`py run_local.py` also works) | `set SF_GCP_PROJECT=my-dev-project` then `run_local.bat` (PowerShell: `$env:SF_GCP_PROJECT="my-dev-project"`) |

Then use http://localhost:8080 (API docs at `/api/docs`).

**Google credentials:** every Google call uses **Application Default Credentials (ADC)**. That covers Gemini, the
BigQuery dry-runs, the GCS and Secret Manager checks in *Validate all*, the bucket browser, lab test runs, and the
workspace bucket if you set one. There are no keys in the code. The header shows the identity as `ADC: <email>`; when it is amber, run
`gcloud auth application-default login`.
- On Cloud Run, ADC is the app's service account.
- On a laptop, ADC is whatever `gcloud auth application-default login` stored. That is *your own* account and its IAM
  roles, unless you add `--impersonate-service-account=datapipeline-hub-sa@<project>.iam.gserviceaccount.com`. With
  that flag, you test with exactly the service account's permissions. The group has Token Creator on the service
  account for this purpose.
- The BigQuery and GCS checks run in each process's lab `write_project`. Without ADC they fail with *"default
  credentials were not found"*.
- A lab test run of a **Copy files** step needs the Google Cloud CLI (`gcloud`) on the PATH.

Your identity *in the app* is separate: locally there is no IAP, so you are `SF_DEV_USER_EMAIL` (default
`developer@localhost`). Set it to your email so the processes you save show you as the owner. The settings keep the
`SF_` prefix from the app's earlier name.

Both launchers call `run_local.py`, which:
- uses `./.venv`, whatever venv is active. On first run it creates the venv and installs `backend/requirements.txt`, which takes a minute or two
- starts uvicorn from `backend/` with auto-reload on code and template changes only
- prints `✔ Datapipeline Automation Hub is ready → http://localhost:8080` and opens the browser (`--no-browser` to skip)
- keeps the terminal as the server log until you press **Ctrl+C**. No new prompt appears while it runs; that is normal.
  On Windows, cmd then asks *Terminate batch job (Y/N)?*: answer `Y`
- if the hub is already running on that port, says so and opens it instead of starting a second copy
- `--port 8081` (or `PORT=8081`) uses another port, and any other options go to uvicorn (e.g. `--log-level debug`)
- keeps the workspace in `output/workspace`
- uses the shared bucket instead if you set `SF_WORKSPACE_BUCKET=my-bucket`
- runs the server in UTF-8 mode (`PYTHONUTF8=1`), so non-ASCII text in SQL, schemas and generated code is safe on Windows

`.gitattributes` keeps `.sh` files on LF and `.bat` files on CRLF line endings. A `.sh` checked out with Windows line
endings fails with `bash\r: No such file or directory`.

Running uvicorn by hand works too, but it must be **from `backend/`** with the dependencies installed:

```bash
cd backend && ../.venv/bin/uvicorn app.main:create_app --factory --reload --reload-dir app
```

```bash
cd backend
../.venv/bin/python -m app.cli generate ../samples/sales_daily.json -o ../output --zip   # generate without the UI
../.venv/bin/python -m pytest -q                                                        # all tests (~185, ~25 s), no GCP needed
UPDATE_GOLDEN=1 ../.venv/bin/python -m pytest -q tests/test_renderer.py -k golden      # accept intended template changes
```

On Windows, use `..\.venv\Scripts\python.exe` in place of `../.venv/bin/python`, and `python -m uvicorn` in place of
`uvicorn`. Set `PYTHONUTF8=1` first, as the launcher does.

## How correctness is protected

| Guard | Where |
|---|---|
| Gemini output is schema-validated (`response_schema`) and treated as a *proposal* | `services/gemini.py` |
| Equivalence check: placeholders substituted back, the parsed SQL (sqlglot AST) must match the original, for Gemini, rule-based and automatic project rewrites alike | `services/sql_analyzer.py` |
| Embedded SQL is evaluated statically (no `eval`) and must equal the approved SQL exactly | `services/filters.py`, `renderer.py` |
| The generated YAML (and table schema YAML) is loaded back and compared with the spec | `renderer.py` |
| black + `ast.parse` + `ruff` (E9, F) on every generated Python file | `renderer.py` |
| **Offline smoke run**: the script runs `lab -cf …` end to end against fake Google clients with the team modules: imports, YAML keys, argparse, call order, pandas typing of the ingestion, SQL log calls, email | `services/smoke_run.py`, `app/sandbox/` |
| **Lab test run**: the real thing, in lab, on request; refused when lab would write where prod writes | `services/test_runner.py` |
| Prod is never touched: every check resolves names with the lab values (tested with fakes that record every call) | `services/validators.py` |
| Values used in SQL are restricted to `[A-Za-z0-9_.:-]`; YAML values may not contain quotes, backslashes or line breaks | `models/spec.py` |
| Secret values never reach the spec, the YAML, the logs or the hub: only locations and metadata | `models/spec.py`, `validators.py` |

## Deployment

See [deploy/README_DEPLOY.md](deploy/README_DEPLOY.md). It covers:
- Cloud Run behind IAP
- running as a dedicated service account (the hub's ADC identity)
- access for the Google group that may use that service account
- a versioned GCS workspace
- Secret Manager Viewer for the secret checks
