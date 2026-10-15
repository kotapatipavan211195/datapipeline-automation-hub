"""UI tests with NiceGUI's simulated user (no browser): dashboard, create → pipeline → SQL → generate → save,
read-only view, email, lab test run."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.elements.upload_files import SmallFileUpload
from nicegui.testing import User

from app.services.smoke_run import SANDBOX_DIR
from app.services.workspace import LocalBackend, WorkspaceStore
from app.ui import components

from conftest import load_sample

pytestmark = [pytest.mark.nicegui_main_file("tests/ui_main.py")]

ALICE = "alice@example.com"


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("SF_UI_TEST_WS", str(tmp_path))
    return tmp_path


@pytest.fixture()
def no_lab_tests(monkeypatch) -> None:
    """The hub has lab test runs turned off (list it before `user`: the app reads its settings when it starts)."""
    monkeypatch.setenv("SF_TEST_RUN_ENABLED", "false")


def store(path: Path) -> WorkspaceStore:
    return WorkspaceStore(LocalBackend(path))


async def _draft(workspace: Path, name: str, check) -> dict:
    """Wait for the autosaved draft (every 2 s) to satisfy check(spec)."""
    draft = None
    for _ in range(100):
        draft = store(workspace).get_draft(ALICE, name, ALICE)
        if draft and check(draft["spec"]):
            return draft["spec"]
        await asyncio.sleep(0.1)
    raise AssertionError(f"draft never matched: {json.dumps(draft)[:500]}")


async def _new_process(user: User, name: str) -> None:
    await user.open("/new")
    await user.should_see(marker="process-name")
    user.find(marker="process-name").type(name)
    await _lab_projects(user)
    user.find(marker="next-basics").click()
    await user.should_see(f"Process {name} created")


async def _lab_projects(user: User, project: str = "acme-dev") -> None:
    """Type the lab read/write project; prod (value) starts with the hub's default project, value-gcp."""
    for name in ("read_project", "write_project"):
        assert user.find(marker=f"value-{name}").elements.pop().value == "value-gcp"
        user.find(marker=f"lab-{name}").type(project)


async def _enabled(user: User, marker: str, tries: int = 300) -> None:
    """Wait until a gated button (test-run, generate) is enabled: the gates refresh every 2 s."""
    for _ in range(tries):
        if all(e.enabled for e in user.find(marker=marker).elements):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{marker} never became enabled")


async def _validate(user: User) -> None:
    """1 · Validate all, without the GCP checks (the fakes do not hold every lab table)."""
    switch = user.find(kind=ui.switch, content="Include GCP checks").elements.pop()
    switch.value = False
    user.find(marker="validate").click()
    await user.should_see("Checked in lab only", retries=400)


async def _downloaded(user: User, count: int) -> bytes:
    """The content of the count-th download (the test client records downloads in a background task)."""
    for _ in range(100):
        if len(user.download.http_responses) >= count:
            return user.download.http_responses[count - 1].content
        await asyncio.sleep(0.05)
    raise AssertionError(f"download {count} never came")


async def _upload(user: User, marker: str, name: str, data: bytes) -> None:
    upload = user.find(marker=marker).elements.pop()
    with user.client:
        await upload.handle_uploads([SmallFileUpload(name, "application/octet-stream", data)])


async def test_dashboard_empty_state(user: User) -> None:
    await user.open("/")
    await user.should_see("Processes")
    await user.should_see("No processes yet")
    await user.should_see("Datapipeline Automation Hub")
    for marker in ("new-pipeline", "new-copy", "enhance", "find-files"):  # the home page's four cards
        await user.should_see(marker=marker)
    await user.should_see("Enhance a script")
    await user.should_see("ADC: hub-sa@acme-dev.iam.gserviceaccount.com")  # top right: whose Google permissions apply


async def test_create_process_needs_the_lab_write_project(user: User) -> None:
    await user.open("/new")
    user.find(marker="process-name").type("no_project")
    user.find(marker="next-basics").click()
    await user.should_see("Enter the lab (dev) write_project")


