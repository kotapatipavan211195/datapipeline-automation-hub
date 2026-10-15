"""The Copy files tool: which buckets it may use, what a plan copies where, and running copy / move jobs."""

from __future__ import annotations

import time

import pytest

from app.services.copy_tool import CopyJobManager, CopyPolicy, CopyRefused
from app.services.validators import ClientFactory
from app.services.workspace import LocalBackend, WorkspaceStore
from app.settings import Settings

from fakes import FakeGcs

USER = "alice@example.com"


def _objects() -> dict[str, dict[str, bytes]]:
    return {
        "drop-dev": {"out/Daily File_20260114.csv": b"a,b\n1,2\n", "out/Daily File_20260115.csv": b"a,b\n3,4\n",
                     "out/readme.txt": b"hi", "out/old/Daily File_20251231.csv": b"x"},
        "landing-dev": {"in/Daily File_20260114.csv": b"older copy"},
        "value-gcp-prod": {"in/x.csv": b"prod"},
    }


def _manager(tmp_path, gcs: FakeGcs, **settings) -> CopyJobManager:
    s = Settings(auth_mode=settings.pop("auth_mode", "local"), local_workspace_dir=tmp_path, **settings)
    return CopyJobManager(s, ClientFactory(gcs=gcs), WorkspaceStore(LocalBackend(tmp_path)))


def _wait(job) -> None:
    for _ in range(200):
        if not job.active:
            return
        time.sleep(0.02)
    raise AssertionError("the copy job did not finish")


def test_scope_follows_where_the_hub_runs():
    local = CopyPolicy(Settings(auth_mode="local"))
    assert local.scope == "any" and local.allows("value-gcp-prod") and local.unavailable() is None
    deployed = CopyPolicy(Settings(auth_mode="iap", copy_tool_buckets="drop-*, landing-dev"))
    assert deployed.scope == "allowlist" and deployed.allows("drop-dev") and deployed.allows("landing-dev")
    assert not deployed.allows("value-gcp-prod")
    assert "SF_COPY_TOOL_BUCKETS" in CopyPolicy(Settings(auth_mode="iap")).unavailable()
    assert CopyPolicy(Settings(auth_mode="local", copy_tool_scope="allowlist")).scope == "allowlist"
    assert "turned off" in CopyPolicy(Settings(copy_tool_enabled=False)).unavailable()


def test_plan_matches_files_and_flags_existing_ones(tmp_path):
    gcs = FakeGcs(_objects())
    plan = _manager(tmp_path, gcs).plan(project="p", operation="copy", source_bucket="drop-dev", source_prefix="out",
                                        pattern="Daily File_*.csv", destination_bucket="landing-dev", destination_prefix="in/")
    assert plan.problems == []
    assert [(i.source, i.destination, i.exists) for i in plan.items] == [
        ("out/Daily File_20260114.csv", "in/Daily File_20260114.csv", True),
        ("out/Daily File_20260115.csv", "in/Daily File_20260115.csv", False),
    ]  # the old/ subfolder is left out unless subfolders are included
    assert plan.total_bytes == 16 and plan.existing == 1
    recursive = _manager(tmp_path, gcs).plan(project="p", operation="copy", source_bucket="drop-dev", source_prefix="out",
                                             pattern="*.csv", recursive=True, destination_bucket="landing-dev",
                                             destination_prefix="archive")
    assert "archive/old/Daily File_20251231.csv" in [i.destination for i in recursive.items]
    picked = _manager(tmp_path, gcs).plan(project="p", operation="copy", source_bucket="drop-dev", source_prefix="out/",
                                          names=["readme.txt"], destination_bucket="landing-dev")
    assert [(i.source, i.destination) for i in picked.items] == [("out/readme.txt", "readme.txt")]


