"""Render a ProcessSpec into the generated bundle (script, YAML, table schema, README, …) and prove it is sound.

The script follows the team's production template (header block, sys.path module imports, CONFIG + logger,
one function per step with begin/end timing, ``main(args)``, argparse ``environment -cf [-ts]``).

Rendering pipeline: build template views → Jinja2 (StrictUndefined) → black → ast.parse → SQL round-trip
guard → YAML round-trip guards → ruff → offline smoke run (optional). Any failure raises RenderError, so broken
code is never handed to a user.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import string
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import black
import jinja2
import yaml

from .. import GENERATOR
from ..models.rules import fill_variables, parse_receivers, sql_var_tokens, substitute_sql_vars, template_fields
from ..models.spec import (
    BUILTIN_PROJECTS,
    COPY_DIRECTIONS,
    ENV_LABELS,
    EXCEL_MAX_ROWS,
    FILE_EXTENSIONS,
    FORMAT_LABELS,
    GSM_PROJECT,
    INGEST_EXTENSIONS,
    CopyStep,
    CustomStep,
    DqCheck,
    DqStep,
    ExportStep,
    IngestStep,
    Parameter,
    ProcessSpec,
    SqlStep,
    bq_format,
    custom_code_reads,
    has_header_rows,
    has_text_rows,
    is_delimited,
)
from ..settings import Settings, get_settings
from . import sql_utils
from .filters import FILTERS, EmbedError, embed_sql, verify_embedded_sql, yaml_value
from .lineage import TableInventory, export_uri_template, ingest_source_template, ingest_tables, process_tables

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
BLACK_MODE = black.Mode(line_length=110, string_normalization=False)
SQL_INDENT = 12  # SQL text sits inside the step's try: block

KIND_LABELS = {
    "ingest": "File ingestion (GCS → BigQuery)",
    "sql": "SQL",
    "export": "Export (BigQuery → GCS)",
    "copy": "Copy files (gcloud storage)",
    "dq": "Data quality",
    "custom": "Custom code",
}
RUN_COMMENTS = {"lab": "lab (dev): your own gcloud credentials", "value": "value (prod): the Composer service account"}
WRITE_DISPOSITIONS = {"append": "WRITE_APPEND", "truncate": "WRITE_TRUNCATE", "empty": "WRITE_EMPTY"}
PARAM_EXPRESSIONS = {
    "today": ("date.today()", "today's date"),
    "today_offset": ("date.today() + timedelta(days={offset})", "today {offset_txt}"),
    "month_start": ("date.today().replace(day=1)", "first day of this month"),
    "month_end": (
        "(date.today().replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)",
        "last day of this month",
    ),
    "prev_month_start": ("(date.today().replace(day=1) - timedelta(days=1)).replace(day=1)", "first day of last month"),
    "prev_month_end": ("date.today().replace(day=1) - timedelta(days=1)", "last day of last month"),
    "year_start": ("date.today().replace(month=1, day=1)", "1 January of this year"),
}
EXPORT_DATA_FORMATS = {"CSV": "CSV", "TXT": "CSV", "NEWLINE_DELIMITED_JSON": "JSON", "AVRO": "AVRO", "PARQUET": "PARQUET"}
YAML_COMMENTS = {
    "read_project": "project the SQL reads from",
    "write_project": "project the process writes to",
    "gsm_project": "project that holds the secrets (gsm_module)",
    "bucket": "change accordingly",
    "path": "change accordingly ('' = the bucket root)",
    "file_name": "a date suffix and the extension are added by the script",
    "gcs_read_bucket": "change accordingly",
    "gcs_read_bucket_path": "change accordingly",
    "gcs_archive_bucket": "processed files are moved here",
    "filename": "files whose name contains this text are loaded",
    "header": "pandas header row ('None' = no header row)",
    "sheet_name": "Excel sheet to read ('' = the first one)",
    "column_widths": "fixed-width file: characters per column, in file order",
    "skiprows": "pandas skiprows ('None', '1' or '[0, 2]')",
    "temporary": "staging dataset (every file is loaded here first)",
    "target": "dataset of the history and target tables",
    "sources": "gs:// or local paths; {date:%Y%m%d} = today",
    "destination": "gs:// or local folder",
    "sender": "the ADC identity's email",
    "receiver": "recipients",
}


class RenderError(Exception):
    def __init__(self, problems: list[str], result: RenderResult | None = None):
        super().__init__("; ".join(problems))
        self.problems = problems
        self.result = result


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class RenderResult:
    process: str
    files: dict[str, str]
    script: str = ""  # file stem: <script>.py, <script>.yaml (defaults to the process name)
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_file: str | None = None
    # what the offline smoke run executes instead of a bundle file (custom code is not run offline); not in the bundle
    smoke_files: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.script = self.script or self.process

    def language_of(self, path: str) -> str:
        return {".py": "python", ".yaml": "yaml", ".json": "json", ".md": "markdown"}.get(Path(path).suffix, "text")


@lru_cache(maxsize=1)
def jinja_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters.update(FILTERS)
    return env


# ====================================================================================== small helpers
def _log_begin(fn: str) -> str:
    return "logger.info(f'Function {" + fn + ".__name__} execution begin')"


def _log_end(fn: str) -> str:
    return 'logger.info(f"Function {' + fn + '.__name__} execution end:  {execution_time} seconds.")'


def _fstr(template: str) -> str:
    """Python expression for a {var} table template, e.g. f'{write_project}.{mart_dataset}.sales'."""
    return ("f" if _names(template) else "") + repr(template)


def _names(template: str | None) -> list[str]:
    try:
        return [n for n, _spec in template_fields(template or "") if n != "date"]
    except ValueError:
        return []


def _brace_escape(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def _ordered(spec: ProcessSpec, names: Iterable[str]) -> list[str]:
    wanted = set(names)
    return [v.name for v in spec.variables if v.name in wanted]


def _groups(spec: ProcessSpec, names: Iterable[str]) -> tuple[list[str], list[str], list[str]]:
    """(projects, datasets, others) in definition order; read_project/write_project first."""
    vmap = spec.variable_map()
    ordered = _ordered(spec, names)
    projects = sorted((n for n in ordered if vmap[n].group == "project"),
                      key=lambda n: BUILTIN_PROJECTS.index(n) if n in BUILTIN_PROJECTS else len(BUILTIN_PROJECTS))
    datasets = [n for n in ordered if vmap[n].group == "dataset"]
    others = [n for n in ordered if vmap[n].group in ("bucket", "other")]
    return projects, datasets, others


def _arg_plan(spec: ProcessSpec, used: Iterable[str], params: list[str]) -> tuple[list[str], list[str]]:
    """Function arguments (values main() reads from the YAML) and the lines that unpack data_sets."""
    projects, datasets, others = _groups(spec, used)
    args = projects + (["data_sets"] if datasets else []) + others + params
    breakout = [f"{d} = data_sets.get({d!r})" for d in datasets]
    return args, breakout


def _config_breakout(spec: ProcessSpec, used: Iterable[str], params: list[str]) -> list[str]:
    """Lines that read the values a config_data-style step needs (export: source table / query variables)."""
    projects, datasets, others = _groups(spec, used)
    lines = [f"{n} = config_data.get({n!r})" for n in projects + others]
    if datasets:
        lines.append("data_sets = config_data.get('data_sets', {})")
        lines += [f"{d} = data_sets.get({d!r})" for d in datasets]
    lines += [f"{p} = config_data.get({p!r})" for p in params]
    return lines


# ====================================================================================== views
def _param_view(p: Parameter, process: str) -> SimpleNamespace:
    if p.source == "config":
        return SimpleNamespace(name=p.name, bq_type=p.bq_type, expr=p.name,
                               explain=f"'{p.name}' in the YAML (default {p.default})", config=True)
    if p.source == "process_name":
        return SimpleNamespace(name=p.name, bq_type=p.bq_type, expr=repr(process), explain="the process name", config=False)
    template, explain = PARAM_EXPRESSIONS[p.source]
    expr = template.format(offset=p.offset_days)
    if p.source == "today_offset" and p.offset_days == 0:
        expr = "date.today()"
    explain = explain.format(offset_txt=f"{p.offset_days:+d} day(s)")
    if p.bq_type == "STRING":
        expr = f"({expr}).isoformat()" if " " in expr else f"{expr}.isoformat()"
    elif p.bq_type in ("TIMESTAMP", "DATETIME"):
        expr = f"datetime.combine({expr}, datetime.min.time())"
    return SimpleNamespace(name=p.name, bq_type=p.bq_type, expr=expr, explain=explain, config=False)


def _used_params(fn: str, sql: str, params: list[Parameter], values: dict[str, str], warnings: list[str]) -> list[Parameter]:
    """Parameters actually referenced by the SQL (errors on undefined ones)."""
    try:
        concrete = substitute_sql_vars(sql, values)
    except KeyError as exc:
        raise RenderError([f"{fn}: SQL uses undefined variable ${{{exc.args[0]}}}"]) from None
    used = sql_utils.find_query_params(concrete)
    defined = {p.name: p for p in params}
    missing = [u for u in used if u not in defined]
    if missing:
        raise RenderError([f"{fn}: SQL uses @{', @'.join(missing)} but no such parameter is defined"])
    unused = [p.name for p in params if p.name not in used]
    if unused:
        warnings.append(f"{fn}: parameter(s) @{', @'.join(unused)} are defined but not used in the SQL; left out")
    return [defined[u] for u in used]


def _embed(fn: str, sql: str, values: dict[str, str], indent: int) -> str:
    literal = embed_sql(sql, indent=indent)
    try:
        verify_embedded_sql(literal, sql, values, indent)
    except EmbedError as exc:
        raise RenderError([f"{fn}: {exc}"]) from exc
    return literal


def _default_doc(step: Any) -> str:
    if isinstance(step, IngestStep):
        return f"Load files named '{step.filename}*' from gs://{step.read_bucket}/{step.read_path} into {step.target()}"
    if isinstance(step, SqlStep):
        targets = ", ".join(step.targets) or step.destination_table or "BigQuery"
        return f"Run {step.statement_kind} SQL on {targets}"
    if isinstance(step, ExportStep):
        source = step.source_table if step.source_kind == "table" else "query results"
        return f"Export {source} to gs://{step.bucket}/{step.path}"
    if isinstance(step, CopyStep):
        return f"gcloud storage {step.operation} {', '.join(step.sources)} to {step.destination}"
    if isinstance(step, DqStep):
        return f"Run {len(step.checks)} data-quality check(s)"
    if isinstance(step, CustomStep) and step.request:
        return " ".join(step.request.split())[:200]
    return step.function_name


def _ident(name: str) -> str:
    return f"`{name}`"


def _sql_str(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def dq_check_sql(check: DqCheck, step_fn: str) -> str:
    """SQL (with ${var} placeholders) returning one row: value [, details]."""
    table_sql = ""
    if check.table:
        parts = []
        for literal, name, _spec, _conv in string.Formatter().parse(check.table):
            parts.append(literal)
            if name is not None:
                parts.append("${" + name + "}")
        table_sql = f"`{''.join(parts)}`"
    where = f"\nWHERE ({check.where})" if check.where else ""
    cols = check.columns
    if check.kind == "row_count_min":
        return f"SELECT COUNT(*) AS value\nFROM {table_sql}{where}"
    if check.kind == "not_null":
        total = " + ".join(f"COUNTIF({_ident(c)} IS NULL)" for c in cols)
        details = ", ".join(f"COUNTIF({_ident(c)} IS NULL) AS {c.replace('.', '_')}" for c in cols)
        return f"SELECT {total} AS value,\n  TO_JSON_STRING(STRUCT({details})) AS details\nFROM {table_sql}{where}"
    if check.kind == "unique":
        keys = ", ".join(_ident(c) for c in cols)
        return (
            "SELECT COUNT(*) AS value,\n"
            f"  TO_JSON_STRING(ARRAY_AGG(STRUCT({keys}) LIMIT 5)) AS details\n"
            f"FROM (\n  SELECT {keys}\n  FROM {table_sql}{where.replace(chr(10), chr(10) + '  ')}\n"
            f"  GROUP BY {keys}\n  HAVING COUNT(*) > 1\n)"
        )
    if check.kind == "accepted_values":
        col = _ident(cols[0])
        values = ", ".join(_sql_str(v) for v in check.accepted)
        extra = f"\n  AND ({check.where})" if check.where else ""
        return (
            "SELECT COUNT(*) AS value,\n"
            f"  TO_JSON_STRING(ARRAY_AGG(DISTINCT CAST({col} AS STRING) LIMIT 10)) AS details\n"
            f"FROM {table_sql}\nWHERE {col} IS NOT NULL\n  AND CAST({col} AS STRING) NOT IN ({values}){extra}"
        )
    if check.kind == "freshness":
        col = _ident(cols[0])
        return f"SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), CAST(MAX({col}) AS TIMESTAMP), HOUR) AS value\nFROM {table_sql}{where}"
    if check.kind == "custom_sql":
        body = (check.sql or "").strip().rstrip(";")
        return f"SELECT COUNT(*) AS value\nFROM (\n{body}\n)"
    raise RenderError([f"{step_fn}: unknown check kind {check.kind}"])


def dq_check_expectation(check: DqCheck) -> tuple[str, int]:
    if check.kind == "row_count_min":
        return ">=", check.min_rows
    if check.kind == "freshness":
        return "<=", check.max_age_hours
    return "==", 0


def _sql_target(step: SqlStep) -> str | None:
    """The table a DDL/DML/MERGE step writes (for the 'Loaded N rows' log line), if there is a clear one."""
    _sources, ops = sql_utils.table_template_ops(step.sql)
    for template, op in ops.items():
        if "DROP" in (op or "").upper():
            continue
        return template
    return None


def build_step_views(spec: ProcessSpec, warnings: list[str]) -> list[SimpleNamespace]:
    values = spec.variable_values("lab")
    process = spec.metadata.name
    views = []
    position = 0
    for step in spec.steps:
        if step.enabled:
            position += 1
        fn = step.function_name
        doc = step.description or _default_doc(step)
        v = SimpleNamespace(
            fn=fn,
            kind=step.type,
            kind_label=KIND_LABELS[step.type],
            enabled=step.enabled,
            on_failure=step.on_failure,
            doc=doc,
            position=position if step.enabled else None,
            position_label=f"{position}." if step.enabled else "–",
            summary=step.description,
            log_begin=_log_begin(fn),
            log_end=_log_end(fn),
            args=["config_data"],
        )
        if isinstance(step, SqlStep):
            v.kind_label = f"SQL ({step.statement_kind})"
            used = _used_params(fn, step.sql, step.parameters, values, warnings)
            v.params = [_param_view(p, process) for p in used]
            names = set(sql_var_tokens(step.sql)) | set(_names(step.destination_table))
            target = None if step.destination_table else _sql_target(step)
            names |= set(_names(target))
            v.args, v.breakout = _arg_plan(spec, names, [p.name for p in used if p.source == "config"])
            v.sql_literal = _embed(fn, step.sql, values, SQL_INDENT)
            v.destination = bool(step.destination_table)
            v.write_disposition = WRITE_DISPOSITIONS[step.write_mode]
            v.dest_expr = _fstr(step.destination_table) if step.destination_table else (_fstr(target) if target else "")
            v.has_job_config = bool(v.params or v.destination)
            v.target_label = step.destination_table or target or ", ".join(step.targets)
        elif isinstance(step, ExportStep):
            v.config_key = step.config_key
            v.source_kind = step.source_kind
            v.xlsx = step.file_format == "XLSX"
            v.sheet_name = step.sheet_name or "Sheet1"
            v.max_rows = EXCEL_MAX_ROWS - (1 if step.print_header else 0)
            v.delimited = is_delimited(step.file_format)
            v.bq_format = bq_format(step.file_format)
            v.compression = step.compression
            v.print_header = step.print_header
            v.field_delimiter = step.field_delimiter
            v.date_suffix = step.date_suffix
            ext = FILE_EXTENSIONS[step.file_format] + (".gz" if step.compression == "GZIP" else "")
            v.name_tail = ("-*" if step.sharded else "") + ext
            v.format_label = FORMAT_LABELS.get(step.file_format, step.file_format)
            if step.source_kind == "query":
                used = _used_params(fn, step.source_sql or "", step.parameters, values, warnings)
                v.params = [_param_view(p, process) for p in used]
                v.sql_literal = _embed(fn, step.source_sql or "", values, SQL_INDENT)
                v.breakout = _config_breakout(spec, sql_var_tokens(step.source_sql or ""),
                                              [p.name for p in used if p.source == "config"])
                options = ["uri = '{destination_uri}'", f"format = '{EXPORT_DATA_FORMATS.get(step.file_format, '')}'",
                           "overwrite = true"]
                if v.delimited:
                    delim = "\\\\t" if step.field_delimiter == "\t" else step.field_delimiter
                    options += [f"header = {'true' if step.print_header else 'false'}", f"field_delimiter = '{delim}'"]
                if step.compression != "NONE":
                    options.append(f"compression = '{step.compression}'")
                v.export_options = ", ".join(options)
            else:
                v.params = []
                v.source_expr = _fstr(step.source_table or "")
                v.breakout = _config_breakout(spec, _names(step.source_table), [])
        elif isinstance(step, IngestStep):
            v.config_key = step.config_key
            v.delimited = is_delimited(step.file_format)
            v.fixed = step.file_format == "FIXED"
            v.xlsx = step.file_format == "XLSX"
            v.text_rows = has_text_rows(step.file_format)
            v.header_rows = has_header_rows(step.file_format)
            v.file_format = step.file_format
            v.archive = step.archive
            v.history = step.history
            v.target = step.target_enabled
            v.truncate_target = step.target_enabled and step.load_mode == "truncate"
            v.target_verb = "merged into" if step.load_mode == "merge" else "inserted into"
            v.load_mode = step.load_mode
            v.on_no_files = step.on_no_files
            v.audit_columns = step.add_audit_columns
            v.extensions = repr(INGEST_EXTENSIONS[step.file_format])
            target = step.target()
            v.target_expr = "table_name" if target == step.table_name else repr(target)
            v.args = ["config_data", "table_def"] + (["environment"] if step.on_no_files == "email" else [])
            v.merge_keys = repr(list(step.merge_keys))
            v.partition_by = ", ".join(_ident(k) for k in step.merge_keys)
            v.on_expr = " AND ".join(f"T.`{k}` = S.`{k}`" for k in step.merge_keys)
            v.merge_order_by = step.merge_order_by or ""
            v.merge_filter = _brace_escape(step.merge_filter.strip())
            v.merge_doc = (f"newest row per {', '.join(step.merge_keys)} (by {step.merge_order_by}) wins"
                           if step.merge_order_by else f"matched on {', '.join(step.merge_keys)}")
        elif isinstance(step, CopyStep):
            v.config_key = step.config_key
            v.operation = step.operation
            flags = []
            if step.recursive:
                flags.append("--recursive")
            if step.no_clobber:
                flags.append("--no-clobber")
            if step.delete_unmatched:
                flags.append("--delete-unmatched-destination-objects")
            v.flags = flags
            v.flags_text = "".join(" " + f for f in flags)
            v.uses_date = any("date" in [n for n, _s in template_fields(p)] for p in [*step.sources, step.destination])
        elif isinstance(step, DqStep):
            checks = []
            names: set[str] = set()
            for c in step.checks:
                sql = dq_check_sql(c, fn)
                concrete = substitute_sql_vars(sql, values)
                if sql_utils.find_query_params(concrete):
                    raise RenderError([f"{fn}: data-quality checks cannot use @parameters (use CURRENT_DATE() etc.)"])
                names |= set(sql_var_tokens(sql))
                op, threshold = dq_check_expectation(c)
                checks.append(SimpleNamespace(name=c.label(), severity=c.severity, op=op, threshold=threshold, sql=sql,
                                              sql_literal=_embed(fn, sql, values, SQL_INDENT)))
            v.checks = checks
            v.args, v.breakout = _arg_plan(spec, names, [])
        elif isinstance(step, CustomStep):
            v.config_key = step.config_key
            v.uses_config = uses_name(step.code, step.config_key)
            v.code_block = textwrap.indent(step.code, " " * 8)
        views.append(v)
    return views


def uses_name(code: str, name: str) -> bool:
    """Does the code read this name (the custom step's settings dict)?"""
    try:
        return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(ast.parse(code)))
    except SyntaxError:
        return bool(re.search(rf"\b{re.escape(name)}\b", code))


