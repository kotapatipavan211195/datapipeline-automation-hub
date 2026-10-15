from __future__ import annotations

import copy
import datetime as dt

import pytest
from pydantic import ValidationError

from app.models.migrate import upgrade_spec
from app.models.rules import (
    function_name_problem,
    parse_receivers,
    resolve_template,
    suggest_function_name,
    template_problems,
)
from app.models.spec import ProcessSpec, format_validation_errors

from conftest import minimal_spec

EXPORT = {
    "type": "export", "function_name": "extract_data_file_csv", "source_kind": "table",
    "source_table": "{write_project}.{ds}.t", "bucket": "ftp-bucket", "path": "outbound", "file_name": "members_",
}
INGEST = {
    "type": "ingest", "function_name": "ingest_files", "read_bucket": "landing-bucket", "read_path": "inbound",
    "filename": "Daily File_", "columns": [{"name": "Client_Id", "type": "STRING"}, {"name": "Created", "type": "DATETIME"}],
    "table_name": "engagement", "staging_dataset": "{ds}", "target_dataset": "{ds}", "merge_keys": ["Client_Id"],
    "merge_order_by": "Created", "archive_path": "inbound/Archive",
}
COPY = {"type": "copy", "function_name": "copy_files", "sources": ["gs://drop-bucket/out/*.csv"], "destination": "gs://landing-bucket/in/"}


def test_samples_are_valid(sales_spec_dict, customer_spec_dict, strategic_spec_dict, ingest_spec_dict):
    assert ProcessSpec.model_validate(sales_spec_dict).metadata.name == "sales_daily"
    assert len(ProcessSpec.model_validate(customer_spec_dict).enabled_steps()) == 5
    assert ProcessSpec.model_validate(strategic_spec_dict).script_stem() == "dl_strategic_pipeline_template"
    assert ProcessSpec.model_validate(ingest_spec_dict).ingest_step().target() == "engagement_daily"


@pytest.mark.parametrize(
    "name,ok",
    [
        ("ingest_sales_files", True),
        ("build_x2", True),
        ("main", False),  # clashes with the generated main()
        ("email", False),  # the generated email()
        ("merge_to_main_table", False),  # an ingestion helper
        ("class", False),  # keyword
        ("list", False),  # builtin
        ("2fast", False),
        ("BuildSales", False),
        ("_private", False),
        ("has-dash", False),
    ],
)
def test_function_name_rules(name, ok):
    assert (function_name_problem(name) is None) is ok


def test_suggest_function_name_is_unique_and_valid():
    assert suggest_function_name("Build Sales Daily!") == "build_sales_daily"
    assert suggest_function_name("main") == "step_main"
    assert suggest_function_name("load", taken=["load"]) == "load_2"


def test_duplicate_function_names_rejected():
    step = {"type": "sql", "function_name": "same_name", "sql": "SELECT 1"}
    with pytest.raises(ValidationError, match="unique"):
        ProcessSpec.model_validate(minimal_spec(step, dict(step)))


def test_undefined_sql_variable_rejected():
    step = {"type": "sql", "function_name": "q", "sql": "SELECT * FROM `${missing}.t`"}
    with pytest.raises(ValidationError, match=r"undefined variable\(s\) \$\{missing\}"):
        ProcessSpec.model_validate(minimal_spec(step))


# ---------------------------------------------------------------- environments
def test_read_and_write_project_are_builtin_and_added_when_missing():
    data = minimal_spec()
    data["variables"] = [v for v in data["variables"] if v["name"] == "ds"]
    with pytest.raises(ValidationError) as info:
        ProcessSpec.model_validate(data)
    paths = {e["path"] for e in format_validation_errors(info.value)}
    assert {"variables.read_project", "variables.write_project"} <= paths


def test_variable_values_per_environment():
    step = {"type": "sql", "function_name": "q", "sql": "SELECT * FROM `${write_project}.${ds}.t`"}
    spec = ProcessSpec.model_validate(minimal_spec(step))
    assert spec.variable_values() == {"read_project": "proj-dev", "write_project": "proj-dev", "ds": "dataset_a"}
    assert spec.variable_values("value")["write_project"] == "proj-prod"
    assert spec.code_variables() == ["write_project", "ds"]