@pytest.mark.parametrize("kwargs, problem", [
    ({"source_bucket": "drop-dev", "source_prefix": "out", "destination_bucket": "drop-dev", "destination_prefix": "out/"},
     "the destination folder is the source folder"),
    ({"source_bucket": "drop-dev", "source_prefix": "out", "destination_bucket": "drop-dev", "destination_prefix": "out/copy",
      "recursive": True}, "inside the source folder"),
    ({"source_bucket": "drop-dev", "pattern": "*.parquet", "destination_bucket": "landing-dev"}, "no file matches"),
    ({"source_bucket": "Bad_Bucket!", "destination_bucket": "landing-dev"}, "not a valid bucket name"),
    ({"source_bucket": "", "destination_bucket": "landing-dev"}, "choose the source bucket"),
])
def test_plan_problems(tmp_path, kwargs, problem):
    plan = _manager(tmp_path, FakeGcs(_objects())).plan(project="p", operation="copy", **kwargs)
    assert any(problem in p for p in plan.problems), plan.problems
    with pytest.raises(CopyRefused):
        _manager(tmp_path, FakeGcs(_objects())).start(plan, project="p", user=USER, identity="me")


def test_deployed_hub_refuses_buckets_outside_the_allowlist(tmp_path):
    gcs = FakeGcs(_objects())
    manager = _manager(tmp_path, gcs, auth_mode="iap", copy_tool_buckets="drop-dev,landing-dev")
    plan = manager.plan(project="p", operation="copy", source_bucket="drop-dev", source_prefix="out",
                        destination_bucket="value-gcp-prod", destination_prefix="in")
    assert any("gs://value-gcp-prod is not one of the buckets" in p for p in plan.problems)
    assert gcs.calls == []  # nothing was even listed


def test_copy_skips_existing_files_unless_overwrite(tmp_path):
    gcs = FakeGcs(_objects())
    manager = _manager(tmp_path, gcs)
    plan = manager.plan(project="p", operation="copy", source_bucket="drop-dev", source_prefix="out",
                        pattern="Daily File_*.csv", destination_bucket="landing-dev", destination_prefix="in")
    job = manager.start(plan, project="p", user=USER, identity="me@acme.com")
    _wait(job)
    assert job.status == "done" and job.counts()["copied"] == 1 and job.counts()["skipped"] == 1
    assert gcs.objects["landing-dev"]["in/Daily File_20260114.csv"] == b"older copy"  # not overwritten
    assert gcs.objects["landing-dev"]["in/Daily File_20260115.csv"] == b"a,b\n3,4\n"
    assert "out/Daily File_20260114.csv" in gcs.objects["drop-dev"]  # a copy keeps the source
    again = manager.plan(project="p", operation="copy", source_bucket="drop-dev", source_prefix="out",
                         pattern="Daily File_*.csv", destination_bucket="landing-dev", destination_prefix="in", overwrite=True)
    job2 = manager.start(again, project="p", user=USER, identity="me@acme.com")
    _wait(job2)
    assert job2.counts()["copied"] == 2 and gcs.objects["landing-dev"]["in/Daily File_20260114.csv"] == b"a,b\n1,2\n"


def test_move_deletes_the_sources_and_the_job_is_recorded(tmp_path):
    gcs = FakeGcs(_objects())
    manager = _manager(tmp_path, gcs)
    plan = manager.plan(project="p", operation="move", source_bucket="drop-dev", source_prefix="out",
                        pattern="Daily File_*.csv", destination_bucket="landing-dev", destination_prefix="in", overwrite=True)
    job = manager.start(plan, project="p", user=USER, identity="me@acme.com")
    _wait(job)
    assert job.status == "done" and job.counts()["moved"] == 2
    assert not any(n.startswith("out/Daily") for n in gcs.objects["drop-dev"])
    assert "out/old/Daily File_20251231.csv" in gcs.objects["drop-dev"]  # not selected: untouched
    recent = manager.recent(USER)
    assert recent[0]["id"] == job.id and recent[0]["operation"] == "move" and recent[0]["counts"]["moved"] == 2
    assert recent[0]["user"] == USER and recent[0]["identity"] == "me@acme.com"
    assert manager.recent("bob@example.com") == []