# ====================================================================================== config YAML
def env_config(spec: ProcessSpec, env: str, *, sender: str, receivers: list[str] | None = None) -> dict[str, Any]:
    """The gcp_projects.<env> block: values resolved for that environment."""
    values = spec.variable_values(env)

    def fill(template: str | None) -> str:
        return fill_variables(template or "", values)

    code_vars = spec.code_variables()
    out: dict[str, Any] = {"read_project": values.get("read_project", ""), "write_project": values.get("write_project", "")}
    if spec.secrets:
        out[GSM_PROJECT] = values.get(GSM_PROJECT, "")
    projects, datasets, others = _groups(spec, code_vars)
    for n in projects:
        if n not in out:
            out[n] = values[n]
    for name, p in spec.config_params().items():
        out[name] = p.default
    if datasets:
        out["data_sets"] = {d: values[d] for d in datasets}
    for n in others:
        out[n] = values[n]
    ingest = spec.ingest_step()
    if ingest:
        out["write_disposition"] = ingest.write_disposition
        gcs: dict[str, Any] = {"gcs_read_bucket": fill(ingest.read_bucket), "gcs_read_bucket_path": fill(ingest.read_path).strip("/")}
        if ingest.archive:
            gcs["gcs_archive_bucket"] = fill(ingest.archive_bucket or ingest.read_bucket)
            gcs["gcs_archive_bucket_path"] = fill(ingest.archive_path).strip("/")
        gcs["filename"] = fill(ingest.filename)
        if ingest.file_format == "XLSX":
            gcs["sheet_name"] = ingest.sheet_name
        if has_header_rows(ingest.file_format):
            gcs.update(header=ingest.header, skiprows=ingest.skiprows)
        if is_delimited(ingest.file_format):
            gcs["separator"] = ingest.separator
        out[ingest.config_key] = gcs
        out["datasets"] = {"temporary": fill(ingest.staging_dataset)}
        if ingest.uses_target_dataset():  # the history and target tables
            out["datasets"]["target"] = fill(ingest.target_dataset)
    for step in spec.steps:
        if isinstance(step, ExportStep):
            out[step.config_key] = {"bucket": fill(step.bucket), "path": fill(step.path).strip("/"), "file_name": fill(step.file_name)}
        elif isinstance(step, CopyStep):
            out[step.config_key] = {"sources": [fill(s) for s in step.sources], "destination": fill(step.destination)}
        elif isinstance(step, CustomStep):
            out[step.config_key] = {s.key: fill(s.value) for s in step.settings}
    if spec.email.enabled:
        addresses = receivers if receivers is not None else parse_receivers(spec.email.receivers.get(env))[0]
        out["email"] = {"sender": sender, "receiver": list(addresses), "attachment": [fill(a) for a in spec.email.attachments]}
    return out


