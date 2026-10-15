"""Find files: bucket targets, how patterns match, searching several buckets at any depth, and the bucket rule."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.deps import Services
from app.main import create_app
from app.services.file_search import name_matcher, parse_targets, search_files
from app.services.sql_analyzer import SqlAnalyzer
from app.services.validators import ClientFactory
from app.services.workspace import LocalBackend, WorkspaceStore
from app.settings import Settings

from fakes import FakeGcs


def _gcs() -> FakeGcs:
    return FakeGcs({
        "drop-dev": {"out/Daily File_20260114.csv": b"a", "out/2026/01/deep/Daily File_20260115.CSV": b"bb",
                     "out/readme.txt": b"hi", "out/": b""},
        "landing-dev": {"in/daily file_20260114.csv": b"ccc", "inbound/x/members.txt": b"d"},
        "value-gcp-prod": {"in/secret.csv": b"prod"},
    }, forbidden={"locked-dev"})


def _search(buckets, pattern, gcs=None, **kw):
    settings = Settings(auth_mode=kw.pop("auth_mode", "local"), **{k: kw.pop(k) for k in list(kw) if k.startswith(("copy_", "file_"))})
    return search_files(ClientFactory(gcs=gcs or _gcs()), settings, project="acme-dev", buckets=buckets, pattern=pattern, **kw)


def test_targets_are_buckets_or_folders_separated_by_commas():
    targets, problems = parse_targets("drop-dev, gs://landing-dev/in/ ;gs://drop-dev  not/a bucket!")
    assert [(t.bucket, t.prefix) for t in targets] == [("drop-dev", ""), ("landing-dev", "in/"), ("not", "a/")]
    assert problems == ["'bucket!' is not a bucket name (write my-bucket or gs://my-bucket/folder/)"]


def test_patterns_text_wildcards_and_paths():
    contains = name_matcher("daily file")
    assert contains("out/2026/Daily File_1.csv") and not contains("Daily File/readme.txt")  # only the file name counts
    assert not name_matcher("daily file", case_sensitive=True)("out/Daily File_1.csv")
    glob = name_matcher("*_2026011?.csv")
    assert glob("a/b/c/Daily File_20260114.CSV") and not glob("Daily File_20260114.csv.gz")
    path = name_matcher("inbound/*.txt")
    assert path("landing/inbound/x/members.txt") and path("inbound/members.txt") and not path("in/members.txt")
    assert name_matcher("inbound/x")("a/inbound/x/members.txt")


def test_search_finds_files_at_any_depth_in_several_buckets():
    r = _search("drop-dev, landing-dev", "daily file_")
    assert [f.as_dict()["uri"] for f in r.files] == [
        "gs://drop-dev/out/2026/01/deep/Daily File_20260115.CSV", "gs://drop-dev/out/Daily File_20260114.csv",
        "gs://landing-dev/in/daily file_20260114.csv"]
    assert r.scanned == 6 and not r.problems and not r.truncated  # the folder placeholder out/ is skipped
    assert r.files[0].size == 2 and r.files[0].updated.startswith("2026-01-01")


def test_a_folder_limits_the_search_and_problems_are_per_bucket():
    r = _search(["gs://drop-dev/out/2026/", "locked-dev"], "*.csv")
    assert [f.name for f in r.files] == ["out/2026/01/deep/Daily File_20260115.CSV"]
    assert len(r.problems) == 1 and r.problems[0].startswith("could not list gs://locked-dev/")


def test_limits_stop_the_search_with_a_note():
    r = _search("drop-dev", "*", max_results=2)
    assert len(r.files) == 2 and r.truncated and "first 2 matches" in r.notes[0]
    r = _search("drop-dev", "*.txt", max_scan=2)
    assert r.truncated and "stopped after 2 files in gs://drop-dev/" in r.notes[0]


def test_nothing_is_searched_without_a_pattern_or_buckets():
    gcs = _gcs()
    assert "enter a file name" in _search("drop-dev", "  ", gcs=gcs).problems[0]
    assert _search("", "*.csv", gcs=gcs).problems == ["enter at least one bucket"]
    assert gcs.calls == []


def test_deployed_hub_searches_only_the_allowlisted_buckets():
    gcs = _gcs()
    r = _search("drop-dev, value-gcp-prod", "*.csv", gcs=gcs, auth_mode="iap", copy_tool_buckets="*-dev")
    assert r.problems == ["gs://value-gcp-prod is not one of the buckets this hub may search (*-dev)"]
    assert gcs.calls == []  # nothing is listed when one bucket is refused
    assert "no buckets are allowed yet" in _search("drop-dev", "*", auth_mode="iap").problems[0]


def test_search_api(tmp_path):
    settings = Settings(auth_mode="local", dev_user_email="alice@example.com", local_workspace_dir=tmp_path,
                        smoke_run_enabled=False, gemini_enabled=False, log_format="text", ui_enabled=False)
    services = Services(settings=settings, store=WorkspaceStore(LocalBackend(tmp_path)), gemini=None,
                        analyzer=SqlAnalyzer(None), clients=ClientFactory(gcs=_gcs()))
    client = TestClient(create_app(settings, services))
    body = client.post("/api/files/search", json={"buckets": "drop-dev,landing-dev", "pattern": "*.txt"}).json()
    assert [f["uri"] for f in body["files"]] == ["gs://drop-dev/out/readme.txt", "gs://landing-dev/inbound/x/members.txt"]
    assert body["problems"] == [] and body["scanned"] == 6


def test_no_credentials_is_a_problem_not_a_crash():
    class NoAdc(ClientFactory):
        def storage(self, project):
            raise RuntimeError("Your default credentials were not found.")

    r = search_files(NoAdc(), Settings(), project="", buckets="drop-dev", pattern="*.csv")
    assert r.problems == ["could not reach Cloud Storage as the hub's identity: Your default credentials were not found."]


def test_a_file_found_starts_a_pipeline_that_ingests_files_like_it():
    from app.models.spec import ProcessSpec
    from app.services.renderer import render_process
    from app.services.schema_infer import infer_from_bytes
    from app.ui.search_page import file_name_prefix, ingestion_spec

    assert file_name_prefix("Daily Engagement File_20260115.csv") == "Daily Engagement File_"
    assert file_name_prefix("MBR_EXTRACT_2026_01_15.dat") == "MBR_EXTRACT_"
    assert file_name_prefix("members.csv") == "members"
    inferred = infer_from_bytes(b"member_id|amount\n1|2.5\n", "TXT")
    spec, notes = ingestion_spec("gs://landing-dev/inbound/pay/Payments_20260115.txt", "alice@example.com",
                                 {"value": "value-gcp"}, inferred, None)
    step = spec["steps"][0]
    assert (step["read_bucket"], step["read_path"], step["filename"], step["file_format"], step["separator"]) == (
        "{landing_bucket}", "{landing_path}", "Payments_", "TXT", "|")
    assert [c["name"] for c in step["columns"]] == ["member_id", "amount"] and step["target_enabled"] is False
    assert spec["metadata"]["name"] == "payments_ingest" and step["table_name"] == "payments"
    assert "2 column(s) read from the file" in " ".join(notes)
    # what the notes ask for: prod values, a lab write project, the staging and history datasets
    for v in spec["variables"]:
        v["values"]["lab"] = v["values"]["lab"] or "proj-dev"
        v["values"]["value"] = v["values"]["value"] or {"landing_bucket": "landing-prod", "landing_path": "inbound/pay"}.get(v["name"], "")
    step["staging_dataset"] = step["target_dataset"] = "raw_lab"
    result = render_process(ProcessSpec.model_validate(spec), smoke_run=True)
    assert all(c.ok for c in result.checks), result.checks
    fixed, notes = ingestion_spec("gs://b/MBR_EXTRACT_20260115.dat", "alice@example.com", {}, None, None)
    assert fixed["steps"][0]["file_format"] == "FIXED" and fixed["steps"][0]["read_path"] == ""
    assert any("paste the layout" in n for n in notes)
