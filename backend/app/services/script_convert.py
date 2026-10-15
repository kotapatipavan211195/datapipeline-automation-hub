"""Turn a fully mapped script analysis into a hub process (spec v3) that the wizard opens for review.

Values come from the script's own YAML, per environment: a value that is the same everywhere stays a literal, one that
differs becomes a {variable}. Nothing is invented: a value the YAML does not hold is left empty, so the wizard asks for
it. Hard-coded GCP projects in SQL become ${read_project} / ${write_project} with the same proven-equivalent rewrite
as pasted SQL.
"""

from __future__ import annotations

import datetime as dt
import re
import textwrap
from typing import Any

from pydantic import ValidationError

from ..models.enhance import EnhanceHeader, FunctionInfo, ScriptAnalysis
from ..models.rules import (
    GENERATED_SCRIPT_NAMES,
    IDENTIFIER_RE,
    function_name_problem,
    script_name_problem,
    suggest_function_name,
    to_snake_case,
)
from ..models.schema import Column
from ..models.spec import (
    BUILTIN_PROJECTS,
    ENVIRONMENTS,
    GSM_PROJECT,
    SPEC_VERSION,
    EmailSettings,
    ProcessSpec,
    format_validation_errors,
)
from .script_scan import name_values, yaml_lookup
from .sql_analyzer import scan_projects

BQ_PARAM_TYPES = {"INTEGER": "INT64", "INT": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "DECIMAL": "NUMERIC"}
DESTINATION_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConvertError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class _Variables:
    """The process variables being collected, with one value per environment from the YAML."""

    def __init__(self, analysis: ScriptAnalysis, envs: list[str]):
        self.config, self.envs = analysis.config, envs
        self.items: dict[str, dict[str, Any]] = {}
        for name in BUILTIN_PROJECTS:
            self.add(name, [name], "project")

    def values(self, path: list[str]) -> dict[str, str]:
        out = {}
        for env in self.envs:
            value = yaml_lookup(self.config.get(env) or {}, path)
            out[env] = "" if value is None or isinstance(value, (dict, list)) else str(value)
        return out

    def add(self, name: str, path: list[str] | None, group: str, values: dict[str, str] | None = None) -> str:
        name = safe_name(name)
        if name not in self.items:
            vals = values if values is not None else (self.values(path) if path else {})
            self.items[name] = {"name": name, "group": group, "description": "",
                                "values": {env: vals.get(env, "") for env in ENVIRONMENTS}}
        return name

    def lab_values(self) -> dict[str, str]:
        return {n: v["values"]["lab"] for n, v in self.items.items()}

    def env_values(self) -> dict[str, dict[str, str]]:
        """{env: {name: value}} of the environments other than lab."""
        return {env: {n: v["values"][env] for n, v in self.items.items()} for env in ENVIRONMENTS if env != "lab"}

    def literal_or_var(self, path: list[str], name: str, group: str = "other") -> str:
        """The value itself when every environment has the same one, else a {variable} holding each."""
        vals = self.values(path)
        distinct = {v for v in vals.values()}
        if len(distinct) == 1 and "" not in distinct:
            return distinct.pop()
        return "{" + self.add(name, path, group) + "}"

    def for_local(self, local: str, fn: FunctionInfo) -> str:
        """The variable behind a name the script read from its YAML (write_project, data_sets.get('x') …)."""
        path = fn.details.get("config_paths", {}).get(local)
        if path:
            group = "dataset" if path[0] in ("data_sets", "datasets") else "project" if path[-1].endswith("project") else \
                "bucket" if "bucket" in path[-1] else "other"
            return self.add(path[-1], path, group)
        for candidate in ([local], ["data_sets", local], ["datasets", local]):
            if any(yaml_lookup(self.config.get(env) or {}, candidate) is not None for env in self.envs):
                group = "dataset" if candidate[0] != local else "project" if local.endswith("project") else "other"
                return self.add(local, candidate, group)
        group = "project" if local.endswith("project") else "dataset" if local.endswith("dataset") else "other"
        return self.add(local, None, group)


def safe_name(name: str) -> str:
    name = to_snake_case(name) if not (IDENTIFIER_RE.match(name) and name == name.lower()) else name
    if name in GENERATED_SCRIPT_NAMES and name not in (*BUILTIN_PROJECTS, GSM_PROJECT) or name == "date":
        name = f"{name}_value"
    return name