def build_config_dict(
    spec: ProcessSpec, *, sender: str = "", envs: list[str] | None = None, receivers: list[str] | None = None
) -> dict[str, Any]:
    """{gcp_projects: {lab: …, value: …[, qa: …]}} — what read_config_yaml.read_configuration reads."""
    envs = envs or spec.active_envs()
    return {"gcp_projects": {env: env_config(spec, env, sender=sender, receivers=receivers) for env in envs}}


def _yaml_lines(data: dict[str, Any], indent: int) -> list[str]:
    lines: list[str] = []
    pad = "  " * indent
    for key, value in data.items():
        comment = YAML_COMMENTS.get(key)
        tail = f"  # {comment}" if comment else ""
        if isinstance(value, dict):
            if value:
                lines.append(f"{pad}{key}:{tail}")
                lines += _yaml_lines(value, indent + 1)
            else:
                lines.append(f"{pad}{key}: {{}}{tail}")
        elif isinstance(value, list):
            if value:
                lines.append(f"{pad}{key}:{tail}")
                lines += [f"{pad}  - {yaml_value(item)}" for item in value]
            else:
                lines.append(f"{pad}{key}: []{tail}")
        else:
            lines.append(f"{pad}{key}: {yaml_value(value)}{tail}")
    return lines


def render_config_yaml(config: dict[str, Any], header: list[str]) -> str:
    lines = [f"# {h}" if h else "#" for h in header] + ["", "# Configuration for GCP projects", "gcp_projects:"]
    for env, block in config["gcp_projects"].items():
        lines.append(f"  {env}:")
        kind = {"lab": " (dev)", "value": " (prod)"}.get(env, "")
        lines.append(f"    # Configuration for the '{env}'{kind} project")
        lines += _yaml_lines(block, 2)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def table_schema_dict(step: IngestStep) -> dict[str, Any]:
    definition: dict[str, Any] = {"table_name": step.table_name, "table_schema": {c.name: c.type for c in step.columns}}
    if step.file_format == "FIXED":
        definition["column_widths"] = {c.name: c.width for c in step.columns}
    return {"table_definition": definition}


