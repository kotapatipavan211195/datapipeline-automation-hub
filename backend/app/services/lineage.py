"""Which tables and files a process reads and writes: for the step-order checks, validation, test-run side effects
and the README. Names are resolved with one environment's values (lab unless stated: the hub never looks at prod).

Two views of the same facts:

* ``step_io`` — per enabled step, the *resolved* table names it reads/writes (order checks).
* ``process_tables`` — the whole process as the README shows it: external dependencies (tables read but not built
  earlier in the process), target tables (with how they are written), and the GCS/local files read and written.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.rules import fill_variables, substitute_sql_vars
from ..models.spec import (
    FILE_EXTENSIONS,
    FORMAT_LABELS,
    CopyStep,
    DqStep,
    ExportStep,
    IngestStep,
    ProcessSpec,
    SqlStep,
)
from . import sql_utils

INGEST_HOW = {
    "merge": "MERGE from the staging table (newest row per key)",
    "append": "INSERT from the staging table (append)",
    "truncate": "TRUNCATE (first file of the run) + INSERT from the staging table",
}
SQL_DESTINATION_HOW = {
    "truncate": "query results (replace the table)",
    "append": "query results (append)",
    "empty": "query results (only if the table is empty)",
}
COPY_VERBS = {"cp": "copied", "mv": "moved", "rsync": "synchronised"}


def ingest_tables(step: IngestStep) -> dict[str, str]:
    """Table templates an ingest step writes: staging, history (optional) and target."""
    out = {"staging": f"{{write_project}}.{step.staging_dataset}.{step.table_name}"}
    if step.target_enabled:
        out["target"] = f"{{write_project}}.{step.target_dataset}.{step.target()}"
    if step.history:
        out["history"] = f"{{write_project}}.{step.target_dataset}.{step.table_name}_history"
    return out


def export_uri_template(step: ExportStep) -> str:
    """gs:// path an export writes, with {variables} and a readable date placeholder."""
    ext = FILE_EXTENSIONS.get(step.file_format, "")
    if step.compression == "GZIP":
        ext += ".gz"
    folder = step.path.strip("/")
    suffix = f"<{step.date_suffix}>" if step.date_suffix else ""
    name = f"{step.file_name}{suffix}{'-*' if step.sharded else ''}{ext}"
    return f"gs://{step.bucket}/{folder + '/' if folder else ''}{name}"


def ingest_source_template(step: IngestStep) -> str:
    folder = step.read_path.strip("/")
    return f"gs://{step.read_bucket}/{folder + '/' if folder else ''}*{step.filename}*"


# ====================================================================== per-step, resolved (order checks)
@dataclass
class StepIO:
    fn: str
    index: int
    reads: set[str]
    writes: set[str]


def norm_table(name: str, project: str) -> str:
    """Lower-case project.dataset.table; a dataset.table name runs in (and so belongs to) the job's project."""
    name = name.strip("`").lower()
    return f"{project.lower()}.{name}" if name.count(".") == 1 else name


def step_io(spec: ProcessSpec, env: str = "lab") -> list[StepIO]:
    values = spec.variable_values(env)
    project = values.get("write_project") or "job-project"

    def res(template: str | None) -> str | None:
        if not template:
            return None
        resolved = fill_variables(template, values)
        return None if "{" in resolved else norm_table(resolved, project)

    def sql_tables(sql: str) -> tuple[set[str], set[str]]:
        try:
            f = sql_utils.facts(substitute_sql_vars(sql, values))
        except KeyError:
            return set(), set()
        return {norm_table(s, project) for s in f.sources}, {norm_table(t, project) for t in f.targets}

    out = []
    for i, step in enumerate(spec.enabled_steps()):
        reads: set[str] = set()
        writes: set[str] = set()
        if isinstance(step, IngestStep):
            writes = {res(t) or "" for t in ingest_tables(step).values()}
        elif isinstance(step, SqlStep):
            reads, writes = sql_tables(step.sql)
            if step.destination_table:
                writes.add(res(step.destination_table) or "")
        elif isinstance(step, ExportStep):
            if step.source_kind == "table":
                reads.add(res(step.source_table) or "")
            else:
                reads, _ = sql_tables(step.source_sql or "")
        elif isinstance(step, DqStep):
            reads = {res(c.table) or "" for c in step.checks if c.table}
            for c in step.checks:
                if c.kind == "custom_sql" and c.sql:
                    reads |= sql_tables(c.sql)[0]
        out.append(StepIO(step.function_name, i, {r for r in reads if r}, {w for w in writes if w}))
    return out


# ====================================================================== whole process (README, test-run summary)
@dataclass
class TableUse:
    template: str  # as configured: {write_project}.{raw_dataset}.store_sales
    resolved: str  # with one environment's values: demo-analytics-dev.raw_sales.store_sales
    job_project: bool = False  # written as dataset.table: BigQuery uses the credentials' project
    read_by: list[str] = field(default_factory=list)
    written_by: list[str] = field(default_factory=list)
    how: list[str] = field(default_factory=list)

    @property
    def read_after_write(self) -> list[str]:
        """Steps that read this table and do not write it (e.g. checks or exports of a table built here)."""
        return [s for s in self.read_by if s not in self.written_by]