def _template(text: str, fn: FunctionInfo, variables: _Variables) -> str:
    """{local} names of a path/table template → {variable} names."""
    return DESTINATION_RE.sub(lambda m: "{" + variables.for_local(m.group(1), fn) + "}", text)


# ====================================================================== steps
def _sql_steps(fn: FunctionInfo, variables: _Variables, taken: list[str], notes: list[str]) -> list[dict[str, Any]]:
    steps = []
    params = []
    for p in fn.details.get("parameters", []):
        bq_type = BQ_PARAM_TYPES.get(p["bq_type"], p["bq_type"])
        source = "today" if p["source"] == "today_offset" and not p["offset_days"] else p["source"]
        params.append({"name": p["name"], "bq_type": bq_type, "source": source, "offset_days": p.get("offset_days", 0),
                       "default": p.get("default"), "original_literal": None, "description": ""})
    for i, block in enumerate(fn.sql):
        sql = block.text
        for local in block.names:
            sql = sql.replace("${" + local + "}", "${" + variables.for_local(local, fn) + "}")
        scan = scan_projects(sql, variables.lab_values(), step=fn.name, env_values=variables.env_values())
        if scan.changed:
            sql = scan.sql
            for v in scan.variables:
                found = {env: val for env, val in v.values.model_dump().items() if val}
                if v.name not in variables.items:
                    variables.add(v.name, None, "project", found)
                    project = next(iter(found.values()), "")
                    notes.append(f"{fn.name}: the hard-coded project {project} is now ${{{v.name}}}, with the same name "
                                 "in every environment (as the script used it): change it there if prod differs")
                else:  # a built-in: only environments that are still empty
                    slot = variables.items[v.name]["values"]
                    slot.update({env: val for env, val in found.items() if not slot.get(env)})
        sql = textwrap.dedent(sql).strip()
        name = fn.name if i == 0 else f"{fn.name}_{i + 1}"
        name = _function_name(name, taken, notes)
        destination = fn.details.get("destination")
        steps.append({
            "type": "sql", "function_name": name, "description": fn.doc if i == 0 else f"{fn.doc} (part {i + 1})".strip(),
            "original_sql": sql, "sql": sql, "sql_file": "",
            "parameters": [p for p in params if "@" + p["name"] in sql],
            "statement_kind": scan.statement_kind if scan.statement_kind in ("SELECT", "DML", "DDL", "MERGE", "SCRIPT") else "OTHER",
            "destination_table": _template(destination, fn, variables) if destination and i == len(fn.sql) - 1 else None,
            "write_mode": fn.details.get("write_mode", "truncate"),
            "sources": scan.sources, "targets": scan.targets,
        })
    if len(fn.sql) > 1:
        notes.append(f"{fn.name}() ran {len(fn.sql)} queries: each one is now its own SQL step")
    return steps


def _export_step(fn: FunctionInfo, variables: _Variables, taken: list[str], notes: list[str], attach: bool) -> dict[str, Any]:
    d = fn.details
    key = d["config_key"]
    file_name = variables.literal_or_var([key, "file_name"], f"{key}_file_name").replace("*", "")
    return {
        "type": "export", "function_name": _function_name(fn.name, taken, notes), "description": fn.doc,
        "config_key": key if re.fullmatch(r"[a-z][a-z0-9_]{0,62}", key) else "ftp", "source_kind": "table",
        "source_table": _template(d.get("source_table", ""), fn, variables),
        "bucket": variables.literal_or_var([key, "bucket"], f"{key}_bucket", "bucket"),
        "path": variables.literal_or_var([key, "path"], f"{key}_path").strip("/"),
        "file_name": file_name, "date_suffix": d.get("date_suffix", ""), "sharded": bool(d.get("sharded")),
        "file_format": d.get("file_format", "CSV"), "compression": d.get("compression", "NONE"),
        "print_header": d.get("print_header", True), "field_delimiter": d.get("field_delimiter", ","),
        "attach_to_email": attach,
    }


