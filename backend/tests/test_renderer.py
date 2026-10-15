from __future__ import annotations

import ast
import datetime as dt
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.models.spec import ProcessSpec
from app.services.renderer import RenderError, build_config_dict, render_main_preview, render_process
from app.settings import Settings

from conftest import load_sample, minimal_spec

GOLDEN_DIR = Path(__file__).parent / "golden"
FIXED_DAY = dt.date(2026, 1, 1)
SAMPLES = ["sales_daily", "customer_snapshot", "strategic_template", "engagement_file_ingest", "fixed_width_ingest",
           "file_copy", "excel_ingest_export", "custom_geocode", "raw_file_ingest"]
ADC = "pipeline.developer@example.com"


def _render(spec_dict: dict, **kw):
    kw.setdefault("adc_email", ADC)
    kw.setdefault("smoke_run", False)
    return render_process(ProcessSpec.model_validate(spec_dict), today=FIXED_DAY, **kw)


def _main_calls(script: str) -> list[str]:
    """Names of the step functions main() calls, in order (direct calls and attachments.append(step(...)))."""
    tree = ast.parse(script)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    skip = {"round", "get", "info", "exception", "warning", "CaptureSQL", "read_configuration", "read_table_definition",
            "perf_counter", "email", "append", "join", "get_gsm_secret", "RuntimeError"}
    found = []
    for node in ast.walk(main):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name and name not in skip:
                found.append((node.lineno, name))
    return list(dict.fromkeys(name for _line, name in sorted(found)))


