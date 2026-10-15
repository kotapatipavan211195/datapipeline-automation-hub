"""Health, identity and the option lists that drive the wizard's dropdowns."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import VERSION
from ..auth import CurrentUser
from ..deps import Services, current_user, services
from ..models.rules import RUNTIME_TOKENS
from ..models.schema import BQ_TYPES
from ..models.spec import ENV_LABELS, ENVIRONMENTS, EXPORT_COMPRESSION

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    s = svc.settings
    adc = svc.adc.get()
    return {
        "email": user.email,
        "auth": user.source,
        "adc": {"email": adc.email, "kind": adc.kind, "error": adc.error},
        "version": VERSION,
        "gemini": {"enabled": svc.gemini is not None, "model": s.gemini_model if svc.gemini else None},
        "workspace": svc.store.kind,
        "gcp_checks": s.gcp_checks_enabled,
        "default_project": s.gcp_project,
        "environments": s.environments,
        "test_runs": {"enabled": s.test_run_enabled, "environment": "lab", "timeout_seconds": s.test_run_timeout_seconds},
        "sql_logger": s.sql_logger_enabled,
        "email_relay": f"{s.smtp_host}:{s.smtp_port}",
    }


@router.get("/options")
def options() -> dict:
    """Enumerations with plain-English labels so the UI never shows raw jargon without explanation."""
    return {
        "environments": [{"value": e, "label": ENV_LABELS[e]} for e in ENVIRONMENTS],
        "variable_groups": [
            {"value": "project", "label": "GCP project"},
            {"value": "dataset", "label": "BigQuery dataset"},
            {"value": "bucket", "label": "Cloud Storage bucket"},
            {"value": "other", "label": "Other (folder, file name, …)"},
        ],
        "column_types": list(BQ_TYPES),
        "column_modes": ["NULLABLE", "REQUIRED", "REPEATED"],
        "ingest_formats": [
            {"value": "CSV", "label": "CSV (.csv)"},
            {"value": "TXT", "label": "TXT — delimited text (.txt)"},
            {"value": "FIXED", "label": "TXT — fixed width (column lengths in the table schema)"},
            {"value": "NEWLINE_DELIMITED_JSON", "label": "JSON (one object per line)"},
            {"value": "PARQUET", "label": "Parquet"},
            {"value": "XLSX", "label": "Excel (.xlsx; sheet_name picks the sheet)"},
        ],
        "process_types": [
            {"value": "pipeline", "label": "Pipeline (SQL, file ingestion, exports, copies, data quality)"},
            {"value": "copy", "label": "Copy files (gcloud storage cp / mv / rsync)"},
        ],
        "export_formats": [
            {"value": "CSV", "label": "CSV (.csv)"},
            {"value": "TXT", "label": "TXT — delimited text (.txt)"},
            {"value": "NEWLINE_DELIMITED_JSON", "label": "JSON (one object per line)"},
            {"value": "PARQUET", "label": "Parquet"},
            {"value": "AVRO", "label": "Avro"},
            {"value": "XLSX", "label": "Excel (.xlsx; one file, written with pandas)"},
        ],
        "export_compression": {k: sorted(v) for k, v in EXPORT_COMPRESSION.items()},
        "ingest_load_modes": [
            {"value": "merge", "label": "MERGE into the target (update matching keys, insert new ones)"},
            {"value": "append", "label": "Append the rows to the target"},
            {"value": "truncate", "label": "Replace the target's rows with each file"},
        ],
        "sql_write_modes": [
            {"value": "truncate", "label": "Replace the destination table", "bq": "WRITE_TRUNCATE"},
            {"value": "append", "label": "Append to the destination table", "bq": "WRITE_APPEND"},
            {"value": "empty", "label": "Only write if the destination is empty", "bq": "WRITE_EMPTY"},
        ],
        "no_files_policies": [
            {"value": "email", "label": "Email the team ('no file received today')"},
            {"value": "warn", "label": "Log a warning and continue"},
            {"value": "fail", "label": "Fail the run"},
        ],
        "copy_operations": [
            {"value": "cp", "label": "Copy (gcloud storage cp)"},
            {"value": "mv", "label": "Move (gcloud storage mv)"},
            {"value": "rsync", "label": "Sync a folder (gcloud storage rsync)"},
        ],
        "param_sources": [
            {"value": "today", "label": "Today (the day the script runs)"},
            {"value": "today_offset", "label": "Today ± N days"},
            {"value": "month_start", "label": "First day of this month"},
            {"value": "month_end", "label": "Last day of this month"},
            {"value": "prev_month_start", "label": "First day of last month"},
            {"value": "prev_month_end", "label": "Last day of last month"},
            {"value": "year_start", "label": "1 January of this year"},
            {"value": "config", "label": "A value in the YAML (per environment)"},
            {"value": "process_name", "label": "The process name"},
        ],
        "param_types": ["DATE", "TIMESTAMP", "DATETIME", "STRING", "INT64", "FLOAT64", "NUMERIC", "BOOL"],
        "dq_kinds": [
            {"value": "row_count_min", "label": "At least N rows"},
            {"value": "not_null", "label": "Columns are never empty (NULL)"},
            {"value": "unique", "label": "Column combination is unique"},
            {"value": "accepted_values", "label": "Column only has allowed values"},
            {"value": "freshness", "label": "Newest timestamp is recent enough"},
            {"value": "custom_sql", "label": "Custom query returns no rows"},
        ],
        "runtime_tokens": RUNTIME_TOKENS,
        "path_tokens": ["{date:%Y%m%d}", "{date:%Y_%m_%d}", "{date:%Y-%m-%d}"],
        "date_suffixes": ["%Y_%m_%d", "%Y%m%d", "%Y-%m-%d", "%m%d%Y", ""],
    }
