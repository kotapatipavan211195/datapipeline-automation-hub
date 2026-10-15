"""ProcessSpec — the single source of truth the wizard edits, the workspace stores and the renderer turns into files.

The order of ``steps`` is the order ``main()`` calls the generated step functions.

Spec v3 follows the team's production standard: one YAML block per environment (``lab`` = dev, ``value`` = prod,
optionally ``qa``) under ``gcp_projects``. Every variable has one value per environment; the renderer writes the
resolved values into each block. v1/v2 specs are upgraded on load (see ``migrate.py``).
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import re
import textwrap
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .rules import (
    BUCKET_RE,
    CONFIG_KEY_RE,
    EMAIL_SUBJECT_TOKENS,
    GENERATED_SCRIPT_NAMES,
    IDENTIFIER_RE,
    PROCESS_NAME_RE,
    RESERVED_FUNCTION_NAMES,
    SAFE_VALUE_RE,
    SECRET_ID_RE,
    UNSAFE_PATH_CHARS,
    fill_variables,
    function_name_problem,
    parse_receivers,
    script_name_problem,
    sql_var_tokens,
    template_fields,
    template_problems,
)
from .schema import Column, check_unique_names

SPEC_VERSION = 3  # 3: environments (lab/value/qa), prod-standard script; run_date, logging, audit, SMTP settings removed

ENVIRONMENTS = ("lab", "value", "qa")
ENV_LABELS = {"lab": "lab (dev)", "value": "value (prod)", "qa": "qa"}
BUILTIN_PROJECTS = ("read_project", "write_project")
GSM_PROJECT = "gsm_project"
BUILTIN_VARIABLES = (*BUILTIN_PROJECTS, GSM_PROJECT)
VARIABLE_GROUPS = ("project", "dataset", "bucket", "other")
# Keys of every environment block the script reads itself (config_keys, variables and params must not reuse them).
RESERVED_CONFIG_KEYS = frozenset({*BUILTIN_VARIABLES, "data_sets", "datasets", "write_disposition", "email"})

BqParamType = Literal["DATE", "TIMESTAMP", "DATETIME", "STRING", "INT64", "FLOAT64", "NUMERIC", "BOOL"]
ParamSource = Literal[
    "today",
    "today_offset",
    "month_start",
    "month_end",
    "prev_month_start",
    "prev_month_end",
    "year_start",
    "config",
    "process_name",
]
DATE_SOURCES = {"today", "today_offset", "month_start", "month_end", "prev_month_start", "prev_month_end", "year_start"}

EXPORT_COMPRESSION = {
    "CSV": {"NONE", "GZIP"},
    "TXT": {"NONE", "GZIP"},  # delimited text written as .txt (BigQuery CSV under the hood)
    "NEWLINE_DELIMITED_JSON": {"NONE", "GZIP"},
    "AVRO": {"NONE", "DEFLATE", "SNAPPY"},
    "PARQUET": {"NONE", "GZIP", "SNAPPY", "ZSTD"},
    "XLSX": {"NONE"},  # written with pandas + openpyxl (BigQuery cannot extract Excel); .xlsx is already a ZIP
}

DELIMITED_FORMATS = ("CSV", "TXT")
FORMAT_LABELS = {"CSV": "CSV", "TXT": "TXT (delimited text)", "FIXED": "TXT (fixed width)", "NEWLINE_DELIMITED_JSON": "JSON lines",
                 "PARQUET": "Parquet", "AVRO": "Avro", "ORC": "ORC", "XLSX": "Excel (.xlsx)"}
FILE_EXTENSIONS = {"CSV": ".csv", "TXT": ".txt", "FIXED": ".txt", "NEWLINE_DELIMITED_JSON": ".json", "PARQUET": ".parquet",
                   "AVRO": ".avro", "ORC": ".orc", "XLSX": ".xlsx"}
# File-name endings an ingestion step picks up, per format
INGEST_EXTENSIONS = {"CSV": (".csv",), "TXT": (".txt",), "FIXED": (".txt", ".dat"), "NEWLINE_DELIMITED_JSON": (".json", ".jsonl"),
                     "PARQUET": (".parquet",), "XLSX": (".xlsx",)}
EXCEL_MAX_ROWS = 1_048_576  # rows in one Excel sheet, the header row included
SHEET_NAME_BAD = set("[]:*?/\\")
TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
PANDAS_ROWS_RE = re.compile(r"^(None|\d+|\[\s*\d+(\s*,\s*\d+)*\s*\])$")


def is_delimited(file_format: str) -> bool:
    """CSV and TXT are both delimited text; BigQuery loads/exports them with its CSV format."""
    return (file_format or "").upper() in DELIMITED_FORMATS


def has_text_rows(file_format: str) -> bool:
    """Delimited and fixed-width text are read line by line (as text)."""
    return is_delimited(file_format) or (file_format or "").upper() == "FIXED"


def has_header_rows(file_format: str) -> bool:
    """Formats read row by row, which take the pandas header and skiprows settings: text and Excel."""
    return has_text_rows(file_format) or (file_format or "").upper() == "XLSX"


def _sheet_name_problem(value: str) -> str | None:
    if len(value) > 31 or any(ch in SHEET_NAME_BAD for ch in value) or value.startswith("'") or value.endswith("'"):
        return f"sheet name {value!r} must be at most 31 characters, without [ ] : * ? / \\ or quotes at either end"
    return None


def bq_format(file_format: str) -> str:
    """The BigQuery source/destination format behind a file format (TXT → CSV)."""
    return "CSV" if is_delimited(file_format) else file_format


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _path_value_problem(value: str) -> str | None:
    bad = [ch for ch in UNSAFE_PATH_CHARS if ch in (value or "")]
    if bad:
        return f"{value!r} contains characters that are not allowed (quotes, backslash, backtick, line breaks)"
    return None


# ---------------------------------------------------------------- shared pieces
class EnvValues(_Model):
    """One value per environment: lab (dev), value (prod) and qa."""

    lab: str = ""
    value: str = ""
    qa: str = ""

    @field_validator("lab", "value", "qa", mode="before")
    @classmethod
    def _none_is_blank(cls, v: Any) -> Any:
        return "" if v is None else v

    def get(self, env: str) -> str:
        return getattr(self, env)


class Variable(_Model):
    """A config value (project, dataset, bucket, …) referenced as ${name} in SQL and {name} in paths,
    with one value per environment."""

    name: str
    group: Literal["project", "dataset", "bucket", "other"] = "other"
    values: EnvValues = Field(default_factory=EnvValues)
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not IDENTIFIER_RE.match(v) or v != v.lower():
            raise ValueError(f"variable name {v!r} must be lower_snake_case")
        if v == "date":
            raise ValueError("'date' is the built-in {date} token and cannot be a variable name")
        if v in GENERATED_SCRIPT_NAMES and v not in BUILTIN_VARIABLES:
            raise ValueError(f"'{v}' clashes with a name the generated script already uses")
        return v

    @field_validator("values")
    @classmethod
    def _values(cls, v: EnvValues) -> EnvValues:
        for env in ENVIRONMENTS:
            value = v.get(env)
            problem = _path_value_problem(value)
            if problem or "{" in value or "}" in value:
                raise ValueError(problem or f"{value!r}: braces are not allowed in variable values")
        return v


class Parameter(_Model):
    """A BigQuery query parameter (@name). Values are never formatted into SQL text."""

    name: str
    bq_type: BqParamType = "DATE"
    source: ParamSource = "today"
    offset_days: int = 0
    default: str | None = None  # source "config": the value written into every environment block of the YAML
    original_literal: str | None = None
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not IDENTIFIER_RE.match(v):
            raise ValueError(f"parameter name {v!r} must be a valid identifier")
        return v

    @model_validator(mode="after")
    def _check(self) -> Parameter:
        if self.source == "config":
            if self.default is None or self.default == "":
                raise ValueError(f"parameter @{self.name} reads from the config, so it needs a value")
            if self.name != self.name.lower() or self.name in RESERVED_CONFIG_KEYS or self.name in GENERATED_SCRIPT_NAMES:
                raise ValueError(f"parameter @{self.name} reads from the config: use a lower_snake_case name that is "
                                 "not one of the script's own names")
            problem = _path_value_problem(self.default)
            if problem:
                raise ValueError(f"parameter @{self.name}: {problem}")
        if self.source in DATE_SOURCES and self.bq_type not in ("DATE", "TIMESTAMP", "DATETIME", "STRING"):
            raise ValueError(f"parameter @{self.name} is date-derived but typed {self.bq_type}")
        if self.source == "process_name" and self.bq_type != "STRING":
            raise ValueError(f"parameter @{self.name} ({self.source}) must be STRING")
        return self


class StepBase(_Model):
    function_name: str
    description: str = ""
    enabled: bool = True
    on_failure: Literal["stop", "continue"] = "stop"

    @field_validator("function_name")
    @classmethod
    def _fn(cls, v: str) -> str:
        problem = function_name_problem(v)
        if problem:
            raise ValueError(problem)
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        return " ".join((v or "").split())[:300]

    def config_section(self) -> str | None:
        """Key of this step's block inside each environment of the YAML (None = the step has no block)."""
        return getattr(self, "config_key", None)

    def template_values(self) -> list[tuple[str, str, bool]]:
        """(field, value, allow_date) of every {variable} template this step uses."""
        return []