def test_every_used_variable_needs_a_prod_value():
    data = minimal_spec({"type": "sql", "function_name": "q", "sql": "SELECT * FROM `${write_project}.${ds}.t`"})
    data["variables"][2]["values"] = {"lab": "dataset_a"}
    with pytest.raises(ValidationError, match=r"variables.ds: enter the value \(prod\) value"):
        ProcessSpec.model_validate(data)
    data["environments"] = {"qa_enabled": True}
    data["variables"][2]["values"] = {"lab": "a", "value": "b"}
    with pytest.raises(ValidationError, match="enter the qa value"):
        ProcessSpec.model_validate(data)


def test_unused_variables_may_stay_empty():
    data = minimal_spec()
    data["variables"].append({"name": "spare", "values": {"lab": "x"}})
    ProcessSpec.model_validate(data)


def test_sql_variable_values_are_injection_safe():
    data = minimal_spec({"type": "sql", "function_name": "q", "sql": "SELECT * FROM `${write_project}.${ds}.t`"})
    data["variables"][2]["values"] = {"lab": "x`; DROP TABLE t; --", "value": "ok"}
    with pytest.raises(ValidationError):
        ProcessSpec.model_validate(data)


def test_path_variables_may_hold_folders_but_not_quotes():
    data = minimal_spec(dict(EXPORT, path="{folder}"))
    data["variables"].append({"name": "folder", "values": {"lab": "outbound/test", "value": "outbound/rpm"}})
    ProcessSpec.model_validate(data)
    data["variables"][-1]["values"]["value"] = "out'bound"
    with pytest.raises(ValidationError, match="not allowed"):
        ProcessSpec.model_validate(data)


def test_unknown_path_placeholder_rejected():
    with pytest.raises(ValidationError, match="unknown placeholder '{nope}'"):
        ProcessSpec.model_validate(minimal_spec(dict(EXPORT, path="{nope}/x")))


def test_bucket_values_are_checked_per_environment():
    data = minimal_spec(dict(EXPORT, bucket="{ftp_bucket}"))
    data["variables"].append({"name": "ftp_bucket", "group": "bucket", "values": {"lab": "good-bucket", "value": "Bad_Bucket!"}})
    with pytest.raises(ValidationError, match=r"not a valid bucket name"):
        ProcessSpec.model_validate(data)


# ---------------------------------------------------------------- export
@pytest.mark.parametrize(
    "fmt,compression,ok",
    [("CSV", "GZIP", True), ("CSV", "SNAPPY", False), ("PARQUET", "ZSTD", True), ("AVRO", "GZIP", False)],
)
def test_export_compression_pairs(fmt, compression, ok):
    step = dict(EXPORT, file_format=fmt, compression=compression)
    if ok:
        ProcessSpec.model_validate(minimal_spec(step))
    else:
        with pytest.raises(ValidationError, match="supports compression"):
            ProcessSpec.model_validate(minimal_spec(step))


def test_export_file_name_and_bucket_rules():
    for bad, message in [({"bucket": "gs://x"}, "without gs://"), ({"file_name": "a*b"}, "must not contain"),
                         ({"date_suffix": "%Y/%m"}, "must not contain '/'"), ({"path": "/abs"}, "must not start with")]:
        with pytest.raises(ValidationError, match=message):
            ProcessSpec.model_validate(minimal_spec(dict(EXPORT, **bad)))


def test_query_exports_are_always_sharded():
    step = dict(EXPORT, source_kind="query", source_sql="SELECT 1", source_table=None)
    assert ProcessSpec.model_validate(minimal_spec(step)).steps[0].sharded is True


def test_config_keys_must_be_unique():
    second = dict(EXPORT, function_name="second_export")
    with pytest.raises(ValidationError, match="config key 'ftp' is already used"):
        ProcessSpec.model_validate(minimal_spec(EXPORT, second))
    ProcessSpec.model_validate(minimal_spec(EXPORT, dict(second, config_key="ftp_2")))