def test_failures_are_reported_per_file(tmp_path):
    gcs = FakeGcs(_objects())
    manager = _manager(tmp_path, gcs)
    plan = manager.plan(project="p", operation="copy", source_bucket="drop-dev", source_prefix="out",
                        pattern="Daily File_*.csv", destination_bucket="landing-dev", destination_prefix="new")
    gcs.forbidden.add("landing-dev")  # access lost after the plan was made
    job = manager.start(plan, project="p", user=USER, identity="me")
    _wait(job)
    assert job.status == "failed" and job.counts()["failed"] == 2
    assert "403" in job.record()["failures"][0]["error"]


def test_one_running_job_per_user(tmp_path):
    gcs = FakeGcs(_objects())
    manager = _manager(tmp_path, gcs)
    plan = manager.plan(project="p", operation="copy", source_bucket="drop-dev", source_prefix="out",
                        destination_bucket="landing-dev", destination_prefix="x")
    job = manager.start(plan, project="p", user=USER, identity="me")
    _wait(job)
    job.status = "running"  # pretend it is still busy
    with pytest.raises(CopyRefused, match="still running"):
        manager.start(plan, project="p", user=USER, identity="me")
    job.status = "done"


def test_upload_from_your_computer(tmp_path):
    from app.services.copy_tool import LocalFile, new_temp_dir

    gcs = FakeGcs(_objects())
    manager = _manager(tmp_path, gcs)
    staging = new_temp_dir("upload")
    files = []
    for name, data in (("Daily File_20260114.csv", b"new"), ("extra.txt", b"hello")):
        (staging / name).write_bytes(data)
        files.append(LocalFile(name=name, size=len(data), path=str(staging / name)))
    plan = manager.plan_upload(project="p", files=files, destination_bucket="landing-dev", destination_prefix="in",
                               staging_dir=str(staging))
    assert plan.problems == [] and plan.source_uri() == "your computer"
    assert [(i.destination, i.exists) for i in plan.items] == [("in/Daily File_20260114.csv", True), ("in/extra.txt", False)]
    job = manager.start(plan, project="p", user=USER, identity="me")
    _wait(job)
    assert job.status == "done" and job.message == "1 file(s) uploaded, 1 skipped (already there), 0 failed"
    assert gcs.objects["landing-dev"]["in/extra.txt"] == b"hello"
    assert gcs.objects["landing-dev"]["in/Daily File_20260114.csv"] == b"older copy"  # skipped, not overwritten
    assert not staging.exists()  # the hub's copy is removed once the job ends
    assert manager.recent(USER)[0]["operation"] == "upload"
    bad = manager.plan_upload(project="p", files=files * 2, destination_bucket="", destination_prefix="")
    assert "choose the destination bucket" in bad.problems and any("same name" in p for p in bad.problems)


def test_download_to_your_computer(tmp_path):
    import zipfile

    gcs = FakeGcs(_objects())
    manager = _manager(tmp_path, gcs)
    plan = manager.plan_download(project="p", source_bucket="drop-dev", source_prefix="out", pattern="*.csv", recursive=True)
    assert plan.problems == [] and plan.destination_uri() == "your computer"
    assert [i.destination for i in plan.items] == ["Daily File_20260114.csv", "Daily File_20260115.csv",
                                                   "old/Daily File_20251231.csv"]
    job = manager.start(plan, project="p", user=USER, identity="me")
    _wait(job)
    assert job.status == "done" and job.message == "3 file(s) downloaded, 0 failed"
    assert job.download_path.endswith(".zip")
    with zipfile.ZipFile(job.download_path) as zf:
        assert sorted(zf.namelist()) == ["Daily File_20260114.csv", "Daily File_20260115.csv", "old/Daily File_20251231.csv"]
        assert zf.read("old/Daily File_20251231.csv") == b"x"
    assert "out/Daily File_20260114.csv" in gcs.objects["drop-dev"]  # a download never changes the bucket

    one = manager.plan_download(project="p", source_bucket="drop-dev", source_prefix="out", names=["readme.txt"])
    job = manager.start(one, project="p", user=USER, identity="me")
    _wait(job)
    assert job.download_path.endswith("readme.txt") and open(job.download_path, "rb").read() == b"hi"  # one file, no ZIP