async def test_create_process_add_sql_step_generate_and_save(no_lab_tests, user: User, workspace: Path) -> None:
    # no lab test possible on this hub: Generate opens after the validation
    await _new_process(user, "orders_daily")
    meta, _ = store(workspace).get_meta(ALICE, "orders_daily")
    assert meta.owner == ALICE

    user.find(marker="add-sql").click()
    await user.should_see(marker="original-sql")
    preview = user.find(marker="main-preview").elements.pop()
    assert "run_sql()" in preview.content and "def main(args):" in preview.content

    user.find(marker="original-sql").type("INSERT INTO `acme-dev.mart.orders` SELECT * FROM `acme-dev.raw.orders` WHERE d = DATE '2026-01-01'")
    user.find(marker="analyze").click()
    await user.should_see("Verified: same logic as your SQL")
    user.find(marker="apply-analysis").click()
    await user.should_see("Applied.")
    await user.should_see(marker="var-3-value", retries=100)  # the new dataset variables show up on the Variables page
    user.find(marker="var-2-value").type("mart")
    user.find(marker="var-3-value").type("raw")

    await user.should_see("Locked: run 1 · Validate all first", retries=100)
    await _validate(user)
    await user.should_see("Lab test not possible here: lab test runs are turned off", retries=100)
    await _enabled(user, "generate")
    user.find(marker="generate").click()
    await user.should_see("orders_daily.yaml", retries=400)  # the generated file list
    await user.should_see("Not tested in lab")
    await user.should_see("py ./orders_daily.py -cf ./orders_daily.yaml lab")  # run commands: environment last
    await user.should_see("py ./orders_daily.py -cf ./orders_daily.yaml value")
    user.find(marker="save-version").click()
    await user.should_see("Saved version 1", retries=400)
    version = store(workspace).get_version(ALICE, "orders_daily", 1)
    spec = version["spec"]
    step = spec["steps"][0]
    assert step["sql"] == ("INSERT INTO `${write_project}.${mart_dataset}.orders` SELECT * FROM `${write_project}.${raw_dataset}.orders` "
                           "WHERE d = @cutoff_date")
    assert step["function_name"] == "load_orders"
    assert {v["name"]: v["values"]["value"] for v in spec["variables"]}["mart_dataset"] == "mart"
    assert spec["metadata"]["created_on"]  # set on the first saved version
    assert "orders_daily.py" in version["files"] and not any(f.startswith("tests/") for f in version["files"])
    assert "spec.json" not in version["files"] and version["test_run"] is None


async def test_author_and_reviewer_pids_need_no_enter_and_show_again(user: User, workspace: Path) -> None:
    await user.open("/new")
    await user.should_see(marker="process-name")
    user.find(marker="process-name").type("pid_check")
    user.find(marker="authors").type("P10000, P12345")  # typed, no Enter
    user.find(marker="reviewers").type("P1")
    user.find(marker="tags").type("claims, daily")
    await _lab_projects(user)
    user.find(marker="next-basics").click()
    await user.should_see("Process pid_check created")
    md = store(workspace).get_draft(ALICE, "pid_check", ALICE)["spec"]["metadata"]
    assert (md["authors"], md["reviewers"], md["tags"]) == (["P10000", "P12345"], ["P1"], ["claims", "daily"])
    await user.open(f"/p/{ALICE}/pid_check")  # saved PIDs show when the process is opened again
    await user.should_see(marker="authors")
    assert user.find(marker="authors").elements.pop().value == "P10000, P12345"
    assert user.find(marker="reviewers").elements.pop().value == "P1"


async def test_template_adds_steps_in_order(user: User) -> None:
    await _new_process(user, "drop_pipeline")
    user.find(marker="template-drop_to_table").click()
    await user.should_see("Added 3 steps")
    code = user.find(marker="main-preview").elements.pop().content
    order = [code.index(n) for n in ("copy_files", "ingest_files", "check_data_quality")]
    assert order == sorted(order)
    assert "ingest_files(config_data, table_def)" in code


async def test_someone_elses_process_is_read_only_with_clone(user: User, workspace: Path) -> None:
    spec = load_sample("sales_daily")
    spec["metadata"]["owner"] = "bob@example.com"
    store(workspace).create_process("bob@example.com", json.loads(json.dumps(spec)))
    await user.open("/p/bob@example.com/sales_daily")
    await user.should_see("read-only")
    await user.should_see("Clone to my workspace")
    user.find(marker="add-sql")  # the editor still renders for exploring/downloading


async def test_dashboard_lists_processes(user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("customer_snapshot"))
    await user.open("/")
    await user.should_see("customer_snapshot")
    user.find(kind=ui.toggle).elements.pop().value = "all"
    await user.should_see("customer_snapshot")