@pytest.mark.parametrize("sample", SAMPLES)
def test_golden_files(sample):
    """Rendered output must match tests/golden/<sample>/ exactly. Refresh with UPDATE_GOLDEN=1 after intended changes."""
    result = _render(load_sample(sample))
    folder = GOLDEN_DIR / sample
    if os.environ.get("UPDATE_GOLDEN") == "1":
        for rel, content in result.files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text(content, encoding="utf-8")
    expected = {
        str(p.relative_to(folder)): p.read_text(encoding="utf-8")
        for p in folder.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert sorted(expected) == sorted(result.files)
    for rel, content in result.files.items():
        assert content == expected[rel], f"{sample}/{rel} differs from golden (UPDATE_GOLDEN=1 to accept)"


@pytest.mark.parametrize("sample", SAMPLES)
def test_generated_script_runs_end_to_end_offline(sample):
    """The offline smoke run executes `python ./<script>.py -cf ./<script>.yaml [-ts …] lab` against fake Google clients."""
    result = _render(load_sample(sample), smoke_run=True)
    names = {c.name: c for c in result.checks}
    assert names["offline smoke run"].ok, names["offline smoke run"].detail
    assert names["ruff lint"].ok and names["embedded SQL round-trip"].ok and names["config YAML round-trip"].ok


# ---------------------------------------------------------------- the team's script template
def test_script_follows_the_team_template(strategic_spec_dict):
    script = _render(strategic_spec_dict).files["dl_strategic_pipeline_template.py"]
    lines = script.splitlines()
    assert lines[0] == "'''" and lines[1].startswith("    Author(s):      P10000")
    for label in ("Reviewer(s):", "Created on:     2026-10-13", "JIRA/SN #:      DE-1234", "Description:",
                  "Usage:          py ./dl_strategic_pipeline_template.py -cf ./dl_strategic_pipeline_template.yaml lab    "
                  "(lab = dev)",
                  "                py ./dl_strategic_pipeline_template.py -cf ./dl_strategic_pipeline_template.yaml value  "
                  "(value = prod)",
                  "Updates:        [YYYY-MM-DD, PID]:"):
        assert f"    {label}" in script, label
    for expected in (
        "# Base imports",
        "# Module paths -do not alter unless you know what you are doing!",
        "sys.path.insert(0, '/data/py_scripts')",
        "sys.path.insert(0, '/data/data_eng/py_scripts')",
        "from modules import read_config_yaml as config",
        "# from modules import gsm_module as gsm  # TODO: Developer uncomment out here if you are using this for source system connections",
        "from modules import sql_logger",
        "credentials, project_id = google.auth.default()",
        "bq_client = bigquery.Client(project=project_id, credentials=credentials)",
        '"LOG_FORMAT": "%(asctime)s - %(levelname)s - %(message)s"',
        'logging.basicConfig(format=CONFIG["LOG_FORMAT"], level=CONFIG["LOG_LEVEL"])',
        'logger = logging.getLogger(CONFIG["SCRIPT_NAME"])',
        "logger.info(f'Function {build_prod_script_test.__name__} execution begin')",
        'logger.info(f"Function {build_prod_script_test.__name__} execution end:  {execution_time} seconds.")',
        "        s = str(e)\n        raise RuntimeError(s)",
        "def main(args):",
        "logger.info(f'Parms-> Environment: {args.environment}')",
        'capture = sql_logger.CaptureSQL(project_id, credentials, CONFIG["SCRIPT_NAME"], environment)',
        "config_data = config.read_configuration(environment, config_file)",
        'if __name__ == "__main__":',
        "choices=['lab', 'value', 'qa']",
        "default='dl_strategic_pipeline_template.yaml'",
    ):
        assert expected in script, expected
    assert "--run-date" not in script and "RunContext" not in script and "-ts" not in script.split("argparse.ArgumentParser")[1]
    assert len(lines) < 400


def test_main_calls_steps_in_spec_order_and_comments_out_disabled(customer_spec_dict):
    script = _render(customer_spec_dict).files["dl_customer_snapshot.py"]
    assert _main_calls(script) == [
        "load_crm_customer_extract", "merge_customer_dimension", "publish_customer_snapshot",
        "export_customer_contacts_txt", "copy_contacts_to_share",
    ]
    assert "# refresh_customer_counts(write_project, data_sets)" in script
    assert "def refresh_customer_counts(write_project, data_sets):" in script  # still generated, ready to re-enable
    assert "            publish_customer_snapshot(config_data)\n        except Exception:" in script  # on_failure=continue
    assert 'raise RuntimeError(f"Step(s) failed: {\', \'.join(failed_steps)}")' in script


def test_reordering_steps_reorders_main(sales_spec_dict):
    sales_spec_dict["steps"] = [sales_spec_dict["steps"][i] for i in (1, 0, 3, 2)]
    script = _render(sales_spec_dict).files["sales_daily.py"]
    assert _main_calls(script) == [
        "build_sales_daily_agg", "ingest_store_sales_files", "check_sales_daily_quality", "export_sales_daily_to_finance",
    ]
    defs = [n.name for n in ast.parse(script).body if isinstance(n, ast.FunctionDef)]
    assert defs.index("build_sales_daily_agg") < defs.index("ingest_store_sales_files")


def test_sql_is_embedded_with_plain_locals(sales_spec_dict):
    result = _render(sales_spec_dict)
    assert not any(p.endswith(".sql") for p in result.files)
    script = result.files["sales_daily.py"]
    assert "def build_sales_daily_agg(write_project, reference_project, data_sets, min_amount):" in script
    assert "mart_dataset = data_sets.get('mart_dataset')" in script
    assert "MERGE `{write_project}.{mart_dataset}.sales_daily` T" in script
    assert "r'^[A-Z]{{3}}\\d+$'" in script and 'sql = rf"""' in script  # literal braces doubled, backslash kept
    assert "reference_project = config_data.get('reference_project')" in script  # main reads it once


# ---------------------------------------------------------------- YAML
def test_config_yaml_has_one_block_per_environment(sales_spec_dict):
    spec = ProcessSpec.model_validate(sales_spec_dict)
    text = _render(sales_spec_dict).files["sales_daily.yaml"]
    data = yaml.safe_load(text)
    assert data == build_config_dict(spec, sender=ADC)
    assert list(data["gcp_projects"]) == ["lab", "value"]
    lab, prod = data["gcp_projects"]["lab"], data["gcp_projects"]["value"]
    assert lab["write_project"] == "acme-analytics-dev" and prod["write_project"] == "acme-analytics-prod"
    assert lab["data_sets"] == {"raw_dataset": "raw_sales", "mart_dataset": "sales_mart"}
    assert lab["min_amount"] == "0.01"  # config parameter
    assert lab["gcs_config"]["gcs_read_bucket"] == "acme-landing-dev" and prod["gcs_config"]["gcs_read_bucket"] == "acme-landing-prod"
    assert lab["datasets"] == {"temporary": "raw_sales", "target": "raw_sales"} and lab["write_disposition"] == "WRITE_APPEND"
    assert lab["ftp"] == {"bucket": "acme-exports-dev", "path": "finance/sales_daily", "file_name": "sales_daily_"}
    assert lab["email"] == {"sender": ADC, "receiver": ["retail-analytics-dev@acme.example.com"], "attachment": []}
    assert "# Configuration for the 'lab' (dev) project" in text and "# change accordingly" in text
    assert "run_date_default" not in text and "logging" not in text and "audit" not in text


def test_qa_block_only_when_enabled():
    data = minimal_spec(environments={"qa_enabled": True})
    for v in data["variables"]:
        v["values"]["qa"] = v["values"]["lab"] + "-qa"
    assert list(yaml.safe_load(_render(data).files["mini_process.yaml"])["gcp_projects"]) == ["lab", "value", "qa"]


def test_table_schema_yaml_for_ingestion(ingest_spec_dict):
    result = _render(ingest_spec_dict)
    assert result.schema_file == "mkt_engagement_ingest_table_schema.yaml"
    schema = yaml.safe_load(result.files[result.schema_file])["table_definition"]
    assert schema["table_name"] == "engagement" and schema["table_schema"]["Created"] == "DATETIME"
    script = result.files["mkt_engagement_ingest.py"]
    assert "default='mkt_engagement_ingest_table_schema.yaml'" in script
    assert "table_def = config.read_table_definition(schema_file)" in script
    for fn in ("def process_file(", "def load_to_bq(", "def merge_to_main_table(", "def archive_data_file("):
        assert fn in script
    assert "PARTITION BY `Client_Id`" in script and "ORDER BY `Created` DESC" in script
    assert "WHERE upper(Campaign) NOT LIKE '%UAT%'" in script
    assert 'email(config_data, environment, "NO FILE", message=f"There is no {filename} file received today.")' in script


# ---------------------------------------------------------------- parameters
def test_undefined_query_parameter_fails_render():
    step = {"type": "sql", "function_name": "q", "sql": "SELECT * FROM `${ds}.t` WHERE d = @report_date"}
    with pytest.raises(RenderError, match="@report_date but no such parameter"):
        _render(minimal_spec(step))


def test_unused_parameter_is_dropped_with_warning():
    step = {"type": "sql", "function_name": "q", "sql": "SELECT 1 AS x",
            "parameters": [{"name": "report_date", "bq_type": "DATE", "source": "today"}]}
    result = _render(minimal_spec(step))
    assert any("not used" in w for w in result.warnings)
    assert "ScalarQueryParameter" not in result.files["mini_process.py"]


def test_parameter_derivations_render_to_python():
    step = {
        "type": "sql", "function_name": "q",
        "sql": "SELECT @a AS a, @b AS b, @c AS c, @d AS d, @e AS e",
        "parameters": [
            {"name": "a", "bq_type": "DATE", "source": "today_offset", "offset_days": -7},
            {"name": "b", "bq_type": "DATE", "source": "prev_month_start"},
            {"name": "c", "bq_type": "INT64", "source": "config", "default": "5"},
            {"name": "d", "bq_type": "STRING", "source": "today"},
            {"name": "e", "bq_type": "STRING", "source": "process_name"},
        ],
    }
    script = _render(minimal_spec(step)).files["mini_process.py"]
    assert "'a', 'DATE', date.today() + timedelta(days=-7)" in script
    assert "'b', 'DATE', (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)" in script
    assert "bigquery.ScalarQueryParameter('c', 'INT64', c)" in script and "def q(c):" in script
    assert "c = config_data.get('c')" in script
    assert "'d', 'STRING', date.today().isoformat()" in script
    assert "'e', 'STRING', 'mini_process'" in script
    assert "from datetime import datetime, date, timedelta" in script


def test_dq_checks_cannot_use_parameters():
    step = {"type": "dq", "function_name": "check_it", "checks": [{"kind": "row_count_min", "table": "{ds}.t", "where": "d = @other"}]}
    with pytest.raises(RenderError, match="cannot use @parameters"):
        _render(minimal_spec(step))


def test_main_preview_accepts_partial_steps():
    code = render_main_preview(
        [
            {"function_name": "load_files", "description": "Load", "type": "ingest"},
            {"function_name": "build_mart", "enabled": False, "type": "sql", "sql": "SELECT * FROM `${write_project}.${mart_dataset}.t`"},
            {"function_name": "export_mart", "on_failure": "continue", "type": "export"},
        ],
        [{"name": "write_project", "group": "project"}, {"name": "mart_dataset", "group": "dataset"}],
    )
    assert "load_files(config_data, table_def)" in code
    assert "# build_mart(write_project, data_sets)" in code
    assert "export_mart(config_data)\n        except Exception:" in code
    assert "# 2. export" in code


# ---------------------------------------------------------------- hub settings
def test_sql_logger_is_a_hub_setting(strategic_spec_dict):
    on = _render(strategic_spec_dict).files["dl_strategic_pipeline_template.py"]
    off = _render(strategic_spec_dict, settings=Settings(sql_logger_enabled=False)).files["dl_strategic_pipeline_template.py"]
    assert 'capture.write_to_log(build_prod_script_test.__name__, sql, "success", job_id)' in on
    assert "sql_logger" not in off and "capture" not in off


def test_email_relay_and_sender_come_from_the_hub(strategic_spec_dict):
    result = _render(strategic_spec_dict)
    script = result.files["dl_strategic_pipeline_template.py"]
    assert "smtp_server = 'smtp.example.com'" in script and "smtp_port = 25" in script and "server.starttls()" in script
    assert "server.login" not in script and 'message["Cc"]' not in script
    assert 'message["To"] = ", ".join(receiver_email)' in script
    assert "attachments.append(extract_data_file_csv(config_data))" in script
    assert 'email(config_data, environment, "SUCCESS", attachments=attachments)' in script
    lab = yaml.safe_load(result.files["dl_strategic_pipeline_template.yaml"])["gcp_projects"]["value"]
    assert lab["email"]["sender"] == ADC
    assert lab["email"]["receiver"] == ["pipeline.developer@example.com", "analytics-team@example.com"]
    relay = _render(strategic_spec_dict, settings=Settings(smtp_host="relay.example.com", smtp_port=2525)).files[
        "dl_strategic_pipeline_template.py"]
    assert "smtp_server = 'relay.example.com'" in relay and "smtp_port = 2525" in relay


def test_module_paths_and_environments_are_hub_settings():
    settings = Settings(module_paths=["/opt/modules"], environments=["lab", "value"])
    script = _render(minimal_spec(), settings=settings).files["mini_process.py"]
    assert "sys.path.insert(0, '/opt/modules')" in script and "/data/py_scripts" not in script
    assert "choices=['lab', 'value']" in script


# ---------------------------------------------------------------- copy, secrets, layout
def test_copy_step_uses_gcloud_storage():
    step = {"type": "copy", "function_name": "move_files", "operation": "mv", "recursive": True, "no_clobber": True,
            "sources": ["gs://drop/out/f_{date:%Y%m%d}*.csv"], "destination": "gs://landing/in/"}
    result = _render(minimal_spec(step))
    script = result.files["mini_process.py"]
    assert 'gcloud = shutil.which("gcloud")' in script
    assert "cmd = [gcloud, \"storage\", 'mv', '--recursive', '--no-clobber', *sources, destination]" in script
    assert "sources = [source.format(date=date.today()) for source in file_copy.get('sources', [])]" in script
    assert "gsutil" not in script.replace("successor of gsutil", "")
    block = yaml.safe_load(result.files["mini_process.yaml"])["gcp_projects"]["lab"]["file_copy"]
    assert block == {"sources": ["gs://drop/out/f_{date:%Y%m%d}*.csv"], "destination": "gs://landing/in/"}


def test_secrets_use_the_team_gsm_module():
    data = minimal_spec(secrets=[{"name": "pdw", "secret_id": "pdw-conn", "version": "3"}])
    data["variables"].append({"name": "gsm_project", "group": "project", "values": {"lab": "gsm-dev", "value": "gsm-prod"}})
    result = _render(data)
    script = result.files["mini_process.py"]
    assert "from modules import gsm_module as gsm\n" in script
    assert "secret_conn = gsm.google_secret(credentials, gsm_project)" in script
    assert "logger.info(f'Function {get_gsm_secret.__name__} execution begin: {secret} (version {version})')" in script
    assert 'raise RuntimeError(f"Could not retrieve secret {secret}. Reason: {e}") from e' in script
    assert "SECRETS['pdw'] = get_gsm_secret(gsm_project, 'pdw-conn', '3')" in script
    assert yaml.safe_load(result.files["mini_process.yaml"])["gcp_projects"]["value"]["gsm_project"] == "gsm-prod"
    assert "SECRETS['<name>']` holds the whole payload" in result.files["README.md"]


def test_the_secret_reader_hands_the_steps_the_whole_payload():
    """get_gsm_secret returns the whole payload (JSON as a dict, else the text): the steps pick the keys they need
    (api_credentials → api today, other keys tomorrow). An empty payload fails; the payload is never logged."""
    data = minimal_spec(secrets=[{"name": "geo", "secret_id": "geo"}])
    data["variables"].append({"name": "gsm_project", "group": "project", "values": {"lab": "gsm-dev", "value": "gsm-prod"}})
    tree = ast.parse(_render(data).files["mini_process.py"])
    reader = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "get_gsm_secret")
    payloads = {"geo": '{"api_credentials": [{"api": "k-123"}]}', "token": "plain-token", "empty": "  "}

    class Secret:  # the team's gsm_module.google_secret
        def __init__(self, credentials, project):
            self.project = project

        def set_secret_name(self, name):
            self.name = name

        def set_version(self, version):
            self.version = version

        def get_gsm_secret(self):
            return SimpleNamespace(payload=SimpleNamespace(data=payloads[self.name].encode("utf-8")))

    logged: list[str] = []
    ns = {"gsm": SimpleNamespace(google_secret=Secret), "credentials": None, "time": time, "json": json,
          "logger": SimpleNamespace(info=logged.append, error=logged.append)}
    exec(compile(ast.Module(body=[reader], type_ignores=[]), "get_gsm_secret", "exec"), ns)
    get_gsm_secret = ns["get_gsm_secret"]
    payload = get_gsm_secret("gsm-dev", "geo")
    assert payload == {"api_credentials": [{"api": "k-123"}]}
    assert payload["api_credentials"][0].get("api") == "k-123"  # how a custom step builds on it
    assert get_gsm_secret("gsm-dev", "token") == "plain-token"
    with pytest.raises(RuntimeError, match="Could not retrieve secret empty. Reason: Secret empty has an empty payload"):
        get_gsm_secret("gsm-dev", "empty")
    assert any("execution end" in m for m in logged)
    assert not any("k-123" in m or "plain-token" in m for m in logged)