# ---------------------------------------------------------------- ingest / copy
def test_ingest_rules():
    ProcessSpec.model_validate(minimal_spec(INGEST))
    for bad, message in [
        ({"columns": []}, "add the file's columns"),
        ({"merge_keys": []}, "at least one key"),
        ({"merge_keys": ["nope"]}, "not in the schema"),
        ({"header": "first"}, "header must be None"),
        ({"archive_path": ""}, "enter the archive folder"),
        ({"separator": ";;"}, "single character"),
        ({"merge_filter": 'a """ b'}, "must not contain"),
    ]:
        with pytest.raises(ValidationError, match=message):
            ProcessSpec.model_validate(minimal_spec(dict(INGEST, **bad)))


def test_fixed_width_ingest_needs_every_column_length():
    fixed = dict(INGEST, file_format="FIXED", header="None",
                 columns=[{"name": "Client_Id", "type": "STRING", "width": 10}, {"name": "Created", "type": "DATETIME", "width": 19}])
    spec = ProcessSpec.model_validate(minimal_spec(fixed))
    assert spec.steps[0].record_length() == 29
    with pytest.raises(ValidationError, match="needs the length of every column: Created"):
        ProcessSpec.model_validate(minimal_spec(dict(fixed, columns=[fixed["columns"][0], {"name": "Created", "type": "DATETIME"}])))
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        ProcessSpec.model_validate(minimal_spec(dict(fixed, columns=[dict(fixed["columns"][0], width=0), fixed["columns"][1]])))
    with pytest.raises(ValidationError, match="skiprows must be None"):
        ProcessSpec.model_validate(minimal_spec(dict(fixed, skiprows="two")))
    ProcessSpec.model_validate(minimal_spec(dict(fixed, separator="ignored for fixed width")))


def test_copy_process_only_holds_copy_jobs():
    data = minimal_spec(COPY)
    data["process_type"] = "copy"
    assert ProcessSpec.model_validate(data).process_type == "copy"
    data = minimal_spec(COPY, INGEST)
    data["process_type"] = "copy"
    with pytest.raises(ValidationError, match="only has copy jobs: ingest_files"):
        ProcessSpec.model_validate(data)


def test_attached_sql_file_name_is_a_plain_name():
    sql = {"type": "sql", "function_name": "run_sql", "sql": "SELECT 1", "sql_file": "monthly build.sql"}
    assert ProcessSpec.model_validate(minimal_spec(sql)).steps[0].sql_file == "monthly build.sql"
    with pytest.raises(ValidationError, match="plain file name"):
        ProcessSpec.model_validate(minimal_spec(dict(sql, sql_file="../etc/x.sql")))


def test_only_one_ingest_step_per_process():
    with pytest.raises(ValidationError, match="only one file-ingestion step"):
        ProcessSpec.model_validate(minimal_spec(INGEST, dict(INGEST, function_name="ingest_more", config_key="gcs_config_2")))


def test_no_file_email_needs_the_email_on():
    with pytest.raises(ValidationError, match="needs the email turned on"):
        ProcessSpec.model_validate(minimal_spec(dict(INGEST, on_no_files="email")))


def test_copy_rules():
    spec = ProcessSpec.model_validate(minimal_spec(dict(COPY, sources=["gs://b/x_{date:%Y%m%d}.csv", ""], destination="/data/in/")))
    assert spec.steps[0].sources == ["gs://b/x_{date:%Y%m%d}.csv"]  # blank rows dropped
    for bad, message in [
        ({"sources": ["/local/a.csv"], "destination": "/local/b/"}, "at least one side must be a gs:// path"),
        ({"operation": "rsync", "sources": ["gs://a/x/", "gs://a/y/"]}, "exactly one source"),
        ({"operation": "rsync", "sources": ["gs://a/*.csv"]}, "remove the"),
        ({"delete_unmatched": True}, "only applies to rsync"),
        ({"destination": "~/x"}, "full local path"),
        ({"sources": ["gs://a/{nope}.csv"]}, "unknown placeholder"),
    ]:
        with pytest.raises(ValidationError, match=message):
            ProcessSpec.model_validate(minimal_spec(dict(COPY, **bad)))


