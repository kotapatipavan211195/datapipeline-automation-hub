"""Ingestion without a target table (raw rows stay in staging), several files per run, and the email's file summary."""

from __future__ import annotations

import copy
import datetime as dt

import pytest
import yaml
from pydantic import ValidationError

from app.models.spec import ProcessSpec
from app.services import smoke_run
from app.services.lineage import process_tables
from app.services.renderer import render_process
from app.services.validators import SpecValidator
from app.settings import Settings
from app.ui.state import blank_spec, new_step

from conftest import load_sample, minimal_spec

SETTINGS = Settings(gcp_project="test", sql_logger_enabled=False)


def sample(name: str, **step_changes) -> dict:
    data = copy.deepcopy(load_sample(name))
    for step in data["steps"]:
        if step["type"] == "ingest":
            step.update(step_changes)
    return data


def render(data: dict, *, smoke: bool = False):
    return render_process(ProcessSpec.model_validate(data), smoke_run=smoke, today=dt.date(2026, 1, 1),
                          adc_email="dev@example.com", settings=SETTINGS)


@pytest.fixture()
def smoke_records(monkeypatch) -> list[dict]:
    """The calls the fake Google clients record during smoke runs."""
    records: list[dict] = []
    original = smoke_run._read_log

    def capture(path):
        out = original(path)
        records.extend(out)
        return out

    monkeypatch.setattr(smoke_run, "_read_log", capture)
    return records


# ====================================================================== model
def test_without_a_target_no_merge_keys_or_target_dataset_are_needed():
    data = sample("raw_file_ingest", history=False, target_dataset="", merge_keys=[], load_mode="merge")
    spec = ProcessSpec.model_validate(data)
    assert spec.steps[0].uses_target_dataset() is False
    with pytest.raises(ValidationError, match="a merge needs at least one key column"):
        ProcessSpec.model_validate(sample("raw_file_ingest", target_enabled=True, load_mode="merge", merge_keys=[]))


def test_new_fields_at_their_default_keep_the_spec_hash():
    data = load_sample("sales_daily")
    before = ProcessSpec.model_validate(data).spec_hash()
    explicit = copy.deepcopy(data)
    explicit["email"]["file_summary"] = False
    for step in explicit["steps"]:
        if step["type"] == "ingest":
            step["target_enabled"] = True
    assert ProcessSpec.model_validate(explicit).spec_hash() == before


def test_new_steps_keep_raw_rows_and_new_processes_list_their_files():
    assert new_step("ingest", [])["target_enabled"] is False
    assert blank_spec("alice@example.com")["email"]["file_summary"] is True


# ====================================================================== generated code without a target
def test_raw_ingestion_code_has_no_target_and_appends_this_runs_files():
    result = render(sample("raw_file_ingest", history=False))
    script = result.files["dl_member_payments_raw.py"]
    assert "def merge_to_main_table" not in script and "def insert_to_main_table" not in script
    assert "target_table" not in script and 'config_data["datasets"]["target"]' not in script
    assert 'staging_disposition = "WRITE_TRUNCATE"' in script and 'staging_disposition = "WRITE_APPEND"' in script
    assert "write_disposition=staging_disposition" in script
    assert "empty_staging_table(write_project, dataset_id_1, table_name)" in script
    assert "from google.api_core.exceptions import NotFound" in script
    lab = yaml.safe_load(result.files["dl_member_payments_raw.yaml"])["gcp_projects"]["lab"]
    assert lab["datasets"] == {"temporary": "membership_raw_lab"}
    assert all(c.ok for c in result.checks)


def test_a_run_that_fails_without_files_does_not_empty_the_staging_table():
    script = render(sample("raw_file_ingest", on_no_files="fail")).files["dl_member_payments_raw.py"]
    assert "empty_staging_table" not in script and "NotFound" not in script


def test_raw_ingestion_smoke_run_loads_both_files_into_one_staging_table(smoke_records):
    result = render(sample("raw_file_ingest"), smoke=True)
    assert {c.name: c for c in result.checks}["offline smoke run"].ok
    staging = [r for r in smoke_records if r["kind"] == "load_dataframe" and r["table"].endswith(".member_payments_raw")]
    assert [r["write_disposition"] for r in staging] == ["WRITE_TRUNCATE", "WRITE_APPEND"]
    assert not [r for r in smoke_records if r["kind"] == "query" and "MERGE" in r["sql"]]