def _config_key(v: str) -> str:
    if not CONFIG_KEY_RE.match(v or ""):
        raise ValueError(f"config key {v!r} must be lower_snake_case (it becomes a key in the YAML)")
    if v in RESERVED_CONFIG_KEYS:
        raise ValueError(f"config key {v!r} is reserved")
    return v


def _table_template_problem(value: str, what: str) -> str | None:
    """A table reference: project.dataset.table or dataset.table, parts may be {variables}."""
    if not value:
        return f"choose the {what}"
    problem = _path_value_problem(value)
    if problem:
        return problem
    plain = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "x", value)
    if not re.fullmatch(r"[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+){1,2}", plain):
        return f"{what} must look like project.dataset.table or dataset.table (parts may be {{variables}}), got {value!r}"
    return None


def _bucket_template_problem(value: str, what: str) -> str | None:
    if not value:
        return f"enter the {what}"
    if value.startswith("gs://"):
        return f"{what}: enter only the bucket name, without gs://"
    if "/" in value:
        return f"{what}: enter only the bucket name; put folders in the path field"
    return _path_value_problem(value)


# ---------------------------------------------------------------- ingest (team's file-ingestion pattern)
class IngestStep(StepBase):
    """Files in GCS → pandas → staging table (+ history) → MERGE/INSERT into the target table → archive.

    Mirrors the team's file-ingestion scripts: a config block (``gcs_config``), a table schema YAML
    (``-ts``) and helper functions process_file / load_to_bq / merge_to_main_table / archive_data_file.
    """

    type: Literal["ingest"] = "ingest"
    config_key: str = "gcs_config"
    read_bucket: str
    read_path: str = ""  # folder inside the bucket
    filename: str  # files whose name contains this text are loaded (e.g. 'Daily Engagement File_')
    file_format: Literal["CSV", "TXT", "FIXED", "NEWLINE_DELIMITED_JSON", "PARQUET", "XLSX"] = "CSV"  # FIXED: column widths
    sheet_name: str = ""  # XLSX: the sheet to read ('' = the first one)
    header: str = "0"  # pandas header row: '0' = the first row is a header, 'None' = no header row
    skiprows: str = "None"  # pandas skiprows: 'None', '1' or '[0, 2]'
    separator: str = ","
    columns: list[Column] = Field(default_factory=list)  # → <script>_table_schema.yaml
    table_name: str  # staging table (in the staging dataset); history = <table_name>_history
    target_table: str = ""  # final table in the target dataset ('' = same name as table_name)
    # False: no target table; the raw rows stay in the staging table (this run's files) for the next steps
    target_enabled: bool = True
    staging_dataset: str  # dataset or {variable}
    target_dataset: str  # dataset or {variable}
    history: bool = True  # also append every file to <target_dataset>.<table_name>_history
    load_mode: Literal["merge", "append", "truncate"] = "merge"
    merge_keys: list[str] = Field(default_factory=list)
    merge_order_by: str | None = None  # newest row per key wins; existing rows are only updated by newer ones
    merge_filter: str = ""  # optional SQL condition on staged rows, e.g. upper(Campaign) NOT LIKE '%UAT%'
    write_disposition: Literal["WRITE_APPEND", "WRITE_TRUNCATE"] = "WRITE_APPEND"  # history load
    add_audit_columns: bool = True  # file_batch_id, inserted_ts, updated_ts, file_name
    archive: bool = True
    archive_bucket: str = ""  # '' = the read bucket
    archive_path: str = ""
    on_no_files: Literal["email", "warn", "fail"] = "warn"

    @field_validator("config_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _config_key(v)

    @field_validator("separator", mode="before")
    @classmethod
    def _sep(cls, v: Any) -> Any:
        return "\t" if v in ("\\t", "tab", "TAB") else v

    @model_validator(mode="after")
    def _check(self) -> IngestStep:
        fn = self.function_name
        problems: list[str] = []
        if not self.columns:
            problems.append("add the file's columns (table schema)")
        nested = [c.name for c in self.columns if c.is_nested()]
        if nested:
            problems.append(f"nested/repeated columns cannot be loaded from files: {', '.join(nested)}")
        dupes = check_unique_names(self.columns)
        if dupes:
            problems.append(f"duplicate column names: {', '.join(dupes)}")
        names = {c.name.lower() for c in self.columns}
        if is_delimited(self.file_format) and len(self.separator) != 1:
            problems.append("the separator must be a single character (use \\t for tab)")
        if self.file_format == "FIXED":
            no_width = [c.name for c in self.columns if not c.width]
            if no_width:
                problems.append(f"a fixed-width file needs the length of every column: {', '.join(no_width[:10])}"
                                + (" …" if len(no_width) > 10 else ""))
        if self.file_format == "XLSX" and self.sheet_name:
            problem = _sheet_name_problem(self.sheet_name)
            if problem:
                problems.append(problem)
        if has_header_rows(self.file_format):
            for label, value in (("header", self.header), ("skiprows", self.skiprows)):
                if not PANDAS_ROWS_RE.match(value or ""):
                    problems.append(f"{label} must be None, a row number or a list such as [0, 2] (got {value!r})")
        tables = [("table name", self.table_name)] + ([("target table", self.target())] if self.target_enabled else [])
        for label, value in tables:
            if not TABLE_NAME_RE.match(value or ""):
                problems.append(f"{label} {value!r} must be letters, digits and underscores")
        datasets = [("staging dataset", self.staging_dataset)]
        if self.uses_target_dataset():
            datasets.append(("target dataset", self.target_dataset))
        for label, value in datasets:
            plain = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "x", value or "")
            if not re.fullmatch(r"[A-Za-z0-9_]+", plain):
                problems.append(f"{label} must be a dataset name or a {{variable}} (got {value!r})")
        for label, value in (("read bucket", self.read_bucket), ("archive bucket", self.archive_bucket)):
            if value or label == "read bucket":
                problem = _bucket_template_problem(value, label)
                if problem:
                    problems.append(problem)
        for label, value in (("read path", self.read_path), ("file name", self.filename), ("archive path", self.archive_path)):
            problem = _path_value_problem(value)
            if problem:
                problems.append(f"{label}: {problem}")
        if not self.filename:
            problems.append("enter the file name (or the start of it) to look for")
        if self.read_path.startswith("/") or self.archive_path.startswith("/"):
            problems.append("folders must not start with '/'")
        if self.archive and not self.archive_path:
            problems.append("archiving is on, so enter the archive folder")
        if self.target_enabled and self.load_mode == "merge":
            if not self.merge_keys:
                problems.append("a merge needs at least one key column")
            missing = [k for k in self.merge_keys if k.lower() not in names]
            if self.merge_order_by and self.merge_order_by.lower() not in names:
                missing.append(self.merge_order_by)
            if missing and self.columns:
                problems.append(f"merge column(s) not in the schema: {', '.join(missing)}")
        if '"""' in self.merge_filter or "\\" in self.merge_filter:
            problems.append('the merge filter must not contain """ or backslashes')
        if problems:
            raise ValueError(f"{fn}: " + "; ".join(problems))
        return self

    def target(self) -> str:
        return self.target_table or self.table_name

    def uses_target_dataset(self) -> bool:
        """The target dataset holds the history table and the target table."""
        return self.target_enabled or self.history

    def record_length(self) -> int:
        """Fixed width: characters per line (the sum of the column widths)."""
        return sum(c.width or 0 for c in self.columns)

    def template_values(self) -> list[tuple[str, str, bool]]:
        out = [("read_bucket", self.read_bucket, False), ("read_path", self.read_path, False),
               ("filename", self.filename, False), ("staging_dataset", self.staging_dataset, False)]
        if self.uses_target_dataset():
            out.append(("target_dataset", self.target_dataset, False))
        if self.archive:
            out += [("archive_bucket", self.archive_bucket, False), ("archive_path", self.archive_path, False)]
        return out


