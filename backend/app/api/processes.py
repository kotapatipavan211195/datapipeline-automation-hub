"""Shared workspace: list, create, autosave drafts, save versions, clone, co-owners, download, delete."""

from __future__ import annotations

import datetime as dt

import io
import json
import zipfile
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field, ValidationError

from ..auth import CurrentUser
from ..deps import Services, current_user, parse_spec, services
from ..models.migrate import upgrade_spec
from ..models.spec import ProcessSpec, format_validation_errors
from ..services.docs_writer import ai_overview
from ..services.packager import build_zip, bundle_stem
from ..services.renderer import RenderError, render_process

router = APIRouter(prefix="/api/processes", tags=["processes"])


class CreateRequest(BaseModel):
    spec: dict[str, Any]


class DraftRequest(BaseModel):
    spec: dict[str, Any]


class VersionRequest(BaseModel):
    spec: dict[str, Any]
    note: str = ""
    validation: dict[str, Any] | None = None


class CloneRequest(BaseModel):
    new_name: str
    version: int | None = None


class CoOwnersRequest(BaseModel):
    co_owners: list[str] = Field(default_factory=list)


@router.get("")
def list_processes(
    scope: str = Query("all", pattern="^(all|mine)$"),
    q: str = "",
    user: CurrentUser = Depends(current_user),
    svc: Services = Depends(services),
) -> list[dict]:
    return [s.model_dump() for s in svc.store.list_processes(user=user.email, scope=scope, query=q)]


@router.post("", status_code=201)
def create_process(body: CreateRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    spec = dict(body.spec)
    md = dict(spec.get("metadata") or {})
    md["owner"] = user.email
    spec["metadata"] = md
    meta = svc.store.create_process(user.email, spec)
    return {"meta": meta.model_dump(), "can_edit": True}


@router.post("/import")
async def import_spec(file: UploadFile = File(...), user: CurrentUser = Depends(current_user)) -> dict:
    """Read spec.json (or a Datapipeline Automation Hub ZIP containing it) so the wizard can open it as a new process."""
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "file too large")
    try:
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                name = next((n for n in zf.namelist() if n.endswith("spec.json")), None)
                if not name:
                    raise HTTPException(422, "the ZIP does not contain spec.json (bundles leave it out since 3.1): "
                                             "download the spec from the process, or bring the script in with "
                                             "'Enhance a script'")
                raw = zf.read(name)
        data = json.loads(raw)
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(422, f"not a Datapipeline Automation Hub spec.json or ZIP: {exc}") from exc
    data, notes = upgrade_spec(data) if isinstance(data, dict) else (data, [])
    errors: list[dict] = []
    try:
        ProcessSpec.model_validate(data)
    except ValidationError as exc:
        errors = format_validation_errors(exc)
    return {"spec": data, "errors": errors, "migration_notes": notes}


@router.delete("/{owner}/{name}", status_code=204)
def delete_process(owner: str, name: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> Response:
    """Delete a process with its drafts and saved versions (owner only; it cannot be undone)."""
    svc.store.delete_process(owner, name, user.email)
    return Response(status_code=204)


@router.get("/{owner}/{name}")
def get_process(owner: str, name: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    meta, _ = svc.store.get_meta(owner, name)
    can_edit = svc.store.can_edit(meta, user.email)
    latest = svc.store.get_version(owner, name, meta.latest_version) if meta.latest_version else None
    draft = svc.store.get_draft(owner, name, user.email) if can_edit else None
    if draft is None and can_edit and not latest:
        draft = svc.store.get_draft(owner, name, meta.owner)
    return {"meta": meta.model_dump(), "can_edit": can_edit, "latest": latest, "draft": draft}


@router.put("/{owner}/{name}/draft")
def save_draft(owner: str, name: str, body: DraftRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    saved_at = svc.store.save_draft(owner, name, user.email, body.spec)
    errors: list[dict] = []
    try:
        ProcessSpec.model_validate(body.spec)
    except ValidationError as exc:
        errors = format_validation_errors(exc)
    return {"saved_at": saved_at, "errors": errors}


@router.delete("/{owner}/{name}/draft", status_code=204)
def discard_draft(owner: str, name: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> Response:
    svc.store.discard_draft(owner, name, user.email)
    return Response(status_code=204)


@router.post("/{owner}/{name}/versions", status_code=201)
def save_version(owner: str, name: str, body: VersionRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    meta, _ = svc.store.get_meta(owner, name)
    if not svc.store.can_edit(meta, user.email):
        raise HTTPException(403, "Only the owner and co-owners can save versions. Clone the process to make your own copy.")
    spec = parse_spec(body.spec)
    if spec.metadata.name != name:
        raise HTTPException(422, "the process name cannot change between versions; clone it under the new name instead")
    if not spec.metadata.created_on:  # header "Created on": the first saved version
        spec.metadata.created_on = dt.date.today().isoformat()
    try:
        result = render_process(spec, generated_by=user.email, ai_overview=ai_overview(spec, svc.gemini),
                                adc_email=svc.adc_email(), smoke_run=svc.settings.smoke_run_enabled, settings=svc.settings)
    except RenderError as exc:
        raise HTTPException(422, {"message": "Generation failed", "errors": [{"path": "", "message": p} for p in exc.problems]}) from exc
    run = svc.test_runs.latest_for(owner, name)
    tested = run if run is not None and not run.active and run.spec_hash == spec.spec_hash() else None
    info = svc.store.save_version(
        owner, name, user.email,
        spec=json.loads(spec.model_dump_json()), spec_hash=spec.spec_hash(), files=result.files,
        validation=body.validation, note=body.note,
        checks_ok=bool(body.validation.get("ok")) if body.validation else True,
        test_run=tested.record() if tested else None, test_run_log=tested.log_file() if tested else None,
    )
    svc.store.discard_draft(owner, name, user.email)
    return {"version": info.model_dump(), "checks": [c.__dict__ for c in result.checks], "warnings": result.warnings}


@router.get("/{owner}/{name}/versions/{version}")
def get_version(owner: str, name: str, version: int, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    return svc.store.get_version(owner, name, version)


@router.get("/{owner}/{name}/versions/{version}/zip")
def download_version(owner: str, name: str, version: int, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> Response:
    files = svc.store.get_version_files(owner, name, version)
    stem = bundle_stem(files, name)
    log = svc.store.get_version_log(owner, name, version)
    if log:  # the lab test run saved with this version sits next to the script
        files[f"{stem}_lab_test_run.log"] = log
    return Response(
        build_zip(stem, files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}_v{version}.zip"'},
    )


@router.get("/{owner}/{name}/versions/{version}/files/{path:path}", response_class=PlainTextResponse)
def version_file(owner: str, name: str, version: int, path: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> str:
    files = svc.store.get_version_files(owner, name, version)
    if path not in files:
        raise HTTPException(404, "file not found")
    return files[path]


@router.post("/{owner}/{name}/clone", status_code=201)
def clone(owner: str, name: str, body: CloneRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    meta = svc.store.clone(owner, name, body.version, user.email, body.new_name)
    return {"meta": meta.model_dump(), "can_edit": True}


@router.put("/{owner}/{name}/co-owners")
def set_co_owners(owner: str, name: str, body: CoOwnersRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    return svc.store.set_co_owners(owner, name, user.email, body.co_owners).model_dump()