def test_copy_direction_checks_each_side():
    from app.models.spec import copy_direction

    upload = dict(COPY, direction="local_to_bucket", sources=["/data/exports/members_{date:%Y%m%d}.csv"])
    assert ProcessSpec.model_validate(minimal_spec(upload)).steps[0].resolved_direction() == "local_to_bucket"
    download = dict(COPY, direction="bucket_to_local", destination="/data/inbound/")
    ProcessSpec.model_validate(minimal_spec(download))
    for bad, message in [
        (dict(upload, sources=["gs://drop-bucket/x.csv"]), "the source 1 must be a local path"),
        (dict(upload, sources=["exports/x.csv"]), "must be a full local path"),
        (dict(download, destination="gs://landing-bucket/in/"), "the destination must be a local path"),
        (dict(COPY, direction="bucket_to_bucket", destination="/data/in/"), "the destination must be a gs://bucket"),
    ]:
        with pytest.raises(ValidationError, match=message):
            ProcessSpec.model_validate(minimal_spec(bad))
    # processes made before 3.1 have no direction: any mix still validates, and the direction is derived for display
    legacy = ProcessSpec.model_validate(minimal_spec(dict(COPY, destination="/data/in/"))).steps[0]
    assert legacy.direction is None and legacy.resolved_direction() == "bucket_to_local"
    assert copy_direction(["/a/x", "gs://b/y"], "gs://c/") is None and copy_direction([], "gs://c/") is None


def test_date_token_only_where_the_script_formats_it():
    with pytest.raises(ValidationError, match=r"'\{date\}' is not available here"):
        ProcessSpec.model_validate(minimal_spec(dict(EXPORT, path="out/{date:%Y}")))


# ---------------------------------------------------------------- parameters
def test_config_parameters_need_a_value_and_a_free_name():
    sql = {"type": "sql", "function_name": "q", "sql": "SELECT @cutoff_date", "parameters": [
        {"name": "cutoff_date", "source": "config", "default": "2026-01-01"}]}
    assert "cutoff_date" in ProcessSpec.model_validate(minimal_spec(sql)).config_params()
    for bad, message in [({"default": ""}, "needs a value"), ({"name": "write_project"}, "script's own names")]:
        p = dict(sql["parameters"][0], **bad)
        with pytest.raises(ValidationError, match=message):
            ProcessSpec.model_validate(minimal_spec(dict(sql, parameters=[p])))


def test_old_run_date_sources_are_rejected():
    sql = {"type": "sql", "function_name": "q", "sql": "SELECT @d", "parameters": [{"name": "d", "source": "run_date"}]}
    with pytest.raises(ValidationError):
        ProcessSpec.model_validate(minimal_spec(sql))


# ---------------------------------------------------------------- email
def test_receivers_must_be_comma_separated():
    assert parse_receivers("a@x.com, b@x.com,a@x.com") == (["a@x.com", "b@x.com"], [])
    assert "commas, not semicolons" in parse_receivers("a@x.com; b@x.com")[1][0]
    assert "contains spaces" in parse_receivers("a@x.com b@x.com")[1][0]
    assert "not an email address" in parse_receivers("nobody")[1][0]


def test_email_settings_validate_only_when_enabled():
    assert not ProcessSpec.model_validate(minimal_spec(email={"enabled": False})).email.enabled
    ok = {"enabled": True, "receivers": {"lab": "me@example.com", "value": "team@example.com, me@example.com"}}
    spec = ProcessSpec.model_validate(minimal_spec(email=ok))
    assert spec.email.send_on.success and spec.email.send_on.failure
    for bad, message in [
        ({"receivers": {"lab": "me@example.com"}}, "add at least one recipient for value"),
        ({"receivers": {"lab": "a@x.com; b@x.com", "value": "c@x.com"}}, "commas, not semicolons"),
        ({"subject": "{oops}"}, "subject placeholders"),
        ({"message": 'a """ b'}, "must not contain"),
        ({"send_on": {"failure": False, "success": False}}, "choose when to send"),
        ({"attachments": ["gs://b/folder/"]}, "is a folder"),
    ]:
        with pytest.raises(ValidationError, match=message):
            ProcessSpec.model_validate(minimal_spec(email={**ok, **bad}))


