"""Enhance a script: analyse an existing script (+ its YAML files), convert it into a process, or rewrite it with
Gemini under the hub's guards. Nothing here touches GCP."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..deps import Services, current_user, services
from ..models.enhance import EnhanceHeader
from ..models.rules import PROCESS_NAME_RE
from ..services.script_convert import ConvertError, to_spec
from ..services.script_rewrite import rewrite_script
from ..services.script_scan import scan_script

router = APIRouter(prefix="/api/enhance", tags=["enhance"])


class ScriptRequest(BaseModel):
    file_name: str = "script.py"
    source: str = Field(description="the Python script")
    config_yaml: str | None = Field(default=None, description="its YAML (gcp_projects), if any")
    schema_yaml: str | None = Field(default=None, description="its table schema YAML (-ts), if any")
    header: EnhanceHeader | None = Field(default=None, description="the header block (authors, reviewers, JIRA, "
                                         "description, updates); default: the old script's header")


class ConvertRequest(ScriptRequest):
    process_name: str


def _scan(body: ScriptRequest, svc: Services):
    limit = svc.settings.enhance_max_script_kb * 1024
    if len(body.source.encode("utf-8")) > limit:
        raise HTTPException(413, f"the script is larger than {svc.settings.enhance_max_script_kb} KB")
    return scan_script(body.source, body.file_name, config_yaml=body.config_yaml, schema_yaml=body.schema_yaml,
                       settings=svc.settings)


@router.post("/analyze")
def analyze(body: ScriptRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    return _scan(body, svc).model_dump()


@router.post("/convert")
def convert(body: ConvertRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    """The process spec the wizard opens (POST it to /api/processes to create the process), with review notes."""
    if not PROCESS_NAME_RE.match(body.process_name):
        raise HTTPException(422, "process_name must be lower_snake_case, 3-63 characters")
    try:
        spec, notes = to_spec(_scan(body, svc), process_name=body.process_name, owner=user.email, header=body.header)
    except ConvertError as exc:
        raise HTTPException(422, {"message": "The script cannot become a process; use /api/enhance/rewrite",
                                  "problems": exc.problems}) from exc
    return {"spec": spec, "notes": notes}


@router.post("/rewrite")
def rewrite(body: ScriptRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    """Gemini's rewrite to the team standard; ``ok`` is false (and no files are returned) when a guard fails."""
    analysis = _scan(body, svc)
    return rewrite_script(analysis, body.source, svc.gemini, config_yaml=body.config_yaml, settings=svc.settings,
                          header=body.header).model_dump()
