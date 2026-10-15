"""Find files: search buckets for a file name or pattern at any folder depth (read-only; the Copy files bucket rule)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..deps import Services, current_user, services
from ..services.file_search import search_files

router = APIRouter(prefix="/api/files", tags=["find files"])


class SearchRequest(BaseModel):
    buckets: str | list[str] = Field(description="bucket names or gs://bucket/folder/ paths, comma separated or a list")
    pattern: str = Field(description="text the file name contains, or a pattern such as *.csv or inbound/*.txt")
    case_sensitive: bool = False


@router.post("/search")
def search(body: SearchRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    """Every matching file with its gs:// path, size and last update; problems explain buckets that were not searched."""
    return search_files(svc.clients, svc.settings, project=svc.copy_project(), buckets=body.buckets, pattern=body.pattern,
                        case_sensitive=body.case_sensitive).as_dict()
