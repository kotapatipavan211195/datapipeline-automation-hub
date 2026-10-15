"""Browse buckets/prefixes and preview which files an ingestion step would load (lab values)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..deps import Services, current_user, services
from ..models.rules import resolve_template, template_problems
from ..services.gcp_tools import gcs_browse, gcs_match
from ..services.validators import gcp_error_text

router = APIRouter(prefix="/api/gcs", tags=["gcs"])


class MatchRequest(BaseModel):
    project_id: str  # project the listing runs in (lab write_project)
    bucket: str
    folder: str = ""
    filename: str = ""
    extensions: list[str] = Field(default_factory=lambda: [".csv"])
    variables: dict[str, str] = Field(default_factory=dict)  # name -> lab value
    exclude_prefix: str = ""


@router.get("/browse")
def browse(
    project_id: str,
    bucket: str = "",
    prefix: str = "",
    user: CurrentUser = Depends(current_user),
    svc: Services = Depends(services),
) -> dict:
    try:
        return gcs_browse(svc.clients, project_id=project_id, bucket=bucket, prefix=prefix)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, gcp_error_text(exc)) from exc


@router.post("/match")
def match(body: MatchRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    """Files a file-ingestion step would load right now in lab (name contains `filename`)."""
    return gcs_match(
        svc.clients, project_id=body.project_id, bucket=body.bucket, folder=body.folder, filename=body.filename,
        extensions=tuple(body.extensions), variables=body.variables, exclude_prefix=body.exclude_prefix,
    )


@router.get("/preview-path")
def preview_path(
    template: str,
    variables: str = Query("", description="name=value pairs (lab values) separated by &"),
    user: CurrentUser = Depends(current_user),
) -> dict:
    values: dict[str, str] = {}
    for pair in filter(None, variables.split("&")):
        k, _, v = pair.partition("=")
        values[k] = v
    problems = template_problems(template, values, allow_date=True)
    if problems:
        return {"ok": False, "problems": problems}
    return {"ok": True, "resolved": resolve_template(template, values)}
