"""Custom process steps: the model rules, the generated function, Gemini's code + the hub's checks, and lab test runs."""

from __future__ import annotations

import ast
import copy
import datetime as dt

import pytest
from pydantic import ValidationError

from app.models.spec import ProcessSpec, format_validation_errors
from app.services import test_runner as tr
from app.services.custom_step import CustomCodeProposal, apply_proposal, check_custom_step, write_custom_code
from app.services.gemini import GeminiError, Usage
from app.services.renderer import render_custom_preview, render_process
from app.services.test_runner import TestRunManager, side_effects
from app.settings import Settings

from conftest import load_sample, minimal_spec

SETTINGS = Settings(gcp_project="test", sql_logger_enabled=False)
SECRETS = [{"name": "db2_credentials", "secret_id": "db2-lab-credentials", "description": "user and password"}]
GSM = {"name": "gsm_project", "group": "project", "values": {"lab": "sec-dev", "value": "sec-prod"}}
DB2_CODE = """\
credentials_db2 = SECRETS['db2_credentials']
conn = ibm_db.connect(
    f"DATABASE={db2_config['database']};HOSTNAME={db2_config['host']};PORT={db2_config['port']};"
    f"UID={credentials_db2['user']};PWD={credentials_db2['password']}", "", "")
try:
    stmt = ibm_db.exec_immediate(conn, "SELECT COUNT(*) FROM MEMBERS")
    logger.info(f"DB2 members: {ibm_db.fetch_tuple(stmt)[0]}")
finally:
    ibm_db.close(conn)
"""


def custom(**over) -> dict:
    step = {"type": "custom", "function_name": "count_db2_members", "description": "Count the DB2 members",
            "config_key": "db2_config", "request": "Count the members in DB2",
            "settings": [{"key": "host", "value": "{db2_host}"}, {"key": "port", "value": "50000"},
                         {"key": "database", "value": "MEMBERS"}],
            "imports": ["import ibm_db"], "requirements": ["ibm_db>=3.2"], "code": DB2_CODE}
    step.update(over)
    return step


def spec_dict(*steps: dict, **over) -> dict:
    variables = minimal_spec()["variables"] + [GSM, {"name": "db2_host", "group": "other",
                                                      "values": {"lab": "db2-lab.example.com", "value": "db2.example.com"}}]
    return minimal_spec(*(steps or (custom(),)), variables=variables, secrets=SECRETS, **over)


def errors(data: dict) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        ProcessSpec.model_validate(data)
    return [e["message"] for e in format_validation_errors(exc.value)]


# ====================================================================== model
def test_custom_step_validates_and_fills_its_yaml_block():
    spec = ProcessSpec.model_validate(spec_dict())
    result = render_process(spec, smoke_run=False, adc_email="dev@example.com", settings=SETTINGS)
    lab, prod = result.files["mini_process.yaml"].split("\n  value:\n")
    assert "    db2_config:\n      host: db2-lab.example.com\n      port: '50000'\n      database: MEMBERS\n" in lab
    assert "      host: db2.example.com\n" in prod


@pytest.mark.parametrize(("code", "message"), [
    ("x = SECRETS['other']", "secret(s) the process does not define: other"),
    ("x = db2_config['user']", "setting(s) user that are not in the step's settings"),
    ("print('hi')", "not print()"),
    ("import sys\nsys.exit(1)", "do not call sys.exit()"),
    ("logger.info(f\"pw {SECRETS['db2_credentials']}\")", "never log secrets"),
    ("t = 'proj-dev.dataset_a.members'", "hard-coded 'proj-dev'"),
    ("p = 'gs://bucket/in/'", "hard-coded GCS path"),
    ("x = (", "not valid Python"),
    ("", "write the code"),
])
def test_custom_code_rules(code, message):
    found = errors(spec_dict(custom(code=code)))
    assert any(message in e for e in found), found


def test_custom_step_settings_imports_and_names():
    found = errors(spec_dict(custom(settings=[{"key": "host", "value": ""}], code="x = 1",
                                    imports=["from x import *"], requirements=["not a requirement!"])))
    text = " ".join(found)
    assert "enter the value of setting(s) host" in text and "no '*'" in text and "package>=1.2" in text
    assert any("pick another" in e for e in errors(spec_dict(custom(config_key="logger"))))  # would shadow the logger
    assert any("name of a step function" in e for e in errors(spec_dict(custom(config_key="count_db2_members"))))


# ====================================================================== generated function
def test_custom_function_is_wrapped_in_the_team_standard():
    result = render_process(ProcessSpec.model_validate(spec_dict()), smoke_run=False, settings=SETTINGS)
    script = result.files["mini_process.py"]
    fn = next(n for n in ast.parse(script).body if isinstance(n, ast.FunctionDef) and n.name == "count_db2_members")
    src = ast.get_source_segment(script, fn)
    assert "logger.info(f'Function {count_db2_members.__name__} execution begin')" in src
    assert "execution end:  {execution_time} seconds." in src and "raise RuntimeError(s)" in src
    assert "db2_config = config_data.get('db2_config', {})" in src
    assert "\nimport ibm_db\n" in script.split("# Module paths")[0]  # with the base imports
    assert "ibm_db>=3.2" in result.files["requirements.txt"]
    assert "count_db2_members(config_data)" in script.split("def main(args)")[1]