def render_table_schema(step: IngestStep, header: list[str]) -> str:
    lines = [f"# {h}" for h in header] + ["table_definition:", f"  table_name: {yaml_value(step.table_name)}", "  table_schema:"]
    lines += [f"    {c.name}: {c.type}" + (f"  # {c.description}" if c.description else "") for c in step.columns]
    if step.file_format == "FIXED":
        lines.append(f"  column_widths:  # {YAML_COMMENTS['column_widths']} (record length {step.record_length()})")
        position = 1
        for c in step.columns:
            lines.append(f"    {c.name}: {c.width}  # positions {position}-{position + (c.width or 0) - 1}")
            position += c.width or 0
    return "\n".join(lines) + "\n"


# ====================================================================================== script pieces
def _docblock(text: str) -> str:
    return " ".join(str(text or "").split()).replace("\\", "\\\\").replace("'''", "\\'\\'\\'")


def header_block(*, authors: list[str], reviewers: list[str], created_on: str, jira: str, description: str,
                 steps: list[str], usage: list[str], updates: list[tuple[str, str, str]], footer: list[str]) -> str:
    """The team's ''' header block (generated scripts and enhanced ones): ``steps`` are "1. fn: what it does" lines,
    ``updates`` (date, pid, note) tuples, ``footer`` the hub's closing lines."""
    pad = " " * 20
    lines = ["'''"]

    def put(label: str, value: str) -> None:
        lines.append(f"    {label:<16}{value}".rstrip())

    put("Author(s):", _docblock(", ".join(authors)) or "TODO: Developer(s) add your pid(s)")
    put("Reviewer(s):", _docblock(", ".join(reviewers)))
    put("Created on:", created_on)
    put("JIRA/SN #:", _docblock(jira) or "TODO: JIRA# or Service Now#")
    lines.append("")
    desc = textwrap.wrap(_docblock(description), 90) or ["TODO: the program's purpose"]
    put("Description:", desc[0])
    lines += [pad + d for d in desc[1:]]
    if steps:
        lines.append("")
        lines.append(pad + "Steps (main() runs them in this order):")
        lines += [pad + f"  {_docblock(s)}" for s in steps]
    lines.append("")
    for i, line in enumerate(usage):
        put("Usage:" if i == 0 else "", line)
    lines.append("")
    if updates:
        for i, (date, pid, note) in enumerate(updates):
            put("Updates:" if i == 0 else "", _docblock(f"[{date}, {pid}]: {note}"))
    else:
        put("Updates:", "[YYYY-MM-DD, PID]:")
    lines.append("")
    lines += [f"    {_docblock(f)}" for f in footer]
    lines.append("'''")
    return "\n".join(lines)


def step_line(position: str, fn: str, doc: str, enabled: bool = True) -> str:
    """One line of the header's step list (header_block escapes it)."""
    doc = " ".join(str(doc or "").split())
    doc = doc if len(doc) <= 90 else doc[:87] + "..."
    return f"{position} {fn}: {doc}" + ("" if enabled else " [disabled]")


def build_header(spec: ProcessSpec, views: list[SimpleNamespace], *, usage: list[str], adc_email: str, today: dt.date,
                 generator: str) -> str:
    m = spec.metadata
    return header_block(
        authors=m.authors, reviewers=m.reviewers, created_on=m.created_on or today.isoformat(), jira=m.jira,
        description=_docblock(m.description) or f"{m.name} pipeline",
        steps=[step_line(v.position_label, v.fn, v.doc, v.enabled) for v in views], usage=usage,
        updates=[(u.date, u.pid, u.note) for u in m.updates],
        footer=[f"Generated by {generator} on {today.isoformat()} (spec {spec.spec_hash()}).",
                f"ADC identity at generation: {adc_email}"],
    )