# ---------------------------------------------------------------- SQL
class SqlStep(StepBase):
    type: Literal["sql"] = "sql"
    original_sql: str = ""
    sql_file: str = ""  # name of the .sql/.txt file the SQL was attached from (shown in the wizard only)
    sql: str
    parameters: list[Parameter] = Field(default_factory=list)
    statement_kind: Literal["SELECT", "DML", "DDL", "MERGE", "SCRIPT", "OTHER"] = "OTHER"
    destination_table: str | None = None  # SELECT only: {write_project}.{mart_dataset}.table
    write_mode: Literal["truncate", "append", "empty"] = "truncate"
    sources: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)

    @field_validator("sql_file")
    @classmethod
    def _sql_file(cls, v: str) -> str:
        if v and (any(ch in v for ch in "/\\") or _path_value_problem(v)):
            raise ValueError(f"attached file name {v!r} must be a plain file name")
        return v

    @model_validator(mode="after")
    def _check(self) -> SqlStep:
        if not self.sql.strip():
            raise ValueError(f"{self.function_name}: SQL is empty")
        _check_params_unique(self.function_name, self.parameters)
        if self.destination_table:
            if self.statement_kind not in ("SELECT", "OTHER"):
                raise ValueError(
                    f"{self.function_name}: a destination table only applies to a SELECT "
                    f"(this SQL is {self.statement_kind} and writes its own target)"
                )
            problem = _table_template_problem(self.destination_table, "destination table")
            if problem:
                raise ValueError(f"{self.function_name}: {problem}")
        return self

    def template_values(self) -> list[tuple[str, str, bool]]:
        return [("destination_table", self.destination_table, False)] if self.destination_table else []