def test_settings_line_only_when_the_code_reads_them_and_imports_are_not_doubled():
    step = custom(settings=[], code="logger.info(json.dumps({'ok': requests.__name__}))", imports=["import json", "import requests"],
                  requirements=[])
    script = render_process(ProcessSpec.model_validate(spec_dict(step)), smoke_run=False, settings=SETTINGS).files["mini_process.py"]
    assert "db2_config = config_data" not in script  # unused → no line (ruff F841)
    head = script.split("# Module paths")[0]
    assert head.count("import json\n") == 1 and "import requests\n" in head


def test_requirements_fall_back_to_the_package_of_each_import():
    step = custom(settings=[], code="logger.info(requests.__name__)", imports=["import requests"], requirements=[])
    result = render_process(ProcessSpec.model_validate(spec_dict(step)), smoke_run=False, settings=SETTINGS)
    assert "requests>=2.31" in result.files["requirements.txt"]


def test_smoke_run_does_not_execute_custom_code():
    result = render_process(ProcessSpec.model_validate(spec_dict()), smoke_run=True, settings=SETTINGS)
    smoke = {c.name: c for c in result.checks}["offline smoke run"]
    assert smoke.ok, smoke.detail
    assert "custom code of count_db2_members is not run offline" in smoke.detail
    assert "ibm_db" in result.files["mini_process.py"] and "ibm_db" not in result.smoke_files["mini_process.py"]


def test_readme_says_why_a_bundle_is_not_tested():
    spec = ProcessSpec.model_validate(spec_dict())
    readme = render_process(spec, smoke_run=False, settings=SETTINGS, untested_reason="DB2 is not reachable from the hub").files["README.md"]
    assert "> **Not tested in lab:** DB2 is not reachable from the hub" in readme
    assert "| Custom code | written by hand from: Count the members in DB2 |" in readme
    plain = render_process(spec, smoke_run=False, settings=SETTINGS).files["README.md"]
    assert "Not tested in lab" not in plain


def test_preview_shows_the_wrapper_for_partial_steps():
    out = render_custom_preview({"function_name": "pull", "config_key": "src", "code": "x = src['a']\nlogger.info(x)"})
    assert "def pull(config_data):" in out and "src = config_data.get('src', {})" in out and "        x = src['a']" in out
    assert "pass  # your code" in render_custom_preview({})


# ====================================================================== Gemini
class Gemini:
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def generate_structured(self, prompt, schema, **_):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer), Usage()


GOOD = {"code": "url = geo_config['url']\nresponse = requests.get(url, timeout=30)\nlogger.info(response.status_code)",
        "imports": ["import requests"], "requirements": ["requests>=2.31"],
        "settings": [{"key": "url", "description": "endpoint", "example": "https://maps.example.com"}],
        "notes": ["create the API key secret"]}


def geo_spec() -> dict:
    return spec_dict(custom(function_name="call_api", config_key="geo_config", request="Call the maps API", code="",
                            settings=[], imports=[], requirements=[]))


def test_gemini_code_is_checked_and_applied():
    gemini = Gemini(GOOD)
    r = write_custom_code(geo_spec(), 0, gemini, SETTINGS)
    assert r.ok and r.attempts == 1 and r.notes == ["create the API key secret"]
    assert r.step["code"].startswith("url = geo_config['url']") and r.step["generated_by"] == SETTINGS.gemini_model
    assert r.step["settings"] == [{"key": "url", "value": "", "description": "endpoint (e.g. https://maps.example.com)"}]
    prompt = gemini.prompts[0]
    assert "<request>\nCall the maps API\n</request>" in prompt and "SECRETS['db2_credentials']" in prompt
    assert "def call_api(config_data):" in prompt and "geo_config = config_data.get('geo_config', {})" in prompt
    assert ("- SECRETS['db2_credentials'] (secret id db2-lab-credentials): its whole payload; it holds user and password"
            in prompt)  # never values
    assert "SECRETS (dict: secret name → the secret's whole payload" in prompt


HYBRID_CODE = """\
secret_list_key = geo_config.get('secret_list_key', 'api_credentials')
secret_value_key = geo_config.get('secret_value_key', 'api')
api_key = SECRETS['geo_key'][secret_list_key][0].get(secret_value_key)
if not api_key:
    raise ValueError("API key not found in secret payload. Check secret structure.")
response = requests.get(geo_config['url'], params={"key": api_key}, timeout=30)
logger.info(f"maps API answered {response.status_code}")
"""