def test_replace_mode_truncates_the_target_once_per_run(smoke_records):
    result = render(load_sample("customer_snapshot"), smoke=True)
    assert {c.name: c for c in result.checks}["offline smoke run"].ok
    truncates = [r for r in smoke_records if r["kind"] == "query" and r["sql"].startswith("TRUNCATE TABLE")]
    inserts = [r for r in smoke_records if r["kind"] == "query" and "INSERT INTO" in r["sql"] and "customer_extract" in r["sql"]]
    assert len(truncates) == 1 and len(inserts) == 2  # two files: replace once, then add both


def test_files_are_loaded_oldest_upload_first():
    script = render(load_sample("sales_daily")).files["sales_daily.py"]
    assert "qualified_files.sort(key=lambda f: (f.updated, f.name))" in script


# ====================================================================== lineage and checks
def test_lineage_and_checks_follow_the_target_switch():
    spec = ProcessSpec.model_validate(sample("raw_file_ingest"))
    inv = process_tables(spec)
    written = {t.resolved: t.how for t in inv.targets}
    assert written["demo-analytics-dev.membership_raw_lab.member_payments_raw"] == [
        "staging: this run's files (raw rows for the next steps)"]
    assert "demo-analytics-dev.membership_raw_lab.member_payments_raw_history" in written
    report = SpecValidator(settings=SETTINGS).validate(spec, gcp_checks=False)
    ids = {i.id for i in report.items}
    assert "ingest_member_payments:raw" in ids and "ingest_member_payments:dedupe" not in ids
    readme = render(sample("raw_file_ingest")).files["README.md"]
    assert "| Raw rows | kept in `demo-analytics-dev.membership_raw_lab.member_payments_raw` for the next steps" in readme


# ====================================================================== the email's file summary
def test_the_summary_is_only_in_scripts_that_ask_for_it():
    off = render(load_sample("sales_daily")).files["sales_daily.py"]
    assert "FILE_SUMMARY" not in off
    on = render(sample("raw_file_ingest")).files["dl_member_payments_raw.py"]
    assert "FILE_SUMMARY = []" in on and 'message = f"{message}\\n\\nFiles processed:\\n"' in on


def test_the_email_lists_each_file_its_rows_and_where_it_went(smoke_records):
    result = render(sample("raw_file_ingest"), smoke=True)
    assert {c.name: c for c in result.checks}["offline smoke run"].ok
    body = next(r for r in smoke_records if r["kind"] == "email")["body"]
    assert "Files processed:\n" in body
    for day in ("20260115", "20260116"):
        assert f"- Member Payments_{day}.csv: 2 rows loaded into demo-analytics-dev.membership_raw_lab.member_payments_raw" in body


def test_the_email_lists_merged_rows_and_every_kind_of_export(smoke_records):
    export = {"type": "export", "bucket": "ftp-dev", "path": "out", "file_name": "daily_", "date_suffix": ""}
    steps = [
        {**export, "function_name": "export_table", "source_kind": "table", "source_table": "{write_project}.{ds}.t"},
        {**export, "function_name": "export_query", "source_kind": "query", "source_sql": "SELECT 1 AS x",
         "config_key": "ftp_2", "file_name": "query_"},
        {**export, "function_name": "export_excel", "source_kind": "table", "source_table": "{write_project}.{ds}.t",
         "file_format": "XLSX", "config_key": "ftp_3", "file_name": "book_"},
    ]
    data = minimal_spec(*steps, email={"enabled": True, "receivers": {"lab": "dev@example.com", "value": "team@example.com"},
                                       "file_summary": True})
    result = render(data, smoke=True)
    assert {c.name: c for c in result.checks}["offline smoke run"].ok, result.checks
    body = next(r for r in smoke_records if r["kind"] == "email")["body"]
    assert "- 2 rows of proj-dev.dataset_a.t exported to gs://ftp-dev/out/daily_.csv" in body
    assert "- 2 rows exported to gs://ftp-dev/out/query_-*.csv" in body
    assert "- 0 rows exported to gs://ftp-dev/out/book_.xlsx" in body  # the fake table has no rows to read
    merged = render(sample("excel_ingest_export"), smoke=True)
    assert {c.name: c for c in merged.checks}["offline smoke run"].ok
    body = [r for r in smoke_records if r["kind"] == "email"][-1]["body"]
    assert "rows merged into demo-analytics-dev." in body