def test_download_and_upload_limits(tmp_path):
    gcs = FakeGcs({"drop-dev": {"big.bin": b"x" * 2048}})
    manager = _manager(tmp_path, gcs, copy_tool_max_download_mb=0)
    plan = manager.plan_download(project="p", source_bucket="drop-dev")
    assert any("more than a download may hold" in p for p in plan.problems)
    deployed = _manager(tmp_path, gcs, auth_mode="iap", copy_tool_buckets="landing-dev")
    assert any("not one of the buckets" in p for p in deployed.plan_download(project="p", source_bucket="drop-dev").problems)


def test_copy_api(tmp_path):
    from fastapi.testclient import TestClient

    from app.deps import Services
    from app.main import create_app
    from app.services.sql_analyzer import SqlAnalyzer

    gcs = FakeGcs(_objects())
    settings = Settings(auth_mode="local", dev_user_email=USER, local_workspace_dir=tmp_path, gcp_project="acme-dev",
                        smoke_run_enabled=False, gemini_enabled=False, log_format="text", ui_enabled=False)
    services = Services(settings=settings, store=WorkspaceStore(LocalBackend(tmp_path)), gemini=None,
                        analyzer=SqlAnalyzer(None), clients=ClientFactory(gcs=gcs))
    client = TestClient(create_app(settings, services))
    assert client.get("/api/copy/scope").json()["scope"] == "any"
    body = {"operation": "copy", "source_bucket": "drop-dev", "source_prefix": "out", "pattern": "*.csv",
            "destination_bucket": "landing-dev", "destination_prefix": "in", "overwrite": False}
    plan = client.post("/api/copy/plan", json=body).json()
    assert plan["files"] == 2 and plan["existing"] == 1 and plan["problems"] == []
    job = client.post("/api/copy/jobs", json=body).json()
    for _ in range(200):
        snap = client.get(f"/api/copy/jobs/{job['id']}").json()
        if snap["status"] != "running":
            break
        time.sleep(0.02)
    assert snap["status"] == "done" and snap["counts"]["copied"] == 1 and snap["counts"]["skipped"] == 1
    assert client.get("/api/copy/jobs").json()[0]["id"] == job["id"]
    bad = client.post("/api/copy/jobs", json=dict(body, destination_bucket="drop-dev", destination_prefix="out"))
    assert bad.status_code == 409 and "source folder" in bad.json()["detail"]["problems"][0]
    assert client.get(f"/api/copy/jobs/{job['id']}", headers={"x-dev-user": "bob@example.com"}).status_code == 404

    def finished(job_id):
        for _ in range(200):
            snap = client.get(f"/api/copy/jobs/{job_id}").json()
            if snap["status"] != "running":
                return snap
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    up = client.post("/api/copy/upload", data={"destination_bucket": "landing-dev", "destination_prefix": "uploads"},
                     files=[("files", ("a.csv", b"1,2\n", "text/csv")), ("files", ("b.csv", b"3,4\n", "text/csv"))])
    assert up.status_code == 201, up.text
    assert finished(up.json()["id"])["counts"]["copied"] == 2 and gcs.objects["landing-dev"]["uploads/b.csv"] == b"3,4\n"
    down = client.post("/api/copy/download", json={"source_bucket": "landing-dev", "source_prefix": "uploads"}).json()
    assert finished(down["id"])["download"].endswith(".zip")
    got = client.get(f"/api/copy/jobs/{down['id']}/file")
    assert got.status_code == 200 and got.content[:2] == b"PK"
    assert client.get(f"/api/copy/jobs/{down['id']}/file", headers={"x-dev-user": "bob@example.com"}).status_code == 404
