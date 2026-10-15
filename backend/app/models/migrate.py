"""Upgrade v1/v2 process specs (single project, run date, hub runtime) to v3 (lab/value/qa environments,
the team's production script standard).

The upgrade is best effort: everything that maps cleanly is carried over, the rest is dropped with a note the
wizard shows once. Values for the value (prod) environment are never invented — they are left empty, so the
spec reports what to fill in before it can be generated.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

RUN_TOKEN_RE = re.compile(r"\{(run_date|run_ts|run_id|process)(:[^{}]*)?\}")


def upgrade_spec(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (v3 spec dict, notes). Input is not modified."""
    d = copy.deepcopy(data)
    version = d.get("spec_version") or 1
    if version >= 3:
        return d, []
    notes: list[str] = [f"Upgraded from spec v{version} to v3 (environments lab / value / qa, team script standard)."]

    gcp = d.pop("gcp", None) or {}
    runtime = d.pop("runtime", None) or {}
    if d.pop("logging", None) is not None:
        notes.append("Logging settings were removed: scripts always log the team way (CONFIG + logging.basicConfig).")
    audit = d.pop("audit", None) or {}
    if audit.get("enabled"):
        notes.append("The per-process audit table was removed: SQL is captured with sql_logger (a hub setting).")
    yesterday = runtime.get("run_date_default", "yesterday") == "yesterday"
    if runtime:
        notes.append("Run date, timezone, retries and byte caps were removed: dates are relative to the day the "
                     "script runs" + (" (the old default was yesterday: date parameters now use today - 1 day)" if yesterday else "") + ".")
    if gcp.get("impersonate_service_account"):
        notes.append("Service-account impersonation was removed: scripts use the default credentials (google.auth.default).")

    # ------------------------------------------------------------ variables → per-environment values
    project = gcp.get("project_id", "") or ""
    old_vars = d.get("variables") or []
    if isinstance(old_vars, dict):  # very old specs kept variables as a mapping
        old_vars = [{"name": k, "value": v} for k, v in old_vars.items()]
    new_vars: list[dict[str, Any]] = [
        {"name": "read_project", "group": "project", "values": {"lab": project}, "description": "project the SQL reads from"},
        {"name": "write_project", "group": "project", "values": {"lab": project}, "description": "project the process writes to"},
    ]
    for v in old_vars:
        name, value = v.get("name", ""), v.get("value", "") or ""
        if name == "project_id":
            new_vars[0]["values"]["lab"] = new_vars[1]["values"]["lab"] = value
            continue
        if name in ("read_project", "write_project"):
            new_vars[0 if name == "read_project" else 1]["values"]["lab"] = value
            continue
        new_vars.append({"name": name, "group": _guess_group(name), "values": {"lab": value},
                         "description": v.get("description", "")})
    d["variables"] = new_vars
    notes.append("Every variable kept its value as the lab (dev) value. Enter the value (prod) values on 'Variables & secrets'.")
    d["environments"] = {"qa_enabled": False}

    # ${project_id} → ${write_project} (SQL) and {project_id} → {write_project} (paths / table names)
    steps_json = json.dumps(d.get("steps") or [])
    steps_json = steps_json.replace("${project_id}", "${write_project}").replace("{project_id}", "{write_project}")
    steps = json.loads(steps_json)

    exports_seen = 0
    new_steps = []
    for step in steps:
        kind = step.get("type")
        fn = step.get("function_name", "step")
        if kind == "sql":
            step.pop("timeout_minutes", None)
            step["parameters"] = [_upgrade_param(p, yesterday, notes, fn) for p in step.get("parameters") or []]
        elif kind == "export":
            exports_seen += 1
            step = _upgrade_export(step, exports_seen, yesterday, notes)
        elif kind == "ingest":
            step = _upgrade_ingest(step, notes)
        elif kind == "dq":
            for c in step.get("checks") or []:
                for key in ("sql", "where"):
                    if c.get(key) and "@run_date" in c[key]:
                        c[key] = c[key].replace("@run_date", "DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)" if yesterday else "CURRENT_DATE()")
                        notes.append(f"{fn}: @run_date in a data-quality check became a date relative to today.")
                if c.get("table"):
                    c["table"] = _strip_tokens(c["table"])
        new_steps.append(step)
    d["steps"] = new_steps

    # ------------------------------------------------------------ secrets / email
    email = d.get("email") or {}
    smtp_secret = ((email.get("smtp") or {}).get("password_secret")) if isinstance(email, dict) else None
    new_secrets = []
    for s in d.get("secrets") or []:
        if smtp_secret and s.get("name") == smtp_secret:
            notes.append(f"Secret '{s.get('name')}' (SMTP password) was removed: the mail relay needs no login.")
            continue
        if s.get("project_id"):
            notes.append(f"Secret '{s.get('name')}': secrets now live in the gsm_project of each environment.")
        new_secrets.append({k: s[k] for k in ("name", "secret_id", "version", "description") if k in s})
    d["secrets"] = new_secrets
    if new_secrets:
        d["variables"].append({"name": "gsm_project", "group": "project", "values": {"lab": project},
                               "description": "project that holds the secrets (gsm_module)"})
    d["email"] = _upgrade_email(email, notes)
    d["spec_version"] = 3
    return d, list(dict.fromkeys(notes))