# ---------------------------------------------------------------- export (BigQuery → GCS, extract job)
class ExportStep(StepBase):
    type: Literal["export"] = "export"
    config_key: str = "ftp"
    source_kind: Literal["table", "query"] = "table"
    source_table: str | None = None  # {write_project}.{membership_dataset}.table
    source_sql: str | None = None
    parameters: list[Parameter] = Field(default_factory=list)
    bucket: str
    path: str = ""  # folder inside the bucket
    file_name: str  # file name without the date suffix and extension, e.g. ms_acq_dm_active_members_
    date_suffix: str = "%Y_%m_%d"  # strftime added after file_name ('' = none)
    sharded: bool = False  # tables over 1 GB must be exported to several files (name_*.csv)
    file_format: Literal["CSV", "TXT", "NEWLINE_DELIMITED_JSON", "AVRO", "PARQUET", "XLSX"] = "CSV"
    sheet_name: str = ""  # XLSX: the sheet's name ('' = Sheet1)
    compression: Literal["NONE", "GZIP", "DEFLATE", "SNAPPY", "ZSTD"] = "NONE"
    print_header: bool = True
    field_delimiter: str = ","
    attach_to_email: bool = False

    @field_validator("config_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _config_key(v)

    @model_validator(mode="after")
    def _check(self) -> ExportStep:
        fn = self.function_name
        problems: list[str] = []
        if self.source_kind == "table":
            problem = _table_template_problem(self.source_table or "", "table to export")
            if problem:
                problems.append(problem)
        if self.source_kind == "query":
            if not (self.source_sql or "").strip():
                problems.append("enter the SELECT query to export")
            if self.file_format != "XLSX":
                self.sharded = True  # EXPORT DATA always writes name_*.ext files
        if self.file_format == "XLSX":
            if self.sharded:
                problems.append("an Excel export is one file: turn 'Sharded files' off")
            problem = _sheet_name_problem(self.sheet_name) if self.sheet_name else None
            if problem:
                problems.append(problem)
        problem = _bucket_template_problem(self.bucket, "bucket")
        if problem:
            problems.append(problem)
        if not self.file_name:
            problems.append("enter the file name")
        for label, value in (("path", self.path), ("file name", self.file_name)):
            problem = _path_value_problem(value)
            if problem:
                problems.append(f"{label}: {problem}")
        if "*" in self.file_name or "/" in self.file_name:
            problems.append("the file name must not contain '*' or '/' (use 'Sharded files' and the folder field)")
        if self.path.startswith("/"):
            problems.append("the folder must not start with '/'")
        if self.date_suffix:
            if "/" in self.date_suffix or _path_value_problem(self.date_suffix):
                problems.append("the date suffix must not contain '/' or quotes")
            try:
                dt.date(2026, 1, 31).strftime(self.date_suffix)
            except ValueError as exc:
                problems.append(f"invalid date suffix: {exc}")
        if self.compression not in EXPORT_COMPRESSION[self.file_format]:
            allowed = ", ".join(sorted(EXPORT_COMPRESSION[self.file_format]))
            problems.append(f"{self.file_format} export supports compression: {allowed}")
        if self.field_delimiter in ("\\t", "tab"):
            self.field_delimiter = "\t"
        if len(self.field_delimiter) != 1:
            problems.append("field delimiter must be a single character")
        if problems:
            raise ValueError(f"{fn}: " + "; ".join(problems))
        _check_params_unique(fn, self.parameters)
        return self

    def template_values(self) -> list[tuple[str, str, bool]]:
        out = [("bucket", self.bucket, False), ("path", self.path, False), ("file_name", self.file_name, False)]
        if self.source_kind == "table" and self.source_table:
            out.append(("source_table", self.source_table, False))
        return out


# ---------------------------------------------------------------- copy files (gcloud storage)
COPY_DIRECTIONS = {"bucket_to_bucket": "Bucket → bucket", "local_to_bucket": "Local folder → bucket",
                   "bucket_to_local": "Bucket → local folder"}
LOCAL_PATH_RE = re.compile(r"^(/|[A-Za-z]:/|\{)")  # absolute: /data/exports/…, C:/… or a {variable} holding one


def copy_direction(sources: list[str], destination: str) -> str | None:
    """The direction a set of copy paths has (None while they are empty or mixed)."""
    sources = [s for s in sources if s]
    if not sources or not destination:
        return None
    from_bucket, to_bucket = all(s.startswith("gs://") for s in sources), destination.startswith("gs://")
    if not from_bucket and any(s.startswith("gs://") for s in sources):
        return None
    if from_bucket:
        return "bucket_to_bucket" if to_bucket else "bucket_to_local"
    return "local_to_bucket" if to_bucket else None


class CopyStep(StepBase):
    """Copy/move/sync files between buckets, or between a bucket and a local folder, with `gcloud storage`."""

    type: Literal["copy"] = "copy"
    config_key: str = "file_copy"
    operation: Literal["cp", "mv", "rsync"] = "cp"
    sources: list[str] = Field(default_factory=list)  # gs://bucket/path/*.csv or /local/folder/file.csv
    destination: str = ""
    # where the files go; "local" = the machine that runs the script. None: any mix (processes made before 3.1)
    direction: Literal["bucket_to_bucket", "local_to_bucket", "bucket_to_local"] | None = None
    recursive: bool = False
    no_clobber: bool = False  # skip files that already exist at the destination
    delete_unmatched: bool = False  # rsync only: delete destination files that are not in the source

    @field_validator("config_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _config_key(v)

    @field_validator("sources", mode="before")
    @classmethod
    def _sources(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [str(s).strip() for s in v if str(s or "").strip()]
        return v

    @model_validator(mode="after")
    def _check(self) -> CopyStep:
        fn = self.function_name
        problems: list[str] = []
        if not self.sources:
            problems.append("add at least one source")
        if not self.destination:
            problems.append("enter the destination")
        paths = [*self.sources, self.destination]
        for p in paths:
            problem = _path_value_problem(p)
            if problem:
                problems.append(problem)
            if p.startswith("gs://") and not p[5:].split("/")[0]:
                problems.append(f"{p!r}: gs:// paths need a bucket name")
            if p.startswith("~"):
                problems.append(f"{p!r}: write the full local path (~ is not expanded)")
        if paths and not any(p.startswith("gs://") for p in paths if p):
            problems.append("at least one side must be a gs:// path (for local-only copies use the operating system)")
        if self.direction:
            from_bucket, to_bucket = self.direction.startswith("bucket"), self.direction.endswith("bucket")
            for label, path, bucket in [*((f"source {i + 1}", s, from_bucket) for i, s in enumerate(self.sources)),
                                        ("destination", self.destination, to_bucket)]:
                if not path:
                    continue
                if bucket and not path.startswith("gs://"):
                    problems.append(f"{COPY_DIRECTIONS[self.direction]}: the {label} must be a gs://bucket/… path")
                elif not bucket and path.startswith("gs://"):
                    problems.append(f"{COPY_DIRECTIONS[self.direction]}: the {label} must be a local path on the machine "
                                    "that runs the script, not gs://")
                elif not bucket and not LOCAL_PATH_RE.match(path):
                    problems.append(f"the {label} {path!r} must be a full local path, e.g. /data/exports/")
        if self.operation == "rsync":
            if len(self.sources) > 1:
                problems.append("rsync takes exactly one source folder")
            if any("*" in p for p in paths):
                problems.append("rsync works on folders: remove the '*' wildcards")
        elif self.delete_unmatched:
            problems.append("'delete unmatched destination files' only applies to rsync")
        if problems:
            raise ValueError(f"{fn}: " + "; ".join(problems))
        return self

    def template_values(self) -> list[tuple[str, str, bool]]:
        out = [(f"sources[{i}]", s, True) for i, s in enumerate(self.sources)]
        return out + [("destination", self.destination, True)]

    def resolved_direction(self) -> str | None:
        return self.direction or copy_direction(self.sources, self.destination)


# ---------------------------------------------------------------- data quality
class DqCheck(_Model):
    kind: Literal["row_count_min", "not_null", "unique", "accepted_values", "freshness", "custom_sql"]
    table: str | None = None
    columns: list[str] = Field(default_factory=list)
    min_rows: int = Field(default=1, ge=0)
    accepted: list[str] = Field(default_factory=list)
    max_age_hours: int = Field(default=24, ge=1)
    sql: str | None = None
    where: str | None = None
    severity: Literal["error", "warn"] = "error"
    name: str | None = None

    @model_validator(mode="after")
    def _check(self) -> DqCheck:
        if self.kind == "custom_sql":
            if not (self.sql or "").strip():
                raise ValueError("custom SQL check needs a query (it passes when the query returns no rows)")
        elif not self.table:
            raise ValueError(f"{self.kind} check needs a table")
        elif _table_template_problem(self.table, "check table"):
            raise ValueError(_table_template_problem(self.table, "check table"))
        if self.kind in ("not_null", "unique", "accepted_values", "freshness") and not self.columns:
            raise ValueError(f"{self.kind} check needs at least one column")
        if self.kind in ("accepted_values", "freshness") and len(self.columns) != 1:
            raise ValueError(f"{self.kind} check takes exactly one column")
        if self.kind == "accepted_values" and not self.accepted:
            raise ValueError("accepted_values check needs the list of accepted values")
        for c in self.columns:
            if not IDENTIFIER_RE.match(c.replace(".", "_")):
                raise ValueError(f"invalid column name {c!r} in {self.kind} check")
        return self

    def label(self) -> str:
        if self.name:
            return self.name
        target = (self.table or "custom").split(".")[-1]
        cols = f"({', '.join(self.columns)})" if self.columns else ""
        return f"{self.kind}{cols} on {target}"


class DqStep(StepBase):
    type: Literal["dq"] = "dq"
    checks: list[DqCheck] = Field(min_length=1)

    def template_values(self) -> list[tuple[str, str, bool]]:
        return [(f"checks[{i}].table", c.table, False) for i, c in enumerate(self.checks) if c.table]


# ---------------------------------------------------------------- custom process (code written with Gemini)
REQUIREMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"
                            r"(\s*(==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+(\s*,\s*(==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+)*)?$")
# Names the step wrapper itself uses inside the function (a config key must not shadow them)
CUSTOM_WRAPPER_NAMES = frozenset({"start", "execution_time", "e", "s"})


class CustomSetting(_Model):
    """One key of a custom step's YAML block, e.g. host / port / database (passwords belong in Secret Manager)."""

    key: str
    value: str = ""  # a literal or {variables}: resolved per environment into the YAML
    description: str = ""

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not CONFIG_KEY_RE.match(v or ""):
            raise ValueError(f"setting {v!r} must be lower_snake_case (it becomes a key in the YAML)")
        return v

    @field_validator("value")
    @classmethod
    def _value(cls, v: str) -> str:
        problem = _path_value_problem(v)
        if problem:
            raise ValueError(problem)
        return v


def import_names(line: str) -> list[str]:
    """The names one import line binds (``import a.b`` → a, ``from x import y as z`` → z); ValueError if it is not
    exactly one absolute import statement."""
    try:
        tree = ast.parse(line.strip())
    except SyntaxError as exc:
        raise ValueError(f"import {line!r} is not valid Python ({exc.msg})") from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.Import, ast.ImportFrom)):
        raise ValueError(f"{line!r} must be one import statement, e.g. import requests")
    node = tree.body[0]
    if isinstance(node, ast.ImportFrom) and (node.level or any(a.name == "*" for a in node.names)):
        raise ValueError(f"{line!r}: write an absolute import of named objects (no '*', no relative import)")
    return [a.asname or a.name.split(".")[0] for a in node.names]