def _copy_step(fn: FunctionInfo, variables: _Variables, taken: list[str], notes: list[str]) -> dict[str, Any]:
    words: list[tuple[str, Any]] = fn.details["commands"][0]
    flat = [w if k == "text" else "".join(t for kind, t in w if kind == "text") if k == "parts" and all(kind == "text" for kind, _t in w)
            else None for k, w in words]
    start = next((i for i, w in enumerate(flat) if w and w.split("/")[-1] in ("gsutil", "storage")), None)
    if start is None:
        raise ConvertError([f"{fn.name}(): the gsutil / gcloud storage command could not be read"])
    rest = list(zip(words[start + 1:], flat[start + 1:]))
    op = None
    step: dict[str, Any] = {"type": "copy", "function_name": _function_name(fn.name, taken, notes), "description": fn.doc,
                            "recursive": False, "no_clobber": False, "delete_unmatched": False, "sources": []}
    paths: list[tuple[str, Any]] = []
    keys: set[str] = set()
    for (kind, value), text in rest:
        if op is None and text in ("cp", "mv", "rsync"):
            op = text
        elif text in ("-r", "-R", "--recursive"):
            step["recursive"] = True
        elif text in ("-n", "--no-clobber"):
            step["no_clobber"] = True
        elif text in ("-d", "--delete-unmatched-destination-objects"):
            step["delete_unmatched"] = True
        elif text is not None and text.startswith("-"):
            continue  # -m and other transport flags
        elif op is not None:
            paths.append((kind, value))
    if op is None or len(paths) < 2:
        raise ConvertError([f"{fn.name}(): expected gsutil/gcloud storage cp|mv|rsync <source…> <destination>"])
    resolved: list[str] = []
    for i, (kind, value) in enumerate(paths):
        role = "destination" if i == len(paths) - 1 else "source"
        if kind == "star":
            path = fn.details["config_paths"].get(value)
            if not path:
                raise ConvertError([f"{fn.name}(): *{value} is not read from the YAML"])
            keys.add(path[0])
            per_env = {env: yaml_lookup(variables.config.get(env) or {}, path) or [] for env in variables.envs}
            count = max((len(v) for v in per_env.values()), default=0)
            for j in range(count):
                resolved.append(_copy_path({env: (v[j] if j < len(v) else "") for env, v in per_env.items()},
                                           f"{path[0]}_{role}_{j + 1}", fn, variables))
            continue
        if kind == "parts":
            per_env = {env: "" for env in variables.envs}
            for env in variables.envs:
                text = ""
                for part_kind, part in value:
                    if part_kind == "text":
                        text += part
                    else:
                        found = name_values(part, fn.details["config_paths"], variables.config, env)
                        if part in fn.details["config_paths"]:
                            keys.add(fn.details["config_paths"][part][0])
                        if found is None:
                            raise ConvertError([f"{fn.name}(): the copy path uses {{{part}}}, which the YAML does not hold"])
                        text += str(found)
                per_env[env] = text
            resolved.append(_copy_path(per_env, f"{(sorted(keys) or ['file_copy'])[0]}_{role}", fn, variables))
        else:
            raise ConvertError([f"{fn.name}(): the copy path {value} could not be read"])
    key = next(iter(sorted(keys)), "file_copy")
    step.update(operation=op, sources=resolved[:-1], destination=resolved[-1],
                config_key=key if re.fullmatch(r"[a-z][a-z0-9_]{0,62}", key) else "file_copy")
    return step


def _copy_path(per_env: dict[str, str], name: str, fn: FunctionInfo, variables: _Variables) -> str:
    distinct = set(per_env.values())
    if len(distinct) == 1:
        return distinct.pop()
    split = {env: re.match(r"^gs://([^/]+)(/.*)?$", v or "") for env, v in per_env.items()}
    rests = {(m.group(2) or "") if m else None for m in split.values()}
    if all(split.values()) and len(rests) == 1:
        bucket = variables.add(f"{name}_bucket", None, "bucket", {env: m.group(1) for env, m in split.items()})
        return f"gs://{{{bucket}}}{rests.pop()}"
    raise ConvertError([f"{fn.name}(): the copy path differs between environments in more than the bucket "
                        f"({', '.join(f'{e}: {v}' for e, v in per_env.items())})"])


