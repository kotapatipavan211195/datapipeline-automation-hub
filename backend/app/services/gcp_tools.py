"""Read-only GCP helpers shared by the REST API and the UI: SQL dry-runs, GCS browse/match, schema from a GCS sample.

Callers pass **lab** values only: the hub never reads the value (prod) environment.
"""

from __future__ import annotations

import datetime as dt
import re
import zlib
from typing import Any

from ..models.rules import fill_variables, split_gs_uri, substitute_sql_vars, template_problems
from ..models.spec import Parameter
from . import sql_utils
from .schema_infer import InferResult, infer_from_bytes
from .validators import ClientFactory, gcp_error_text, sample_param_value


def dry_run_sql(
    clients: ClientFactory,
    *,
    sql: str,
    project_id: str,
    location: str,
    variables: dict[str, str],
    parameters: list[Parameter],
) -> dict[str, Any]:
    """BigQuery dry run of SQL with ${variables} filled from the **lab** values (jobs run in project_id)."""
    from google.cloud import bigquery

    try:
        concrete = substitute_sql_vars(sql, variables)
    except KeyError as exc:
        return {"ok": False, "error": f"variable ${{{exc.args[0]}}} is not defined"}
    today = dt.date.today()
    used = set(sql_utils.find_query_params(concrete))
    missing = sorted(used - {p.name for p in parameters})
    if missing:
        return {"ok": False, "error": f"define the parameter(s) @{', @'.join(missing)} first"}
    params = [
        bigquery.ScalarQueryParameter(p.name, p.bq_type, sample_param_value(p, today, "dry_run"))
        for p in parameters
        if p.name in used
    ]
    try:
        job = clients.bigquery(project_id, location).query(
            concrete,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=params),
            location=location,
        )
    except Exception as exc:  # noqa: BLE001
        text = gcp_error_text(exc)
        m = re.search(r"\[(\d+):(\d+)\]", text)
        return {"ok": False, "error": text, "line": int(m.group(1)) if m else None, "column": int(m.group(2)) if m else None}
    schema = [{"name": f.name, "type": f.field_type, "mode": f.mode} for f in (getattr(job, "schema", None) or [])]
    tables = [f"{t.project}.{t.dataset_id}.{t.table_id}" for t in (getattr(job, "referenced_tables", None) or [])]
    return {
        "ok": True,
        "bytes": job.total_bytes_processed or 0,
        "schema": schema,
        "referenced_tables": tables,
        "as_of": today.isoformat(),
    }


def gcs_browse(clients: ClientFactory, *, project_id: str, bucket: str = "", prefix: str = "") -> dict[str, Any]:
    client = clients.storage(project_id)
    if not bucket:
        return {"buckets": sorted(b.name for b in client.list_buckets(project=project_id, max_results=500))}
    it = client.list_blobs(bucket, prefix=prefix or None, delimiter="/", max_results=500)
    objects = [
        {"name": b.name, "size": b.size, "updated": b.updated.isoformat() if b.updated else None}
        for b in it
        if b.name != prefix
    ]
    return {"bucket": bucket, "prefix": prefix, "prefixes": sorted(getattr(it, "prefixes", []) or []), "objects": objects}


def gcs_match(
    clients: ClientFactory,
    *,
    project_id: str,
    bucket: str,
    folder: str,
    filename: str,
    extensions: tuple[str, ...],
    variables: dict[str, str],
    exclude_prefix: str = "",
) -> dict[str, Any]:
    """Files the ingestion would load: objects under ``folder`` whose name contains ``filename`` (lab values)."""
    problems = [p for t in (bucket, folder, filename) for p in template_problems(t, variables)]
    if problems:
        return {"ok": False, "problems": problems}
    b = fill_variables(bucket, variables)
    f = fill_variables(folder, variables).strip("/")
    name = fill_variables(filename, variables)
    exclude = fill_variables(exclude_prefix, variables).strip("/")
    where = f"gs://{b}/{f + '/' if f else ''}"
    try:
        blobs = [
            x
            for x in clients.storage(project_id).list_blobs(b, prefix=f or None, max_results=2000)
            if not x.name.endswith("/") and name in x.name.rsplit("/", 1)[-1]
            and any(x.name.lower().endswith(e) for e in extensions)
            and not (exclude and x.name.startswith(exclude + "/"))
        ]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "resolved": where, "problems": [gcp_error_text(exc)]}
    blobs.sort(key=lambda x: (x.updated or dt.datetime.min.replace(tzinfo=dt.timezone.utc), x.name))
    return {
        "ok": True,
        "resolved": f"{where}*{name}*",
        "count": len(blobs),
        "total_bytes": sum(x.size or 0 for x in blobs),
        "latest": blobs[-1].name if blobs else None,
        "sample": [
            {"name": x.name, "size": x.size, "updated": x.updated.isoformat() if x.updated else None} for x in blobs[-10:]
        ],
    }


def maybe_gunzip(data: bytes, name: str, limit: int) -> tuple[bytes, bool]:
    if name.endswith(".gz") or data[:2] == b"\x1f\x8b":
        return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, limit), True
    return data, False


def infer_from_upload(data: bytes, filename: str, *, file_format: str, delimiter: str | None, has_header: bool,
                      limit: int) -> InferResult:
    truncated = len(data) > limit
    if truncated and file_format.upper() == "XLSX":
        raise ValueError(f"the workbook is larger than {limit // (1024 * 1024)} MB: save a smaller sample")
    data, gz = maybe_gunzip(data[:limit], filename, limit)
    return infer_from_bytes(data, file_format, filename=filename, delimiter=delimiter or None, has_header=has_header,
                            truncated=truncated or gz)


def infer_from_gcs(
    clients: ClientFactory,
    *,
    project_id: str,
    uri: str,
    file_format: str,
    delimiter: str | None,
    has_header: bool,
    sample_bytes: int,
) -> InferResult:
    bucket, path = split_gs_uri(uri)
    blob = clients.storage(project_id).bucket(bucket).get_blob(path)
    if blob is None:
        raise ValueError(f"{uri} not found")
    whole = file_format.upper() in ("PARQUET", "ORC", "XLSX")  # binary formats are read whole
    if whole and (blob.size or 0) > 50 * 1024 * 1024:
        raise ValueError("Parquet/ORC/Excel samples must be under 50 MB; upload a smaller file instead")
    data = blob.download_as_bytes() if whole else blob.download_as_bytes(start=0, end=sample_bytes - 1)
    data, gz = maybe_gunzip(data, path, sample_bytes * 8)
    return infer_from_bytes(data, file_format, filename=path, delimiter=delimiter or None, has_header=has_header,
                            truncated=gz or (not whole and (blob.size or 0) > sample_bytes))