def test_secret_payload_keys_are_settings_with_todays_keys_as_defaults():
    """The secret holds its whole payload; the code reads the payload keys (api_credentials → api) from the step's
    settings, so a new layout of the secret is a YAML change."""
    from app.services.custom_step import PROMPT_PATH

    rules = PROMPT_PATH.read_text(encoding="utf-8")
    assert "WHOLE payload" in rules and "geocode_config.get('secret_list_key', 'api_credentials')" in rules
    settings = [{"key": "secret_list_key", "value": "api_credentials"}, {"key": "secret_value_key", "value": "api"},
                {"key": "url", "value": "https://maps.example.com"}]
    step = custom(function_name="call_api", config_key="geo_config", code=HYBRID_CODE, settings=settings,
                  imports=["import requests"], requirements=["requests>=2.31"])
    data = spec_dict(step)
    data["secrets"] = [{"name": "geo_key", "secret_id": "maps-api", "description": "api_credentials → api"}]
    assert check_custom_step(data, 0, SETTINGS) == []
    lab = render_process(ProcessSpec.model_validate(data), smoke_run=False, settings=SETTINGS).files["mini_process.yaml"]
    assert "secret_list_key: api_credentials" in lab and "secret_value_key: api" in lab
    data["steps"][0]["settings"] = settings[2:]  # the keys must be settings, not only defaults in the code
    assert any("secret_list_key, secret_value_key that are not in the step's settings" in p
               for p in check_custom_step(data, 0, SETTINGS))


def test_gemini_gets_one_retry_with_the_failed_checks():
    bad = {**GOOD, "imports": []}  # requests undefined → ruff F821
    gemini = Gemini(bad, GOOD)
    r = write_custom_code(geo_spec(), 0, gemini, SETTINGS)
    assert r.ok and r.attempts == 2
    assert "F821 Undefined name `requests`" in gemini.prompts[1] and "Your previous code" in gemini.prompts[1]


def test_gemini_code_that_keeps_failing_is_handed_back_with_the_problems():
    bad = {**GOOD, "code": "print(geo_config['url'])"}
    r = write_custom_code(geo_spec(), 0, Gemini(bad, bad), SETTINGS)
    assert not r.ok and r.attempts == 2 and r.step["code"] == "print(geo_config['url'])"
    assert any("not print()" in p for p in r.problems)


def test_gemini_off_errors_and_values_already_entered():
    assert "Gemini is off" in write_custom_code(geo_spec(), 0, None, SETTINGS).problems[0]
    assert "describe what the step" in write_custom_code(spec_dict(custom(request="")), 0, Gemini(), SETTINGS).problems[0]
    r = write_custom_code(geo_spec(), 0, Gemini(GeminiError("quota")), SETTINGS)
    assert not r.ok and "quota" in r.problems[0]
    step = {"settings": [{"key": "url", "value": "https://kept", "description": "mine"}]}
    out = apply_proposal(step, CustomCodeProposal.model_validate(GOOD), "m")
    assert out["settings"] == [{"key": "url", "value": "https://kept", "description": "mine"}]


def test_check_ignores_the_rest_of_an_unfinished_process():
    data = geo_spec()
    data["steps"][0].update(code=GOOD["code"], imports=GOOD["imports"], settings=[{"key": "url", "value": ""}])
    data["steps"].insert(0, {"type": "sql", "function_name": "unfinished", "sql": ""})  # not valid yet
    data["variables"][0]["values"]["value"] = ""
    assert check_custom_step(data, 1, SETTINGS) == []
    data["steps"][1]["code"] = "x = undefined_name"
    assert any("F821" in p for p in check_custom_step(data, 1, SETTINGS))


# ====================================================================== lab test runs
def test_missing_packages_make_the_lab_test_unavailable(monkeypatch):
    manager = TestRunManager(Settings(gcp_project="test"))
    spec = ProcessSpec.model_validate(spec_dict(custom(imports=["import dahub_no_such_package"], requirements=[],
                                                       code="logger.info(dahub_no_such_package.x)", settings=[])))
    reasons = manager.unavailable_reasons(spec, adc_email="dev@example.com")
    assert any("needs package(s) the hub's Python does not have: dahub_no_such_package" in r for r in reasons)
    present = ProcessSpec.model_validate(spec_dict(custom(imports=["import json"], code="logger.info(json.dumps(1))",
                                                          settings=[], requirements=[])))
    assert not any("needs package" in r for r in manager.unavailable_reasons(present, adc_email="dev@example.com"))
    assert any("Custom code: count_db2_members runs for real in lab" in e
               for e in side_effects(ProcessSpec.model_validate(spec_dict()), "dev@example.com"))


def test_importable_asks_another_interpreter_once(monkeypatch):
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    monkeypatch.setattr(tr, "_IMPORTABLE", {})
    assert tr.importable("/other/python", "ibm_db") and tr.importable("/other/python", "ibm_db")
    assert len(calls) == 1 and calls[0][0] == "/other/python"


def test_the_sample_custom_step_renders():
    spec = ProcessSpec.model_validate(copy.deepcopy(load_sample("custom_geocode")))
    result = render_process(spec, smoke_run=False, today=dt.date(2026, 1, 1))
    assert all(c.ok for c in result.checks)