async def test_owner_sees_every_step_form_and_can_validate_and_download(no_lab_tests, user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("customer_snapshot"))
    await user.open(f"/p/{ALICE}/customer_snapshot")
    for fn in ("load_crm_customer_extract", "merge_customer_dimension", "publish_customer_snapshot", "copy_contacts_to_share"):
        await user.should_see(fn)
    await user.should_not_see("read-only")
    await _validate(user)
    await _enabled(user, "generate")
    user.find(marker="generate").click()
    await user.should_see("dl_customer_snapshot.yaml", retries=400)  # the generated file list
    await _enabled(user, "download")
    user.find(marker="download").click()
    for _ in range(100):  # the test client records the download in a background task
        if user.download.http_responses:
            break
        await asyncio.sleep(0.1)
    assert user.download.http_responses, "a ZIP download should have been triggered"
    assert user.download.http_responses[-1].content[:2] == b"PK"


async def test_sales_daily_forms_render_for_all_step_types(user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("sales_daily"))
    await user.open(f"/p/{ALICE}/sales_daily")
    await user.should_see("Where are the files?")      # ingest
    await user.should_see("SQL embedded in the script")  # sql
    await user.should_see("Where to write the file")     # export
    await user.should_see("Checks")                      # data quality
    await user.should_see(marker="main-preview")


async def test_copy_form_previews_the_gcloud_command(user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("engagement_file_ingest"))
    await user.open(f"/p/{ALICE}/engagement_file_ingest")
    await user.should_see("What to copy")
    await user.should_see("gcloud storage mv --no-clobber", retries=50)
    await user.should_see("Tables")  # the ingestion form


async def test_pasted_sql_is_scanned_and_projects_become_variables(user: User, workspace: Path) -> None:
    await user.open("/new")
    user.find(marker="process-name").type("orders_scan")
    user.find(marker="lab-write_project").type("acme-dev")
    user.find(marker="next-basics").click()
    await user.should_see("Process orders_scan created")
    user.find(marker="add-sql").click()
    await user.should_see(marker="original-sql")
    user.find(marker="original-sql").type(
        "INSERT INTO `acme-dev.mart.orders` SELECT o.id FROM `acme-dev.raw.orders` o "
        "JOIN `acme-ref-prod.reference.stores` s ON s.id = o.id"
    )
    await user.should_see("Tables in this SQL", retries=100)
    await user.should_see("project → ${acme_ref_project}", retries=100)
    spec = await _draft(workspace, "orders_scan", lambda s: s["steps"] and s["steps"][0]["sql"])
    step = spec["steps"][0]
    # read_project and write_project are both value-gcp in prod: another project gets a parameter of its own, with the
    # same name in every environment
    assert step["sql"] == ("INSERT INTO `${write_project}.mart.orders` SELECT o.id FROM `${write_project}.raw.orders` o "
                           "JOIN `${acme_ref_project}.reference.stores` s ON s.id = o.id")
    assert {v["name"]: (v["values"]["lab"], v["values"]["value"]) for v in spec["variables"]} == {
        "read_project": ("", "value-gcp"), "write_project": ("acme-dev", "value-gcp"),
        "acme_ref_project": ("acme-ref-prod", "acme-ref-prod")}
    assert step["statement_kind"] == "DML" and step["targets"] == ["{write_project}.mart.orders"]


async def test_email_section_shows_the_adc_sender_and_checks_commas(user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("customer_snapshot"))
    await user.open(f"/p/{ALICE}/customer_snapshot")
    await user.should_see("Email notification")
    await user.should_see("Off: the generated script contains no email code.")
    user.find(marker="email-enabled").click()
    await user.should_see(marker="email-sender")
    await user.should_not_see("smtp.example.com")  # the relay is a hub setting, not shown
    await user.should_see(marker="email-to-lab")
    await user.should_see(marker="email-to-value")
    await user.should_not_see(marker="email-host")
    user.find(marker="email-to-lab").type("a@acme.com; b@acme.com")
    await user.should_see("separate addresses with commas, not semicolons")


