"""SQL analysis (sqlglot + Gemini with the equivalence guard), dry-runs and formatting."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..deps import Services, current_user, services
from ..models.analysis import ProjectScan, SqlAnalysis
from ..models.spec import Parameter
from ..services import sql_utils
from ..services.gcp_tools import dry_run_sql
from ..services.sql_analyzer import scan_projects

router = APIRouter(prefix="/api/sql", tags=["sql"])


class AnalyzeRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=500_000)
    variables: dict[str, str] = Field(default_factory=dict)  # name -> lab value
    env_values: dict[str, dict[str, str]] = Field(default_factory=dict)  # env -> {name -> value} (value, qa)
    taken_function_names: list[str] = Field(default_factory=list)
    use_ai: bool = True


class TablesRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=500_000)
    variables: dict[str, str] = Field(default_factory=dict)  # name -> lab value
    env_values: dict[str, dict[str, str]] = Field(default_factory=dict)  # env -> {name -> value} (value, qa)
    step: str = ""


class DryRunRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=500_000)
    project_id: str  # project the dry-run job runs in (lab write_project)
    location: str = "US"
    variables: dict[str, str] = Field(default_factory=dict)  # name -> lab value
    parameters: list[Parameter] = Field(default_factory=list)


class FormatRequest(BaseModel):
    sql: str


@router.post("/analyze", response_model=SqlAnalysis)
def analyze(body: AnalyzeRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> SqlAnalysis:
    return svc.analyzer.analyze(
        body.sql,
        existing_variables=body.variables,
        taken_function_names=body.taken_function_names,
        use_ai=body.use_ai,
        env_values=body.env_values,
        defaults=svc.settings.default_projects(),
    )


@router.post("/tables", response_model=ProjectScan)
def tables(body: TablesRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> ProjectScan:
    """The automatic scan the wizard runs on paste: tables read/written, and projects turned into ${variables}."""
    return scan_projects(body.sql, body.variables, step=body.step, env_values=body.env_values,
                         defaults=svc.settings.default_projects())


@router.post("/dry-run")
def dry_run(body: DryRunRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    if not svc.settings.gcp_checks_enabled:
        return {"ok": False, "error": "BigQuery checks are disabled on this server"}
    return dry_run_sql(
        svc.clients, sql=body.sql, project_id=body.project_id, location=body.location,
        variables=body.variables, parameters=body.parameters,
    )


@router.post("/format")
def format_sql(body: FormatRequest, user: CurrentUser = Depends(current_user)) -> dict:
    return {"sql": sql_utils.pretty(body.sql)}
