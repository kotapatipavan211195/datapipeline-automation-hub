"""Schema sources for the ingest step: sample file (upload or GCS), BigQuery JSON, CREATE TABLE DDL, existing table."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..auth import CurrentUser
from ..deps import Services, current_user, services
from ..models.schema import columns_to_bq_json
from ..services.gcp_tools import infer_from_gcs, infer_from_upload
from ..services.schema_infer import InferResult, columns_from_bq_table, columns_from_ddl, columns_from_json_text
from ..services.validators import gcp_error_text

router = APIRouter(prefix="/api/schema", tags=["schema"])


class GcsInferRequest(BaseModel):
    uri: str
    file_format: str = "CSV"
    delimiter: str | None = None
    has_header: bool = True
    project_id: str


class DdlRequest(BaseModel):
    ddl: str


class JsonRequest(BaseModel):
    text: str


class TableRequest(BaseModel):
    table: str
    project_id: str


def _out(result: InferResult) -> dict:
    return {
        "columns": [c.model_dump() for c in result.columns],
        "sample_rows": result.sample_rows,
        "warnings": result.warnings,
        "detected": result.detected,
        "bq_json": columns_to_bq_json(result.columns),
    }


@router.post("/infer")
async def infer_upload(
    file: UploadFile = File(...),
    file_format: str = Form("CSV"),
    delimiter: str | None = Form(None),
    has_header: bool = Form(True),
    user: CurrentUser = Depends(current_user),
    svc: Services = Depends(services),
) -> dict:
    limit = svc.settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    try:
        return _out(infer_from_upload(data, file.filename or "", file_format=file_format, delimiter=delimiter,
                                      has_header=has_header, limit=limit))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/infer-gcs")
def infer_gcs(body: GcsInferRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    try:
        return _out(infer_from_gcs(svc.clients, project_id=body.project_id, uri=body.uri, file_format=body.file_format,
                                   delimiter=body.delimiter, has_header=body.has_header, sample_bytes=svc.settings.sample_read_bytes))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, gcp_error_text(exc)) from exc


@router.post("/from-ddl")
def from_ddl(body: DdlRequest, user: CurrentUser = Depends(current_user)) -> dict:
    try:
        cols = columns_from_ddl(body.ddl)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"columns": [c.model_dump() for c in cols], "warnings": []}


@router.post("/parse-json")
def parse_json(body: JsonRequest, user: CurrentUser = Depends(current_user)) -> dict:
    try:
        cols = columns_from_json_text(body.text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"columns": [c.model_dump() for c in cols], "warnings": []}


@router.post("/from-table")
def from_table(body: TableRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    try:
        cols = columns_from_bq_table(svc.clients.bigquery(body.project_id), body.table.strip("`"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, gcp_error_text(exc)) from exc
    return {"columns": [c.model_dump() for c in cols], "warnings": []}