async def test_lab_test_run_from_the_review_page(user: User, workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(SANDBOX_DIR))  # the script talks to the fake Google clients
    monkeypatch.setenv("DAHUB_SMOKE_LOG", str(workspace / "calls.jsonl"))
    store(workspace).create_process(ALICE, load_sample("strategic_template"))
    await user.open(f"/p/{ALICE}/strategic_template")
    await user.should_see("Locked: run 1 · Validate all first", retries=100)
    await _validate(user)
    await _enabled(user, "test-run")
    await user.should_see("Locked: run 2 · Test run in lab first", retries=100)
    user.find(marker="test-run").click()
    await user.should_see("Run this process in lab now?")
    await user.should_see("Email: sent only to hub-sa@acme-dev.iam.gserviceaccount.com")
    user.find(marker="test-run-confirm").click()
    await user.should_see("lab test: passed", retries=600)
    await user.should_see("build_prod_script_test:")
    await user.should_see(marker="test-run-log-download", retries=100)  # the log, once the run has ended
    user.find(marker="test-run-log-download").click()
    log = (await _downloaded(user, 1)).decode()
    assert log.startswith("# Lab test run of dl_strategic_pipeline_template.py") and "# status:      passed" in log
    assert "Function build_prod_script_test execution end" in log
    await _enabled(user, "generate")
    await user.should_see("Lab test passed (", retries=100)
    user.find(marker="generate").click()
    await user.should_see("dl_strategic_pipeline_template.yaml", retries=400)
    await user.should_not_see("Not tested in lab")
    await _enabled(user, "download")
    user.find(marker="download").click()  # the tested bundle carries its lab test log next to the script
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(await _downloaded(user, 2))) as zf:
        assert "dl_strategic_pipeline_template/dl_strategic_pipeline_template_lab_test_run.log" in zf.namelist()
    user.find(marker="save-version").click()
    await user.should_see("with its lab test run (passed)", retries=400)
    version = store(workspace).get_version(ALICE, "strategic_template", 1)
    assert version["test_run"]["status"] == "passed" and version["test_run"]["environment"] == "lab"
    assert store(workspace).get_version_log(ALICE, "strategic_template", 1).startswith("# Lab test run of")


async def test_old_specs_are_upgraded_with_a_banner(user: User, workspace: Path) -> None:
    from test_spec import V2

    store(workspace).create_process(ALICE, json.loads(json.dumps(V2)))
    await user.open(f"/p/{ALICE}/old_proc")
    await user.should_see("This process was upgraded to the team's script standard")
    await user.should_see(marker="migration-notes")


async def test_copy_files_process_from_the_home_page(no_lab_tests, user: User, workspace: Path) -> None:
    await user.open("/")
    user.find(marker="new-copy").click()
    await user.should_see(marker="copy-make-script")  # the Copy files page: copy now, or make a script
    user.find(marker="copy-make-script").click()
    await user.should_see(marker="process-name")
    await user.should_see("Copy files process")
    user.find(marker="process-name").type("vendor_copy")
    await _lab_projects(user)
    user.find(marker="next-basics").click()
    await user.should_see("Process vendor_copy created")
    await user.should_see("Copy jobs, in the order main() runs them")
    await user.should_not_see(marker="add-sql")  # no pipeline page in a copy process
    user.find(kind=ui.input, content="Source 1").type("gs://drop-bucket/out/Daily File_*.csv")
    user.find(kind=ui.input, content="Destination").type("gs://landing-bucket/in/")
    user.find(marker="add-copy-job").click()
    await user.should_see(marker="copy-fn-1")
    spec = await _draft(workspace, "vendor_copy", lambda s: len(s["steps"]) == 2)
    assert spec["process_type"] == "copy" and [st["type"] for st in spec["steps"]] == ["copy", "copy"]
    assert spec["steps"][1]["config_key"] == "file_copy_2"
    assert [st["direction"] for st in spec["steps"]] == ["bucket_to_bucket", "bucket_to_bucket"]
    user.find(marker="copy-direction-1").elements.pop().value = "local_to_bucket"  # the second job uploads from the server
    await user.should_see("“Local” is the machine that runs the script")
    await user.should_see("Source 1: a file or folder on the machine that runs the script")
    user.find(marker="copy-fn-1").elements.pop().value = "copy_more"
    inputs = user.find(kind=ui.input, content="Source 1").elements
    max(inputs, key=lambda e: e.id).value = "/data/exports/members_{date:%Y%m%d}.csv"
    max(user.find(kind=ui.input, content="Destination").elements, key=lambda e: e.id).value = "gs://landing-bucket/other/"
    spec = await _draft(workspace, "vendor_copy", lambda s: s["steps"][1]["destination"] == "gs://landing-bucket/other/")
    assert spec["steps"][1]["direction"] == "local_to_bucket"
    assert spec["steps"][1]["sources"] == ["/data/exports/members_{date:%Y%m%d}.csv"]
    await _validate(user)
    await _enabled(user, "generate")
    user.find(marker="generate").click()
    await user.should_see("vendor_copy.yaml", retries=400)
    await user.should_see("py ./vendor_copy.py -cf ./vendor_copy.yaml lab")
    meta = store(workspace).list_processes(user=ALICE)
    assert meta[0].process_type == "copy"


