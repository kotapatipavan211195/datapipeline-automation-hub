"""Enhance a script: static scan of old scripts, conversion into a hub process, and the guarded Gemini rewrite."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.models.enhance import EnhanceHeader
from app.models.spec import ProcessSpec
from app.services.renderer import render_process
from app.services.script_convert import ConvertError, to_spec
from app.services.script_rewrite import rewrite_script
from app.services.script_scan import classify_yaml, scan_script
from app.settings import Settings

from fakes import FakeGemini

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = Settings(gcp_project="acme-dev", smoke_run_enabled=True)
LEGACY = (FIXTURES / "legacy_sql_extract.py").read_text(encoding="utf-8")
LEGACY_YAML = (FIXTURES / "legacy_sql_extract.yaml").read_text(encoding="utf-8")
JDBC = (FIXTURES / "legacy_jdbc_pull.py").read_text(encoding="utf-8")
JDBC_ENHANCED = (FIXTURES / "legacy_jdbc_pull_enhanced.py").read_text(encoding="utf-8")
SA_KEY = (FIXTURES / "legacy_sa_key.py").read_text(encoding="utf-8")
TODAY = dt.date(2026, 10, 15)
JDBC_YAML = ("gcp_projects:\n  lab:\n    write_project: acme-analytics-dev\n    pdw: {driver: x, url: y, user: u, password: p, jar: z}\n"
             "  value:\n    write_project: acme-analytics-prod\n    pdw: {driver: x, url: y, user: u, password: p, jar: z}\n")
PROPOSAL = {"script": JDBC_ENHANCED, "config_yaml": JDBC_YAML, "changes": ["Added the header, step timing and main(args)"],
            "todos": ["Move the PDW password to Secret Manager"]}


def _legacy():
    return scan_script(LEGACY, "dl_active_members.py", config_yaml=LEGACY_YAML, settings=SETTINGS)


def test_scan_reads_header_steps_sql_and_email():
    a = _legacy()
    assert a.convertible and a.blockers == []
    assert a.header["authors"] == ["P10000"] and a.header["created_on"] == "2025-11-02" and a.header["jira"] == ""
    assert a.header["updates"] == [{"date": "2026-02-10", "pid": "P10000", "note": "pipe-delimited extract"}]
    assert a.step_order == ["build_active_members", "extract_data_file_csv"]
    sql_fn, export_fn = a.steps()
    assert sql_fn.kind == "sql" and "`${write_project}.${membership_dataset}.ms_acq_dm_active_members`" in sql_fn.sql[0].text
    assert sql_fn.details["parameters"] == [{"name": "cutoff", "bq_type": "DATE", "source": "today_offset", "offset_days": -365}]
    assert export_fn.kind == "export" and export_fn.details["config_key"] == "ftp"
    assert export_fn.details["file_format"] == "TXT" and export_fn.details["field_delimiter"] == "|"
    assert export_fn.details["date_suffix"] == "%Y%m%d"
    assert a.email["success"] and not a.email["failure"] and a.email["blocks"] == ["ftp"]


def test_scan_reports_the_gaps_to_the_team_standard():
    gaps = {s.key: s.detail for s in _legacy().standards if not s.ok}
    assert gaps["header"] == "missing Reviewer(s), Usage"
    assert gaps["no_exit"] == "sys.exit() in extract_data_file_csv, send_email, stream_csv"
    assert gaps["projects_in_config"] == "hard-coded: acme-raw-dev"
    assert "jaydebeapi" in gaps["imports"] and gaps["no_print"] == "1 print() call(s)"
    assert "step_timing" in gaps and "argparse" not in gaps and "module_paths" not in gaps


def test_legacy_script_becomes_a_process_that_generates_and_runs():
    spec_dict, notes = to_spec(_legacy(), process_name="active_members", owner="alice@example.com")
    assert notes[0].startswith("Imported from dl_active_members.py: 2 step(s)")
    spec = ProcessSpec.model_validate(spec_dict)
    assert spec.metadata.script_name == "dl_active_members" and spec.metadata.authors == ["P10000"]
    values = {v.name: (v.values.lab, v.values.value) for v in spec.variables}
    assert values["read_project"] == ("acme-raw-dev", "acme-raw-prod")
    assert values["membership_dataset"] == ("membership_lab", "membership")
    assert values["ftp_bucket"] == ("acme-ftp-dev", "acme-ftp-prod")
    sql, export = spec.steps
    assert "`${read_project}.membership.members`" in sql.sql  # the hard-coded project became the config value
    assert sql.parameters[0].source == "today_offset" and sql.parameters[0].offset_days == -365
    assert (export.bucket, export.path, export.file_name) == ("{ftp_bucket}", "outbound/members", "ms_acq_dm_active_members_")
    assert export.file_format == "TXT" and export.field_delimiter == "|" and export.attach_to_email
    assert spec.email.enabled and spec.email.receivers.value == "membership-team@example.com, pipeline.developer@example.com"
    result = render_process(spec, smoke_run=True, settings=SETTINGS, today=dt.date(2026, 1, 1))
    assert all(c.ok for c in result.checks), [c for c in result.checks if not c.ok]


def test_scripts_with_source_system_code_are_not_convertible():
    a = scan_script(JDBC, "pdw_pull.py", settings=SETTINGS)
    assert not a.convertible
    assert any("pull_members(): uses a JDBC source-system connection" in b for b in a.blockers)
    with pytest.raises(ConvertError):
        to_spec(a, process_name="pdw_pull", owner="alice@example.com")


def test_unparsable_scripts_and_yaml_kinds():
    a = scan_script("def broken(:\n", "x.py", settings=SETTINGS)
    assert not a.convertible and a.parse_error and "does not parse" in a.blockers[0]
    assert classify_yaml(LEGACY_YAML) == "config"
    assert classify_yaml("table_definition:\n  table_name: t\n  table_schema: {a: STRING}\n") == "schema"
    bad = scan_script(LEGACY, "x.py", config_yaml="just: text\n", settings=SETTINGS)
    assert not bad.convertible and "no gcp_projects block" in bad.blockers[0]


def test_rewrite_is_accepted_when_every_guard_passes():
    a = scan_script(JDBC, "pdw_pull.py", settings=SETTINGS)
    result = rewrite_script(a, JDBC, FakeGemini(PROPOSAL), settings=SETTINGS)
    assert result.ok, result.problems
    assert sorted(result.files) == ["README.md", "pdw_pull.py", "pdw_pull.yaml", "requirements.txt"]
    assert "JayDeBeApi" in result.files["requirements.txt"] and "pyarrow" in result.files["requirements.txt"]
    assert "py ./pdw_pull.py -cf ./pdw_pull.yaml lab" in result.files["README.md"]
    assert "Move the PDW password to Secret Manager" in result.files["README.md"]
    assert "+    logger.info(f'Function {pull_members.__name__} execution begin')" in result.diff
    after = {s.key: s.ok for s in result.standards_after}
    assert after["step_timing"] and after["argparse"] and after["module_paths"]


@pytest.mark.parametrize("change, guard", [
    (lambda s: s.replace("select * from `{write_project}.staging.members`", "select * except(x) from `{write_project}.staging.members`"),
     "SQL unchanged"),
    (lambda s: s.replace("def main(args):", "def main(args)"), "python syntax"),
    (lambda s: s.replace("def pull_members(conn_info):", "def pull(conn_info):").replace("pull_members(conn_info)\n", "pull(conn_info)\n")
     .replace("{pull_members.__name__}", "{pull.__name__}"), "functions kept"),
    (lambda s: s.replace("import time\n", ""), "ruff lint (E9, F)"),
    (lambda s: s.replace("import google.auth\n", "from google.oauth2 import service_account\n").replace(
        "credentials, project_id = google.auth.default()",
        "credentials = service_account.Credentials.from_service_account_file('/data/keys/pdw-sa.json')\n"
        "project_id = credentials.project_id"), "ADC credentials"),
])
def test_rewrite_is_rejected_when_a_guard_fails(change, guard):
    a = scan_script(JDBC, "pdw_pull.py", settings=SETTINGS)
    result = rewrite_script(a, JDBC, FakeGemini(dict(PROPOSAL, script=change(JDBC_ENHANCED))), settings=SETTINGS)
    assert not result.ok and result.files == {}
    assert any(p.startswith(guard) for p in result.problems), result.problems


def test_rewrite_needs_gemini():
    a = scan_script(JDBC, "pdw_pull.py", settings=SETTINGS)
    result = rewrite_script(a, JDBC, None, settings=SETTINGS)
    assert not result.ok and "needs Gemini" in result.problems[0]


def test_scan_reads_older_header_spellings_and_comment_headers():
    a = scan_script(SA_KEY, "dl_claims_summary.py", settings=SETTINGS)
    h = a.header
    assert (h["authors"], h["reviewers"], h["created_on"]) == (["P55555"], ["P66666"], "2023-03-15")
    assert h["description"] == "Refreshes the daily claims summary for the claims dashboard."
    assert h["updates"] == [{"date": "2024-06-01", "pid": "P55555", "note": "added the claim_type column"}]
    header = {s.key: s for s in a.standards}["header"]
    assert not header.ok and header.detail.startswith("missing Author(s)")  # the team's labels are still asked for
    doc_only = scan_script('"""Pulls members from PDW.\n\nMore text."""\nimport os\n', "x.py", settings=SETTINGS)
    assert doc_only.header["description"] == "Pulls members from PDW." and doc_only.header["authors"] == []
    assert scan_script("# Copyright ACME\nimport os\n", "x.py", settings=SETTINGS).header == {}


def test_service_account_access_is_found_and_the_process_uses_adc():
    a = scan_script(SA_KEY, "dl_claims_summary.py", settings=SETTINGS)
    assert a.credentials == ["line 16: GOOGLE_APPLICATION_CREDENTIALS set in the code (a key file)",
                             "line 22: a service-account key file (from_service_account_file)"]
    credentials = {s.key: s for s in a.standards}["credentials"]
    assert not credentials.ok and "from_service_account_file" in credentials.detail
    assert a.convertible and a.step_order == ["build_claims_summary"]  # get_client() is no step
    assert a.function("get_client").kind == "auth"
    assert any(n.startswith("get_client() builds credentials or clients") for n in a.notes)
    spec_dict, notes = to_spec(a, process_name="claims_summary", owner="alice@example.com", today=TODAY)
    assert any(n.startswith("Credentials: the old script did not use ADC") for n in notes)
    for v in spec_dict["variables"]:
        v["values"].update(lab="acme-claims-dev", value="acme-claims-prod")
    result = render_process(ProcessSpec.model_validate(spec_dict), smoke_run=True, settings=SETTINGS, today=TODAY)
    assert all(c.ok for c in result.checks), [c for c in result.checks if not c.ok]
    script = result.files["dl_claims_summary.py"]
    assert "credentials, project_id = google.auth.default()" in script
    assert "service_account" not in script and "GOOGLE_APPLICATION_CREDENTIALS" not in script


def test_convert_attaches_the_header_confirmed_on_the_enhance_page():
    a = scan_script(SA_KEY, "dl_claims_summary.py", settings=SETTINGS)
    header = EnhanceHeader.from_analysis(a)
    assert header.update_pid == "P55555" and header.authors == ["P55555"]
    header = header.model_copy(update={"authors": ["P55555", "P77777"], "jira": "DE-1234", "update_pid": "P77777",
                                       "update_note": "Moved to the team standard, ADC credentials"})
    spec_dict, _notes = to_spec(a, process_name="claims_summary", owner="alice@example.com", header=header, today=TODAY)
    md = spec_dict["metadata"]
    assert (md["authors"], md["reviewers"], md["jira"], md["created_on"]) == (["P55555", "P77777"], ["P66666"], "DE-1234",
                                                                              "2023-03-15")
    assert md["updates"] == [{"date": "2024-06-01", "pid": "P55555", "note": "added the claim_type column"},
                             {"date": "2026-10-15", "pid": "P77777", "note": "Moved to the team standard, ADC credentials"}]
    for v in spec_dict["variables"]:
        v["values"].update(lab="acme-claims-dev", value="acme-claims-prod")
    script = render_process(ProcessSpec.model_validate(spec_dict), smoke_run=False, settings=SETTINGS,
                            today=TODAY).files["dl_claims_summary.py"]
    assert script.startswith("'''\n    Author(s):      P55555, P77777\n    Reviewer(s):    P66666\n"
                             "    Created on:     2023-03-15\n    JIRA/SN #:      DE-1234\n")
    assert "[2026-10-15, P77777]: Moved to the team standard, ADC credentials" in script


@pytest.mark.parametrize("keep_gemini_header", [True, False])
def test_rewrite_gets_the_hub_header_block(keep_gemini_header):
    """The hub writes the header from the confirmed values, whether Gemini kept a header (replaced) or dropped it."""
    a = scan_script(JDBC, "pdw_pull.py", settings=SETTINGS)
    script = JDBC_ENHANCED if keep_gemini_header else JDBC_ENHANCED[JDBC_ENHANCED.index("# Base imports"):]
    header = EnhanceHeader.from_analysis(a).model_copy(update={"reviewers": ["P22222"], "update_pid": "P99999"})
    result = rewrite_script(a, JDBC, FakeGemini(dict(PROPOSAL, script=script)), settings=SETTINGS, header=header,
                            today=TODAY)
    assert result.ok, result.problems
    out = result.files["pdw_pull.py"]
    assert out.startswith("'''\n    Author(s):      P12345\n    Reviewer(s):    P22222\n    Created on:     2024-05-01\n"
                          "    JIRA/SN #:      DE-77\n")
    assert out.count("Author(s):") == 1 and "TODO: reviewer PID(s)" not in out
    assert "Steps (main() runs them in this order):" in out and "1. pull_members: Read the member table from PDW" in out
    assert "py ./pdw_pull.py -cf ./pdw_pull.yaml lab    (lab = dev)" in out
    assert "[2026-10-15, P99999]: Enhanced to the team script standard" in out
    assert "Enhanced by Datapipeline Automation Hub 3.4.0 on 2026-10-15" in out and "from pdw_pull.py" in out
    assert {s.key: s.ok for s in result.standards_after}["header"]
    assert "| Reviewer(s) | P22222 |" in result.files["README.md"]


def test_rewrite_readme_lists_the_credentials_it_replaced():
    a = scan_script(JDBC, "pdw_pull.py", settings=SETTINGS)
    a.credentials = ["line 3: a service-account key file (from_service_account_file)"]
    result = rewrite_script(a, JDBC, FakeGemini(PROPOSAL), settings=SETTINGS, today=TODAY)
    assert result.ok, result.problems
    readme = result.files["README.md"]
    assert "## Credentials" in readme and "line 3: a service-account key file" in readme


# ---------------------------------------------------------------- API
@pytest.fixture()
def api(tmp_path):
    from fastapi.testclient import TestClient

    from app.deps import Services
    from app.main import create_app
    from app.services.sql_analyzer import SqlAnalyzer
    from app.services.validators import ClientFactory
    from app.services.workspace import LocalBackend, WorkspaceStore

    settings = Settings(auth_mode="local", dev_user_email="alice@example.com", local_workspace_dir=tmp_path / "ws",
                        smoke_run_enabled=False, gemini_enabled=False, log_format="text", ui_enabled=False)
    services = Services(settings=settings, store=WorkspaceStore(LocalBackend(tmp_path / "ws")), gemini=FakeGemini(PROPOSAL),
                        analyzer=SqlAnalyzer(None), clients=ClientFactory())
    return TestClient(create_app(settings, services))


def test_enhance_api_analyze_convert_and_rewrite(api):
    body = {"file_name": "dl_active_members.py", "source": LEGACY, "config_yaml": LEGACY_YAML}
    analysis = api.post("/api/enhance/analyze", json=body).json()
    assert analysis["convertible"] and analysis["step_order"] == ["build_active_members", "extract_data_file_csv"]
    converted = api.post("/api/enhance/convert", json=dict(body, process_name="active_members")).json()
    assert converted["spec"]["metadata"]["owner"] == "alice@example.com" and converted["notes"]
    created = api.post("/api/processes", json={"spec": converted["spec"]})
    assert created.status_code == 201, created.text

    jdbc = {"file_name": "pdw_pull.py", "source": JDBC}
    r = api.post("/api/enhance/convert", json=dict(jdbc, process_name="pdw_pull"))
    assert r.status_code == 422 and "rewrite" in r.json()["detail"]["message"]
    rewritten = api.post("/api/enhance/rewrite", json=jdbc).json()
    assert rewritten["ok"] and "pdw_pull.py" in rewritten["files"]
    assert api.post("/api/enhance/convert", json=dict(body, process_name="Bad Name")).status_code == 422