def _guess_group(name: str) -> str:
    if name.endswith("_project"):
        return "project"
    if name.endswith("_dataset") or name.endswith("_ds"):
        return "dataset"
    if "bucket" in name:
        return "bucket"
    return "other"


def _strip_tokens(text: str, *, keep_date: bool = False) -> str:
    """Drop {run_date…}/{run_ts…}/{run_id}/{process}; with keep_date, {run_date:%fmt} becomes {date:%fmt}."""
    def repl(m: re.Match) -> str:
        if keep_date and m.group(1) == "run_date":
            return "{date" + (m.group(2) or ":%Y%m%d") + "}"
        return ""
    return RUN_TOKEN_RE.sub(repl, text or "")


def _upgrade_param(p: dict[str, Any], yesterday: bool, notes: list[str], fn: str) -> dict[str, Any]:
    p = dict(p)
    src = p.get("source", "run_date")
    shift = -1 if yesterday else 0
    if src == "run_date":
        p["source"], p["offset_days"] = ("today_offset", shift) if shift else ("today", 0)
    elif src == "run_date_offset":
        p["source"], p["offset_days"] = "today_offset", int(p.get("offset_days") or 0) + shift
    elif src == "run_id":
        p["source"] = "process_name"
        notes.append(f"{fn}: @{p.get('name')} used the run id, which no longer exists; it now holds the process name.")
    elif src in ("month_start", "month_end", "prev_month_start", "prev_month_end", "year_start") and yesterday:
        notes.append(f"{fn}: @{p.get('name')} is now relative to today (it was relative to the run date, i.e. yesterday).")
    return p


def _split_gs(uri: str) -> tuple[str, str, str]:
    """gs://bucket/a/b/name.ext → (bucket, 'a/b', 'name.ext')."""
    rest = uri[5:] if uri.startswith("gs://") else uri
    bucket, _, path = rest.partition("/")
    folder, _, name = path.rpartition("/")
    return bucket, folder, name


def _upgrade_export(step: dict[str, Any], index: int, yesterday: bool, notes: list[str]) -> dict[str, Any]:
    fn = step.get("function_name", "export")
    uri = step.pop("destination_uri", "") or ""
    for key in ("overwrite_policy", "write_success_marker"):
        if step.pop(key, None) not in (None, "overwrite", False):
            notes.append(f"{fn}: '{key}' was removed (extract jobs overwrite files with the same name).")
    bucket, folder, name = _split_gs(uri)
    date_fmt = ""
    m = re.search(r"\{run_date(:[^{}]*)?\}", name)
    if m:
        date_fmt = (m.group(1) or ":%Y%m%d")[1:]
    if RUN_TOKEN_RE.search(folder):
        notes.append(f"{fn}: date folders in the export path were removed; the date is now a file-name suffix.")
    sharded = "*" in name
    stem = _strip_tokens(name)
    stem = re.sub(r"\.(csv|txt|json|parquet|avro)(\.gz)?$", "", stem, flags=re.I).replace("*", "").rstrip("_-.") + "_"
    step.update(
        config_key="ftp" if index == 1 else fn,
        bucket=bucket,
        path=_strip_tokens(folder).strip("/").replace("//", "/"),
        file_name=stem if stem != "_" else f"{fn}_",
        date_suffix=date_fmt,
        sharded=sharded,
    )
    if yesterday:
        step["parameters"] = [_upgrade_param(p, yesterday, notes, fn) for p in step.get("parameters") or []]
    else:
        step["parameters"] = [_upgrade_param(p, False, notes, fn) for p in step.get("parameters") or []]
    if step.get("source_table"):
        step["source_table"] = _strip_tokens(step["source_table"])
    return step