IMPORT_RULES = [  # (import line, regex that proves it is used)
    ("import argparse", r"\bargparse\."),
    ("import ast", r"\bast\."),
    ("import decimal", r"\bdecimal\."),
    ("import io", r"\bio\."),
    ("import json", r"\bjson\."),
    ("import logging", r"\blogging\."),
    ("import os", r"\bos\."),
    ("import shutil", r"\bshutil\."),
    ("import smtplib", r"\bsmtplib\."),
    ("import subprocess", r"\bsubprocess\."),
    ("import sys", r"\bsys\."),
    ("import time", r"\btime\."),
    ("import uuid", r"\buuid\."),
    ("import warnings", r"\bwarnings\."),
]


def build_imports(body: str, extra: Iterable[str] = ()) -> list[str]:
    """The "# Base imports" lines: what the body uses, plus the custom steps' own import lines (``extra``): standard
    library ones with the first group, other packages after pandas, google ones after the google clients."""
    lines = [line for line, pattern in IMPORT_RULES if re.search(pattern, body)]
    dt_names = ["datetime"] + [n for n, p in (("date", r"\bdate\.today\("), ("timedelta", r"\btimedelta\(")) if re.search(p, body)]
    stdlib, packages, google = [], [], []
    for line in dict.fromkeys(" ".join(x.split()) for x in extra):
        top = line.split()[1].split(".")[0]
        (google if top == "google" else stdlib if top in sys.stdlib_module_names else packages).append(line)
    lines += [x for x in stdlib if x not in lines]
    lines.append(f"from datetime import {', '.join(dt_names)}")
    if re.search(r"\bpd\.", body):
        lines.append("import pandas as pd")
    if re.search(r"\bEmailMessage\(", body):
        lines.append("from email.message import EmailMessage")
    lines += [x for x in packages if x not in lines]
    lines.append("")
    lines.append("import google.auth")
    if re.search(r"\bexcept NotFound\b", body):
        lines.append("from google.api_core.exceptions import NotFound")
    lines.append("from google.cloud import bigquery")
    if re.search(r"\bstorage\.Client\(", body):
        lines.append("from google.cloud import storage")
    lines += [x for x in google if x not in lines]
    return lines


def _main_view(spec: ProcessSpec, views: list[SimpleNamespace]) -> SimpleNamespace:
    email_on = spec.email.enabled
    attach = {s.function_name for s in spec.steps if isinstance(s, ExportStep) and s.attach_to_email}
    collect = bool(email_on and attach and spec.email.send_on.success)
    code_args: list[str] = []
    calls = []
    for v in views:
        call_args = list(v.args)
        call = f"{v.fn}({', '.join(call_args)})"
        if collect and v.fn in attach and v.enabled:
            call = f"attachments.append({call})"
        if v.enabled:
            code_args += [a for a in call_args if a not in code_args]
        label = f"{v.position}. {v.doc}" if v.enabled else f"(disabled in the hub) {v.doc}"
        calls.append(SimpleNamespace(fn=v.fn, call=call, enabled=v.enabled, on_failure=v.on_failure,
                                     label=" ".join(label.split())))
    skip = {"config_data", "table_def", "environment"}
    lines: list[str] = []
    var_names = [a for a in code_args if a not in skip and a != "data_sets" and a in spec.variable_map()]
    projects, _datasets, others = _groups(spec, var_names)
    for n in projects + others:
        if n == GSM_PROJECT and spec.secrets:
            continue
        lines.append(f"{n} = config_data.get({n!r})")
    if "data_sets" in code_args:
        lines.append("data_sets = config_data.get('data_sets', {})")
    params = [a for a in code_args if a not in skip and a != "data_sets" and a not in spec.variable_map()]
    lines += [f"{p} = config_data.get({p!r})" for p in params]
    return SimpleNamespace(
        has_ingest=spec.ingest_step() is not None,
        extract_lines=lines,
        calls=calls,
        has_continue=any(c.enabled and c.on_failure == "continue" for c in calls),
        collect_attachments=collect,
        email_on_success=email_on and spec.email.send_on.success,
        email_on_failure=email_on and spec.email.send_on.failure,
        secrets=spec.secrets,
    )


def _mail_view(spec: ProcessSpec, settings: Settings) -> SimpleNamespace:
    email = spec.email
    attach_exports = [s.function_name for s in spec.steps if isinstance(s, ExportStep) and s.attach_to_email]
    has_attachments = bool(email.attachments or attach_exports)
    signature = _brace_escape(settings.email_signature.replace("\\", "\\\\").replace('"""', '\\"\\"\\"'))
    return SimpleNamespace(
        has_attachments=has_attachments,
        attachments_use_date=any("{date" in a for a in email.attachments),
        subject_literal=repr(email.subject),
        message_literal=repr(email.message),
        body_literal='f"""Hello Team,\n\n{message}\n\n' + signature + '"""',
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_starttls=settings.smtp_starttls,
        attach_exports=attach_exports,
    )


# ====================================================================================== docs helpers
WRITE_MODE_TEXT = {"truncate": "replace the table", "append": "append", "empty": "only if the table is empty"}


def _code(value: Any) -> str:
    return "`" + str(value).replace("`", "'") + "`"


def _codes(values: list[str]) -> str:
    return ", ".join(_code(v) for v in values) if values else "—"


