from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.deps import Services
from app.main import create_app
from app.services.sql_analyzer import SqlAnalyzer
from app.services.validators import ClientFactory
from app.services.workspace import LocalBackend, WorkspaceStore
from app.settings import Settings

from conftest import load_sample
from fakes import FakeBq, FakeGcs

OWNER = "alice@example.com"
OTHER = "bob@example.com"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(auth_mode="local", dev_user_email=OWNER, local_workspace_dir=tmp_path / "ws",
                        smoke_run_enabled=False, gemini_enabled=False, log_format="text", ui_enabled=False)
    services = Services(
        settings=settings,
        store=WorkspaceStore(LocalBackend(tmp_path / "ws")),
        gemini=None,
        analyzer=SqlAnalyzer(None),
        clients=ClientFactory(bq=FakeBq(datasets={"acme-analytics-dev.raw_sales", "acme-analytics-dev.pipeline_ops"}),
                              gcs=FakeGcs({"acme-landing-dev": {"sales/incoming/store_sales_20260114.csv": b"store_id,sku\n1,a\n"}})),
    )
    return TestClient(create_app(settings, services))


def as_user(email):
    return {"x-dev-user": email}


def test_me_and_options(client):
    me = client.get("/api/me").json()
    assert me["email"] == OWNER and me["workspace"] == "local" and me["gemini"]["enabled"] is False
    assert me["adc"]["kind"] == "off"  # tests never look up real credentials
    assert me["environments"] == ["lab", "value", "qa"] and me["test_runs"]["environment"] == "lab"
    assert me["email_relay"] == "smtp.example.com:25"
    opts = client.get("/api/options").json()
    assert [m["value"] for m in opts["ingest_load_modes"]] == ["merge", "append", "truncate"]
    assert [m["value"] for m in opts["copy_operations"]] == ["cp", "mv", "rsync"]
    assert "run_date" not in json.dumps(opts)


def test_full_lifecycle_create_draft_version_clone(client):
    spec = load_sample("sales_daily")
    r = client.post("/api/processes", json={"spec": spec})
    assert r.status_code == 201
    # autosave an invalid draft: allowed, errors returned for the UI
    broken = json.loads(json.dumps(spec))
    broken["steps"][0]["function_name"] = "Bad Name"
    r = client.put(f"/api/processes/{OWNER}/sales_daily/draft", json={"spec": broken})
    assert r.status_code == 200 and r.json()["errors"][0]["path"] == "steps[0].function_name"
    # save a version
    r = client.post(f"/api/processes/{OWNER}/sales_daily/versions", json={"spec": spec, "note": "first"})
    assert r.status_code == 201, r.text
    assert r.json()["version"]["version"] == 1 and r.json()["version"]["lab_test"] is None
    # listing
    listed = client.get("/api/processes?scope=mine").json()
    assert listed[0]["name"] == "sales_daily" and listed[0]["latest_version"] == 1
    # get + files + zip
    got = client.get(f"/api/processes/{OWNER}/sales_daily").json()
    assert got["can_edit"] and "sales_daily.py" in got["latest"]["files"]
    assert got["latest"]["spec"]["metadata"]["created_on"] == "2026-01-05"  # kept from the spec (set on first save otherwise)
    text = client.get(f"/api/processes/{OWNER}/sales_daily/versions/1/files/sales_daily.py").text
    assert "def main(" in text
    z = client.get(f"/api/processes/{OWNER}/sales_daily/versions/1/zip")
    assert zipfile.ZipFile(io.BytesIO(z.content)).namelist()[0].startswith("sales_daily/")
    # another user can view and clone, but not save
    got_b = client.get(f"/api/processes/{OWNER}/sales_daily", headers=as_user(OTHER)).json()
    assert got_b["can_edit"] is False
    r = client.post(f"/api/processes/{OWNER}/sales_daily/versions", json={"spec": spec}, headers=as_user(OTHER))
    assert r.status_code == 403
    r = client.put(f"/api/processes/{OWNER}/sales_daily/draft", json={"spec": spec}, headers=as_user(OTHER))
    assert r.status_code == 403
    r = client.post(f"/api/processes/{OWNER}/sales_daily/clone", json={"new_name": "sales_daily_bob"}, headers=as_user(OTHER))
    assert r.status_code == 201 and r.json()["meta"]["owner"] == OTHER
    assert {p["name"] for p in client.get("/api/processes").json()} == {"sales_daily", "sales_daily_bob"}
    # co-owner can save
    client.put(f"/api/processes/{OWNER}/sales_daily/co-owners", json={"co_owners": [OTHER]})
    r = client.post(f"/api/processes/{OWNER}/sales_daily/versions", json={"spec": spec}, headers=as_user(OTHER))
    assert r.status_code == 201 and r.json()["version"]["version"] == 2


