"""Fast, local (no GCP) checks while the wizard is being edited: spec errors, order warnings, main() preview."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError

from ..auth import CurrentUser
from ..deps import current_user
from ..models.rules import function_name_problem, resolve_template, suggest_function_name, template_problems
from ..models.spec import ProcessSpec, format_validation_errors
from ..services.renderer import render_main_preview
from ..services.validators import order_checks

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class CheckRequest(BaseModel):
    spec: dict[str, Any]


class MainPreviewRequest(BaseModel):
    steps: list[dict[str, Any]] = Field(default_factory=list)
    variables: list[dict[str, Any]] = Field(default_factory=list)


class PathPreviewRequest(BaseModel):
    template: str
    variables: dict[str, str] = Field(default_factory=dict)  # lab values


class NameRequest(BaseModel):
    base: str
    taken: list[str] = Field(default_factory=list)


@router.post("/check")
def check(body: CheckRequest, user: CurrentUser = Depends(current_user)) -> dict:
    steps = body.spec.get("steps") or []
    name_problems = {}
    seen: set[str] = set()
    for i, s in enumerate(steps):
        fn = str(s.get("function_name") or "")
        problem = function_name_problem(fn) or (f"'{fn}' is used by another step" if fn in seen else None)
        if problem:
            name_problems[i] = problem
        seen.add(fn)
    main_code = render_main_preview(steps, body.spec.get("variables") or [],
                                    bool((body.spec.get("email") or {}).get("enabled")))
    try:
        spec = ProcessSpec.model_validate(body.spec)
    except ValidationError as exc:
        return {"valid": False, "errors": format_validation_errors(exc), "name_problems": name_problems, "order": [], "main": main_code}
    order = [i.model_dump() for i in order_checks(spec)]
    return {"valid": True, "errors": [], "name_problems": name_problems, "order": order, "main": main_code}


@router.post("/preview-main")
def preview_main(body: MainPreviewRequest, user: CurrentUser = Depends(current_user)) -> dict:
    return {"code": render_main_preview(body.steps, body.variables)}


@router.post("/preview-path")
def preview_path(body: PathPreviewRequest, user: CurrentUser = Depends(current_user)) -> dict:
    problems = template_problems(body.template, body.variables, allow_date=True)
    if problems:
        return {"ok": False, "problems": problems}
    try:
        return {"ok": True, "resolved": resolve_template(body.template, body.variables)}
    except (KeyError, ValueError) as exc:
        return {"ok": False, "problems": [str(exc)]}


@router.post("/suggest-name")
def suggest_name(body: NameRequest, user: CurrentUser = Depends(current_user)) -> dict:
    return {"name": suggest_function_name(body.base, body.taken)}