def step_details(step: Any, view: SimpleNamespace, inv: TableInventory, spec: ProcessSpec) -> list[tuple[str, str]]:
    """(label, markdown) rows for the README's "Process steps in detail" section (lab values)."""
    fn, values = step.function_name, spec.variable_values("lab")
    reads = [t.resolved for t in inv.dependencies + inv.targets if fn in t.read_by]
    writes = [f"{_code(t.resolved)} ({'; '.join(t.how)})" for t in inv.targets if fn in t.written_by]
    rows: list[tuple[str, str]] = []
    if isinstance(step, IngestStep):
        rows.append(("Reads files", f"{_code(fill_variables(ingest_source_template(step), values))}; when none arrive: "
                     f"{ {'email': 'send the no-file email', 'warn': 'log a warning', 'fail': 'fail the step'}[step.on_no_files] }"))
        fmt = FORMAT_LABELS[step.file_format]
        if is_delimited(step.file_format):
            sep = "tab" if step.separator == "\t" else _code(step.separator)
            fmt += f", separator {sep}, header {_code(step.header)}, skiprows {_code(step.skiprows)}"
        elif step.file_format == "FIXED":
            fmt += (f", record length {step.record_length()} characters (column lengths in {_code(spec.schema_file())}), "
                    f"header {_code(step.header)}, skiprows {_code(step.skiprows)}")
        elif step.file_format == "XLSX":
            fmt += (f", sheet {_code(step.sheet_name) if step.sheet_name else 'the first one'}, header {_code(step.header)}, "
                    f"skiprows {_code(step.skiprows)}")
        rows.append(("Format", fmt + f"; read with pandas, typed by {_code(spec.schema_file())} ({len(step.columns)} columns)"))
        rows.append(("Writes", ", ".join(writes) if writes else "—"))
        if not step.target_enabled:
            staging = fill_variables(ingest_tables(step)["staging"], values)
            rows.append(("Raw rows", f"kept in {_code(staging)} for the next steps: the run's first file empties it and "
                         "every file of the run is added (emptied when no file arrives"
                         + (", unless the run fails" if step.on_no_files == "fail" else "") + ")"))
        elif step.load_mode == "merge":
            rows.append(("Merge", f"keys {_codes(step.merge_keys)}"
                         + (f"; newest {_code(step.merge_order_by)} wins" if step.merge_order_by else "")
                         + (f"; only rows where {_code(step.merge_filter)}" if step.merge_filter else "")))
        if step.add_audit_columns:
            rows.append(("Added columns", "`file_batch_id`, `inserted_ts`, `updated_ts`, `file_name`"))
        if step.archive:
            bucket = fill_variables(step.archive_bucket or step.read_bucket, values)
            rows.append(("Processed files", f"moved to {_code(f'gs://{bucket}/' + fill_variables(step.archive_path, values))}"))
    elif isinstance(step, SqlStep):
        rows.append(("Statement", step.statement_kind))
        rows.append(("Reads", _codes(reads)))
        rows.append(("Writes", ", ".join(writes) if writes else "—"))
        if view.params:
            rows.append(("Parameters", "; ".join(f"{_code('@' + p.name)} {p.bq_type}: {p.explain}" for p in view.params)))
    elif isinstance(step, ExportStep):
        rows.append(("Reads", _codes(reads) if step.source_kind == "query" else _code(fill_variables(step.source_table or "", values))))
        what = FORMAT_LABELS[step.file_format]
        if step.compression != "NONE":
            what += f", {step.compression.lower()} compressed"
        if is_delimited(step.file_format):
            delim = "tab" if step.field_delimiter == "\t" else _code(step.field_delimiter)
            what += f", delimiter {delim}, {'with' if step.print_header else 'without'} a header row"
        elif step.file_format == "XLSX":
            what += (f", sheet {_code(step.sheet_name or 'Sheet1')}, {'with' if step.print_header else 'without'} a header row; "
                     f"written with pandas, at most {EXCEL_MAX_ROWS - 1:,} rows")
        rows.append(("Writes files", f"{_code(fill_variables(export_uri_template(step), values))} ({what})"))
        if view.params:
            rows.append(("Parameters", "; ".join(f"{_code('@' + p.name)} {p.bq_type}: {p.explain}" for p in view.params)))
        if step.attach_to_email and spec.email.enabled:
            rows.append(("Email", "the file is attached to the success email"))
    elif isinstance(step, CopyStep):
        direction = step.resolved_direction()
        if direction:
            rows.append(("Direction", COPY_DIRECTIONS[direction]
                         + (" (local = the machine that runs the script)" if "local" in direction else "")))
        rows.append(("Command", _code(f"gcloud storage {step.operation}{view.flags_text} "
                                      + " ".join(fill_variables(s, values) for s in step.sources)
                                      + " " + fill_variables(step.destination, values))))
    elif isinstance(step, DqStep):
        errors = sum(1 for c in step.checks if c.severity == "error")
        rows.append(("Checks", f"{len(step.checks)} ({errors} stop the run when they fail, {len(step.checks) - errors} only warn); "
                     "see Data-quality checks below"))
        rows.append(("Reads", _codes(reads)))
    elif isinstance(step, CustomStep):
        author = f"written with Gemini ({step.generated_by})" if step.generated_by else "written by hand"
        rows.append(("Custom code", f"{author}" + (f" from: {' '.join(step.request.split())}" if step.request else "")))
        if step.settings:
            rows.append(("Settings", _codes([s.key for s in step.settings])))
        secrets = custom_code_reads(step)[0]
        if secrets:
            rows.append(("Secrets", _codes(secrets)))
        if step.imports:
            rows.append(("Imports", _codes(step.imports)))
        rows.append(("Review", "the hub only checks that this code compiles and passes ruff; the lab test run is what "
                     "proves it works"))
    key = step.config_section()
    rows.append(("Config", f"{_code(f'gcp_projects.<environment>.{key}')}" if key else "variables only (see Environments)"))
    rows.append(("On failure", "logged; the run continues with the next step and ends as failed"
                 if step.on_failure == "continue" else "the run stops (later steps do not run)"))
    return rows


# ====================================================================================== render
def run_command(stem: str, schema_file: str | None, env: str) -> str:
    """The command that runs the script, as the team writes it: the environment comes last."""
    ts = f" -ts ./{schema_file}" if schema_file else ""
    return f"py ./{stem}.py -cf ./{stem}.yaml{ts} {env}"


def usage_lines(stem: str, schema_file: str | None, envs: list[str]) -> list[str]:
    """The header's Usage lines: one command per environment, aligned, with what the environment is."""
    width = max(len(run_command(stem, schema_file, env)) for env in envs)
    return [f"{run_command(stem, schema_file, env):<{width}}  ({ENV_LABELS[env].replace(' (', ' = ').rstrip(')')})"
            for env in envs]


def logs_sql(spec: ProcessSpec) -> bool:
    """Does any step send SQL to BigQuery (and so capture it with sql_logger)? Copy-only scripts do not."""
    return any(isinstance(s, (SqlStep, IngestStep, DqStep)) or (isinstance(s, ExportStep) and s.source_kind == "query")
               or (isinstance(s, CustomStep) and uses_name(s.code, "capture")) for s in spec.steps)


def custom_steps(spec: ProcessSpec) -> list[CustomStep]:
    return [s for s in spec.steps if isinstance(s, CustomStep)]


def custom_requirements(spec: ProcessSpec) -> list[str]:
    """requirements.txt lines the custom steps add: theirs, else the hub's pip name for each imported package."""
    from .script_rewrite import REQUIREMENTS

    out: list[str] = []
    for step in custom_steps(spec):
        named = {re.split(r"[\[<>=!~ ]", r, maxsplit=1)[0].lower().replace("_", "-") for r in step.requirements}
        out += step.requirements
        for line in step.imports:
            module = line.split()[1]
            top = module.split(".")[0]
            if top in sys.stdlib_module_names or top in ("google", "modules"):
                continue
            requirement = next((r for name, r in REQUIREMENTS.items() if module == name or module.startswith(name + ".")
                                or top == name), None) or top
            if re.split(r"[\[<>=!~ ]", requirement, maxsplit=1)[0].lower().replace("_", "-") not in named:
                out.append(requirement)
    return list(dict.fromkeys(out))