def code_body_problem(code: str) -> str | None:
    """None when the code compiles as the body of the step function."""
    try:
        ast.parse("def _step(config_data):\n" + textwrap.indent(code, "    "))
    except SyntaxError as exc:
        return f"the code is not valid Python (line {max((exc.lineno or 2) - 1, 1)}: {exc.msg})"
    return None


class CustomStep(StepBase):
    """A process the developer describes in plain words; Gemini (or the developer) writes the code that runs inside the
    team's step wrapper (begin/end logging, timing, try/except → RuntimeError). Its settings live in the YAML block
    ``config_key`` (per environment); secrets come from the script's SECRETS dict."""

    type: Literal["custom"] = "custom"
    config_key: str = "custom_config"
    request: str = ""  # what the step should do, in plain words (the brief Gemini writes the code from)
    settings: list[CustomSetting] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)  # lines for "# Base imports"
    requirements: list[str] = Field(default_factory=list)  # pip requirements for requirements.txt
    code: str = ""  # the statements inside the step's try: block, written at indent 0
    generated_by: str = ""  # the Gemini model that wrote the code ('' = written or edited by hand)

    @field_validator("config_key")
    @classmethod
    def _key(cls, v: str) -> str:
        v = _config_key(v)
        if v in RESERVED_FUNCTION_NAMES or v in CUSTOM_WRAPPER_NAMES:
            raise ValueError(f"config key {v!r} is a name the script already uses; pick another (e.g. {v}_config)")
        return v

    @field_validator("imports", "requirements", mode="before")
    @classmethod
    def _lines(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.splitlines()
        if isinstance(v, list):
            return [str(s).strip() for s in v if str(s or "").strip()]
        return v

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        return textwrap.dedent((v or "").replace("\t", "    ")).strip("\n")

    @model_validator(mode="after")
    def _check(self) -> CustomStep:
        problems: list[str] = []
        if not self.code.strip():
            problems.append("write the code (or let Gemini write it from the description)")
        else:
            problem = code_body_problem(self.code)
            if problem:
                problems.append(problem)
        bound: list[str] = []
        for line in self.imports:
            try:
                bound += import_names(line)
            except ValueError as exc:
                problems.append(str(exc))
        if self.config_key in bound:
            problems.append(f"config key {self.config_key!r} has the name of an import; pick another")
        bad = [r for r in self.requirements if not REQUIREMENT_RE.match(r)]
        if bad:
            problems.append(f"requirements must look like package>=1.2: {', '.join(bad[:5])}")
        keys = [s.key for s in self.settings]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            problems.append(f"duplicate settings: {', '.join(dupes)}")
        empty = [s.key for s in self.settings if not s.value]
        if empty:
            problems.append(f"enter the value of setting(s) {', '.join(empty)} (a literal, or {{variables}} that differ per environment)")
        if problems:
            raise ValueError(f"{self.function_name}: " + "; ".join(problems))
        return self

    def template_values(self) -> list[tuple[str, str, bool]]:
        return [(f"settings.{s.key}", s.value, False) for s in self.settings]


def custom_code_reads(step: CustomStep) -> tuple[list[str], list[str]]:
    """(secret names, setting keys) the code reads: SECRETS['x'] / SECRETS.get('x') and <config_key>['k'] / .get('k')."""
    secrets: list[str] = []
    keys: list[str] = []
    try:
        tree = ast.parse(step.code)
    except SyntaxError:
        return [], []
    for node in ast.walk(tree):
        target, key = None, None
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and isinstance(node.slice, ast.Constant):
            target, key = node.value.id, node.slice.value
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
              and isinstance(node.func.value, ast.Name) and node.args and isinstance(node.args[0], ast.Constant)):
            target, key = node.func.value.id, node.args[0].value
        if isinstance(key, str):
            if target == "SECRETS":
                secrets.append(key)
            elif target == step.config_key:
                keys.append(key)
    return list(dict.fromkeys(secrets)), list(dict.fromkeys(keys))


