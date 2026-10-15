"""'Validate all': everything we can check before a user downloads the bundle — in the **lab** environment only.

The hub acts as if it had no access to prod: every name below is resolved with the lab (dev) values, and the value
(prod) values are never used for a client call.

Local checks (always): step order vs table dependencies, parameters, SQL parsing, a full render.
GCP checks (read-only, the hub's ADC identity): BigQuery dry-runs, dataset/table existence and schema
compatibility, GCS source files + schema-vs-sample, bucket access, the sql_logger table, secret metadata.
Nothing is ever written to BigQuery or GCS here (the lab test run does that, on request).
"""

from __future__ import annotations

import datetime as dt
import shutil
import sys
import zlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models.rules import fill_variables, parse_receivers, split_gs_uri, substitute_sql_vars
from ..models.spec import (
    ENV_LABELS,
    EXCEL_MAX_ROWS,
    FILE_EXTENSIONS,
    FORMAT_LABELS,
    GSM_PROJECT,
    INGEST_EXTENSIONS,
    CopyStep,
    CustomStep,
    DqStep,
    ExportStep,
    IngestStep,
    Parameter,
    ProcessSpec,
    SqlStep,
    custom_code_reads,
    is_delimited,
)
from ..settings import TEST_RUN_ENVIRONMENT, Settings, get_settings
from . import sql_utils
from .lineage import ingest_tables, step_io
from .lineage import norm_table as _norm_table
from .renderer import RenderError, dq_check_sql, render_process
from .schema_infer import InferResult, compare_with_sample, infer_from_bytes, pandas_rows, preview_fixed_width

Level = Literal["ok", "info", "warn", "error"]
ENV = TEST_RUN_ENVIRONMENT  # every check uses the lab values
AUDIT_COLUMNS = ("file_batch_id", "inserted_ts", "updated_ts", "file_name")


class CheckItem(BaseModel):
    id: str
    level: Level
    title: str
    detail: str = ""
    step: str | None = None
    section: Literal["basics", "variables", "pipeline", "step", "operations", "generate"] = "step"
    fix: dict[str, Any] | None = None


class ValidationReport(BaseModel):
    ok: bool
    environment: str = ENV
    checked_on: str = ""
    counts: dict[str, int]
    items: list[CheckItem] = Field(default_factory=list)
    gcp_checks: bool = True


# ====================================================================== GCP client access
class ClientFactory:
    """Creates (and caches per project) BigQuery/GCS clients as the app's ADC identity. Tests pass fakes."""

    def __init__(self, bq: Any = None, gcs: Any = None, secrets: Any = None):
        self._fixed_bq, self._fixed_gcs, self._fixed_secrets = bq, gcs, secrets
        self._bq: dict[str, Any] = {}
        self._gcs: dict[str, Any] = {}
        self._secrets: Any = None

    def bigquery(self, project: str, location: str | None = None) -> Any:
        if self._fixed_bq is not None:
            return self._fixed_bq
        if project not in self._bq:
            from google.cloud import bigquery

            self._bq[project] = bigquery.Client(project=project)
        return self._bq[project]

    def storage(self, project: str) -> Any:
        if self._fixed_gcs is not None:
            return self._fixed_gcs
        if project not in self._gcs:
            from google.cloud import storage

            self._gcs[project] = storage.Client(project=project)
        return self._gcs[project]

    def secretmanager(self) -> Any:
        if self._fixed_secrets is not None:
            return self._fixed_secrets
        if self._secrets is None:
            from google.cloud import secretmanager

            self._secrets = secretmanager.SecretManagerServiceClient()
        return self._secrets


def secret_version_info(clients: ClientFactory, *, project: str, secret_id: str, version: str = "latest") -> dict[str, Any]:
    """Metadata of a secret version, never its value (needs secretmanager.versions.get: Secret Manager Viewer)."""
    name = f"projects/{project}/secrets/{secret_id}/versions/{version}"
    try:
        v = clients.secretmanager().get_secret_version(request={"name": name})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": name, "error": gcp_error_text(exc)}
    state = getattr(v.state, "name", str(v.state))
    created = getattr(v, "create_time", None)
    return {
        "ok": state == "ENABLED",
        "name": v.name,
        "version": v.name.rsplit("/", 1)[-1],
        "state": state,
        "created": created.isoformat()[:19] if created else None,
    }


def gcp_error_text(exc: Exception) -> str:
    name = type(exc).__name__
    msg = getattr(exc, "message", None) or str(exc)
    msg = msg.split("\n")[0][:400]
    if name == "Forbidden":
        return f"the hub's identity (ADC) is not allowed to read this ({msg}); ask for read access in lab or verify manually"
    return msg