def test_duplicate_create_is_a_conflict(client):
    spec = load_sample("sales_daily")
    assert client.post("/api/processes", json={"spec": spec}).status_code == 201
    r = client.post("/api/processes", json={"spec": spec})
    assert r.status_code == 409


def test_invalid_spec_gives_field_errors(client):
    spec = load_sample("sales_daily")
    spec["steps"][1]["function_name"] = "main"
    r = client.post("/api/generate", json={"spec": spec, "smoke_run": False})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["path"] == "steps[1].function_name"


def test_generate_and_zip(client):
    r = client.post("/api/generate", json={"spec": load_sample("customer_snapshot"), "smoke_run": False})
    assert r.status_code == 200
    paths = [f["path"] for f in r.json()["files"]]
    assert "dl_customer_snapshot.py" in paths and "dl_customer_snapshot.yaml" in paths  # metadata.script_name
    assert not any(p.endswith(".sql") for p in paths)
    z = client.post("/api/generate/zip", json={"spec": load_sample("customer_snapshot"), "smoke_run": False})
    assert z.headers["content-type"] == "application/zip"
    assert 'filename="dl_customer_snapshot.zip"' in z.headers["content-disposition"]
    assert zipfile.ZipFile(io.BytesIO(z.content)).namelist()[0].startswith("dl_customer_snapshot/")


def test_validate_endpoint(client):
    r = client.post("/api/validate", json={"spec": load_sample("sales_daily")})
    assert r.status_code == 200
    body = r.json()
    assert body["environment"] == "lab" and body["counts"]["ok"] > 0


def test_sql_analyze_rule_based(client):
    r = client.post("/api/sql/analyze", json={
        "sql": "SELECT a FROM `acme-dev.raw.t` WHERE d = DATE '2026-01-01'", "variables": {"write_project": ""},
    })
    body = r.json()
    assert body["sql"] == "SELECT a FROM `${read_project}.${raw_dataset}.t` WHERE d = @cutoff_date"
    assert body["parameters"][0]["source"] == "config" and body["parameters"][0]["default"] == "2026-01-01"
    assert body["equivalence"]["ok"] and body["suggested_function_name"] == "build_sql"


def test_pipeline_check_and_previews(client):
    spec = load_sample("sales_daily")
    spec["steps"] = [spec["steps"][i] for i in (0, 2, 1, 3)]
    body = client.post("/api/pipeline/check", json={"spec": spec}).json()
    assert body["valid"] and any(o["level"] == "warn" for o in body["order"])
    assert body["main"].index("export_sales_daily_to_finance") < body["main"].index("build_sales_daily_agg")
    r = client.post("/api/pipeline/preview-path", json={"template": "gs://{b}/x_{date:%Y}/", "variables": {"b": "x"}})
    assert r.json()["resolved"] == f"gs://x/x_{dt.date.today():%Y}/"
    r = client.post("/api/pipeline/suggest-name", json={"base": "Load Sales", "taken": ["load_sales"]})
    assert r.json()["name"] == "load_sales_2"


def test_schema_endpoints(client):
    r = client.post("/api/schema/infer", files={"file": ("s.csv", b"id,name\n1,x\n", "text/csv")}, data={"file_format": "CSV"})
    assert [c["name"] for c in r.json()["columns"]] == ["id", "name"]
    r = client.post("/api/schema/from-ddl", json={"ddl": "CREATE TABLE t (a INT64)"})
    assert r.json()["columns"][0]["type"] == "INTEGER"
    r = client.post("/api/schema/from-ddl", json={"ddl": "not sql at all"})
    assert r.status_code == 422