async def test_fixed_width_ingestion_shows_lengths_and_record_length(user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("fixed_width_ingest"))
    await user.open(f"/p/{ALICE}/member_fixed_width_ingest")
    await user.should_see("Where are the files?")
    await user.should_see("Record length: 69 characters per line", retries=50)
    await user.should_see(marker="col-width-5")
    lines = (b"0000000001Ann Smith                     A 2026-01-15      125.50   12\n"
             b"0000000002Bob Jones                     I 2025-12-01        0.00    3\n")
    upload = next(e for e in user.find(kind=ui.upload).elements if e.props.get("label") == "Check the layout against a sample file")
    with user.client:
        await upload.handle_uploads([SmallFileUpload("MBR_EXTRACT_1.txt", "text/plain", lines)])
    await user.should_see("Every line of MBR_EXTRACT_1.txt is 69 characters", retries=100)
    await user.should_see(marker="layout-preview")


async def test_attaching_a_sql_file_fills_the_sql_step(user: User, workspace: Path) -> None:
    await _new_process(user, "orders_attach")
    user.find(marker="add-sql").click()
    await user.should_see(marker="attach-sql")
    sql = "-- monthly build\r\nINSERT INTO `acme-dev.mart.orders`\r\nSELECT * FROM `acme-dev.raw.orders`\r\n"
    await _upload(user, "attach-sql", "monthly_build.sql", ("\ufeff" + sql).encode("utf-8"))
    await user.should_see("Attached monthly_build.sql")
    spec = await _draft(workspace, "orders_attach", lambda s: s["steps"] and s["steps"][0]["sql"])
    step = spec["steps"][0]
    assert step["original_sql"] == sql.replace("\r\n", "\n") and step["sql_file"] == "monthly_build.sql"
    assert step["sql"].startswith("-- monthly build\nINSERT INTO `${write_project}.mart.orders`")
    await user.should_see("📎 monthly_build.sql", retries=50)
    await _upload(user, "attach-sql", "notes.docx", b"PK\x03\x04")
    await user.should_see("Not attached: notes.docx: attach a .sql or .txt file")



FIXTURES = Path(__file__).parent / "fixtures"


async def test_enhance_converts_an_old_script_into_a_process(user: User, workspace: Path) -> None:
    await user.open("/")
    user.find(marker="enhance").click()
    await user.should_see(marker="enhance-upload")
    await _upload(user, "enhance-upload", "dl_active_members.py", (FIXTURES / "legacy_sql_extract.py").read_bytes())
    await _upload(user, "enhance-upload", "dl_active_members.yaml", (FIXTURES / "legacy_sql_extract.yaml").read_bytes())
    await user.should_see(marker="enhance-file-config")
    user.find(marker="enhance-analyze").click()
    await user.should_see("Every step maps to a hub step", retries=100)
    await user.should_see("hard-coded: acme-raw-dev")  # the standards report
    assert user.find(marker="enhance-process-name").elements.pop().value == "dl_active_members"
    user.find(marker="enhance-process-name").elements.pop().value = "active_members"
    # 3 · Script header: read from the old header, checked and completed here
    authors = user.find(marker="enhance-authors").elements.pop()
    assert authors.value == "P10000" and user.find(marker="enhance-update-pid").elements.pop().value == "P10000"
    authors.value = ""
    user.find(marker="enhance-convert").click()
    await user.should_see("Enter the author PID(s) under 3 · Script header")
    authors.value = "P10000, P77777"
    user.find(marker="enhance-reviewers").type("P22222")
    user.find(marker="enhance-jira").type("DE-1234")
    user.find(marker="enhance-convert").click()
    await user.should_see("Imported from dl_active_members.py: please review", retries=100)
    await user.should_see(marker="migration-notes")
    await user.should_see("build_active_members")
    assert user.find(marker="authors").elements.pop().value == "P10000, P77777"  # the wizard shows the header
    user.find(marker="next-basics").click()
    await user.should_see("Process active_members created")
    spec = store(workspace).get_draft(ALICE, "active_members", ALICE)["spec"]
    assert [s["function_name"] for s in spec["steps"]] == ["build_active_members", "extract_data_file_csv"]
    md = spec["metadata"]
    assert (md["authors"], md["reviewers"], md["jira"]) == (["P10000", "P77777"], ["P22222"], "DE-1234")
    assert md["updates"][-1]["pid"] == "P10000" and md["updates"][-1]["note"] == "Enhanced to the team script standard"


async def test_enhance_offers_the_rewrite_for_custom_code(user: User) -> None:
    await user.open("/enhance")
    await user.should_see(marker="enhance-upload")
    await _upload(user, "enhance-upload", "pdw_pull.py", (FIXTURES / "legacy_jdbc_pull.py").read_bytes())
    user.find(marker="enhance-analyze").click()
    await user.should_see("It cannot become a hub process as it is", retries=100)
    await user.should_see("uses a JDBC source-system connection (jaydebeapi)")
    await user.should_see("Rewriting needs Gemini, which is turned off on this hub.")


