"""Validate-all (lab only), generate (preview) and ZIP download for an unsaved spec."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..auth import CurrentUser
from ..deps import Services, current_user, parse_spec, services
from ..services.docs_writer import ai_overview
from ..services.packager import build_zip
from ..services.renderer import RenderError, RenderResult, render_process

router = APIRouter(prefix="/api", tags=["generate"])


class ValidateRequest(BaseModel):
    spec: dict[str, Any]
    gcp_checks: bool = True


class GenerateRequest(BaseModel):
    spec: dict[str, Any]
    smoke_run: bool | None = None  # run the script offline against fake Google clients (default: hub setting)
    ai_readme: bool = True


@router.post("/validate")
def validate(body: ValidateRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    spec = parse_spec(body.spec)
    report = svc.validator().validate(spec, gcp_checks=body.gcp_checks and svc.settings.gcp_checks_enabled)
    return report.model_dump()


def _render(body: GenerateRequest, user: CurrentUser, svc: Services) -> RenderResult:
    spec = parse_spec(body.spec)
    smoke = svc.settings.smoke_run_enabled if body.smoke_run is None else body.smoke_run
    overview = ai_overview(spec, svc.gemini) if body.ai_readme else ""
    try:
        return render_process(spec, generated_by=user.email, ai_overview=overview, adc_email=svc.adc_email(),
                              smoke_run=smoke, settings=svc.settings)
    except RenderError as exc:
        detail = {"message": "Generation failed", "errors": [{"path": "", "message": p} for p in exc.problems]}
        if exc.result is not None:
            detail["checks"] = [c.__dict__ for c in exc.result.checks]
        raise HTTPException(422, detail) from exc


@router.post("/generate")
def generate(body: GenerateRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    result = _render(body, user, svc)
    return {
        "process": result.process,
        "script": result.script,
        "files": [{"path": p, "content": c, "language": result.language_of(p)} for p, c in sorted(result.files.items())],
        "checks": [c.__dict__ for c in result.checks],
        "warnings": result.warnings,
    }


@router.post("/generate/zip")
def generate_zip(body: GenerateRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> Response:
    result = _render(body, user, svc)
    return Response(
        build_zip(result.script, result.files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{result.script}.zip"'},
    )