def render_process(
    spec: ProcessSpec,
    *,
    generated_by: str = "",
    ai_overview: str = "",
    adc_email: str = "",
    smoke_run: bool = True,
    today: dt.date | None = None,
    settings: Settings | None = None,
    untested_reason: str = "",
) -> RenderResult:
    """``untested_reason``: why the bundle was not tested in lab (shown at the top of the README)."""
    settings = settings or get_settings()
    today = today or dt.date.today()
    warnings: list[str] = []
    views = build_step_views(spec, warnings)
    enabled_views = [v for v in views if v.enabled]
    if not enabled_views:
        raise RenderError(["at least one enabled step is required"])
    process = spec.metadata.name
    stem = spec.script_stem()
    sender = adc_email if adc_email and "@" in adc_email else "TODO: sender email"
    config = build_config_dict(spec, sender=sender)
    ingest = spec.ingest_step()
    schema_file = spec.schema_file() if ingest else None
    has_email = spec.email.enabled
    mail = _mail_view(spec, settings)
    xlsx_exports = any(isinstance(s, ExportStep) and s.file_format == "XLSX" for s in spec.steps)
    file_summary = has_email and spec.email.file_summary and any(isinstance(s, (IngestStep, ExportStep)) for s in spec.steps)
    customs = custom_steps(spec)
    uses_gcs = (bool(ingest) or bool(has_email and mail.has_attachments) or xlsx_exports
                or any(uses_name(s.code, "storage_client") for s in customs))
    envs = spec.active_envs()
    width = max(len(run_command(stem, schema_file, env)) for env in envs)
    usage = usage_lines(stem, schema_file, envs)
    sql_logger = settings.sql_logger_enabled and logs_sql(spec)
    ingest_view = next((v for v in views if v.kind == "ingest"), None)
    context = {
        "process": process,
        "script": stem,
        "steps": views,
        "ingest": ingest_view,
        "has_secrets": bool(spec.secrets),
        "has_email": has_email,
        "uses_gcs": uses_gcs,
        "sql_logger": sql_logger,
        "module_paths": settings.module_paths,
        "location": settings.bq_location,
        "mail": mail,
        "main": _main_view(spec, views),
        "cli": SimpleNamespace(
            description="Retrieve process configuration.",
            environments=repr(list(settings.environments)),
            env_help=f"Environment ({', '.join(settings.environments)})",
            config_file=f"{stem}.yaml",
            schema_file=schema_file,
        ),
        "smoke": False,
        "file_summary": file_summary,
    }
    env = jinja_env()
    body = env.get_template("body.py.j2").render(**context)
    header = build_header(spec, views, usage=usage, adc_email=adc_email or "not recorded", today=today, generator=GENERATOR)
    files: dict[str, str] = {}
    custom_imports = [line for s in customs for line in s.imports]
    files[f"{stem}.py"] = env.get_template("script.py.j2").render(header=header, imports=build_imports(body, custom_imports),
                                                                  body=body)
    smoke_files: dict[str, str] = {}
    if any(s.enabled for s in customs):  # offline, custom code is replaced by a log line (the lab test run executes it)
        smoke_body = env.get_template("body.py.j2").render(**{**context, "smoke": True})
        smoke_files[f"{stem}.py"] = env.get_template("script.py.j2").render(header=header, imports=build_imports(smoke_body),
                                                                            body=smoke_body)

    yaml_header = [
        f"{process}: configuration for {stem}.py",
        f"Run:  {run_command(stem, schema_file, '<environment>')}   (environment: " + " | ".join(envs) + ")",
        f"Generated by {GENERATOR} on {today.isoformat()} (spec {spec.spec_hash()}).",
        f"ADC identity at generation: {adc_email or 'not recorded'}",
    ]
    files[f"{stem}.yaml"] = render_config_yaml(config, yaml_header)
    if ingest:
        files[schema_file] = render_table_schema(ingest, [f"Table definition for {stem}.py (-ts {schema_file})", ""])
    files["requirements.txt"] = env.get_template("requirements.txt.j2").render(
        script=stem, uses_gcs=uses_gcs, has_secrets=bool(spec.secrets), uses_pandas=bool(ingest) or xlsx_exports,
        uses_excel=xlsx_exports or bool(ingest and ingest.file_format == "XLSX"), extra=custom_requirements(spec))

    inventory = process_tables(spec)
    for view, step in zip(views, spec.steps):
        view.details = step_details(step, view, inventory, spec) if step.enabled else []
    used_in = _variable_usage(spec)
    files["README.md"] = env.get_template("README.md.j2").render(
        process=process,
        script=stem,
        description=spec.metadata.description or f"{process} pipeline",
        ai_overview=ai_overview,
        untested_reason=" ".join(untested_reason.split()),
        authors=", ".join(spec.metadata.authors),
        reviewers=", ".join(spec.metadata.reviewers),
        jira=spec.metadata.jira,
        owner=generated_by or spec.metadata.owner,
        co_owners=spec.metadata.co_owners,
        team=spec.metadata.team,
        schema_file=schema_file,
        generator=GENERATOR,
        generated_on=today.isoformat(),
        spec_hash=spec.spec_hash(),
        adc_email=adc_email or "not recorded",
        sql_logger=sql_logger,
        has_secrets=bool(spec.secrets),
        secrets=spec.secrets,
        has_email=has_email,
        has_attachments=mail.has_attachments,
        file_summary=file_summary,
        attach_exports=mail.attach_exports,
        email_when=" and ".join(w for w, on in (("when the run succeeds", spec.email.send_on.success),
                                                 ("when a step fails", spec.email.send_on.failure)) if on),
        has_copy=any(isinstance(s, CopyStep) for s in spec.steps),
        module_paths=settings.module_paths,
        envs=envs,
        env_labels=ENV_LABELS,
        run_lines=[f"{run_command(stem, schema_file, env):<{width}}    # {RUN_COMMENTS.get(env, env)}" for env in envs],
        variables=[SimpleNamespace(name=v.name, group=v.group, values=v.values, used=used_in.get(v.name, "—"))
                   for v in spec.variables],
        steps=views,
        tables=inventory,
        dq_checks=[SimpleNamespace(step=v.fn, name=c.name, op=c.op, threshold=c.threshold, severity=c.severity)
                   for v in views if v.kind == "dq" for c in v.checks],
    )
    result = RenderResult(process=process, files=files, script=stem, warnings=warnings, schema_file=schema_file,
                          smoke_files=smoke_files)
    _post_checks(result, spec, config, smoke_run=smoke_run, settings=settings)
    failed = [c for c in result.checks if not c.ok]
    if failed:
        raise RenderError([f"{c.name}: {c.detail}" for c in failed], result)
    return result


def _variable_usage(spec: ProcessSpec) -> dict[str, str]:
    code = set(spec.code_variables())
    out: dict[str, list[str]] = {}
    for step in spec.steps:
        for _field, value, _allow in step.template_values():
            for n in _names(value):
                out.setdefault(n, []).append(step.function_name)
        for sql_text, _where in _sql_texts_of(step):
            for n in sql_var_tokens(sql_text):
                out.setdefault(n, []).append(step.function_name)
    text = {n: ", ".join(f"`{s}`" for s in dict.fromkeys(fns)) for n, fns in out.items()}
    for n in code:
        text[n] = text.get(n, "—") + " (SQL)"
    return text


def _sql_texts_of(step: Any) -> list[tuple[str, str]]:
    from ..models.spec import _sql_texts

    return _sql_texts(step)


