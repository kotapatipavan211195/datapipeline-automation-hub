"""Lab test runs. The real runner is exercised end to end offline: the fake Google package (app/sandbox/fake_google)
is put first on PYTHONPATH, so `python ./<script>.py -cf … lab` runs exactly as in lab, just without GCP."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from app.models.spec import ProcessSpec
from app.services import test_runner as tr
from app.services.smoke_run import SANDBOX_DIR
from app.services.test_runner import TestRunManager, TestRunRefused, side_effects
from app.settings import Settings

from conftest import load_sample, minimal_spec

TESTER = "tester@example.com"


@pytest.fixture()
def offline(monkeypatch, tmp_path):
    """Run test runs against the fake Google clients; returns the path of their call log."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PYTHONPATH", str(SANDBOX_DIR))
    monkeypatch.setenv("DAHUB_SMOKE_LOG", str(log))
    monkeypatch.delenv("DAHUB_SMOKE_MANIFEST", raising=False)
    return log


def _wait(run, seconds: float = 60):
    deadline = time.monotonic() + seconds
    while run.active and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not run.active, "test run did not finish"
    return run


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def test_lab_test_run_passes_and_records_timings(offline):
    manager = TestRunManager(Settings())
    spec = ProcessSpec.model_validate(load_sample("strategic_template"))
    run = _wait(manager.start(spec, owner="me@x.com", name="strategic_template", user="me@x.com", adc_email=TESTER))
    assert run.status == "passed", "\n".join(run.lines)
    assert run.exit_code == 0 and set(run.timings) == {"build_prod_script_test", "extract_data_file_csv"}
    assert run.command == ("python ./dl_strategic_pipeline_template.py -cf ./dl_strategic_pipeline_template.yaml lab")
    assert any("Parms-> Environment: lab" in line for line in run.lines)
    calls = _calls(offline)
    queries = " ".join(c.get("sql", "") for c in calls if c["kind"] == "query")
    assert "demo-analytics-dev.lab_strategic.prod_script_test" in queries and "value-gcp" not in queries
    emails = [c for c in calls if c["kind"] == "email"]
    assert emails and all(e["to"] == TESTER for e in emails)  # only the tester gets the test email
    record = run.record()
    assert record["environment"] == "lab" and record["spec_hash"] == spec.spec_hash() and "lines" not in record
    assert manager.latest_for("me@x.com", "strategic_template") is run


def test_ingestion_and_copy_steps_run_in_the_test_too(offline):
    manager = TestRunManager(Settings())
    spec = ProcessSpec.model_validate(load_sample("engagement_file_ingest"))
    run = _wait(manager.start(spec, owner="me@x.com", name="engagement", user="me@x.com", adc_email=TESTER))
    assert run.status == "passed", "\n".join(run.lines)
    assert run.command.endswith("-ts ./mkt_engagement_ingest_table_schema.yaml lab")
    kinds = [c["kind"] for c in _calls(offline)]
    assert "gcloud" in kinds and "email" in kinds  # no file in the fake bucket: the "no file received" email goes out


def test_the_sandbox_yaml_holds_only_lab_and_the_tester(offline, monkeypatch):
    seen = {}
    real_spawn = tr._spawn

    def spy(cmd, cwd=None, **kw):
        seen["yaml"] = yaml.safe_load((Path(cwd) / "dl_strategic_pipeline_template.yaml").read_text(encoding="utf-8"))
        seen["modules"] = sorted(p.name for p in (Path(cwd) / "modules").glob("*.py"))
        seen["env"] = kw.get("env", {})
        return real_spawn(cmd, cwd=cwd, **kw)

    monkeypatch.setattr(tr, "_spawn", spy)
    spec = ProcessSpec.model_validate(load_sample("strategic_template"))
    _wait(TestRunManager(Settings()).start(spec, owner="a@x.com", name="p", user="a@x.com", adc_email=TESTER))
    assert list(seen["yaml"]["gcp_projects"]) == ["lab"]
    lab = seen["yaml"]["gcp_projects"]["lab"]
    assert lab["email"]["receiver"] == [TESTER] and lab["email"]["sender"] == TESTER
    assert seen["modules"] == ["__init__.py", "gsm_module.py", "read_config_yaml.py", "sql_logger.py"]
    assert seen["env"]["DAG_ID"] == "datapipeline-hub-test-run"


