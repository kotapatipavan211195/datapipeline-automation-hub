"""Per-browser-tab wizard state: the spec being edited (a plain dict the inputs bind to) plus bookkeeping."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..models.rules import suggest_function_name
from ..models.spec import (
    BUILTIN_PROJECTS,
    GSM_PROJECT,
    SPEC_VERSION,
    CopyStep,
    CustomStep,
    DqStep,
    EmailSettings,
    ExportStep,
    IngestStep,
    Metadata,
    ProcessSpec,
    SqlStep,
    format_validation_errors,
)

STEP_KINDS: dict[str, dict[str, str]] = {
    "ingest": {"label": "Ingest files", "icon": "upload_file", "color": "teal",
               "hint": "Load files from GCS the team way: staging → history → MERGE into the target → archive"},
    "sql": {"label": "Run SQL", "icon": "code", "color": "indigo", "hint": "Run your SQL on BigQuery (MERGE, INSERT, CREATE, SELECT …)"},
    "export": {"label": "Export to GCS", "icon": "cloud_upload", "color": "orange",
               "hint": "Extract a table or query result to a dated file in a bucket (e.g. the FTP bucket)"},
    "copy": {"label": "Copy files", "icon": "drive_file_move", "color": "brown",
             "hint": "Copy, move or sync files between buckets and local folders (gcloud storage)"},
    "dq": {"label": "Data quality", "icon": "fact_check", "color": "green", "hint": "Check row counts, NULLs, duplicates, freshness …"},
    "custom": {"label": "Custom process (AI)", "icon": "auto_awesome", "color": "purple",
               "hint": "Describe it in plain words (geocode with an API key from Secret Manager, pull from DB2 …); "
                       "Gemini writes the function in the team standard"},
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "file_to_table": {"label": "File → Table", "kinds": ["ingest", "dq"], "hint": "Load files from GCS into BigQuery and check them"},
    "drop_to_table": {"label": "Vendor drop → Table", "kinds": ["copy", "ingest", "dq"],
                      "hint": "Move files from a drop bucket, ingest them, check the result"},
    "sql_to_table": {"label": "SQL → Table", "kinds": ["sql", "dq"], "hint": "Run SQL that builds or updates a table"},
    "sql_to_gcs": {"label": "SQL → FTP extract", "kinds": ["sql", "export"], "hint": "Build a table, then extract it to a dated file"},
    "blank": {"label": "Blank", "kinds": [], "hint": "Add the steps yourself"},
}

_DEFAULT_NAMES = {"ingest": "ingest_files", "sql": "run_sql", "export": "extract_data_file", "copy": "copy_files",
                  "dq": "check_data_quality", "custom": "run_custom_process"}
_DEFAULT_KEYS = {"ingest": "gcs_config", "export": "ftp", "copy": "file_copy", "custom": "custom_config"}
_BUILTIN_HELP = {"read_project": "project the SQL reads from", "write_project": "project the process writes to",
                 GSM_PROJECT: "project that holds the secrets (gsm_module)"}


def empty_values() -> dict[str, str]:
    return {"lab": "", "value": "", "qa": ""}


def builtin_variable(name: str, defaults: dict[str, str] | None = None) -> dict[str, Any]:
    """A built-in project variable; read_project / write_project start with the hub's default projects (e.g. value-gcp
    in value)."""
    values = empty_values()
    if name in BUILTIN_PROJECTS:
        values.update({env: project for env, project in (defaults or {}).items() if env in values})
    return {"name": name, "group": "project", "values": values, "description": _BUILTIN_HELP.get(name, "")}


def fill_default_projects(variables: list[dict[str, Any]], defaults: dict[str, str] | None) -> None:
    """Give read_project / write_project the default project of every environment where they are still empty."""
    for v in variables:
        if v.get("name") in BUILTIN_PROJECTS:
            values = v.setdefault("values", empty_values())
            for env, project in (defaults or {}).items():
                if not values.get(env):
                    values[env] = project


def blank_spec(owner: str, process_type: str = "pipeline", defaults: dict[str, str] | None = None) -> dict[str, Any]:
    """An empty process; a copy-files process starts with one copy job. ``defaults``: {env: project} for the
    built-in read/write projects."""
    return {
        "spec_version": SPEC_VERSION,
        "process_type": process_type,
        "metadata": json.loads(Metadata.model_construct(name="", owner=owner).model_dump_json()),
        "environments": {"qa_enabled": False},
        "variables": [builtin_variable(b, defaults) for b in BUILTIN_PROJECTS],
        "secrets": [],
        "email": json.loads(EmailSettings(file_summary=True).model_dump_json()),  # new processes list their files
        "steps": [new_step("copy", [], [])] if process_type == "copy" else [],
    }


def new_step(kind: str, taken: list[str], taken_keys: list[str] | None = None) -> dict[str, Any]:
    fn = suggest_function_name(_DEFAULT_NAMES[kind], taken)
    if kind == "ingest":
        model: Any = IngestStep.model_construct(function_name=fn, read_bucket="", filename="", table_name="",
                                                staging_dataset="", target_dataset="", columns=[])
    elif kind == "sql":
        model = SqlStep.model_construct(function_name=fn, sql="", original_sql="")
    elif kind == "export":
        model = ExportStep.model_construct(function_name=fn, bucket="", file_name="", source_table="")
    elif kind == "copy":
        model = CopyStep.model_construct(function_name=fn, sources=[""], destination="")
    elif kind == "custom":
        model = CustomStep.model_construct(function_name=fn)
    else:
        model = DqStep.model_construct(function_name=fn, checks=[])
    data = json.loads(model.model_dump_json())
    data["type"] = kind
    if kind == "copy":
        data["direction"] = "bucket_to_bucket"  # new copy steps; older ones keep None (any mix of paths)
    if kind == "ingest":
        data["target_enabled"] = False  # new ingestions keep the raw rows in staging (older ones keep their target)
    if kind in _DEFAULT_KEYS:
        key, n, taken_keys = _DEFAULT_KEYS[kind], 2, taken_keys or []
        while key in taken_keys:
            key = f"{_DEFAULT_KEYS[kind]}_{n}"
            n += 1
        data["config_key"] = key
    return data


def new_check(kind: str) -> dict[str, Any]:
    return {
        "kind": kind, "table": "", "columns": [], "min_rows": 1, "accepted": [], "max_age_hours": 24,
        "sql": "", "where": "", "severity": "error", "name": None,
    }


@dataclass
class WizardState:
    spec: dict[str, Any]
    user: str
    owner: str | None = None
    name: str | None = None
    can_edit: bool = True
    is_new: bool = True
    versions: list[dict[str, Any]] = field(default_factory=list)
    saved_hash: str = ""
    saved_at: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    analyses: dict[int, dict[str, Any]] = field(default_factory=dict)
    validation: dict[str, Any] | None = None
    generated: dict[str, Any] | None = None
    test_run_id: str | None = None
    migration_notes: list[str] = field(default_factory=list)
    notes_title: str = "This process was upgraded to the team's script standard. Please review:"
    validated_hash: str | None = None  # spec_hash of the last "Validate all" without errors (unlocks the lab test run)
    test_skip: dict[str, str] | None = None  # {hash, reason, by, at}: lab test skipped for this spec (custom steps only)
    default_projects: dict[str, str] = field(default_factory=dict)  # the hub's read/write project per env (value-gcp)

    # ------------------------------------------------------------------ helpers
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.spec, sort_keys=True, default=str).encode()).hexdigest()

    @property
    def dirty(self) -> bool:
        return self.digest() != self.saved_hash

    def mark_saved(self, saved_at: str | None) -> None:
        self.saved_hash, self.saved_at = self.digest(), saved_at

    @property
    def steps(self) -> list[dict[str, Any]]:
        return self.spec.setdefault("steps", [])

    @property
    def variables(self) -> list[dict[str, Any]]:
        return self.spec.setdefault("variables", [])

    @property
    def is_copy(self) -> bool:
        return self.spec.get("process_type") == "copy"

    def active_envs(self) -> list[str]:
        return ["lab", "value"] + (["qa"] if (self.spec.get("environments") or {}).get("qa_enabled") else [])

    def variable_values(self, env: str = "lab") -> dict[str, str]:
        """{name: value} for one environment (lab: the only one the hub checks, previews or runs)."""
        values = {}
        for v in self.variables:
            if v.get("name"):
                values[v["name"]] = ((v.get("values") or {}).get(env) or "")
        return values

    def variable(self, name: str) -> dict[str, Any] | None:
        return next((v for v in self.variables if v.get("name") == name), None)

    def taken_names(self, except_index: int | None = None) -> list[str]:
        return [s.get("function_name", "") for i, s in enumerate(self.steps) if i != except_index]

    def taken_keys(self) -> list[str]:
        return [s.get("config_key") for s in self.steps if s.get("config_key")]

    def validate(self) -> ProcessSpec | None:
        try:
            spec = ProcessSpec.model_validate(self.spec)
        except ValidationError as exc:
            self.errors = format_validation_errors(exc)
            return None
        self.errors = []
        return spec

    def errors_for(self, prefix: str) -> list[dict[str, str]]:
        return [e for e in self.errors if e["path"].startswith(prefix)]

    def step_errors(self, index: int) -> list[str]:
        """Errors for one step: field errors plus process-level errors that mention its function name."""
        fn = self.steps[index].get("function_name", "")
        out = [e["message"] for e in self.errors if e["path"].startswith(f"steps[{index}]")]
        out += [e["message"] for e in self.errors if e["path"] == "" and fn and e["message"].startswith(f"{fn}")]
        return out

    def add_variables(self, variables: list[dict[str, Any]]) -> list[str]:
        """Add variables found in SQL, with the values they bring per environment. An existing variable only gets the
        environments where it is still empty (a value someone typed is never replaced)."""
        added = []
        for v in variables:
            found = {env: val for env, val in (v.get("values") or {}).items() if env in empty_values() and val}
            if not found and v.get("value"):
                found = {"lab": v["value"]}
            existing = self.variable(v["name"])
            if existing is not None:
                values = existing.setdefault("values", empty_values())
                filled = [env for env, val in found.items() if not values.get(env)]
                for env in filled:
                    values[env] = found[env]
                if filled:
                    added.append(v["name"])
                continue
            self.variables.append({"name": v["name"], "group": v.get("group", "other"),
                                   "values": {**empty_values(), **found}, "description": v.get("description", "")})
            added.append(v["name"])
        return added

    def all_env_values(self) -> dict[str, dict[str, str]]:
        """{env: {name: value}} for every environment (the SQL scan matches projects against all of them)."""
        return {env: self.variable_values(env) for env in empty_values()}

    def ensure_gsm_project(self) -> None:
        if self.spec.get("secrets") and self.variable(GSM_PROJECT) is None:
            self.variables.insert(len(BUILTIN_PROJECTS), builtin_variable(GSM_PROJECT))

    def tables_written_before(self, index: int) -> list[str]:
        """Table templates produced by earlier steps (for quick-pick dropdowns)."""
        out: list[str] = []
        for s in self.steps[:index]:
            if s.get("type") == "ingest" and s.get("table_name"):
                if s.get("target_enabled", True):
                    out.append(f"{{write_project}}.{s.get('target_dataset', '')}.{s.get('target_table') or s['table_name']}")
                else:  # the raw rows stay in staging
                    out.append(f"{{write_project}}.{s.get('staging_dataset', '')}.{s['table_name']}")
            elif s.get("type") == "sql":
                out += s.get("targets") or []
                if s.get("destination_table"):
                    out.append(s["destination_table"])
        return list(dict.fromkeys(out))

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.spec)