async def test_copy_files_now_moves_files_between_buckets(user: User, workspace: Path) -> None:
    await user.open("/copy")
    await user.should_see("Runs with your own Google credentials")  # a local hub: any bucket the credentials reach
    user.find(marker="copy-src-bucket").elements.pop().value = "drop"
    await user.should_see("out/", retries=100)
    user.find(kind=ui.button, content="out/").click()
    await user.should_see("Daily File_1.csv", retries=100)
    user.find(marker="copy-pattern").elements.pop().value = "Daily File_*.csv"
    user.find(marker="copy-dst-bucket").elements.pop().value = "landing"
    user.find(marker="copy-dst-folder").elements.pop().value = "in/vendor"
    user.find(marker="copy-op").elements.pop().value = "move"
    user.find(marker="copy-preview").click()
    await user.should_see("2 file(s), 4 B: move gs://drop/out/ → gs://landing/in/vendor/", retries=100)
    user.find(marker="copy-run").click()
    await user.should_see("Moving deletes each source file once it is copied")
    user.find(marker="copy-confirm").click()
    await user.should_see("2 file(s) moved, 0 skipped (already there), 0 failed", retries=200)
    record = store(workspace).list_copy_jobs(ALICE)[0]
    assert record["operation"] == "move" and record["counts"]["moved"] == 2 and record["user"] == ALICE
    assert record["source"] == "gs://drop/out/" and record["destination"] == "gs://landing/in/vendor/"


@pytest.fixture()
def lab_buckets_only(monkeypatch) -> None:
    """A deployed hub: only the allowlisted lab buckets (list it before `user`)."""
    monkeypatch.setenv("SF_COPY_TOOL_SCOPE", "allowlist")
    monkeypatch.setenv("SF_COPY_TOOL_BUCKETS", "landing")


async def test_deployed_copy_tool_keeps_to_the_allowlisted_buckets(lab_buckets_only, user: User) -> None:
    await user.open("/copy")
    await user.should_see("Lab buckets only: landing")
    user.find(marker="copy-src-bucket").elements.pop().value = "landing"
    await user.should_see("in/", retries=100)
    destination = user.find(marker="copy-dst-bucket").elements.pop()
    assert destination.options == ["landing"]  # only allowlisted buckets are offered
    destination.set_options(["landing", "drop"], value="drop")  # typed in by hand
    user.find(marker="copy-preview").click()
    await user.should_see("gs://drop is not one of the buckets this hub may copy to", retries=100)
    await user.should_not_see(marker="copy-run")


async def test_copy_files_upload_from_your_computer(user: User, workspace: Path) -> None:
    await user.open("/copy")
    user.find(marker="copy-direction").elements.pop().value = "local_to_bucket"
    await user.should_see(marker="copy-upload")
    await _upload(user, "copy-upload", "vendor_a.csv", b"id\n1\n")
    await _upload(user, "copy-upload", "vendor_b.csv", b"id\n2\n")
    await user.should_see("2 file(s), 10 B")
    user.find(marker="copy-dst-bucket").elements.pop().value = "landing"
    user.find(marker="copy-dst-folder").elements.pop().value = "uploads"
    user.find(marker="copy-preview").click()
    await user.should_see("2 file(s), 10 B: upload your computer → gs://landing/uploads/", retries=100)
    user.find(marker="copy-run").click()
    await user.should_see("Upload 2 file(s) now?")
    user.find(marker="copy-confirm").click()
    await user.should_see("2 file(s) uploaded, 0 skipped (already there), 0 failed", retries=200)
    record = store(workspace).list_copy_jobs(ALICE)[0]
    assert record["operation"] == "upload" and record["destination"] == "gs://landing/uploads/" and record["counts"]["copied"] == 2


async def test_copy_files_download_to_your_computer(user: User) -> None:
    await user.open("/copy")
    user.find(marker="copy-direction").elements.pop().value = "bucket_to_local"
    await user.should_see("2 · To your computer")
    await user.should_not_see(marker="copy-dst-bucket")
    user.find(marker="copy-src-bucket").elements.pop().value = "drop"
    await user.should_see("out/", retries=100)
    user.find(kind=ui.button, content="out/").click()
    await user.should_see("Daily File_1.csv", retries=100)
    user.find(marker="copy-pattern").elements.pop().value = "Daily File_*.csv"
    user.find(marker="copy-preview").click()
    await user.should_see("2 file(s), 4 B: download gs://drop/out/ → your computer", retries=100)
    user.find(marker="copy-run").click()  # nothing changes in the bucket: no confirmation
    await user.should_see("2 file(s) downloaded, 0 failed", retries=200)
    await user.should_see(marker="copy-save")
    for _ in range(100):  # the ZIP is handed to the browser when the job ends
        if user.download.http_responses:
            break
        await asyncio.sleep(0.05)
    # the browser fetches it from the API (not mounted in this UI-only test app; tests/test_copy_tool.py covers it)
    url = str(user.download.http_responses[-1].url)
    assert re.search(r"/api/copy/jobs/[0-9a-f]{12}/file$", url), url