def _ingest_step(fn: FunctionInfo, analysis: ScriptAnalysis, variables: _Variables, taken: list[str], notes: list[str],
                 email_on: bool) -> dict[str, Any]:
    d = fn.details
    lab = analysis.config.get("lab") or {}
    key = next((k for k, v in lab.items() if isinstance(v, dict) and "gcs_read_bucket" in v and "filename" in v), None)
    if key is None:
        raise ConvertError([f"{fn.name}(): the YAML has no block with gcs_read_bucket and filename"])
    if yaml_lookup(lab, ["datasets", "temporary"]) is None or yaml_lookup(lab, ["datasets", "target"]) is None:
        raise ConvertError([f"{fn.name}(): the YAML has no datasets: temporary / target"])
    schema = analysis.table_schema or {}
    widths = schema.get("column_widths") or {}
    columns = []
    for name, bq_type in schema["table_schema"].items():
        try:
            col = Column(name=name, type=str(bq_type), width=widths.get(name)).model_dump()
        except ValidationError as exc:
            raise ConvertError([f"table schema: {name}: {exc.errors()[0]['msg']}"]) from exc
        columns.append(col)
    reader = d.get("reader")
    separator = str(lab[key].get("separator") or d.get("separator_literal") or ",")
    file_format = {"read_fwf": "FIXED", "read_json": "NEWLINE_DELIMITED_JSON", "read_parquet": "PARQUET",
                   "read_excel": "XLSX"}.get(reader or "", "CSV" if separator == "," else "TXT")
    archive = "gcs_archive_bucket" in lab[key]
    step = {
        "type": "ingest", "function_name": _function_name(fn.name, taken, notes), "description": fn.doc, "config_key": key,
        "read_bucket": variables.literal_or_var([key, "gcs_read_bucket"], "landing_bucket", "bucket"),
        "read_path": variables.literal_or_var([key, "gcs_read_bucket_path"], "landing_path").strip("/"),
        "filename": variables.literal_or_var([key, "filename"], "file_name_prefix"),
        "file_format": file_format, "header": str(lab[key].get("header", "0")), "skiprows": str(lab[key].get("skiprows", "None")),
        "sheet_name": str(lab[key].get("sheet_name") or "") if file_format == "XLSX" else "",
        "separator": separator, "columns": columns, "table_name": str(schema.get("table_name") or ""), "target_table": "",
        "staging_dataset": variables.literal_or_var(["datasets", "temporary"], "staging_dataset", "dataset"),
        "target_dataset": variables.literal_or_var(["datasets", "target"], "target_dataset", "dataset"),
        "history": bool(d.get("history")), "load_mode": d.get("load_mode", "append"),
        "merge_keys": d.get("merge_keys") or [], "merge_order_by": d.get("merge_order_by"), "merge_filter": "",
        "write_disposition": str(lab.get("write_disposition") or "WRITE_APPEND"),
        "add_audit_columns": bool(d.get("audit_columns")), "archive": archive,
        "archive_bucket": variables.literal_or_var([key, "gcs_archive_bucket"], "archive_bucket", "bucket") if archive else "",
        "archive_path": variables.literal_or_var([key, "gcs_archive_bucket_path"], "archive_path").strip("/") if archive else "",
        "on_no_files": "email" if d.get("no_file_email") and email_on else "fail" if d.get("fails_without_files") else "warn",
    }
    if step["load_mode"] == "merge" and not step["merge_keys"]:
        step["load_mode"] = "append"
        notes.append(f"{fn.name}: the MERGE keys could not be read, so the rows are appended: set the keys if it should merge")
    notes.append(f"{fn.name}: the target table is taken to be {step['table_name']} (as in the table schema): check it")
    return step


def _function_name(name: str, taken: list[str], notes: list[str]) -> str:
    final = name
    if function_name_problem(name) or name in taken:
        final = suggest_function_name(to_snake_case(name) + ("_step" if name in GENERATED_SCRIPT_NAMES else ""), taken)
        notes.append(f"{name}() is now {final}() (step names must be unique, lower_snake_case and not a name the script uses)")
    taken.append(final)
    return final