def test_import_spec_from_zip(client):
    gen = client.post("/api/generate/zip", json={"spec": load_sample("sales_daily"), "smoke_run": False})
    with zipfile.ZipFile(io.BytesIO(gen.content)) as zf:
        assert not any(n.endswith("spec.json") for n in zf.namelist())  # bundles hold the deliverables only
    r = client.post("/api/processes/import", files={"file": ("bundle.zip", gen.content, "application/zip")})
    assert r.status_code == 422 and "Enhance a script" in r.json()["detail"]
    # ZIPs of older versions (and any ZIP with a spec.json in it) still import
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("sales_daily/spec.json", json.dumps(load_sample("sales_daily")))
    r = client.post("/api/processes/import", files={"file": ("old.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200 and r.json()["spec"]["metadata"]["name"] == "sales_daily" and r.json()["errors"] == []


def test_gcs_match(client):
    r = client.post("/api/gcs/match", json={
        "project_id": "acme-analytics-dev", "bucket": "{landing}", "folder": "sales/incoming", "filename": "store_sales_",
        "extensions": [".csv"], "variables": {"landing": "acme-landing-dev"},
    })
    body = r.json()
    assert body["ok"] and body["count"] == 1 and body["resolved"] == "gs://acme-landing-dev/sales/incoming/*store_sales_*"


def test_iap_mode_requires_a_token(tmp_path):
    settings = Settings(auth_mode="iap", iap_audience="/projects/1/global/backendServices/2", local_workspace_dir=tmp_path,
                        gemini_enabled=False, ui_enabled=False, log_format="text")
    c = TestClient(create_app(settings))
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/me", headers={"x-dev-user": "evil@example.com"}).status_code == 401  # dev header ignored in IAP mode
    assert c.get("/api/health").status_code == 200


def test_sql_tables_scan(client):
    r = client.post("/api/sql/tables", json={"sql": "SELECT * FROM `other-prod.ds.t`",
                                              "variables": {"read_project": "", "write_project": "acme-dev"}})
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] == "SELECT * FROM `${read_project}.ds.t`"
    assert body["tables"][0]["variable"] == "read_project" and body["variables"][0]["values"]["lab"] == "other-prod"


def test_import_upgrades_a_v2_spec_with_notes(client):
    from test_spec import V2

    r = client.post("/api/processes/import", files={"file": ("spec.json", json.dumps(V2).encode(), "application/json")})
    body = r.json()
    assert body["spec"]["spec_version"] == 3 and body["migration_notes"]
    assert any("value" in e["message"] for e in body["errors"])  # prod values still to fill in


def test_test_run_plan_and_refusals(client):
    spec = load_sample("strategic_template")
    plan = client.post("/api/test-runs/plan", json={"owner": OWNER, "name": "strategic_template", "spec": spec}).json()
    assert plan["environment"] == "lab"
    assert any("BigQuery:" in e and "demo-analytics-dev.lab_strategic.prod_script_test" in e for e in plan["side_effects"])
    assert any("no email address" in p for p in plan["problems"])  # tests run without an ADC identity
    r = client.post("/api/test-runs", json={"owner": OWNER, "name": "strategic_template", "spec": spec})
    assert r.status_code == 409 and r.json()["detail"]["problems"]
    assert client.get("/api/test-runs/nope").status_code == 404
    assert client.get("/api/test-runs/nope/log").status_code == 404


def test_owner_deletes_a_process_and_others_cannot(client):
    spec = load_sample("sales_daily")
    assert client.post("/api/processes", json={"spec": spec}).status_code == 201
    assert client.post(f"/api/processes/{OWNER}/sales_daily/versions", json={"spec": spec}).status_code == 201
    r = client.delete(f"/api/processes/{OWNER}/sales_daily", headers=as_user(OTHER))
    assert r.status_code == 403 and "only the owner" in r.text
    assert client.delete(f"/api/processes/{OWNER}/sales_daily").status_code == 204
    assert client.get("/api/processes?scope=all").json() == []
    assert client.get(f"/api/processes/{OWNER}/sales_daily").status_code == 404
    assert client.post("/api/processes", json={"spec": spec}).status_code == 201  # the name is free again


def test_version_zip_holds_the_saved_lab_test_log(client, tmp_path):
    spec = load_sample("sales_daily")
    client.post("/api/processes", json={"spec": spec})
    client.post(f"/api/processes/{OWNER}/sales_daily/versions", json={"spec": spec})
    store = WorkspaceStore(LocalBackend(tmp_path / "ws"))
    assert store.get_version_log(OWNER, "sales_daily", 1) is None
    (tmp_path / "ws" / "users" / OWNER / "sales_daily" / "versions" / "v0001" / "test_run.log").write_text("# Lab test run\nok\n")
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/processes/{OWNER}/sales_daily/versions/1/zip").content)) as zf:
        assert zf.read("sales_daily/sales_daily_lab_test_run.log") == b"# Lab test run\nok\n"