async def test_owner_deletes_a_process_from_the_home_page(user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("sales_daily"))
    bob_spec = load_sample("file_copy")
    bob_spec["metadata"]["owner"] = "bob@example.com"
    store(workspace).create_process("bob@example.com", bob_spec)
    await user.open("/")
    user.find(kind=ui.toggle).elements.pop().value = "all"
    await user.should_see(bob_spec["metadata"]["name"], retries=100)
    assert len(user.find(marker="delete-process").elements) == 1  # only on your own processes
    user.find(marker="delete-process").click()
    await user.should_see("Delete sales_daily?")
    user.find(marker="delete-process-confirm").click()
    await user.should_see("Deleted sales_daily", retries=100)
    assert [p.name for p in store(workspace).list_processes(user=ALICE)] == [bob_spec["metadata"]["name"]]
    assert not (workspace / "users" / ALICE / "sales_daily").exists()


async def test_find_files_searches_every_folder_of_several_buckets(user: User) -> None:
    await user.open("/")
    user.find(marker="find-files").click()
    await user.should_see(marker="search-buckets")
    user.find(marker="search-buckets").type("landing, gs://drop")
    user.find(marker="search-pattern").type("*.CSV")  # case does not matter unless asked
    user.find(marker="search-run").click()
    await user.should_see("3 files found in 2 buckets", retries=200)
    table = user.find(marker="search-results").elements.pop()
    assert sorted(r["uri"] for r in table.rows) == [
        "gs://drop/out/Daily File_1.csv", "gs://drop/out/Daily File_2.csv", "gs://landing/in/a_20260101.csv"]
    user.find(marker="search-pattern").elements.pop().value = "readme"
    user.find(marker="search-run").click()
    await user.should_see("1 file found in 2 buckets", retries=200)


async def test_import_lets_bigquery_guess_the_types_in_lab(user: User, workspace: Path, monkeypatch) -> None:
    from app.services.schema_infer import InferResult

    seen = {}

    def fake_guess(bq, target, columns, rows, location=None):
        seen.update(target=target, rows=rows)
        return InferResult([c.model_copy(update={"type": "INTEGER"}) for c in columns], [], [],
                           {"guessed_by": "bigquery", "rows_loaded": len(rows)})

    monkeypatch.setattr(components, "guess_types_with_bigquery", fake_guess)
    store(workspace).create_process(ALICE, load_sample("engagement_file_ingest"))
    await user.open(f"/p/{ALICE}/engagement_file_ingest")
    await user.should_see("Where are the files?")
    user.find("Import columns…").click()
    await user.should_see(marker="bq-guess")
    assert user.find(marker="bq-guess").elements.pop().value is True  # on: the lab staging dataset is not prod's
    upload = next(e for e in user.find(kind=ui.upload).elements if e.props.get("label") == "Drop a sample file here")
    with user.client:
        await upload.handle_uploads([SmallFileUpload("sample.csv", "text/csv", b"a,b\n1,2\n3,4\n")])
    await user.should_see("BigQuery guessed the types from 2 row(s)", retries=100)
    table = user.find(marker="import-preview").elements.pop()
    assert [c["label"] for c in table.columns] == ["Name", "Type (BigQuery's guess)", "Mode", "Hub rules"]
    assert [(r["name"], r["type"], r["hub"]) for r in table.rows] == [("a", "INTEGER", "INTEGER"), ("b", "INTEGER", "INTEGER")]
    assert seen == {"target": "demo-analytics-dev.clubwide", "rows": [["1", "2"], ["3", "4"]]}


