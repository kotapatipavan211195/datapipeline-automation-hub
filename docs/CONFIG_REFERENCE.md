# Config reference: `<script>.yaml` and `<script>_table_schema.yaml`

The generated YAML follows the team's layout: one block per environment under `gcp_projects`. The script picks the
block with its first argument and reads it with `read_config_yaml.read_configuration(environment, -cf file)`:

```
py ./dl_script.py -cf ./dl_script.yaml lab        # gcp_projects.lab
py ./dl_script.py -cf ./dl_script.yaml value      # gcp_projects.value
```

The values come from the hub: each variable has a lab, a value and (optionally) a qa value. You can edit the YAML by
hand, for example a bucket that changed. To change *what* the script does, change the process in the hub and
regenerate, so the YAML and the code stay in step.

```yaml
# Configuration for GCP projects
gcp_projects:
  lab:
    # Configuration for the 'lab' (dev) project
    read_project: value-confidential  # project the SQL reads from
    write_project: demo-analytics-dev  # project the process writes to
    gsm_project: demo-analytics-dev    # only when the process reads secrets
    cutoff_date: '2026-01-14'         # parameters whose value comes from the YAML
    data_sets:
      strategic_dataset: lab_strategic
    ftp:                              # one block per export / copy / ingestion step (its "YAML block" name)
      bucket: demo_file_test_bucket
      path: outbound/test
      file_name: ms_acq_dm_active_members_
    email:
      sender: pipeline.developer@example.com
      receiver:
        - pipeline.developer@example.com
      attachment: []
  value:
    # Configuration for the 'value' (prod) project
    ...
```

## Keys of every environment block

| Key | When | Meaning |
|---|---|---|
| `read_project` | always | project the SQL reads from (`${read_project}` in SQL) |
| `write_project` | always | project the process writes to (`${write_project}`); also the project of ingestion tables |
| `gsm_project` | secrets | project that holds the process's Google Secret Manager secrets |
| `<project variable>` | used in SQL | any further project, e.g. `reference_project` |
| `<parameter>` | a parameter reads from the YAML | e.g. `cutoff_date: '2026-01-14'`; quoted so YAML keeps it text |
| `data_sets` | datasets used in SQL | `{<name>_dataset: <dataset>}`; the step functions read them with `data_sets.get(...)` |
| `<other variable>` | used in SQL | buckets or other values the SQL itself uses |

Variables that only appear in step settings (a bucket, a folder) are not listed on their own: their value is written
straight into the step's block.

## Export steps (`ftp` by default)