def test_secrets_need_the_gsm_project():
    data = minimal_spec(secrets=[{"name": "pdw", "secret_id": "pdw-conn"}])
    with pytest.raises(ValidationError, match="variables.gsm_project: enter the lab"):
        ProcessSpec.model_validate(data)  # gsm_project is added automatically, but needs values
    data["variables"].append({"name": "gsm_project", "group": "project", "values": {"lab": "p-dev", "value": "p-prod"}})
    assert ProcessSpec.model_validate(data).secrets[0].version == "latest"


# ---------------------------------------------------------------- misc
def test_format_validation_errors_points_at_fields():
    step = {"type": "sql", "function_name": "Bad-Name", "sql": "SELECT 1"}
    try:
        ProcessSpec.model_validate(minimal_spec(step))
    except ValidationError as exc:
        errors = format_validation_errors(exc)
    assert errors[0]["path"] == "steps[0].function_name"
    assert "not a valid Python function name" in errors[0]["message"]


def test_templates_resolve_variables_and_today():
    out = resolve_template("gs://{bucket}/x_{date:%Y%m%d}.csv", {"bucket": "b"}, today=dt.date(2026, 1, 5))
    assert out == "gs://b/x_20260105.csv"
    assert template_problems("{write_project:%Y}", ["write_project"])  # formats only on {date}
    assert template_problems("x_{date:%Y}", []) and not template_problems("x_{date:%Y}", [], allow_date=True)


def test_spec_hash_ignores_timestamps_and_header_dates(sales_spec_dict):
    a = ProcessSpec.model_validate(sales_spec_dict)
    sales_spec_dict["metadata"].update(updated_at="2030-01-01T00:00:00", created_on="2030-01-01")
    assert ProcessSpec.model_validate(sales_spec_dict).spec_hash() == a.spec_hash()


@pytest.mark.parametrize(
    "script_name,ok",
    [("dl_script", True), ("dl_script.py", True), ("", True), ("json", False), ("yaml", False), ("modules", False),
     ("pandas", False), ("test_load", False), ("DL", False), ("dl-script", False)],
)
def test_script_name_rules(script_name, ok):
    data = minimal_spec()
    data["metadata"]["script_name"] = script_name
    if ok:
        spec = ProcessSpec.model_validate(data)
        assert spec.script_stem() == (script_name.removesuffix(".py") or "mini_process")
    else:
        with pytest.raises(ValidationError):
            ProcessSpec.model_validate(data)


# ---------------------------------------------------------------- v1/v2 → v3
V2 = {
    "spec_version": 2,
    "metadata": {"name": "old_proc", "description": "old"},
    "gcp": {"project_id": "acme-dev", "location": "US", "impersonate_service_account": None},
    "variables": [{"name": "mart_dataset", "value": "mart"}, {"name": "export_bucket", "value": "acme-exports"}],
    "runtime": {"run_date_default": "yesterday", "timezone": "UTC", "schedule_cron": "0 2 * * *"},
    "logging": {"level": "INFO"},
    "audit": {"enabled": True, "table": "{project_id}.ops.audit"},
    "secrets": [{"name": "smtp_password", "secret_id": "smtp-pw"}],
    "email": {"enabled": True, "sender": "x@acme.com", "to": ["a@acme.com"], "cc": ["b@acme.com"],
              "smtp": {"host": "smtp.acme.com", "username": "svc", "password_secret": "smtp_password"},
              "send_on": {"failure": True, "warning": True, "success": False}, "subject": "[{status}] {process} {run_date}",
              "attachments": {"gcs": ["gs://{export_bucket}/x_{run_date:%Y%m%d}.csv"], "local": []}},
    "steps": [
        {"type": "sql", "function_name": "build_mart", "sql": "SELECT * FROM `${project_id}.${mart_dataset}.t` WHERE d = @run_date",
         "statement_kind": "SELECT", "destination_table": "{project_id}.{mart_dataset}.out", "timeout_minutes": 60,
         "parameters": [{"name": "run_date", "bq_type": "DATE", "source": "run_date"}]},
        {"type": "export", "function_name": "export_mart", "source_kind": "table", "source_table": "{project_id}.{mart_dataset}.out",
         "destination_uri": "gs://{export_bucket}/daily/{run_date:%Y/%m/%d}/mart_{run_date:%Y%m%d}_*.csv",
         "overwrite_policy": "replace_matching", "write_success_marker": True},
    ],
}