@dataclass
class FileUse:
    step: str
    uri: str  # variables filled in; {date:...} kept
    role: str


@dataclass
class TableInventory:
    dependencies: list[TableUse]
    targets: list[TableUse]
    gcs_inputs: list[FileUse]
    gcs_outputs: list[FileUse]


def process_tables(spec: ProcessSpec, env: str = "lab") -> TableInventory:
    values = spec.variable_values(env)
    uses: dict[str, TableUse] = {}
    first_read: dict[str, tuple[int, int]] = {}  # (step index, order of appearance)
    first_write: dict[str, tuple[int, int]] = {}
    seq = iter(range(1_000_000))
    gcs_inputs: list[FileUse] = []
    gcs_outputs: list[FileUse] = []

    def use(template: str | None) -> tuple[str, TableUse] | None:
        template = (template or "").strip().strip("`")
        if not template:
            return None
        two_part = template.count(".") == 1
        resolved = fill_variables(template, values)
        key = resolved.lower()
        if key not in uses:
            uses[key] = TableUse(template=template, resolved=resolved, job_project=two_part)
        return key, uses[key]

    def read(index: int, fn: str, template: str | None) -> None:
        hit = use(template)
        if hit:
            key, u = hit
            first_read.setdefault(key, (index, next(seq)))
            if fn not in u.read_by:
                u.read_by.append(fn)

    def write(index: int, fn: str, template: str | None, how: str) -> None:
        hit = use(template)
        if hit:
            key, u = hit
            first_write.setdefault(key, (index, next(seq)))
            if fn not in u.written_by:
                u.written_by.append(fn)
            if how and how not in u.how:
                u.how.append(how)

    def sql_reads(index: int, fn: str, sql: str) -> None:
        sources, _ = sql_utils.table_template_ops(sql)
        for t in sources:
            read(index, fn, t)

    def file_use(fn: str, uri: str, role: str) -> FileUse:
        return FileUse(fn, fill_variables(uri, values), role)

    for i, step in enumerate(spec.enabled_steps()):
        fn = step.function_name
        if isinstance(step, IngestStep):
            tables = ingest_tables(step)
            gcs_inputs.append(file_use(fn, ingest_source_template(step),
                                      f"{FORMAT_LABELS[step.file_format]} files loaded (name contains '{step.filename}')"))
            if "target" in tables:
                write(i, fn, tables["staging"], "staging: replaced by every file")
            else:
                write(i, fn, tables["staging"], "staging: this run's files (raw rows for the next steps)")
            if "history" in tables:
                write(i, fn, tables["history"], "history: every file appended")
            if "target" in tables:
                write(i, fn, tables["target"], INGEST_HOW[step.load_mode])
            if step.archive:
                bucket = step.archive_bucket or step.read_bucket
                gcs_outputs.append(file_use(fn, f"gs://{bucket}/{step.archive_path.strip('/')}/",
                                            "archive: loaded files are moved here"))
        elif isinstance(step, SqlStep):
            sources, target_ops = sql_utils.table_template_ops(step.sql)
            for t in sources:
                read(i, fn, t)
            for t, op in target_ops.items():
                write(i, fn, t, op or step.statement_kind)
            if step.destination_table:
                write(i, fn, step.destination_table, SQL_DESTINATION_HOW[step.write_mode])
        elif isinstance(step, ExportStep):
            if step.source_kind == "table":
                read(i, fn, step.source_table)
            else:
                sql_reads(i, fn, step.source_sql or "")
            compression = "" if step.compression == "NONE" else f", {step.compression.lower()}"
            gcs_outputs.append(file_use(fn, export_uri_template(step),
                                        f"export: {FORMAT_LABELS[step.file_format]}{compression}"))
        elif isinstance(step, CopyStep):
            verb = COPY_VERBS[step.operation]
            for src in step.sources:
                role = f"{'moved' if step.operation == 'mv' else 'read'} by gcloud storage {step.operation}"
                gcs_inputs.append(file_use(fn, src, role + ("" if src.startswith("gs://") else " (local)")))
            gcs_outputs.append(file_use(fn, step.destination, f"files {verb} here"
                                        + ("" if step.destination.startswith("gs://") else " (local)")))
        elif isinstance(step, DqStep):
            for c in step.checks:
                if c.kind == "custom_sql":
                    sql_reads(i, fn, c.sql or "")
                else:
                    read(i, fn, c.table)

    dependencies = [
        uses[k] for k in sorted(first_read, key=lambda k: first_read[k])
        if k not in first_write or first_read[k][0] <= first_write[k][0]  # read before (or while) the process builds it
    ]
    targets = [uses[k] for k in sorted(first_write, key=lambda k: first_write[k])]
    return TableInventory(dependencies, targets, gcs_inputs, gcs_outputs)