Read by the step function (the team's `extract_data_file_csv` pattern). The script adds today's date and the extension:
`gs://<bucket>/<path>/<file_name><date suffix>[-*].<ext>`.

| Key | Meaning |
|---|---|
| `bucket` | bucket name (no `gs://`) |
| `path` | folder inside the bucket (`''` = the bucket root) |
| `file_name` | file name before the date suffix, e.g. `ms_acq_dm_active_members_` |

The date-suffix format, the file format, delimiter, header and compression are in the code (the hub's step settings).
A second export step uses its own block (e.g. `ftp_contacts`). An Excel export (`.xlsx`) uses the same keys; its sheet
name and header row are in the code too.

## File ingestion (`gcs_config`, `write_disposition`, `datasets`)

The team's file-ingestion pattern: every file in the folder whose name **contains** `filename` (and ends with the
format's extension: `.txt` or `.dat` for fixed width, `.xlsx` for Excel) is read with pandas, one file after the other,
oldest upload first. Each file is loaded into the staging table, appended to the history table, MERGEd (or inserted)
into the target table and moved to the archive folder. Without a target table the raw rows stay in the staging table:
the run's first file empties it and every file of the run is added (it is emptied when no file arrives).

```yaml
    write_disposition: WRITE_APPEND
    gcs_config:
      gcs_read_bucket: demo_file_test_bucket
      gcs_read_bucket_path: engagement_ingestion
      gcs_archive_bucket: demo_file_test_bucket
      gcs_archive_bucket_path: engagement_ingestion/Archive
      filename: 'Vendor Daily Engagement File_'
      header: '0'
      skiprows: 'None'
      separator: ','
    datasets:
      temporary: clubwide     # staging table's dataset
      target: clubwide        # history and target tables' dataset
```

| Key | Meaning |
|---|---|
| `write_disposition` | how each file is added to the history table (`WRITE_APPEND` / `WRITE_TRUNCATE`) |
| `gcs_config.gcs_read_bucket` / `gcs_read_bucket_path` | where the files arrive |
| `gcs_config.gcs_archive_bucket` / `gcs_archive_bucket_path` | where processed files are moved (files there are never re-loaded) |
| `gcs_config.filename` | text the file name must contain |
| `gcs_config.sheet_name` | Excel only: the sheet to read (`''` = the first sheet) |
| `gcs_config.header` | pandas `header`: `'0'` = the first row is a header, `'None'` = no header (delimited and fixed-width text, Excel) |
| `gcs_config.skiprows` | pandas `skiprows`: `'None'`, a number of lines, or a list such as `'[0, 2]'` |
| `gcs_config.separator` | delimiter of CSV/TXT files |
| `datasets.temporary` / `datasets.target` | datasets of the staging table and of the history/target tables (`target` only when the step has a history or a target table) |

The staging table is `table_name` from the table schema YAML, the history table `<table_name>_history`, and the
target table the name set in the hub (in the code). The first run creates the target with `CREATE TABLE IF NOT EXISTS …
LIKE` the staging table.

## Copy steps (`file_copy` by default)

```yaml
    file_copy:
      sources:
        - gs://demo_vendor_drop_test/outbound/Daily Engagement File_*.csv
      destination: gs://demo_file_test_bucket/engagement_ingestion/
```

The step runs `gcloud storage cp|mv|rsync [flags] <sources…> <destination>`. Paths are gs:// paths or local paths on
the machine that runs the script. `{date:%Y%m%d}` (any strftime format) is filled in with today's date when the
script runs.

## Custom process steps (`custom_config` by default)

```yaml
    db2_config:
      host: db2-lab.example.com
      port: '50000'
      database: MEMBERS
```

One key per setting of the step, with the value for that environment (`{variables}` are resolved per environment). The
step's code reads them from the dict of the same name (`db2_config['host']`). Passwords and API keys are not settings:
the code reads them from `SECRETS`, which `main()` fills from Google Secret Manager.

## `email`

| Key | Meaning |
|---|---|
| `sender` | the From address: the hub's ADC identity at generation (edit if needed) |
| `receiver` | list of recipients for this environment |
| `attachment` | extra files to attach (gs:// or local paths; `{date:%Y%m%d}` allowed; a `*` in a gs:// file name attaches every match) |

Files written by exports marked *Attach to the success email* are attached on top of `attachment`. The relay (host,
port) is in the code, from the hub's settings.

# `<script>_table_schema.yaml` (file ingestion only)

Passed with `-ts` and read with `read_config_yaml.read_table_definition(file)`:

```yaml
table_definition:
  table_name: vendor_engagement
  table_schema:
    Created: DATETIME
    Drip_Id: STRING
    Client_Id: STRING
    Engaged: INTEGER
```

| Key | Meaning |
|---|---|
| `table_name` | the staging table (in `datasets.temporary`); the history table is `<table_name>_history` |
| `table_schema` | column → BigQuery type, in file order. pandas reads each column with this type: `STRING`, `INTEGER` (nullable), `FLOAT`, `BOOLEAN`, `NUMERIC` (exact decimals); `DATE`, `DATETIME` and `TIMESTAMP` are parsed as dates (`%Y-%m-%d %H:%M:%S`, invalid values become empty) |
| `column_widths` | fixed-width files only: column → number of characters, in file order (the comments show each column's positions and the record length) |

With *Add batch columns* on, every row also gets `file_batch_id`, `inserted_ts`, `updated_ts` and `file_name`.

A fixed-width file (format *TXT — fixed width*) adds the lengths:

```yaml
table_definition:
  table_name: member_extract
  table_schema:
    member_id: STRING
    member_name: STRING
    join_date: DATE
  column_widths:  # fixed-width file: characters per column, in file order (record length 50)
    member_id: 10  # positions 1-10
    member_name: 30  # positions 11-40
    join_date: 10  # positions 41-50
```

# Hub settings that shape every generated script

Set in `backend/app/settings.py` or as environment variables (`SF_…`, or a `.env` file next to the backend). The wizard
does not show them. The full list, with the deployment and test-run settings, is in `deploy/README_DEPLOY.md`.

| Setting | Default | Meaning |
|---|---|---|
| `SF_SQL_LOGGER_ENABLED` | `true` | **Audit:** every SQL a script runs is logged by the team's `sql_logger` module (`CaptureSQL`) with its status, BigQuery job id, environment and Composer `DAG_ID` / `TASK_ID`; `false` = scripts do not use it |
| `SF_SQL_LOGGER_TABLE` | `pipeline_audit.pipeline_sql_log` | **Audit table** (`dataset.table`, in the credentials' project). The module itself names the table: keep this the same so *Validate all* checks the right one |
| `SF_SMTP_HOST` / `SF_SMTP_PORT` / `SF_SMTP_STARTTLS` | `smtp.example.com` / `25` / `true` | **Mail relay** of the notification email (internal, no login); written into the script |
| `SF_EMAIL_SIGNATURE` | "Regards, The Data Engineering Team …" | closes every notification email |
| `SF_MODULE_PATHS` | `["/data/py_scripts","/data/data_eng/py_scripts"]` | `sys.path` entries for the team modules |
| `SF_ENVIRONMENTS` | `["lab","value","qa"]` | the script's `environment` argument choices |
| `SF_BQ_LOCATION` | `US` | `location=` of extract jobs |
| `SF_DEFAULT_PROJECT_VALUE` | `value-gcp` | `read_project` / `write_project` prefilled in value (prod) |

# Hub settings added in 3.1

| Setting | Default | Meaning |
|---|---|---|
| `SF_MAX_SQL_FILE_MB` | `5` | largest `.sql` / `.txt` file a SQL step accepts as an attachment |
| `SF_ENHANCE_MAX_SCRIPT_KB` | `1024` | largest script *Enhance a script* accepts |
| `SF_COPY_TOOL_BUCKETS` | — | lab buckets (or patterns) the deployed *Copy files* tool may use; see `deploy/README_DEPLOY.md` |
| `SF_COPY_TOOL_SCOPE` | `auto` | `auto`: local hub = any bucket your credentials reach, deployed hub = only `SF_COPY_TOOL_BUCKETS` |

Process definitions (`spec.json`) gained `process_type` (`pipeline` or `copy`), `direction` on copy steps (`bucket_to_bucket`, `local_to_bucket`, `bucket_to_local`; empty for older steps), `sql_file` on SQL steps (the name of an
attached file, display only), `width` on columns and the `FIXED` ingestion format. They all have defaults, so older
definitions load unchanged.

# Hub settings added in 3.2

| Setting | Default | Meaning |
|---|---|---|
| `SF_DEFAULT_PROJECT_VALUE` | `value-gcp` | `read_project` / `write_project` prefilled in value (prod) for new processes (and filled in older ones where empty); pasted SQL naming it maps to those built-ins |
| `SF_DEFAULT_PROJECT_LAB` / `SF_DEFAULT_PROJECT_QA` | — | the same for lab and qa (none by default) |
| `SF_FILE_SEARCH_ENABLED` | `true` | the home page's *Find files* |
| `SF_FILE_SEARCH_MAX_RESULTS` | `1000` | matches shown; the search stops there |
| `SF_FILE_SEARCH_MAX_SCAN` | `500000` | files looked at per bucket (or folder) before the search stops |

*Find files* searches the same buckets as *Copy files* (`SF_COPY_TOOL_SCOPE` / `SF_COPY_TOOL_BUCKETS`).

Process definitions gained `sheet_name` on ingestion and export steps and the `XLSX` format for both (openpyxl is a
new requirement of the hub and of scripts that use Excel). Older definitions load unchanged and keep their spec hash.

# Changes in 3.3

No new hub settings. *Let BigQuery guess the types* uses the hub's identity and `SF_BQ_LOCATION`; it needs
`bigquery.tables.create` / `delete` and `bigquery.jobs.create` on the lab dataset it loads into (the staging dataset by
default).

Process definitions gained `target_enabled` on ingestion steps (false = no target table; new steps start without
one, older definitions keep theirs), `file_summary` on the email (list the files loaded and exported, with their
rows and destination, in the email body; on for new processes) and the `custom` step type: `config_key`, `request` (the description Gemini writes from),
`settings` (`[{key, value, description}]`), `imports`, `requirements`, `code` (the statements inside the step's `try:`)
and `generated_by`. Older definitions load unchanged and keep their spec hash.

# Changes in 3.4

No new hub settings and no change to process definitions (spec hashes stay the same).

- Generated scripts read secrets with a `get_gsm_secret()` in the team's style (begin/end timing, `ValueError` on an
  empty payload, `RuntimeError(...) from e`) that returns the **whole payload**; custom steps read the payload keys
  (e.g. `secret_list_key: api_credentials`, `secret_value_key: api`) from their settings in the YAML.
- *Enhance a script* attaches the author header block (checked on the page; both the converted process and the
  rewritten script carry it) and replaces service-account access with ADC; the rewrite has a new guard,
  *ADC credentials*. `POST /api/enhance/convert` and `/rewrite` take an optional `header`
  (`authors`, `reviewers`, `jira`, `created_on`, `description`, `updates`, `update_pid`, `update_note`).
- Author(s), Reviewer(s) and Tags on the Basics page are comma-separated text boxes.
