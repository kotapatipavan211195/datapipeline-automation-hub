"""The hub's copies of the team modules behave like the originals, and read what the generated bundles write."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.models.spec import ProcessSpec
from app.services.renderer import render_process, write_bundle
from app.vendor.modules import gsm_module, read_config_yaml, sql_logger

from conftest import load_sample


def _bundle(tmp_path, sample: str):
    result = render_process(ProcessSpec.model_validate(load_sample(sample)), adc_email="me@example.com",
                            today=dt.date(2026, 1, 1), smoke_run=False)
    write_bundle(result.files, tmp_path)
    return result


def test_read_configuration_returns_one_environment(tmp_path):
    result = _bundle(tmp_path, "strategic_template")
    lab = read_config_yaml.read_configuration("lab", str(tmp_path / f"{result.script}.yaml"))
    value = read_config_yaml.read_configuration("value", str(tmp_path / f"{result.script}.yaml"))
    assert lab["write_project"] == "demo-analytics-dev" and value["write_project"] == "value-gcp"
    assert lab["data_sets"]["strategic_dataset"] == "lab_strategic"
    assert value["ftp"] == {"bucket": "value-gcp-insights", "path": "outbound/rpm", "file_name": "ms_acq_dm_active_members_"}
    with pytest.raises(RuntimeError):
        read_config_yaml.read_configuration("qa", str(tmp_path / f"{result.script}.yaml"))


def test_read_table_definition(tmp_path):
    result = _bundle(tmp_path, "engagement_file_ingest")
    table = read_config_yaml.read_table_definition(str(tmp_path / result.schema_file))
    assert table["table_name"] == "engagement"
    assert list(table["table_schema"])[:3] == ["Created", "Drip_Id", "Client_Id"]


def test_capture_sql_writes_parameterized_rows(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, project=None, credentials=None):
            self.project = project

        def query(self, query, job_config):
            sent.append((query, {p.name: p.value for p in job_config.query_parameters}))
            return SimpleNamespace(result=lambda: [])

    monkeypatch.setattr(sql_logger.bigquery, "Client", FakeClient)
    capture = sql_logger.CaptureSQL("proj-dev", None, "dl_x.py", "lab")
    capture.write_to_log("step_one", "SELECT 1", "SUCCESS", "job-1")
    capture.write_to_log("step_two", "SELECT 2", "failure")
    assert "INSERT INTO `proj-dev.pipeline_audit.pipeline_sql_log`" in sent[0][0]
    assert sent[0][1]["status"] == "success" and sent[0][1]["sql_step_order"] == 1 and sent[1][1]["sql_step_order"] == 2
    assert sent[1][1]["job_id"] is None and sent[0][1]["environment"] == "lab"
    with pytest.raises(ValueError, match="Invalid value for status"):
        capture.write_to_log("x", "SELECT 3", "maybe")


def test_google_secret_reads_the_requested_version(monkeypatch):
    asked = []

    class FakeSecretClient:
        def __init__(self, credentials=None):
            pass

        def access_secret_version(self, request):
            asked.append(request["name"])
            return SimpleNamespace(name=request["name"], payload=SimpleNamespace(data=b'{"user": "svc"}'))

    monkeypatch.setattr(gsm_module.secretmanager, "SecretManagerServiceClient", FakeSecretClient)
    secret = gsm_module.google_secret(None, "gsm-dev")
    secret.set_secret_name("pdw-conn")
    secret.set_version("latest")
    response = secret.get_gsm_secret()
    assert asked == ["projects/gsm-dev/secrets/pdw-conn/versions/latest"]
    assert response.payload.data == b'{"user": "svc"}' and secret.get_project_id() == "gsm-dev"