def test_script_name_sets_the_file_layout_and_run_command():
    data = minimal_spec()
    data["metadata"]["script_name"] = "dl_script"
    result = _render(data)
    assert result.script == "dl_script" and result.process == "mini_process"
    assert sorted(result.files) == ["README.md", "dl_script.py", "dl_script.yaml", "requirements.txt"]
    for env in ("lab", "value"):  # the environment comes last
        assert f"py ./dl_script.py -cf ./dl_script.yaml {env}" in result.files["dl_script.py"]
        assert f"py ./dl_script.py -cf ./dl_script.yaml {env}" in result.files["README.md"]
    assert "Run:  py ./dl_script.py -cf ./dl_script.yaml <environment>" in result.files["dl_script.yaml"]


def test_txt_export_writes_a_dated_pipe_delimited_file():
    export = {"type": "export", "function_name": "export_txt", "source_table": "{write_project}.{ds}.cust", "bucket": "out-bucket",
              "file_name": "cust_", "file_format": "TXT", "field_delimiter": "|", "compression": "GZIP"}
    script = _render(minimal_spec(export), smoke_run=True).files["mini_process.py"]
    assert "field_delimiter='|'" in script and "destination_format=bigquery.DestinationFormat.CSV" in script
    assert 'extract_file_name = f"{extract_file_name}{extract_filename_suffix}.txt.gz"' in script
    assert "extract_filename_suffix = date.today().strftime('%Y_%m_%d')" in script