def _upgrade_ingest(step: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    fn = step.get("function_name", "ingest")
    csv = step.get("csv") or {}
    pattern = _strip_tokens(step.get("file_pattern") or "")
    filename = re.split(r"[*?\[]", pattern, maxsplit=1)[0] or pattern.replace("*", "")
    target = (step.get("target_table") or "").strip("`")
    parts = target.split(".")
    table = parts[-1] if parts else "table"
    dataset = parts[-2] if len(parts) >= 2 else ""
    skip = int(csv.get("skip_leading_rows", 1) or 0)
    header = "0" if skip >= 1 else "None"
    skiprows = "None" if skip <= 1 else str(list(range(skip - 1)))
    fmt = step.get("file_format", "CSV")
    if fmt in ("AVRO", "ORC"):
        notes.append(f"{fn}: {fmt} files are not supported by the team's ingestion pattern; choose CSV, TXT, JSON lines or Parquet.")
        fmt = "CSV"
    write_mode = step.get("write_mode", "append")
    load_mode = {"append": "append", "truncate": "truncate", "truncate_partition": "truncate", "empty": "append"}.get(write_mode, "append")
    if write_mode in ("truncate_partition", "empty"):
        notes.append(f"{fn}: write mode '{write_mode}' became '{load_mode}'.")
    if step.get("partitioning") or step.get("clustering"):
        notes.append(f"{fn}: partitioning/clustering is no longer set by the load; create the target table with them.")
    if step.get("schema_mode", "explicit") != "explicit" or not step.get("columns"):
        notes.append(f"{fn}: the team's ingestion needs an explicit table schema: add the columns.")
    archive = step.get("archive") or {}
    a_bucket, a_folder, _ = _split_gs((archive.get("archive_path") or "").rstrip("/") + "/x")
    new = {
        "type": "ingest",
        "function_name": fn,
        "description": step.get("description", ""),
        "enabled": step.get("enabled", True),
        "on_failure": step.get("on_failure", "stop"),
        "read_bucket": step.get("source_bucket", ""),
        "read_path": _strip_tokens(step.get("source_prefix") or "").strip("/"),
        "filename": filename,
        "file_format": fmt,
        "header": header,
        "skiprows": skiprows,
        "separator": csv.get("field_delimiter", ","),
        "columns": step.get("columns") or [],
        "table_name": f"{table}_stg",
        "target_table": table,
        "staging_dataset": dataset,
        "target_dataset": dataset,
        "history": False,
        "load_mode": load_mode,
        "archive": bool(archive.get("enabled")),
        "archive_bucket": a_bucket if archive.get("enabled") else "",
        "archive_path": _strip_tokens(a_folder).strip("/").replace("//", "/") if archive.get("enabled") else "",
        "on_no_files": {"fail": "fail", "warn": "warn", "skip": "warn"}.get(step.get("on_no_files", "fail"), "warn"),
    }
    notes.append(f"{fn}: now loads into staging table {table}_stg, then into {table} (the team's ingestion pattern).")
    if len(parts) == 3 and parts[0] not in ("{write_project}",):
        notes.append(f"{fn}: the target project is always write_project now (was {parts[0]}).")
    return new


def _upgrade_email(email: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    if not isinstance(email, dict) or not email:
        return {"enabled": False}
    to = list(email.get("to") or []) + list(email.get("cc") or [])
    if email.get("cc") or email.get("bcc"):
        notes.append("Email: cc recipients were merged into To; bcc was removed.")
    if (email.get("smtp") or {}).get("host"):
        notes.append("Email: the SMTP server, port and login are now hub settings (internal relay); the sender is "
                     "the ADC identity's email.")
    send_on = email.get("send_on") or {}
    attachments = email.get("attachments") or {}
    paths = [_strip_tokens(p, keep_date=True) for p in list(attachments.get("gcs") or []) + list(attachments.get("local") or [])]
    subject = (email.get("subject") or "{script} | {status} | {environment}")
    subject = re.sub(r"\{run_date(:[^{}]*)?\}", lambda m: "{date" + (m.group(1) or "") + "}", subject)
    subject = re.sub(r"\{(run_id|host)\}", "", subject).strip(" |-")
    return {
        "enabled": bool(email.get("enabled")),
        "receivers": {"lab": ", ".join(to)},
        "send_on": {"failure": bool(send_on.get("failure", True)),
                    "success": bool(send_on.get("success", False) or send_on.get("warning", False))},
        "subject": subject or "{script} | {status} | {environment}",
        "attachments": paths,
    }