def test_v2_specs_are_upgraded_with_notes():
    data, notes = upgrade_spec(copy.deepcopy(V2))
    assert data["spec_version"] == 3 and notes
    names = {v["name"]: v for v in data["variables"]}
    assert names["write_project"]["values"]["lab"] == "acme-dev" and "value" not in names["write_project"]["values"]
    sql = data["steps"][0]
    assert "${write_project}" in sql["sql"] and sql["destination_table"].startswith("{write_project}")
    assert sql["parameters"][0]["source"] == "today_offset" and sql["parameters"][0]["offset_days"] == -1
    export = data["steps"][1]
    assert (export["bucket"], export["path"], export["file_name"], export["date_suffix"], export["sharded"]) == (
        "{export_bucket}", "daily", "mart_", "%Y%m%d", True)
    assert data["secrets"] == []  # the SMTP password secret is gone
    assert data["email"]["receivers"]["lab"] == "a@acme.com, b@acme.com" and data["email"]["send_on"]["success"]
    assert data["email"]["attachments"] == ["gs://{export_bucket}/x_{date:%Y%m%d}.csv"]
    assert any("value (prod)" in n for n in notes)


def test_upgraded_spec_validates_once_prod_values_are_entered():
    data, _notes = upgrade_spec(copy.deepcopy(V2))
    with pytest.raises(ValidationError, match="enter the value"):
        ProcessSpec.model_validate(data)
    for v in data["variables"]:
        v["values"]["value"] = v["values"]["lab"].replace("dev", "prod") or "x"
    data["email"]["receivers"]["value"] = "team@acme.com"
    spec = ProcessSpec.model_validate(data)
    assert spec.spec_version == 3
    with pytest.raises(ValidationError, match="enter the value"):  # model_validate upgrades v2 on its own too
        ProcessSpec.model_validate(copy.deepcopy(V2))


# ---------------------------------------------------------------- the hub's default project (value-gcp in prod)
def test_new_and_opened_processes_get_the_prod_default_project_only_where_empty():
    from app.settings import Settings
    from app.ui.pages import _fill_defaults
    from app.ui.state import WizardState, blank_spec

    defaults = Settings().default_projects()
    assert defaults == {"value": "value-gcp"}
    spec = blank_spec("alice@example.com", defaults=defaults)
    assert {v["name"]: v["values"] for v in spec["variables"]} == {
        "read_project": {"lab": "", "value": "value-gcp", "qa": ""}, "write_project": {"lab": "", "value": "value-gcp", "qa": ""}}
    older = blank_spec("alice@example.com")
    older["variables"][1]["values"]["value"] = "other-prod"  # typed by someone: never replaced
    filled, _notes = _fill_defaults(older, defaults)
    assert [v["values"]["value"] for v in filled["variables"]] == ["value-gcp", "other-prod"]
    state = WizardState(spec=filled, user="alice@example.com")
    added = state.add_variables([{"name": "write_project", "values": {"lab": "lab-gcp", "value": "value-gcp"}},
                                 {"name": "mkt_project", "group": "project", "values": {"lab": "mkt", "value": "mkt", "qa": "mkt"}}])
    assert added == ["write_project", "mkt_project"]
    assert state.variable("write_project")["values"] == {"lab": "lab-gcp", "value": "other-prod", "qa": ""}
    assert state.variable("mkt_project")["values"] == {"lab": "mkt", "value": "mkt", "qa": "mkt"}