# ====================================================================== sample values
def sample_param_value(p: Parameter, today: dt.date, process: str) -> Any:
    d = today
    if p.source == "today":
        value: Any = d
    elif p.source == "today_offset":
        value = d + dt.timedelta(days=p.offset_days)
    elif p.source == "month_start":
        value = d.replace(day=1)
    elif p.source == "month_end":
        value = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    elif p.source == "prev_month_start":
        value = (d.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    elif p.source == "prev_month_end":
        value = d.replace(day=1) - dt.timedelta(days=1)
    elif p.source == "year_start":
        value = d.replace(month=1, day=1)
    elif p.source == "process_name":
        value = process
    else:
        value = p.default
    return _coerce(p.bq_type, value)


def _coerce(bq_type: str, value: Any) -> Any:
    import decimal

    if value is None:
        return None
    if bq_type == "DATE":
        return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))
    if bq_type == "TIMESTAMP":
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return dt.datetime.combine(value, dt.time.min, tzinfo=dt.timezone.utc)
        return value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if bq_type == "DATETIME":
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return dt.datetime.combine(value, dt.time.min)
        return value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if bq_type == "INT64":
        return int(value)
    if bq_type == "FLOAT64":
        return float(value)
    if bq_type == "NUMERIC":
        return decimal.Decimal(str(value))
    if bq_type == "BOOL":
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "y")
    return value.isoformat() if isinstance(value, dt.date) else str(value)


# ====================================================================== dependency / order checks
def order_checks(spec: ProcessSpec) -> list[CheckItem]:
    ios = step_io(spec, ENV)
    items = []
    for io in ios:
        for table in sorted(io.reads):
            writers = [w for w in ios if table in w.writes and w.fn != io.fn]
            if writers and all(w.index > io.index for w in writers):
                first = min(writers, key=lambda w: w.index)
                items.append(
                    CheckItem(
                        id=f"order:{io.fn}:{table}",
                        level="warn",
                        section="pipeline",
                        step=io.fn,
                        title=f"Step {io.index + 1} `{io.fn}` reads {table}, which step {first.index + 1} `{first.fn}` builds later",
                        detail="As ordered, this step reads the previous run's (or no) data. Move it after the step that builds the table.",
                        fix={"action": "move_after", "step": io.fn, "after": first.fn},
                    )
                )
    if not items:
        items.append(CheckItem(id="order:ok", level="ok", section="pipeline", title="Step order is consistent with table dependencies"))
    return items


def prod_collisions(spec: ProcessSpec) -> list[str]:
    """Places where lab would write to the same project/bucket/folder as value or qa (a test run must refuse)."""
    lab = spec.variable_values(ENV)
    problems: list[str] = []
    for env in spec.active_envs():
        if env == ENV:
            continue
        other = spec.variable_values(env)
        if lab.get("write_project") and lab.get("write_project") == other.get("write_project"):
            problems.append(f"write_project is {lab['write_project']!r} in both lab and {env}")
        for step in spec.steps:
            for label, template in _write_locations(step):
                a, b = fill_variables(template, lab), fill_variables(template, other)
                if a and a == b:
                    problems.append(f"{step.function_name}: the {label} is {a!r} in both lab and {env}")
    return problems


def _write_locations(step: Any) -> list[tuple[str, str]]:
    if isinstance(step, ExportStep):
        return [("export bucket + folder", f"{step.bucket}/{step.path}")]
    if isinstance(step, IngestStep):
        out = [("staging dataset", step.staging_dataset)]
        if step.uses_target_dataset():
            out.append(("target dataset", step.target_dataset))
        if step.archive:
            out.append(("archive bucket + folder", f"{step.archive_bucket or step.read_bucket}/{step.archive_path}"))
        return [(label, "{write_project}." + t if "dataset" in label else t) for label, t in out]
    if isinstance(step, CopyStep):
        return [("copy destination", step.destination)]
    return []