async def test_custom_step_form_and_skipping_the_lab_test(user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("custom_geocode"))
    await user.open(f"/p/{ALICE}/member_geocode")
    await user.should_see("What should this step do?")
    await user.should_see("Gemini is off on this hub: write the code yourself.")
    await user.should_see("SECRETS['geocode_api_key']")
    assert not user.find(marker="custom-write").elements.pop().enabled
    user.find(marker="custom-check").click()
    await user.should_see("The code compiles, passes ruff", retries=200)
    await _validate(user)
    await user.should_see(marker="skip-test", retries=100)
    user.find(marker="skip-test").click()
    user.find(marker="skip-reason").type("The maps API is not reachable from the hub")
    user.find(marker="skip-confirm").click()
    await _enabled(user, "generate")
    await user.should_see("2 Lab test skipped")
    user.find(marker="generate").click()
    await user.should_see("Not tested in lab: skipped by alice@example.com", retries=400)


async def test_fixed_width_layout_check_lets_bigquery_type_the_columns(user: User, workspace: Path, monkeypatch) -> None:
    from app.services.schema_infer import InferResult

    def fake_guess(bq, target, columns, rows, location=None):
        assert rows[0][0] == "0000000001" and len(rows[0]) == len(columns)  # split by the column lengths
        return InferResult([c.model_copy(update={"type": "INTEGER" if i == 0 else c.type}) for i, c in enumerate(columns)],
                           [], [], {"guessed_by": "bigquery", "rows_loaded": len(rows)})

    monkeypatch.setattr(components, "guess_types_with_bigquery", fake_guess)
    store(workspace).create_process(ALICE, load_sample("fixed_width_ingest"))
    await user.open(f"/p/{ALICE}/member_fixed_width_ingest")
    await user.should_see("Record length: 69 characters per line", retries=50)
    lines = (b"0000000001Ann Smith                     A 2026-01-15      125.50   12\n"
             b"0000000002Bob Jones                     I 2025-12-01        0.00    3\n")
    upload = next(e for e in user.find(kind=ui.upload).elements if e.props.get("label") == "Check the layout against a sample file")
    with user.client:
        await upload.handle_uploads([SmallFileUpload("MBR_EXTRACT_1.txt", "text/plain", lines)])
    await user.should_see(marker="layout-guess", retries=100)
    user.find(marker="layout-guess").click()
    await user.should_see("BigQuery typed the columns from 2 line(s)", retries=100)
    spec = await _draft(workspace, "member_fixed_width_ingest", lambda s: s["steps"][0]["columns"][0]["type"] == "INTEGER")
    assert spec["steps"][0]["columns"][0]["type"] == "INTEGER"


async def test_new_ingestion_keeps_raw_rows_and_the_email_lists_files(user: User, workspace: Path) -> None:
    await _new_process(user, "raw_payments")
    user.find(marker="add-ingest").click()
    await user.should_see(marker="ingest-target", retries=100)
    switch = user.find(marker="ingest-target").elements.pop()
    assert switch.value is False  # new ingestions keep the raw rows in staging
    await user.should_see("The raw rows stay in the staging table for the next steps")
    await user.should_not_see(marker="ingest-merge-filter")
    switch.value = True
    await user.should_see(marker="ingest-merge-filter", retries=100)
    spec = await _draft(workspace, "raw_payments", lambda s: s["steps"] and s["steps"][0].get("target_enabled") is True)
    assert spec["email"]["file_summary"] is True
    user.find(marker="email-enabled").click()
    await user.should_see(marker="email-file-summary", retries=100)
    assert user.find(marker="email-file-summary").elements.pop().value is True


async def test_header_search_finds_processes_from_any_page(user: User, workspace: Path) -> None:
    store(workspace).create_process(ALICE, load_sample("customer_snapshot"))
    store(workspace).create_process(ALICE, load_sample("sales_daily"))
    await user.open("/")
    await user.should_not_see("New process")  # the header button is gone: the New pipeline card starts one
    await user.should_see("sales_daily")
    user.find(marker="process-search").type("customer")
    await user.should_not_see("sales_daily", retries=100)
    await user.should_see("customer_snapshot")
    await user.open("/search")
    user.find(marker="process-search").type("sales").trigger("keydown.enter")
    await user.should_see("sales_daily", retries=100)
    await user.should_not_see("customer_snapshot")


async def test_find_files_starts_an_ingestion_of_the_file_found(user: User, workspace: Path) -> None:
    await user.open("/search")
    user.find(marker="search-buckets").type("landing")
    user.find(marker="search-pattern").type("*.csv")
    user.find(marker="search-run").click()
    await user.should_see("1 file found in 1 bucket", retries=200)
    table = user.find(marker="search-results").elements.pop()
    table.selected = [table.rows[0]]
    await _enabled(user, "search-ingest")
    user.find(marker="search-ingest").click()
    await user.should_see("New ingestion from a file found with Find files", retries=200)
    await user.should_see("2 column(s) read from the file")
    assert user.find(marker="process-name").elements.pop().value == "a_ingest"