# ====================================================================== entry point
def to_spec(analysis: ScriptAnalysis, *, process_name: str, owner: str, header: EnhanceHeader | None = None,
            today: dt.date | None = None) -> tuple[dict[str, Any], list[str]]:
    """(spec dict, notes for the wizard banner). Raises ConvertError when the script cannot become a process.
    ``header``: the header block as edited on the Enhance page (default: the old script's), plus this update's line."""
    if not analysis.convertible:
        raise ConvertError(analysis.blockers or ["the script cannot be converted"])
    envs = [e for e in ENVIRONMENTS if e in analysis.config] or ["lab"]
    notes = [f"Imported from {analysis.file_name}: {len(analysis.step_order)} step(s) "
             f"({', '.join(analysis.step_order)}). Check every page, then validate, test in lab and generate."]
    notes += analysis.notes
    if analysis.credentials:
        notes.append("Credentials: the old script did not use ADC (" + "; ".join(analysis.credentials[:4])
                     + "). The generated script uses ADC (google.auth.default()): your own IAM identity in lab, the "
                     "Composer service account in value. Once it runs, delete the key file and any "
                     "GOOGLE_APPLICATION_CREDENTIALS setting, and check that both identities have the BigQuery / GCS "
                     "roles the steps need.")
    if not analysis.config:
        notes.append("No YAML was given: enter every value under Basics and Variables for lab and value (prod).")
    else:
        missing = [e for e in ("lab", "value") if e not in analysis.config]
        if missing:
            notes.append(f"The YAML has no {' / '.join(missing)} block: enter those values in the wizard.")
    variables = _Variables(analysis, envs)
    email_on = bool(analysis.email.get("sends") or analysis.email.get("success") or analysis.email.get("failure"))
    taken: list[str] = []
    steps: list[dict[str, Any]] = []
    for fn in analysis.steps():
        if fn.kind == "sql":
            steps += _sql_steps(fn, variables, taken, notes)
        elif fn.kind == "export":
            attach = fn.name in analysis.email.get("attach", []) or fn.details.get("config_key") in analysis.email.get("blocks", [])
            steps.append(_export_step(fn, variables, taken, notes, attach and email_on))
        elif fn.kind == "copy":
            steps.append(_copy_step(fn, variables, taken, notes))
        elif fn.kind == "ingest":
            steps.append(_ingest_step(fn, analysis, variables, taken, notes, email_on))

    header = header or EnhanceHeader.from_analysis(analysis)
    stem = analysis.stem if not script_name_problem(analysis.stem) else None
    if stem is None:
        notes.append(f"The script name {analysis.stem!r} does not follow the naming rules: the process name is used instead.")
    email = EmailSettings().model_dump(mode="json")
    if email_on:
        email["enabled"] = True
        for env in envs:
            receivers = yaml_lookup(analysis.config.get(env) or {}, ["email", "receiver"]) or []
            email["receivers"][env] = ", ".join(receivers) if isinstance(receivers, list) else str(receivers)
        email["send_on"] = {"success": bool(analysis.email.get("success") or not analysis.email.get("failure")),
                            "failure": bool(analysis.email.get("failure"))}
        attachments = yaml_lookup(analysis.config.get("lab") or {}, ["email", "attachment"]) or []
        email["attachments"] = [str(a) for a in attachments] if isinstance(attachments, list) else []
        notes.append("Email: the subject and message use the hub's defaults; adjust them on the Email page.")
    spec = {
        "spec_version": SPEC_VERSION,
        "process_type": "copy" if steps and all(s["type"] == "copy" for s in steps) else "pipeline",
        "metadata": {
            "name": process_name, "script_name": stem, "description": header.description, "owner": owner,
            "co_owners": [], "team": "", "tags": ["enhanced"], "authors": header.authors,
            "reviewers": header.reviewers, "jira": header.jira,
            "created_on": header.created_on, "updates": header.all_updates(today or dt.date.today()),
        },
        "environments": {"qa_enabled": "qa" in envs},
        "variables": list(variables.items.values()),
        "secrets": [],
        "email": email,
        "steps": steps,
    }
    try:
        ProcessSpec.model_validate(spec)
    except ValidationError as exc:
        errors = format_validation_errors(exc)
        notes.append(f"{len(errors)} thing(s) to fix in the wizard, e.g.: "
                     + "; ".join(f"{e['path'] or 'process'}: {e['message']}" for e in errors[:3]))
    return spec, notes