def _post_checks(result: RenderResult, spec: ProcessSpec, config: dict[str, Any], *, smoke_run: bool,
                 settings: Settings) -> None:
    stem = result.script
    py_files = [p for p in result.files if p.endswith(".py")]
    for path in py_files:
        try:
            result.files[path] = format_python(result.files[path])
            ast.parse(result.files[path])
            result.checks.append(Check(f"python syntax ({path})", True))
        except Exception as exc:  # noqa: BLE001
            result.checks.append(Check(f"python syntax ({path})", False, str(exc).split("\n")[0][:500]))
    result.checks.append(_sql_round_trip_check(result.files.get(f"{stem}.py", ""), spec))
    try:
        loaded = yaml.safe_load(result.files[f"{stem}.yaml"])
        ok = loaded == config
        result.checks.append(Check("config YAML round-trip", ok, "" if ok else "rendered YAML differs from the spec"))
        if result.schema_file:
            ingest = spec.ingest_step()
            loaded_schema = yaml.safe_load(result.files[result.schema_file])
            ok = ingest is not None and loaded_schema == table_schema_dict(ingest)
            result.checks.append(Check("table schema YAML round-trip", ok, "" if ok else "rendered table schema differs"))
    except yaml.YAMLError as exc:
        result.checks.append(Check("config YAML round-trip", False, str(exc)[:500]))
    if all(c.ok for c in result.checks):
        result.checks.append(_ruff_check(result.files, py_files))
    if smoke_run and all(c.ok for c in result.checks):
        from .smoke_run import run_smoke

        result.checks.append(run_smoke(result, spec, settings=settings))


def format_python(source: str) -> str:
    """black, except for a leading ''' header block, which keeps the team's indented layout."""
    if source.startswith("'''"):
        end = source.find("'''", 3)
        if end != -1:
            head, rest = source[: end + 3], source[end + 3 :]
            return head + "\n\n" + black.format_str(rest.lstrip("\n"), mode=BLACK_MODE)
    return black.format_str(source, mode=BLACK_MODE)


def _sql_round_trip_check(script: str, spec: ProcessSpec) -> Check:
    """Every f-string assigned to `sql` in a SQL/export step must evaluate back to the approved SQL."""
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return Check("embedded SQL round-trip", False, str(exc))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    values = spec.variable_values("lab")
    checked = 0
    for step in spec.steps:
        sql = step.sql if isinstance(step, SqlStep) else (
            step.source_sql if isinstance(step, ExportStep) and step.source_kind == "query" else None
        )
        if sql is None:
            continue
        fn = funcs.get(step.function_name)
        node = next(
            (
                n.value
                for n in ast.walk(fn)
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "sql" for t in n.targets)
            ),
            None,
        ) if fn else None
        if node is None:
            return Check("embedded SQL round-trip", False, f"{step.function_name}: SQL assignment not found")
        try:
            verify_embedded_sql(ast.unparse(node), sql, values, SQL_INDENT)
        except EmbedError as exc:
            # black may re-wrap implicit concatenations; compare semantically as a fallback
            if not _semantic_match(node, sql, values):
                return Check("embedded SQL round-trip", False, f"{step.function_name}: {exc}")
        checked += 1
    return Check("embedded SQL round-trip", True, f"{checked} SQL block(s) verified")


def _semantic_match(node: ast.AST, sql: str, values: dict[str, str]) -> bool:
    from .filters import _static_eval

    try:
        got = _static_eval(node, values)
    except EmbedError:
        return False
    return " ".join(got.split()) == " ".join(substitute_sql_vars(sql, values).split())


def _ruff_check(files: dict[str, str], py_files: list[str]) -> Check:
    problems = []
    for path in py_files:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", "--select", "E9,F", "--output-format", "concise",
             "--stdin-filename", path, "-"],
            input=files[path],
            capture_output=True,
            encoding="utf-8",  # not the locale codepage: generated code contains non-ASCII (Windows: cp1252)
            errors="replace",
            timeout=60,
        )
        if proc.returncode not in (0, 1):
            return Check("ruff lint", False, (proc.stderr or proc.stdout)[:500])
        if proc.returncode == 1:
            problems.append(proc.stdout.strip())
    return Check("ruff lint", not problems, "\n".join(problems)[:2000])


def write_bundle(files: dict[str, str], root: Path) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# ====================================================================================== main() preview
def render_main_preview(steps: list[dict[str, Any]], variables: list[dict[str, Any]] | None = None,
                        email_enabled: bool = False) -> str:
    """Render just `def main()` for the wizard's live preview (accepts partial step dicts)."""
    groups = {v.get("name"): v.get("group", "other") for v in variables or [] if isinstance(v, dict)}
    calls, position = [], 0
    for raw in steps:
        enabled = bool(raw.get("enabled", True))
        if enabled:
            position += 1
        fn = raw.get("function_name") or "unnamed_step"
        kind = raw.get("type", "sql")
        if kind in ("sql", "dq"):
            sql = raw.get("sql") or " ".join((c.get("sql") or "") + (c.get("table") or "") for c in raw.get("checks") or [])
            names = list(dict.fromkeys(sql_var_tokens(sql) + re.findall(r"\{([a-z_][a-z0-9_]*)\}", sql)))
            projects = [n for n in names if groups.get(n, "project" if n.endswith("_project") else "") == "project"]
            datasets = [n for n in names if groups.get(n, "dataset" if n.endswith("_dataset") else "") == "dataset"]
            others = [n for n in names if n not in projects and n not in datasets]
            args = projects + (["data_sets"] if datasets else []) + others
        elif kind == "ingest":
            args = ["config_data", "table_def"]
        else:
            args = ["config_data"]
        doc = " ".join((raw.get("description") or kind).split())
        calls.append(SimpleNamespace(fn=fn, call=f"{fn}({', '.join(args)})", enabled=enabled,
                                     on_failure=raw.get("on_failure", "stop"),
                                     label=f"{position}. {doc}" if enabled else f"(disabled in the hub) {doc}"))
    main = SimpleNamespace(
        has_ingest=any(r.get("type") == "ingest" for r in steps),
        extract_lines=["read_project = config_data.get('read_project')", "write_project = config_data.get('write_project')"],
        calls=calls,
        has_continue=any(c.enabled and c.on_failure == "continue" for c in calls),
        collect_attachments=False,
        email_on_success=email_enabled,
        email_on_failure=email_enabled,
        secrets=[],
    )
    src = jinja_env().get_template("partials/_main.py.j2").render(main=main, sql_logger=get_settings().sql_logger_enabled)
    try:
        return black.format_str(src, mode=BLACK_MODE)
    except Exception:  # noqa: BLE001 - invalid names while the user is typing
        return src


def render_custom_preview(step: dict[str, Any], code: str | None = None, *, breakout: bool | None = None) -> str:
    """A custom step's function as the script will hold it (the wizard's preview; Gemini's picture of the wrapper).
    ``breakout``: show the settings line even when the code does not read it (yet)."""
    fn = step.get("function_name") or "run_custom_process"
    key = step.get("config_key") or "custom_config"
    body = textwrap.dedent(code if code is not None else (step.get("code") or "")).strip("\n") or "pass  # your code"
    uses_config = uses_name(body, key) if breakout is None else breakout
    v = SimpleNamespace(fn=fn, doc=step.get("description") or fn, config_key=key, uses_config=uses_config,
                        code_block=textwrap.indent(body, " " * 8), log_begin=_log_begin(fn), log_end=_log_end(fn))
    src = jinja_env().get_template("partials/_step_custom.py.j2").render(v=v, smoke=False)
    try:
        return black.format_str(src, mode=BLACK_MODE)
    except Exception:  # noqa: BLE001 - code that does not parse yet is shown as typed
        return src


def template_tokens(template: str) -> list[str]:
    try:
        return [name for name, _spec in template_fields(template)]
    except ValueError:
        return []
