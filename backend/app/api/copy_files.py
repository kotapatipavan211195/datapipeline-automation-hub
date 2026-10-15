"""The Copy files tool: bucket → bucket (copy / move), your computer → bucket (upload) and bucket → your computer
(download), as the hub's ADC identity (see services/copy_tool.py for which buckets a local or a deployed hub may use)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..auth import CurrentUser
from ..deps import Services, current_user, services
from ..services.copy_tool import CopyRefused, LocalFile, new_temp_dir

router = APIRouter(prefix="/api/copy", tags=["copy files"])


class CopyRequest(BaseModel):
    operation: Literal["copy", "move"] = "copy"
    source_bucket: str
    source_prefix: str = ""
    names: list[str] | None = None  # files relative to source_prefix; empty = every file matching pattern
    pattern: str = "*"
    recursive: bool = False
    destination_bucket: str
    destination_prefix: str = ""
    overwrite: bool = False


class DownloadRequest(BaseModel):
    source_bucket: str
    source_prefix: str = ""
    names: list[str] | None = None
    pattern: str = "*"
    recursive: bool = False


def _start(plan, user: CurrentUser, svc: Services) -> dict:
    try:
        job = svc.copy_jobs.start(plan, project=svc.copy_project(), user=user.email, identity=svc.adc_email())
    except CopyRefused as exc:
        raise HTTPException(409, {"message": "The copy cannot start", "problems": exc.problems}) from exc
    return job.snapshot()


def _plan(body: CopyRequest, svc: Services):
    return svc.copy_jobs.plan(project=svc.copy_project(), **body.model_dump())


@router.get("/scope")
def scope(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    policy = svc.copy_jobs.policy()
    return {"scope": policy.scope, "buckets": policy.patterns, "unavailable": policy.unavailable(),
            "identity": svc.adc_email(), "description": policy.describe(svc.adc_email())}


@router.post("/plan")
def plan(body: CopyRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    """What a job would copy where (nothing is changed)."""
    p = _plan(body, svc)
    return {**p.summary(), "items": [vars(i) for i in p.items[:1000]]}


@router.post("/jobs", status_code=201)
def start(body: CopyRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    """Bucket → bucket (copy or move)."""
    return _start(_plan(body, svc), user, svc)


@router.post("/upload", status_code=201)
async def upload(files: list[UploadFile] = File(...), destination_bucket: str = Form(...), destination_prefix: str = Form(""),
                 overwrite: bool = Form(False), user: CurrentUser = Depends(current_user),
                 svc: Services = Depends(services)) -> dict:
    """Your computer → bucket: the files are kept by the hub until the job has written them, then removed."""
    limit = svc.settings.copy_tool_max_upload_mb * 1024 * 1024
    staging = new_temp_dir("upload")
    local = []
    for f in files:
        name = Path(f.filename or "file").name
        data = await f.read()
        if len(data) > limit:
            raise HTTPException(413, f"{name} is larger than {svc.settings.copy_tool_max_upload_mb} MB")
        (staging / name).write_bytes(data)
        local.append(LocalFile(name=name, size=len(data), path=str(staging / name)))
    plan = svc.copy_jobs.plan_upload(project=svc.copy_project(), files=local, destination_bucket=destination_bucket,
                                     destination_prefix=destination_prefix, overwrite=overwrite, staging_dir=str(staging))
    return _start(plan, user, svc)


@router.post("/download", status_code=201)
def download(body: DownloadRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    """Bucket → your computer: when the job is done, GET /api/copy/jobs/{id}/file returns the file (or a ZIP)."""
    return _start(svc.copy_jobs.plan_download(project=svc.copy_project(), **body.model_dump()), user, svc)


@router.get("/jobs/{job_id}/file")
def download_file(job_id: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> FileResponse:
    job = svc.copy_jobs.get(job_id)
    if job is None or job.user != user.email or not job.download_path or not Path(job.download_path).is_file():
        raise HTTPException(404, "no download for this job (downloads are kept for an hour)")
    return FileResponse(job.download_path, filename=Path(job.download_path).name)


@router.get("/jobs")
def recent(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> list[dict]:
    return svc.copy_jobs.recent(user.email)


@router.get("/jobs/{job_id}")
def get(job_id: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    job = svc.copy_jobs.get(job_id)
    if job is None or job.user != user.email:
        raise HTTPException(404, "copy job not found (running jobs are kept in memory; finished ones are under GET /api/copy/jobs)")
    return job.snapshot()


@router.delete("/jobs/{job_id}")
def cancel(job_id: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    job = svc.copy_jobs.cancel(job_id, user.email)
    if job is None or job.user != user.email:
        raise HTTPException(404, "copy job not found")
    return job.snapshot()