# ====================================================================== validator
class SpecValidator:
    def __init__(self, clients: ClientFactory | None = None, *, sample_bytes: int = 2 * 1024 * 1024,
                 settings: Settings | None = None, adc_project: str | None = None, adc_email: str | None = None):
        self.clients = clients or ClientFactory()
        self.sample_bytes = sample_bytes
        self.settings = settings or get_settings()
        self.adc_project = adc_project
        self.adc_email = adc_email

    def validate(self, spec: ProcessSpec, *, gcp_checks: bool = True) -> ValidationReport:
        today = dt.date.today()
        items: list[CheckItem] = [CheckItem(
            id="env:lab-only", level="info", section="basics",
            title="Checked in lab only: the value (prod) environment is never read, dry-run or run by the hub",
            detail="The value settings are written into the YAML exactly as entered; review them before deploying.",
        )]
        for p in prod_collisions(spec):
            items.append(CheckItem(id=f"env:collision:{abs(hash(p)) % 10**8}", level="warn", section="variables",
                                   title=f"Lab and prod share a write location: {p}",
                                   detail="A lab test run would write where prod writes; the hub refuses to test-run until they differ."))
        items += order_checks(spec)
        writes_before = self._writes_before(spec)
        for step in spec.enabled_steps():
            fn = step.function_name
            try:
                if isinstance(step, SqlStep):
                    items += self._sql_step(spec, step, today, gcp_checks, writes_before[fn])
                elif isinstance(step, IngestStep):
                    items += self._ingest_step(spec, step, gcp_checks)
                elif isinstance(step, ExportStep):
                    items += self._export_step(spec, step, today, gcp_checks, writes_before[fn])
                elif isinstance(step, CopyStep):
                    items += self._copy_step(spec, step, gcp_checks)
                elif isinstance(step, DqStep):
                    items += self._dq_step(spec, step, today, gcp_checks, writes_before[fn])
                elif isinstance(step, CustomStep):
                    items += self._custom_step(spec, step)
            except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
                items.append(CheckItem(id=f"{fn}:internal", level="warn", step=fn, title="Could not run every check for this step", detail=str(exc)[:300]))
        items += self._secrets(spec, gcp_checks)
        items += self._email(spec, gcp_checks)
        items += self._sql_logger(spec, gcp_checks)
        items += self._render(spec)
        counts = {lvl: sum(1 for i in items if i.level == lvl) for lvl in ("ok", "info", "warn", "error")}
        return ValidationReport(ok=counts["error"] == 0, checked_on=today.isoformat(), counts=counts, items=items,
                                gcp_checks=gcp_checks)

    # ------------------------------------------------------------------ helpers
    def _values(self, spec: ProcessSpec) -> dict[str, str]:
        return spec.variable_values(ENV)

    def _project(self, spec: ProcessSpec) -> str:
        """Project the hub's read-only jobs run in: lab write_project (dry runs cost nothing)."""
        return self._values(spec).get("write_project") or self.settings.gcp_project or "unknown-project"

    def _writes_before(self, spec: ProcessSpec) -> dict[str, set[str]]:
        seen: set[str] = set()
        out: dict[str, set[str]] = {}
        for io in step_io(spec, ENV):
            out[io.fn] = set(seen)
            seen |= io.writes
        return out

    def _bq(self, spec: ProcessSpec) -> Any:
        return self.clients.bigquery(self._project(spec), self.settings.bq_location)

    def _gcs(self, spec: ProcessSpec) -> Any:
        return self.clients.storage(self._project(spec))

    def _dry_run(self, spec: ProcessSpec, sql: str, params: list[Parameter], today: dt.date) -> Any:
        from google.cloud import bigquery

        qp = [bigquery.ScalarQueryParameter(p.name, p.bq_type, sample_param_value(p, today, spec.metadata.name)) for p in params]
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=qp)
        return self._bq(spec).query(sql, job_config=config, location=self.settings.bq_location)

    def _dry_run_items(
        self, spec: ProcessSpec, fn: str, label: str, sql: str, params: list[Parameter], today: dt.date,
        built_earlier: set[str], *, is_script: bool = False,
    ) -> tuple[list[CheckItem], Any]:
        try:
            job = self._dry_run(spec, sql, params, today)
        except Exception as exc:  # noqa: BLE001
            text = gcp_error_text(exc)
            missing_built = [t for t in built_earlier if t.split(".")[-1] in text.lower()]
            if "not found" in text.lower() and missing_built:
                return [CheckItem(id=f"{fn}:{label}:dryrun", level="info", step=fn,
                                  title=f"{label}: dry run skipped — it reads {missing_built[0]}, created by an earlier step",
                                  detail=text)], None
            if is_script and ("script" in text.lower() or "dry run" in text.lower()):
                return [CheckItem(id=f"{fn}:{label}:dryrun", level="warn", step=fn,
                                  title=f"{label}: BigQuery could not dry-run this multi-statement script", detail=text)], None
            return [CheckItem(id=f"{fn}:{label}:dryrun", level="error", step=fn, title=f"{label}: BigQuery dry run failed (lab)", detail=text)], None
        size = job.total_bytes_processed or 0
        return [CheckItem(id=f"{fn}:{label}:dryrun", level="ok", step=fn,
                          title=f"{label}: dry run OK in lab — will process about {_human(size)}")], job

    def _bucket_access(self, spec: ProcessSpec, fn: str, label: str, bucket: str, *, section: str = "step") -> list[CheckItem]:
        try:
            list(self._gcs(spec).list_blobs(bucket, max_results=1))
            return [CheckItem(id=f"{fn}:{label}:bucket", level="ok", step=fn if section == "step" else None, section=section,
                              title=f"{label.capitalize()} bucket gs://{bucket} is reachable")]
        except Exception as exc:  # noqa: BLE001
            return [CheckItem(id=f"{fn}:{label}:bucket", level="warn", step=fn if section == "step" else None, section=section,
                              title=f"Could not access the {label} bucket gs://{bucket}", detail=gcp_error_text(exc))]

    def _dataset_exists(self, spec: ProcessSpec, fn: str, dataset: str, label: str) -> CheckItem | None:
        project = self._values(spec).get("write_project", "")
        try:
            self._bq(spec).get_dataset(f"{project}.{dataset}")
            return None
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "NotFound":
                return CheckItem(id=f"{fn}:dataset:{dataset}", level="error", step=fn,
                                 title=f"The {label} dataset {project}.{dataset} does not exist in lab",
                                 detail="Loads create tables but not datasets: create it first (or fix the value).")
            return CheckItem(id=f"{fn}:dataset:{dataset}", level="warn", step=fn,
                             title=f"Could not check the {label} dataset {project}.{dataset}", detail=gcp_error_text(exc))

    # ------------------------------------------------------------------ SQL
    def _sql_step(self, spec, step: SqlStep, today, gcp_checks, built_earlier) -> list[CheckItem]:
        fn = step.function_name
        items: list[CheckItem] = []
        concrete = substitute_sql_vars(step.sql, self._values(spec))
        facts = sql_utils.facts(concrete)
        if facts.parse_error:
            items.append(CheckItem(id=f"{fn}:parse", level="warn", step=fn, title="SQL could not be parsed locally (BigQuery's dry run is authoritative)", detail=facts.parse_error))
        used = facts.params
        defined = {p.name for p in step.parameters}
        for name in used:
            if name not in defined:
                items.append(CheckItem(id=f"{fn}:param:{name}", level="error", step=fn, title=f"@{name} is used in the SQL but not defined as a parameter"))
        for p in step.parameters:
            if p.name not in used:
                items.append(CheckItem(id=f"{fn}:param:{p.name}", level="info", step=fn, title=f"Parameter @{p.name} is not used in the SQL and will be left out"))
        for sev, msg in facts.risks:
            items.append(CheckItem(id=f"{fn}:risk:{abs(hash(msg)) % 10**8}", level="warn" if sev in ("warning", "error") else "info", step=fn, title=msg))
        if step.statement_kind == "SELECT" and not step.destination_table:
            items.append(CheckItem(id=f"{fn}:dest", level="warn", step=fn, title="This SELECT has no destination table — its results will be discarded"))
        if gcp_checks and not any(i.level == "error" for i in items):
            dry, _job = self._dry_run_items(spec, fn, "SQL", concrete, [p for p in step.parameters if p.name in used], today,
                                             built_earlier, is_script=step.statement_kind == "SCRIPT")
            items += dry
        return items

    # ------------------------------------------------------------------ ingest
    def _ingest_step(self, spec, step: IngestStep, gcp_checks) -> list[CheckItem]:
        fn = step.function_name
        values = self._values(spec)
        items: list[CheckItem] = []

        def res(t: str) -> str:
            return fill_variables(t, values)

        bucket, folder, name = res(step.read_bucket), res(step.read_path).strip("/"), res(step.filename)
        archive_bucket, archive_path = res(step.archive_bucket or step.read_bucket), res(step.archive_path).strip("/")
        if not step.archive:
            items.append(CheckItem(id=f"{fn}:archive", level="warn", step=fn,
                                   title="Archiving is off: processed files stay in place and are loaded again on the next run"))
        if not step.target_enabled:
            staging = res(ingest_tables(step)["staging"])
            items.append(CheckItem(id=f"{fn}:raw", level="info", step=fn,
                                   title=f"No target table: the raw rows stay in {staging} (this run's files) for the next steps",
                                   detail="The run's first file empties the staging table and every file of the run is added; "
                                          "when no file arrives it is emptied."))
        elif step.load_mode == "merge" and not step.merge_order_by:
            items.append(CheckItem(id=f"{fn}:dedupe", level="info", step=fn,
                                   title="No 'newest row wins' column: a key that appears twice in one file makes the MERGE fail"))
        if not gcp_checks:
            return items
        exts = set(INGEST_EXTENSIONS[step.file_format])
        try:
            blobs = [b for b in self._gcs(spec).list_blobs(bucket, prefix=folder or None, max_results=2000)
                     if name in b.name.rsplit("/", 1)[-1] and any(b.name.lower().endswith(e) for e in exts)
                     and not (step.archive and archive_bucket == bucket and archive_path and b.name.startswith(archive_path + "/"))]
        except Exception as exc:  # noqa: BLE001
            items.append(CheckItem(id=f"{fn}:gcs", level="warn", step=fn, title=f"Could not list gs://{bucket}/{folder}", detail=gcp_error_text(exc)))
            blobs = None
        if blobs is not None:
            where = f"gs://{bucket}/{folder + '/' if folder else ''}"
            if not blobs:
                items.append(CheckItem(id=f"{fn}:files", level="info", step=fn,
                                       title=f"No file named '*{name}*{'/'.join(sorted(exts))}' in {where} right now (lab)",
                                       detail=f"A run without a file will {'email the team' if step.on_no_files == 'email' else 'log a warning' if step.on_no_files == 'warn' else 'fail'}."))
            else:
                latest = max(blobs, key=lambda b: (b.updated or dt.datetime.min.replace(tzinfo=dt.timezone.utc), b.name))
                items.append(CheckItem(id=f"{fn}:files", level="ok", step=fn,
                                       title=f"{len(blobs)} file(s) would be loaded in lab from {where}; latest {latest.name}"))
                items += self._schema_vs_sample(step, latest)
        datasets = [("staging", res(step.staging_dataset))]
        if step.uses_target_dataset():
            datasets.append(("target", res(step.target_dataset)))
        for label, dataset in datasets:
            problem = self._dataset_exists(spec, fn, dataset, label)
            if problem:
                items.append(problem)
        if step.target_enabled:
            items += self._target_checks(spec, step)
        if step.archive:
            items += self._bucket_access(spec, fn, "archive", archive_bucket)
        return items

    def _schema_vs_sample(self, step: IngestStep, blob: Any) -> list[CheckItem]:
        fn = step.function_name
        try:
            full = step.file_format in ("PARQUET", "XLSX")  # binary formats are read whole
            if full and (blob.size or 0) > 50 * 1024 * 1024:
                return [CheckItem(id=f"{fn}:sample", level="info", step=fn, title="Sample file too large to compare its schema (> 50 MB)")]
            data = blob.download_as_bytes() if full else blob.download_as_bytes(start=0, end=self.sample_bytes - 1)
            truncated = not full and (blob.size or 0) > len(data)
            if blob.name.endswith(".gz"):
                data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, 8 * self.sample_bytes)
                truncated = True
            if step.file_format == "FIXED":
                return self._layout_vs_sample(step, blob, data, truncated)
            excel = step.file_format == "XLSX"
            sample: InferResult = infer_from_bytes(
                data, step.file_format, filename=blob.name, delimiter=step.separator if is_delimited(step.file_format) else None,
                has_header=step.header.strip().lower() != "none", truncated=truncated,
                sheet=step.sheet_name or None if excel else None, skiprows=pandas_rows(step.skiprows) if excel else None,
                header_row=pandas_rows(step.header) if excel else None,
            )
        except Exception as exc:  # noqa: BLE001
            return [CheckItem(id=f"{fn}:sample", level="warn", step=fn, title="Could not read the sample file to compare the schema", detail=str(exc)[:300])]
        problems = compare_with_sample(step.columns, sample, step.file_format)
        if problems:
            return [CheckItem(id=f"{fn}:schema:{i}", level="warn", step=fn, title="Table schema vs the lab sample file", detail=p) for i, p in enumerate(problems)]
        return [CheckItem(id=f"{fn}:schema", level="ok", step=fn, title=f"Table schema matches the sample file {blob.name.rsplit('/', 1)[-1]}")]

    def _layout_vs_sample(self, step: IngestStep, blob: Any, data: bytes, truncated: bool) -> list[CheckItem]:
        """Fixed width: do the lines of the lab sample have the length the column widths add up to?"""
        fn, file = step.function_name, blob.name.rsplit("/", 1)[-1]
        _rows, problems = preview_fixed_width(data, step.columns, header=step.header, skiprows=step.skiprows,
                                              truncated=truncated)
        if problems:
            return [CheckItem(id=f"{fn}:layout:{i}", level="warn", step=fn, title="Column lengths vs the lab sample file",
                              detail=f"{file}: {p}") for i, p in enumerate(problems)]
        return [CheckItem(id=f"{fn}:layout", level="ok", step=fn,
                          title=f"The lines of {file} match the layout ({step.record_length()} characters)")]

    def _target_checks(self, spec, step: IngestStep) -> list[CheckItem]:
        fn = step.function_name
        target = fill_variables(ingest_tables(step)["target"], self._values(spec))
        try:
            table = self._bq(spec).get_table(target)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "NotFound":
                return [CheckItem(id=f"{fn}:table", level="info", step=fn,
                                  title=f"{target} does not exist yet in lab; the first run creates it from the staging table")]
            return [CheckItem(id=f"{fn}:table", level="warn", step=fn, title="Could not read the target table", detail=gcp_error_text(exc))]
        existing = {f.name.lower(): f for f in table.schema}
        declared = {c.name.lower(): c for c in step.columns}
        if step.add_audit_columns:
            for c in AUDIT_COLUMNS:
                declared.setdefault(c, None)
        items = []
        missing = [n for n in existing if n not in declared]
        if missing:
            items.append(CheckItem(id=f"{fn}:table:missing", level="error" if step.load_mode != "merge" else "warn", step=fn,
                                   title=f"The target table has columns the file does not: {', '.join(missing)}",
                                   detail="The INSERT/MERGE copies the staging columns; add them to the table schema or drop them from the table."))
        new_cols = [n for n in declared if n not in existing]
        if new_cols:
            items.append(CheckItem(id=f"{fn}:table:new", level="error", step=fn,
                                   title=f"Columns not in the existing target table: {', '.join(new_cols)}",
                                   detail="Add them to the table (ALTER TABLE … ADD COLUMN) before the first run."))
        mismatched = [
            f"{c.name}: file {c.type} vs table {existing[n].field_type}"
            for n, c in declared.items()
            if c is not None and n in existing and _type_family(c.type) != _type_family(existing[n].field_type)
        ]
        if mismatched:
            items.append(CheckItem(id=f"{fn}:table:types", level="warn", step=fn, title="Type differences with the existing table", detail="; ".join(mismatched)))
        if not items:
            items.append(CheckItem(id=f"{fn}:table", level="ok", step=fn, title=f"Target table {target} exists in lab and its columns match"))
        return items

    # ------------------------------------------------------------------ export
    def _export_step(self, spec, step: ExportStep, today, gcp_checks, built_earlier) -> list[CheckItem]:
        fn = step.function_name
        values = self._values(spec)
        bucket = fill_variables(step.bucket, values)
        folder = fill_variables(step.path, values).strip("/")
        sample = step.file_name + (today.strftime(step.date_suffix) if step.date_suffix else "") + \
            ("-*" if step.sharded else "") + FILE_EXTENSIONS[step.file_format]
        uri = f"gs://{bucket}/{folder + '/' if folder else ''}{fill_variables(sample, values)}"
        items = [CheckItem(id=f"{fn}:uri", level="ok", step=fn, title=f"Exports to {uri} (lab, today)")]
        if not gcp_checks:
            return items
        items += self._bucket_access(spec, fn, "export", bucket)
        schema = None
        if step.source_kind == "table":
            table_id = fill_variables(step.source_table or "", values)
            try:
                table = self._bq(spec).get_table(table_id)
                schema = table.schema
                items.append(CheckItem(id=f"{fn}:source", level="ok", step=fn, title=f"Source table exists in lab ({table.num_rows or 0:,} rows, {_human(table.num_bytes)})"))
                if step.file_format == "XLSX":
                    if (table.num_rows or 0) > EXCEL_MAX_ROWS - 1:
                        items.append(CheckItem(id=f"{fn}:size", level="error", step=fn,
                                               title=f"{table.num_rows:,} rows do not fit in one Excel sheet "
                                                     f"(at most {EXCEL_MAX_ROWS - 1:,}): export CSV or filter with a query"))
                    elif (table.num_bytes or 0) > 200 * 1024**2:
                        items.append(CheckItem(id=f"{fn}:size", level="warn", step=fn,
                                               title=f"An Excel export reads the whole table ({_human(table.num_bytes)}) into "
                                                     "memory on the machine that runs the script"))
                elif not step.sharded and (table.num_bytes or 0) > 1024**3:
                    items.append(CheckItem(id=f"{fn}:size", level="error", step=fn, title="Table is larger than 1 GB: turn on 'Sharded files' to export several files"))
            except Exception as exc:  # noqa: BLE001
                norm = _norm_table(table_id, values.get("write_project", ""))
                level = "info" if norm in built_earlier else "error"
                items.append(CheckItem(id=f"{fn}:source", level=level, step=fn,
                                       title="Source table is built by an earlier step" if level == "info" else "Source table not found in lab",
                                       detail=gcp_error_text(exc)))
        else:
            concrete = substitute_sql_vars(step.source_sql or "", values)
            used = sql_utils.find_query_params(concrete)
            dry, job = self._dry_run_items(spec, fn, "Export query", concrete, [p for p in step.parameters if p.name in used], today, built_earlier)
            items += dry
            schema = getattr(job, "schema", None) if job is not None else None
        if schema and (is_delimited(step.file_format) or step.file_format == "XLSX"):
            nested = [f.name for f in schema if f.field_type in ("RECORD", "STRUCT") or f.mode == "REPEATED"]
            if nested:
                items.append(CheckItem(id=f"{fn}:nested", level="error", step=fn,
                                       title=f"{FORMAT_LABELS[step.file_format]} cannot hold nested/repeated columns: {', '.join(nested)}",
                                       detail="Export as JSON, Avro or Parquet, or flatten those columns in a query."))
        return items

    # ------------------------------------------------------------------ copy files
    def _copy_step(self, spec, step: CopyStep, gcp_checks) -> list[CheckItem]:
        fn = step.function_name
        values = self._values(spec)
        items: list[CheckItem] = []
        sources = [fill_variables(s, values) for s in step.sources]
        destination = fill_variables(step.destination, values)
        today = dt.date.today()
        preview = [s.replace("{date", "{0").format(today) if "{date" in s else s for s in [*sources, destination]]
        items.append(CheckItem(id=f"{fn}:cmd", level="ok", step=fn,
                               title=f"gcloud storage {step.operation} {' '.join(preview[:-1])} {preview[-1]} (lab, today)"))
        local = [p for p in [*sources, destination] if not p.startswith("gs://")]
        if local:
            items.append(CheckItem(id=f"{fn}:local", level="info", step=fn,
                                   title=f"Local path(s) {', '.join(local)} are only checked when the script runs",
                                   detail="They must exist on the machine that runs the script (the Composer worker or VM); a lab test run uses the hub's machine."))
        if shutil.which("gcloud") is None:
            items.append(CheckItem(id=f"{fn}:gcloud", level="info", step=fn,
                                   title="The Google Cloud CLI (gcloud) is not installed where the hub runs: a lab test run of this step would fail here"))
        if not gcp_checks:
            return items
        for i, src in enumerate(p for p in preview[:-1] if p.startswith("gs://")):
            bucket, path = split_gs_uri(src)
            prefix = path.split("*", 1)[0]
            try:
                found = [b.name for b in self._gcs(spec).list_blobs(bucket, prefix=prefix or None, max_results=50)]
                if found:
                    items.append(CheckItem(id=f"{fn}:src:{i}", level="ok", step=fn, title=f"{len(found)}{'+' if len(found) == 50 else ''} object(s) under gs://{bucket}/{prefix} in lab"))
                else:
                    items.append(CheckItem(id=f"{fn}:src:{i}", level="info", step=fn, title=f"Nothing under gs://{bucket}/{prefix} right now (lab)",
                                           detail="gcloud storage fails when a source matches no file."))
            except Exception as exc:  # noqa: BLE001
                items.append(CheckItem(id=f"{fn}:src:{i}", level="warn", step=fn, title=f"Could not list gs://{bucket}/{prefix}", detail=gcp_error_text(exc)))
        if destination.startswith("gs://"):
            items += self._bucket_access(spec, fn, "destination", split_gs_uri(preview[-1])[0])
        return items

    # ------------------------------------------------------------------ custom code
    def _custom_step(self, spec: ProcessSpec, step: CustomStep) -> list[CheckItem]:
        """The rules the code must follow are checked with the spec itself; here: what it uses, and whether the hub can
        test it."""
        from .test_runner import custom_modules, importable

        fn = step.function_name
        author = f"written with Gemini ({step.generated_by})" if step.generated_by else "written or edited by hand"
        items = [CheckItem(id=f"{fn}:custom", level="info", step=fn,
                           title=f"Custom code ({author}): review it; the hub only checks that it compiles and passes ruff",
                           detail="The lab test run is what proves it works.")]
        secrets, keys = custom_code_reads(step)
        if secrets:
            items.append(CheckItem(id=f"{fn}:secrets", level="ok", step=fn, title=f"Reads the secret(s) {', '.join(secrets)}"))
        values = self._values(spec)
        unused = [s.key for s in step.settings if s.key not in keys]
        if step.settings:
            lab = ", ".join(f"{s.key}={fill_variables(s.value, values)!r}" for s in step.settings)
            items.append(CheckItem(id=f"{fn}:settings", level="ok", step=fn, title=f"Settings in lab: {lab}"[:300]))
        if unused:
            items.append(CheckItem(id=f"{fn}:unused", level="info", step=fn,
                                   title=f"Setting(s) the code does not read: {', '.join(unused)}"))
        python = self.settings.test_run_python or sys.executable
        missing = [m for m in custom_modules(step) if not importable(python, m)]
        if missing:
            items.append(CheckItem(id=f"{fn}:packages", level="warn", step=fn,
                                   title=f"The hub's Python does not have {', '.join(missing)}: the lab test run is not possible here",
                                   detail="Install the package(s) where the hub runs, or generate without a lab test "
                                          "(the README says so) and test on the pipeline server."))
        return items

    # ------------------------------------------------------------------ data quality
    def _dq_step(self, spec, step: DqStep, today, gcp_checks, built_earlier) -> list[CheckItem]:
        fn = step.function_name
        items: list[CheckItem] = []
        for i, check in enumerate(step.checks):
            try:
                sql = substitute_sql_vars(dq_check_sql(check, fn), self._values(spec))
            except (RenderError, KeyError) as exc:
                items.append(CheckItem(id=f"{fn}:check{i}", level="error", step=fn, title=f"Check {check.label()}: {exc}"))
                continue
            if not gcp_checks:
                continue
            dry, _ = self._dry_run_items(spec, fn, f"Check {check.label()}", sql, [], today, built_earlier)
            items += dry
        return items

    # ------------------------------------------------------------------ process level
    def _secrets(self, spec: ProcessSpec, gcp_checks: bool) -> list[CheckItem]:
        items: list[CheckItem] = []
        if not gcp_checks or not spec.secrets:
            return items
        project = self._values(spec).get(GSM_PROJECT, "")
        for s in spec.secrets:
            info = secret_version_info(self.clients, project=project, secret_id=s.secret_id, version=s.version)
            label = f"Secret {s.name} ({s.secret_id} in {project}, version {s.version})"
            if info["ok"]:
                items.append(CheckItem(id=f"secret:{s.name}", level="ok", section="variables",
                                       title=f"{label}: version {info['version']} is enabled",
                                       detail=f"created {info['created']}" if info.get("created") else ""))
            elif info.get("state"):
                items.append(CheckItem(id=f"secret:{s.name}", level="error", section="variables",
                                       title=f"{label}: version {info['version']} is {info['state']}",
                                       detail="gsm_module reads exactly this version: pin an enabled version or use latest."))
            else:
                items.append(CheckItem(
                    id=f"secret:{s.name}", level="warn", section="variables", title=f"{label}: could not be checked",
                    detail=f"{info['error']}. The hub only needs the secret's metadata (Secret Manager Viewer); "
                           "the identity that runs the script needs roles/secretmanager.secretAccessor on it.",
                ))
        return items

    def _email(self, spec: ProcessSpec, gcp_checks: bool) -> list[CheckItem]:
        em = spec.email
        if not em.enabled:
            return []
        when = [k for k, on in (("failure", em.send_on.failure), ("success", em.send_on.success)) if on]
        counts = ", ".join(f"{len(parse_receivers(em.receivers.get(env))[0])} in {env}" for env in spec.active_envs())
        sender = self.adc_email if self.adc_email and "@" in self.adc_email else None
        items = [CheckItem(
            id="email:summary", level="ok", section="operations",
            title=f"Email on {' and '.join(when)} to {counts} recipient(s)" + (f" from {sender}" if sender else ""),
            detail="Sent through the internal mail relay (a hub setting), only reachable inside the network; a lab test "
                   "run sends the email to you only.",
        )]
        if not sender:
            items.append(CheckItem(id="email:sender", level="warn", section="operations",
                                   title="The ADC identity has no email address, so the sender is unknown",
                                   detail="Run the hub with your own credentials (gcloud auth application-default login) or edit email.sender in the YAML."))
        values = self._values(spec)
        for i, template in enumerate(em.attachments):
            path = fill_variables(template, values)
            if path.startswith("gs://") and gcp_checks:
                bucket, _p = split_gs_uri(path)
                items += [item.model_copy(update={"id": f"email:gcs:{i}"}) for item in
                          self._bucket_access(spec, "email", "email attachment", bucket, section="operations")]
        return items

    def _sql_logger(self, spec: ProcessSpec, gcp_checks: bool) -> list[CheckItem]:
        if not self.settings.sql_logger_enabled:
            return []
        table = self.settings.sql_logger_table
        if not gcp_checks:
            return [CheckItem(id="sqllog", level="info", section="operations", title=f"SQL is captured with sql_logger in <ADC project>.{table}")]
        if not self.adc_project:
            return [CheckItem(id="sqllog", level="info", section="operations",
                              title=f"SQL is captured with sql_logger in <credentials' project>.{table}",
                              detail="The hub could not tell its ADC project, so the table was not checked.")]
        full = f"{self.adc_project}.{table}"
        try:
            self._bq(spec).get_table(full)
            return [CheckItem(id="sqllog", level="ok", section="operations", title=f"SQL log table {full} exists (sql_logger)")]
        except Exception as exc:  # noqa: BLE001
            return [CheckItem(id="sqllog", level="warn", section="operations",
                              title=f"SQL log table {full} is not reachable: sql_logger would fail the first SQL step",
                              detail=f"{gcp_error_text(exc)}. sql_logger writes to the credentials' project; in lab that is your gcloud project.")]

    def _render(self, spec: ProcessSpec) -> list[CheckItem]:
        try:
            result = render_process(spec, smoke_run=False, adc_email=self.adc_email or "", settings=self.settings)
        except RenderError as exc:
            return [CheckItem(id=f"render:{i}", level="error", section="generate", title="Code generation problem", detail=p)
                    for i, p in enumerate(exc.problems)]
        items = [CheckItem(id="render:ok", level="ok", section="generate",
                           title=f"Code generates cleanly ({', '.join(c.name for c in result.checks)})")]
        items += [CheckItem(id=f"render:warn:{i}", level="warn", section="generate", title=w) for i, w in enumerate(result.warnings)]
        return items


def _type_family(t: str) -> str:
    t = t.upper()
    return {"INT64": "INTEGER", "FLOAT64": "FLOAT", "BOOL": "BOOLEAN", "STRUCT": "RECORD", "BIGNUMERIC": "NUMERIC"}.get(t, t)


def _human(n: int | None) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


__all__ = ["CheckItem", "ClientFactory", "SpecValidator", "ValidationReport", "ENV_LABELS", "gcp_error_text",
           "order_checks", "prod_collisions", "sample_param_value", "secret_version_info"]