def test_adc_identity_is_recorded_in_the_script_yaml_and_readme():
    result = _render(minimal_spec())
    assert f"ADC identity at generation: {ADC}" in result.files["mini_process.py"]
    assert f"# ADC identity at generation: {ADC}" in result.files["mini_process.yaml"]
    assert f"| ADC identity at generation | `{ADC}`" in result.files["README.md"]


def test_fixed_width_files_are_split_by_the_column_widths():
    """Run the generated process_file() on real fixed-width lines: values land in the right columns, typed."""
    import decimal
    import io
    import logging
    import uuid
    from types import SimpleNamespace

    import pandas as pd

    result = _render(load_sample("fixed_width_ingest"))
    schema = yaml.safe_load(result.files["dl_member_fixed_width_ingest_table_schema.yaml"])["table_definition"]
    assert schema["column_widths"] == {"member_id": 10, "member_name": 30, "status_code": 2, "join_date": 10,
                                       "balance": 12, "visits": 5}
    tree = ast.parse(result.files["dl_member_fixed_width_ingest.py"])
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "process_file")
    namespace = {"pd": pd, "io": io, "ast": ast, "decimal": decimal, "uuid": uuid, "os": os,
                 "logger": logging.getLogger("t"), "CONFIG": {"EXEC_DATETIME": dt.datetime(2026, 1, 1)}}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "process_file", "exec"), namespace)  # noqa: S102
    lines = ("0000000001Ann Smith                     A 2026-01-15      125.50   12\n"
             "0000000002Bob Jones                     I 2025-12-01        0.00    3\n")
    blob = SimpleNamespace(name="member_extract/MBR_EXTRACT_20260114.txt", download_as_text=lambda: lines)
    widths = [schema["column_widths"][c] for c in schema["table_schema"]]
    df = namespace["process_file"](blob, schema["table_schema"], widths, "None", "None")
    first = df.iloc[0]
    assert first["member_id"] == "0000000001" and first["member_name"] == "Ann Smith" and first["status_code"] == "A"
    assert first["join_date"] == dt.date(2026, 1, 15) and first["balance"] == decimal.Decimal("125.50")
    assert int(first["visits"]) == 12 and len(df) == 2 and first["file_name"] == "MBR_EXTRACT_20260114.txt"


def test_copy_only_scripts_have_no_sql_logger():
    result = _render(load_sample("file_copy"))
    script = result.files["dl_vendor_file_copy.py"]
    assert "sql_logger" not in script and "capture" not in script.replace("capture_output", "")
    assert script.count("gcloud storage") >= 2
    assert "sql_logger" in _render(load_sample("strategic_template")).files["dl_strategic_pipeline_template.py"]