def test_real_modules_folder_is_used_when_configured(offline, monkeypatch, tmp_path):
    custom = tmp_path / "real_modules"
    vendored = Path(__file__).resolve().parents[1] / "app" / "vendor" / "modules"
    custom.mkdir()
    for f in vendored.glob("*.py"):
        (custom / f.name).write_text(f.read_text(encoding="utf-8") + "\n# real copy\n", encoding="utf-8")
    seen = {}
    real_spawn = tr._spawn

    def spy(cmd, cwd=None, **kw):
        seen["text"] = (Path(cwd) / "modules" / "sql_logger.py").read_text(encoding="utf-8")
        return real_spawn(cmd, cwd=cwd, **kw)

    monkeypatch.setattr(tr, "_spawn", spy)
    spec = ProcessSpec.model_validate(minimal_spec())
    run = _wait(TestRunManager(Settings(modules_dir=custom)).start(spec, owner="a@x.com", name="p", user="a@x.com", adc_email=TESTER))
    assert run.status == "passed" and "# real copy" in seen["text"]


def test_refuses_when_lab_would_write_where_prod_writes():
    data = load_sample("strategic_template")
    for v in data["variables"]:
        if v["name"] == "write_project":
            v["values"]["value"] = v["values"]["lab"]
    spec = ProcessSpec.model_validate(data)
    with pytest.raises(TestRunRefused, match="lab and prod share a write location: write_project"):
        TestRunManager(Settings()).start(spec, owner="a@x.com", name="p", user="a@x.com", adc_email=TESTER)


def test_refuses_email_without_an_adc_email_and_when_disabled():
    spec = ProcessSpec.model_validate(load_sample("strategic_template"))
    with pytest.raises(TestRunRefused, match="no email address"):
        TestRunManager(Settings()).start(spec, owner="a@x.com", name="p", user="a@x.com", adc_email="unknown")
    with pytest.raises(TestRunRefused, match="turned off"):
        TestRunManager(Settings(test_run_enabled=False)).start(spec, owner="a@x.com", name="p", user="a@x.com", adc_email=TESTER)


def _sleeping_popen(monkeypatch, code: str):
    def fake(cmd, cwd=None, **kw):
        return subprocess.Popen([sys.executable, "-u", "-c", code], cwd=cwd, **kw)

    monkeypatch.setattr(tr, "_spawn", fake)


def test_one_run_per_process_cancel_and_timeout(monkeypatch):
    _sleeping_popen(monkeypatch, "import time; print('Function step execution begin', flush=True); time.sleep(30)")
    manager = TestRunManager(Settings(test_run_timeout_seconds=2))
    spec = ProcessSpec.model_validate(minimal_spec())
    first = manager.start(spec, owner="a@x.com", name="p", user="a@x.com", adc_email=TESTER)
    with pytest.raises(TestRunRefused, match="already running"):
        manager.start(spec, owner="a@x.com", name="p", user="a@x.com", adc_email=TESTER)
    manager.cancel(first.id)
    assert _wait(first).status == "cancelled"
    second = _wait(manager.start(spec, owner="a@x.com", name="p", user="a@x.com", adc_email=TESTER))
    assert second.status == "timeout" and "Stopped after 2 s" in second.message


def test_a_failing_script_is_reported_with_its_last_line(monkeypatch):
    _sleeping_popen(monkeypatch, "print('Function run_report execution begin'); print('RuntimeError: 403 Access Denied'); raise SystemExit(1)")
    run = _wait(TestRunManager(Settings()).start(ProcessSpec.model_validate(minimal_spec()), owner="a@x.com", name="p",
                                                 user="a@x.com", adc_email=TESTER))
    assert run.status == "failed" and run.exit_code == 1 and "403 Access Denied" in run.message
    snap = run.snapshot(since=1)
    assert snap["lines"] == ["RuntimeError: 403 Access Denied"] and snap["next"] == 2


def test_side_effects_describe_lab_changes():
    spec = ProcessSpec.model_validate(load_sample("engagement_file_ingest"))
    effects = side_effects(spec, TESTER)
    assert any("demo-analytics-dev.clubwide.engagement_daily" in e for e in effects)
    assert any("moves (deletes) the source files" in e for e in effects)
    assert any(f"Email: sent only to {TESTER}" in e for e in effects)
    assert not any("value-gcp" in e for e in effects)


def test_the_log_file_has_a_summary_then_every_line(offline):
    manager = TestRunManager(Settings(test_run_max_log_lines=5))
    spec = ProcessSpec.model_validate(load_sample("strategic_template"))
    run = _wait(manager.start(spec, owner="me@x.com", name="strategic_template", user="me@x.com", adc_email=TESTER))
    text = run.log_file()
    assert run.log_name() == "dl_strategic_pipeline_template_lab_test_run.log"
    head = text.split("\n#\n")[0]
    assert head.startswith("# Lab test run of dl_strategic_pipeline_template.py (") and "# status:      passed (exit code 0)" in head
    assert f"# spec:        {spec.spec_hash()}" in head and "# step:        build_prod_script_test: " in head
    assert f"the first {run.line_count - 5} line(s) are not kept" in head  # the hub keeps the last 5 lines
    assert text.endswith(run.log_text())
