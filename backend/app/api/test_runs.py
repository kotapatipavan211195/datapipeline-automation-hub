"""Lab test runs: run a generated script for real in the lab (dev) environment and follow its log.

There is no environment parameter: the hub only ever runs lab, with a lab-only YAML.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..auth import CurrentUser
from ..deps import Services, current_user, parse_spec, services
from ..services.test_runner import TestRunRefused, side_effects

router = APIRouter(prefix="/api/test-runs", tags=["test runs"])


class TestRunRequest(BaseModel):
    __test__ = False
    owner: str
    name: str
    spec: dict[str, Any]


@router.post("/plan")
def plan(body: TestRunRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    """What a lab test run would change, and why it could not start (if so)."""
    spec = parse_spec(body.spec)
    adc = svc.adc_email()
    return {"environment": "lab", "adc_email": adc, "side_effects": side_effects(spec, adc),
            "problems": svc.test_runs.check(spec, owner=body.owner, name=body.name, adc_email=adc)}


@router.post("")
def start(body: TestRunRequest, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    spec = parse_spec(body.spec)
    try:
        run = svc.test_runs.start(spec, owner=body.owner, name=body.name, user=user.email, adc_email=svc.adc_email())
    except TestRunRefused as exc:
        raise HTTPException(409, {"message": "The lab test run cannot start", "problems": exc.problems}) from exc
    return run.snapshot()


@router.get("/{run_id}")
def get(run_id: str, since: int = 0, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    run = svc.test_runs.get(run_id)
    if run is None:
        raise HTTPException(404, "test run not found (runs are kept in memory until the hub restarts)")
    return run.snapshot(since)


@router.get("/{run_id}/log", response_class=PlainTextResponse)
def log(run_id: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> PlainTextResponse:
    """The run's log as a file (<script>_lab_test_run.log)."""
    run = svc.test_runs.get(run_id)
    if run is None:
        raise HTTPException(404, "test run not found (runs are kept in memory until the hub restarts)")
    return PlainTextResponse(run.log_file(), headers={"Content-Disposition": f'attachment; filename="{run.log_name()}"'})


@router.delete("/{run_id}")
def cancel(run_id: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)) -> dict:
    run = svc.test_runs.cancel(run_id)
    if run is None:
        raise HTTPException(404, "test run not found")
    return run.snapshot()