def custom_code_problems(step: CustomStep, spec: ProcessSpec) -> list[str]:
    """What the custom code must not do (checked on every edit, and before Gemini's code is accepted)."""
    try:
        tree = ast.parse(step.code)
    except SyntaxError:
        return []  # reported by the step itself
    problems: list[str] = []
    secrets, keys = custom_code_reads(step)
    known = {s.name for s in spec.secrets}
    missing = [s for s in secrets if s not in known]
    if missing:
        problems.append(f"the code reads secret(s) the process does not define: {', '.join(missing)} "
                        "(add them under Variables & secrets)")
    if secrets and not spec.secrets:
        problems.append("the code reads SECRETS but the process has no secrets")
    settings = {s.key for s in step.settings}
    unknown = [k for k in keys if k not in settings]
    if unknown:
        problems.append(f"the code reads setting(s) {', '.join(unknown)} that are not in the step's settings")
    values = {val for v in spec.variables if v.group in ("project", "dataset", "bucket")
              for val in (v.values.lab, v.values.value, v.values.qa) if len(val) >= 4}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else f"{getattr(fn.value, 'id', '')}.{fn.attr}" \
                if isinstance(fn, ast.Attribute) else ""
            if name == "print":
                problems.append("use logger.info(...), not print()")
            if name in ("sys.exit", "exit", "quit", "os._exit"):
                problems.append(f"do not call {name}(): raise an exception so main() reports the failure")
            if name == "print" or name.startswith("logger.") or name.startswith("logging."):
                args = [*node.args, *(k.value for k in node.keywords)]
                if any(isinstance(n, ast.Name) and n.id == "SECRETS" for a in args for n in ast.walk(a)):
                    problems.append("never log secrets (a logger call mentions SECRETS)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if "gs://" in text:
                problems.append(f"hard-coded GCS path {text[:60]!r}: read the bucket and folder from the settings")
            for val in values:
                if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(val)}(?![A-Za-z0-9_-])", text):
                    problems.append(f"hard-coded {val!r} (a variable's value): read it from config_data or the settings")
    return list(dict.fromkeys(problems))


Step = Annotated[Union[IngestStep, SqlStep, ExportStep, CopyStep, DqStep, CustomStep], Field(discriminator="type")]
STEP_TYPES = ("ingest", "sql", "export", "copy", "dq", "custom")


# ---------------------------------------------------------------- secrets (team's gsm_module)
class SecretRef(_Model):
    """A Google Secret Manager secret the script reads with the team's gsm_module (project = gsm_project).
    Only the location is stored; the script reads the value at run time."""

    name: str  # key in the script's SECRETS dict
    secret_id: str
    version: str = "latest"  # latest = newest version; a number pins one version
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not IDENTIFIER_RE.match(v) or v != v.lower():
            raise ValueError(f"secret name {v!r} must be lower_snake_case")
        return v

    @field_validator("secret_id")
    @classmethod
    def _secret_id(cls, v: str) -> str:
        if not SECRET_ID_RE.match(v):
            raise ValueError(f"secret id {v!r} may only contain letters, digits, '_' and '-' (the id, not the full path)")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        v = str(v or "latest").strip().lower()
        if v != "latest" and not v.isdigit():
            raise ValueError("secret version must be 'latest' or a version number such as 7")
        return v


# ---------------------------------------------------------------- email notification
class SendOn(_Model):
    failure: bool = True  # a step failed
    success: bool = True  # every step finished


class EmailSettings(_Model):
    """End-of-run email through the internal relay (host/port live in the hub's settings).
    The sender is the ADC identity's email, filled in when the bundle is generated."""

    enabled: bool = False
    receivers: EnvValues = Field(default_factory=EnvValues)  # comma-separated addresses per environment
    send_on: SendOn = Field(default_factory=SendOn)
    subject: str = "{script} | {status} | {environment}"
    message: str = "The {process} process finished with status {status}."
    attachments: list[str] = Field(default_factory=list)  # gs://bucket/path/file or local paths; {variables}, {date:%Y%m%d}
    file_summary: bool = False  # list the files ingestion and export steps handled (file, rows, table / gs:// path)

    @field_validator("attachments", mode="before")
    @classmethod
    def _attachments(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [str(s).strip() for s in v if str(s or "").strip()]
        return v

    @model_validator(mode="after")
    def _check(self) -> EmailSettings:
        if not self.enabled:
            return self
        problems = []
        if not (self.send_on.failure or self.send_on.success):
            problems.append("choose when to send the email (failure and/or success)")
        if any(ch in self.subject for ch in "\r\n"):
            problems.append("the subject must be one line")
        if '"""' in self.message or "\\" in self.message or '"""' in self.subject or "\\" in self.subject:
            problems.append('the subject and message must not contain """ or backslashes')
        for label, text in (("subject", self.subject), ("message", self.message)):
            try:
                unknown = [f for f, _ in template_fields(text) if f not in EMAIL_SUBJECT_TOKENS]
            except ValueError as exc:
                problems.append(f"{label}: {exc} (write literal braces as {{{{ and }}}})")
                continue
            if unknown:
                problems.append(f"{label} placeholders must be one of {{{'}, {'.join(EMAIL_SUBJECT_TOKENS)}}} "
                                f"(found {{{unknown[0]}}})")
        for path in self.attachments:
            problem = _path_value_problem(path)
            if problem:
                problems.append(f"attachment {problem}")
            elif path.endswith("/"):
                problems.append(f"attachment {path!r} is a folder; name a file (a * matches several)")
        if problems:
            raise ValueError("email: " + "; ".join(problems))
        return self


# ---------------------------------------------------------------- process-level settings
class UpdateNote(_Model):
    date: str  # YYYY-MM-DD
    pid: str = ""
    note: str = ""

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        try:
            dt.date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"update date {v!r} must be YYYY-MM-DD") from exc
        return v


class Metadata(_Model):
    name: str
    script_name: str | None = None  # file stem of <script_name>.py / <script_name>.yaml; None = the process name
    description: str = ""
    owner: str = ""
    co_owners: list[str] = Field(default_factory=list)
    team: str = ""
    tags: list[str] = Field(default_factory=list)
    # Header block of the generated script (team standard)
    authors: list[str] = Field(default_factory=list)  # developer PIDs
    reviewers: list[str] = Field(default_factory=list)
    jira: str = ""  # JIRA# or Service Now#
    created_on: str | None = None  # initial deployment date (YYYY-MM-DD); set on the first saved version
    updates: list[UpdateNote] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not PROCESS_NAME_RE.match(v):
            raise ValueError("process name must be lower_snake_case, 3-63 chars, starting with a letter")
        return v

    @field_validator("script_name")
    @classmethod
    def _script_name(cls, v: str | None) -> str | None:
        v = (v or "").strip().removesuffix(".py")
        if not v:
            return None
        problem = script_name_problem(v)
        if problem:
            raise ValueError(problem)
        return v

    @field_validator("authors", "reviewers", mode="before")
    @classmethod
    def _people(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.split(",")
        if isinstance(v, list):
            return [str(p).strip() for p in v if str(p or "").strip()]
        return v

    @field_validator("created_on")
    @classmethod
    def _created_on(cls, v: str | None) -> str | None:
        if v:
            dt.date.fromisoformat(v)
        return v or None


class Environments(_Model):
    qa_enabled: bool = False  # lab (dev) and value (prod) always exist

    def active(self) -> list[str]:
        return ["lab", "value"] + (["qa"] if self.qa_enabled else [])


PROCESS_TYPES = {"pipeline": "Pipeline", "copy": "Copy files"}


class ProcessSpec(_Model):
    spec_version: int = SPEC_VERSION
    process_type: Literal["pipeline", "copy"] = "pipeline"  # copy = a copy-files process (copy steps only)
    metadata: Metadata
    environments: Environments = Field(default_factory=Environments)
    variables: list[Variable] = Field(default_factory=list)
    secrets: list[SecretRef] = Field(default_factory=list)
    email: EmailSettings = Field(default_factory=EmailSettings)
    steps: list[Step] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        version = data.get("spec_version") or 1
        if isinstance(version, int) and version < SPEC_VERSION:
            from .migrate import upgrade_spec

            data, _notes = upgrade_spec(data)
        return ensure_builtin_variables(data)

    @field_validator("spec_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != SPEC_VERSION:
            raise ValueError(f"unsupported spec_version {v} (this version reads 1 to {SPEC_VERSION})")
        return v

    # ------------------------------------------------------------ helpers
    def script_stem(self) -> str:
        """File stem of the generated script and its YAML: <stem>.py, <stem>.yaml."""
        return self.metadata.script_name or self.metadata.name

    def schema_file(self) -> str:
        return f"{self.script_stem()}_table_schema.yaml"

    def active_envs(self) -> list[str]:
        return self.environments.active()

    def variable_map(self) -> dict[str, Variable]:
        return {v.name: v for v in self.variables}

    def variable_values(self, env: str = "lab") -> dict[str, str]:
        """{name: value} for one environment (lab by default: the only one the hub checks or runs)."""
        return {v.name: v.values.get(env) for v in self.variables}

    def enabled_steps(self) -> list[Any]:
        return [s for s in self.steps if s.enabled]

    def ingest_step(self) -> IngestStep | None:
        return next((s for s in self.steps if isinstance(s, IngestStep)), None)

    def code_variables(self) -> list[str]:
        """Variables the script reads from the YAML itself (used in SQL or in table names inside the code),
        in definition order. Other variables are only resolved into the YAML's step blocks."""
        used: set[str] = set()
        for step in self.steps:
            for sql_text, _where in _sql_texts(step):
                used.update(sql_var_tokens(sql_text))
            for field_name, value, _allow in step.template_values():
                if field_name in ("destination_table", "source_table") or field_name.startswith("checks["):
                    used.update(_template_names(value))
        return [v.name for v in self.variables if v.name in used]

    def used_variables(self) -> set[str]:
        used = set(BUILTIN_PROJECTS) | set(self.code_variables())
        for step in self.steps:
            for _f, value, _a in step.template_values():
                used.update(_template_names(value))
        for path in self.email.attachments if self.email.enabled else []:
            used.update(_template_names(path))
        if self.secrets:
            used.add(GSM_PROJECT)
        return used

    def config_params(self) -> dict[str, Parameter]:
        """Parameters whose value comes from the YAML (source 'config'), by name."""
        out: dict[str, Parameter] = {}
        for step in self.steps:
            for p in getattr(step, "parameters", []):
                if p.source == "config":
                    out.setdefault(p.name, p)
        return out

    def spec_hash(self) -> str:
        data = self.model_dump(
            mode="json",
            exclude={"metadata": {"created_at", "updated_at", "updated_by", "co_owners", "created_on", "updates"}},
        )
        for step in data["steps"]:  # fields added later, at their default: the hash of older processes stays the same
            if step.get("sheet_name") == "":
                del step["sheet_name"]
            if step.get("target_enabled") is True:
                del step["target_enabled"]
        if data["email"].get("file_summary") is False:
            del data["email"]["file_summary"]
        blob = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    # ------------------------------------------------------------ cross-field validation
    @model_validator(mode="after")
    def _cross_checks(self) -> ProcessSpec:
        problems: list[str] = []
        envs = self.active_envs()
        names = [s.function_name for s in self.steps]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            problems.append(f"step function names must be unique: {', '.join(dupes)}")
        var_names = [v.name for v in self.variables]
        vdupes = sorted({n for n in var_names if var_names.count(n) > 1})
        if vdupes:
            problems.append(f"duplicate variables: {', '.join(vdupes)}")
        secret_names = [s.name for s in self.secrets]
        sdupes = sorted({n for n in secret_names if secret_names.count(n) > 1})
        if sdupes:
            problems.append(f"duplicate secrets: {', '.join(sdupes)}")
        if self.steps and not self.enabled_steps():
            problems.append("at least one step must be enabled")
        if self.process_type == "copy":
            others = [s.function_name for s in self.steps if not isinstance(s, CopyStep)]
            if others:
                problems.append(f"a copy-files process only has copy jobs: {', '.join(others)}")
        ingests = [s.function_name for s in self.steps if isinstance(s, IngestStep)]
        if len(ingests) > 1:
            problems.append(f"only one file-ingestion step per process (the script takes one -ts table schema): "
                            f"{', '.join(ingests)}")
        vmap = self.variable_map()
        for b in BUILTIN_PROJECTS:
            if b in vmap and vmap[b].group != "project":
                problems.append(f"variables.{b}: must be in the 'project' group")

        known_vars = set(vmap)
        for step in self.steps:
            for sql_text, where in _sql_texts(step):
                missing = [t for t in sql_var_tokens(sql_text) if t not in known_vars]
                if missing:
                    problems.append(
                        f"{step.function_name}: {where} uses undefined variable(s) " + ", ".join(f"${{{m}}}" for m in missing)
                    )
            for field_name, value, allow_date in step.template_values():
                for p in template_problems(value, known_vars, allow_date=allow_date):
                    problems.append(f"{step.function_name}.{field_name}: {p}")

        # values: every variable the script uses needs a value in every active environment
        code_vars = set(self.code_variables())
        used = self.used_variables()
        for v in self.variables:
            if v.name not in used:
                continue
            for env in envs:
                value = v.values.get(env)
                if not value:
                    problems.append(f"variables.{v.name}: enter the {ENV_LABELS[env]} value")
                elif v.name in code_vars and not SAFE_VALUE_RE.match(value):
                    problems.append(f"variables.{v.name}: it is used in SQL, so its {env} value may only contain "
                                    f"letters, digits, '_', '-', '.', ':' (got {value!r})")
        for step in self.steps:
            for field_name, value, _allow in step.template_values():
                if field_name in ("read_bucket", "archive_bucket", "bucket") and value:
                    for env in envs:
                        resolved = fill_variables(value, self.variable_values(env))
                        if "{" not in resolved and resolved and not BUCKET_RE.match(resolved):
                            problems.append(f"{step.function_name}.{field_name}: {resolved!r} ({env}) is not a valid bucket name")

        # YAML keys must not collide
        seen_keys: dict[str, str] = {}
        top_level = [n for n in self.code_variables() if vmap[n].group != "dataset"]
        for n in top_level:
            seen_keys[n] = f"variable {n}"
        for name, p in self.config_params().items():
            if name in seen_keys:
                problems.append(f"config parameter @{name} has the same name as {seen_keys[name]}")
            seen_keys[name] = f"config parameter @{name}"
            for step in self.steps:
                for other in getattr(step, "parameters", []):
                    if other.name == name and other.source == "config" and other.default != p.default:
                        problems.append(f"config parameter @{name} has different values in different steps")
        for step in self.steps:
            key = step.config_section()
            if key:
                if key in seen_keys:
                    problems.append(f"{step.function_name}: config key '{key}' is already used by {seen_keys[key]}")
                seen_keys[key] = f"step {step.function_name}"

        if self.secrets and GSM_PROJECT not in vmap:
            problems.append("secrets need the gsm_project variable (the project that holds them)")
        if self.email.enabled:
            for env in envs:
                addresses, email_problems = parse_receivers(self.email.receivers.get(env))
                for p in email_problems:
                    problems.append(f"email.receivers.{env}: {p}")
                if not addresses and not email_problems:
                    problems.append(f"email.receivers.{env}: add at least one recipient for {ENV_LABELS[env]}")
            for path in self.email.attachments:
                for p in template_problems(path, known_vars, allow_date=True):
                    problems.append(f"email.attachments: {p}")
        for step in self.steps:
            if isinstance(step, IngestStep) and step.on_no_files == "email" and not self.email.enabled:
                problems.append(f"{step.function_name}: 'email when no file arrives' needs the email turned on")
            if isinstance(step, CustomStep):
                if step.config_key in names:
                    problems.append(f"{step.function_name}: config key '{step.config_key}' has the name of a step function")
                problems += [f"{step.function_name}: {p}" for p in custom_code_problems(step, self)]
        if problems:
            raise ValueError("; ".join(problems))
        return self


def ensure_builtin_variables(data: dict[str, Any]) -> dict[str, Any]:
    """read_project and write_project always exist (and gsm_project when there are secrets)."""
    variables = list(data.get("variables") or [])
    names = {v.get("name") for v in variables if isinstance(v, dict)}
    wanted = list(BUILTIN_PROJECTS) + ([GSM_PROJECT] if data.get("secrets") else [])
    missing = [b for b in wanted if b not in names]
    if not missing:
        return data
    data = dict(data)
    data["variables"] = [{"name": b, "group": "project", "values": {}} for b in missing] + variables
    return data


def _template_names(value: str | None) -> set[str]:
    try:
        return {name for name, _spec in template_fields(value or "") if name != "date"}
    except ValueError:
        return set()


def _sql_texts(step: Any) -> list[tuple[str, str]]:
    if isinstance(step, SqlStep):
        return [(step.sql, "SQL")]
    if isinstance(step, ExportStep) and step.source_kind == "query" and step.source_sql:
        return [(step.source_sql, "export query")]
    if isinstance(step, DqStep):
        out = []
        for i, c in enumerate(step.checks):
            if c.sql:
                out.append((c.sql, f"check {i + 1} SQL"))
            if c.where:
                out.append((c.where, f"check {i + 1} filter"))
        return out
    return []


def _check_params_unique(fn: str, params: list[Parameter]) -> None:
    names = [p.name for p in params]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"{fn}: duplicate parameters: {', '.join(dupes)}")


def format_validation_errors(exc: Exception) -> list[dict[str, str]]:
    """Turn a pydantic ValidationError into [{path, message}] the UI can show next to fields."""
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        return [{"path": "", "message": str(exc)}]
    out = []
    for err in exc.errors():
        loc = []
        for part in err.get("loc", ()):
            if isinstance(part, int):
                loc.append(f"[{part}]")
            elif part in STEP_TYPES or str(part).startswith("function-after["):
                continue
            else:
                loc.append(f".{part}" if loc else str(part))
        msg = err.get("msg", "")
        msg = msg.removeprefix("Value error, ")
        path = "".join(loc)
        if path:
            out.append({"path": path, "message": msg})
            continue
        # process-level checks arrive as one "a; b; c" message: one entry each, placed by their prefix
        for item in msg.split("; "):
            m = re.match(r"^((?:variables|email|secrets|metadata|environments)[\w.\[\]]*): ", item)
            out.append({"path": m.group(1) if m else "", "message": item})
    return out
